from .filter import Classification, PackageFilter
from .cmd_label import ClassificationGadget
from .util import NameMatcher, DictOfSets
from .util import errormsg, infomsg, loggingFacade


class QueryContextBase(object):
	def __init__(self, application):
		self.application = application

		self.db = application.loadDBForSnapshot()

		gadget = ClassificationGadget(self.db, application.modelDescription)
		self.classification = gadget.solve(application.productCodebase)

		self.classificationScheme = gadget.classificationScheme
		self.epicOrder = self.classificationScheme.componentOrder()

	def enumerateLayers(self):
		for layer in sorted(self.classification.layers, key = str):
			yield layer

	def enumerateEpics(self):
		for epic in sorted(self.classificationScheme.allEpics, key = str):
			yield epic

	def enumerateBuilds(self):
		for build in self.db.builds:
			hints = build.labelHints
			if hints is None:
				continue

			epic = hints.epic
			if epic is not None:
				yield build, epic

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
			for layer in self.enumerateLayers():
				if not layer.epics:
					infomsg(f"{layer}: (no epics)")
					continue
				infomsg(f"{layer}:")
				with loggingFacade.temporaryIndent():
					for epic in sorted(layer.epics, key = str):
						yield epic
			return

		nameMatcher = NameMatcher(requestedNames)
		for epic in self.enumerateEpics():
			if nameMatcher.match(epic.name):
				yield epic

		unmatched = nameMatcher.reportUnmatched()
		if unmatched:
			raise Exception(f"Could not find matching epic or topic label: {' '.join(sorted(unmatched))}")

	def xxx_matchEpics(self, nameMatcher):
		for epic in self.classificationScheme.allEpics:
			if nameMatcher.match(epic.name):
				yield epic

	def xxx_matchTopics(self, nameMatcher):
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
		loggingFacade.enableStdout()

		with loggingFacade.temporaryIndent():
			for label in self.context.enumerateLabelsForQuery(queryNames, **kwargs):
				self(label)

	def topicsForLabel(self, label):
		return []
