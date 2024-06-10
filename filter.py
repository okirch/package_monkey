import yaml
import fnmatch

from util import ExecTimer
from util import filterHighestRanking
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from util import VariableExpander, FrequencyCounter
from ordered import PartialOrder
from functools import reduce
from stree import SolvingTreeBuilder
from pmatch import ParallelStringMatcher
from profile import profiling

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

	VALID_TYPES = (
		TYPE_BINARY,
		TYPE_SOURCE,
		TYPE_AUTOFLAVOR,
		TYPE_PURPOSE,
		TYPE_BUILDCONFIG,
		TYPE_BUILDCONFIG_FLAVOR,
	)

	DISPOSITION_SEPARATE = 'separate'
	DISPOSITION_MERGE = 'merge'
	DISPOSITION_MAYBE_MERGE = 'maybe_merge'
	DISPOSITION_IGNORE = 'ignore'
	DISPOSITION_COMPONENT_WIDE = 'component_wide'

	# should this be a member of the classification scheme?
	domain = fastsets.Domain("label")

	class LabelCategory(object):
		def __init__(self, name):
			self.name = name
			self.frozen = False

		def __str__(self):
			return self.name

		def freeze(self):
			self.frozen = True

	class Label(domain.member):
		def __init__(self, name, type, id):
			super().__init__()

			self.name = name
			self.type = type
			self.id = id
			self.description = None
			self.gravity = None
			self.runtimeRequires = Classification.createLabelSet()
			self.buildRequires = Classification.createLabelSet()
			self.runtimeAugmentations = Classification.createLabelSet()
			self.configuredRuntimeRequires = Classification.createLabelSet()
			self.configuredBuildRequires = Classification.createLabelSet()
			self.configuredRuntimeAugmentations = Classification.createLabelSet()
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
			self.fromAutoFlavor = None

			# This is populated for base flavors like @Core
			self._flavors = {}

			# This is populated for labels that can have different purposes
			self._purposes = {}

			self.mergeableAutoFlavors = set()

			# binary labels and build config labels have a source project assigned
			self.sourceProject = None

			# for a component label, this will hold the set of topic labels that
			# belong to this component.
			# It will be set after the component tree has been frozen
			self._referencingLabels = None

			# if autoSelect is true, then a group referencing a label
			# "@Foo" will automatically select all flavors "@Foo+bar"
			# if it supports all requirements of this flavor.
			# For instance, "@Core+systemd" may contain utilities that
			# need libsystemd. A label "@Bar" that requires both @Core
			# and @MinimalSystemd will automatically add @Foo+bar to its
			# closure
			self.autoSelect = True

			# In the label that receives auto-selected flavors,
			# track the labels that were added due to auto-selection
			self.automaticRuntimeRequires = Classification.createLabelSet()

			# if isFeature is true, then the solving algorithm may give
			# this label greater significance than other labels.
			# The idea is to distinguish between labels that exist purely
			# for internal reasons, and those that can be considered as a
			# user-visible feature of the distro
			self.isFeature = False

			# if isInheritable is true for a buildconfig label Foo/blah, then all
			# components SuperFoo will also have a buildconfig SuperFoo/blah
			self.isInheritable = True

			# The may be used later to locate "favorite sibling" packages,
			# eg systemd-mini-devel -> systemd-mini
			self.packageSuffixes = []

			self.isPurpose = False
			if self.purposeName is not None or self.type == Classification.TYPE_PURPOSE:
				self.isPurpose = True

			self._globalPurposeLabels = None
			if self.type == Classification.TYPE_SOURCE:
				self._globalPurposeLabels = {}

			self.correspondingAPI = None
			self.apiForLabels = None
			# Initialize this as None rather than False so that we know which labels
			# still need propagation from their parent label.
			self.isAPI = None

			# This exist to make handling of @SystemPythonFoobar easier.
			# The @System label usually requires @PythonFoobar311 or some such,
			# and we want it to inherit all flavors of Foobar311. IOW, if a label
			# @PythonFoobar311+tex exists, we want to define @SystemPythonFoobar+tex
			self.inheritAllFlavors = False

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
			return None

		@property
		def componentLabel(self):
			if self.sourceProject is not None:
				result = self.sourceProject
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

		@property
		def isBaseLabel(self):
			return self.parent is None

		def setAPI(self, api):
			assert(self.correspondingAPI is None)

			self.correspondingAPI = api

			if api.apiForLabels is None:
				api.apiForLabels = Classification.createLabelSet()
			api.apiForLabels.add(self)
			api.isAPI = True

		# This is called during validation
		def validateAPI(self):
			if not self.isAPI:
				for req in self.runtimeRequires:
					if req.isAPI:
						warnmsg(f"  Non-API {self} requires API {req}")
				return

			if self.apiForLabels is None:
				return

			for other in self.apiForLabels:
				if other.sourceProject != self.sourceProject:
					raise Exception(f"{self} cannot be declared the API for {other} - conflicting source projects {self.sourceProject} and {other.sourceProject}")

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

		def finalizeExports(self):
			assert(self.type == Classification.TYPE_SOURCE)

			apis = Classification.createLabelSet()
			for exportedLabel in self.exports:
				if exportedLabel.componentLabel != self:
					raise Exception(f"{self}: illegal export of {exportedLabel} which is part of component {exportedLabel.componentLabel}")

				# if component Foo exports @BarLibraries, we also want to export @BarAPI
				api = exportedLabel.correspondingAPI
				if api is not None:
					if exportedLabel.componentLabel != self:
						raise Exception(f"{self}: api {api} for {exportedLabel} is in different component {api.componentLabel}")
					apis.add(api)

			api = self.globalPurposeLabel('devel')
			if api is not None:
				apis.add(api)

			if False:
				adding = apis.difference(self.exports)
				if adding:
					infomsg(f"{self}: automatically exporting the following APIs: {' '.join(map(str, adding))}")

			self.exports.update(apis)

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
			if self.type is Classification.TYPE_BUILDCONFIG:
				raise Exception(f"{self}: requires not valid in definition of buildconfig labels")
			if not self.okayToAdd(other):
				raise Exception(f"Attempt to add incompatible dependency to {self.type} label {self}: {other} (type {other.type})")

			self.runtimeRequires.add(other)

		def addRuntimeAugmentation(self, other):
			self.addRuntimeDependency(other)
			self.runtimeAugmentations.add(other)

		def addBuildDependency(self, other):
			assert(isinstance(other, Classification.Label))
			if self.type is not Classification.TYPE_BUILDCONFIG:
				raise Exception(f"{self}: buildrequires only valid in definition of buildconfig labels")
			self.buildRequires.add(other)

		def configureRuntimeDependency(self, other):
			self.addRuntimeDependency(other)
			self.configuredRuntimeRequires.add(other)

		def configureBuildDependency(self, other):
			self.addBuildDependency(other)
			self.configuredBuildRequires.add(other)

		def configureRuntimeAugmentation(self, other):
			self.addRuntimeAugmentation(other)
			self.configuredRuntimeAugmentations.add(other)

		def explainRuntimeDependency(self, other, path = []):
			path = path + [self]
			if other in self.configuredRuntimeRequires:
				return path

			found = None
			for label in self.configuredRuntimeRequires:
				found = label.explainRuntimeDependency(other, path)
				if found:
					break
			return found

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

			# purposes inherit the parent's source project by default
			if self.sourceProject and not otherLabel.sourceProject:
				otherLabel.setSourceProject(self.sourceProject)

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
			self.setSourceProject(configLabel.sourceProject)
			# self.copyBuildRequirementsFrom(configLabel)

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

			# "Normal" case:
			#	a label @Foo, requiredLabel @Bar
			#	 - we want to add all @Bar+something if its requirements are
			#	   a subset of @Foo's requirements
			#	 - question: should we do this for all +something flavors,
			#	   or just for those that have disposition maybe_merge?
			# "DONT" case
			#	a label @Foo+python, requiredLabel @Bar+typelib
			# Borderline cases
			#	a label @Foo+python, requiredLabel @Bar
			#	 - we may want to add @Bar+python
			#
			# For the time being, we just handle the "normal" case
			if self.parent is not None:
				return

			myClosure = order.downwardClosureFor(self).copy()
			availableFlavors = Classification.createLabelSet()
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
				eligibleFlavors = Classification.createLabelSet()
				for flavor in order.bottomUpTraversal(candidateFlavors):
					flavorBaseClosure = order.downwardClosureFor(flavor)
					if flavor in flavorBaseClosure:
						flavorBaseClosure.remove(flavor)

					if flavorBaseClosure.issubset(myClosure):
						if False:
							infomsg(f"{self} auto-selected {flavor} (disposition {flavor.disposition})")
						myClosure.update(order.downwardClosureFor(flavor))
						eligibleFlavors.add(flavor)

				if not eligibleFlavors:
					break

				for flavor in eligibleFlavors:
					self.addRuntimeDependency(flavor) 
				self.automaticRuntimeRequires.update(eligibleFlavors)

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

			existingLabel = self._globalPurposeLabels.get(name)
			if existingLabel is label:
				return

			if existingLabel is not None:
				raise Exception(f"{self}: attempt to redefine global purpose {name} as {label}")

			if name == 'devel':
				label.isAPI = True

			self._globalPurposeLabels[name] = label
			label.sourceProject = self

		def updateGlobalPurposeLabels(self, generalizedRequires, componentOrder):
			assert(self.type is Classification.TYPE_SOURCE)
			if not self._globalPurposeLabels:
				return

			# Update the label (eg CoreDevel) with all labels that are associated with
			# component Core, and which are not a component level label themselves.
			for name, label in self._globalPurposeLabels.items():

				for req in generalizedRequires:
					# should not happen:
					if req.componentLabel is None:
						continue

					# prevent loops
					if req is label:
						continue

					if label.canAccessDirectly(req, componentOrder) or req.isExported:
						label.runtimeRequires.add(req)

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
	def buildSolvingTree(classificationContext, builds, **kwargs):
		builder = SolvingTreeBuilder(classificationContext)
		return builder.buildTree(builds, **kwargs)

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
		return pkg.setLabel(label, labelReason or "no reason provided")

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

	class Options(object):
		def __init__(self):
			self.splitMultibuilds = set()

	class Scheme:
		def __init__(self):
			self._labels = {}
			self._nextLabelId = 0
			self._final = False

			self._defaultComponentOrder = None
			self._defaultBinaryOrder = None

			self._category = {}
			for type in Classification.VALID_TYPES:
				self._category[type] = Classification.LabelCategory(str(type))

			self.options = Classification.Options()

		@property
		def fingerprint(self):
			values = tuple(label.fingerprint for label in self.allLabels)
			return hash(values)

		def getAllLabelsWithType(self, type):
			return Classification.createLabelSet(filter(lambda label: label.type == type, self._labels.values()))

		@property
		def allBinaryLabels(self):
			return self.getAllLabelsWithType(Classification.TYPE_BINARY)

		@property
		def allAutoPurposes(self):
			return self.getAllLabelsWithType(Classification.TYPE_PURPOSE)

		@property
		def allAutoFlavors(self):
			return self.getAllLabelsWithType(Classification.TYPE_AUTOFLAVOR)

		@property
		def allBuildConfigs(self):
			return self.getAllLabelsWithType(Classification.TYPE_BUILDCONFIG)

		@property
		def allComponents(self):
			return self.getAllLabelsWithType(Classification.TYPE_SOURCE)

		def isFrozen(self, type):
			return self._category[type].frozen

		def freezeCategory(self, type):
			self._category[type].frozen = True

		@property
		def isFinal(self):
			return self._final

		def getLabel(self, name):
			return self._labels.get(name)

		def createLabel(self, name, type):
			label = self._labels.get(name)
			if label is None:
				if self.isFrozen(type):
					raise Exception(f"Refusing to create {type} label after this category has been declared final")

				label = Classification.Label(name, type, self._nextLabelId)
				self._labels[name] = label
				self._nextLabelId += 1
			elif label.type != type:
				raise Exception(f"Conflicting types for label {name}. Already have a label of type {label.type}, now asked to create {type}")
			return label

		def createFlavor(self, baseLabel, flavorName, sourceProject = None):
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
			label.setSourceProject(sourceProject or baseLabel.sourceProject)

			# ... and share their requirements ...
			label.copyRequirementsFrom(baseLabel)

			# @Foo+blah always requires @Foo for runtime
			if label.type is Classification.TYPE_BINARY:
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
			label.fromAutoFlavor = baseLabel.fromAutoFlavor

			# the new purpose label inherits the base label's gravity
			label.gravity = baseLabel.gravity

			baseLabel.addObjectPurpose(label)

			# Packages built for a specific purpose share the source project
			# of their base label ...
			label.setSourceProject(baseLabel.sourceProject)

			# ... and share their requirements ...
			label.copyRequirementsFrom(baseLabel)

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
			if type is Classification.TYPE_BUILDCONFIG:
				return self.resolveBuildConfigLabel(name)
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

		@profiling
		def createOrdering(self, labelType):
			if labelType not in (Classification.TYPE_BINARY, Classification.TYPE_SOURCE):
				raise Exception(f"Unable to create an ordering for {labelType} labels")

			good = True

			order = PartialOrder(Classification.domain, f"{labelType} label order")
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
			# Until the binary order has been frozen, we re-create every time
			# someone calls this function
			if self._defaultBinaryOrder is not None:
				return self._defaultBinaryOrder

			return self.createOrdering(Classification.TYPE_BINARY)

		def componentOrder(self):
			# Until the component order has been frozen, we re-create every time
			# someone calls this function
			if self._defaultComponentOrder is not None:
				return self._defaultComponentOrder

			return self.createOrdering(Classification.TYPE_SOURCE)

		def freezeComponentOrder(self):
			self.freezeCategory(Classification.TYPE_SOURCE)
			self.freezeCategory(Classification.TYPE_BUILDCONFIG)
			self.freezeCategory(Classification.TYPE_BUILDCONFIG_FLAVOR)

			self._defaultComponentOrder = self.componentOrder()

			return self._defaultComponentOrder

		def freezeBinaryOrder(self):
			self.freezeCategory(Classification.TYPE_BINARY)
			self.freezeCategory(Classification.TYPE_PURPOSE)
			self.freezeCategory(Classification.TYPE_AUTOFLAVOR)

			for componentLabel in self._defaultComponentOrder.bottomUpTraversal():
				self.updateReferencingLabels(componentLabel)

			self._defaultBinaryOrder = self.defaultOrder()
			return self._defaultBinaryOrder

		@profiling
		def finalize(self):
			def inheritSourceProject(label):
				if label.sourceProject is None:
					if label.parent:
						source = inheritSourceProject(label.parent)
						if source:
							label.setSourceProject(source)
				return label.sourceProject

			def inheritBuildConfigs(componentLabel):
				# build config X11/standard inherits from XXX/standard for all XXX components below X11
				# The same applies for all XXX/othername *unless* XXX/othername has been defined with
				# inheritable: false
				for lowerComponent in componentLabel.runtimeRequires:
					for lowerConfig in lowerComponent.flavors:
						if not lowerConfig.isInheritable:
							continue

						flavorName = lowerConfig.flavorName
						buildConfig = componentLabel.getBuildFlavor(flavorName)
						if buildConfig is None:
							buildConfig = self.createFlavor(componentLabel, flavorName)
						buildConfig.buildRequires.update(lowerConfig.buildRequires)
						buildConfig.runtimeRequires.update(lowerConfig.runtimeRequires)

			if self._final:
				raise Exception(f"Duplicate call to ClassificationScheme.finalize()")

			infomsg(f"Finalizing classification")

			for label in self.allBinaryLabels:
				if label.inheritAllFlavors:
					if len(label.configuredRuntimeRequires) != 1:
						raise Exception(f"{label} has inherit_all_flavors=yes but uses more than one runtime req. Currently not implemented")
					req = next(iter(label.configuredRuntimeRequires))
					for reqFlavor in req.flavors:
						flavor = self.resolveBuildFlavor(label, reqFlavor.flavorName)
						flavor.addRuntimeDependency(reqFlavor)
						# infomsg(f"  {flavor} -> {reqFlavor}")

			# Children of API labels are APIs too
			def detectAPI(label):
				if label.isAPI is None:
					if label.parent:
						label.isAPI = detectAPI(label.parent)
					else:
						label.isAPI = False
				return label.isAPI

			for label in self.allBinaryLabels:
				detectAPI(label)

			for label in self._labels.values():
				if label.sourceProject is None:
					inheritSourceProject(label)

				if label.type == Classification.TYPE_BINARY:
					if label.sourceProject is None:
						if label.disposition != Classification.DISPOSITION_IGNORE:
							raise Exception(f"Label {label}: no buildconfig specified")
					elif not label.sourceProject.defined:
						raise Exception(f"Label {label} references component {label.sourceProject}, but it's not defined anywhere")

					label.validateAPI()

			componentOrder = self.componentOrder()

			for componentLabel in componentOrder.bottomUpTraversal():
				componentLabel.finalizeExports()

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

			# Once more, we need a temporary ordering for binary labels
			topicOrder = self.createOrdering(Classification.TYPE_BINARY)

			from inversions import InversionBuilder
			inversionBuilder = InversionBuilder(self)

			globalPurposeLabelNames = set()
			for componentLabel in componentOrder.bottomUpTraversal():
				globalPurposeLabelNames.update(componentLabel.globalPurposeLabelNames)

				globalDevel = componentLabel.globalPurposeLabel('devel')
				if globalDevel is not None:
					globalDevel.runtimeRequires.update(componentLabel.buildRequires)

				inheritBuildConfigs(componentLabel)

				inversionBuilder.process(componentLabel)

			componentOrder = self.freezeComponentOrder()

			inversionMap = inversionBuilder.inversionMap
			for componentLabel in componentOrder.bottomUpTraversal():
				selfContained = inversionMap.getGoodComponentTopics(componentLabel)
				componentLabel.updateGlobalPurposeLabels(selfContained, componentOrder)

			self.inversionMap = inversionMap

			for name in globalPurposeLabelNames:
				label = self.getLabel(name)
				if label is None or label.type != Classification.TYPE_PURPOSE:
					raise Exception(f"A component defines a global named {name}, but there is no corresponding purpose label")

			# ugly... this should really be attached to the classificationScheme *instance*
			Classification.baseLabelsForSet = fastsets.Transform(Classification.domain, lambda label: label.baseLabel)

			self._final = True
			self.freezeBinaryOrder()

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
			if target._referencingLabels is not None:
				return target._referencingLabels
			return self.getReferencingLabelsSlow(target)

		def updateReferencingLabels(self, target):
			assert(self.isFrozen(Classification.TYPE_BINARY))

			# This creates a circular reference between Labels, but I don't care for now
			target._referencingLabels = self.getReferencingLabelsSlow(target)

		def getReferencingLabelsSlow(self, target):
			if target.type != Classification.TYPE_SOURCE:
				raise Exception(f"getReferencingLabels({target}): label type {target.type} not implemented")

			result = Classification.createLabelSet()
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

	class ClassificationContext:
		def __init__(self, worker, productArchitecture, classificationScheme, labelOrder, store):
			self.worker = worker
			self.productArchitecture = productArchitecture
			self.classificationScheme = classificationScheme
			self.labelOrder = labelOrder
			self.store = store

