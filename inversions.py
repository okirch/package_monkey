
from util import ExecTimer
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from functools import reduce
from profile import profiling
from filter import Classification

class InversionMap(object):
	class Inversions(object):
		def __init__(self, topic, inversions):
			self.topic = topic
			self.inversions = inversions

	def __init__(self):
		self.topicToInversion = {}
		self.componentSelfContained = {}

	def add(self, topic, inversions):
		self.topicToInversion[topic] = inversions

	def get(self, topic):
		return self.topicToInversion.get(topic)

	def addGoodComponentTopics(self, label, goodTopics):
		self.componentSelfContained[label] = goodTopics

	def getGoodComponentTopics(self, label):
		return self.componentSelfContained[label]

class InversionInspector(object):
	def __init__(self, component, builder):
		self.component = component

		self.inversionMap = builder.inversionMap
		self.topicOrder = builder.topicOrder
		self.allExportedTopics = builder.allExportedTopics

		self.initialTopics = Classification.createLabelSet()
		self.candidateTopics = Classification.createLabelSet()
		self.goodTopics = Classification.createLabelSet()
		self.topicsOfInterest = None
		self.internalTopics = None

	def inspectRequirement(self, label, req, inversions):
		if req.sourceProject is self.component:
			indirectInversions = self.inversionMap.get(req)
			if indirectInversions is None:
				raise Exception(f"Lacking inversions for {req}")
			inversions.update(indirectInversions)
			return True

		if label.name == "@GdkPixbuf" and req.name == "@Core+systemdlibs":
			infomsg(f"req base label is good: {req.baseLabel in self.goodTopics}")

		return False

		# All of the stuff below is highly speculative and probably not a good idea at all

		autoLabel = req.fromAutoFlavor
		if autoLabel is None:
			return False

		# Consider @Foo requiring inversion @UdevLibraries
		# Then @Foo+python will depend on inversion @UdevLibraries+python
		# Detect whether that adds any new inversions or not.
		infomsg(f"{label} requires {req}, auto = {autoLabel}")
		if autoLabel and label.fromAutoFlavor is autoLabel and \
		   req.purposeName == label.purposeName:
			baseLabel = label.baseLabel
			reqBaseLabel = req.baseLabel
			if reqBaseLabel in baseLabel.runtimeRequires:
				infomsg(f"{label} requires {req}, and {baseLabel} requires {reqBaseLabel}")

				x = autoLabel.runtimeRequires.copy()
				x.add(baseLabel)
				x.add(reqBaseLabel)
				supOfBaseLabels = self.topicOrder.downwardClosureForSet(x)

				if label.runtimeRequires.issubset(supOfBaseLabels):
					infomsg(f"{label} requires {req}, and its requirements are satisfied by {req} and {baseLabel}")
					inversions.update(autoLabel.runtimeRequires)
					req = reqBaseLabel
				else:
					lacking = label.runtimeRequires.difference(supOfBaseLabels)
					infomsg(f"   lacking {' '.join(map(str, lacking))}")

		return False

	def filterStrangeInversions(self, label, inversions):
		imports = self.allExportedTopics.intersection(inversions)

		strangeInversions = Classification.createLabelSet()
		for dep in inversions.difference(imports):
			if dep in self.allExportedTopics:
				continue

			# if @Foo+python requires @Bar+python, and @Foo requires @Bar
			# then it's not a "strange" inversion
			if dep.fromAutoFlavor == label.fromAutoFlavor and \
			   dep.baseLabel in label.baseLabel.configuredRuntimeRequires:
				continue

			strangeInversions.add(dep)

		return strangeInversions

