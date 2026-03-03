##################################################################
#
# New, simplified classification implementation
#
##################################################################

import os
from .filter import Classification
from .util import debugmsg, infomsg, warnmsg, errormsg, loggingFacade
from .arch import ArchSet
from .new_compose import *
from .reports import LocationIndexedReport

__names__ = [
	'NewResult',
]

class RpmControl(Composable):
	TYPE = "rpm"

	def __init__(self, rpm):
		super().__init__()

		self.rpm = rpm
		self.trace = rpm.trace
		self.optionSet = None
		self.choice = None
		self.definedByOption = None
		self.requiredBy = {}
		self.tentativePolicy = None
		self.indirectRequiredOptions = None

		rpm.composable = self

	def __str__(self):
		return str(self.rpm)

	def addRequiredBy(self, requiringRpmControl, arch):
		if requiringRpmControl not in self.requiredBy:
			self.requiredBy[requiringRpmControl] = ArchSet()

		if arch is None:
			self.requiredBy[requiringRpmControl].update(requiringRpmControl.rpm.architectures)
		else:
			self.requiredBy[requiringRpmControl].add(arch)

	@property
	def isUnresolvable(self):
		return self.rpm.isUnresolvable

	def markUnresolvable(self, dependsOn = None):
		rpm = self.rpm

		if dependsOn is not None:
			self.propagateDecisionFrom(dependsOn)
			assert(self.isExcluded)
		else:
			self.markExcluded(ReasonPolicy("rpm {rpm} is defined to be unresolvable"))

		for other, archSet in self.requiredBy.items():
			if other.disableArchitectures(archSet, self):
				# only go here if archSet was not disabled to begin with.
				# otherwise we may end up in an infinite recursion
				if not other._validArchitectures:
					other.markUnresolvable(self)

	def addOptionDependency(self, option):
		if self.optionSet is None:
			self.optionSet = Classification.createLabelSet()
		self.optionSet.add(option)

	@property
	def hasOptionDependencies(self):
		return bool(self.optionSet)

	@property
	def optionDependencies(self):
		return self.optionSet

	def allOptionsEnabled(self, validLabels):
		if self.optionSet is None:
			return True

		return self.optionSet.issubset(validLabels)

	def updateIndirectRequiredOptions(self, requires):
		if self.choice and False:
			requires = requires.copy()
			for reqControl in self.choice.requiredOptions:
				requires.discard(reqControl.label)

		if not requires:
			return

		if self.indirectRequiredOptions is None:
			self.indirectRequiredOptions = Classification.createLabelSet()
		elif requires.issubset(self.indirectRequiredOptions):
			return

		self.indirectRequiredOptions.update(requires)
		for other in self.requiredBy:
			other.updateIndirectRequiredOptions(requires)

	def setTentativePolicy(self, policy):
		self.tentativePolicy = policy

		if self.tentativePolicy.decision == COMPOSE_EXCLUDE:
			self.excludeRecursively()

	def setPolicy(self, policy):
		# avoid transition excluded->included
		if policy.decision > self._decision and self._decision != COMPOSE_UNSPEC:
			raise Exception(f"{self}: reverting decision {self._decision} -> {policy.decision}")
		# do not downgrade decision
		if self._decision > policy.decision:
			return

		if self.rpmClass not in policy.validClasses:
			raise Exception(f"{self}: refusing to apply policy {policy}; invalid class {self.rpmClass} (valid={policy.validClasses})")

		super().setPolicy(policy)

	def setDecision(self, decision, reason = None):
		if self._decision == decision:
			if reason is not None:
				self.justifications.add(reason)
			return True

		if self._decision != COMPOSE_UNSPEC:
			infomsg(f"{self}: refusing to change decision {self.decisionString} to {Policy.decisionString(decision)}")
			return False

		if self.trace:
			infomsg(f"POLICY: {Policy.decisionAsString(decision)} {self} because {reason}")

		super().propagateDecision(decision, reason)

		if decision == COMPOSE_EXCLUDE:
			reason = ReasonRequires(self)
			for reqControl, archSet in self.requiredBy.items():
				reqControl.disableArchitectures(archSet, self)
		return True

	def markExcluded(self, reason = None):
		self.setDecision(COMPOSE_EXCLUDE, reason)

	# disable this rpm on a set of architectures (usually because a required
	# package was excluded and/or is not resolvable).
	# return True if there was a change in arch set. return False if the archs
	# in question were already disabled.
	def disableArchitectures(self, badArchSet, dependsOn = None):
		assert(badArchSet is not None)

		if self._validArchitectures is None:
			self._validArchitectures = self.rpm.architectures.copy()

		badArchSet = badArchSet.intersection(self._validArchitectures)
		if not badArchSet:
			return False

		if self.trace:
			infomsg(f"{self}: disable architecures {badArchSet} because it depends on {dependsOn}")

		self.justifications.add(ReasonDisableArchitectures(badArchSet, dependsOn))
		self._validArchitectures.difference_update(badArchSet)

		if not self._validArchitectures:
			self.markExcluded(ReasonRequires(dependsOn))
			return True

		for reqControl, reqArchSet in self.requiredBy.items():
			removeArchSet = badArchSet.intersection(reqArchSet)
			if removeArchSet:
				reqControl.disableArchitectures(removeArchSet, self)

		return True

	@property
	def rpmClass(self):
		klass = self.rpm.new_class
		if klass is None:
			raise Exception(f"{self}: rpm has not class")
		return klass

	def getSupportLevel(self, dummy = None):
		return super().getSupportLevel(self.rpmClass)

