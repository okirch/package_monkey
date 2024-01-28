import yaml
import fnmatch

from util import ExecTimer
from util import filterHighestRanking
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from ordered import PartialOrder
from functools import reduce
from stree import SolvingTreeBuilder
from pmatch import ParallelStringMatcher

# hack until I'm packaging fastsets properly
import fastsets.fastsets as fastsets

initialPlacementLogger = loggingFacade.getLogger('initial')
debugInitialPlacement = initialPlacementLogger.debug

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
	DISPOSITION_COMPONENT_WIDE = 'component_wide'

	# should this be a member of the classification scheme?
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
			self.imports = Classification.createLabelSet()
			self.exports = Classification.createLabelSet()
			self.disposition = Classification.DISPOSITION_SEPARATE
			# This is used in autoflavor labels only
			self.preferredLabels = []
			self.defined = False
			self.instanceOfTemplate = None
			self.compatibility = None

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

			# if isFeature is true, then the solving algorithm may give
			# this label greater significance than other labels.
			# The idea is to distinguish between labels that exist purely
			# for internal reasons, and those that can be considered as a
			# user-visible feature of the distro
			self.isFeature = False

			# The may be used later to locate "favorite sibling" packages,
			# eg systemd-mini-devel -> systemd-mini
			self.packageSuffixes = []

			self.isPurpose = False
			if self.purposeName is not None or self.type == Classification.TYPE_PURPOSE:
				self.isPurpose = True

			self._globalPurposeLabels = None
			if self.type == Classification.TYPE_SOURCE:
				self._globalPurposeLabels = {}

			self.isComponentLevel = False

			self.numImports = 0

		@property
		def fingerprint(self):
			values = [self.name, self.type, self.disposition, tuple(self.preferredLabels), self.gravity]
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
		def componentLabel(self):
			if self.sourceProject is not None:
				result = self.sourceProject
			elif self.buildConfig is not None:
				result = self.buildConfig.baseLabel
			else:
				return None
			assert(result.type is Classification.TYPE_SOURCE)
			return result

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

		@property
		def isImported(self):
			return self.componentLabel and self in self.componentLabel.imports

		@property
		def isExported(self):
			return self.componentLabel and self in self.componentLabel.exports

		def okayToAccess(self, other, componentLabelOrder):
			return self.canAccessDirectly(other, componentLabelOrder) or \
			       other.isExported

		def canAccessDirectly(self, other, componentLabelOrder):
			if self.componentLabel is None or other.componentLabel is None:
				return False

			if self.componentLabel is other.componentLabel:
				return True

			if componentLabelOrder.isBelow(other.componentLabel, self.componentLabel):
				return True

			return False

		def isCompatibleWithAutoFlavor(self, autoFlavor):
			# Do not instantiate CoreDevel+something
			if self.isComponentLevel:
				return False

			# templated labels and autoflavors may come with a "compatibility" setting. The goal is
			# to avoid creating labels like "PythonStandard311/python310".
			return (self.compatibility == autoFlavor.compatibility or \
					self.compatibility is None or
					autoFlavor.compatibility is None)

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

		def addImport(self, other):
			assert(isinstance(other, Classification.Label))
			self.imports.add(other)

		def addExport(self, other):
			assert(isinstance(other, Classification.Label))
			self.exports.add(other)

		def addMergeableFlavor(self, autoFlavor):
			assert(autoFlavor.type == Classification.TYPE_AUTOFLAVOR and self.parent is None)
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
				warnmsg(f"Ignoring duplicate buildconfig for {self}: {self.buildConfig} vs {configLabel}")
				return
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

		def autoSelectCompatibleFlavors(self, order, componentOrder):
			if not self.autoSelect:
				return

			myClosure = order.downwardClosureFor(self).copy()
			availableFlavors = set()
			for requiredLabel in myClosure:
				if requiredLabel is self:
					continue
				for flavor in requiredLabel.flavors:
					# check whether the component model would allow us to access this flavor
					if not self.okayToAccess(flavor, componentOrder):
						continue
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

		def globalPurposeLabel(self, name):
			if self._globalPurposeLabels is None:
				return None
			return self._globalPurposeLabels.get(name)

		def setGlobalPurposeLabel(self, name, label):
			# This can only be set on Components
			assert(self.type == Classification.TYPE_SOURCE)

			if name in self._globalPurposeLabels:
				raise Exception(f"{self}: attempt to redefine global purpose {name} as {label}")

			self._globalPurposeLabels[name] = label
			label.sourceProject = self

		def updateGlobalPurposeLabels(self, classification):
			if not self._globalPurposeLabels:
				return

			generalizedRequires = Classification.createLabelSet()
			for label in classification.allBinaryLabels:
				if label.componentLabel is not self:
					continue

				if label.isComponentLevel:
					continue

				generalizedRequires.add(label)

			# Update the label (eg CoreDevel) with all labels that are associated with
			# component Core, and which are not a component level label themselves.
			for name, label in self._globalPurposeLabels.items():
				label.runtimeRequires.update(generalizedRequires)

				for requiredComponent in self.runtimeRequires:
					required = requiredComponent.globalPurposeLabel(name)
					if required is None:
						warnmsg(f"{requiredComponent} does not define a label for purpose {name}")
						continue
					label.runtimeRequires.add(required)

		@property
		def globalPurposeLabelNames(self):
			return self._globalPurposeLabels.keys()

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