class InversionBuilder:
	def __init__(self, classification):
		self.classification = classification
		self.componentOrder = classification.componentOrder()
		self.topicOrder = classification.createOrdering(Classification.TYPE_BINARY)

		self.allExportedTopics = reduce(Classification.domain.set.union,
				(componentLabel.exports for componentLabel in classification.allComponents))

		# InversionBuilder.extendTopicsFlavors(self.allExportedTopics)
		InversionBuilder.extendTopicsPurposes(self.allExportedTopics)

		self.inversionMap = InversionMap()

	def allComponentTopics(self, componentLabel):
		return self.classification.getReferencingLabels(componentLabel)

	def addInversion(self, topic, inversions):
		self.inversionMap.add(topic, inversions)

	@staticmethod
	def extendTopicsFlavors(topicSet):
		extended = Classification.createLabelSet()
		for topic in topicSet:
			for purpose in topic.flavors:
				extended.add(purpose)
		topicSet.update(extended)

	@staticmethod
	def extendTopicsPurposes(topicSet):
		extended = Classification.createLabelSet()
		for topic in topicSet:
			for purpose in topic.objectPurposes:
				if purpose.purposeName != 'devel':
					extended.add(purpose)
		topicSet.update(extended)

	def createRuntimeScope(self, componentLabel):
		result = InversionInspector(componentLabel, self)

		componentClosure = self.componentOrder.downwardClosureFor(componentLabel)
		for component in componentClosure:
			result.initialTopics.update(component.imports)

			topicLabels = self.allComponentTopics(component)
			result.candidateTopics.update(topicLabels)
			if component is componentLabel:
				result.topicsOfInterest = topicLabels
				result.internalTopics = topicLabels
			else:
				goodTopics = self.inversionMap.getGoodComponentTopics(component)
				result.goodTopics.update(goodTopics)
