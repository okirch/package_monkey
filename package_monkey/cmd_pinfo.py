from .options import ApplicationBase
from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .util import NameMatcher
from .arch import *
from .postprocess import *

loggingFacade.disableTimestamps()

class CommonInfoApplication(ApplicationBase):
	def __init__(self, name, opts):
		super().__init__(name, opts)

		self.db = None
		self.extraDB = None

	def run(self):
		self.db = self.loadNewDB()

		labelFacade = None
		if not self.opts.no_labels:
			labelFacade = self.loadClassificationForSnapshot(None)

		renderer = Renderer(labelFacade)

		if self.opts.verbose:
			renderer.rpmPackageInfo = RpmSummaryRendererLong()

		if self.opts.requires_only:
			renderer.rpmPackageInfo = None
			renderer.provides = None
			renderer.promisedTo = None
			renderer.policy = None
		if self.opts.provides_only:
			renderer.rpmPackageInfo = None
			renderer.requires = None
			renderer.policy = None
		if self.opts.names_only:
			renderer.rpmPackageInfo = None
			renderer.provides = None
			renderer.promisedTo = None
			renderer.requires = None
			renderer.obsPackageInfo = None
			renderer.policy = None

		if self.opts.siblings:
			renderer.obsPackageInfo = OBSBuildSiblingRenderer()

		if renderer.rpmPackageInfo is not None:
			self.extraDB = self.codebaseData.loadExtraDB()

		self.renderer = renderer

		self.processQuery(self.db, self.opts.packages)

	def renderOneRpm(self, rpm, obsBuild = None):
		renderer = self.renderer

		print(f"{renderer.renderRpmName(rpm)}")

		renderer.renderExtraInfo(rpm, self.extraDB)

		renderer.renderArchitectures(rpm)
		renderer.renderVersion(rpm)
		renderer.renderPolicy(rpm)
		renderer.renderScenarios(rpm)

		if obsBuild is not None:
			renderer.renderOBSBuildInfo(obsBuild, exceptRpm = rpm)

		renderer.renderRequires(rpm)
		renderer.renderUnresolvables(rpm)

		if not rpm.isSourcePackage:
			renderer.renderProvides(rpm, self.db)

		if not rpm.isSourcePackage:
			renderer.renderPromises(rpm, self.db)

class PackageInfoApplication(CommonInfoApplication):
	def processQuery(self, db, nameList):
		validArchitectures = ('src', 'nosrc', 'noarch', 'i686', 'x86_64', 'aarch64', 's390x', 'ppc64le', )
		for packageName in nameList:
			packageArch = None
			if '.' in packageName:
				baseName, arch = packageName.rsplit('.', maxsplit = 1)
				if arch in validArchitectures:
					packageName, packageArch = baseName, arch

			matcher = NameMatcher([packageName])
			rpmList = []
			for rpm in db.rpms:
				if matcher.match(rpm.name):
					rpmList.append(rpm)

			if not rpmList:
				print(f"{packageName}: no match")
				continue

			for rpm in rpmList:
				obsBuild = db.lookupBuildForRpm(rpm)
				self.renderOneRpm(rpm, obsBuild)

class BuildInfoApplication(CommonInfoApplication):
	def processQuery(self, db, nameList):
		for buildName in nameList:
			matcher = NameMatcher([buildName])
			buildList = []
			for build in db.builds:
				if matcher.match(build.name):
					buildList.append(build)

			if not buildList:
				print(f"{buildName}: not found")
				continue

			for build in buildList:
				if not build.rpms:
					print(f"{build}: no rpms for this package?!")
					continue

				print(f"Build {build} ({len(build.binaries)} rpms)")
				for rpm in build.binaries:
					self.renderOneRpm(rpm)
				print("")

class TrivialStringRenderer(object):
	def render(self, s):
		return s

	def renderItems(self, values, indent = ''):
		for s in sorted(values):
			print(f"{indent} - {self.render(s)}")

