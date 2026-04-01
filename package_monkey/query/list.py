
from ..util import CountingDict
from ..queries import GenericQuery
from ..filter import Classification


class ListQuery(GenericQuery):
	def __init__(self, context, onlyBaseLabels = None):
		super().__init__(context, None)

		classification = context.classification
		membershipSizes = CountingDict()

		if onlyBaseLabels is None and context.verbosityLevel < 2:
			onlyBaseLabels = True

		for label, members in classification.enumeratePackages():
			if onlyBaseLabels:
				label = label.baseLabel
			membershipSizes.increment(label, len(members))

		for label, build in classification.enumerateBuilds():
			membershipSizes.increment(label, 1)

		self._membershipSizes = membershipSizes

	def __call__(self, queryTargetLabel):
		# stats = self.getComponentStats(queryTargetLabel)

		size = self._membershipSizes[queryTargetLabel]
		print(f"  {queryTargetLabel} ({size} OBS packages)")

		verbosityLevel = self.context.verbosityLevel
		if verbosityLevel > 0:
			classificationScheme = self.context.classificationScheme
			topicOrder = self.context.labelOrder

			binaryLabels = classificationScheme.getReferencingLabels(queryTargetLabel)
			for label in topicOrder.bottomUpTraversal(binaryLabels):
				self.processTopic(label)

			print()
		return

	def processTopic(self, label):
		size = self._membershipSizes[label]
		if not size:
			return

		print(f"      - {label} ({size} rpms)")
		if self.context.verbosityLevel > 1:
			classification = self.context.classification
			for rpm in sorted(classification.getPackagesForLabel(label), key = str):
				print(f"          {rpm}")

class ShowQuery(GenericQuery):
	def __init__(self, context):
		super().__init__(context, None)

		classification = context.classification

		self._archSet = classification.classificationScheme.defaultArchSet
		self._epics = {}

		for epic, build in classification.enumerateBuilds():
			if build.isSynthetic:
				continue

			buildSet = self._epics.get(epic)
			if buildSet is None:
				buildSet = set()
				self._epics[epic] = buildSet
			buildSet.add(build)

	def __call__(self, queryTargetLabel):
		builds = self._epics.get(queryTargetLabel)

		if not builds:
			print(f"  {queryTargetLabel}: no builds")
			return

		wantNewline = False

		print(f"  {queryTargetLabel}: {len(builds)} build(s)")
		if queryTargetLabel.description:
			print(f"    Description:")
			for s in queryTargetLabel.description.split('\n'):
				print(f"      {s}")
			wantNewline = True

		if queryTargetLabel.lifecycleID is not None:
			policy = self.context.classificationScheme.policy

			lifecycle = policy.getLifeCycle(queryTargetLabel.lifecycleID)
			if lifecycle is None:
				print(f"    Life Cycle: {queryTargetLabel.lifecycleID} [NOT DEFINED]")
			else:
				print(f"    Life Cycle: {queryTargetLabel.lifecycleID}")
				for impl in sorted(lifecycle.implementations or [], key = str):
					implEpics = Classification.createLabelSet()
					for epic in self.context.classificationScheme.allEpics:
						if epic.lifecycleID == impl:
							implEpics.add(epic)

					if implEpics:
						print(f"       implemented by {implEpics}")
					else:
						print(f"       implemented by life cycle {impl} [NO EPICS]")

			wantNewline = True

		if wantNewline:
			print()

		for build in sorted(builds, key = str):
			print(f"    {build}")
			for rpm in sorted(build.rpms, key = str):
				attrs = []
				if rpm.label and rpm.label.definingBuildOption:
					attrs.append(f"option={rpm.label.definingBuildOption}")
				elif rpm.label and rpm.label.fromAutoFlavor:
					name = rpm.label.fromAutoFlavor.name.lstrip('%')
					attrs.append(f"extra={name}")
				if rpm.label and rpm.label.klass:
					attrs.append(f"class={rpm.label.klass}")
				if rpm.architectures != self._archSet:
					attrs.append(f"arch={rpm.architectures}")

				extra = ""
				if attrs:
					extra = "; " + ' '.join(attrs)
				print(f"      {rpm}{extra}")
			print()
		return

	def processTopic(self, label):
		size = self._membershipSizes[label]
		if not size:
			return

		print(f"      - {label} ({size} rpms)")
		if self.context.verbosityLevel > 1:
			classification = self.context.classification
			for rpm in sorted(classification.getPackagesForLabel(label), key = str):
				print(f"          {rpm}")