#				infomsg(f"{componentLabel}: updating goodTopics with those of {component}")
#				for lll in sorted(goodTopics, key = str):
#					if lll.parent is None:
#						infomsg(f"    * {lll}")

		# Do not inspect any binary labels that we already found to be inversion-free
		# within the context of the component they belong to.
		result.candidateTopics.difference_update(result.goodTopics)

		# if we pull in @GccRuntime, we also want @GccRuntime-{doc,i18n,32bit,...}
		InversionBuilder.extendTopicsPurposes(result.initialTopics)

		result.initialTopics = self.topicOrder.downwardClosureForSet(result.initialTopics)
		return result

	def createBuildScope(self, componentLabel, runtimeScope):
		assert(runtimeScope.goodTopics)

		result = InversionInspector(componentLabel, self)

		buildConfig = componentLabel.getBuildFlavor('standard')
		if buildConfig is not None:
			result.initialTopics = runtimeScope.goodTopics.union(buildConfig.buildRequires)
		else:
			result.initialTopics = runtimeScope.goodTopics.copy()
		result.initialTopics = self.topicOrder.downwardClosureForSet(result.initialTopics)

		# if we pull in @GccRuntime, we also want @GccRuntime-{doc,i18n,32bit,...}
		if False:
			for topic in result.initialTopics:
				for purpose in topic.objectPurposes:
					result.initialTopics.add(purpose)

		# if a topic is free of inversions in a runtime context, then it'll also be
		# find in a build context
		result.candidateTopics = runtimeScope.candidateTopics.difference(runtimeScope.goodTopics)

		result.topicsOfInterest = Classification.createLabelSet(filter(lambda label: label.isAPI, result.candidateTopics))

		develTopic = componentLabel.globalPurposeLabel('devel')
		if develTopic is not None:
			result.topicsOfInterest.add(develTopic)

		result.goodTopics = runtimeScope.goodTopics.copy()

		return result

	def process(self, componentLabel):
		runtimeScope = self.createRuntimeScope(componentLabel)
		self.evaluate(runtimeScope)

		self.inversionMap.addGoodComponentTopics(componentLabel, runtimeScope.goodTopics)

		buildScope = self.createBuildScope(componentLabel, runtimeScope)
		self.evaluate(buildScope)

	def evaluate(self, scope):
		scope.goodTopics.update(scope.initialTopics)

		if False:
			infomsg(f"Good topics for {scope.component}: ")
			for lll in sorted(scope.goodTopics, key = str):
				if lll.parent is None:
					infomsg(f"    * {lll}")

		topicOrder = self.topicOrder
		for label in topicOrder.bottomUpTraversal(scope.candidateTopics):
			if label.runtimeRequires.issubset(scope.goodTopics):
				# infomsg(f"  {label} has NO inversions")
				scope.goodTopics.add(label)
			elif scope.topicsOfInterest is None or label in scope.topicsOfInterest:
				dependenciesWithInversions = label.runtimeRequires.difference(scope.goodTopics)

				inversions = Classification.createLabelSet()
				for req in dependenciesWithInversions:
					if not scope.inspectRequirement(label, req, inversions):
						inversions.add(req)

				assert(inversions)

				strangeInversions = scope.filterStrangeInversions(label, inversions)

				# do not consiser auto-selected runtime dependencies "strange"
				# The reason being, they were auto-selected because all their requirements
				# were already met by label before they were added.
				strangeInversions.difference_update(label.automaticRuntimeRequires)

				if label.purposeName is None and strangeInversions:
					warnmsg(f"{scope.component}: {label} has strange inversions {' '.join(map(str, strangeInversions))}")

				self.addInversion(label, inversions)

				# FIXME: some inversions are "expected" in the sense that they are
				# specified by an auto flavor (e.g. @Foo+typelib will always depend
				# on @Glib2 because the typelib auto flavor specifies this as a
				# runtime requirement

				if True and label.purposeName is None:
					if len(inversions) > 10:
						infomsg(f"  {label} has {len(inversions)} inversions")
					else:
						infomsg(f"  {label} has these inversions: {' '.join(map(str, inversions))}")


					def explainDependency(desc, label, inversion):
						path = label.explainRuntimeDependency(inversion)
						if not path:
							infomsg(f"    - {desc} {inversion} not configured")
						else:
							infomsg(f"    - {desc} {inversion} configured via {' -> '.join(map(str, path))}")

					if False and label.name in ('@LLVM', '@SystemPython', '@GdkPixbuf'):
						for i in inversions:
							infomsg(f"     - {i.componentName}: {i}")
							zz = self.inversionMap.get(i)
							if zz:
								infomsg(f"       which has these inversions: {' '.join(map(str, zz))}")
							else:
								infomsg(f"       which has no inversions")

							explainDependency("base label", label, i.baseLabel)

						infomsg("====")
						minimal = self.topicOrder.minima(inversions)
						for i in minimal:
							explainDependency("minimal label", label, i)
						zzz
		return scope.goodTopics

	class ComponentState(object):
		def __init__(self, componentLabel, builder):
			self.componentLabel = componentLabel
			self.builder = builder

			self.initialTopics = Classification.createLabelSet()
			self.candidateTopics = Classification.createLabelSet()

			componentClosure = builder.componentOrder.downwardClosureFor(componentLabel)
			for componentLabel in componentClosure:
				self.initialTopics.update(componentLabel.imports)
				self.candidateTopics.update(self.allComponentTopics(componentLabel))

			self.initialTopics = topicOrder.downwardClosureForSet(self.initialTopics)

			self._inversionFreeTopics = None

		@property
		def inversionFreeTopics(self):
			if self._inversionFreeTopics is None:
				self._inversionFreeTopics = self.computeInversions(self.componentLabel, self.initialTopics)
			return self._inversionFreeTopics

		@property
		def inversionFreeBuildTopics(self):
			if self._inversionFreeBuildTopics is None:
				initialTopics = self.initialTopics

				buildConfig = self.componentLabel.getBuildFlavor('standard')
				if buildConfig is not None:
					initialTopics.union(buildConfig.buildRequires)

				apiTopics = Classification.createLabelSet(
						filter(lambda label: label.isAPI,
							self.candidateTopics))
				self._inversionFreeBuildTopics = self.computeInversions(self.componentLabel, initialTopics,
						topicsOfInterest = apiTopics)
			return self._inversionFreeBuildTopics

		def computeInversionFreeTopics(self, initialTopics, topicsOfInterest = None):
			inversionFreeTopics = initialTopics.copy()

			# if we pull in @GccRuntime, we also want @GccRuntime-{doc,i18n,32bit,...}
			for topic in initialTopics:
				for purpose in topic.objectPurposes:
					if purpose.purposeName != 'devel':
						inversionFreeTopics.add(purpose)

			topicOrder = self.builder.topicOrder
			for label in topicOrder.bottomUpTraversal(self.candidateTopics):
				if label.runtimeRequires.issubset(inversionFreeTopics):
					inversionFreeTopics.add(label)
				elif topicsOfInterest is None or label in topicsOfInterest:
					inversions = label.runtimeRequires.difference(inversionFreeTopics)
					if True:
						if len(inversions) > 10:
							infomsg(f"  {label} has {len(inversions)} inversions")
						else:
							infomsg(f"  {label} has these inversions: {' '.join(map(str, inversions))}")

			return inversionFreeTopics



	def enter(self, componentLabel):
		return self.ComponentState(componentLabel, self)

