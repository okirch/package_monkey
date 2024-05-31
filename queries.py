from filter import Classification, PackageFilter
from util import ANSITreeFormatter
from util import errormsg, infomsg

class QueryContext(object):
	QUASI_INFINITE = 4242 

	def __init__(self, application):
		self.application = application
		classificationScheme = Classification.Scheme()
		classification = application.loadClassification(classificationScheme)

		self._store = None
		self._model = None
		self.classificationScheme = classificationScheme
		self.classification = classification
		self.labelOrder = classificationScheme.defaultOrder()
		self.componentOrder = classificationScheme.componentOrder()
		self.inversionMap = classification.inversionMap
		self._allAPIs = self.getAPIs()

		self._rpmToBuildMap = None
		self._rpmToLabelMap = None

		self.verbosityLevel = 1
		if application.opts.terse:
			self.verbosityLevel = 0
		if application.opts.verbose:
			self.verbosityLevel = 2

	def connectDatabase(self):
		return self.application.loadBackingStore(readonly = True,
					dependencyTreeLookups = True,
					sourceLookups = True)

	def enumerateLabelsForQuery(self, requestedNames):
		if not requestedNames:
			for component in self.componentOrder.bottomUpTraversal():
				yield component
		else:
			for name in requestedNames:
				if name.startswith('='):
					for componentLabel in self.enumerateProjectComponents(name[1:]):
						yield componentLabel
					continue

				yield self.getLabel('component or topic', name)

	def enumerateProjectComponents(self, name):
		if self._model is None:
			self._model = self.application.loadModelMapping()

		projectDefinition = self._model.getProject(name)
		if projectDefinition is None:
			raise Exception(f"No project named \"{name}\" in model definition")

		for labelName in projectDefinition.componentNames:
			yield self.getLabel(f"project {name} component", labelName, Classification.TYPE_SOURCE)

	def getAPIs(self, componentList = None):
		if componentList is None:
			componentList = list(self.classificationScheme.allComponents)

		result = Classification.createLabelSet()
		for component in componentList:
			result.update(component.exports)
		return result


	def getLabel(self, desc, name, expectedType = None):
		result = self.classificationScheme.getLabel(name)
		if result is None:
			raise Exception(f"Unknown {desc} {name}")
		if expectedType is not None and result.type != expectedType:
			raise Exception(f"Incompatible {desc} {name} - defined as {result.type} label, but expected {expectedType}")
		return result

	@property
	def allBinaryLabels(self):
		return self.classificationScheme.allBinaryLabels

	def getLabelsForComponent(self, component):
		return self.classificationScheme.getReferencingLabels(component)

	def getPackagesForLabel(self, label, fromDB = False):
		if not fromDB:
			return self.classification.getPackagesForLabel(label)

		result = []
		store = self.connectDatabase()
		for orpm in self.classification.getPackagesForLabel(label):
			if orpm.isSynthetic:
				continue

			# Retrieve rpm dependencies from the DB
			rpm = store.recoverLatestPackageByName(orpm.name)

			if rpm is None:
				raise Exception(f"label {label} references {orpm} which is not in the DB")

			result.append(rpm)

		return result

	def getPackageCountForLabel(self, label):
		packages = self.getPackagesForLabel(label)
		if packages is None:
			return 0
		return len(packages)

	def getBuildForPackage(self, queryRpm):
		if self._rpmToBuildMap is None:
			self._rpmToBuildMap = {}
			for label, buildSpec in self.classification.enumerateBuilds():
				for rpm in buildSpec.binaries:
					self._rpmToBuildMap[rpm.shortname] = buildSpec

		return self._rpmToBuildMap.get(queryRpm.shortname)

	def getLabelForPackage(self, queryRpm):
		if self._rpmToLabelMap is None:
			self._rpmToLabelMap = {}
			for label, members in self.classification.enumeratePackages():
				for rpm in members:
					self._rpmToLabelMap[rpm.shortname] = label

		return self._rpmToLabelMap.get(queryRpm.shortname)

	def getSiblingsForPackage(self, queryRpm):
		obsBuild = self.getBuildForPackage(queryRpm)
		if obsBuild is None:
			return []
		return obsBuild.binaries

	def enumerateBuildRequirements(self, obsBuild):
		if not obsBuild.sources:
			print(f"Warning: no source package for {obsBuild} (could be an import)")

		for srpm in obsBuild.sources:
			for rpm in srpm.enumerateRequiredRpms():
				label = self.getLabelForPackage(rpm)
				if label is None:
					print(f"Error: {obsBuild} requires {rpm} for building, but this package has not been labelled yet")
					continue

				yield rpm, label

	def bottomUpTraversal(self, *args, **kwargs):
		return iter(self.labelOrder.bottomUpTraversal(*args, **kwargs))

	def topDownTraversal(self, *args, **kwargs):
		return iter(self.labelOrder.topDownTraversal(*args, **kwargs))

	def getBuildsForComponent(self, component):
		for label, buildInfo in self.classification.enumerateBuilds():
                        if label is component:
                                yield buildInfo

	# FIXME move
	def getUnclassifiedForComponent(self, component):
		for rpm, candidates in self.classification.enumerateUnclassifiedPackages():
			if candidates is None:
				yield rpm, self.QUASI_INFINITE
			elif component in candidates:
				yield rpm, len(candidates) - 1

	# FIXME move
	def getInversionsForComponent(self, component):
		if self.inversionMap is None:
			return []

		for topic in self.getLabelsForComponent(component):
			if self.getPackageCountForLabel(topic) == 0:
				continue

			inversions = self.inversionMap.get(topic)
			if inversions:
				yield topic, inversions

	@staticmethod
	def getAPIsForComponentList(componentList = None):
		if componentList is None:
			componentList = list(classificationScheme.allComponents)

		result = Classification.createLabelSet()
		for component in componentList:
			result.update(component.exports)
		return result

	@staticmethod
	def reduceLabelSet(labelSet):
		result = []
		seen = set()
		for label in sorted(labelSet, key = str):
			seen.add(label)
			if label.parent not in seen:
				result.append(label)
		return result