class RpmNameRenderer(object):
	def __init__(self):
		pass

	def render(self, rpm):
		return rpm.name

	def renderPolicy(self, rpm):
		pass

	def renderVersion(self, rpm):
		pass

	def renderArchitectures(self, rpm):
		pass

	def renderItems(self, rpms, indent = ''):
		for rpm in sorted(rpms, key = lambda p: p.name):
			print(f"{indent} - {self.render(rpm)}")

class LabellingRpmNameRenderer(RpmNameRenderer):
	def __init__(self, labelFacade):
		super().__init__()

		self.labelFacade = labelFacade

	def render(self, rpm):
		result = super().render(rpm)

		labelHints = self.labelFacade.getHintsForRpm(rpm)
		if labelHints is not None:
			result = f"{result} ({labelHints})"
		if rpm.type == rpm.TYPE_MISSING:
			result = result + " [MISSING]"
		return result

class PolicyRenderer(object):
	def __init__(self, labelFacade):
		self.labelFacade = labelFacade
		self.policy = labelFacade.policy

	def render(self, rpm):
		if self.policy is None:
			return

		epic = self.labelFacade.getEpicForBuild(rpm.new_build)
		if epic is None:
			return

		if epic.ownerID:
			team = self.policy.getTeam(epic.ownerID)
			if team is not None:
				print(f"  reviewer: {team}")
			else:
				print(f"  reviewer: {epic.ownerID}")

		if epic.lifecycleID:
			lifecycle = self.policy.getLifeCycle(epic.lifecycleID)
			if lifecycle is not None:
				print(f"  lifecycle: {lifecycle}")
			else:
				print(f"  lifecycle: {epic.lifecycleID}")

class ListRenderer(object):
	def renderList(self, items, itemRenderer):
		if not items:
			if self.MSG_EMPTY is not None:
				print(f"  {self.MSG_EMPTY}")
			return

		print(f"  {self.MSG_HEADER}:")
		itemRenderer.renderItems(items, indent = "  ")

class DependencyRenderer(ListRenderer):
	def __init__(self, rpmNameRenderer):
		self.rpmNameRenderer = rpmNameRenderer

	def dependencyListToPackages(self, depList):
		packages = set()
		for dep in depList:
			packages.update(dep.packages)
		return packages

	def render(self, depList):
		self.renderPackageList(depList)

	def renderPackageList(self, packages):
		self.renderList(packages, self.rpmNameRenderer)

class RequiresRenderer(DependencyRenderer):
	MSG_EMPTY = "does not require anything"
	MSG_HEADER = "requires"

	def render(self, rpm):
		packages = list(rpm.enumerateRequiredRpms())
		super().render(packages)

class ProvidesRenderer(DependencyRenderer):
	MSG_EMPTY = "not required by anything"
	MSG_HEADER = "required by"

	def render(self, rpm, db):
		packages = db.lookupRequiredBy(rpm)
		packages = set(filter(lambda p: not p.isSourcePackage, packages))
		self.renderPackageList(packages)

class PromisedToRenderer(object):
	MSG_EMPTY = None
	MSG_HEADER = "promised to"

	def __init__(self, rpmNameRenderer):
		self.rpmNameRenderer = rpmNameRenderer

	def render(self, rpm, db):
		promiseList = db.lookupPromisedTo(rpm)
		if not promiseList:
			if self.MSG_EMPTY is not None:
				print(f"  {self.MSG_EMPTY}")
			return

		print(f"  {self.MSG_HEADER}:")
		archSpecific = {}
		for promise, packages in promiseList:
			if promise.arch is None:
				archSet = archRegistry.fullset
			else:
				archSet = ArchSet((promise.arch, ))

			for rpm in sorted(packages, key = str):
				existing = archSpecific.get(rpm)
				if existing is None:
					archSpecific[rpm] = archSet
				else:
					archSpecific[rpm] = existing.union(archSet)

		for rpm in sorted(archSpecific.keys(), key = str):
			rpmName = self.rpmNameRenderer.render(rpm)
			archSet = archSpecific.get(rpm)
			if archSet == archRegistry.fullset:
				print(f"   - {rpmName}")
			else:
				print(f"   - {rpmName} ({archSet})")