#	@classmethod
#	def baseLabelsForSet(klass, labels):
#		# for a base label, return self. For a derived label, return immediate parent
#		def transform(label): return label.parent or label
#
#		# if we ever allow more than 3 components in a label name, this needs to be adjusted
#		result = klass.createLabelSet(map(transform, labels))
#		result = klass.createLabelSet(map(transform, result))
#		return result

	# return the list of labels rated highest (ie with the lowest priority value)
	@staticmethod
	def filterLabelsByGravity(labels):
		return set(filterHighestRanking(labels, lambda l: l.gravity))

	@staticmethod
	def buildSolvingTree(classificationContext, packages, **kwargs):
		builder = SolvingTreeBuilder(classificationContext)
		return builder.buildTree(packages, **kwargs)

	@staticmethod
	def parseBinaryLabel(name):
		purposeName = None
		flavorName = None

		if '-' in name:
			(name, purposeName) = name.split('-')

		if '+' in name:
			(name, flavorName) = name.split('+')

		return (name, flavorName, purposeName)

	@staticmethod
	def parseBuildconfigLabel(name):
		flavorName = 'standard'

		if '/' in name:
			(name, flavorName) = name.split('/')

		return (name, flavorName)

	@staticmethod
	def parseLabel(type, name):
		if '/' in name and type is not Classification.TYPE_BUILDCONFIG:
			raise Exception(f"Invalid label {name}: not compatible with type {type}")

		flavorName = None
		purposeName = None

		if type is Classification.TYPE_BINARY:
			(baseName, flavorName, purposeName) = Classification.parseBinaryLabel(name)
			baseLabelType = type
		elif type is Classification.TYPE_BUILDCONFIG:
			baseName, flavorName = Classification.parseBuildconfigLabel(name)
			baseLabelType = Classification.TYPE_SOURCE
		else:
			assert('+' not in name)
			assert('-' not in name)
			baseName = name
			baseLabelType = type

		return (baseLabelType, baseName, flavorName, purposeName)

	@staticmethod
	def labelPackage(pkg, label, labelReason = None):
		if pkg.label is None or pkg.label.type in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
			pkg.label = label
			pkg.labelReason = labelReason
		elif pkg.label is not label:
			raise Exception(f"Refusing to change {pkg.fullname()} label from {pkg.label} to {label}")

	@staticmethod
	def labelBuild(build, label, labelReason = None):
		if build.baseLabel is None:
			build.baseLabel = label
			build.baseLabelReason = labelReason
		elif build.baseLabel is not label:
			raise Exception(f"Refusing to change {pkg.fullname()} label from {build.baseLabel} to {self.label}")

	class Scheme:
		def __init__(self):
			self._labels = {}
			self._nextLabelId = 0
			self._final = False

		@property
		def fingerprint(self):
			values = tuple(label.fingerprint for label in self.allLabels)
			return hash(values)

		@property
		def allBinaryLabels(self):
			return set(filter(lambda label: label.type == Classification.TYPE_BINARY, self._labels.values()))

		@property
		def allAutoPurposes(self):
			return set(filter(lambda label: label.type == Classification.TYPE_PURPOSE, self._labels.values()))

		@property
		def allAutoFlavors(self):
			return set(filter(lambda label: label.type == Classification.TYPE_AUTOFLAVOR, self._labels.values()))

		@property
		def allBuildConfigs(self):
			return set(filter(lambda label: label.type == Classification.TYPE_BUILDCONFIG, self._labels.values()))

		@property
		def allComponents(self):
			return set(filter(lambda label: label.type == Classification.TYPE_SOURCE, self._labels.values()))

		@property
		def isFinal(self):
			return self._final

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

			if baseLabel.isComponentLevel:
				raise Exception(f"Cannot derive flavor {flavorName} from label {baseLabel} because it is a component-level label")

			if baseLabel.type == Classification.TYPE_BINARY:
				label = self.createLabel(f"{baseLabel}+{flavorName}", Classification.TYPE_BINARY)
			elif baseLabel.type == Classification.TYPE_SOURCE:
				label = self.createLabel(f"{baseLabel}/{flavorName}", Classification.TYPE_BUILDCONFIG)
				sourceProject = baseLabel
			else:
				raise Exception(f"Cannot create flavor {flavorName} for {baseLabel.type} label {baseLabel}: unexpected type")

			if '/' in label.name:
				assert(baseLabel.type == Classification.TYPE_SOURCE)

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
			if template and template.disposition == Classification.DISPOSITION_COMPONENT_WIDE:
				raise Exception(f"Attempt to create {baseLabel}-{purposeName} even though {template} has disposition {template.disposition}")

			if baseLabel.purposeName is not None:
				raise Exception(f"Cannot derive purpose {purposeName} from label {baseLabel} because it already has a purpose")

			if baseLabel.isComponentLevel:
				raise Exception(f"Cannot derive purpose {purposeName} from label {baseLabel} because it is a component-level label")

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
				if not req.isPurpose and not req.isComponentLevel:
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

		def resolveLabel(self, name, type):
			if type is Classification.TYPE_BINARY:
				return self.resolveBinaryLabel(name)
			if type is Classification.TYPE_SOURCE:
				return self.resolveSourceLabel(name)
			raise Exception(f"Classification.resolveLabel: unsupported label type {type}")

		def resolveBinaryLabel(self, name):
			baseName, flavorName, purposeName = Classification.parseBinaryLabel(name)
			label = self.createLabel(baseName, Classification.TYPE_BINARY)
			if flavorName:
				label = self.resolveBuildFlavor(label, flavorName)
			if purposeName:
				label = self.resolvePurpose(label, purposeName)
				if label.disposition == Classification.DISPOSITION_COMPONENT_WIDE:
					errormsg(f"Encountered {label.type} label {label} with disposition {label.disposition} while parsing label {name}")
			return label

		def resolveBuildFlavor(self, label, flavorName):
			flavor = label.getBuildFlavor(flavorName)
			if flavor is None:
				assert(not self._final)
				flavor = self.createFlavor(label, flavorName)
			return flavor

		def resolvePurpose(self, label, purposeName):
			purpose = label.getObjectPurpose(purposeName)
			if purpose is None:
				assert(not self._final)
				purpose = self.createPurpose(label, purposeName)
			return purpose

		def resolveSourceLabel(self, name):
			# if it has a '/' in the name, it's a buildconfig not a source project
			if '/' in name:
				return None
			return self.createLabel(name, Classification.TYPE_SOURCE)

		def resolveBuildConfigLabel(self, name):
			baseName, flavorName = Classification.parseBuildconfigLabel(name)

			label = self.createLabel(baseName, Classification.TYPE_SOURCE)
			if flavorName:
				label = self.resolveBuildFlavor(label, flavorName)
			return label

		@property
		def allLabels(self):
			return sorted(self._labels.values(), key = lambda _: _.name)

		def createOrdering(self, labelType):
			if labelType not in (Classification.TYPE_BINARY, Classification.TYPE_SOURCE):
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

		def componentOrder(self):
			return self.createOrdering(Classification.TYPE_SOURCE)

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

			if self._final:
				raise Exception(f"Duplicate call to ClassificationScheme.finalize()")

			componentOrder = self.componentOrder()

			globalPurposeLabelNames = set()
			for componentLabel in componentOrder.bottomUpTraversal():
				globalPurposeLabelNames.update(componentLabel.globalPurposeLabelNames)

				globalDevel = componentLabel.globalPurposeLabel('devel')
				if globalDevel is not None:
					globalDevel.runtimeRequires.update(componentLabel.buildRequires)
					if globalDevel.buildConfig is None:
						globalDevel.buildConfig = self.resolveBuildConfigLabel(componentLabel.name)

				componentLabel.updateGlobalPurposeLabels(self)

				# build config X11/standard inherits from XXX/standard for all XXX components below X11
				# for lowerComponent in componentOrder.downwardClosureFor(componentLabel):
				for lowerComponent in componentLabel.runtimeRequires:
					for lowerConfig in lowerComponent.flavors:
						buildConfig = self.createFlavor(componentLabel, lowerConfig.name)
						buildConfig.buildRequires.update(lowerConfig.buildRequires)
						buildConfig.runtimeRequires.update(lowerConfig.runtimeRequires)

			for name in globalPurposeLabelNames:
				label = self.getLabel(name)
				if label is None or label.type != Classification.TYPE_PURPOSE:
					raise Exception(f"A component defines a global named {name}, but there is no corresponding purpose label")

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
			topicOrder = self.createOrdering(Classification.TYPE_BINARY)
			for label in topicOrder.bottomUpTraversal():
				label.autoSelectCompatibleFlavors(topicOrder, componentOrder)

			# A build config like Java/standard should buildrequire binary labels like @Java or @Core, but
			# it can also reference another buildconfig like Core/python. In this case, we want to expand
			# that reference to the actual binary labels that are used by Core/python.
			resolved = set()
			for label in self.allBuildConfigs:
				self.resolveBuildConfigDependencies(label, resolved)

			# ugly... this should really be attached to the classificationScheme *instance*
			Classification.baseLabelsForSet = fastsets.Transform(Classification.domain, lambda label: label.baseLabel)

			self._final = True

		def resolveBuildConfigDependencies(self, buildConfig, resolved, resolving = None):
			if resolving is None:
				resolving = set()

			assert(buildConfig not in resolving)
			resolving.add(buildConfig)

			resolvedSet = Classification.createLabelSet()
			for label in buildConfig.buildRequires:
				if label.type == Classification.TYPE_BUILDCONFIG or \
				   label.type == Classification.TYPE_BUILDCONFIG_FLAVOR:
					if label not in resolved:
						self.resolveBuildConfigDependencies(label, resolved, resolving)
					infomsg(f"{buildConfig} requires {label.type} {label}: resolved to {len(label.buildRequires)} labels")
					resolvedSet.update(label.buildRequires)
				elif label.type == Classification.TYPE_BINARY:
					resolvedSet.add(label)
				else:
					raise Exception(f"{label} references invalid {label.type} label {label}")

			buildConfig.buildRequires = resolvedSet

			resolving.discard(buildConfig)

		def getReferencingLabels(self, target):
			if target.type != Classification.TYPE_SOURCE:
				raise Exception(f"getReferencingLabels({target}): label type {target.type} not implemented")

			result = set()
			for label in self.allBinaryLabels:
				if label.sourceProject == target:
					result.add(label)

			return result

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

