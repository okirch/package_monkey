from .filter import Classification, PackageFilter
from .cmd_label import ClassificationGadget
from .util import NameMatcher, DictOfSets
from .util import errormsg, infomsg, loggingFacade


class QueryContext(object):
	def __init__(self, application):
		self.application = application

		self.verbosityLevel = 1
		if application.opts.terse:
			self.verbosityLevel = 0
		if application.opts.verbose:
			self.verbosityLevel = 2

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

class GenericQuery(object):
	def __init__(self, context):
		self.context = context

	def perform(self, queryNames, **kwargs):
		loggingFacade.enableStdout()

		with loggingFacade.temporaryIndent():
			for label in self.context.enumerateLabelsForQuery(queryNames, **kwargs):
				self(label)
