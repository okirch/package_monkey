#!/usr/bin/python3.11
#
# This is the second step. For a set of repositories, use libsolv to resolve all
# package dependencies.
# We allow a certain degree of ambiguity, but disambiguate dependencies that cover
# eg different versions of java.
# Output a package DB that contains the generic dependencies

import solv
import os
import re
import functools

from .util import debugmsg, infomsg, warnmsg, errormsg, loggingFacade
from .util import ThatsProgress, TableFormatter
from .options import ApplicationBase
from .preprocess import *
from .scenario import *
from .newdb import NewDB
from .reports import GenericStringReport
from .arch import *

class SolverApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.repositoryCollection = None
		self.architectures = set()
		self.promises = set()
		self.hints = None
		self.pedantic = False
		self.ignoreErrors = False

		self.resolverLog = None
		self.errorReport = GenericStringReport()

	def run(self):
		self.ignoreErrors = self.opts.ignore_errors
		self.traceDisambiguation = self.opts.trace_scenarios
		if self.opts.trace:
			self.traceDisambiguation = True

		self.loadHints()
		self.loadRepositories(withStaging = self.opts.staging)

		if self.opts.only_arch:
			archSet = set()
			for s in self.opts.only_arch:
				archSet.update(s.split(','))
		else:
			archSet = self.repositoryCollection.architectures
		self.architectures = archSet

		infomsg(f"Using the following repositories:")
		for repository in self.repositoryCollection:
			infomsg(f"   {repository}")

		self.openResolverLog()

		archSolvers = []
		for arch in sorted(archSet):
			archSolvers.append(self.createArchSolver(arch))

		totalRpmCount = sum(len(a.queue) for a in archSolvers)
		progressMeter = ThatsProgress(totalRpmCount)

		db = NewDB(traceMatcher = self.traceMatcher)
		for repository in self.repositoryCollection:
			repository.loadBuilds(db)

		for archSolver in archSolvers:
			archSolver.constrainRpms(db)
			archSolver.solve(progressMeter)
			archSolver.validateHidden()

		for archSolver in archSolvers:
			self.extractResolution(archSolver, db)

		self.collapseResults(db)
		# self.logResults(db)

		info = self.codebaseData.loadDownloadInfo()
		db.downloadTimestamp = info.timestamp

		self.saveDB(db)

		if self.errorReport:
			self.errorReport.display()
			return 1

		return 0

	def openResolverLog(self):
		if self.opts.reslog is None:
			self.opts.reslog = self.getCodebasePath("resolver.log")
		self.resolverLog = ResolverLog(self.opts.reslog)

	def loadHints(self):
		self.hints = self.modelDescription.loadPreprocessorHints()

	def loadRepositories(self, withStaging = None):
		solverDir = self.getCachePath('solve')
		self.repositoryCollection = SolverRepositoryCollection.fromCodebase(self.productCodebase, solverDir)

		if withStaging is not None:
			self.repositoryCollection.enableStaging(withStaging)

	def createArchSolver(self, arch):
		archSolver = ArchSolver(arch, hints = self.hints, traceMatcher = self.traceMatcher, errorReport = self.errorReport)

		for repository in self.repositoryCollection:
			if repository.arch == arch:
				archSolver.addRepository(repository)

		archSolver.resolverLog = self.resolverLog
		archSolver.pedantic = self.opts.pedantic
		archSolver.traceDisambiguation = self.opts.trace_scenarios

		return archSolver

	def extractResolution(self, archSolver, db):
		arch = archSolver.arch

		db.addArchitecture(arch)

		for rpm in archSolver.getAllRpms(RpmBase.TYPE_MISSING):
			genericRpm = db.createRpm(rpm.shortname)
			genericRpm.missingArchitectures.add(arch)

		for type in (RpmBase.TYPE_SYNTHETIC, RpmBase.TYPE_SCENARIO, RpmBase.TYPE_PROMISE):
			for rpm in archSolver.getAllRpms(type):
				assert(rpm.type == type)
				genericRpm = db.createRpm(rpm.shortname, type)

		unresolvedRpm = db.lookupRpm('__unresolved__')

		for result in archSolver.resolvedRpms:
			if result.requiringPkg.suppress:
				# infomsg(f"{arch}: suppress {result.requiringPkg}")
				continue

			rpmName = result.requiringPkg.shortname

			genericRpm = db.createRpm(rpmName)
			genericRpm.architectures.add(arch)

			for archSpecificDep in result:
				solution = archSpecificDep.solutions
				if not solution:
					solution = archSpecificDep.alternatives

				required = set(map(db.createRpm, (rpm.shortname for rpm in solution)))
				genericRpm.addDependencies(str(archSpecificDep.dep), arch, required,
							unresolvable = (unresolvedRpm in required))

			if result.validScenarioChoices is not None:
				genericRpm.addScenarios(arch, set(map(str, result.validScenarioChoices)))

			if result.controllingScenarios:
				genericRpm.addControllingScenarios(arch, set(map(str, result.controllingScenarios)))

			version = result.version
			if version is not None:
				genericRpm.addVersion(arch, version)

			if result.requiringPkg.isExternal:
				debugmsg(f"creating synthetic build for external rpm {result.requiringPkg}")
				build = db.createBuild(f"{rpmName}:build")
				build.addRpm(genericRpm);
				build.isSynthetic = True

	def displayBuildsWithVersionDrift(self, db):
		tableFormatter = TableFormatter(["name"] + list(map(str, self.architectures)),
					[30, 12, 12, 12, 12, 12, 12])
		for build in db.builds:
			if not build.successful:
				status = list(f"{arch}={status}" for (arch, status) in build.buildFailures)
				warnmsg(f"{build} has build failures: {' '.join(status)}")
				continue

			if build.isSynthetic:
				continue

			if self.hints and build.name in self.hints.buildNoVersionCheckSet:
				continue

			for rpm in build.rpms:
				if not rpm.isSynthetic and not rpm.versions.common:
					row = tableFormatter.addRow(rpm.name)
					for arch, vset in rpm.versions.items():
						row[arch] = f"{' '.join(vset)}"

		tableFormatter.render("The following rpms have version drift", displayfn = infomsg)

	def collapseResults(self, db):
		unresolvedRpm = db.lookupRpm('__unresolved__')

		# db.rpms returns an iterator to a dict that keeps changing (because we add
		# new promises to the DB). Use list() to create a copy
		for genericRpm in list(db.rpms):
			# FIXME: we should mark architecture as missing *only* if it was required by something
			if not genericRpm.architectures and genericRpm.missingArchitectures == db.architectures:
				infomsg(f"Looks like {genericRpm} is indeed missing")
				# fudge the rpm type:
				genericRpm._type = RpmBase.TYPE_MISSING
				genericRpm.isSynthetic = True
				continue

			if genericRpm.validScenarios and not genericRpm.validScenarios.common:
				if not self.scenarioDetective(db, genericRpm):
					warnmsg(f"{genericRpm}: problematic combination of scenarios")
					for arch in genericRpm.architectures:
						scenarios = list(genericRpm.getScenarios(arch) or [])
						infomsg(f"    - {arch}: {' '.join(map(str, scenarios))}")

			if genericRpm.solutions.allIdentical():
				unresolvableDeps = genericRpm.unresolvables.common

				if unresolvableDeps:
					unresolvableDeps = self.hints.filterUnresolvedRequirements(genericRpm.name, unresolvableDeps)

				if unresolvableDeps:
					errormsg(f"{genericRpm} has unresolvable dependencies on all architectures: {unresolvableDeps}")
				continue

			common = genericRpm.solutions.common 
			for arch in genericRpm.architectures:
				solution = genericRpm.getDependencies(arch)
				delta = solution.difference(common)

				# If a package is unresolvable on _all_ architectures, the package will show up as
				# depending on unresolvedRpm.
				# If it is unresolved on just some architectures, we have two options:
				#  - add promise:arch:__unresolved__
				#    This requires complex solver code when handling these partial unresolvables
				#  - disable the package for this architecture
				#    This will potentially leave us with dangling references to an unresolvable pkg.
				# Both options are icky.
				# The proper fix would be to trace these architectures as rpm.badarch and percolate that
				# up the depedency chain inside the resolver, or in the composer.
				if unresolvedRpm in delta:
					# FIXME: it would be nice to identify the failed dependencies and suppress any noise
					# according to the nowarn rules.
					# The following call will suppress errors if the requiring rpm is flagged as nowarn,
					# but it will not catch any nowarn required names (because the failed requirement has been
					# translated to __unresolved__ at this point).
					if self.hints.filterUnresolvedRequirements(genericRpm.name, set(map(str, delta))):
						errormsg(f"{genericRpm} is unresolvable on {arch} - disabling package on this architecture")
					genericRpm.architectures.remove(arch)
					continue

		self.displayBuildsWithVersionDrift(db)

		nAmbiguityErrors = 0

		# Try to detect when a build like rust1.99 provides rpms for just a single scenario version
		# Not all packages covered by scenarios do that; for example, build systemd-default-settings
		# spits out a bunch of rpms, for different products, and hence for different product=XXX scenarios.
		for build in db.builds:
			controllingScenarios = set()
			for rpm in build.binaries:
				rpmScenarios = functools.reduce(set.union, rpm.controllingScenarios.values(), set())
				controllingScenarios.update(rpmScenarios)

			if build.trace and controllingScenarios:
				infomsg(f"{build}: rpms covered by {' '.join(map(str, controllingScenarios))}")

			# up to this point, the scenario set contains just strings formatted as var/version/rpmname;
			# now parse it into a set of ScenarioTuples:
			controllingScenarios = ScenarioTupleSet(map(ScenarioTuple.parse, controllingScenarios))
			controllingVersions = controllingScenarios.versions

			if len(controllingVersions) == 1:
				build.controllingScenarioVersion = next(iter(controllingVersions))

		rpmMap = {}
		for build in db.builds:
			for rpm in build.rpms:
				existing = rpmMap.get(rpm)
				if existing and existing is not build:
					errormsg(f"{rpm} is referenced by two builds: {build} and {existing}")

					build = self.suggestBuildDisambiguation(rpm, build, existing)
					if build is None:
						nAmbiguityErrors += 1
						continue

					infomsg(f"   will use {build} and hope that it will win")

				rpmMap[rpm] = build

		if nAmbiguityErrors and not self.ignoreErrors:
			raise Exception(f"Encountered {nAmbiguityErrors} build ambiguity error(s)")

	def suggestBuildDisambiguation(self, rpm, buildA, buildB):
		if buildA.name.startswith(buildB.name + ":") or rpm.name == buildA.name:
			return buildA

		if buildB.name.startswith(buildA.name + ":") or rpm.name == buildB.name:
			return buildB

		if ':' in buildA.name and ':' in buildB.name:
			baseBuildA, flavorA = buildA.name.split(':', maxsplit = 1)
			baseBuildB, flavorB = buildB.name.split(':', maxsplit = 1)

			if flavorA == flavorB:
				if len(baseBuildA) < len(baseBuildA):
					return buildA
				return buildB

		# hard-coded hack
		if buildA.name == 'SDL2' and buildB.name == 'sdl2-compat':
			return buildB
		if buildA.name == 'sdl2-compat' and buildB.name == 'SDL2':
			return buildA

		return None

	def logResults(self, db):
		for genericRpm in db.rpms:
			rating = genericRpm.levelOfPerfection(db.architectures)
			if rating == 15:
				continue

			infomsg(f"{genericRpm}: {genericRpm.architectures}")
			if not (rating & 2):
				common = genericRpm.solutions.common 
				promises = set()

				infomsg(f"  packages:")
				infomsg(f"    common {' '.join(common)}")
				for arch in genericRpm.architectures:
					solution = genericRpm.getDependencies(arch)
					delta = solution.difference(common)
					if delta:
						# infomsg(f"    {arch} {' '.join(delta)}")
						# FIXME: is this really rpmName or is it an rpm object?
						for rpmName in delta:
							# FIXME: do not create a promise for rpmName if the
							# required rpm is available for all architectures of the
							# requiring rpm.
							promises.add(f"promise:{arch}:{rpmName}")

				infomsg(f"    promise {' '.join(promises)}")
				common.update(promises)

			if not (rating & 4):
				common = genericRpm.controllingScenarios.common 

				infomsg(f"  controlling scenarios:")
				infomsg(f"    common {' '.join(common)}")
				for arch in genericRpm.architectures:
					scenarios = genericRpm.getScenarios(arch)
					delta = scenarios.difference(common)
					if delta:
						infomsg(f"    {arch} {' '.join(delta)}")

			if not (rating & 8):
				common = genericRpm.validScenarios.common 

				infomsg(f"  scenario dependencies:")
				infomsg(f"    common {' '.join(common)}")
				for arch in genericRpm.architectures:
					scenarios = genericRpm.getScenarios(arch)
					delta = scenarios.difference(common)
					if delta:
						infomsg(f"    {arch} {' '.join(delta)}")

	# We get here when an rpm has a dependency on a scenario (on at least one architecture),
	# but there is no single scenario common across all architectures.
	# This can happen due to a number of reasons
	#  fwupd: requires a kernel on x86_64 but not on aarch64
	#  podman: requires a kernel on all architectures, but s390x only has kernel-default.
	#	With s390x being unambiguous, we never went to check for a scenario, we just
	#	have a dependency on kernel-default.
	#
	# podman-like cases can be fixed by trying to find that lonely kernel-default dependency and
	# converting it to scenario kernel/image (valid for kernel=default).
	# The fwupd case is probably harder to handle.
	def scenarioDetective(self, db, genericRpm):
		def displayRpmDependencies(genericRpm, msg):
			if msg:
				infomsg(f"{genericRpm}: {msg}")
			infomsg(f"   common req {' '.join(map(str, genericRpm.solutions.common))}")
			for arch in genericRpm.architectures:
				specific = genericRpm.getDependencies(arch).difference(genericRpm.solutions.common)
				infomsg(f"   {arch} req {' '.join(map(str, specific))}")

				archScenarios = genericRpm.getScenarios(arch) or ["-"]
				infomsg(f"   {arch} scn {' '.join(map(str, archScenarios))}")

			infomsg(f"")

		if self.hints is None:
			return True

		if self.traceDisambiguation:
			infomsg(f"{genericRpm}: uses scenario(s) on one or more architectures, but I can't find a scenario valid across all architectures")

		if genericRpm.trace:
			displayRpmDependencies(genericRpm, "original dependencies")

		allRequiredScenarios = None
		for arch in genericRpm.architectures:
			reqScenarios = genericRpm.getScenarios(arch)
			if not reqScenarios:
				continue
			if allRequiredScenarios is None:
				allRequiredScenarios = reqScenarios
			else:
				allRequiredScenarios = allRequiredScenarios.intersection(reqScenarios)

		if not allRequiredScenarios:
			errormsg(f"{genericRpm}: different scenarios required on different architectures")
			return False

		for arch in genericRpm.architectures:
			if genericRpm.getScenarios(arch):
				continue

			archDependencies = genericRpm.getDependencies(arch)

			replace = []
			for requiredRpm in archDependencies:
				for concreteScenario in requiredRpm.getControllingScenarios(arch):
					if concreteScenario in allRequiredScenarios:
						replace.append((requiredRpm, concreteScenario))

			if replace:
				for (requiredRpm, concreteScenario) in replace:
					# concreteScenario is a string at this point, like kernel/default/image
					w = concreteScenario.split('/')
					assert(len(w) == 3)
					abstractPackageName = f"{w[0]}/{w[2]}"
					abstractRpm = db.createRpm(abstractPackageName, genericRpm.TYPE_SCENARIO)

					if genericRpm.trace:
						infomsg(f"{genericRpm}: replace {requiredRpm} with {concreteScenario} on {arch}")

					# Now replace the real dependency with the scenario package
					archDependencies.discard(requiredRpm)
					archDependencies.add(abstractRpm)

					genericRpm.addScenarios(arch, set((concreteScenario, )))

		genericRpm.validScenarios._common = None
		genericRpm.solutions._common = None

		if genericRpm.trace:
			displayRpmDependencies(genericRpm, "updated dependencies")

		if not genericRpm.validScenarios.common:
			return False

		if self.traceDisambiguation:
			infomsg(f"   {genericRpm}: successfully fixed up scenario dependencies")

		return True
