from filter import Classification, PackageFilter
from util import ANSITreeFormatter

class QueryContext(object):
	QUASI_INFINITE = 4242 

	def __init__(self, application):
		self.application = application
		classificationScheme = Classification.Scheme()
		classification = application.loadClassification(classificationScheme)

		self._store = None
		self.classificationScheme = classificationScheme
		self.classification = classification
		self.labelOrder = classificationScheme.defaultOrder()
		self.componentOrder = classificationScheme.componentOrder()
		self.inversionMap = classification.inversionMap
		self._allAPIs = self.getAPIs()

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
				yield self.getLabel('component or topic', name)

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

	def getLabelsForComponent(self, component):
		return self.classificationScheme.getReferencingLabels(component)

	def getPackagesForLabel(self, label):
		return self.classification.getPackagesForLabel(label)

	def getPackageCountForLabel(self, label):
		packages = self.getPackagesForLabel(label)
		if packages is None:
			return 0
		return len(packages)

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

class RequirementsReport(object):
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

	def __init__(self, context, requiredLabel, verbosityLevel = 0):
		self.context = context
		self.requiredLabel = requiredLabel
		self.verbosityLevel = verbosityLevel
		self.requiringLabels = Classification.createLabelSet()
		self._paths = {}
		self._packages = {}
		self._dependencies = {}
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

	def addDependency(self, rpm, requiredRpm, label):
		self._dependencies[rpm] = (requiredRpm, label)

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

	def requirementsForRpm(self, rpm):
		return self._dependencies.get(rpm.name, None)

	def subtreeForLabel(self, topic):
		subtree = self._subtrees.get(topic)
		if subtree is None:
			return None

		tf = ANSITreeFormatter()
		self.subtreeForLabelWork(topic, subtree, tf.root, set())

		return tf

	def subtreeForLabelWork(self, topic, subtree, tfParent, seen):
		seen.add(topic)

		labelOrder = self.context.labelOrder
		for child in labelOrder.lowerNeighbors(topic):
			if child not in subtree or child in seen:
				continue

			tfChild = tfParent.add(child)
			self.subtreeForLabelWork(child, subtree, tfChild, seen)

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
	def __init__(self, classification, focusLabels):
		super().__init__()
		self.classification = classification
		self.focusLabels = focusLabels

		self._nameToLabel = {}
		for label, members in classification.enumeratePackages():
			for rpm in members:
				self._nameToLabel[rpm.name] = label

	def compute(self, rpm):
		label = self._nameToLabel.get(rpm.name)
		if label in self.focusLabels:
			return True

		for req in rpm.enumerateRequiredRpms():
			if req is rpm:
				continue

			# I'm too lazy right now to use the ResolverHints to transform
			# dependencies:
			if req.name in ('systemd', 'udev'):
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