class ClassificationResult(object):
	class PackageMembership(object):
		def __init__(self, label):
			self.label = label
			self.packages = set()

		def track(self, pkg, reason):
			Classification.labelPackage(pkg, self.label, reason)
			self.packages.add(pkg)

	class BuildTracking(object):
		def __init__(self, label):
			self.label = label
			self.builds = set()
			self.frequencyCounter = FrequencyCounter(lambda rpm: rpm)

		def track(self, build):
			self.builds.add(build)

			if build.label is None:
				warnmsg(f"We should not be here - BuildTracking labels build {build} as {self.label}")
				build.label = self.label

			# There can be builds w/o any buildrequires. glibc:i686 is such a case.
			if build.buildRequires:
				self.frequencyCounter.addEvent(build.buildRequires)
				if len(build.buildRequires) < 50:
					infomsg(f"{build} has only {len(build.buildRequires)} build requires")

		@staticmethod
		def computeCommonBuildRequires(frequencyCounter, buildInfoList):
			# We're only interested in packages required by *all* builds
			spectrum = frequencyCounter.frequencyBands([100])
			band = spectrum.bands[0]

			commonBuildRequires = set(band.objects)
			if not commonBuildRequires:
				return None

			return commonBuildRequires

		def finalize(self, baseCommonBuildRequires = set()):
			commonBuildRequires = self.computeCommonBuildRequires(self.frequencyCounter, self.builds)
			if not commonBuildRequires:
				if self.builds:
					raise Exception(f"Something's wrong with {self.label}: no common build requirements")
				commonBuildRequires = set()

			for buildInfo in self.builds:
				# We can override a previous set of "common" build requirements if
				# we go from component Foo to buildconfig Foo/something
				if buildInfo.commonBuildRef is not None:
					assert(self.label.parent == buildInfo.commonBuildRef)

				buildInfo.buildRequires = buildInfo.buildRequires.difference(commonBuildRequires)
				buildInfo.commonBuildRef = self.label

			self.commonBuildRequires = commonBuildRequires
			self.incrementalBuildRequires = commonBuildRequires.difference(baseCommonBuildRequires)

			debugmsg(f"{self.label} has {len(self.commonBuildRequires)} common build requirements; {len(self.incrementalBuildRequires)} new")

	class ProjectMembership(BuildTracking):
		def __init__(self, label):
			super().__init__(label)
			self._buildConfigs = []
			self._standardBuildConfig = None

		def finalize(self, projectTable):
			baseCommonBuildRequires = set()
			for lowerComponentLabel in self.label.runtimeRequires:
				lowerProject = projectTable.get(lowerComponentLabel)
				if lowerProject is not None:
					baseCommonBuildRequires.update(lowerProject.commonBuildRequires)

			super().finalize(baseCommonBuildRequires)

			for buildMembership in self._buildConfigs:
				buildMembership.finalize(self.commonBuildRequires)

	class BuildConfigMembership(BuildTracking):
		def __init__(self, label, projectMembership):
			super().__init__(label)

			projectMembership._buildConfigs.append(self)
			if label.flavorName == 'standard':
				projectMembership._standardBuildConfig = self

		@property
		def requiredTopics(self):
			label = self.label
			return label.runtimeRequires.union(label.buildRequires)

	class BuildInfo(object):
		def __init__(self, name):
			self.name = name
			self.binaries = []
			self.sources = []
			self.buildRequires = []
			self.label = None
			self.buildConfig = None
			self.commonBuildRef = None

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
		self._effectiveRequirements = {}

		self.inversionMap = None

		self._buildConfigs = {}

	def enableBrokenDependencyTracking(self):
		if self._brokenDependencies is None:
			self._brokenDependencies = self.PackageRequirements()

	def packageMembership(self, label, create = True):
		m = self._packages.get(label)
		if m is None and create:
			m = self.PackageMembership(label)
			self._packages[label] = m
		return m

	def projectMembership(self, label):
		m = self._projects.get(label)
		if m is None:
			m = self.ProjectMembership(label)
			self._projects[label] = m
		return m

	def buildConfigMembership(self, label):
		m = self._buildConfigs.get(label)
		if m is None:
			m = self.BuildConfigMembership(label, self.projectMembership(label.parent))
			self._buildConfigs[label] = m
		return m

	def labelOnePackage(self, pkg, label, reason):
		self.packageMembership(label).track(pkg, reason)

	def getPackagesForLabel(self, label):
		m = self.packageMembership(label, create = False)
		if m is not None:
			return m.packages
		return []

	# FIXME: should we just use the buildSpec here and ditch the BuildInfo class?
	def labelOneBuild(self, name, label, binaries, sources, buildSpec = None):
		buildInfo = self.BuildInfo(name)
		self._builds.append(buildInfo)

		buildInfo.binaries += binaries
		buildInfo.sources += sources
		buildInfo.label = label
		if buildSpec and buildSpec.buildEnvironment:
			buildInfo.buildConfig = buildSpec.buildEnvironment.buildConfig

		buildInfo.buildRequires = set()
		for src in sources:
			if not src.resolvedRequires:
				infomsg(f"Missing build requirements for {src}")
				continue

			buildInfo.buildRequires.update(set(src.enumerateRequiredRpms()))

		if label is not None:
			self.projectMembership(label).track(buildInfo)

		if buildInfo.buildConfig is not None:
			self.buildConfigMembership(buildInfo.buildConfig).track(buildInfo)

		return buildInfo

	def compactBuildRequires(self):
		for componentLabel in self._componentOrder.bottomUpTraversal():
			project = self._projects.get(componentLabel)
			if project is None:
				infomsg(f"no builds for {componentLabel}")
				continue

			# Make sure that we have Foo/standard for every component Foo
			standardLabel = componentLabel.getBuildFlavor('standard')
			assert(standardLabel.type is Classification.TYPE_BUILDCONFIG)
			self.buildConfigMembership(standardLabel)

			project.finalize(self._projects)

	def getIncrementalBuildRequires(self, label):
		if label.type is Classification.TYPE_SOURCE:
			membership = self._projects.get(label)
		elif label.type is Classification.TYPE_BUILDCONFIG:
			membership = self._buildConfigs.get(label)
		else:
			membership = None

		if membership is None:
			return set()

		return membership.incrementalBuildRequires

	def getCommonBuildRequires(self, label):
		if label.type is Classification.TYPE_SOURCE:
			membership = self._projects.get(label)
		elif label.type is Classification.TYPE_BUILDCONFIG:
			membership = self._buildConfigs.get(label)
		else:
			membership = None

		if membership is None:
			return set()

		return membership.commonBuildRequires

	def addComponent(self, label):
		self._components.append(label)

	def addUnclassified(self, pkg, candidates):
		self._unclassified.append(self.UnclassifiedPackage(pkg, candidates))

	def enumeratePackages(self):
		for label in self._labelOrder.bottomUpTraversal():
			members = self.packageMembership(label).packages
			yield label, members

	def enumerateBuilds(self):
		for buildInfo in self._builds:
			yield buildInfo.label, buildInfo

	def enumerateComponents(self):
		for label in self._components:
			requires = label.runtimeRequires
			if self._componentOrder is not None:
				requires = self._componentOrder.maxima(requires)
			yield label, requires

	def enumerateBuildConfigs(self, componentLabel):
		project = self._projects.get(componentLabel)
		if project is None:
			return []

		for membership in project._buildConfigs:
			requires = membership.requiredTopics
			if project._standardBuildConfig is not None:
				requires.update(project._standardBuildConfig.requiredTopics)

			# DISABLED FOR NOW
			if self._componentOrder is not None and False:
				requires = self._labelOrder.maxima(requires)

			yield membership.label, requires

	def enumerateUnclassifiedPackages(self):
		for entry in self._unclassified:
			yield entry.pkg, entry.candidates

	def getBuildSpec(self, name):
		for b in self._builds:
			if b.name == name:
				return b

	# initially, we create the label tree with a maximum of edges
	# When reporting it in the output, we want to cut this down
	# to a reasonable complexity
	def getMinimalRuntimeRequirements(self, label):
		if not label.runtimeRequires:
			return Classification.createLabelSet()

		if label in self._effectiveRequirements:
			return self._effectiveRequirements[label]

		actualRequirements = self.collectActualRuntimeRequirements(label)
		if actualRequirements is None:
			# at least return something, even if it may be inconsistent
			actualRequirements = label.runtimeRequires

		effectiveRequirements = self.reduceRequirements(label, actualRequirements)

		if False:
			infomsg(f"Effective requirements for {label}: reduced from {len(label.runtimeRequires)} to {len(effectiveRequirements)} labels")
			if len(label.runtimeRequires) < 10 and len(effectiveRequirements) < 10:
				infomsg(f"  orig:    {' '.join(map(str, label.runtimeRequires))}")
				infomsg(f"  reduced: {' '.join(map(str, effectiveRequirements))}")

		self._effectiveRequirements[label] = effectiveRequirements

		return effectiveRequirements

	def collectActualRuntimeRequirements(self, label):
		actualRequirements = Classification.createLabelSet()
		failed = False

		# Always include the labels that were configured by the yaml file.
		# This is important for computing build dependencies
		actualRequirements.update(label.configuredRuntimeRequires)

		fullRequirements = self._labelOrder.downwardClosureForSet(label.runtimeRequires)

		for reqLabel in fullRequirements:
			if self.packageMembership(reqLabel).packages:
				actualRequirements.add(reqLabel)

		members = self.packageMembership(label).packages
		for pkg in members:
			if pkg.resolvedRequires is None:
				infomsg(f"Unable to compute minimal requirements for {label}: requirements for {pkg} have not been resolved")
				failed = True
				continue

			for required in pkg.enumerateRequiredRpms():
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

			for required in pkg.enumerateRequiredRpms():
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

	def getInversions(self, topic):
		if self.inversionMap is None:
			return None

		inversions = self.inversionMap.get(topic)
		if inversions is None:
			return None

		inversions = inversions.intersection(self.getMinimalRuntimeRequirements(topic))
		return inversions

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
		self.buildConfigMatcher = ParallelStringMatcher()

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

	def addBuildConfigMatch(self, pattern, priority, label):
		m = self.Match(pattern, 'package', priority, label)
		self.buildConfigMatcher.add(pattern, m)

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
				infomsg(f"{name}: {m.label} matched by {m.type} filter {m.pattern}; precedence {m.precedence}")
				infomsg(f"   {len(matches)} lower priority matches were ignored:")
				for other in matches:
					infomsg(f"      prec {other.precedence} {other.label} {other.type} {other.pattern}")
		else:
			m = next(iter(matches))

			if trace:
				infomsg(f"{name}: {m.label} matched by {m.type} filter {m.pattern}")

		return PackageFilter.Verdict(m.label, f"{m.type} filter {m.pattern}")

