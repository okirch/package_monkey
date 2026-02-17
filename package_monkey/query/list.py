
from ..util import infomsg
from ..queries import GenericQuery
from ..filter import Classification

class EpicQueryBase(GenericQuery):
	def __init__(self, context):
		super().__init__(context)

		classification = context.classification

		self._members = {}
		for epic in context.enumerateEpics():
			self._members[epic] = set()

		for build, epic in context.enumerateBuilds():
			if build.isSynthetic:
				continue
			self._members[epic].add(build)

		self._archSet = classification.classificationScheme.defaultArchSet

class ListQuery(EpicQueryBase):
	def __call__(self, queryTargetLabel):
		members = self._members[queryTargetLabel]
		infomsg(f"{queryTargetLabel} ({len(members)} builds)")

		verbosityLevel = self.context.verbosityLevel
		if verbosityLevel > 1:
			# FIXME: we could save additional epic information to classification.db,
			# such as the file where the epic is defined
			for build in sorted(members, key = str):
				infomsg(f"   {build}")

class ShowQuery(EpicQueryBase):
	def __call__(self, queryTargetLabel):
		builds = self._members.get(queryTargetLabel)

		if not builds:
			infomsg(f"{queryTargetLabel}: no builds")
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