class BuildControl(Composable):
	TYPE = "build"

	def __init__(self, build):
		super().__init__()

		self.build = build
		self.trace = build.trace
		self.shippable = True
		self._rpms = {}

	def __str__(self):
		return str(self.build)

	def addRpm(self, rpmControl):
		assert(rpmControl.rpm)
		self._rpms[rpmControl.rpm] = rpmControl

	@property
	def rpms(self):
		return iter(self._rpms.values())

class LabelControl(Composable):
	def __init__(self, label):
		super().__init__()

		self._label = label
		self.trace = label.trace

		self._validArchitectures = label.architectures

	def __str__(self):
		return str(self._label)

	@property
	def label(self):
		return self._label

class OptionControl(LabelControl):
	TYPE = "build option"

	def __init__(self, buildOption):
		super().__init__(buildOption)
		self.definedBy = None
		self._rpms = {}

	@property
	def option(self):
		warnmsg(f"{self}: use .label member instead of .option")
		return self._label

	def addRpm(self, rpmControl):
		rpm = rpmControl.rpm
		self._rpms[rpm] = rpmControl

	@property
	def rpms(self):
		return iter(self._rpms.values())

class GlobalFlavorControl(LabelControl):
	TYPE = "global choice"

	def __init__(self, choice):
		super().__init__(choice)

		self.shippable = True
		self.requiredOptions = set()

	@property
	def choice(self):
		warnmsg(f"{self}: use .label member instead of .choice")
		return self._label

	def addRequiredOption(self, optionControl):
		self.requiredOptions.add(optionControl)
		if optionControl.trace:
			self.trace = True

	@property
	def canInclude(self):
		return all(_.isIncluded for _ in self.requiredOptions)

	@property
	def hasOptionDependencies(self):
		return bool(self.requiredOptions)

	@property
	def lackingOptions(self):
		return set(filter(lambda optionControl: not optionControl.isIncluded, self.requiredOptions))

	# An epic defines a flavor, e.g. Printing+funkyformat or Zypper+syspython.
	# When should we include the rpms belonging to these flavors?
	# Obviously, the epic itself needs to be included for this. Then, it
	# depends on whether the flavor depends on some options or not. If
	# it depends on a option (eg syspython), then we enable the flavor
	# Zypper+syspython whenever the underlying option is enabled.
	# Else, it does not depend on any build option, we do not enable the flavor
	# by default; only when explicitly requested.
	@property
	def autoEnableWhenPossible(self):
		return bool(self.hasOptionDependencies)

class LocalFlavorControl(Composable):
	TYPE = "choice"

	def __init__(self, epic, globalFlavorControl):
		super().__init__()

		self.name = f"{epic}+{globalFlavorControl}"
		self.shippable = True
		self.globalFlavorControl = globalFlavorControl
		self.trace = globalFlavorControl.trace
		self._rpms = {}

	def __str__(self):
		return self.name

	def addRpm(self, rpmControl):
		assert(rpmControl.rpm)
		self._rpms[rpmControl.rpm] = rpmControl

	@property
	def decision(self):
		if self._decision == COMPOSE_UNSPEC:
			return self.globalFlavorControl.decision
		return self._decision

	@property
	def rpms(self):
		for m in self._rpms.values():
			yield m

	@property
	def canInclude(self):
		return self.globalFlavorControl.canInclude

	@property
	def lackingOptions(self):
		return self.globalFlavorControl.lackingOptions

	@property
	def requiredOptions(self):
		return self.globalFlavorControl.requiredOptions

	@property
	def hasOptionDependencies(self):
		return self.globalFlavorControl.hasOptionDependencies

	@property
	def autoEnableWhenPossible(self):
		return self.globalFlavorControl.autoEnableWhenPossible

