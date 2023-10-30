import yaml
import fnmatch

from util import ExecTimer
from util import filterHighestRanking
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from ordered import PartialOrder
from functools import reduce
from stree import SolvingTreeBuilder

# hack until I'm packaging fastsets properly
import fastsets.fastsets as fastsets

initialPlacementLogger = loggingFacade.getLogger('initial')
debugInitialPlacement = initialPlacementLogger.debug

def intersectSets(a, b):
	if a is None:
		return b
	elif b is None:
		return a
	return a.intersection(b)

def renderLabelSet(name, labels):
	if labels is None:
		return "[unconstrained]"

	if not labels:
		return f"[no {name}]"

	if len(labels) >= 6:
		return f"[{len(labels)} {name}]"

	return f"[{name} {' '.join(map(str, labels))}]";

def displayLabelSetFull(candidates, indent = ""):
	def renderPurposes(label, labelIdent):
		purposeSet = set(label.objectPurposes)
		if purposeSet.issubset(candidates):
			return [f"{labelIdent}-*"]

		purposeSet.intersection_update(candidates)
		return sorted(f"{labelIdent}-{purpose.purposeName}" for purpose in purposeSet)

	if candidates is None:
		infomsg(f"{indent}ALL")
		return

	baseLabels = set(label.baseLabel for label in candidates)
	for baseLabel in sorted(baseLabels, key = str):
		subs = []
		if baseLabel in candidates:
			subs.append('.')

		for flavor in baseLabel.flavors:
			if flavor in candidates:
				subs.append(f"+{flavor.flavorName}")
			subs += renderPurposes(flavor, f"+{flavor.flavorName}")

		subs += renderPurposes(baseLabel, f"")

		if subs:
			subs = ' '.join(subs)
			infomsg(f"{indent}{baseLabel}: {subs}")
		else:
			infomsg(f"{indent}{baseLabel}")


