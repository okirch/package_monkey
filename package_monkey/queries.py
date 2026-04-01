from .filter import Classification, PackageFilter
from .util import ANSITreeFormatter, NameMatcher, DictOfSets
from .util import errormsg, infomsg

class QueryContextBase(object):
	def __init__(self, application):
		self.application = application

		self.classificationScheme = Classification.Scheme()
		self.classification = application.loadClassification(self.classificationScheme)

		self.labelOrder = None
		self.componentOrder = self.classificationScheme.componentOrder()

	def argumentsToTopics(self, names, buildClosure = False):
		result = Classification.createLabelSet()

		nameMatcher = NameMatcher(names)
		for epic in self.matchEpics(nameMatcher):
			result.update(epic.topicMembers)
		for topic in self.matchTopics(nameMatcher):
			result.add(topic)

		unmatched = nameMatcher.reportUnmatched()
		if unmatched:
			raise Exception(f"Could not find matching epic or topic label: {' '.join(sorted(unmatched))}")

		if buildClosure:
			result = self.labelOrder.downwardClosureForSet(result)

		return result

	def enumerateLabelsForQuery(self, requestedNames):
		if not requestedNames:
			for component in self.componentOrder.bottomUpTraversal():
				yield component
			return

		nameMatcher = NameMatcher(requestedNames)
		for epic in self.matchEpics(nameMatcher):
			yield epic
		for topic in self.matchTopics(nameMatcher):
			yield topic

		unmatched = nameMatcher.reportUnmatched()
		if unmatched:
			raise Exception(f"Could not find matching epic or topic label: {' '.join(sorted(unmatched))}")

	def matchEpics(self, nameMatcher):
		for epic in self.classificationScheme.allEpics:
			if nameMatcher.match(epic.name):
				yield epic

	def matchTopics(self, nameMatcher):
		for topic in self.classificationScheme.allTopics:
			if nameMatcher.match(topic.name):
				yield topic

class QueryContext(QueryContextBase):
	def __init__(self, application):
		super().__init__(application)

		self._store = None

		self._rpmToBuildMap = None
		self._rpmToLabelMap = None

		self.verbosityLevel = 1
		if application.opts.terse:
			self.verbosityLevel = 0
		if application.opts.verbose:
			self.verbosityLevel = 2

	def connectDatabase(self):
		return self.application.loadNewDB()

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

	def __init__(self, context):
		self.context = context
		self._dependencies = {}

	def addPackageRequirements(self, predicate, rpm):
		requirements = self._dependencies.get(rpm.name)
		if requirements is None:
			label = predicate.getLabel(rpm)
			requirements = self.PackageRequirements(rpm, label)
			self._dependencies[rpm.name] = requirements

			for req in filter(predicate, rpm.enumerateRequiredRpms()):
				requirements.children.append(self.addPackageRequirements(predicate, req))

		return requirements

	def requirementsForRpm(self, rpm):
		return self._dependencies.get(rpm.name, None)

class RequirementsReport(RequirementsReportBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.requiringLabels = Classification.createLabelSet()
		self._paths = {}
		self._packages = DictOfSets()
		self._subtrees = {}

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
		self._packages.add(topic, rpm)
		if predicate is not None:
			self.addPackageRequirements(predicate, rpm)

	def __bool__(self):
		return bool(self.requiringLabels)

	@property
	def allLabels(self):
		return sorted(self.requiringLabels, key = str)

	@property
	def minimalLabels(self):
		return self.context.reduceLabelSet(self.requiringLabels)

	def packagesForLabel(self, topic):
		return self._packages.get(topic) or []

	def pathsForLabel(self, topic):
		return self._paths.get(topic, [])

	def subtreeForLabel(self, topic):
		subtree = self._subtrees.get(topic)
		if subtree is None:
			return None

		topicOrder = None
		return topicOrder.asTreeFormatter(subtree, topDown = True)

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
		self.excludePackages = excludePackages or set()

		self._nameToLabel = {}
		for label, members in classification.enumeratePackages():
			for rpm in members:
				self._nameToLabel[rpm.name] = label

	def compute(self, rpm):
		assert(rpm is not None)
		if rpm in self.excludePackages:
			return False

		label = self._nameToLabel.get(rpm.name)
		if label in self.focusLabels:
			return True

		for req in rpm.enumerateRequiredRpms():
			if req is not rpm and self.evaluate(req):
				return True

		return False

	def getLabel(self, rpm):
		label = self._nameToLabel.get(rpm.name)
		if label not in self.focusLabels:
			return None
		return label

##################################################################
# Generic base classes for queries and results rendering
##################################################################
class GenericRenderer(object):
	def __init__(self, context):
		self.context = context

	@property
	def verbosityLevel(self):
		return self.context.verbosityLevel

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

	def perform(self, queryNames, **kwargs):
		for label in self.context.enumerateLabelsForQuery(queryNames, **kwargs):
			self(label)

	def topicsForLabel(self, label):
		return []