class EpicControl(LabelControl):
	TYPE = "epic"

	def __init__(self, db, epic, closure, dependencyReport = None):
		super().__init__(epic)

		self.db = db
		self.trace = epic.trace
		self.closure = closure
		self.dependencyReport = dependencyReport

		self._choices = {}
		self._builds = {}
		self._rpms = {}

		self.optional = {}
		self.byClass = {}

		self.definedOptions = set()

	@property
	def epic(self):
		warnmsg(f"{self}: use .label member instead of .epic")
		return self._label

	def addChoice(self, globalFlavorControl):
		flavorLabel = globalFlavorControl.label

		localChoiceControl = self._choices.get(flavorLabel)
		if localChoiceControl is None:
			localChoiceControl = LocalFlavorControl(self.label, globalFlavorControl)
			self._choices[flavorLabel] = localChoiceControl
		return localChoiceControl

	@property
	def choices(self):
		return iter(self._choices.values())

	def addBuild(self, build):
		m = self._builds.get(build);
		if m is None:
			m = BuildControl(build)
			self._builds[build] = m
		return m

	@property
	def builds(self):
		return self._builds.values()

	def addRpm(self, rpm):
		m = self._rpms.get(rpm);
		if m is None:
			m = RpmControl(rpm)
			self._rpms[rpm] = m

			klass = rpm.new_class
			if klass is not None:
				self.classMembership(klass, True).add(m)
		return m

	@property
	def rpms(self):
		return self._rpms.values()

	def addDefinedOption(self, optionControl):
		self.definedOptions.add(optionControl)
		optionControl.definedBy = self

	def classMembership(self, klass, create = False):
		memberSet = self.byClass.get(klass)
		if memberSet is None and create:
			memberSet = set()
			self.byClass[klass] = memberSet
		return memberSet

	def checkForDependencyInversion(self, rpmControl, req, arch = None):
		rpm = rpmControl.rpm

		# This is wrong; we need to handle dependencies on stuff like
		# this-is-only-for-build-envs, and hence we should not return early from here.
#		if req.isSynthetic:
#			return False

		if rpm.new_build is req.new_build:
			return False

		if req.isUnresolvable:
			if rpm.trace or req.trace:
				infomsg(f"{rpm} is unresolvable because it depends on {req}")
			return False

		if req.new_build is None:
			raise Exception(f"{rpm} requires {req}, which is not attached to any build")

		reqBuild = req.new_build
		if reqBuild.new_epic is None:
			errormsg(f"{reqBuild}: no epic (requirement)")
			return False

		if reqBuild.new_epic in self.closure:
			return False

		if reqBuild.new_layer is rpm.new_build.new_layer:
			return False

		optionLabel = None
		promiseRpm = None

		if req.labelHints is not None:
			optionLabel = req.labelHints.definingBuildOption
			if optionLabel is not None:
				# if the promise doesn't exist yet, we create it on the fly
				promiseRpm = self.db.createPromise(req)

		if promiseRpm is None:
			promiseRpm = self.db.lookupPromise(req)
		if promiseRpm is None:
			epicLabel = rpm.new_build.new_epic
			location = epicLabel.definingLocation
			msg = f"{epicLabel}: {rpm.new_build}: dependency inversion {rpm} -> {req} (build {req.new_build}, epic {req.new_build.new_layer}/{req.new_build.new_epic})"

			if self.dependencyReport is None:
				warnmsg(msg)
			else:
				self.dependencyReport.add(location, msg)

			rpmControl.shippable = False
			return False

		promiseEpic = None

		if promiseRpm.labelHints:
			promiseEpic = promiseRpm.labelHints.epic

		if promiseEpic in self.closure:
			# We can replace the requirement with a promise, without having to mark
			# it as optional
			optionLabel = None
		elif optionLabel is None:
			warnmsg(f"{rpm.new_build}: unsatisfied dependency {rpm} -> {req} (epic {req.new_build.new_epic}): {promiseRpm} (epic {promiseEpic}) not visible from here")
			rpmControl.shippable = False
			return False

		if rpm.trace or req.trace:
			debugmsg(f"{rpm.new_build}: replace {rpm} -> {req} (option {optionLabel}) with {promiseRpm}; arch={arch}")

		# FIXME: should this really be unconditional? Making this per-arch seems
		#  (a) lots of effort
		#  (b) not quite the thing we want to achieve
		if optionLabel is not None:
			rpmControl.addOptionDependency(optionLabel)

		return True

