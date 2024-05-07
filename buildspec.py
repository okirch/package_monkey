##################################################################
# Catalog all build configs (Core/standard, Core/java, ...) and
# compute the set of labels that they can access.
# For any given OBS package, get the source project it has been assigned
# to, and loop over build configs it provides.
# Select the build config that matches the build requirements of this
# OBS package. If there is no perfect match, choose the one that covers
# most of the requirements, and record the ones that could not be
# satisfied.
##################################################################

from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from filter import Classification
from profile import profiling

buildLogger = loggingFacade.getLogger('build')
debugBuild = buildLogger.debug

def renderLabelSet(name, labels, max = 6):
	if labels is None:
		return f"[unconstrained {name}]"

	if not labels:
		return f"[no {name}]"

	if len(labels) >= max:
		return f"[{len(labels)} {name}]"

	return f"[{name} {' '.join(map(str, labels))}]";


class BuildSpec(object):
	def __init__(self, buildName, component, requiredList, trace = False):
		self.name = buildName
		self.component = component
		self.buildEnvironment = None
		self.requiredLabels = Classification.createLabelSet()
		self.requiredPackages = set()
		self.required = requiredList
		self.unsatisfied = []
		self.trace = trace
		self.unlabelledBuildRequires = set()
		self.requiresUnlabelledPackages = False

		# The odd structure of the requiredList is due to our use of
		# the SolvingTree. The source RPM corresponds to a TreeNode,
		# and as we cycle through its list of lower neighbors (i.e.
		# its build requirements), we look at each node in turn. These
		# nodes have a label, and while most of them represent a single
		# binary RPM, some represent several packages (because these
		# packages form a dependency cycle and had to be collapsed into a
		# single node).
		for label, packages in requiredList:
			if label is not None:
				self.requiredLabels.add(label)
			else:
				self.unlabelledBuildRequires.update(set(packages))
				self.requiresUnlabelledPackages = True
			self.requiredPackages.update(set(packages))

	@property
	def buildRequires(self):
		return self.requiredLabels

	def __str__(self):
		return self.name

class BuildEnvironment(object):
	def __init__(self, buildConfig, apis, labelOrder, baseConfigSet = []):
		self.name = buildConfig.name
		# relativeName would be s.th. like 'standard'
		self.relativeName = buildConfig.flavorName
		self.component = buildConfig.parent
		self.buildConfig = buildConfig

		assert(buildConfig.type == Classification.TYPE_BUILDCONFIG)
		assert(self.component.type == Classification.TYPE_SOURCE)

		for baseConfig in baseConfigSet:
			assert(baseConfig.type == Classification.TYPE_BUILDCONFIG)

		for api in apis:
			if api.componentLabel is not self.component:
				raise Exception(f"{api} component is {api.componentLabel} - expected {self.component}")

		visible = apis.copy()

		for baseConfig in baseConfigSet:
			visible.update(baseConfig.buildRequires)
			visible.update(baseConfig.runtimeRequires)

		visible.update(buildConfig.buildRequires)
		visible.update(buildConfig.runtimeRequires)

		self.visible = labelOrder.downwardClosureForSet(visible)

	def __str__(self):
		return self.name