class RequirementsReportBase(object):
	class PackageRequirements:
		def __init__(self, rpm, label):
			self.rpm = rpm
			self.label = label
			self.children = []

		def __str__(self):
			return self.rpm.name

		def format(self):
			tf = ANSITreeFormatter()
			self.formatWork(tf.root, set())
			return tf

		def formatWork(self, tfParent, seen):
			# If we've seen an rpm already, add the rpm itself but do
			# not recurse into it
			tfNode = tfParent.add(self)

			if self in seen:
				return
			seen.add(self)

			for child in self.children:
				child.formatWork(tfNode, seen)

	def __init__(self, context, inspectLabelSet, verbosityLevel = 0):
		self.context = context
		self.verbosityLevel = verbosityLevel
		self.inspectLabelSet = inspectLabelSet
		self._dependencies = {}

	def addPackageRequirements(self, predicate, rpm):
		requirements = self._dependencies.get(rpm.name)
		if requirements is None:
			label = predicate.getLabel(rpm)
			requirements = self.PackageRequirements(rpm, label)
			self._dependencies[rpm.name] = requirements

			for req in filter(predicate, rpm.enumerateRequiredRpms()):
				if req is rpm:
					continue
				# I'm too lazy right now to use the ResolverHints to transform
				# dependencies:
				if req.name in ('systemd', 'udev'):
					continue

				requirements.children.append(self.addPackageRequirements(predicate, req))
		return requirements

	def requirementsForRpm(self, rpm):
		return self._dependencies.get(rpm.name, None)