class StringMatchBuilder(object):
	def __init__(self, classificationScheme, stringMatcher, label, priority = None):
		self.classificationScheme = classificationScheme
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

	def addBuildConfigFilter(self, name):
		pattern, priority, label = self.processPattern(name)
		self.stringMatcher.addBuildConfigMatch(pattern, priority, label)

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
					purposeName = argValue
					classificationScheme = self.classificationScheme
					purposeDef = classificationScheme.getLabel(purposeName)
					if purposeDef is None or purposeDef.type is not Classification.TYPE_PURPOSE:
						raise Exception(f"{value} {param} specifies invalid purpose")

					purposeLabel = label.getObjectPurpose(purposeName)

					# if the purpose is devel, check if we have an API label specified
					if purposeLabel is None and purposeName == 'devel':
						purposeLabel = label.correspondingAPI
						# infomsg(f"{value} purpose=devel, {label} has API {purposeLabel}")

					if purposeLabel is None:
						if purposeDef.disposition == Classification.DISPOSITION_COMPONENT_WIDE:
							componentLabel = label.componentLabel
							if componentLabel is not None:
								purposeLabel = componentLabel.globalPurposeLabel(purposeName)
						else:
							purposeLabel = classificationScheme.createPurpose(label, purposeName, purposeDef)

					if purposeLabel is None:
						raise Exception(f"Cannot add filter for \"{value}\" - unknown purpose {purposeName} in label {label}")
					label = purposeLabel
				else:
					raise Exception(f"Unknown match parameter {param} in {self.filterSet} expression \"{value}\" for label {label}");

		return (value, priority, label)