class BuildSpecFactory(object):
	def __init__(self):
		self._map = {}

	def defineBuildEnvironment(self, *args, **kwargs):
		buildEnv = BuildEnvironment(*args, **kwargs)

		component = buildEnv.component
		bucket = self._map.get(component)
		if bucket is None:
			bucket = []
			self._map[component] = bucket

		for existing in bucket:
			if existing.name == buildEnv.name:
				raise Exception(f"duplicate definition of {buildEnv}")
		bucket.append(buildEnv)

		return buildEnv

	def enumerateBuildEnvironments(self):
		return iter(self._map.items())

	def findOptimalBuildConfig(self, buildSpec):
		if buildSpec.requiresUnlabelledPackages:
			if buildSpec.trace:
				infomsg(f"Cannot determine buildconfig for {buildSpec} - unlabelled build requirements")
			return

		buildEnvironments = self._map.get(buildSpec.component)
		if buildEnvironments is None:
			raise Exception(f"{buildSpec} uses unknown component {buildSpec.component}")

		if buildSpec.trace:
			infomsg(f"Trying to determine buildconfig for {buildSpec}")

		fullMatch = None
		best = None
		bestCoverage = -1
		for env in buildEnvironments:
			if buildSpec.buildRequires.issubset(env.visible):
				if buildSpec.trace:
					infomsg(f"   {env} is a match; footprint={len(env.visible)}")
				# if Foo/standard can build this package, we're done.
				if env.relativeName == 'standard':
					fullMatch = env
					break
				if fullMatch is None or len(env.visible) < len(fullMatch.visible):
					fullMatch = env
				continue

			coverage = len(buildSpec.buildRequires.intersection(env.visible))
			if buildSpec.trace:
				infomsg(f"   {env} coverage {coverage}")
				missing = buildSpec.buildRequires.difference(env.visible)
				infomsg(f"      {renderLabelSet('missing', missing, max = 20)}")
			if coverage > bestCoverage:
				best = env
				bestCoverage = coverage

		if fullMatch is not None:
			if buildSpec.trace:
				infomsg(f"{buildSpec}: selected {fullMatch} (smallest complete match)")
			return fullMatch

		# infomsg(f"{buildSpec}: best build env is {best} coverage={bestCoverage}")
		if best is None:
			raise Exception(f"Cannot find build config for {buildSpec}: {buildSpec.component} does not seem to define any build configs")

		for requiredLabel, packages in buildSpec.required:
			if requiredLabel in best.visible:
				continue

			buildSpec.unsatisfied.append((requiredLabel, packages))

		if buildSpec.trace:
			infomsg(f"{buildSpec}: selected {fullMatch} (best approximate match)")
		return best

	@classmethod
	def create(klass, classificationScheme, labelOrder):
		result = klass()
		topicOrder = classificationScheme.defaultOrder()
		componentOrder = classificationScheme.componentOrder()
		development = classificationScheme.getLabel('Development')
		alwaysVisible = classificationScheme.getReferencingLabels(development)

		# Hack: define a kind of workbench
		for name in ('@ManDoc', '@Docbook', '@GCC', '@Guile'):
			label = classificationScheme.getLabel(name)
			if label is not None:
				alwaysVisible.add(label)

		for component in classificationScheme.allComponents:
			componentsBelow = componentOrder.downwardClosureFor(component)
			visibleTopics = alwaysVisible.copy()
			for below in componentsBelow:
				visibleTopics.update(classificationScheme.getReferencingLabels(below))

			apis = Classification.createLabelSet()

			devel = component.globalPurposeLabel("devel")
			if devel is not None and topicOrder.downwardClosureFor(devel).issubset(visibleTopics):
				apis.add(devel)

			# Loop over all topics in this component and select all inversion-free APIs
			# This makes up the default set of labels against which to build this component.
			for binary in classificationScheme.getReferencingLabels(component):
				if not binary.isAPI:
					continue

				if not topicOrder.downwardClosureFor(binary).issubset(visibleTopics):
					if binary.isBaseLabel:
						debugBuild(f"{component}/standard: ignoring API {binary} due to inversions")
						inversions = topicOrder.downwardClosureFor(binary).difference(visibleTopics)
						for inversion in topicOrder.maxima(inversions):
							path = topicOrder.findPath(inversion, binary)
							assert(path is not None)
							debugBuild(f"   inversion {' -> '.join(map(str, path))}")
					continue

				apis.add(binary)

			if not apis:
				debugBuild(f"{component}/standard: no inversion-free APIs for default set of ")

			baseConfigs = Classification.createLabelSet()

			standardConfig = component.getBuildFlavor('standard')
			if standardConfig is not None:
				baseConfigs.add(standardConfig)
				result.defineBuildEnvironment(standardConfig, apis, labelOrder, baseConfigSet = baseConfigs)

			for buildConfig in component.flavors:
				if buildConfig is standardConfig:
					continue

				# We're building FunkyComponent/foo
				# For all components LowerComponent below FunkyComponent, check whether LowerComponent/foo
				# and if so, add it to the base configs
				theseBaseConfigs = baseConfigs.copy()
				for lowerComponent in componentsBelow:
					lowerBuildConfig = lowerComponent.getBuildFlavor(buildConfig.name)
					if lowerBuildConfig is not None:
						theseBaseConfigs.add(lowerBuildConfig)

				result.defineBuildEnvironment(buildConfig, apis, labelOrder, baseConfigSet = theseBaseConfigs)

		return result

	def createBuildSpec(self, buildName, component, buildRequires, **kwargs):
		result = BuildSpec(buildName, component, buildRequires, **kwargs)

		if result.trace:
			infomsg(f"Created build spec for {result.name}; component={result.component}")

		if result.component is not None:
			result.buildEnvironment = self.findOptimalBuildConfig(result)

		return result