class PackageGroup:
	def __init__(self, name):
		self.name = name
		self.label = None
		self._buildFlavors = {}
		self._objectPurposes = {}

	def track(self, pkg):
		Classification.labelPackage(pkg, self.label, "by PackageGroup.track")

	@property
	def type(self):
		return self.label.type

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

	def addImport(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addImport(otherGroup.label)

	def addExport(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addExport(otherGroup.label)

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

class ClassificationResult(object):
	class PackageMembership(object):
		def __init__(self, label):
			self.label = label
			self.packages = set()

		def track(self, pkg, reason):
			Classification.labelPackage(pkg, self.label, reason)
			self.packages.add(pkg)

	class ProjectMembership(object):
		def __init__(self, label):
			self.label = label
			self.packages = set()

		def track(self, build):
			self.packages.add(build)
			build.label = self.label

	class BuildInfo(object):
		def __init__(self, name):
			self.name = name
			self.binaries = []
			self.sources = []
			self.buildRequires = []
			self.label = None
			self.buildConfig = None

		def __str__(self):
			return self.name

		@property
		def basePackageName(self):
			name = self.name
			if ':' in name:
				return name.split(':')[0]
			return name

	class PackageRequirements(dict):
		def __init__(self):
			pass

		def add(self, pkg, target):
			requires = self.get(pkg)
			if requires is None:
				requires = set()
				self[pkg] = requires

			requires.add(target)

	class UnclassifiedPackage(object):
		def __init__(self, pkg, candidates):
			self.pkg = pkg
			self.candidates = candidates

	def __init__(self, labelOrder, componentOrder = None):
		self._labelOrder = labelOrder
		self._packages = {}
		self._projects = {}
		self._builds = []

		self._componentOrder = componentOrder
		self._components = []

		self._unclassified = []

		self._brokenDependencies = None

	def enableBrokenDependencyTracking(self):
		if self._brokenDependencies is None:
			self._brokenDependencies = self.PackageRequirements()

	def packageMembership(self, label):
		m = self._packages.get(label)
		if m is None:
			m = self.PackageMembership(label)
			self._packages[label] = m
		return m

	def projectMembership(self, label):
		m = self._projects.get(label)
		if m is None:
			m = self.ProjectMembership(label)
			self._projects[label] = m
		return m

	def labelOnePackage(self, pkg, label, reason):
		self.packageMembership(label).track(pkg, reason)

	def labelOneBuild(self, name, label, binaries, sources, buildConfig = None):
		buildInfo = self.BuildInfo(name)
		self._builds.append(buildInfo)

		buildInfo.binaries += binaries
		buildInfo.sources += sources
		buildInfo.label = label
		buildInfo.buildConfig = buildConfig

		for rpm in sources:
			if rpm.resolvedRequires is None:
				infomsg(f"Missing build requirements for {rpm}")
				continue

			for dep, required in rpm.resolvedRequires:
				buildInfo.buildRequires.append(required)

		if label is not None:
			self.projectMembership(label).track(buildInfo)

	def addComponent(self, label):
		self._components.append(label)

	def addUnclassified(self, pkg, candidates):
		self._unclassified.append(self.UnclassifiedPackage(pkg, candidates))

	def enumeratePackages(self):
		for label in self._labelOrder.bottomUpTraversal():
			members = self.packageMembership(label).packages
			yield label, members

	def enumerateBuilds(self):
		infomsg(f"result contains {len(self._builds)} builds")
		for buildInfo in self._builds:
			yield buildInfo.label, buildInfo

	def enumerateComponents(self):
		for label in self._components:
			requires = label.runtimeRequires
			if self._componentOrder is not None:
				requires = self._componentOrder.maxima(requires)
			yield label, requires

	def enumerateUnclassifiedPackages(self):
		for entry in self._unclassified:
			yield entry.pkg, entry.candidates

	# initially, we create the label tree with a maximum of edges
	# When reporting it in the output, we want to cut this down
	# to a reasonable complexity
	def getMinimalRuntimeRequirements(self, label):
		if not label.runtimeRequires:
			return set()

		actualRequirements = self.collectActualRuntimeRequirements(label)
		if actualRequirements is None:
			# at least return something, even if it may be inconsistent
			return label.runtimeRequires

		effectiveRequirements = self.reduceRequirements(label, actualRequirements)

		if False:
			infomsg(f"Effective requirements for {label}: reduced from {len(label.runtimeRequires)} to {len(effectiveRequirements)} labels")
			if len(label.runtimeRequires) < 10 and len(effectiveRequirements) < 10:
				infomsg(f"  orig:    {' '.join(map(str, label.runtimeRequires))}")
				infomsg(f"  reduced: {' '.join(map(str, effectiveRequirements))}")

		return effectiveRequirements

	def collectActualRuntimeRequirements(self, label):
		# Should really be a fastset not a set
		actualRequirements = set()
		failed = False

		fullRequirements = self._labelOrder.downwardClosureForSet(label.runtimeRequires)

		members = self.packageMembership(label).packages
		for pkg in members:
			if pkg.resolvedRequires is None:
				infomsg(f"Unable to compute minimal requirements for {label}: requirements for {pkg} have not been resolved")
				failed = True
				continue

			for dep, required in pkg.resolvedRequires:
				requiredLabel = required.label
				if requiredLabel is None:
					infomsg(f"Unable to compute minimal requirements for {label}: {pkg} requires {required} which has not been labelled")
					failed = True
					continue

				if requiredLabel is label:
					continue

				if requiredLabel.type is Classification.TYPE_AUTOFLAVOR or \
				   requiredLabel.type is Classification.TYPE_PURPOSE:
					infomsg(f"Unable to compute minimal requirements for {label}: {pkg} requires {required} has automatic label {requiredLabel}")
					failed = True
					continue

				if requiredLabel not in fullRequirements:
					# either the user's input created a contradction, or we made a bad decision somewhere along the way
					# warnmsg(f"CONFLICT: {pkg} has been placed in {label}, but it requires {required} which is in {requiredLabel}")
					if self._brokenDependencies is not None:
						self._brokenDependencies.add(pkg, required)
					failed = True
					continue

				actualRequirements.add(requiredLabel)

		if failed:
			return None

		return actualRequirements

	def reduceRequirements(self, what, actualRequirements):
		if not actualRequirements:
			return actualRequirements

		def BUG(msg):
			warnmsg(f"BUG in computing minimal requirements for {what}: {msg}")
			infomsg(f"  actual requirements: {' '.join(map(str, actualRequirements))}")
			infomsg(f"  effective requirements: {' '.join(map(str, effectiveRequirements))}")
			raise Exception()

		# reduce the set to its maxima.
		# We have to bloat the set first, then reduce it again
		actualRequirements = self._labelOrder.downwardClosureForSet(actualRequirements)

		effectiveRequirements = self._labelOrder.maxima(actualRequirements)

		if not effectiveRequirements:
			BUG("effective set is empty")

		return effectiveRequirements

	def getMinimalBuildRequirements(self, buildInfo):
		actualRequirements = self.collectActualBuildRequirements(buildInfo)

		if actualRequirements is None:
			return None

		return self.reduceRequirements(buildInfo.name, actualRequirements)

	def collectActualBuildRequirements(self, buildInfo):
		# Should really be a fastset not a set
		actualRequirements = set()
		failed = False

		buildName = buildInfo.name
		for pkg in buildInfo.sources:
			if pkg.resolvedRequires is None:
				infomsg(f"Unable to compute minimal requirements for {buildName}: requirements for {pkg} have not been resolved")
				failed = True
				continue

			for dep, required in pkg.resolvedRequires:
				requiredLabel = required.label
				if requiredLabel is None:
					infomsg(f"Unable to compute minimal requirements for {buildName}: {pkg} requires {required} which has not been labelled")
					failed = True
					continue

				if requiredLabel.type is Classification.TYPE_AUTOFLAVOR or \
				   requiredLabel.type is Classification.TYPE_PURPOSE:
					infomsg(f"Unable to compute minimal requirements for {buildName}: {pkg} requires {required} has automatic label {requiredLabel}")
					failed = True
					continue

				actualRequirements.add(requiredLabel)

		if failed:
			return None

		return actualRequirements

	def reportBrokenDependencies(self):
		if not self._brokenDependencies:
			return False

		for pkg, brokenRequires in self._brokenDependencies.items():
			for required in brokenRequires:
				warnmsg(f"CONFLICT: {pkg} has been placed in {pkg.label}, but it requires {required} which is in {required.label}")
		return True

class PackageLabelling(object):
	PRIORITY_DEFAULT = 5

	class Match:
		def __init__(self, pattern, type, priority, label):
			self.type = type # binary or source
			self.pattern = pattern
			self.label = label

			assert(priority <= 10)
			precedence = (10 - priority) * 100

			# non-wildcard matches have a higher precedence than wildcarded ones
			if '?' not in pattern and '*' not in pattern:
				precedence += 1000

			# longer patterns have higher precedence than shorter ones
			precedence += len(pattern)

			self.precedence = precedence

		def __str__(self):
			return f"{self.label}/{self.precedence}"

	def __init__(self):
		self.binaryMatcher = ParallelStringMatcher()
		self.sourceMatcher = ParallelStringMatcher()
		self.buildMatcher = ParallelStringMatcher()

	# FIXME: rather than sorting each and every result of the table, we could
	# sort ALL matches by precedence once and then feed the patterns to the
	# ParallelMatcher in order.
	# However, that does not really address the problem as a shorter match may
	# return less important results before a longer match with a higher precedence
	# result.
	def addBinaryMatch(self, pattern, priority, label):
		m = self.Match(pattern, 'binary', priority, label)
		self.binaryMatcher.add(pattern, m)

	def addSourceMatch(self, pattern, priority, label):
		m = self.Match(pattern, 'source', priority, label)
		self.sourceMatcher.add(pattern, m)

	def addBuildMatch(self, pattern, priority, label):
		m = self.Match(pattern, 'package', priority, label)
		self.buildMatcher.add(pattern, m)

	def finalize(self):
		pass

	def applyToPackage(self, pkg):
		if not pkg.isSourcePackage:
			matches = self.binaryMatcher.match(pkg.name)
			if not matches:
				src = pkg.sourcePackage
				if src is not None:
					matches = self.sourceMatcher.match(src.name)
		else:
			matches = self.sourceMatcher.match(pkg.name)

		return self.returnMatches(pkg.name, matches, pkg.trace)

	def applyToBuild(self, build):
		matches = self.buildMatcher.match(build.name)
		return self.returnMatches(build.name, matches, build.trace)

	def returnMatches(self, name, matches, trace):
		if not matches:
			if trace:
				infomsg(f"{name}: no match by package filter")
			return None

		if len(matches) > 1:
			matches = sorted(matches, key = lambda m: m.precedence, reverse = True)
			m = matches.pop(0)

			if trace:
				infomsg(f"{name}: {m.label} matched by {m.type} filter {m.pattern}")
				infomsg(f"   {len(matches)} lower priority matches were ignored:")
				for other in matches:
					infomsg(f"      {other.label} {other.type} {other.pattern}")
		else:
			m = next(iter(matches))

			if trace:
				infomsg(f"{name}: {m.label} matched by {m.type} filter {m.pattern}")

		return PackageFilter.Verdict(m.label, f"{m.type} filter {m.pattern}")

class StringMatchBuilder(object):
	def __init__(self, stringMatcher, label, priority = None):
		self.stringMatcher = stringMatcher
		self.label = label

		if priority is None:
			priority = PackageLabelling.PRIORITY_DEFAULT
		self.priority = priority

	def addBinaryPackageFilter(self, name):
		pattern, priority, label = self.processPattern(name)
		self.stringMatcher.addBinaryMatch(pattern, priority, label)

	def addSourcePackageFilter(self, name):
		pattern, priority, label = self.processPattern(name)
		self.stringMatcher.addSourceMatch(pattern, priority, label)

	def addOBSPackageFilter(self, name):
		pattern, priority, label = self.processPattern(name)
		self.stringMatcher.addBuildMatch(pattern, priority, label)

	def processPattern(self, value):
		label = self.label
		priority = self.priority

		# A match may come with additional parameters, as in
		#
		#	postgresql-* priority=8
		#
		if ' ' in value or '\t' in value:
			words = value.split()
			value = words[0]
			for param in words[1:]:
				(argName, argValue) = param.split('=')
				if argName == 'priority':
					priority = int(argValue)
				elif argName == 'purpose':
					purposeLabel = label.getObjectPurpose(argValue)
					if purposeLabel is None:
						componentLabel = label.componentLabel
						if componentLabel is not None:
							purposeLabel = componentLabel.globalPurposeLabel(argValue)
					if purposeLabel is None:
						raise Exception(f"Cannot add filter for \"{value}\" - unknown purpose {argValue} in label {label}")
					label = purposeLabel
				else:
					raise Exception(f"Unknown match parameter {param} in {self.filterSet} expression \"{value}\" for label {label}");

		return (value, priority, label)

class FilterTemplate:
	def __init__(self, name, key, document):
		self.name = name
		self._key = key
		self._doc = document

	class Instance:
		def __init__(self, template, value):
			self.template = template
			self._key = template._key
			self._value = value

		def expand(self):
			return self.expandData(self.template._doc)

		def expandData(self, data):
			t  = type(data)
			if t is str:
				return data.replace(self._key, self._value)
			if t is list:
				return list(map(self.expandData, data))
			if t is dict:
				return {name: self.expandData(value) for (name, value) in data.items()}
			if t in (bool, int, float) or data is None:
				return data

			raise Exception(f"data type {t} not yet implemented")

	def instantiate(self, arg):
		return self.Instance(self, arg)

class PackageFilter:
	class Verdict:
		def __init__(self, label, reason):
			self.label = label
			self.reasonString = reason

		def labelPackage(self, pkg):
			labelReason = Classification.ReasonFilter(pkg, self.reasonString)
			Classification.labelPackage(pkg, self.label, labelReason)

		def labelBuild(self, build):
			labelReason = Classification.ReasonFilter(build, self.reasonString)
			Classification.labelBuild(build, self.label, labelReason)

	def __init__(self, filename = 'filter.yaml', scheme = None):
		self.classificationScheme = scheme or Classification.Scheme()
		self._groups = {}
		self._autoflavors = []
		self._purposes = []
		self._templates = {}

		self.stringMatcher = PackageLabelling()

		timer = ExecTimer()
		self.load(filename)
		infomsg(f"Loaded filter definition from {filename}: {timer} elapsed")

	def load(self, filename):
		with open(filename) as f:
			data = yaml.full_load(f)

		for gd in data.get('templates') or []:
			self.parseTemplate(gd)

		# Parse autoflavors *before* everything else so that we can populate
		# newly created flavors from default settings.
		for gd, template in self.expandYamlObjectList(data, 'autoflavors'):
			group = self.parseGroup(Classification.TYPE_AUTOFLAVOR, gd, template)
			self._autoflavors.append(group)

		for gd, template in self.expandYamlObjectList(data, 'purposes'):
			group = self.parseGroup(Classification.TYPE_PURPOSE, gd, template)
			self._purposes.append(group)

		for gd, template in self.expandYamlObjectList(data, 'build_configs'):
			self.parseGroup(Classification.TYPE_BUILDCONFIG, gd, template)

		for gd, template in self.expandYamlObjectList(data, 'buildconfig_flavors'):
			self.parseGroup(Classification.TYPE_BUILDCONFIG_FLAVOR, gd, template)

		for gd, template in self.expandYamlObjectList(data, 'components'):
			self.parseGroup(Classification.TYPE_SOURCE, gd, template)

		for gd, template in self.expandYamlObjectList(data, 'groups'):
			self.parseGroup(Classification.TYPE_BINARY, gd, template)

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

		self.stringMatcher.finalize()

		for group in self._groups.values():
			label = group.label
			validateDependencies(label.buildRequires)

			# resolve the preferred labels
			if label.preferredLabels:
				resolved = []
				for labelName in label.preferredLabels:
					defaultLabel = self.classificationScheme.getLabel(labelName)
					if defaultLabel is None or not defaultLabel.defined:
						raise Exception(f"Label {label} specifies preferred label {labelName}, which is not defined anywhere")
					if defaultLabel.type is not Classification.TYPE_BINARY:
						raise Exception(f"Label {label} specifies preferred label {labelName}, which is of type {defaultLabel.type}")
					resolved.append(defaultLabel)
				label.preferredLabels = resolved

		# For all base labels, instantiate their auto flavors (ie for @Foo, instantiate
		# @Foo+python, @Foo+ruby, etc)
		for group in list(self._groups.values()):
			if group.label.parent is not None or \
			   group.label.type is not Classification.TYPE_BINARY:
				continue

			for autoFlavor in self.autoFlavors:
				if autoFlavor.label.disposition is Classification.DISPOSITION_SEPARATE:
					self.maybeInstantiateAutoFlavor(group, autoFlavor)

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
					self.maybeInstantiateAutoFlavor(group, autoFlavor)

		for group in list(self._groups.values()):
			if group.label.type is Classification.TYPE_BINARY and not group.label.isPurpose and not group.label.isComponentLevel:
				assert(not group.label.isPurpose)
				for purposeDef in self._purposes:
					if purposeDef.label.disposition == Classification.DISPOSITION_COMPONENT_WIDE:
						continue

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

	def tryToLabelPackage(self, pkg):
		verdict = self.stringMatcher.applyToPackage(pkg)
		if verdict is not None:
			verdict.labelPackage(pkg)
			debugInitialPlacement(f"{pkg} is placed in {verdict.label} by package filter rules")

	def tryToLabelBuild(self, build):
		verdict = self.stringMatcher.applyToBuild(build)
		if verdict is not None:
			verdict.labelBuild(build)
			debugInitialPlacement(f"{build} is placed in {verdict.label} by package filter rules")

	def makeGroup(self, name, type = None):
		assert(type)
		return self.makeGroupInternal(name, type)

	def resolveLabelReference(self, name, labelType = Classification.TYPE_BINARY):
		group = self.resolveGroupReference(name, labelType)
		if group.label is None:
			raise Exception(f"Group {name} has a NULL label")
		return group.label

	def resolveGroupReference(self, name, labelType = Classification.TYPE_BINARY):
		baseLabelType, baseName, flavorName, purposeName = Classification.parseLabel(labelType, name)

		baseLabel = self.classificationScheme.createLabel(baseName, baseLabelType)
		group = self.getGroupForLabel(baseLabel, create = True);

		if flavorName is not None:
			group = self.makeFlavorGroup(group, flavorName)

		if purposeName is not None:
			group = self.makePurposeGroup(group, purposeName)

		return group

	def resolveBuildReference(self, name):
		sourceProjectName, flavorName = Classification.parseBuildconfigLabel(name)

		group = self.makeGroupInternal(sourceProjectName, Classification.TYPE_SOURCE)
		return self.makeFlavorGroup(group, flavorName)

	def makeSourceGroup(self, name):
		return self.makeGroupInternal(name, Classification.TYPE_SOURCE)

	def makeBinaryGroup(self, name):
		return self.makeGroupInternal(name, Classification.TYPE_BINARY)

	def maybeInstantiateAutoFlavor(self, baseGroup, autoFlavor):
		if baseGroup.label.isCompatibleWithAutoFlavor(autoFlavor.label):
			self.instantiateAutoFlavor(baseGroup, autoFlavor)

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
		buildLabel = baseGroup.label.getBuildConfigFlavor(flavorName)

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

		if group is not None:
			if type and group.type != type:
				raise Exception(f"Group {name} does not match expected type ({group.type} vs {type})")
			return group

		if not type:
			raise Exception(f"Cannot create group {name} with no type")
		return self.resolveGroupReference(name, labelType = type)

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

	def parseTemplate(self, gd):
		template = FilterTemplate(gd['name'], gd['substitute'], gd['document'])
		if template.name in self._templates:
			raise Exception(f"Duplicate definition of template {template.name}")
		self._templates[template.name] = template

	def instantiateTemplate(self, reference):
		templateName, templateArg = reference.split(':')
		template = self._templates.get(templateName)
		if template is None:
			raise Exception(f"Unknown template name in template instantiation {reference}")

		return template.instantiate(templateArg)

	def parseGroup(self, groupType, gd, template):
		groupName = gd['name']
		group = self.makeGroupInternal(groupName, groupType)
		return self.processGroupDefinition(group, gd, template)

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
		'priority',
		'gravity',
		'requires',
		'buildrequires',
		'augments',
		'imports',
		'exports',
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
		'compatibility',
		'disposition',
		'autoselect',
		'defaultlabel',
		'defaultlabels',
		'feature',
		'globals',
	))

	# FIXME: reduce use of `group' in this function
	def processGroupDefinition(self, group, gd, template = None):
		def getBoolean(gd, tag):
			value = gd.get(tag)
			if value is not None and type(value) is not bool:
				raise Exception(f"{group.label}: bad value {tag}={value} (expected boolean value not {type(value)})")
			return value

		def getString(gd, tag):
			value = gd.get(tag)
			if value is not None and type(value) is not str:
				raise Exception(f"{group.label}: bad value {tag}={value} (expected string value not {type(value)})")
			return value

		groupLabel = group.label

		if group.defined:
			raise Exception(f"Duplicate definition of group \"{group.name}\" in filter yaml")
		group.defined = True

		if template is not None:
			group.label.instanceOfTemplate = template.name

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

		value = gd.get('disposition')
		if value is not None:
			if value == 'ignore' and group.type == Classification.TYPE_BINARY:
				# we allow regular labels to be marked as "ignore", which helps us hide problematic
				# packages like patterns-*
				pass
			elif value not in ('separate', 'merge', 'ignore', 'maybe_merge', 'component_wide') or group.type not in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
				raise Exception(f"Invalid disposition={value} in definition of {group.type} group {group.name}")
			group.label.disposition = value

		value = getBoolean(gd, 'autoselect')
		if value is not None:
			group.label.autoSelect = value

		value = getBoolean(gd, 'feature')
		if value is not None:
			group.label.isFeature = value

		value = gd.get('defaultlabel')
		if value is not None:
			if group.label.type != Classification.TYPE_AUTOFLAVOR:
				raise Exception(f"Error: defaultlabel is not valid for {group.label.type} labels")
			group.label.preferredLabels.insert(0, value)

		nameList = self.getYamlList(gd, 'defaultlabels', group)
		for name in nameList:
			if group.label.type != Classification.TYPE_AUTOFLAVOR:
				raise Exception(f"Error: defaultlabels is not valid for {group.label.type} labels")
			if type(name) != str:
				raise Exception(f"Error: unexpected {type(name)} in list of default labels")
			group.label.preferredLabels.append(name)

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
			nameList = self.getYamlList(gd, 'requires', group)
			for name in nameList:
				if group.type is Classification.TYPE_SOURCE:
					labelType = group.type
				else:
					labelType = Classification.TYPE_BINARY

				otherGroup = self.resolveGroupReference(name, labelType)
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
			nameList = self.getYamlList(gd, 'augments', group)
			for name in nameList:
				otherGroup = self.resolveGroupReference(name)
				group.addAugmentation(otherGroup)

			nameList = self.getYamlList(gd, 'buildrequires', group)
			for name in nameList:
				otherGroup = self.resolveGroupReference(name)
				group.addBuildRequires(otherGroup)

			nameList = self.getYamlList(gd, 'imports', group)
			for name in nameList:
				referencedLabel = self.resolveLabelReference(name)
				groupLabel.addImport(referencedLabel)

			nameList = self.getYamlList(gd, 'exports', group)
			for name in nameList:
				referencedLabel = self.resolveLabelReference(name)
				groupLabel.addExport(referencedLabel)

		# The yaml file may specify per-group priorities for filters, but there is just
		# one global set of filters. Rather than passing the group and priority argument
		# into each add*Filter function, create a Builder object that does this transparently.
		filterSetBuilder = StringMatchBuilder(self.stringMatcher, group.label, priority)

		nameList = self.getYamlList(gd, 'products', group)
		for name in nameList:
			raise Exception(f"package filter 'products' no longer supported")

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
		if nameList and group.label.parent is not None:
			raise Exception(f"{group.label}: packages list only valid in base labels")
		for name in nameList:
			filterSetBuilder.addOBSPackageFilter(name)

		nameList = self.getYamlList(gd, 'sources', group)
		for name in nameList:
			filterSetBuilder.addSourcePackageFilter(name)

		nameList = self.getYamlList(gd, 'binaries', group)
		for name in nameList:
			filterSetBuilder.addBinaryPackageFilter(name)

		nameList = self.getYamlList(gd, 'rpmGroups', group)
		for name in nameList:
			raise Exception(f"package filter 'rpmGroups' no longer supported")

		flavors = self.getYamlList(gd, 'buildflavors', group)
		for fd in flavors:
			self.parseBuildFlavor(group, fd)

		purposes = self.getYamlList(gd, 'purposes', group)
		for fd in purposes:
			self.parseObjectPurpose(group, fd)

		globals = gd.get('globals')
		if globals is not None:
			if group.type != Classification.TYPE_SOURCE:
				raise Exception(f"You cannot specify globals in a {group.type} group definition")

			for purposeName, labelName in globals.items():
				# print(f"  {group.label}: {purposeName} -> {labelName}")
				purposeLabel = self.classificationScheme.createLabel(labelName, Classification.TYPE_BINARY)
				purposeLabel.isComponentLevel = True
				group.label.setGlobalPurposeLabel(purposeName, purposeLabel)

		group.label.compatibility = getString(gd, 'compatibility')

		return group

	def expandYamlObjectList(self, data, name):
		objectList = data.get(name)
		if objectList is None:
			return

		for gd in objectList:
			templateRef = gd.get('instantiate')
			if templateRef is None:
				yield gd, None
				continue

			instance = self.instantiateTemplate(templateRef)

			instanceData = instance.expand()

			if type(instanceData) is not list:
				yield instanceData, instance.template
			else:
				for expanded in instanceData:
					yield expanded, instance.template

	def getYamlList(self, node, name, context):
		value = node.get(name)
		if value is None:
			return []

		if type(value) != list:
			raise Exception(f"In definition of {context.label}, {name} should be a list not a {type(value)}")

		return value