class Classification:
	TYPE_BINARY = 'binary'
	TYPE_SOURCE = 'source'
	TYPE_AUTOFLAVOR = 'autoflavor'
	TYPE_PURPOSE = 'purpose'
	TYPE_BUILDCONFIG = 'buildconf'
	TYPE_BUILDCONFIG_FLAVOR = 'build-flavor'

	DISPOSITION_SEPARATE = 'separate'
	DISPOSITION_MERGE = 'merge'
	DISPOSITION_MAYBE_MERGE = 'maybe_merge'
	DISPOSITION_IGNORE = 'ignore'

	domain = fastsets.Domain("label")

	class Label(domain.member):
		def __init__(self, name, type, id):
			super().__init__()

			self.name = name
			self.type = type
			self.id = id
			self.description = None
			self.gravity = None
			self.runtimeRequires = set()
			self.buildRequires = set()
			self.runtimeAugmentations = set()
			self.disposition = Classification.DISPOSITION_SEPARATE
			self.defaultLabel = None
			self.defined = False

			# This is populated for labels that represent a build flavor like @Core+python,
			# or a purpose like @Core-devel, or a flavor AND purpose, like @Core+python-devel
			self.parent = None
			self.flavorName = None
			self._purposeName = None

			# This is populated for base flavors like @Core
			self._flavors = {}

			# This is populated for labels that can have different purposes
			self._purposes = {}

			self.mergeableAutoFlavors = set()

			# build config labels have a source project assigned
			self.sourceProject = None

			# binary labels have a build config assigned
			self.buildConfig = None

			# if autoSelect is true, then a group referencing a label
			# "@Foo" will automatically select all flavors "@Foo+bar"
			# if it supports all requirements of this flavor.
			# For instance, "@Core+systemd" may contain utilities that
			# need libsystemd. A label "@Bar" that requires both @Core
			# and @MinimalSystemd will automatically add @Foo+bar to its
			# closure
			self.autoSelect = True

			# The may be used later to locate "favorite sibling" packages,
			# eg systemd-mini-devel -> systemd-mini
			self.packageSuffixes = []

			self.isPurpose = False
			if self.purposeName is not None or self.type == Classification.TYPE_PURPOSE:
				self.isPurpose = True

		@property
		def fingerprint(self):
			values = [self.name, self.type, self.disposition, self.defaultLabel, self.gravity]
			for attrName in ('_flavors', '_purposes', 'runtimeRequires', 'buildRequires', 'runtimeAugmentations', 'mergeableAutoFlavors'):
				values.append(attrName)

				attr = getattr(self, attrName)

				# some of these are label valued dicts
				if type(attr) == dict:
					attr = attr.values()

				values += sorted(map(str, attr))

			return hash(tuple(values))

		@property
		def purposeName(self):
			return self._purposeName

		@purposeName.setter
		def purposeName(self, name):
			self._purposeName = name

			self.isPurpose = (self._purposeName is not None or self.type == Classification.TYPE_PURPOSE)

		@property
		def componentName(self):
			if self.sourceProject is not None:
				return self.sourceProject.name
			if self.buildConfig is not None:
				return self.buildConfig.name
			return None

		@property
		def baseLabel(self):
			result = self
			while result.parent:
				result = result.parent
			return result

		def okayToAdd(self, other):
			if self.type == other.type:
				return True

			if other.type == Classification.TYPE_BINARY:
				return self.type in (Classification.TYPE_AUTOFLAVOR,
						Classification.TYPE_PURPOSE,
						Classification.TYPE_BUILDCONFIG_FLAVOR,
						Classification.TYPE_SOURCE)

			if other.type == Classification.TYPE_SOURCE:
				return self.type in (Classification.TYPE_BUILDCONFIG, )

			return False

		def addRuntimeDependency(self, other):
			assert(isinstance(other, Classification.Label))
			if not self.okayToAdd(other):
				raise Exception(f"Attempt to add incompatible dependency to {self.type} label {self}: {other} (type {other.type})")

			self.runtimeRequires.add(other)

		def addRuntimeAugmentation(self, other):
			self.addRuntimeDependency(other)
			self.runtimeAugmentations.add(other)

		def addBuildDependency(self, other):
			assert(isinstance(other, Classification.Label))
			self.buildRequires.add(other)

		def addMergeableFlavor(self, autoFlavor):
			assert(autoFlavor.type == Classification.TYPE_AUTOFLAVOR)
			self.mergeableAutoFlavors.add(autoFlavor)

		def autoFlavorCanBeMerged(self, autoFlavor):
			if autoFlavor.disposition == Classification.DISPOSITION_MERGE:
				return True
			return autoFlavor in self.mergeableAutoFlavors

		def copyRequirementsFrom(self, other):
			if self.type == Classification.TYPE_BINARY and \
			   other.type in (Classification.TYPE_BINARY, Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
				self.runtimeRequires.update(other.runtimeRequires)
				self.runtimeAugmentations.update(other.runtimeAugmentations)

			self.buildRequires.update(other.buildRequires)

		def copyBuildRequirementsFrom(self, other):
			self.buildRequires.update(other.buildRequires)

		@property
		def flavors(self):
			return map(lambda pair: pair[1], sorted(self._flavors.items()))

		def getBuildFlavor(self, name):
			return self._flavors.get(name)

		def addBuildFlavor(self, otherLabel):
			flavorName = otherLabel.flavorName

			if self.getBuildFlavor(flavorName) is not None:
				raise Exception(f"Duplicate definition of flavor {flavorName} for {self.name}")

			self._flavors[flavorName] = otherLabel

			# flavors inherit the parent's build project by default
			if self.sourceProject and not otherLabel.sourceProject:
				otherLabel.setSourceProject(self.sourceProject)
			if self.buildConfig and not otherLabel.buildConfig:
				otherLabel.setBuildConfig(self.buildConfig)

			if self.sourceProject and otherLabel.sourceProject is not self.sourceProject:
				raise Exception(f"build flavor {otherLabel} uses source project {otherLabel.sourceProject}, but {self} uses {self.sourceProject}")

			assert(otherLabel.parent is self and otherLabel.flavorName == flavorName)

			# This creates a circular reference that kills garbage collection, but
			# we'll live with this for now
			#otherLabel.parent = self
			#otherLabel.flavorName = flavorName

		@property
		def objectPurposes(self):
			return sorted(self._purposes.values(), key = lambda label: label.name)

		def getObjectPurpose(self, purposeName):
			return self._purposes.get(purposeName)

		def addObjectPurpose(self, otherLabel):
			purposeName = otherLabel.purposeName

			if self.getObjectPurpose(purposeName) is not None:
				raise Exception(f"Duplicate definition of purpose {purposeName} for {self.name}")

			self._purposes[purposeName] = otherLabel

			# purposes inherit the parent's build project by default
			if self.sourceProject and not otherLabel.sourceProject:
				otherLabel.setSourceProject(self.sourceProject)
			if self.buildConfig and not otherLabel.buildConfig:
				otherLabel.setBuildConfig(self.buildConfig)

			if self.sourceProject and otherLabel.sourceProject is not self.sourceProject:
				raise Exception(f"build purpose {otherLabel} uses source project {otherLabel.sourceProject}, but {self} uses {self.sourceProject}")

			assert(otherLabel.parent is self and otherLabel.purposeName == purposeName)

			# This creates a circular reference that kills garbage collection, but
			# we'll live with this for now
			# otherLabel.parent = self
			# otherLabel.flavorName = flavorName

		def setSourceProject(self, sourceLabel):
			if self.sourceProject is sourceLabel:
				return
			if self.sourceProject is not None:
				raise Exception(f"Duplicate source group for {self}: {self.sourceProject} vs {sourceLabel}")

			assert(isinstance(sourceLabel, Classification.Label))
			self.sourceProject = sourceLabel

		def setBuildConfig(self, configLabel):
			if self.buildConfig is configLabel:
				return
			if self.buildConfig is not None:
				raise Exception(f"Duplicate source group for {self}: {self.buildConfig} vs {configLabel}")
			self.buildConfig = configLabel
			if self.sourceProject is None:
				self.sourceProject = configLabel.sourceProject

			self.copyBuildRequirementsFrom(configLabel)

		def getBuildConfigFlavor(self, name):
			sourceProject = self.sourceProject
			if sourceProject is None:
				return None

			return sourceProject.getBuildFlavor(name)

		# Starting @Foo+flavor-purpose, look up @Foo-$flavorName-$purposeName
		def findSibling(self, flavorName, purposeName):
			label = self
			while label.parent:
				label = label.parent
			if flavorName is not None:
				label = label.getBuildFlavor(flavorName)
			if label and purposeName:
				label = label.getObjectPurpose(purposeName)
			return label

		def mayAutoSelect(self, order, flavor):
			if flavor is self:
				return False
			if not flavor.autoSelect:
				return False
			if flavor.parent is None:
				return False

			# this is somewhat tricky but important.
			# consider this:
			#	@Python+gnome, which requires @Gnome. it contains python
			#		modules that would be useful in a Gnome environment
			#	@Gnome+python, which requires @Python, and contains a
			#		(hypothetical) python utility named gnome-foo
			#
			# If we're not careful, we end up with @Python+gnome auto-selecting
			# @Gnome+python. But what we really want is the other way around
			flavorClosure = order.downwardClosureFor(flavor)
			if not self.runtimeAugmentations.isdisjoint(flavorClosure):
				return False

			# avoid introducing loops
			if self in flavorClosure:
				return False
			if self.parent and self.parent in flavorClosure:
				return False

			return True

		def autoSelectCompatibleFlavors(self, order):
			if not self.autoSelect:
				return

			myClosure = order.downwardClosureFor(self).copy()
			availableFlavors = set()
			for requiredLabel in myClosure:
				if requiredLabel is self:
					continue
				for flavor in requiredLabel.flavors:
					if self.mayAutoSelect(order, flavor):
						availableFlavors.add(flavor)

			candidateFlavors = availableFlavors.difference(myClosure)

			# Two things to note: we avoid adding new flavors immediately, in order to
			# avoid some costly recomputations of label closures.
			# Second, we iterate because adding one flavor in round #1 may actually
			# provide the needed support to enable a second flavor in round #2.
			# A typical example would be @GnomeLibraries requiring @DBus and @GLib.
			# The first round would enable @GLib+dbus, and the second round would enable
			# @DBus+x11
			while candidateFlavors:
				# infomsg(f"{self} try to select from {' '.join(map(str, candidateFlavors))}")
				eligibleFlavors = set()
				for flavor in order.bottomUpTraversal(candidateFlavors):
					flavorBaseClosure = order.downwardClosureFor(flavor)
					if flavor in flavorBaseClosure:
						flavorBaseClosure.remove(flavor)

					if flavorBaseClosure.issubset(myClosure):
						# infomsg(f"{self} auto-selected {flavor}")
						myClosure.update(order.downwardClosureFor(flavor))
						eligibleFlavors.add(flavor)

				if not eligibleFlavors:
					break

				for flavor in eligibleFlavors:
					self.addRuntimeDependency(flavor) 

				candidateFlavors.difference_update(eligibleFlavors)

		def allFlavorRequirementsSatisfied(self, myClosure, flavor, flavorBaseClosure):
			if flavor.parent is None:
				return False
			if referringLabel.parent == self:
				return False

			missing = myClosure.difference(flavorBaseClosure)
			try:
				missing.remove(self)
			except: pass

			return not(missing)

		def __str__(self):
			return self.name

		def describe(self):
			attrs = []
			if self.gravity is not None:
				attrs.append(f"gravity={self.gravity}")
			if self.disposition is not None:
				attrs.append(f"disposition={self.disposition}")

			if not attrs:
				return self.name

			return f"{self.name} ({', '.join(attrs)})"

	# rather than a regular python set, this creates a fastset that will only
	# accept members from the label domain.
	@classmethod
	def createLabelSet(klass, initialValues = None):
		return klass.domain.set(initialValues)

	@classmethod
	def baseLabelsForSet(klass, labels):
		# for a base label, return self. For a derived label, return immediate parent
		def transform(label): return label.parent or label

		# if we ever allow more than 3 components in a label name, this needs to be adjusted
		result = klass.createLabelSet(map(transform, labels))
		result = klass.createLabelSet(map(transform, result))
		return result

	# return the list of labels rated highest (ie with the lowest priority value)
	@staticmethod
	def filterLabelsByGravity(labels):
		return set(filterHighestRanking(labels, lambda l: l.gravity))

	@staticmethod
	def buildSolvingTree(classificationContext, packages):
		builder = SolvingTreeBuilder(classificationContext)
		return builder.buildTree(packages)

	class Scheme:
		def __init__(self):
			self._labels = {}
			self._nextLabelId = 0

		@property
		def fingerprint(self):
			values = tuple(label.fingerprint for label in self.allLabels)
			return hash(values)

		@property
		def allAutoPurposes(self):
			return set(filter(lambda label: label.type == Classification.TYPE_PURPOSE, self._labels.values()))

		@property
		def allAutoFlavors(self):
			return set(filter(lambda label: label.type == Classification.TYPE_AUTOFLAVOR, self._labels.values()))

		def getLabel(self, name):
			return self._labels.get(name)

		def createLabel(self, name, type):
			label = self._labels.get(name)
			if label is None:
				label = Classification.Label(name, type, self._nextLabelId)
				self._labels[name] = label
				self._nextLabelId += 1
			elif label.type != type:
				raise Exception(f"Conflicting types for label {name}. Already have a label of type {label.type}, now asked to create {type}")
			return label

		def createFlavor(self, baseLabel, flavorName, buildConfig = None, sourceProject = None):
			if baseLabel.flavorName is not None:
				raise Exception(f"Cannot derive flavor {flavorName} from label {baseLabel} because it already is a flavor")

			if baseLabel.type == Classification.TYPE_BINARY:
				label = self.createLabel(f"{baseLabel}+{flavorName}", Classification.TYPE_BINARY)
			elif baseLabel.type == Classification.TYPE_SOURCE:
				label = self.createLabel(f"{baseLabel}/{flavorName}", Classification.TYPE_BUILDCONFIG)
				sourceProject = baseLabel
			else:
				raise Exception(f"Cannot create flavor {flavorName} for {baseLabel.type} label {baseLabel}: unexpected type")

			label.parent = baseLabel
			label.flavorName = flavorName
			label.purposeName = baseLabel.purposeName

			baseLabel.addBuildFlavor(label)

			# Packages built for a specific purpose share the source project
			# of their base label by default...
			if sourceProject is None:
				sourceProject = baseLabel.sourceProject
			label.sourceProject = sourceProject

			# ... and share their requirements ...
			label.copyRequirementsFrom(baseLabel)

			# ... and, unless overridden, their build config
			if buildConfig is None:
				buildConfig = baseLabel.buildConfig
			label.setBuildConfig(buildConfig)

			# @Foo+blah always requires @Foo for runtime
			label.addRuntimeDependency(baseLabel)

			return label

		def createPurpose(self, baseLabel, purposeName, template = None):
			if baseLabel.purposeName is not None:
				infomsg(f"{baseLabel} isPurpose={baseLabel.isPurpose}")
				raise Exception(f"Cannot derive purpose {purposeName} from label {baseLabel} because it already has a purpose")

			label = self.createLabel(f"{baseLabel}-{purposeName}", baseLabel.type)
			label.parent = baseLabel
			label.flavorName = baseLabel.flavorName
			label.purposeName = purposeName

			# the new purpose label inherits the base label's gravity
			label.gravity = baseLabel.gravity

			baseLabel.addObjectPurpose(label)

			# Packages built for a specific purpose share the source project
			# of their base label ...
			label.sourceProject = baseLabel.sourceProject

			# ... and share their requirements ...
			label.copyRequirementsFrom(baseLabel)

			# ... and their build config
			label.setBuildConfig(baseLabel.buildConfig)

			# copy requirements from template, if given
			if template:
				label.copyRequirementsFrom(template)

			# There's a question whether @Foo-devel should always require @Foo for runtime.
			# While this makes sense for some purposes like man, it usually doesn't make sense
			# for others.
			# But it does make sense for things like devel packages (all devel packages that
			# come with a libfoo.so symlink have to require the underlying libfoo package)
			label.addRuntimeDependency(baseLabel)

			# Loop over all depdendencies of the base label and require their purpose-variants
			# ie if we're currently creating NetworkServices-devel, and NetworkServices requires
			# NetworkLibraries, then we make NetworkServices-devel require NetworkLibraries-devel
			for req in baseLabel.runtimeRequires:
				if not req.isPurpose:
					requirePurpose = req.getObjectPurpose(purposeName)
					if requirePurpose is None:
						requirePurpose = self.createPurpose(req, purposeName, template = template)
					label.addRuntimeDependency(requirePurpose)

			# if we're currently creating Blah+flavor-devel, require Blah-devel
			grandParent = baseLabel.parent
			if grandParent is not None:
				gpPurpose = grandParent.getObjectPurpose(purposeName)
				if gpPurpose is None:
					gpPurpose = self.createPurpose(grandParent, purposeName, template = template)
				label.addRuntimeDependency(gpPurpose)

			return label


		@property
		def allLabels(self):
			return sorted(self._labels.values(), key = lambda _: _.name)

		def createOrdering(self, labelType):
			if labelType != Classification.TYPE_BINARY:
				raise Exception(f"Unable to create an ordering for {labelType} labels")

			good = True

			order = PartialOrder(Classification.domain, "runtime dependency")
			for label in self._labels.values():
				if label.type is labelType:
					for rt in label.runtimeRequires:
						if rt.type != labelType:
							infomsg(f"Error: {label} requires label {rt}, which has incompatible type {rt.type}")
							good = False

					order.add(label, label.runtimeRequires)

			order.finalize()

			if not good:
				raise Exception("Consistency error in label tree")

			return order

		def defaultOrder(self):
			return self.createOrdering(Classification.TYPE_BINARY)

		def finalize(self):
			def inheritSourceProject(label):
				if label.sourceProject is None:
					if label.parent:
						source = inheritSourceProject(label.parent)
						if source:
							label.setSourceProject(source)
				return label.sourceProject

			def inheritBuildConfig(label):
				if label.buildConfig is None:
					if label.parent:
						source = inheritBuildConfig(label.parent)
						if source:
							label.setBuildConfig(source)
				return label.buildConfig

			for label in self._labels.values():
				if label.sourceProject is None:
					inheritSourceProject(label)
				if label.buildConfig is None:
					inheritBuildConfig(label)

				if label.type == Classification.TYPE_BINARY:
					if label.buildConfig is None and label.disposition != Classification.DISPOSITION_IGNORE:
						raise Exception(f"Label {label}: no buildconfig specified")
					elif not label.buildConfig.defined:
						raise Exception(f"Label {label} references buildconfig {label.buildConfig}, but it's not defined anywhere")

			# create a partial order but throw it away afterwards. the label hierarchy
			# is changing as part of this exercise
			order = self.createOrdering(Classification.TYPE_BINARY)
			for label in order.bottomUpTraversal():
				label.autoSelectCompatibleFlavors(order)

	class Reason(object):
		def __init__(self, pkg):
			self.package = pkg

		def reasonChain(self, package):
			if package is None or package.labelReason is None:
				result = ["<divine intervention>"]
			else:
				result = package.labelReason.chain()
			return result + [self]

		@property
		def originPackage(self):
			return self.package

	class ReasonFilter(Reason):
		def __init__(self, pkg, filterDesc):
			super().__init__(pkg)
			self.filterDesc = filterDesc

		@property
		def type(self):
			return 'filter'

		def chain(self):
			return [self]

		def __str__(self):
			return f"{self.package} identified by {self.filterDesc}"

	class ReasonPurpose(Reason):
		def __init__(self, pkg, purposeName, buildName):
			super().__init__(pkg)
			self.purposeName = purposeName
			self.buildName = buildName

		@property
		def type(self):
			return f"purpose:{self.purposeName}"

		def chain(self):
			return [self]

		def __str__(self):
			return f"{self.package} is a {self.purposeName} package of {self.buildName}"


	class ReasonRequires(Reason):
		def __init__(self, pkg, dependant, req):
			super().__init__(pkg)
			self.dependant = dependant
			self.req = req

		@property
		def type(self):
			return 'dependency'

		def chain(self):
			return self.reasonChain(self.dependant)

		@property
		def originPackage(self):
			return self.dependant.labelReason.originPackage

		def __str__(self):
			result = f"{self.package} required by {self.dependant}"
			if self.req is not None:
				result += f" via {self.req}"
			return result

	class ReasonSourcePackage(Reason):
		def __init__(self, pkg, binary):
			super().__init__(pkg)
			self.binary = binary

		@property
		def type(self):
			return 'source package'

		def chain(self):
			return self.reasonChain(self.binary)

		def __str__(self):
			return f"{self.package} is the source of {self.binary}"

	class ReasonSiblingPackage(Reason):
		def __init__(self, pkg, sibling):
			super().__init__(pkg)
			self.sibling = sibling

		@property
		def type(self):
			return 'sibling package'

		def chain(self):
			return self.reasonChain(self.sibling)

		def __str__(self):
			return f"{self.package} is a sibling package of {self.sibling}"

	class ReasonSiblingPlacement(Reason):
		def __init__(self, pkg, commonBaseLabel):
			super().__init__(pkg)
			self.baseLabel = commonBaseLabel

		@property
		def type(self):
			return 'sibling package'

		def chain(self):
			return super().chain()

		def __str__(self):
			return f"{self.package} siblings have common base label {self.baseLabel}"


	class ReasonRelatedPackage(ReasonSiblingPackage):
		def __init__(self, relationName, pkg, sibling):
			super().__init__(pkg, sibling)
			self.relation = relationName

		@property
		def type(self):
			return f"{self.relation} package"

		def chain(self):
			return self.reasonChain(self.sibling)

		def __str__(self):
			return f"{self.package} is a {self.relation} package related to {self.sibling}"

	class ReasonBuildDependency(Reason):
		def __init__(self, pkg, parentReason):
			super().__init__(pkg)
			self.parentReason = parentReason

		@property
		def type(self):
			return 'build requirement'

		def chain(self):
			return [self.parentReason]

		def __str__(self):
			return f"{self.package} builds {self.parentReason.package}"

	class ReasonSourceClosure(Reason):
		def __init__(self, pkg, sibling):
			super().__init__(pkg)
			self.sibling = sibling

		@property
		def type(self):
			return 'source'

		def chain(self):
			return self.reasonChain(self.sibling)

		def __str__(self):
			return f"{self.package} built from the same source as {self.sibling}"

	class ClassificationContext:
		def __init__(self, worker, productArchitecture, classificationScheme, labelOrder, store):
			self.worker = worker
			self.productArchitecture = productArchitecture
			self.classificationScheme = classificationScheme
			self.labelOrder = labelOrder
			self.store = store

	class Classifier(object):
		def __init__(self, label):
			self.label = label
			self.result = set()

	class BuildPackageClosure(Classifier):
		def __init__(self, problems, label, store, **kwargs):
			super().__init__(label, **kwargs)
			self.problems = problems
			self.store = store

		def handleSourceProjectConflict(self, build):
			self.problems.addSourceProjectConflict(build)

		def handleUnexpectedBuildDependency(self, pkg, build, required):
			self.problems.addUnexpectedBuildDependency(pkg, build.name, required)

		def enumerate(self, packages):
			alreadySeen = set()
			for rpm in packages:
				buildId = rpm.obsBuildId
				if buildId is None:
					infomsg(f"No OBS package for {rpm.shortname}")
					continue

				if buildId in alreadySeen:
					continue
				alreadySeen.add(buildId)

				build = self.store.retrieveOBSPackageByPackageId(rpm.backingStoreId)
				if build is None:
					infomsg(f"Could not find OBS package {buildId} for {rpm.shortname}")
					continue

				yield rpm, build

	class SiblingPackageClosure(BuildPackageClosure):
		def classify(self, packages):
			label = self.label
			sourceLabel = self.label.sourceProject

			result = set()

			for rpm, build in self.enumerate(packages):
				problematic = False

				for other in build.binaries:
					if other.isSourcePackage:
						# We're not going to label the source package for now
						continue

					if other.label is None:
						other.label = label
						other.labelReason = Classification.ReasonSiblingPackage(other, rpm)
						result.add(other)
					elif other.label is not rpm.label and \
					     (other.label.sourceProject is not rpm.label.sourceProject):
						# report the problem once when we're done with processing all packages
						problematic = True

						if False:
							infomsg(f"Source project conflict for {build.name}")
							infomsg(f"  {rpm.shortname} was labelled as {rpm.label}, built by {rpm.label.sourceProject}")
							infomsg(f"  {other.shortname} was labelled as {other.label}, built by {other.label.sourceProject}")

				if problematic:
					# infomsg(f"Adding SourceProjectConflict for {build.name}")
					self.handleSourceProjectConflict(build)

			self.result.update(result)
			return result

	class AutoflavorPackageClosure(BuildPackageClosure):
		def __init__(self, problemLog, store):
			super().__init__(problemLog, None, store)
			self.flavors = {}

		def addFlavor(self, name):
			if self.flavors.get(name) is None:
				self.flavors[name] = set()
			return self.flavors[name]

		def getFlavor(self, name):
			return self.flavors.get(name)

		def classify(self, packages):
			result = set()
			for rpm, build in self.enumerate(packages):
				for other in build.binaries:
					if other.label is None:
						continue
					if other.label.type == Classification.TYPE_AUTOFLAVOR:
						# infomsg(f"### identified {other.shortname} as a {other.label.name} package")
						self.addFlavor(other.label.name).add((rpm, other))

		def labelFlavoredPackages(self, flavorName, label):
			result = set()

			matching = self.getFlavor(flavorName)
			if matching:
				for rpm, other in matching:
					# infomsg(f"::: label {other.shortname} as {label}")
					other.label = label
					other.labelReason = Classification.ReasonRelatedPackage(flavorName, other, rpm)
					result.add(other)
			return result

	class RelatedPackageClosure(BuildPackageClosure):
		def classify(self, packages):
			relation = self.RELATION
			label = self.label

			result = set()
			for rpm, build in self.enumerate(packages):
				if relation.checkPackage(rpm):
					infomsg(f"Found {relation.NAME} package {rpm.shortname} in non-{relation.NAME} group {rpm.label}")
					continue

				for other in build.binaries:
					if other.label is None and relation.checkPackage(other):
						infomsg(f"### identified {other.shortname} as a {relation.NAME} package")
						other.label = label
						other.labelReason = Classification.ReasonRelatedPackage(relation, other, rpm)
						result.add(other)

			self.result.update(result)
			return result

	class BuildRequiresClosure(BuildPackageClosure):
		def __init__(self, problemLog, label, store):
			super().__init__(problemLog, label, store)
			self.flavors = {}
			self.unlabelledPackages = None

		def classify(self, packages):
			# this is broken right now
			buildClosure = XXX
			unlabelledPackages = set()
			result = set()

			seen = set()
			for rpm, build in self.enumerate(packages):
				if build in seen:
					continue
				seen.add(build)

				for req in build.buildRequires:
					if req.label is None:
						unlabelledPackages.add((rpm, build.name, req))
					elif req.label not in buildClosure:
						self.handleUnexpectedBuildDependency(rpm, build, req)
					else:
						result.add(req)

			self.unlabelledPackages = unlabelledPackages
			return result

	class DependencyClassifier(Classifier):
		def __init__(self, classificationContext, label):
			super().__init__(label)

			self.worker = classificationContext.worker
			self.context = self.worker.contextForArch(classificationContext.productArchitecture)
			self.classificationScheme = classificationContext.classificationScheme
			self.labelOrder = classificationContext.labelOrder
			self.store = classificationContext.store

		def handleUnresolvableDependency(self, pkg, dep):
			self.worker.problems.addUnableToResolve(pkg, dep)

		def handleUnexpectedDependency(self, pkg, reason):
			self.worker.problems.addUnexpectedDependency(self.label, reason, pkg)

		def handleUnlabelledBuildDependency(self, originPackage, buildName, requiredPackage):
			self.worker.problems.addUnlabelledBuildDependency(originPackage, buildName, requiredPackage)

		def handleMissingSource(self, pkg, reason):
			self.worker.problems.addMissingSource(pkg, reason)

		# a is a known dependency of b iff a <= b in the label order
		def isKnownDependency(self, a, b):
			if a in self.labelOrder.downwardClosureFor(b):
				return True

			if a.buildConfig and a.buildConfig == b.buildConfig:
				return True

			if False:
				infomsg(f"label {a} is not a good dependency of {b} [buildconf {a.buildConfig} vs {b.buildConfig}]")
				names = map(str, self.labelOrder[b]._downwardClosure)
				infomsg(f" -> {' '.join(names)}")

			return False

		def debugMsg(self, msg):
			self.worker.debugMsg(msg)

	class DownwardClosure(DependencyClassifier):
		def __init__(self, *args):
			super().__init__(*args)
			self.transform = None

		def edges(self, pkg):
			result = []
			for dep, target in self.context.resolveDownward(pkg):
				result.append(Classification.ReasonRequires(target, pkg, dep))

			return result

		def followEdge(self, edge):
			if self.transform:
				edge = self.transform(edge)
				if edge is None:
					return None

			pkg = edge.package
			if pkg.label is self.label:
				return None

			if pkg.label is not None:
				# if the package belongs to a group that is within our closure,
				# we're fine. Otherwise, flag this dependency as bad
				if not self.isKnownDependency(pkg.label, self.label):
					self.handleUnexpectedDependency(pkg, edge)

				# Do not recurse into this package
				return None

			# infomsg(f"Label {self.label}: classify {edge}")
			pkg.label = self.label
			pkg.labelReason = edge

			self.result.add(pkg)
			return pkg

		def classify(self, packages):
			for pkg in packages:
				assert(pkg.label is self.label)

			self.result.update(set(packages))

			worker = self.worker
			worker.update(packages)
			while True:
				pkg = worker.next()
				if pkg is None:
					break

				edges = self.edges(pkg)
				for e in edges:
					tgt = self.followEdge(e)
					if tgt is not None:
						worker.add(tgt)

			return True

	class UnknownPackageClassifier(DownwardClosure):
		def __init__(self, *args):
			super().__init__(*args)
			self.suggested = {}

		def classify(self, problematicItems):
			worker = self.worker
			for originPackage, buildName, unlabelledPackage in problematicItems:
				if unlabelledPackage.label:
					continue

				worker.add(unlabelledPackage)
				labelClosure = set()
				incrementalPackageClosure = set()

				while True:
					pkg = worker.next()
					if pkg is None:
						break
					for dep, target in self.context.resolveDownward(self.preferences, pkg):
						if target.label is None:
							incrementalPackageClosure.add(target)
							# why are we recursing?
							worker.add(target)
						else:
							labelClosure.add(target.label)

				suggestedLabel = None

				if incrementalPackageClosure:
					# package has dependencies that have not been labelled, either
					# we check for a "good" label recursively, but I'm shying away
					# from the complexity. Or we report the issue.
					self.handleUnlabelledBuildDependency(originPackage, buildName, unlabelledPackage)
				else:
					suggestedLabel = self.findTopmostLabel(labelClosure)

				if suggestedLabel is not None:
					self.suggestLabel(unlabelledPackage, suggestedLabel)
				else:
					pass
					# FIXME: add a problem report

				continue
				names = sorted(_.name for _ in labelClosure)
				infomsg(f"{unlabelledPackage.shortname} -> {' '.join(names)}")
				for pkg in incrementalPackageClosure:
					infomsg(f"  {pkg.shortname} [{pkg.sourceName}]")

		@property
		def suggestions(self):
			result = []
			for label in sorted(self.suggested.keys(), key = lambda _: _.name):
				result.append((label, self.suggested[label]))
			return result

		def suggestLabel(self, pkg, label):
			try:
				suggestions = self.suggested[label]
			except:
				suggestions = []
				self.suggested[label] = suggestions
			suggestions.append(pkg)

		def findTopmostLabel(self, labels):
			# removed code that no longer worked. Need to do this by using PartialOrder.maximum()
			assert(0)

	class BuildRequireClosure(DownwardClosure):
		def __init__(self, *args):
			super().__init__(*args)
			self.transform = self.transformSource

		def classify(self, packages):
			label = self.label

			sources = set()
			for binary in packages:
				assert(binary.arch != 'src')
				src = binary.sourcePackage
				if src is None:
					# add problem to worker
					warnmsg(f"No source for {binary.fullname()} {binary.arch}")
					continue

				if src.label and src.label is not label:
					# add problem to worker
					infomsg(f"Problem with {src.fullname()}: label {label} vs {src.label}")
					continue

				# infomsg(f"label {src.name} as {label}")
				src.label = label
				src.labelReason = Classification.ReasonSourcePackage(src, binary)

				sources.add(src)

			return super().classify(sources)

		def transformSource(self, arg):
			if isinstance(arg, Classification.Reason):
				reason = arg

				binary = reason.package
				src = binary.sourcePackage
				if src is None:
					infomsg(f"No source for {binary.fullname()}")
					self.handleMissingSource(binary, reason)
					return None

				src.label = self.label
				src.labelReason = Classification.ReasonBuildDependency(src, reason)
				return src.labelReason

			raise Exception()

class PotentialClassification(object):
	class Verdict:
		def __init__(self):
			self._placements = []

		def add(self, node):
			assert(node.solution)
			for pkg in node.packages:
				# third element in tuple should be the label reason
				self._placements.append((pkg, node.solution, None))

		def __iter__(self):
			return iter(self._placements)

	def __init__(self, solvingTree):
		self.solvingTree = solvingTree
		self._preferences = self.PlacementPreferences()

		self._recentlyPlaced = []

	@property
	def labelOrder(self):
		return self.solvingTree._order

	@property
	def classificationScheme(self):
		return self.solvingTree._classificationScheme

	def getPackageNode(self, pkg):
		return self.solvingTree.getPackage(pkg)

	class NodeVersusLabelSetReport:
		class LabelSet:
			def __init__(self, key):
				self.key = key
				self.names = []

		def __init__(self):
			self.byLabels = {}

		def add(self, nodeName, labels):
			key = ' '.join(sorted(map(str, labels)))
			info = self.byLabels.get(key)
			if info is None:
				info = self.LabelSet(key)
				self.byLabels[key] = info
			info.names.append(nodeName)

		def display(self, indent = ""):
			output = lambda msg: infomsg(indent + msg)

			for key, info in self.byLabels.items():
				it = iter(info.names)
				if len(key) > 20:
					output(f"    {key}")
				else:
					first = next(it)
					output(f"    {key:20} {first}")

				for name in it:
					output(f"    {'':20} {name}")

	def recordInitialPlacements(self):
		# Create initial _recentlyPlaced list
		self._recentlyPlaced = []
		for node in self.solvingTree.randomWalk():
			if node.solution:
				self._recentlyPlaced.append(node)
				if node.siblings is not None:
					node.siblings.recordDecision(node, node.solution)

	def ignoreFlavoredPackages(self, order, tabooFlavorNames):
		hidden = set()
		for interval in self._packages.values():
			if interval.package and interval.package.label and interval.package.label.flavorName in tabooFlavorNames:
				hidden.add(interval)

		# un-hide any packages that are required by a visible package
		for interval in order.topDownTraversal():
			if interval not in hidden:
				hidden.difference_update(interval._lowerNeighbors)

		order.hide(hidden)

	def placeSiblingsAccordingToPurpose(self, order):
		builds = []
		seen = set()
		for node in order.bottomUpTraversal(self._recentlyPlaced):
			if node.siblings is not None:
				if node.siblings not in seen:
					builds.append(node.siblings)
					seen.add(node.siblings)

		for siblings in builds:
			candidateLabels = Classification.createLabelSet()
			purposes = []
			for pkg in siblings:
				node = self.getPackageNode(pkg)
				if node.solution is not None:
					label = node.solution
					if label.isPurpose:
						label = label.parent
					candidateLabels.add(label)
				else:
					label = pkg.label
					# The node may represent a collapsed cycle. See if the packages that
					# were collapsed share a common purpose
					if label is None:
						label = node.commonLabel
					if label and label.type == Classification.TYPE_PURPOSE:
						purposes.append((pkg, label))

			if not purposes or not candidateLabels:
				continue

			for siblingPackage, siblingPurposeLabel in purposes:
				sibNode = self.getPackageNode(siblingPackage)

				if sibNode.solution is not None:
					warnmsg(f"{siblingPackage} was already placed through a different sibling (most likely it's part of a dependency cycle)")
					continue

				candidatePurposes = Classification.createLabelSet(map(lambda label: label.getObjectPurpose(siblingPurposeLabel.name), candidateLabels))

				labels = sibNode.filterCandidateLabels(candidatePurposes)
				if not labels:
					lnames = map(str, candidatePurposes)
					infomsg(f"Unable to place {siblingPackage} (obs package {siblings}): no matching label; candidates {' '.join(lnames)}")

					# put this into a separate function:
					for cursor in self.Traversal(sibNode):
						neigh = cursor.node

						match = neigh.filterCandidateLabels(candidatePurposes, quiet = True)
						if match == candidatePurposes:
							continue

						match = map(str, match)
						infomsg(f"{cursor}; match <{' '.join(match)}>")

						if cursor.depth < 5:
							cursor.descend()
					continue

				if len(labels) > 1:
					# FIXME: If there is more than one base label, this means some siblings were placed by
					# the user. In this case, we should try all candidate labels in turn and see if
					# our package would fit any of these.
					lnames = map(str, candidatePurposes)
					infomsg(f"Unable to place {siblingPackage} (obs package {siblings}): ambiguous labels {' '.join(lnames)}")
					continue

				purposeLabel = next(iter(labels))
				if siblingPurposeLabel.disposition == Classification.DISPOSITION_MERGE:
					purposeLabel = purposeLabel.parent

				if not sibNode.labelIsValidCandidate(purposeLabel):
					errormsg(f"Uh-oh. {purposeLabel} is not a valid candidate for {sibNode}")
					boundingSet = sibNode.upperCone
					if boundingSet != None and purposeLabel not in boundingSet:
						infomsg(f"    conflicts with some package that require it")

					boundingSet = sibNode.lowerCone
					if boundingSet != None and purposeLabel not in boundingSet:
						missing = self._order.downwardClosureFor(purposeLabel).difference(boundingSet)
						if purposeLabel in missing:
							missing.remove(purposeLabel)
						missing = self._order.maxima(missing)
						if len(missing) < 6:
							infomsg(f"    missing requirements: {' '.join(map(str, missing))}")
						else:
							infomsg(f"    missing {len(missing)} requirements")


				infomsg(f"{siblingPackage} will be placed in {purposeLabel}; close to its siblings")
				reason = Classification.ReasonPurpose(siblingPackage, str(siblingPurposeLabel), siblings.name)
				self.recordDecision(sibNode, purposeLabel, reason)

		self._recentlyPlaced = []

	class PlacementConstraints:
		def __init__(self):
			self.validComponents = None
			self.validBaseLabels = None

		def addValidComponent(self, name):
			if self.validComponents is None:
				self.validComponents = set()
			self.validComponents.add(name)

		def addValidBaseLabel(self, name):
			if self.validBaseLabels is None:
				self.validBaseLabels = Classification.createLabelSet()
			self.validBaseLabels.add(name)

		def preFilterCandidateLabels(self, candidates, flavor = None, purpose = None):
			# everything goes
			if candidates is None:
				return candidates

			if flavor:
				candidates = Classification.createLabelSet(filter(lambda label: label.flavorName == flavor, candidates))
			if purpose:
				candidates = Classification.createLabelSet(filter(lambda label: label.purposeName == purpose, candidates))
			return candidates

		def constrainComponents(self, packagePlacement):
			if packagePlacement.candidates is not None and self.validComponents is not None:
				preferred = Classification.createLabelSet(filter(lambda label: label.componentName in self.validComponents, packagePlacement.candidates))
				if packagePlacement.trace:
					diff = packagePlacement.candidates.difference(preferred)
					infomsg(f" {packagePlacement}: constraining further by removing {' '.join(map(str, diff))}")
				packagePlacement.candidates = preferred

	class PlacementPreferences(object):
		class Hint:
			def __init__(self, preferredLabel, others):
				self.preferred = preferredLabel
				self.others = Classification.createLabelSet(others)
				if preferredLabel in self.others:
					self.others.remove(preferredLabel)

		def __init__(self):
			self._prefs = []

		def add(self, preferredLabel, others):
			self._prefs.append(self.Hint(preferredLabel, others))

		def filterCandidates(self, candidates):
			if not self._prefs:
				return candidates

			for hint in self._prefs:
				if hint.preferred in candidates:
					candidates = candidates.difference(hint.others)

			return candidates

	class PackagePlacement(object):
		def __init__(self, labelOrder, node, label = None):
			# maybe the node should refer to this placement, not the other way around
			self.labelOrder = labelOrder
			self.name = str(node)
			self.node = node
			self.label = label
			self.labelReason = None
			self.autoLabel = None
			self.failed = False
			self.trace = False

		def __str__(self):
			return str(self.node)

		@property
		def isSolved(self):
			return bool(self.label)

		@property
		def isFinal(self):
			return bool(self.label) or self.failed

	class DefinitivePackagePlacement(PackagePlacement):
		def __init__(self, labelOrder, node):
			super().__init__(labelOrder, node, label = node.solution)

		@property
		def baseLabels(self):
			return Classification.createLabelSet((self.label.baseLabel, ))

		def reportVerdict(self, node, verdict):
			pass

	class TentativePackagePlacement(PackagePlacement):
		def __init__(self, labelOrder, node, preferences):
			super().__init__(labelOrder, node)

			self.preferences = preferences
			self.candidates = node.candidates
			self.flavor = None
			self.purpose = None

			self.constrainedAbove = bool(node.upperNeighbors)
			self.constrainedBelow = bool(node.lowerNeighbors)

		def fail(self, msg):
			errormsg(f"{self}: {msg}")
			self.failed = True
			return False

		def setSolution(self, label, labelReason = None):
			self.label = label
			self.labelReason = labelReason

		def setSolutionFromBaseLabel(self, choice, baseLabel):
			infomsg(f"{self} is placed in {choice} (optimal label based on base label {baseLabel})")
			self.setSolution(choice)

		def reportVerdict(self, node, verdict):
			if self.label:
				if node.solution and node.solution is not self.label:
					errormsg(f"BUG: Placement algorithm is trying to change label for {node} from {node.solution} to {self.label}")
				node.solution = self.label
				verdict.add(node)

		@property
		def baseLabels(self):
			if self.candidates is None:
				return None

			return Classification.createLabelSet(map(lambda label: label.baseLabel, self.candidates))

		def applyConstraints(self, constraints):
			if not self.candidates:
				return

			constraints.constrainComponents(self)

		# The node corresponds to a package that has been auto-labelled as "devel" (purpose)
		# or "python" (flavor). Reduce the list of candidates to those that have a matching
		# purpose or flavor
		def applyFlavorOrPurpose(self, label):
			candidates = self.candidates

			# if, originally, the set of candidates is completely unconstrained,
			# we now have to whittle those down to a specific subset
			if candidates is None:
				candidates = self.labelOrder.allkeys

			if label.disposition == Classification.DISPOSITION_MERGE:
				infomsg(f"Not constraining {self} by {label.type} label {label} due to disposition {label.disposition}")
				return

			if label.type == Classification.TYPE_AUTOFLAVOR:
				flavorName = label.name
				if self.flavor is not None and self.flavor is not label:
					return self.fail(f"conflicting purposes {self.flavor} and {flavorName} - this will never work")

				self.candidates = Classification.createLabelSet(filter(lambda label: label.flavorName == flavorName, candidates))

				# if the autoflavor has a disposition of maybe_merge, check for any base labels that
				# cover all requirements of the autoflavor
				if label.disposition == Classification.DISPOSITION_MAYBE_MERGE:
					merged = Classification.createLabelSet(filter(lambda l: l.parent is None and l.autoFlavorCanBeMerged(label), candidates))
					self.candidates.update(merged)
					if self.trace:
						infomsg(f"   {self}: {label} got merged into {len(merged)} candidates")

				self.flavor = label
			elif label.type == Classification.TYPE_PURPOSE:
				purposeName = label.name
				if self.purpose is not None and self.purpose is not label:
					return self.fail(f"conflicting purposes {self.purpose} and {purposeName} - this will never work")

				self.candidates = Classification.createLabelSet(filter(lambda label: label.purposeName == purposeName, candidates))
				self.purpose = label
			else:
				raise Exception(f"{self}: Unexpected label {label} type {label.type}")

			debugmsg(f"{self} constrained by {label.type} {label}")
			return True

		def trivialChecks(self):
			if self.candidates is None:
				# can be placed anywhere
				return False

			numCandidates = len(self.candidates)
			if numCandidates == 1:
				label = next(iter(self.candidates))
				infomsg(f"{self.node} has exactly one candidate label, {label}")
				self.setSolution(label)
				return True

			if numCandidates == 0:
				infomsg(f"{self.node} cannot be placed; no candidate labels")
				self.failed = True
				return True

			return False

		def tryToPlaceTopDown(self, node):
			if self.label is not None:
				return

			if not node.upperNeighbors:
				if self.candidates is None:
					infomsg(f"{node} has no parent and no constraints; please provide a hint where to place it")
					return

				if not self.candidates:
					return

				max = self.labelOrder.maxima(self.candidates)
				if len(max) == 1:
					maxLabel = next(iter(max))
					infomsg(f"{self.node} has no upper neighbors; best candidate is {maxLabel}")
					infomsg(f"   {renderLabelSet('candidates', self.candidates)}")
					self.setSolution(maxLabel)
					return True

				baseLabels = Classification.createLabelSet(label.baseLabel for label in max)
				if len(baseLabels) == 1:
					maxLabel = next(iter(baseLabels))
					if maxLabel in self.candidates:
						choice = self.deriveChoiceFromBaseLabel(maxLabel)

						if choice:
							self.setSolutionFromBaseLabel(choice, maxLabel)
							return True

				infomsg(f"{node} has no parent and ambiguous constraints {renderLabelSet('max candidates', max)}")
				return

			labels = Classification.createLabelSet()
			for neigh in node.upperNeighbors:
				if neigh.placement is None:
					infomsg(f"Why on earth does {neigh} not have a placement object?")
					continue
				if neigh.placement.label is None:
					return

				labels.add(neigh.placement.label)

			# xxx

		# if a build produces exactly one package, we do not have to consider any
		# sibling constraints. Just place it
		# Returns True if the decision was final
		def onlyChildCheck(self):
			if self.candidates is None:
				infomsg(f"{self.node} has no siblings and can be placed anywhere. Please provide a hint in the configuration")
				return True

			candidates = self.preferences.filterCandidates(self.candidates)
			best = self.labelOrder.maxima(candidates)

			if not best:
				infomsg(f"{self.node}: we blew it; list of candidates reduced to empty set")
				return True

			if len(best) == 1:
				label = next(iter(best))
				infomsg(f"{self.node} has no siblings; best candidate is {label}")
				self.setSolution(label)
				return True

			return False

		def deriveChoiceFromBaseLabel(self, baseLabel):
			candidates = self.candidates

			if self.trace:
				infomsg(f"{self}: trying to derive from {baseLabel}")
				displayLabelSetFull(candidates, indent = "   ")

			if candidates is None or baseLabel in candidates:
				# if the node can be placed anywhere, or if the base label is
				# a candidate, choose the base label
				if self.trace:
					infomsg(f"{self}: {baseLabel} is a valid candidate")
				return baseLabel
			else:
				# filter those candidates that have the chosen base label
				candidates = Classification.createLabelSet(filter(lambda label: label.baseLabel == baseLabel, candidates))

				if self.trace:
					infomsg(f"### {self} candidates={' '.join(map(str, candidates))}")

				if not candidates:
					return None

				# Reduce [@Foo-doc, @Foo+flavor1-doc, @Foo+flavor2-doc, ...] to @Foo-doc
				if len(candidates) > 1:
					minimum = self.labelOrder.minimumOf(candidates)
					if minimum is not None:
						return minimum

					# If the package has not been labelled for a specific purpose, check if
					# we get better results by hiding all candidates that do have one
					if self.purpose is None:
						generic = Classification.createLabelSet(filter(lambda label: label.purposeName == None, candidates))
						if generic:
							candidates = generic

#				if len(candidates) > 1:
#					candidates = self.labelOrder.maxima(candidates)

				if len(candidates) > 1:
					if self.trace:
						infomsg(f"{self.node} is still ambiguous [{' '.join(map(str, candidates))}]")
					return None

				choice = next(iter(candidates))
			return choice

		def solveToBaseLabel(self, baseLabel):
			if self.label:
				return True

			choice = self.deriveChoiceFromBaseLabel(baseLabel)
			if choice is None:
				return False

			self.setSolutionFromBaseLabel(choice, baseLabel)
			return True

	class TentativeBuildPlacement:
		def __init__(self, name, labelOrder, preferences):
			self.name = name
			self.labelOrder = labelOrder
			self.preferences = preferences
			self.constraints = PotentialClassification.PlacementConstraints()
			self.packageCount = 0

			self.children = []
			self.packageDict = {}

			self.triedBaseLabels = Classification.createLabelSet()
			self.goodBaseLabels = {}

			self.trace = False

		def __str__(self):
			return self.name

		def addPackagePlacement(self, pkg, packagePlacement):
			self.children.append(packagePlacement)
			self.packageDict[pkg] = packagePlacement
			self.packageCount += 1

			if packagePlacement.trace:
				self.trace = True

		@property
		def isFinal(self):
			return all(placement.isFinal for placement in self.children)

		@property
		def isSolved(self):
			return all(placement.isSolved for placement in self.children)

		@property
		def solved(self):
			return list(filter(lambda p: p.isSolved, self.children))

		@property
		def unsolved(self):
			return list(filter(lambda p: not p.isSolved, self.children))

		@property
		def numPackages(self):
			return self.packageCount

		@property
		def numSolved(self):
			return self.packageCount - len(self.unsolved)

		def addDefinitivePlacement(self, pkg, node, label):
			component = label.componentName
			if component is not None:
				self.constraints.addValidComponent(component)

			# what is this supposed to do?
			# self.constraints.addValidBaseLabel(label.baseFlavors)

			# the node may represent a collapsed cycle, in which case node.placement may already have
			# been set. Just adopt the placement that is already there
			if node.placement is None:
				node.placement = PotentialClassification.DefinitivePackagePlacement(self.labelOrder, node)

			self.addPackagePlacement(pkg, node.placement)
			return node.placement

		def addTentativePlacement(self, pkg, node):
			# the node may represent a collapsed cycle, in which case node.placement may already have
			# been set. Just adopt the placement that is already there
			if node.placement is None:
				node.placement = PotentialClassification.TentativePackagePlacement(self.labelOrder, node, self.preferences)
				if pkg.label is not None:
					node.placement.applyFlavorOrPurpose(pkg.label)
					if pkg.label.type in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
						node.placement.autoLabel = pkg.label

			self.addPackagePlacement(pkg, node.placement)

			if node.upperNeighbors:
				self.constrainedAbove = True
			if node.lowerNeighbors:
				self.constrainedBelow = True

			if pkg.trace:
				node.placement.trace = True

			return node.placement

		def applyConstraints(self):
			for packagePlacement in self.unsolved:
				packagePlacement.applyConstraints(self.constraints)

		def solveTrivialCases(self):
			for packagePlacement in self.unsolved:
				packagePlacement.trivialChecks()

			if self.packageCount == 1 and not self.isFinal:
				packagePlacement = self.unsolved[0]
				packagePlacement.onlyChildCheck()

			return self.isFinal

		class BaseLabelSolution:
			def __init__(self, baseLabel):
				self.baseLabel = baseLabel
				self.placements = []

			def __str__(self):
				return self.baseLabel.name

			def add(self, packagePlacement, label):
				self.placements.append((packagePlacement, label))

			def __iter__(self):
				return iter(self.placements)

		def canSolveUsingBaseLabel(self, baseLabel):
			if baseLabel in self.triedBaseLabels:
				return self.goodBaseLabels[baseLabel]
			self.triedBaseLabels.add(baseLabel)

			result = self.BaseLabelSolution(baseLabel)
			potentialSolution = []
			for packagePlacement in self.unsolved:
				choice = packagePlacement.deriveChoiceFromBaseLabel(baseLabel)
				if choice is None:
					if self.trace:
						infomsg(f"{self}: incompatible base label {baseLabel} - no candidate for {packagePlacement}")
					self.goodBaseLabels[baseLabel] = None
					return None

				result.add(packagePlacement, choice)

			self.goodBaseLabels[baseLabel] = result
			return result

		def tryToSolveUsingBaseLabel(self, baseLabel, desc):
			infomsg(f"{self}: try to solve using {desc} {baseLabel}")

			potentialSolution = self.canSolveUsingBaseLabel(baseLabel)
			if not potentialSolution:
				return False

			for packagePlacement, choice in potentialSolution:
				packagePlacement.setSolutionFromBaseLabel(choice, baseLabel) 
			return True

		# look for packages that have been labelled for a flavor with a defaultlabel.
		# If so, check whether this could be a good base label for placing the entire package.
		# Do this only for packages that have none of their rpms labelled yet
		def solveDefaultBaseLabel(self):
			if self.solved:
				return

			defaultLabel = None
			for packagePlacement in self.unsolved:
				if packagePlacement.autoLabel and packagePlacement.autoLabel.defaultLabel:
					cand = packagePlacement.autoLabel.defaultLabel
					if defaultLabel is None:
						defaultLabel = cand
					elif defaultLabel is not cand:
						infomsg(f"{self} has packages with conflicting default labels {defaultLabel} and {cand}")
						return False

			if defaultLabel is None:
				return False

			if self.tryToSolveUsingBaseLabel(defaultLabel, "default base label"):
				return True

			return False

		# we're dealing with several packages; see whether they share any common base label(s)
		# and try to determine the "best" choice
		def solveCommonBaseLabel(self):
			commonBaseLabels = None
			for packagePlacement in self.children:
				commonBaseLabels = intersectSets(commonBaseLabels, packagePlacement.baseLabels)

			if commonBaseLabels is None:
				return False

			if not commonBaseLabels:
				infomsg(f"{self} has no common base labels");
				return False

			infomsg(f"{self} has common base labels {' '.join(map(str, commonBaseLabels))}");
			# commonBaseLabels = self.preferences.filterCandidates(commonBaseLabels)

			goodLabels = Classification.createLabelSet()
			for baseLabel in commonBaseLabels:
				if self.canSolveUsingBaseLabel(baseLabel):
					if self.trace: infomsg(f"   + {baseLabel}")
					goodLabels.add(baseLabel)
				else:
					if self.trace: infomsg(f"   - {baseLabel}")

			if not goodLabels:
				infomsg(f"{self} has common base labels, but none of them can solve");
				return False

			if len(goodLabels) == 1:
				bestLabel = next(iter(goodLabels))
			else:
				bestLabel = self.labelOrder.maximumOf(goodLabels)

			if bestLabel:
				return self.tryToSolveUsingBaseLabel(bestLabel, "common base label")

			infomsg(f"{self}: found several compatible base labels: {renderLabelSet('good', goodLabels)}")
			return False

		# for all the children that have been placed so far, loop over their base
		# labels and see if there's a common maximum. If so, try to place all remaining
		# packages with this base label
		def solveCompatibleBaseLabel(self):
			if not self.solved:
				return False

			compatibleBaseLabels = Classification.createLabelSet()
			for placement in self.solved:
				baseLabel = placement.label.baseLabel
				if self.canSolveUsingBaseLabel(baseLabel):
					compatibleBaseLabels.add(baseLabel)

			compatibleBaseLabels = self.preferences.filterCandidates(compatibleBaseLabels)
			infomsg(f"Trying to solve {self} using all base labels {' '.join(map(str, compatibleBaseLabels))}")

			maxBaseLabel = self.labelOrder.maximumOf(compatibleBaseLabels)
			if maxBaseLabel is None:
				if True:
					maxes = self.labelOrder.maxima(compatibleBaseLabels)
					infomsg(f"   no single maximum label; max={renderLabelSet('maxima', maxes)}")
				return False

			infomsg(f"    reduced list of all base labels to {maxBaseLabel}")
			return self.tryToSolveUsingBaseLabel(maxBaseLabel, "max base label")

		# For a package like libfoo-devel, remove the suffix
		# This assumes that there is only one matching package suffix; as soon as someone
		# starts introducing ambiguous suffixes (like -32bit-devel vs -devel), we're in trouble
		def removePurposeSuffixFromPackageName(self, pkg, purpose):
			for suffix in purpose.packageSuffixes:
				if pkg.name.endswith(suffix):
					return pkg.name[:-len(suffix)].rstrip('-')
			return None

		# place systemd-mini-devel close to systemd-mini
		def solvePurposeRelativeToSibling(self, classificationScheme):
			# if we have no solved siblings yet, don't even bother
			if self.numSolved == 0:
				return False

			namesToPlacements = {}
			toBeExamined = []
			for pkg, packagePlacement in self.packageDict.items():
				if packagePlacement.label is not None:
					namesToPlacements[pkg.name] = packagePlacement
					label = packagePlacement.label

					# When we've already placed -devel, this rule helps placing -devel-static next to it
					if label.isPurpose:
						# find the underlying purpose label (which has the suffixes)
						purpose = classificationScheme.getLabel(label.purposeName)

						# then, see if the package name is of the form $stem-$suffix, and if
						# so, remove the suffix
						baseName = self.removePurposeSuffixFromPackageName(pkg, purpose)
					else:
						baseName = pkg.name

					if baseName is not None:
						namesToPlacements[baseName] = packagePlacement
						infomsg(f"    {baseName} -> {packagePlacement}")

						# FIXME: this encodes the SUSE lib package naming convention
						# shorten librsvg-2-2 to librsvg
						if baseName.startswith("lib"):
							baseName = baseName.rstrip("-0123456789")
							namesToPlacements[baseName] = packagePlacement
							infomsg(f"    {baseName} -> {packagePlacement}")

				elif packagePlacement.purpose and packagePlacement.purpose.packageSuffixes:
					toBeExamined.append((pkg, packagePlacement))

			if not toBeExamined:
				return False

			for pkg, packagePlacement in toBeExamined:
				purpose = packagePlacement.purpose
				infomsg(f"{pkg} is a {purpose} package; look for favorite siblings")

				baseName = self.removePurposeSuffixFromPackageName(pkg, purpose)
				if baseName is None:
					continue

				infomsg(f"    {pkg} baseName={baseName}")

				favoriteSibling = namesToPlacements.get(baseName)
				infomsg(f"       try {baseName} -> {favoriteSibling}")
				if favoriteSibling is None and baseName.startswith("lib"):
					baseName = baseName.rstrip("-0123456789")
					favoriteSibling = namesToPlacements.get(baseName)
					infomsg(f"       try {baseName} -> {favoriteSibling}")
					if favoriteSibling is None:
						# still no cigar; try and remove the "lib" prefix as well
						baseName = baseName[3:]
						favoriteSibling = namesToPlacements.get(baseName)
						infomsg(f"       try {baseName} -> {favoriteSibling}")

				if favoriteSibling is None:
					continue

				infomsg(f"    {pkg} favorite sibling={favoriteSibling}")
				label = favoriteSibling.label
				if label.isPurpose:
					label = label.parent

				choice = packagePlacement.deriveChoiceFromBaseLabel(label)
				if choice is None:
					infomsg(f"{purpose} package {pkg} has favorite sibling {favoriteSibling}, but {label} is not a good base label for it")
					continue

				infomsg(f"{pkg} is placed in {choice} (based on favorite sibling {favoriteSibling} and purpose {purpose})")
				packagePlacement.setSolution(choice)

			return self.isFinal

		def reportRemaining(self):
			remaining = self.unsolved

			infomsg(f" - {self}: {self.numSolved}/{self.numPackages} solved; {len(remaining)} remain")
			for packagePlacement in self.children:
				if packagePlacement.label:
					infomsg(f"    + {packagePlacement} (solved); labelled as {packagePlacement.label}")
					continue

				status = "unsolved"
				if packagePlacement.failed:
					status = "FAILED"

				if packagePlacement.constrainedAbove:
					extra = renderLabelSet("candidates", packagePlacement.candidates)

					maxBaseLabels = None
					if packagePlacement.baseLabels is not None:
						maxBaseLabels = self.labelOrder.maxima(packagePlacement.baseLabels)
					extra2 = renderLabelSet("max base labels", maxBaseLabels)
				else:
					extra = "nothing requires this package"

					minBaseLabels = None
					if packagePlacement.baseLabels is not None:
						minBaseLabels = self.labelOrder.minima(packagePlacement.baseLabels)
					extra2 = renderLabelSet("min base labels", minBaseLabels)

				infomsg(f"    - {packagePlacement} ({status}); {extra}")
				infomsg(f"         {extra2}")

			if self.goodBaseLabels:
				extra = renderLabelSet('compatible base labels', sorted(map(str, self.goodBaseLabels.keys())))
				infomsg(f"   {extra}")

	def definePreference(self, preferredName, otherNames):
		def getLabel(name):
			label = self.classificationScheme.getLabel(name)
			if label is None:
				raise Exception(f"Unknown label {name}")
			return label

		preferredLabel = getLabel(preferredName)
		others = set(map(getLabel, otherNames))
		if preferredLabel in others:
			others.remove(preferredLabel)

		self._preferences.add(preferredLabel, others)

	def createBuildPlacement(self, buildInfo):
		buildPlacement = self.TentativeBuildPlacement(buildInfo.name, self.labelOrder, self._preferences)

		# First, loop over all packages that this build produces, and add them to the
		# build placement
		for pkg in buildInfo:
			node = self.getPackageNode(pkg)
			if node.solution is not None:
				buildPlacement.addDefinitivePlacement(pkg, node, node.solution)
			else:
				buildPlacement.addTentativePlacement(pkg, node)

		# Then, apply constraints (right now, just the valid component name(s))
		buildPlacement.applyConstraints()
		return buildPlacement

	def solveBuildPlacement(self, tentativePlacement):
		if tentativePlacement.isFinal:
			return

		infomsg(f"{tentativePlacement}: {tentativePlacement.numSolved}/{tentativePlacement.numPackages} solved")

		# it may be better to have the indent handling use "with"
		ti = loggingFacade.temporaryIndent(3)
		success = \
			tentativePlacement.solveTrivialCases() or \
			tentativePlacement.solveDefaultBaseLabel() or \
			tentativePlacement.solveCommonBaseLabel() or \
			tentativePlacement.solveCompatibleBaseLabel() or \
			tentativePlacement.solvePurposeRelativeToSibling(self.classificationScheme)

		if tentativePlacement.isFinal:
			infomsg(f"{tentativePlacement}: completely solved")
			return True

		infomsg(f"{tentativePlacement}: remains to be solved")
		return False

	def constrainPackagesWithAutomaticLabels(self, order):
		flavorConstrained = {}
		purposeConstrained = {}

		flavorConstrained[None] = set()
		purposeConstrained[None] = set()
		for label in self.classificationScheme.allLabels:
			if label.type == Classification.TYPE_AUTOFLAVOR:
				flavorConstrained[label.name] = set()
			elif label.type == Classification.TYPE_PURPOSE:
				purposeConstrained[label.name] = set()

		for label in self.classificationScheme.allLabels:
			if label.type == Classification.TYPE_BINARY:
				cons = flavorConstrained.get(label.flavorName)
				if cons is not None:
					cons.add(label)

				purposeConstrained[label.purposeName].add(label)

		packagesToBeConstrained = []
		for node in self._packages.values():
			constrained = None
			for pkg in node.packages:
				label = pkg.label
				if label is None:
					continue

				if label.type == Classification.TYPE_BINARY:
					cons = flavorConstrained.get(label.flavorName)
					if cons is not None:
						constrained = intersectSets(constrained, cons)

					const = purposeConstrained[label.purposeName]
					constrained = intersectSets(constrained, cons)

			if constrained is None:
				# no further constraints
				pass

			if not constrained:
				errormsg(f"{node} not constraining the candidates")
				continue

			node.constrainCandidatesFurther(constrained)

	def reportUnsolved(self, placements):
		header = "Packages that have not been placed"
		for buildPlacement in placements:
			if buildPlacement.isSolved:
				continue

			if header is not None:
				infomsg(header)
				header = None

			buildPlacement.reportRemaining()

	def solve(self):
		infomsg("### PLACEMENT STAGE 1 ###")

		# populate the _recentlyPlaced list with the nodes that have been assigned
		# a label by the admin.
		self.recordInitialPlacements()

#		self.definePreference("@Gnome", ["@DesktopLibraries", ])
#		self.definePreference("@MinimalCRuntime", ["@GccRuntime", "@Glibc"])

		placements = []
		for siblingInfo in self.solvingTree.allBuilds:
			debugmsg(f"Create build placement for {siblingInfo}")
			placement = self.createBuildPlacement(siblingInfo)
			placements.append(placement)

		if False:
			for node in self.solvingTree.topDownTraversal():
				buildPlacement = node.placement
				if buildPlacement is None or buildPlacement.label is not None:
					continue

				buildPlacement.tryToPlaceTopDown(node)

		for placement in placements:
			self.solveBuildPlacement(placement)

		self.reportUnsolved(placements)

		verdict = self.Verdict()
		for node in self.solvingTree.bottomUpTraversal():
			if node.placement:
				node.placement.reportVerdict(node, verdict)

		return verdict

		##################################################################
		# Everything below here preserved for now, but likely will go away
		##################################################################
		infomsg("### PLACEMENT STAGE 1 ###")

		self.placeSiblingsAccordingToPurpose(order)

		suggestedNewLabels = []
		for interval in order.bottomUpTraversal():
			if interval.solution:
				missing = []
				for lower in interval.lowerNeighbors:
					cone = lower.lowerCone
					if cone is not None and interval.solution not in cone:
						missing.append(lower)

				if missing:
					infomsg(f"{interval} has been placed in {interval.solution} by the user, but not all of its dependencies are covered:")
					for lower in missing:
						if lower.solution:
							infomsg(f" - {lower} [{lower.solution}]")
						else:
							infomsg(f" - {lower} (to be classified)")

			# Report those packages where the lowerCone collapses to an empty set
			# (All the packages above will naturally have an empty lower cone as well)
			# For the time, do not display anything for -devel packages etc
			if interval.lowerBoundConflict and \
			   not (interval.package and interval.package.label and interval.package.label.isPurpose):
				if not any(neigh.lowerBoundConflict for neigh in interval.lowerNeighbors):
					suggestion = self.reportEmptyLowerCone(interval)
					if suggestion is not None:
						suggestedNewLabels.append((interval, suggestion))

		if suggestedNewLabels:
			self.reportSuggestedNewLabels(suggestedNewLabels)

		self.reportUnplaceablePackages(order)

		infomsg("### PLACEMENT STAGE 2 ###")

		for build, siblingInfo in self._builds.items():
			candidateProjects = None

			placement = {}
			for pkg in siblingInfo.packages:
				interval = self.getPackageNode(pkg)
				thisPkgProjectSet = interval.candidateProjects

				# If one of the packages has an empty set of candidate labels, ignore that here
				if thisPkgProjectSet:
					placement[pkg] = thisPkgProjectSet
					candidateProjects = intersectSets(candidateProjects, thisPkgProjectSet)

			if candidateProjects is None:
				infomsg(f"Build {build} - no restrictions")
			elif not candidateProjects:
				infomsg(f"Build {build} - conflicting placement of sibling packages")
				for pkg, projects in placement.items():
					names = map(str, projects)
					infomsg(f"    {pkg} - {' '.join(names)}")
			else:
				projectNames = ' '.join(map(str, candidateProjects))
				infomsg(f"Build {build} - can be placed in {projectNames}")

		for interval in order.bottomUpTraversal():
			# don't do anything if already solved
			if interval.solution:
				continue

			pkg = interval.package
			if interval.package is None:
				# will need to implement support for collapsed cycles here
				continue

			autoFlavor = None
			if pkg.label:
				if pkg.label.type == Classification.TYPE_AUTOFLAVOR:
					# this package has been labeled with a generic autoflavor or purpose like "python"
					autoFlavor = pkg.label

				# For the time being, ignore all supporting packages like devel, doc, lang
				# We deal with them once we've placed the primary packages
				if pkg.label.isPurpose:
					continue

			baseLabel = self.commonSiblingBaseLabel(interval)
			if baseLabel is None:
				continue

			infomsg(f"{interval} siblings have common base label {baseLabel}")
			choice = None
			if autoFlavor:
				# this package has been labeled with a generic autoflavor like "python"
				flavor = baseLabel.getBuildFlavor(autoFlavor.name)
				if flavor is None:
					infomsg(f"{interval} cannot be placed; common base label {baseLabel} has no flavor {autoFlavor}")
				elif interval.labelIsValidCandidate(flavor):
					infomsg(f"{flavor} is a valid candidate for {interval}")
					choice = flavor
				else:
					infomsg(f"{interval} cannot be placed into {flavor} because it's not a candidate label")
					infomsg(f"   Conflicting lower and upper neighbors")
					for below in interval._lowerNeighbors:
						if below._lowerCone is not None and flavor not in below._lowerCone:
							infomsg(f"    - {below} (requires)")
					for above in interval._upperNeighbors:
						if above._upperCone is not None and flavor not in above._upperCone:
							infomsg(f"    - {above} (required by)")
			elif interval.labelIsValidCandidate(baseLabel):
				choice = baseLabel
			else:
				goodFlavors = Classification.createLabelSet()
				for flavor in baseLabel.flavors:
					if interval.labelIsValidCandidate(flavor):
						goodFlavors.add(flavor)

				bestFlavors = intersectSets(goodFlavors, interval.candidates)
				if bestFlavors:
					names = ' '.join(map(str, bestFlavors))
					infomsg(f"   found best flavor(s) {names}")
				elif goodFlavors:
					names = ' '.join(map(str, goodFlavors))
					infomsg(f"   found good flavor(s) {names}")
					bestFlavors = goodFlavors
				else:
					infomsg(f"   no good flavors of {baseLabel} found")

				if len(bestFlavors) > 1:
					bestFlavors = self._order.minima(bestFlavors)

				if len(bestFlavors) == 1:
					choice = bestFlavors.pop()

			if choice:
				infomsg(f"{interval} - try to place into {choice}")
				self.chooseLabelForInterval(interval, choice, f"because its siblings are in {baseLabel}")

		self.placeSiblingsAccordingToPurpose(order)

		for interval in order.topDownTraversal():
			# don't do anything if already solved
			if interval.solution:
				continue

			pkg = interval.package
			if interval.package is None:
				# will need to implement support for collapsed cycles here
				continue

			if pkg.label:
				if pkg.label.type == Classification.TYPE_AUTOFLAVOR:
					# this package has been labeled with a generic autoflavor or purpose like "python"
					infomsg(f"{interval} has been marked as auto-flavor {pkg.label}")
					self.tryToPlaceWithSibling(interval)
					continue

				# For the time being, ignore all supporting packages like devel, doc, lang
				# We deal with them once we've placed the primary packages
				if pkg.label.type == Classification.TYPE_PURPOSE:
					continue

			candidates = interval.candidates
			if candidates is None:
				continue

			infomsg(f"-- inspecting {interval} solution {interval.solution}; {len(candidates)} candidates")
			if len(candidates) == 1:
				uniqueLabel = next(iter(candidates))
				self.chooseLabelForInterval(interval, uniqueLabel, f"because it's the unique candidate")
			elif len(candidates) == 0:
				def showNeighbors(tag, neighbors, getSpan = None):
					if not neighbors:
						return
					infomsg(f"    {tag}")

					found = Classification.createLabelSet()
					for neigh in neighbors:
						if neigh.solution:
							infomsg(f"      {neigh} [{neigh.solution}]")
							found.add(neigh.solution)
						elif neigh.candidates is not None:
							n = len(neigh.candidates)
							infomsg(f"      {neigh} [{n} candidates]")
						else:
							infomsg(f"      {neigh} [unsolveable]")
					if found:
						span = getSpan(found)
						names = ' '.join(map(str, span))
						infomsg(f"     -> bounded by {names}")

				infomsg(f"{interval} cannot be placed due to conflicts")
				if interval.package and interval.package.label:
					infomsg(f"   {interval.package} has been labelled {interval.package.label}")
				self.displayNodes("lower neighbors", interval._lowerNeighbors, self._order.maxima)
				self.displayNodes("upper neighbors", interval._upperNeighbors, self._order.minima)
			elif self.tryToPlaceIntoCommonBase(interval):
				pass
			else:
				self.tryToPlaceWithSibling(interval)

		self.placeSiblingsAccordingToPurpose(order)

		verdict = self.Verdict()
		for node in order.bottomUpTraversal():
			if node.solution:
				verdict.add(node)

		return verdict

	def baseLabelsForSet(self, labels):
		if labels is None:
			return None
		return Classification.createLabelSet(map(lambda label: label.parent or label, labels))

	def reportEmptyLowerCone(self, interval):
		infomsg(f"{interval} has an actual conflict between its requirements")
		for lower in interval.lowerNeighbors:
			if lower.solution:
				infomsg(f"    {lower} labelled {lower.solution}")
			elif lower._lowerCone is not None:
				# the lower cone is an intersection of N upward closures,
				# recover the original labels
				bounds = self._order.minima(lower.lowerCone)
				if len(bounds) < 10:
					names = ' '.join(map(str, bounds))
					infomsg(f"    {lower} bounded by {names}")
				else:
					infomsg(f"    {lower} bounded by {len(bounds)} labels")
			else:
				infomsg(f"    {lower} unbounded")

		cones = []
		for lower in interval.lowerNeighbors:
			cone = lower.lowerCone
			if cone is not None:
				cones.append(cone)

		# sort by increasing size of cone
		cones.sort(key = lambda c: len(c))

		chunks = []
		for cone in cones:
			if not any(chunk.issubset(cone) for chunk in chunks):
				chunks.append(cone)

		infomsg(f"   Found {len(chunks)} distinct closures")
		relatedSubsets = []
		for chunk in chunks:
			subset = set()
			for lower in interval.lowerNeighbors:
				if lower._lowerCone is None:
					continue

				if lower.solution in chunk or \
				   chunk.issubset(lower._lowerCone):
					subset.add(lower)

			relatedSubsets.append(subset)

		common = reduce(set.intersection, relatedSubsets)
		warts = []
		for subset in relatedSubsets:
			wart = subset.difference(common)
			if wart:
				warts.append(wart)

		if warts is None:
			return None

		infomsg(f"   It seems we have {len(warts)} groups of packages that we need to reconcile")
		upwardClosure = set()
		for wart in warts:
			names = map(str, wart)
			infomsg(f"    - {' '.join(names)}")

			for lower in wart:
				upwardClosure.update(lower._lowerCone)

		labels = self._order.minima(upwardClosure)
		names = map(str, labels)
		infomsg(f"   Might be solved by a label that requires {' '.join(names)}")
		return labels

	def reportSuggestedNewLabels(self, suggestedNewLabels):
		class NewLabel:
			def __init__(self, labels, key = None):
				self.below = labels
				self.nodes = []

				if key is None:
					key = self.makeKey(labels)
				self.key = key

			@classmethod
			def makeKey(klass, labels):
				names = sorted(map(str, labels))
				return '/'.join(names)

			def add(self, node):
				self.nodes.append(node)

			def count(self):
				return len(self.nodes)

			def __str__(self):
				return ", ".join(map(str, self.below))

		uniq = dict()
		for node, labels in suggestedNewLabels:
			key = NewLabel.makeKey(labels)
			newLabel = uniq.get(key)
			if newLabel is None:
				newLabel = NewLabel(labels, key)
				uniq[key] = newLabel
			newLabel.add(node)

		infomsg(f"Suggesting the following new labels")
		for newLabel in sorted(uniq.values(), key = NewLabel.count, reverse = True):
			infomsg(f" - upper bound for {newLabel}")
			for node in newLabel.nodes:
				infomsg(f"    * {node}")
		infomsg("")

	def reportAmbiguousLabels(self, interval, candidates):
		if len(candidates) < 6:
			return ", ".join(map(str, candidates))

		lowerBounds = None
		upperBounds = None

		if interval.upperCone is not None:
			upperBounds = self._order.maxima(candidates)
		if interval.lowerCone is not None:
			lowerBounds = self._order.minima(candidates)

		if upperBounds is None and lowerBounds is None:
			return "<all labels>"

		if upperBounds == lowerBounds:
			names = list(map(str, lowerBounds))
			return f"candidates {names}"

		msgs = []
		if lowerBounds:
			names = list(map(str, lowerBounds))
			msgs.append(f"lower bounds {names}")
		if upperBounds:
			names = list(map(str, upperBounds))
			msgs.append(f"upper bounds {names}")
		return '; '.join(msgs)

	# A node is usually associated with a single package, however in the case
	# of a dependency cycle, several packages are collapsed into a single node.
	def chooseLabelForInterval(self, interval, label, reasonMsg):
		if not interval.anyPackageHasLabel():
			# none of the package(s) has been labelled
			choice = label
		else:
			commonLabel = interval.commonLabel
			if commonLabel is None:
				infomsg(f"cannot pick label for {interval} - packages have already been labelled with different labels")
				return

			if commonLabel.type == Classification.TYPE_AUTOFLAVOR:
				choice = label.getBuildFlavor(commonLabel.name)
				if choice is None:
					infomsg(f"Cannot label {interval} with {label} - it should be labeled with build flavor $something+{commonLabel}")
					return False
			elif commonLabel.type == Classification.TYPE_PURPOSE:
				# cheat a little here. The chosen label we've been given may already be
				# a purpose (eg @Core-doc), and our package has been labelled with
				# a (possibly different) purpose like "-devel".
				# Move up to the base flavor; because there's no flavor @Core-doc-devel
				if label.isPurpose:
					label = label.parent
				choice = label.getObjectPurpose(commonLabel.name)
				if choice is None:
					infomsg(f"Cannot label {interval} with {label} - it should be labeled with purpose $something-{commonLabel}")
					return False

		if interval.candidates is not None and choice not in interval.candidates:
			infomsg(f"BUMMER: made a crap choice: {label} is not a candidate label for {interval}")
			return False

		infomsg(f"{interval} is being placed into {choice} because {reasonMsg}")
		self.recordDecision(interval, choice)
		return True

	def commonSiblingBaseLabel(self, interval):
		if interval.siblings is None:
			return None

		result = None
		for sib in interval.siblings:
			sibInterval = self.getPackage(sib)
			if sibInterval is None:
				continue

			baseLabel = sibInterval.solutionBaseLabel
			if baseLabel:
				if result is None:
					result = baseLabel
				elif result is not baseLabel:
					return None
		return result

	def tryToPlaceWithSibling(self, interval):
		if interval.siblings is None:
			return

		# If this OBS package produces just one binary rpm, there are no siblings to
		# place this with
		if len(interval.siblings) == 1:
			infomsg(f"{interval} looks like an only child")
			return

		if interval.siblings.labels:
			return self.tryToPlaceWithLabelledSiblings(interval)

		return self.tryToPlaceWithUnlabelledSiblings(interval)

	def tryToPlaceWithLabelledSiblings(self, interval):
		pkg = interval.package

		if False and interval.candidates is not None:
			commonLabel = interval.siblings.commonLabel
			if commonLabel is not None and commonLabel in interval.candidates:
				infomsg(f"{interval} is being placed into {commonLabel} because its siblings were placed there")
				self.recordDecision(interval, commonLabel)
				return True

		baseLabel = interval.siblings.commonBaseLabel
		if baseLabel is None:
			names = map(str, interval.siblings.baseLabels)
			infomsg(f"{interval} cannot be placed with siblings (ambiguous base labels {' '.join(names)})")
			return

		tryLabels = set()
		if pkg.label is None:
			# no purpose or autoflavor
			tryLabels.add(baseLabel)
			tryLabels.update(baseLabel.flavors)
		elif pkg.label.type == Classification.TYPE_AUTOFLAVOR:
			flavor = baseLabel.getBuildFlavor(pkg.label.name)
			if flavor:
				tryLabels.add(flavor)
		elif pkg.label.type == Classification.TYPE_PURPOSE:
			purposeName = pkg.label.name

			purpose = baseLabel.getObjectPurpose(purposeName)
			if purpose:
				tryLabels.add(purpose)
			for flavor in baseLabel.flavors:
				purpose = flavor.getObjectPurpose(purposeName)
				if purpose:
					tryLabels.add(purpose)
		else:
			raise Exception()

		if not tryLabels:
			infomsg(f"Cannot place {interval} - unable to determine some good labels to try")
			return

		lowerMatches = []
		fullMatches = []
		lowerCone = interval.lowerCone
		for label in tryLabels:
			if lowerCone is None or label in lowerCone:
				lowerMatches.append(label)
			if interval.candidates is not None and label in interval.candidates:
				fullMatches.append(label)

		infomsg(f"{pkg} found {len(fullMatches)} full matches and {len(lowerMatches)} decent matches")
		if len(fullMatches) == 1:
			label = next(iter(fullMatches))

			return self.chooseLabelForInterval(interval, label, f"its siblings were placed into {baseLabel}")

		if len(lowerMatches) == 1:
			label = next(iter(lowerMatches))

			return self.chooseLabelForInterval(interval, label, f"its siblings were placed into {baseLabel} (not a perfect match)")

		return False

	def tryToPlaceWithUnlabelledSiblings(self, interval):
		pkg = interval.package

		traceme = False
		if interval.name == 'libjitterentropy3.x86_64':
			traceme = True

		listOfCandidateSets = []
		for sib in interval.siblings:
			sibInterval = self.getPackage(sib)
			if sibInterval is None:
				infomsg(f"Error: trying to place {pkg} but cannot find its sibling {sib}")
				continue

#			if sibInterval.hasPurposeLabel():
#				continue

			# the sibling doesn't have any constraints
			if sibInterval.candidates is None:
				continue

			baseLabels = self.baseLabelsForSet(sibInterval.candidates)
			listOfCandidateSets.append(baseLabels)

		if not listOfCandidateSets:
			infomsg(f"{interval} is not constrained by sibling placement; we could just use candidates")

		# For all the unlabelled siblings, intersect the candidate sets
		commonBaseLabels = reduce(intersectSets, listOfCandidateSets, None)
		if traceme:
			infomsg(f"    COMMON {len(commonBaseLabels)}")

		if commonBaseLabels == None:
			infomsg(f"{interval} is not constrained by sibling placement; we could just use candidates")

		if len(commonBaseLabels) > 1:
			commonBaseLabels = interval.filterCandidateLabels(commonBaseLabels)
			# commonBaseLabels = self._order.minima(commonBaseLabels)
			if traceme:
				infomsg(f"    FILTERED {len(commonBaseLabels)}")

		if len(commonBaseLabels) == 1:
			baseLabel = next(iter(commonBaseLabels))

			if self.chooseLabelForInterval(interval, baseLabel, f"{baseLabel} is the best candidate base label of its siblings"):
				return True

			infomsg(f"{interval} cannot be placed with siblings - could not choose a matching label from base label {baseLabel}")
		elif not commonBaseLabels:
			infomsg(f"{interval} cannot be placed with siblings - no common candidate base label")

			nodeList = []
			for sib in interval.siblings:
				nodeList.append(self.getPackage(sib))

			self.displayNodes("siblings", nodeList, self._order.minima)
		else:
			detail = ""
			if len(commonBaseLabels) <= 5:
				detail = ":" + ', '.join(map(str, commonBaseLabels))
			infomsg(f"{interval} cannot be placed with siblings - {len(commonBaseLabels)} common candidate base labels{detail}")

		return False

	def tryToPlaceWithSiblingOld(self, interval):
		pkg = interval.package

		# loop over the package and all of its siblings
		# All packages resulting from the same OBS build are considered siblings
		labelledSiblings = []
		unlabelledSiblings = []

		for sib in interval.siblings:
			sibInterval = self.getPackage(sib)
			if sibInterval is None:
				infomsg(f"Error: trying to place {pkg} but cannot find its sibling {sib}")
				continue

			if sibInterval.solution:
				labelledSiblings.append((sibInterval, sibInterval.solutionBaseLabel))
			elif sibInterval.candidates is not None:
				baseLabels = self.baseLabelsForSet(sibInterval.candidates)
				unlabelledSiblings.append((sibInterval, baseLabels))
			else:
				# candidates being None indicates no restrictions on the sibling
				pass

		# For all the labelled siblings, collect their base label (ie @Foo+something -> @Foo)
		sibLabels = set(label for interval, label in labelledSiblings)

		# For all the unlabelled siblings, intersect the candidate sets
		sibLabelScope = reduce(intersectSets, (baseLabels for interval, baseLabels in unlabelledSiblings), None)

		# First priority: try to select a (base) label from siblings that have already been labelled.
		# Second priority: if no siblings have been labelled, 
		if not sibLabels:
			if not sibLabelScope:
				infomsg(f"{interval} cannot be placed with siblings (no common base labels found)")
				return
			sibLabels = sibLabelScope
		elif sibLabelScope is not None:
			if not sibLabels.issubset(sibLabelScope):
				infomsg(f"{interval}: some siblings have been labelled already, but their labels conflict with other siblings")

				for interval, label in labelledSiblings:
					for unlabelledInterval, candidates in unlabelledSiblings:
						if label not in candidates:
							names = ' '.join(map(str, candidates))
							infomsg(f"   {interval} has been labelled {interval.solution}, but is not in scope for sibling {unlabelledInterval}; candidates = {names}")

				return

		if len(sibLabels) == 1:
			label = next(iter(sibLabels))
			return self.chooseLabelForInterval(interval, label, f"its siblings were placed into {label}")
		else:
			msg = self.reportAmbiguousLabels(interval, sibLabels)
			infomsg(f"{interval}: ambiguous choice of sibling labels: {msg}")

			for interval, label in labelledSiblings:
				infomsg(f" - {interval} labelled {label}")
			for interval, baseLabels in unlabelledSiblings:
				min = self._order.minima(baseLabels)
				infomsg(f" - {interval} bounded by {' '.join(map(str, min))}")

	# Given a set of candidates like this:
	#  @CoreLibraries+hpc, @HPC+accounts, @HPC, @HPC+devel, @HPC+python, @HPC+x86-64-v3, @HPC+doc, @HPC+i18n
	# pick @HPC
	#
	# Do this by looking at the base flavors of all labels and finding the single one that
	# is "below" all these candidate labels. In the example above, this would be @HPC because
	#  - @HPC is always a requirement for @HPC+something
	#  - @CoreLibraries+hpc is a build flavor that augments or at least requires @HPC
	def tryToPlaceIntoCommonBase(self, interval):
		def isCommonBaseFlavor(base, candidates):
			for label in candidates:
				if base == label or label.parent == base:
					continue

				if base in label.runtimeRequires:
					continue

				return False
			return True

		baseFlavors = set()
		for label in interval.candidates:
			while label.parent is not None:
				label = label.parent
			baseFlavors.add(label)

		if len(baseFlavors) == 0:
			return

		if len(baseFlavors) == 1:
			best = baseFlavors.pop()
		else:
			best = None
			for base in baseFlavors:
				if isCommonBaseFlavor(base, interval.candidates):
					if best:
						infomsg(f"{interval} has at least two \"common\" base flavors - {best} and {base}")
						return
					best = base

		if best is None:
			return False

		return self.chooseLabelForInterval(interval, best, f"{best} is the common base flavor of all candidates")

	# There may be packages (or more often, clusters of packages) that belong together
	# but cannot be labeled because we haven't labeled any of them yet. Report these
	# together
	def reportUnplaceablePackages(self, order):
		found = set()
		for interval in order.topDownTraversal():
			# don't do anything if already solved
			if interval.solution:
				continue

			if interval.candidates is not None:
				continue

			if interval.package:
				assert(interval is self.getPackage(interval.package))

			# If any of the packages associated with this interval has a label
			# (even if it's an auto-label) there's still a chance we might find a
			# home for it.
			if interval.anyPackageHasLabel():
				continue

			found.add(interval)

		remaining = found.copy()
		while remaining:
			cluster = set()
			pivot = remaining.pop()

			queue = [pivot]
			while queue:
				interval = queue.pop(0)
				if interval in cluster:
					continue
				if interval not in found:
					continue

				cluster.add(interval)

				# inspect all upper and lower neighbors of this package
				# Not all of these will automatically have NULL candidates, too.
				queue += list(interval._lowerNeighbors)
				queue += list(interval._upperNeighbors)

			if len(cluster) > 1:
				infomsg("Found a cluster of packages that should probably be labelled together:")
				for i in sorted(cluster, key = str):
					infomsg(f"  {i}")

				pkgs = set()
				for i in sorted(cluster, key = str):
					if pkgs.intersection(i.packages):
						infomsg(f"{i} has duplicate package(s)")
						fail
					pkgs.update(i.packages)
			else:
				# infomsg(f"{pivot} can be placed anywhere")
				pass

			remaining.difference_update(cluster)

	def renderCandidates(self, order, interval):
		if not interval.lowerCone:
			if not interval.upperCone:
				return "[ALL LABELS]"


	@staticmethod
	def displayNodes(tag, nodeList, getSpan = None):
		if not nodeList:
			return
		infomsg(f"    {tag}")

		found = set()
		for node in nodeList:
			if node.solution:
				infomsg(f"      {node} [{node.solution}]")
				found.add(node.solution)
			elif node.candidates is not None:
				bounds = None
				if getSpan is not None:
					bounds = getSpan(node.candidates)
					if len(bounds) > 6:
						bounds = None

				n = len(node.candidates)
				if n < 5:
					names = map(str, node.candidates)
					infomsg(f"      {node} [{n} candidates {' '.join(names)}]")
				elif bounds:
					names = map(str, bounds)
					infomsg(f"      {node} [{n} candidates bounded by {' '.join(names)}]")
				else:
					infomsg(f"      {node} [{n} candidates]")
			else:
				infomsg(f"      {node} [unsolveable]")

		if found and getSpan:
			span = getSpan(found)
			names = ' '.join(map(str, span))
			infomsg(f"     -> bounded by {names}")

	def __iter__(self):
		for pkg, interval in self._packages.items():
			yield pkg, self.getBestCandidate(interval)

	def getBestCandidate(self, interval):
		candidates = interval.candidates
		if not candidates:
			return None

		return self._order.minimumOf(candidates)

class PackagePreferences:
	def __init__(self):
		self.neverPreferPatterns = []
		self._comparison = {}

	def prefer(self, preferredName, otherName):
		if preferredName is None:
			self.neverPreferPatterns.append(otherName)
		else:
			self._comparison[preferredName, otherName] = 1
			self._comparison[otherName, preferredName] = -1

	def neverPrefer(self, pattern):
		self.neverPreferPatterns.append(pattern)

	def isNeverPreferred(self, name):
		for pattern in self.neverPreferPatterns:
			if fnmatch.fnmatchcase(name, pattern):
				return True
		return False

	def compare(self, name1, name2):
		try:
			return self._comparison[name1, name2]
		except: pass

		bad1 = self.isNeverPreferred(name1)
		bad2 = self.isNeverPreferred(name2)
		if bad1 == bad2:
			r = 0
		elif bad1:
			r = -1
		else:
			r = 1

		self._comparison[name1, name2] = r
		self._comparison[name2, name1] = -r
		return r


class PackageGroup:
	def __init__(self, name):
		self.name = name

		# nuke
		self.matchCount = 0
		# nuke
		self.expand = True
		self.label = None
		self._packages = []
		self._buildFlavors = {}
		self._objectPurposes = {}
		self._closure = set()

	def track(self, pkg):
		self._packages.append(pkg)
		self.matchCount += 1

		if pkg.label is None or pkg.label.type in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
			pkg.label = self.label
		elif pkg.label is self.label:
			pass
		else:
			errormsg(f"Refusing to change {pkg.fullname()} change label from {pkg.label} to {self.label}")

		self._closure.add(pkg)

	@property
	def type(self):
		return self.label.type

	@property
	def packages(self):
		return set(self._packages)

	@property
	def closure(self):
		return set(self._closure)

	@property
	def packageNames(self):
		return set(_.name for _ in self._packages)

	@property
	def groupNames(self):
		return set(_.group for _ in self._packages)

	@property
	def runtimeRequires(self):
		raise Exception("obsolete method called")

	@property
	def buildRequires(self):
		raise Exception("obsolete method called")

	@property
	def isFlavor(self):
		return self.label.parent is not None

	@property
	def defined(self):
		return self.label and self.label.defined

	@defined.setter
	def defined(self, value):
		self.label.defined = value

	def addRequires(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addRuntimeDependency(otherGroup.label)

	def addAugmentation(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addRuntimeAugmentation(otherGroup.label)

	def addBuildRequires(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addBuildDependency(otherGroup.label)

	@property
	def flavors(self):
		return map(lambda pair: pair[1], sorted(self._buildFlavors.items()))

	def addBuildFlavor(self, otherGroup):
		flavorName = otherGroup.label.flavorName
		assert(flavorName)

		if self._buildFlavors.get(flavorName):
			raise Exception(f"Duplicate definition of build flavor {flavorName} for {self.name}")
		self._buildFlavors[flavorName] = otherGroup

	def getBuildFlavor(self, name):
		return self._buildFlavors.get(name)

	@property
	def purposes(self):
		return map(lambda pair: pair[1], sorted(self._objectPurposes.items()))

	def addObjectPurpose(self, otherGroup):
		purposeName = otherGroup.label.purposeName
		assert(purposeName)

		if self._objectPurposes.get(purposeName):
			raise Exception(f"Duplicate definition of build purpose {purposeName} for {self.name}")
		self._objectPurposes[purposeName] = otherGroup

	def getObjectPurpose(self, name):
		return self._objectPurposes.get(name)

	# Having classified a set of packages, we add it to the group's closure
	def update(self, packages):
		self._closure.update(packages)

class GlobMatch:
	PRIORITY_DEFAULT = 5

	def __init__(self, value, group, priority = PRIORITY_DEFAULT):
		self.value = value
		self.group = group
		self.priority = priority

	def __str__(self):
		if self.priority == self.PRIORITY_DEFAULT:
			return self.value
		return f"{self.value} (priority {self.priority})"

	@property
	def key(self):
		return (self.priority, -len(self.value), self.value)

	def match(self, name):
		return fnmatch.fnmatchcase(name, self.value)


class FilterSetBuilder(object):
	def __init__(self, filterSet, group, priority = None):
		self.filterSet = filterSet
		self.group = group
		self.priority = priority

	def addProductFilter(self, name):
		self.addMatch(self.filterSet.productFilters, name)

	def addBinaryPackageFilter(self, name):
		self.addMatch(self.filterSet.binaryPkgFilters, name)

	def addSourcePackageFilter(self, name):
		self.addMatch(self.filterSet.sourcePkgFilters, name)

	def addRpmGroupFilter(self, name):
		self.addMatch(self.filterSet.rpmGroupFilters, name)

	def addMatch(self, filterSet, value):
		group = self.group
		priority = self.priority

		# A match may come with additional parameters, as in 
		#
		#	postgresql-* priority=8
		#
		if ' ' in value:
			words = value.split()
			value = words[0]
			for param in words[1:]:
				(argName, argValue) = param.split('=')
				if argName == 'priority':
					priority = int(argValue)
				elif argName == 'purpose':
					subGroup = group.getObjectPurpose(argValue)
					if argValue is None:
						raise Exception(f"Cannot add filter for \"{value}\" - unknown purpose {argValue} in group {group.label}")
					group = subGroup
				else:
					raise Exception(f"Unknown match parameter {param} in {self.filterSet} expression \"{value}\" for group {group.name}");

		if priority is None:
			priority = GlobMatch.PRIORITY_DEFAULT

		filterSet.addMatch(value, group, priority)

class FilterType(object):
	def __init__(self, type):
		self.type = type
		self._exactMatches = {}
		self._globMatches = []

	def __str__(self):
		return f"{self.type} filter"

	def addMatch(self, value, group, priority):
		if '*' in value or '?' in value:
			self._globMatches.append(GlobMatch(value, group, priority))
		else:
			if self._exactMatches.get(value):
				conflict = self._exactMatches[value]
				raise Exception(f"{self.type} filter is ambiguous for {value} ({group.name} vs {conflict.name})")

			# NB: silently ignore any priority value for exact match
			self._exactMatches[value] = group

	def finalize(self):
		self._globMatches.sort(key = lambda m: m.key)

	def tryFastMatch(self, name):
		group = self._exactMatches.get(name)
		if group is not None:
			return PackageFilter.Verdict(group, f"{self.type} filter {name}")

		return None

	def trySlowNameMatch(self, name):
		for glob in self._globMatches:
			if glob.match(name):
				return PackageFilter.Verdict(glob.group, f"{self.type} filter {glob}")

		return None

	def applyName(self, name):
		verdict = self.tryFastMatch(name)
		if verdict is None:
			verdict = self.trySlowNameMatch(name)
		return verdict

class RpmGroupFilters(FilterType):
	def __init__(self):
		super().__init__('rpmgroup')

	def apply(self, pkg, product):
		return self.applyName(pkg.group)

class ProductFilters(FilterType):
	def __init__(self):
		super().__init__('product')

	def apply(self, pkg, product):
		if product is None:
			return None
		return self.applyName(product.name)

class PackageFilters(FilterType):
	def __init__(self):
		super().__init__('package')

	def apply(self, pkg, product):
		return self.applyName(pkg.name)

class SourcePackageFilters(FilterType):
	def __init__(self):
		super().__init__('source package')

	def apply(self, pkg, product):
		src = pkg.sourcePackage
		if src is None:
			return None
		return self.applyName(src.name)

class PackageFilterSet:
	def __init__(self):
		self._productFilters = None
		self._binaryPkgFilters = None
		self._sourcePkgFilters = None
		self._rpmGroupFilters = None
		self._applicableFilters = None

	@property
	def productFilters(self):
		if not self._productFilters:
			self._productFilters = ProductFilters()
		return self._productFilters

	@property
	def binaryPkgFilters(self):
		if not self._binaryPkgFilters:
			self._binaryPkgFilters = PackageFilters()
		return self._binaryPkgFilters

	@property
	def sourcePkgFilters(self):
		if not self._sourcePkgFilters:
			self._sourcePkgFilters = SourcePackageFilters()
		return self._sourcePkgFilters

	@property
	def rpmGroupFilters(self):
		if not self._rpmGroupFilters:
			self._rpmGroupFilters = RpmGroupFilters()
		return self._rpmGroupFilters

	def finalize(self):
		self._applicableFilters = []
		for filterSet in (self._productFilters, self._binaryPkgFilters, self._sourcePkgFilters, self._rpmGroupFilters):
			if filterSet is not None:
				filterSet.finalize()
				self._applicableFilters.append(filterSet)

	def applyVerbosely(self, pkg, product):
		matches = []
		for filterSet in self._applicableFilters:
			verdict = filterSet.apply(pkg, product)
			if verdict is not None:
				matches.append(verdict)

		if not matches:
			infomsg(f"{pkg}: no match by package filter")
			return None

		verdict = matches.pop(0)
		infomsg(f"{pkg}: {verdict.label} {verdict.reason}")

		if matches:
			infomsg(f"   {len(matches)} lower priority matches were ignored:")
			for other in matches:
				infomsg(f"      {other.label} {other.reason}")

		return verdict

	def apply(self, pkg, product):
		if pkg.trace:
			return self.applyVerbosely(pkg, product)
		for filterSet in self._applicableFilters:
			verdict = filterSet.apply(pkg, product)
			if verdict is not None:
				break
		return verdict

class PackageFilter:
	class Verdict:
		def __init__(self, group, reason):
			self.group = group
			self.label = group.label
			self.reason = reason

		def labelPackage(self, pkg):
			pkg.label = self.label
			pkg.labelReason = Classification.ReasonFilter(pkg, self.reason)

			self.group.track(pkg)

	def __init__(self, filename = 'filter.yaml', scheme = None):
		self.classificationScheme = scheme or Classification.Scheme()
		self._groups = {}
		self._preferences = PackagePreferences()
		self._autoflavors = []
		self._purposes = []

		self.filterSet = PackageFilterSet()

		timer = ExecTimer()
		self.load(filename)
		infomsg(f"Loaded filter definition from {filename}: {timer} elapsed")

	def load(self, filename):
		with open(filename) as f:
			data = yaml.full_load(f)

		# Parse autoflavors *before* everything else so that we can populate
		# newly created flavors from default settings.
		for gd in data.get('autoflavors') or []:
			group = self.parseGroup(Classification.TYPE_AUTOFLAVOR, gd)
			self._autoflavors.append(group)

		for gd in data.get('purposes') or []:
			group = self.parseGroup(Classification.TYPE_PURPOSE, gd)
			self._purposes.append(group)

		for gd in data.get('build_configs') or []:
			self.parseGroup(Classification.TYPE_BUILDCONFIG, gd)

		for gd in data.get('buildconfig_flavors') or []:
			self.parseGroup(Classification.TYPE_BUILDCONFIG_FLAVOR, gd)

		for gd in data.get('build_groups') or []:
			self.parseGroup(Classification.TYPE_SOURCE, gd)

		for gd in data['groups']:
			self.parseGroup(Classification.TYPE_BINARY, gd)

		for pref in data.get('preferences') or []:
			preferred = pref.get('prefer')
			over = pref['over']
			if isinstance(over, str):
				self._preferences.prefer(preferred, over)
			else:
				for other in over:
					self._preferences.prefer(preferred, other)

		self.finalize()
		self.classificationScheme.finalize()

	def finalize(self):
		def validateDependencies(dependencies):
			for req in dependencies:
				chase = req
				while True:
					other = self.getGroup(chase.name, chase.type)
					if other is None:
						raise Exception(f"could not find {chase.type} group {chase.name}")
					if not other.label.parent:
						break
					chase = other.label.parent

				if not other.defined and not other.label.isPurpose:
					raise Exception(f"filter configuration issue: group {group.label} requires {other.label}, which is not defined anywhere")

		self.filterSet.finalize()

		for group in self._groups.values():
			label = group.label
			validateDependencies(label.buildRequires)

			# resolve the defaultlabel
			if label.defaultLabel is not None:
				defaultLabel = self.classificationScheme.getLabel(label.defaultLabel)
				if defaultLabel is None or not defaultLabel.defined:
					raise Exception(f"Label {label} specifies defaultlabel={label.defaultLabel}, which is not defined anywhere")
				if defaultLabel.type is not Classification.TYPE_BINARY:
					raise Exception(f"Label {label} specifies defaultlabel={label.defaultLabel}, which is of type {defaultLabel.type}")
				label.defaultLabel = defaultLabel

		# For all base labels, instantiate their auto flavors (ie for @Foo, instantiate
		# @Foo+python, @Foo+ruby, etc)
		for group in list(self._groups.values()):
			if group.label.parent is not None or \
			   group.label.type is not Classification.TYPE_BINARY:
				continue

			for autoFlavor in self.autoFlavors:
				if autoFlavor.label.disposition is Classification.DISPOSITION_SEPARATE:
					self.instantiateAutoFlavor(group, autoFlavor)

		# Loop over all @Foo labels and look for auto flavors with disposition maybe_merge, such as python
		# If @Foo requires everything that the auto flavor requires (in the case of python, this would
		# be @PythonCore), then mark the auto flavor for merging. Otherwise, create a separate
		# build flavor @Foo+python
		preliminaryOrder = self.classificationScheme.createOrdering(Classification.TYPE_BINARY)
		for group in list(self._groups.values()):
			if group.label.parent is not None:
				continue

			if group.label.type is not Classification.TYPE_BINARY:
				continue

			baseLabel = group.label

			# get the closure of all requirements of @Foo
			baseDependencies = preliminaryOrder.downwardClosureFor(group.label)

			for autoFlavor in self.autoFlavors:
				if autoFlavor.label.disposition != Classification.DISPOSITION_MAYBE_MERGE:
					continue

				if autoFlavor.label.runtimeRequires.issubset(baseDependencies):
					flavor = baseLabel.getBuildFlavor(autoFlavor.name)
					if flavor is not None:
						infomsg(f"{baseLabel}+{autoFlavor.label} packages could be merged into {baseLabel}, but {flavor} exists")
					else:
						# infomsg(f"{baseLabel}+{autoFlavor.label} packages will be merged into {baseLabel}")
						baseLabel.addMergeableFlavor(autoFlavor.label)
				else:
					self.instantiateAutoFlavor(group, autoFlavor)

		for group in list(self._groups.values()):
			if group.label.type is Classification.TYPE_BINARY and not group.label.isPurpose:
				assert(not group.label.isPurpose)
				for purposeDef in self._purposes:
					self.instantiatePurpose(group, purposeDef)

		for group in list(self._groups.values()):
			label = group.label
			if label.purposeName == 'devel':
				baseLabel = label.parent
				for req in baseLabel.runtimeRequires:
					if req.isPurpose:
						continue
					purposeReq = req.getObjectPurpose('devel')
					if purposeReq is None:
						raise Exception(f"no purpose devel for {req}")
					# infomsg(f"{label} should require {purposeReq}")
					label.addRuntimeDependency(purposeReq)

				# XXX FIXME
				# We may also want to make the build config Foo/Standard automatically require @Foo-devel
				if False and baseLabel.flavorName is None:
					if baseLabel.buildConfig:
						baseLabel.buildConfig.addBuildDependency(label)


		return

	def apply(self, pkg, product):
		return self.filterSet.apply(pkg, product)

	def performInitialPlacement(self, pkg):
		verdict = self.apply(pkg, pkg.product)
		if verdict is not None:
			verdict.labelPackage(pkg)
			debugInitialPlacement(f"{pkg} is placed in {verdict.label} by package filter rules")

	def makeGroup(self, name, type = None):
		assert(type)
		return self.makeGroupInternal(name, type)

	def resolveGroupReference(self, name, type = None):
		purposeName = None
		flavorName = None

		if '-' in name:
			(name, purposeName) = name.split('-')

		if '+' in name:
			(name, flavorName) = name.split('+')

		group = self.makeGroupInternal(name, type)

		if flavorName is not None:
			group = self.makeFlavorGroup(group, flavorName)

		if purposeName is not None:
			group = self.makePurposeGroup(group, purposeName)

		return group

	def resolveBuildReference(self, name, type = None):
		if '/' not in name:
			sourceProjectName = name
			flavorName = 'standard'
		else:
			(sourceProjectName, flavorName) = name.split('/')

		group = self.makeGroupInternal(sourceProjectName, Classification.TYPE_SOURCE)
		return self.makeFlavorGroup(group, flavorName)

	def makeSourceGroup(self, name):
		return self.makeGroupInternal(name, Classification.TYPE_SOURCE)

	def makeBinaryGroup(self, name):
		return self.makeGroupInternal(name, Classification.TYPE_BINARY)

	def instantiateAutoFlavor(self, baseGroup, autoFlavor):
		group = self.makeFlavorGroup(baseGroup, autoFlavor.name)

		# even if the group existed already, at this point we need to copy any runtime
		# requirements specified for the auto flavor
		group.label.copyRequirementsFrom(autoFlavor.label)

		return group

	def instantiatePurpose(self, group, purposeDef):
		return self.makePurposeGroup(group, purposeDef.name)

	def instantiateBuildConfigFlavor(self, sourceProject, name):
		if sourceProject is None:
			return None

		flavor = sourceProject.getBuildFlavor(name)
		if flavor is None:
			tmpl = self.getGroup(name, Classification.TYPE_BUILDCONFIG_FLAVOR)
			if tmpl is not None:
				sourceGroup = self.makeGroupInternal(sourceProject.name, Classification.TYPE_SOURCE);

				flavor = self.instantiateAutoFlavor(sourceGroup, tmpl)
				flavor.label.copyRequirementsFrom(tmpl.label)

		return flavor

	def makeFlavorGroup(self, baseGroup, flavorName, type = None):
		flavor = baseGroup.getBuildFlavor(flavorName)
		if flavor is not None:
			return flavor

		if type is not None:
			assert(baseGroup.type == Classification.TYPE_SOURCE and type == Classification.TYPE_BUILDCONFIG)

		if baseGroup.type == Classification.TYPE_BINARY:
			flavor = self.createBinaryFlavor(baseGroup, flavorName)
		elif baseGroup.type == Classification.TYPE_SOURCE:
			flavor = self.createBuildConfigFlavor(baseGroup, flavorName)
		else:
			raise Exception(f"Don't know how to create flavor {flavorName} for {baseGroup.type} label {baseGroup.label}")

		return flavor

	def makePurposeGroup(self, baseGroup, purposeName):
		purpose = baseGroup.getObjectPurpose(purposeName)
		if purpose is not None:
			return purpose

		purpose = self.createObjectPurpose(baseGroup, purposeName)

		return purpose

	def createBinaryFlavor(self, baseGroup, flavorName):
		# When creating @Foo+blah, and @Foo has a sourceProject of FooSource, check
		# whether there's a buildconfig for FooSource/blah.
		# If that doesn't exist, the flavor will build using the same config as the base label.
		buildLabel = self.getBuildConfigFlavor(baseGroup.label.sourceProject, flavorName)

		label = self.classificationScheme.createFlavor(baseGroup.label, flavorName, buildConfig = buildLabel)
		flavor = self.getGroupForLabel(label, create = True)
		baseGroup.addBuildFlavor(flavor)

		for flavorDef in self._autoflavors:
			if flavorDef.name == flavorName:
				# If there is a default flavor definition, copy its autoselect
				# setting.
				flavor.label.autoSelect = flavorDef.label.autoSelect

		return flavor

	def createBuildConfigFlavor(self, baseGroup, flavorName):
		label = self.classificationScheme.createFlavor(baseGroup.label, flavorName)
		flavor = self.getGroupForLabel(label, create = True)
		baseGroup.addBuildFlavor(flavor)

		# For the time being, make all buildconfigs auto-selectable.
		# Probably a useless gesture.
		flavor.autoSelect = True

		return flavor

	def createObjectPurpose(self, baseGroup, purposeName):
		purposeDef = self.getObjectPurposeDefinition(purposeName)
		if purposeDef is None:
			raise Exception(f"Undefined purpose {purposeName} in definition of {baseGroup.label}: you must define {purposeName} globally first")

		label = baseGroup.label.getObjectPurpose(purposeName)
		if label is None:
			label = self.classificationScheme.createPurpose(baseGroup.label, purposeName, template = purposeDef.label)

		# copy requirements from purposeDef
		label.copyRequirementsFrom(purposeDef.label)

		purpose = self.getGroupForLabel(label, create = True)
		baseGroup.addObjectPurpose(purpose)
		return purpose

	def getBuildConfigFlavor(self, sourceProject, flavorName):
		if sourceProject is None:
			return None
		assert(isinstance(sourceProject, Classification.Label))

		buildLabel = sourceProject.getBuildFlavor(flavorName)
		return buildLabel

# do NOT create the build config on the fly; that doesn't make sense.
#		if buildLabel is None:
#			buildGroup = self.instantiateBuildConfigFlavor(sourceProject, flavorName)
#			if buildGroup is not None:
#				buildLabel = buildGroup.label

		if buildLabel is not None:
			if False:
				infomsg(f": {sourceProject} has flavor {buildLabel} type {buildLabel.type}")
				for req in buildLabel.runtimeRequires:
					infomsg(f"  {buildLabel} -> {req}")

			# pretend that it's defined
			buildLabel.defined = True

		return buildLabel

	def getGroup(self, name, type = None):
		try:
			group = self._groups[type, name]
		except:
			return None

		if type and group.type != type:
			raise Exception(f"Group {name} does not match expected type (has {group.type}; expected {type})")

		return group

	def makeGroupInternal(self, name, type):
		try:
			group = self._groups[type, name]
		except:
			group = None

		if group is None:
			if not type:
				raise Exception(f"Cannot create group {name} with no type")

			group = PackageGroup(name)
			self._groups[type, name] = group

		if type:
			if group.label is None:
				group.label = self.classificationScheme.createLabel(name, type)
			elif group.type != type:
				raise Exception(f"Group {name} does not match expected type ({group.type} vs {type})")

		return group

	def getGroupForLabel(self, label, create = False):
		group = self.getGroup(label.name, label.type)
		if group is not None:
			assert(group.label is label)
		elif create:
			group = PackageGroup(label.name)
			group.label = label

			self._groups[label.type, label.name] = group

		return group

	@property
	def groups(self):
		return sorted(self._groups.values(), key = lambda grp: grp.matchCount)

	def updateGroup(self, label, packages):
		self.getGroupForLabel(label).update(packages)

	def labelOnePackage(self, pkg, label, reason):
		self.getGroupForLabel(label).track(pkg)
		pkg.labelReason = reason

	def getGroupPackages(self, label):
		group = self.getGroupForLabel(label)
		return group.closure

	# initially, we create the label tree with a maximum of edges
	# When reporting it in the output, we want to cut this down
	# to a reasonable complexity
	def getMinimalRuntimeRequirements(self, label, order):
		if not label.runtimeRequires:
			return set()

		group = self.getGroupForLabel(label)

		actualRequirements = set()
		fullRequirements = order.downwardClosureForSet(label.runtimeRequires)

		for pkg in group.closure:
			if pkg.resolvedRequires is None:
				infomsg(f"Unable to compute minimal requirements for {label}: requirements for {pkg} have not been resolved")
				return None

			for dep, required in pkg.resolvedRequires:
				requiredLabel = required.label
				if requiredLabel is None:
					infomsg(f"Unable to compute minimal requirements for {label}: {pkg} requires {required} which has not been labelled")
					return None

				if requiredLabel is label:
					continue

				if requiredLabel.type is Classification.TYPE_AUTOFLAVOR or \
				   requiredLabel.type is Classification.TYPE_PURPOSE:
					infomsg(f"Unable to compute minimal requirements for {label}: {pkg} requires {required} has automatic label {requiredLabel}")
					return None

				if requiredLabel not in fullRequirements:
					# either the user's input created a contradction, or we made a bad decision somewhere along the way
					warnmsg(f"CONFLICT: {pkg} has been placed in {label}, but it requires {required} which is in {requiredLabel}")
					return None

				actualRequirements.add(requiredLabel)

		if not actualRequirements:
			return actualRequirements

		def BUG(msg):
			warnmsg(f"BUG in computing minimal requirements for {label}: {msg}")
			infomsg(f"  actual requirements: {' '.join(map(str, actualRequirements))}")
			infomsg(f"  effective requirements: {' '.join(map(str, effectiveRequirements))}")

		# reduce the set to its maxima.
		# We have to bloat the set first, then reduce it again
		actualRequirements = order.downwardClosureForSet(actualRequirements)

		# FIXME: right now, order.maxima() returns a list rather than a set.
		effectiveRequirements = set(order.maxima(actualRequirements))

		if not effectiveRequirements:
			BUG("effective set is empty")

		if False:
			if label in effectiveRequirements:
				effectiveRequirements.remove(label)

		if not actualRequirements.issubset(fullRequirements):
			BUG("actual reqs not a subset of fullReqs")

		if not effectiveRequirements.issubset(fullRequirements):
			delta = effectiveRequirements.difference(fullRequirements)
			names = map(str, delta)
			warnmsg(f"{label} lacks some requirements: {' '.join(names)}")

		infomsg(f"Effective requirements for {label}: reduced from {len(label.runtimeRequires)} to {len(effectiveRequirements)} labels")
		if len(label.runtimeRequires) < 10 and len(effectiveRequirements) < 10:
			infomsg(f"  orig:    {' '.join(map(str, label.runtimeRequires))}")
			infomsg(f"  reduced: {' '.join(map(str, effectiveRequirements))}")
		return effectiveRequirements

	@property
	def packagePreferences(self):
		return self._preferences

	@property
	def autoFlavors(self):
		return self._autoflavors

	def getAutoFlavorDefinition(self, name):
		for label in self._autoflavors:
			if label.name == name:
				return label
		return None

	@property
	def objectPurposes(self):
		return self._purposes

	def getObjectPurposeDefinition(self, name):
		for label in self._purposes:
			if label.name == name:
				return label
		return None

	def parseGroup(self, groupType, gd):
		groupName = gd['name']
		group = self.makeGroupInternal(groupName, groupType)
		return self.processGroupDefinition(group, gd)

	def parseBuildFlavor(self, baseGroup, gd):
		flavorName = gd['name']
		if self.getObjectPurposeDefinition(flavorName):
			raise Exception(f"Invalid build flavor name {flavorName} in definition of {baseGroup.label}: already defined as an object purpose")

		group = self.makeFlavorGroup(baseGroup, flavorName)
		return self.processGroupDefinition(group, gd)

	def parseObjectPurpose(self, baseGroup, gd):
		purposeName = gd['name']
		if not self.getObjectPurposeDefinition(purposeName):
			raise Exception(f"Undefined purpose {purposeName} in definition of {baseGroup.label}: you must define {purposeName} globally first")

		group = self.makePurposeGroup(baseGroup, purposeName)
		return self.processGroupDefinition(group, gd)

	VALID_GROUP_FIELDS = set((
		'name',
		'description',
		'expand',
		'priority',
		'gravity',
		'requires',
		'buildrequires',
		'augments',
		'products',
		'packages',
		'packagesuffixes',
		'sources',
		'binaries',
		'rpmGroups',
		'buildflavors',
		'purposes',
		'sourceproject',
		'buildconfig',
		'disposition',
		'autoselect',
		'defaultlabel',
	))

	def processGroupDefinition(self, group, gd):
		def getBoolean(gd, tag):
			value = gd.get(tag)
			if value is not None and type(value) is not bool:
				raise Exception(f"{group.label}: bad value {tag}={value} (expected boolean value not {type(value)})")
			return value

		if group.defined:
			raise Exception(f"Duplicate definition of group \"{group.name}\" in filter yaml")
		group.defined = True

		for field in gd.keys():
			if field not in self.VALID_GROUP_FIELDS:
				raise Exception(f"Invalid field {field} in definition of group {group.name}")

		group.label.description = gd.get('description')

		name = gd.get('sourceproject')
		if name is not None:
			sourceProject = self.makeGroupInternal(name, Classification.TYPE_SOURCE)
			group.label.setSourceProject(sourceProject.label)

		name = gd.get('buildconfig')
		if name is not None:
			buildConfig = self.resolveBuildReference(name)
			group.label.setBuildConfig(buildConfig.label)

		value = gd.get('expand')
		if type(value) == bool:
			group.expand = value

		value = gd.get('disposition')
		if value is not None:
			if value == 'ignore' and group.type == Classification.TYPE_BINARY:
				# we allow regular labels to be marked as "ignore", which helps us hide problematic
				# packages like patterns-*
				pass
			elif value not in ('separate', 'merge', 'ignore', 'maybe_merge') or group.type not in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
				raise Exception(f"Invalid disposition={value} in definition of {group.type} group {group.name}")
			group.label.disposition = value

		value = getBoolean(gd, 'autoselect')
		if value is not None:
			group.label.autoSelect = value

		value = gd.get('defaultlabel')
		if value is not None:
			if group.label.type != Classification.TYPE_AUTOFLAVOR:
				raise Exception(f"Error: defaultlabel is not valid for {group.label.type} labels")
			group.label.defaultLabel = value

		priority = gd.get('priority')
		if priority is not None:
			assert(type(priority) == int)

		gravity = gd.get('gravity')
		if gravity is not None:
			assert(type(gravity) == int)
			group.label.gravity = gravity

			# we may have defined labels out of order; make sure subordinate purpose labels
			# inherit the gravity value
			for purpose in group.label._purposes.values():
				purpose.gravity = gravity

		if group.label:
			nameList = gd.get('requires') or []
			for name in nameList:
				otherGroup = self.resolveGroupReference(name, Classification.TYPE_BINARY)
				group.addRequires(otherGroup)

			# 'augments' are like runtime requirements, except they also flags the
			# group/flavor as an augmentation. Augmentations will never auto-select any
			# flavors that require any of the labels that are being augmented.
			#
			# Example:
			#  @Python+gnome contains a couple of python modules that are intended for use
			#	with gnome based frameworks. It requires @Gnome
			#  @Gnome+python contains python utilities that need these modules, and requires @Python
			#
			# If we work purely with requirements, we will end up with @Python+gnome
			# auto-selecting @Gnome+python.
			# By having @Python+gnome augment rather than require @Gnome, we end up with
			# @Gnome+python auto-selecting @Python+gnome rather than the other way around
			nameList = gd.get('augments') or []
			for name in nameList:
				otherGroup = self.resolveGroupReference(name, Classification.TYPE_BINARY)
				group.addAugmentation(otherGroup)

			nameList = gd.get('buildrequires') or []
			for name in nameList:
				otherGroup = self.resolveGroupReference(name, Classification.TYPE_BINARY)
				group.addBuildRequires(otherGroup)

		# The yaml file may specify per-group priorities for filters, but there is just
		# one global set of filters. Rather than passing the group and priority argument
		# into each add*Filter function, create a Builder object that does this transparently.
		filterSetBuilder = FilterSetBuilder(self.filterSet, group, priority)

		nameList = self.getYamlList(gd, 'products', group)
		for name in nameList:
			filterSetBuilder.addProductFilter(name)

		# Specifying a packagesuffix "foo" does the same thing as specifying
		# a package pattern "*-foo", except that the suffix is recorded in
		# the label to aid later placement.
		# Only makes sense with purpose labels right now
		nameList = self.getYamlList(gd, 'packagesuffixes', group)
		for name in nameList:
			group.label.packageSuffixes.append(name)

			name = f"*-{name}"
			filterSetBuilder.addBinaryPackageFilter(name)
			filterSetBuilder.addSourcePackageFilter(name)

		nameList = self.getYamlList(gd, 'packages', group)
		for name in nameList:
			filterSetBuilder.addBinaryPackageFilter(name)
			filterSetBuilder.addSourcePackageFilter(name)

		nameList = self.getYamlList(gd, 'sources', group)
		for name in nameList:
			filterSetBuilder.addSourcePackageFilter(name)

		nameList = self.getYamlList(gd, 'binaries', group)
		for name in nameList:
			filterSetBuilder.addBinaryPackageFilter(name)

		nameList = self.getYamlList(gd, 'rpmGroups', group)
		for name in nameList:
			filterSetBuilder.addRpmGroupFilter(name)

		flavors = self.getYamlList(gd, 'buildflavors', group)
		for fd in flavors:
			self.parseBuildFlavor(group, fd)

		purposes = self.getYamlList(gd, 'purposes', group)
		for fd in purposes:
			self.parseObjectPurpose(group, fd)

		return group

	def getYamlList(self, node, name, context):
		value = node.get(name)
		if value is None:
			return []

		if type(value) != list:
			raise Exception(f"In definition of {context.label}, {name} should be a list not a {type(value)}")

		return value