class UnresolvableRenderer(ListRenderer):
	MSG_EMPTY = None
	MSG_HEADER = "unresolvable requires"

	depRenderer = TrivialStringRenderer()

	def render(self, rpm):
		depList = list(rpm.enumerateUnresolvedDependencies())
		self.renderList(depList, self.depRenderer)

class RpmSummaryRenderer(object):
	def render(self, rpm, db):
		auxInfo = db.lookupRpm(rpm.name, 'x86_64')
		if auxInfo is None:
			return

		if auxInfo.summary is not None:
			print(f"  summary: {auxInfo.summary}")

class RpmSummaryRendererLong(RpmSummaryRenderer):
	def render(self, rpm, db):
		auxInfo = db.lookupRpm(rpm.name, 'x86_64')
		if auxInfo is None:
			return

		if auxInfo.summary is not None:
			print(f"  summary: {auxInfo.summary}")
		if auxInfo.description is not None:
			lines = auxInfo.description.split('\n')
			if len(lines) == 1:
				print(f"  description: {lines[0]}")
			else:
				print(f"  description:")
				for line in lines:
					print(f"            {line}")

class OBSBuildNameRenderer(object):
	def render(self, obsBuild, renderer, exceptRpm = None):
		print(f"  OBS build: {obsBuild}")

class OBSBuildSiblingRenderer(OBSBuildNameRenderer):
	def render(self, obsBuild, renderer, exceptRpm = None):
		super().render(obsBuild, renderer)

		src = obsBuild.sourceRpm

		headerShown = False
		for sib in obsBuild.binaries:
			if sib is exceptRpm or sib is src:
				continue

			if not headerShown:
				print(f"     Sibling packages:")
				headerShown = True

			print(f"        {renderer.renderRpmName(sib)}")

class Renderer(object):
	def __init__(self, labelFacade = None):
		if labelFacade is None:
			self.rpm = RpmNameRenderer()
			self.policy = None
		else:
			self.rpm = LabellingRpmNameRenderer(labelFacade)
			self.policy = PolicyRenderer(labelFacade)

		self.obsPackageInfo = OBSBuildNameRenderer()
		self.rpmPackageInfo = RpmSummaryRenderer()
		self.requires = RequiresRenderer(self.rpm)
		self.unresolvable = UnresolvableRenderer()
		self.provides = ProvidesRenderer(self.rpm)
		self.promisedTo = PromisedToRenderer(self.rpm)

	def renderOBSBuildInfo(self, obsBuild, **kwargs):
		if self.obsPackageInfo is None:
			return
		self.obsPackageInfo.render(obsBuild, self, **kwargs)

	def renderRpmName(self, rpm):
		return self.rpm.render(rpm)

	def renderPolicy(self, rpm):
		if self.policy is None:
			return
		self.policy.render(rpm)

	def renderVersion(self, rpm):
		vlist = list(rpm.versions.common)
		if len(vlist) == 1:
			print(f"  version: {vlist[0]}")

	def renderArchitectures(self, rpm):
		if not rpm.architectures:
			print(f"  architectures: none")
		else:
			print(f"  architectures: {rpm.architectures}")

	def renderScenarios(self, rpm):
		scenarios = rpm.validForScenarios
		if scenarios:
			print(f"  valid scenarios: {' '.join(sorted(map(str, scenarios)))}")

	def renderExtraInfo(self, rpm, db):
		if self.rpmPackageInfo is None or db is None:
			return

		self.rpmPackageInfo.render(rpm, db)

	def renderRequires(self, rpm):
		if self.requires is None:
			return
		return self.requires.render(rpm)

	def renderUnresolvables(self, rpm):
		if self.unresolvable is None:
			return
		return self.unresolvable.render(rpm)

	def renderProvides(self, rpm, db):
		if self.provides is None:
			return
		return self.provides.render(rpm, db)

	def renderPromises(self, rpm, db):
		if self.promisedTo is None:
			return
		return self.promisedTo.render(rpm, db)