class NewResult(object):
	def __init__(self, db, classificationScheme):
		self._db = db
		self.classificationScheme = classificationScheme
		self.epicOrder = classificationScheme.epicOrder()
		self._members = {}
		self._choices = {}
		self._options = {}

		self.dependencyReport = LocationIndexedReport()

	@property
	def db(self):
		return self._db

	# Accessors that return all labels of a certain type (from the classification scheme)
	@property
	def layerLabels(self):
		return self.classificationScheme.allLayers

	@property
	def epicLabels(self):
		return self.classificationScheme.allEpics

	@property
	def buildOptionLabels(self):
		return self.classificationScheme.allBuildOptions

	def addEpic(self, epic):
		m = self._members.get(epic)
		if m is None:
			closure = self.epicOrder.downwardClosureFor(epic)
			m = EpicControl(self._db, epic, closure, self.dependencyReport)
			self._members[epic] = m
		return m

	def get(self, epic):
		raise Exception(f"Called obsolete {self.__class__.__name__}.get() method; please use addEpic() instead")

	@property
	def epics(self):
		return iter(self._members.values())

	def addOption(self, buildOption):
		m = self._options.get(buildOption)
		if m is None:
			m = OptionControl(buildOption)
			self._options[buildOption] = m

			m.definedBy = self.addEpic(buildOption.epic)
		return m

	@property
	def options(self):
		for m in self._options.values():
			yield m

	def addGlobalChoice(self, label):
		m = self._choices.get(label)
		if m is None:
			m = GlobalFlavorControl(label)
			for buildOption in label.requiredOptions:
				# infomsg(f"choice {label} depends on option {buildOption}")
				m.addRequiredOption(self.addOption(buildOption))
			self._choices[label] = m
		return m

	@property
	def globalChoices(self):
		for m in self._choices.values():
			yield m

	def membershipForRpm(self, rpm):
		try:
			return rpm.composable
		except:
			pass
		return None

	def getRequiredWork(self, rpm, archSet):
		commonRequires = rpm.resolvedRequires
		for req in commonRequires:
			yield req, None
		for arch in archSet:
			for req in rpm.solutions.raw_get(arch).difference(commonRequires):
				yield req, arch

	def getRequired(self, rpm, archSet):
		for req, arch in self.getRequiredWork(rpm, archSet):
			m = self.membershipForRpm(req)
			if m is None:
				raise Exception(f"{rpm} requires {req}, but I'm not tracking this rpm")
			yield m

	def buildInverseTree(self):
		unresolvables = []
		for epicControl in self._members.values():
			for rpmControl in epicControl.rpms:
				rpm = rpmControl.rpm
				if rpm.isUnresolvable:
					unresolvables.append(rpmControl)

				if rpm.new_class is not None and rpm.new_class.isIgnored:
					# This rpm has been tagged as "noship"
					continue

				for req, arch in self.getRequiredWork(rpm, rpm.architectures):
					reqControl = self.membershipForRpm(req)
					if reqControl is None:
						raise Exception(f"{rpm} requires {req}, but I'm not tracking this rpm")

					reqControl.addRequiredBy(rpmControl, arch)

		for rpmControl in unresolvables:
			# Normally, the only rpm we find in this manner should be __unresolvable__
			rpmControl.markUnresolvable()

	def buildIndirectRequirements(self):
		for epicControl in self._members.values():
			for rpmControl in epicControl.rpms:
				rpm = rpmControl.rpm

				requires = rpmControl.optionSet or set()
				if rpmControl.choice:
					requires = requires.copy()
					for reqControl in rpmControl.choice.requiredOptions:
						requires.discard(reqControl.label)

				if requires:
					rpmControl.updateIndirectRequiredOptions(requires)

	def save(self, path):
		def write(msg):
			print(msg, file = dbf)

		with open(path + ".tmp", "w") as dbf:
			for epicControl in sorted(self._members.values(), key = str):
				write(f"epic {epicControl} layer={epicControl.label.layer}")
				for buildControl in sorted(epicControl.builds, key = str):
					write(f"  build {buildControl}")
					for rpmControl in sorted(buildControl.rpms, key = str):
						extra = []
						extra.append(f"class={rpmControl.rpmClass}")
						if rpmControl.definedByOption:
							extra.append(f"option={rpmControl.definedByOption}")
						if rpmControl.choice:
							extra.append(f"choice={rpmControl.choice}")
						if rpmControl.indirectRequiredOptions:
							extra.append(f"requires={','.join(map(str, rpmControl.indirectRequiredOptions))}")
						if rpmControl.rpm.new_override_epic is not None and \
						   rpmControl.rpm.new_override_epic is not epicControl.label:
							# this rpm was placed in a different epic using split-ok
							extra.append(f"epic={rpmControl.rpm.new_override_epic}")
						write(f"    rpm {rpmControl} {' '.join(extra)}")

		os.rename(path + ".tmp", path)
		infomsg(f"Updated {path}")

	def getRelevantRpms(self, build):
		result = []
		for rpm in build.binaries:
			if rpm.type in (rpm.TYPE_SCENARIO, rpm.TYPE_PROMISE, rpm.TYPE_SYNTHETIC):
				pass
			elif rpm.type in (rpm.TYPE_MISSING, ):
				continue
			elif rpm.type == rpm.TYPE_REGULAR:
				if rpm.new_class is not None and rpm.new_class.isIgnored:
					# This rpm has been tagged as "noship"
					continue
			else:
				infomsg(f"Ignore {rpm} {rpm.type} (build {build}, epic={build.new_epic})")
				continue

			result.append(rpm)
		return result

	@classmethod
	def build(klass, classificationScheme, collection, db):
		newResult = NewResult(db, classificationScheme)

		for build in collection.builds:
			buildRpms = newResult.getRelevantRpms(build)
			if not buildRpms:
				continue

			if build.name.startswith('patchinfo.'):
				continue

			epic = build.new_epic
			if epic is None:
				errormsg(f"{build}: no epic")
				continue

			assert(epic.layer)

			epicControl = newResult.addEpic(epic)

			buildControl = epicControl.addBuild(build)
			for rpm in buildRpms:
				labelHints = rpm.labelHints
				if labelHints is None and rpm.trace:
					infomsg(f"{rpm}: no label hints")

				if rpm.new_class is not None and rpm.new_class.isIgnored:
					# This rpm has been tagged as "noship"
					continue

				if rpm.new_class is None:
					rpm.new_class = classificationScheme.defaultClass

				# In some rare cases, we will have two builds producing the same rpm
				# (which is usually a bug, but it happens).
				if rpm.new_build is not build:
					# do not complain about this here; the classify command will print
					# a report about this
					# errormsg(f"{epic}/{build}: rpm {rpm} has conflicting build {rpm.new_build}")
					continue

				rpmControl = epicControl.addRpm(rpm)
				buildControl.addRpm(rpmControl)

				# the set of resolved requirements may change while we iterate over it,
				# so force a copy
				for req in list(rpm.resolvedRequires):
					epicControl.checkForDependencyInversion(rpmControl, req)

				common = rpm.solutions.common
				for arch in rpm.solutions.keys():
					assert(arch is not None)
					for req in rpm.solutions.raw_get(arch).difference(common):
						epicControl.checkForDependencyInversion(rpmControl, req, arch = arch)

				if labelHints is not None:
					buildOption = labelHints.definingBuildOption
					if buildOption is not None:
						if rpm.trace:
							infomsg(f"{rpm} is controlled by by build option {buildOption}")

						optionControl = newResult.addOption(buildOption)
						rpmControl.definedByOption = optionControl
						optionControl.addRpm(rpmControl)
						continue

				if labelHints is not None:
					autoFlavor = labelHints.getAutoFlavor(classificationScheme)

					if autoFlavor is not None:
						if rpm.trace:
							infomsg(f"{rpm} implements flavor {autoFlavor.describe()}")
							if autoFlavor.requiredOptions:
								infomsg(f"{rpm} depends on {' '.join(map(str, autoFlavor.requiredOptions))}")

						for buildOption in autoFlavor.requiredOptions:
							optionControl = newResult.addOption(buildOption)
							rpmControl.addOptionDependency(buildOption)

						globalFlavorControl = newResult.addGlobalChoice(autoFlavor)
						localFlavorControl = epicControl.addChoice(globalFlavorControl)
						localFlavorControl.addRpm(rpmControl)
						rpmControl.choice = localFlavorControl

						if autoFlavor.trace:
							localFlavorControl.trace = True

		newResult.buildInverseTree()
		newResult.buildIndirectRequirements()

		for buildOption in classificationScheme.allBuildOptions:
			optionControl = newResult.addOption(buildOption)

			epic = buildOption.epic
			newResult.addEpic(epic).addDefinedOption(optionControl)

		for autoFlavor in classificationScheme.allAutoFlavors:
			newResult.addGlobalChoice(autoFlavor)

		return newResult