class RequirementsReport(RequirementsReportBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.requiringLabels = Classification.createLabelSet()
		self._paths = {}
		self._packages = {}
		self._subtrees = {}
		self.dropRequirementsFrom = Classification.createLabelSet()

	def add(self, topic, relevantSubtree = None):
		self.requiringLabels.add(topic)

		self._subtrees[topic] = relevantSubtree

	def addPath(self, topic, path):
		topicPaths = self._paths.get(topic)
		if topicPaths is None:
			topicPaths = []
			self._paths[topic] = topicPaths
		topicPaths.append(path)

	def addPackage(self, topic, rpm, predicate = None):
		packageSet = self._packages.get(topic)
		if packageSet is None:
			packageSet = set()
			self._packages[topic] = packageSet

		packageSet.add(rpm)

		if predicate is not None:
			self.addPackageRequirements(predicate, rpm)

	def adviseDropRequirements(self, label, explicitRequirement):
		self.dropRequirementsFrom.add(label)

	def __bool__(self):
		return bool(self.requiringLabels)

	@property
	def allLabels(self):
		return sorted(self.requiringLabels, key = str)

	@property
	def minimalLabels(self):
		return self.context.reduceLabelSet(self.requiringLabels)

	def packagesForLabel(self, topic):
		return self._packages.get(topic, [])

	def pathsForLabel(self, topic):
		return self._paths.get(topic, [])

	def subtreeForLabel(self, topic):
		subtree = self._subtrees.get(topic)
		if subtree is None:
			return None

		topicOrder = self.context.labelOrder
		subtree = topicOrder.convexClosureForSet(subtree)
		return topicOrder.asTreeFormatter(subtree, topDown = True)

class BuildRequirementsReport(RequirementsReportBase):
	class BuildRequirements(object):
		def __init__(self, requiredRpms = None, requiredTopics = None):
			if requiredRpms is None:
				requiredRpms = set()
			self.requiredRpms = requiredRpms

			if requiredTopics is None:
				requiredTopics = Classification.createLabelSet()
			self.requiredTopics = requiredTopics

		def intersection_update(self, requiredRpms, requiredTopics):
			self.requiredRpms.intersection_update(requiredRpms)
			self.requiredTopics.intersection_update(requiredTopics)

		def update(self, requiredRpms, requiredTopics):
			self.requiredRpms.update(requiredRpms)
			self.requiredTopics.update(requiredTopics)

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self._buildRequires = {}

		self._commonRequired = None
		self._allRequired = self.BuildRequirements()

	def __bool__(self):
		return bool(self._buildRequires)

	def __len__(self):
		return len(self._buildRequires)

	def add(self, obsBuild, requiredRpms, requiredTopics):
		self._buildRequires[obsBuild] = self.BuildRequirements(requiredRpms, requiredTopics)

		if self._commonRequired is None:
			self._commonRequired = self.BuildRequirements(requiredRpms, requiredTopics)
		else:
			self._commonRequired.intersection_update(requiredRpms, requiredTopics)

		self._allRequired.update(requiredRpms, requiredTopics)

	def enumerate(self):
		for obsBuild, req in self._buildRequires.items():
			yield obsBuild, req.requiredTopics, req.requiredRpms

	@property
	def allRequiredTopics(self):
		return self._allRequired.requiredTopics

	@property
	def allRequiredRpms(self):
		return self._allRequired.requiredRpms

	@property
	def commonRequiredTopics(self):
		if self._commonRequired is None:
			return Classification.createLabelSet()
		return self._commonRequired.requiredTopics

	@property
	def commonRequiredRpms(self):
		if self._commonRequired is None:
			return set()
		return self._commonRequired.requiredRpms

class BooleanPredicateWithCache(object):
	def __init__(self):
		self._state = {}

	def __call__(self, key):
		return self.evaluate(key)

	def evaluate(self, key):
		result = self._state.get(key)
		if result is None:
			# prime the result as False in case there's a loop somewhere
			self._state[key] = False

			result = self.compute(key)
			self._state[key] = result
			assert(result is not None)

		return result

class PackageRequiresLabelsPredicate(BooleanPredicateWithCache):
	def __init__(self, classification, focusLabels, excludePackages = None):
		super().__init__()
		self.classification = classification
		self.focusLabels = focusLabels
		self.excludePackages = excludePackages

		self._nameToLabel = {}
		for label, members in classification.enumeratePackages():
			for rpm in members:
				self._nameToLabel[rpm.name] = label

	def compute(self, rpm):
		assert(rpm is not None)
		if self.excludePackages is not None:
			if rpm in self.excludePackages:
				return False

		label = self._nameToLabel.get(rpm.name)
		if label in self.focusLabels:
			return True

		for req in rpm.enumerateRequiredRpms():
			if req is rpm:
				continue

			# I'm too lazy right now to use the ResolverHints to transform
			# dependencies:
			if req.name in ('systemd', 'udev', 'info'):
				continue

			if rpm.name.startswith('libgio-2') and req.name in ('dbus-1', 'dbus-1-x11'):
				continue

			if rpm.name.startswith('gdm'):
				edge = (rpm.name, req.name)
				if edge == ('gdm-branding-SLE', 'gdm') or edge == ('gdm', 'gdm-branding-SLE'):
					continue

			if self.evaluate(req):
				return True

		return False

	def getLabel(self, rpm):
		label = self._nameToLabel.get(rpm.name)
		if label not in self.focusLabels:
			return None
		return label

class QuerySubject(object):
	def __init__(self, queryName, context):
		self.queryName = queryName
		self.context = context

	def __str__(self):
		return self.queryName

	def getLabelsForComponent(self, component):
		return self.context.getLabelsForComponent(component)

class QuerySubjectComponent(QuerySubject):
	def __init__(self, queryName, context, component):
		super().__init__(queryName, context)

		if component.type != Classification.TYPE_SOURCE:
			raise Exception(f"Invalid argument to component query: {component} is not a component label")

		self.component = component

		self._binaryLabels = None
		self._accessibleAPIs = None

	@property
	def binaryLabels(self):
		if self._binaryLabels is None:
			self._binaryLabels = self.getLabelsForComponent(self.component)
		return self._binaryLabels

	@property
	def accessibleAPIs(self):
		if self._accessibleAPIs is None:
			result = Classification.createLabelSet()
			for vc in self.visibleComponents:
				result.update(self.getLabelsForComponent(vc))

			self._accessibleAPIs = result
		return self._accessibleAPIs

	@property
	def visibleComponents(self):
		try:
			return self._visibleComponents
		except:
			pass

		self._visibleComponents = self.context.componentOrder.downwardClosureFor(self.component)
		return self._visibleComponents

	@property
	def visibleAPIs(self):
		try:
			return self._visibleAPIs
		except:
			pass

		self._visibleAPIs = self.context.getAPIsForComponentList(self.visibleComponents).union(self.binaryLabels)
		return self._visibleAPIs

	@property
	def alwaysImportedAPIs(self):
		try:
			return self._alwaysImportedAPIs
		except:
			pass

		self._alwaysImportedAPIs = Classification.createLabelSet()
		for vc in self.visibleComponents:
			self._alwaysImportedAPIs.update(vc.imports)

		return self._alwaysImportedAPIs

	@property
	def importableAPIs(self):
		try:
			return self._importableAPIs
		except:
			pass

		self._importableAPIs = self.context._allAPIs.difference(self.visibleAPIs)
		return self._importableAPIs

class QuerySubjectComponentOrTopic(QuerySubject):
	def __init__(self, queryName, context, label):
		super().__init__(queryName, context)

		self.label = label

		if label.type is Classification.TYPE_SOURCE:
			self.binaryLabels = self.getLabelsForComponent(label)
		elif label.type is Classification.TYPE_BINARY:
			self.binaryLabels = Classification.createLabelSet([label])
		else:
			raise Exception(f"Invalid argument to query {self.queryName}: {label} is neither a topic nor a component label")

##################################################################
# Generic base classes for queries and results rendering
##################################################################
class GenericRenderer(object):
	def __init__(self, context):
		self.context = context

	def renderPreamble(self, query):
		pass

	@staticmethod
	def renderLabelSet(msg, labelSet):
		if not labelSet:
			return

		print(msg)
		for label in sorted(labelSet, key = str):
			print(f"     {label}")

class GenericQuery(object):
	def __init__(self, context, renderer = None):
		self.context = context
		self.renderer = renderer

##################################################################
# Mixin class for InversionsQuery and BuildInversionsQuery
##################################################################
class InversionsQueryMixin(object):
	def __init__(self, context, ignore):
		opts = context.application.opts

		topicOrder = context.labelOrder
		componentOrder = context.componentOrder

		visibleComponents = Classification.createLabelSet()
		for componentLabel in context.enumerateLabelsForQuery(opts.components):
			if componentLabel.type is Classification.TYPE_BINARY:
				assert(componentLabel.sourceProject is not None)
				componentLabel = componentLabel.sourceProject
			visibleComponents.update(componentOrder.downwardClosureFor(componentLabel))

		ignoredTopics = Classification.createLabelSet()
		for labelName in ignore:
			if labelName.startswith('='):
				for componentLabel in context.enumerateProjectComponents(labelName[1:]):
					visibleComponents.add(componentLabel)
				continue

			ignoreLabel = context.getLabel('--ignore label', labelName, Classification.TYPE_BINARY)
			ignoredTopics.add(ignoreLabel)
			for purpose in ignoreLabel.objectPurposes:
				ignoredTopics.add(purpose)

		visibleTopics = Classification.createLabelSet()
		for componentLabel in visibleComponents:
			topics = context.getLabelsForComponent(componentLabel)
			visibleTopics.update(topics)

		ignoredTopics = topicOrder.downwardClosureForSet(ignoredTopics).difference(visibleTopics)
		self.ignoredTopics = ignoredTopics

		visibleTopics.update(ignoredTopics)

		self.inversionLabels = context.allBinaryLabels.difference(visibleTopics)

