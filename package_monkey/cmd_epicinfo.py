##################################################################
#
# Handle various queries on epics
#
##################################################################

from .options import ApplicationBase
from .util import infomsg, errormsg, warnmsg, loggingFacade
from .util import NameMatcher
from .filter import Classification
from .cmd_label import ClassificationGadget

class EpicQueryApplication(ApplicationBase):
	def __init__(self, name, *args, **kwargs):
		super().__init__(name, *args, **kwargs)

	def run(self):
		self.verbosityLevel = 1
		if self.opts.terse:
			self.verbosityLevel = 0
		if self.opts.verbose:
			self.verbosityLevel = 2

		query = self.createQuery(QueryContext(self))

		epics = getattr(self.opts, 'epics', [])
		query.perform(epics)

class EpicListApplication(EpicQueryApplication):
	def createQuery(self, context):
		return ListQuery(context)

class EpicShowApplication(EpicQueryApplication):
	def createQuery(self, context):
		return ShowQuery(context)


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
		self.epicOrder = self.classificationScheme.epicOrder()

	def enumerateLayers(self):
		for layer in sorted(self.classificationScheme.allLayers, key = str):
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

class EpicQueryBase(object):
	def __init__(self, context):
		self.context = context

		self._members = {}
		for epic in context.enumerateEpics():
			self._members[epic] = set()

		for build, epic in context.enumerateBuilds():
			if build.isSynthetic:
				continue
			self._members[epic].add(build)

		self._archSet = context.classificationScheme.defaultArchSet
		self.processedEpics = Classification.createLabelSet()

	def perform(self, queryNames, **kwargs):
		loggingFacade.enableStdout()

		if not queryNames:
			for layer in self.context.enumerateLayers():
				self.queryLayer(layer)
			return

		nameMatcher = NameMatcher(queryNames)

		for layer in self.context.enumerateLayers():
			if nameMatcher.match(layer.name):
				self.queryLayer(layer)

		for epic in self.context.enumerateEpics():
			if nameMatcher.match(epic.name) and \
			   epic not in self.processedEpics:
				self.processedEpics.add(epic)
				self.queryEpic(epic)

		unmatched = nameMatcher.reportUnmatched()
		if unmatched:
			raise Exception(f"Could not find matching epic or topic label: {' '.join(sorted(unmatched))}")

	def queryLayer(self, layer):
		if not layer.members:
			infomsg(f"{layer}: (no epics)")
			return

		infomsg(f"{layer}:")
		with loggingFacade.temporaryIndent():
			for epic in sorted(layer.members, key = str):
				self.processedEpics.add(epic)
				self.queryEpic(epic)

class ListQuery(EpicQueryBase):
	def queryEpic(self, queryTargetLabel):
		members = self._members[queryTargetLabel]
		infomsg(f"{queryTargetLabel} ({len(members)} builds)")

		verbosityLevel = self.context.verbosityLevel
		if verbosityLevel > 1:
			# FIXME: we could save additional epic information to classification.db,
			# such as the file where the epic is defined
			for build in sorted(members, key = str):
				infomsg(f"   {build}")

class ShowQuery(EpicQueryBase):
	def queryEpic(self, queryTargetLabel):
		builds = self._members.get(queryTargetLabel)

		if not builds:
			infomsg(f"{queryTargetLabel}: no builds")
			infomsg("")
			return

		wantNewline = False

		infomsg(f"{queryTargetLabel}: {len(builds)} build(s)")
		if queryTargetLabel.description:
			infomsg(f"   Description:")
			for s in queryTargetLabel.description.split('\n'):
				if s:
					infomsg(f"      {s}")
			wantNewline = True

		if queryTargetLabel.lifecycleID is not None:
			policy = self.context.classificationScheme.policy

			lifecycle = policy.getLifeCycle(queryTargetLabel.lifecycleID)
			if lifecycle is None:
				infomsg(f"   Life Cycle: {queryTargetLabel.lifecycleID} [NOT DEFINED]")
			else:
				infomsg(f"   Life Cycle: {queryTargetLabel.lifecycleID}")
				for impl in sorted(lifecycle.implementations or [], key = str):
					implEpics = Classification.createLabelSet()
					for epic in self.context.classificationScheme.allEpics:
						if epic.lifecycleID == impl:
							implEpics.add(epic)

					if implEpics:
						infomsg(f"       implemented by {implEpics}")
					else:
						infomsg(f"       implemented by life cycle {impl} [NO EPICS]")

			wantNewline = True

		if wantNewline:
			infomsg("")

		for build in sorted(builds, key = str):
			infomsg(f"   {build}")
			for rpm in sorted(build.rpms, key = str):
				hints = rpm.labelHints
				if hints is None or hints.label is None:
					infomsg(f"      {rpm}")
					continue

				label = hints.label

				attrs = []
				if label and label.definingBuildOption:
					attrs.append(f"option={label.definingBuildOption}")
				elif label and label.fromAutoFlavor:
					name = label.fromAutoFlavor.name.lstrip('%')
					attrs.append(f"extra={name}")

				if rpm.new_class is not None:
					attrs.append(f"class={rpm.new_class}")
				if rpm.architectures != self._archSet:
					attrs.append(f"arch={rpm.architectures}")

				extra = ""
				if attrs:
					extra = "; " + ' '.join(attrs)
				infomsg(f"      {rpm}{extra}")
			infomsg("")
		return