class FilterTemplate:
	def __init__(self, name, key, document):
		self.name = name
		if type(key) is str:
			self._keys = [key]
		else:
			assert(type(key) is list)
			self._keys = key
		self._doc = document

	class Instance:
		def __init__(self, template, globalVariables, values):
			self.template = template
			self.globalVariables = globalVariables
			self._vars = dict(zip(template._keys, values))

		def expand(self):
			return self.expandData(self.template._doc)

		def expandData(self, data):
			t  = type(data)
			if t is str:
				for key, value in self._vars.items():
					data = data.replace(key, value)
				data = self.globalVariables.expand(data)
				return data
			if t is list:
				return list(map(self.expandData, data))
			if t is dict:
				return {name: self.expandData(value) for (name, value) in data.items()}
			if t in (bool, int, float) or data is None:
				return data

			raise Exception(f"data type {t} not yet implemented")

	def instantiate(self, globalVariables, args):
		if len(self._keys) != len(args):
			raise Exception(f"Unable to instantiate template {self.name}({', '.join(args)}): expected {len(self._keys)} arguments")

		return self.Instance(self, globalVariables, args)

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
		self._templates = {}
		self.expander = None

		self.stringMatcher = PackageLabelling()

		timer = ExecTimer()
		self.load(filename)
		infomsg(f"Loaded filter definition from {filename}: {timer} elapsed")

	def load(self, filename):
		with open(filename) as f:
			data = yaml.full_load(f)

		defines = data.get('defines')
		if defines is not None:
			self.expander = VariableExpander(defines)

		options = data.get('options')
		if options is not None:
			copts = self.classificationScheme.options
			copts.splitMultiBuilds = set(self.getYamlList(options, 'split_multibuilds', 'options'))

		# Parse autoflavors *before* everything else so that we can populate
		# newly created flavors from default settings.
		for gd, template in self.expandYamlObjectList(data, 'autoflavors'):
			self.parseGroup(Classification.TYPE_AUTOFLAVOR, gd, template)

		for gd, template in self.expandYamlObjectList(data, 'purposes'):
			self.parseGroup(Classification.TYPE_PURPOSE, gd, template)

		for gd, template in self.expandYamlObjectList(data, 'buildconfig_flavors'):
			self.parseGroup(Classification.TYPE_BUILDCONFIG_FLAVOR, gd, template)

		includes = data.get('include')
		if includes is not None:
			for includeName in includes:
				self.includeFile(includeName, filename)

		self.loadPartial(data)

		self.finalize()
		self.classificationScheme.finalize()

	def includeFile(self, includeFile, referencingFile):
		import os

		includeBaseDir = os.path.dirname(referencingFile)
		if includeBaseDir:
			includeFile = os.path.join(includeBaseDir, includeFile)

		with open(includeFile) as f:
			data = yaml.full_load(f)

		if not data:
			errormsg(f"{includeFile} seems to be empty")
			return

		try:
			self.loadPartial(data)
		except Exception as e:
			raise Exception(f"{includeFile}: {e}")

	def loadPartial(self, data):
		for gd in data.get('templates') or []:
			self.parseTemplate(gd)

		for gd, template in self.expandYamlObjectList(data, 'components'):
			self.parseGroup(Classification.TYPE_SOURCE, gd, template)

		for gd, template in self.expandYamlObjectList(data, 'build_configs'):
			self.parseGroup(Classification.TYPE_BUILDCONFIG, gd, template)

		for gd, template in self.expandYamlObjectList(data, 'groups'):
			self.parseGroup(Classification.TYPE_BINARY, gd, template)

	@profiling
	def finalize(self):
		def validateDependencies(label, dependencies):
			for req in dependencies:
				reqBaseLabel = req.baseLabel
				if not reqBaseLabel.defined and not reqBaseLabel.isPurpose:
					raise Exception(f"filter configuration issue: group {label} requires {req}, which is not defined anywhere")

		def validateCompatibility(label, closure):
			if label.parent is not None:
				return

			if label.compatibility in (None, "none"):
				return

			lowerCompat = set(filter(lambda c: (c and c != "none"), (lower.compatibility for lower in closure)))
			if not lowerCompat:
				return

			if len(lowerCompat) > 1:
				errormsg(f"{label} requires labels with conflicting compatibility")

				topicOrder = self.classificationScheme.defaultOrder()
				for compat in lowerCompat:
					errormsg(f"{compat}")
					for lower in closure:
						if lower.compatibility == compat:
							path = topicOrder.findPath(lower, label)
							if path is None:
								errormsg(f"    - {lower} (unclear origin)")
							else:
								errormsg(f"    - {lower} via {' -> '.join(map(str, path))}")
				raise Exception(f"Invalid label {label} combines labels with conflicting compatibility ({' '.join(map(str, lowerCompat))})")

			compat = lowerCompat.pop()
			if compat != label.compatibility:
				raise Exception(f"Invalid label {label} requires labels with conflicting compatibility ({label.compatibility} vs {compat})")

		self.stringMatcher.finalize()

		classificationScheme = self.classificationScheme

		for label in classificationScheme.allAutoFlavors:
			validateDependencies(label, label.buildRequires)

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
		for label in classificationScheme.allBinaryLabels:
			if label.parent is not None:
				continue

			for autoFlavor in self.classificationScheme.allAutoFlavors:
				if autoFlavor.disposition is Classification.DISPOSITION_SEPARATE:
					self.maybeInstantiateAutoFlavor(label, autoFlavor)

		# Loop over all @Foo labels and look for auto flavors with disposition maybe_merge, such as python
		# If @Foo requires everything that the auto flavor requires (in the case of python, this would
		# be @PythonCore), then mark the auto flavor for merging. Otherwise, create a separate
		# build flavor @Foo+python
		preliminaryOrder = self.classificationScheme.createOrdering(Classification.TYPE_BINARY)
		for label in classificationScheme.allBinaryLabels:
			if label.parent is not None:
				continue

			if label.type is not Classification.TYPE_BINARY:
				continue

			baseLabel = label

			# get the closure of all requirements of @Foo
			baseDependencies = preliminaryOrder.downwardClosureFor(baseLabel)

			validateCompatibility(baseLabel, baseDependencies)

			for autoFlavor in self.classificationScheme.allAutoFlavors:
				if autoFlavor.disposition != Classification.DISPOSITION_MAYBE_MERGE:
					continue

				if autoFlavor.runtimeRequires.issubset(baseDependencies):
					flavor = baseLabel.getBuildFlavor(autoFlavor.name)
					if flavor is not None:
						infomsg(f"{baseLabel}+{autoFlavor} packages could be merged into {baseLabel}, but {flavor} exists")
					else:
						# infomsg(f"{baseLabel}+{autoFlavor} packages will be merged into {baseLabel}")
						baseLabel.addMergeableFlavor(autoFlavor)
				else:
					self.maybeInstantiateAutoFlavor(baseLabel, autoFlavor)

		for groupLabel in classificationScheme.allBinaryLabels:
			if groupLabel.type is Classification.TYPE_BINARY and not groupLabel.isPurpose and not groupLabel.isComponentLevel:
				for purposeDef in self.classificationScheme.allAutoPurposes:
					if purposeDef.disposition == Classification.DISPOSITION_COMPONENT_WIDE:
						continue

					self.makePurposeLabel(groupLabel, purposeDef.name)

		for label in classificationScheme.allBinaryLabels:
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

	def resolveLabelReference(self, name, labelType = Classification.TYPE_BINARY):
		baseLabelType, baseName, flavorName, purposeName = Classification.parseLabel(labelType, name)

		newLabel = self.classificationScheme.createLabel(baseName, baseLabelType)
		if flavorName is not None:
			newLabel = self.makeFlavorLabel(newLabel, flavorName)

		if purposeName is not None:
			newLabel = self.makePurposeLabel(newLabel, purposeName)

		return newLabel

	# autoFlavor is a Label
	def maybeInstantiateAutoFlavor(self, baseLabel, autoFlavor):
		if baseLabel.isCompatibleWithAutoFlavor(autoFlavor):
			self.instantiateAutoFlavor(baseLabel, autoFlavor)

	# autoFlavor is a Label
	def instantiateAutoFlavor(self, baseLabel, autoFlavor):
		flavor = self.makeFlavorLabel(baseLabel, autoFlavor.name)
		flavor.fromAutoFlavor = autoFlavor

		# even if the group existed already, at this point we need to copy any runtime
		# requirements specified for the auto flavor
		flavor.copyRequirementsFrom(autoFlavor)

		return flavor

	def makeFlavorLabel(self, baseLabel, flavorName):
		flavor = baseLabel.getBuildFlavor(flavorName)
		if flavor is not None:
			return flavor

		if baseLabel.type == Classification.TYPE_BINARY:
			flavor = self.createBinaryFlavor(baseLabel, flavorName)
		elif baseLabel.type == Classification.TYPE_SOURCE:
			flavor = self.createBuildConfigFlavor(baseLabel, flavorName)
		else:
			raise Exception(f"Don't know how to create flavor {flavorName} for {baseLabel.type} label {baseLabel}")

		return flavor

	def makePurposeLabel(self, baseLabel, purposeName):
		purpose = baseLabel.getObjectPurpose(purposeName)
		if purpose is None:
			purpose = self.createObjectPurpose(baseLabel, purposeName)
		return purpose

	def createBinaryFlavor(self, baseLabel, flavorName):
		label = self.classificationScheme.createFlavor(baseLabel, flavorName)

		flavorDef = self.getGroupLabelNoFail(flavorName, Classification.TYPE_AUTOFLAVOR)
		if flavorDef is not None:
			label.autoSelect = flavorDef.autoSelect

		return label

	def createBuildConfigFlavor(self, baseLabel, flavorName):
		label = self.classificationScheme.createFlavor(baseLabel, flavorName)

		# For the time being, make all buildconfigs auto-selectable.
		# Probably a useless gesture.
		label.autoSelect = True

		return label

	def createObjectPurpose(self, baseLabel, purposeName):
		purposeDef = self.getObjectPurposeDefinition(purposeName)
		if purposeDef is None:
			raise Exception(f"Undefined purpose {purposeName} in definition of {baseLabel}: you must define {purposeName} globally first")

		label = baseLabel.getObjectPurpose(purposeName)
		if label is None:
			label = self.classificationScheme.createPurpose(baseLabel, purposeName, template = purposeDef)

		# copy requirements from purposeDef
		label.copyRequirementsFrom(purposeDef)

		return label

	def getGroupLabel(self, name, type):
		label = self.classificationScheme.getLabel(name)
		if label is not None:
			if label.type != type:
				raise Exception(f"Group {name} does not match expected type (has {label.type}; expected {type})")
		return label

	def getGroupLabelNoFail(self, name, type):
		label = self.classificationScheme.getLabel(name)
		if label is not None:
			if label.type != type:
				label = None
		return label

	def getObjectPurposeDefinition(self, name):
		label = self.classificationScheme.getLabel(name)
		if label is not None and label.type is not Classification.TYPE_PURPOSE:
			label = None
		return label

	def parseTemplate(self, gd):
		template = FilterTemplate(gd['name'], gd['substitute'], gd['document'])
		if template.name in self._templates:
			raise Exception(f"Duplicate definition of template {template.name}")
		self._templates[template.name] = template

	def instantiateTemplate(self, reference):
		words = reference.split(':')
		if len(words) <= 1:
			raise Exception(f"Invalid template reference {reference}: no arguments provided")

		templateName = words.pop(0)
		template = self._templates.get(templateName)
		if template is None:
			raise Exception(f"Unknown template name in template instantiation {reference}")

		return template.instantiate(self.expander, words)

	def parseGroup(self, groupType, gd, template):
		groupName = gd['name']

		label = self.resolveLabelReference(groupName, groupType)
		self.processGroupDefinition(label, gd, template)

	def parseBuildFlavor(self, baseLabel, gd):
		flavorName = gd['name']
		if self.getObjectPurposeDefinition(flavorName):
			raise Exception(f"Invalid build flavor name {flavorName} in definition of {baseLabel}: already defined as an object purpose")

		flavor = self.makeFlavorLabel(baseLabel, flavorName)
		self.processGroupDefinition(flavor, gd)

	def parseObjectPurpose(self, baseLabel, gd):
		purposeName = gd['name']
		if not self.getObjectPurposeDefinition(purposeName):
			raise Exception(f"Undefined purpose {purposeName} in definition of {baseLabel}: you must define {purposeName} globally first")

		purpose = self.makePurposeLabel(baseLabel, purposeName)
		self.processGroupDefinition(purpose, gd)

	VALID_GROUP_FIELDS = set((
		'name',
		'description',
		'note',
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
		'compatibility',
		'disposition',
		'autoselect',
		'defaultlabel',
		'defaultlabels',
		'feature',
		'globals',
		'api',
		'inheritable',
		'is_api',
		'inherit_all_flavors',
	))

	# FIXME: reduce use of `group' in this function
	def processGroupDefinition(self, groupLabel, gd, template = None):
		def getBoolean(gd, tag):
			value = gd.get(tag)
			if value is not None and type(value) is not bool:
				raise Exception(f"{groupLabel}: bad value {tag}={value} (expected boolean value not {type(value)})")
			return value

		def getString(gd, tag):
			value = gd.get(tag)
			if value is not None and type(value) is not str:
				raise Exception(f"{groupLabel}: bad value {tag}={value} (expected string value not {type(value)})")
			return value

		if groupLabel.defined:
			raise Exception(f"Duplicate definition of group \"{groupLabel}\" in filter yaml")
		groupLabel.defined = True

		if template is not None:
			groupLabel.instanceOfTemplate = template.name

		for field in gd.keys():
			if field not in self.VALID_GROUP_FIELDS:
				raise Exception(f"Invalid field {field} in definition of group {groupLabel}")

		groupLabel.description = gd.get('description')

		# ignore any notes
		# dropit = gd.get('note')

		name = gd.get('sourceproject')
		if name is not None:
			sourceProject = self.resolveLabelReference(name, Classification.TYPE_SOURCE)
			groupLabel.setSourceProject(sourceProject)

		value = gd.get('disposition')
		if value is not None:
			if value == 'ignore' and groupLabel.type == Classification.TYPE_BINARY:
				# we allow regular labels to be marked as "ignore", which helps us hide problematic
				# packages like patterns-*
				pass
			elif value not in ('separate', 'merge', 'ignore', 'maybe_merge', 'component_wide') or groupLabel.type not in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
				raise Exception(f"Invalid disposition={value} in definition of {groupLabel.type} group {groupLabel}")
			groupLabel.disposition = value

		value = getBoolean(gd, 'autoselect')
		if value is not None:
			groupLabel.autoSelect = value

		value = getBoolean(gd, 'feature')
		if value is not None:
			groupLabel.isFeature = value

		value = getBoolean(gd, 'inheritable')
		if value is not None:
			groupLabel.isInheritable = value

		value = getBoolean(gd, 'is_api')
		if value is not None:
			groupLabel.isAPI = value

		value = getBoolean(gd, 'inherit_all_flavors')
		if value is not None:
			groupLabel.inheritAllFlavors = value

		value = gd.get('defaultlabel')
		if value is not None:
			if groupLabel.type != Classification.TYPE_AUTOFLAVOR:
				raise Exception(f"Error: defaultlabel is not valid for {groupLabel.type} labels")
			groupLabel.preferredLabels.insert(0, value)

		nameList = self.getYamlList(gd, 'defaultlabels', groupLabel)
		for name in nameList:
			if groupLabel.type != Classification.TYPE_AUTOFLAVOR:
				raise Exception(f"Error: defaultlabels is not valid for {groupLabel.type} labels")
			if type(name) != str:
				raise Exception(f"Error: unexpected {type(name)} in list of default labels")
			groupLabel.preferredLabels.append(name)

		priority = gd.get('priority')
		if priority is not None:
			assert(type(priority) == int)

		# still needed?
		gravity = gd.get('gravity')
		if gravity is not None:
			assert(type(gravity) == int)
			groupLabel.gravity = gravity

			# we may have defined labels out of order; make sure subordinate purpose labels
			# inherit the gravity value
			for purpose in groupLabel.objectPurposes:
				purpose.gravity = gravity

		if groupLabel:
			nameList = self.getYamlList(gd, 'requires', groupLabel)
			for name in nameList:
				if groupLabel.type is Classification.TYPE_SOURCE:
					labelType = groupLabel.type
				else:
					labelType = Classification.TYPE_BINARY

				if type(name) is not str:
					raise Exception(f"{groupLabel}: invalid item in requires list - expected string not {name}")

				referencedLabel = self.resolveLabelReference(name, labelType)
				groupLabel.configureRuntimeDependency(referencedLabel)

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
			nameList = self.getYamlList(gd, 'augments', groupLabel)
			for name in nameList:
				referencedLabel = self.resolveLabelReference(name)
				groupLabel.configureRuntimeAugmentation(referencedLabel)

			nameList = self.getYamlList(gd, 'buildrequires', groupLabel)
			for name in nameList:
				referencedLabel = self.resolveLabelReference(name)
				groupLabel.configureBuildDependency(referencedLabel)

			nameList = self.getYamlList(gd, 'imports', groupLabel)
			for name in nameList:
				referencedLabel = self.resolveLabelReference(name)
				groupLabel.addImport(referencedLabel)

			nameList = self.getYamlList(gd, 'exports', groupLabel)
			for name in nameList:
				referencedLabel = self.resolveLabelReference(name)
				groupLabel.addExport(referencedLabel)

		# The yaml file may specify per-group priorities for filters, but there is just
		# one global set of filters. Rather than passing the group and priority argument
		# into each add*Filter function, create a Builder object that does this transparently.
		filterSetBuilder = StringMatchBuilder(self.classificationScheme, self.stringMatcher, groupLabel, priority)

		nameList = self.getYamlList(gd, 'products', groupLabel)
		for name in nameList:
			raise Exception(f"package filter 'products' no longer supported")

		# Specifying a packagesuffix "foo" does the same thing as specifying
		# a package pattern "*-foo", except that the suffix is recorded in
		# the label to aid later placement.
		# Only makes sense with purpose labels right now
		nameList = self.getYamlList(gd, 'packagesuffixes', groupLabel)
		for name in nameList:
			groupLabel.packageSuffixes.append(name)

			name = f"*-{name}"
			filterSetBuilder.addBinaryPackageFilter(name)
			filterSetBuilder.addSourcePackageFilter(name)

		# note, we need to set any API before we process any package lists, so purpose=devel
		# annotations do the right thing
		apiName = getString(gd, 'api')
		if apiName:
			api = self.resolveLabelReference(apiName)
			groupLabel.setAPI(api)

		nameList = self.getYamlList(gd, 'packages', groupLabel)
		if nameList:
			if groupLabel.type is Classification.TYPE_BUILDCONFIG:
				# add names to buildconfig matching
				for name in nameList:
					filterSetBuilder.addBuildConfigFilter(name)

				# a buildconfig label that matches specific packages
				# cannot be inherited
				if groupLabel.isInheritable:
					infomsg(f"Changing {groupLabel} to non-inheritable because it specifies a packages list")
					groupLabel.isInheritable = False
			else:
				if groupLabel.parent is not None:
					raise Exception(f"{groupLabel}: packages list only valid in base labels and buildconfigs")
				for name in nameList:
					filterSetBuilder.addOBSPackageFilter(name)

		nameList = self.getYamlList(gd, 'sources', groupLabel)
		for name in nameList:
			filterSetBuilder.addSourcePackageFilter(name)

		nameList = self.getYamlList(gd, 'binaries', groupLabel)
		for name in nameList:
			filterSetBuilder.addBinaryPackageFilter(name)

		nameList = self.getYamlList(gd, 'rpmGroups', groupLabel)
		for name in nameList:
			raise Exception(f"package filter 'rpmGroups' no longer supported")

		flavors = self.getYamlList(gd, 'buildflavors', groupLabel)
		for fd in flavors:
			self.parseBuildFlavor(groupLabel, fd)

		purposes = self.getYamlList(gd, 'purposes', groupLabel)
		for fd in purposes:
			self.parseObjectPurpose(groupLabel, fd)

		globals = gd.get('globals')
		if globals is not None:
			if groupLabel.type != Classification.TYPE_SOURCE:
				raise Exception(f"You cannot specify globals in a {groupLabel.type} group definition")

			for purposeName, labelName in globals.items():
				# print(f"  {groupLabel}: {purposeName} -> {labelName}")
				purposeLabel = self.classificationScheme.createLabel(labelName, Classification.TYPE_BINARY)
				purposeLabel.isComponentLevel = True
				groupLabel.setGlobalPurposeLabel(purposeName, purposeLabel)

		groupLabel.compatibility = getString(gd, 'compatibility')

	def variableExpansion(self, data):
		if not self.expander or data is None:
			return data

		dataType = type(data)
		if dataType in (int, bool, float):
			return data
		if dataType is str:
			return self.expander.expand(data)
		if dataType is dict:
			return dict((self.expander.expand(key), self.variableExpansion(value)) for (key, value) in data.items())
		if dataType is list:
			return list(map(self.variableExpansion, data))

		raise Exception(f"Unexpected YAML data {dataType} in variableExpansion")

	def expandYamlObjectList(self, data, name):
		objectList = data.get(name)
		if objectList is None:
			return

		objectList = self.variableExpansion(objectList)

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
			raise Exception(f"In definition of {context}, {name} should be a list not a {type(value)}")

		return value
