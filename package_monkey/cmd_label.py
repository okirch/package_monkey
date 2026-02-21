
from .packages import ProductMediator, PackageCollection
from .filter import Classification
from .floader import FilterLoader
from .options import ApplicationBase
from .util import TimedExecutionBlock, ExecTimer
from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .obsclnt import OBSBuild
from .classify import *
from .new_compose import *
from .scenario import *

# This is the workhorse for performing the classification.
# Split into a separate class so that it can also be used by the composition code
class ClassificationGadget(object):
	def __init__(self, db, modelDescription, traceMatcher = None):
		classificationScheme = Classification.Scheme()
		if traceMatcher is not None:
			classificationScheme.installLabelTracing(traceMatcher)
		classificationScheme.setDefaultArchitectures(db.architectures)

		self.classificationScheme = classificationScheme
		self.traceMatcher = traceMatcher
		self.db = db

		loader = FilterLoader()

		# inspect all scenarios and inform the filter loader about valid versions,
		# and member packages
		scenarioFacade = ScenarioLabellingFacade(db, modelDescription.loadPreprocessorHints())

		filterPath = modelDescription.getPath('filter.yaml')
		self.schemeBuilder = loader.load(filename = filterPath, scheme = classificationScheme, scenarios = scenarioFacade)

	def solve(self, codebase):
		productMediator = self.performInitialPlacement(codebase)

		# Now finalize the label hierarchy
		self.schemeBuilder.complete()

		return NewResult.build(self.classificationScheme,
					productMediator.packageCollection,
					self.db)

	def performInitialPlacement(self, codebase):
		collection = PackageCollection()
		schemeBuilder = self.schemeBuilder
		db = self.db

		with TimedExecutionBlock("loading all packages from database"):
			productMediator = ProductMediator(codebase, collection)

			# generate all synthetic packages like environment_with_systemd, and
			# wrap them in a fake build object.
			# These objects will be added the the PackageCollection
			productMediator.generateSyntheticBuilds(db)

			if not productMediator.loadAndVerifyPackages(db):
				raise Exception("Inconsistencies in package data; refusing to continue")

			# enable tracing of packages as early as possible
			if self.traceMatcher is not None:
				collection.enablePackageTracing(self.traceMatcher)

		with TimedExecutionBlock("performing initial placement of packages"):
			for name in schemeBuilder.promises:
				productMediator.generatePromise(name, db)

			deferred = []
			for build in collection.builds:
				schemeBuilder.tryToLabelBuild(build)
				if build.epic is None and not build.isSynthetic:
					deferred.append(build)

			defaultEpic = self.classificationScheme.defaultEpic
			for build in deferred:
				epic = None

				# build somepkg:blah defaults to the same epic as build 'somepkg'
				if ':' in build.name:
					name = build.name.split(':')[0]
					# FIXME: this is slow, but shouldn't happen too often.
					baseBuild = self.db.lookupBuild(name)
					if baseBuild is not None and baseBuild.epic is not None:
						if build.trace:
							infomsg(f"{build}: inherit epic {build.epic} from base build {baseBuild}")
						epic = baseBuild.epic

				if epic is None and defaultEpic is not None:
					epic = defaultEpic
					if build.trace:
						infomsg(f"{build}: set epic to default epic {defaultEpic}")

				if epic is not None:
					labelHints = Classification.LabelHints(label = epic, epic = epic, layer = epic.layer)
					build.setLabelHints(labelHints)

			defaultClass = schemeBuilder.classificationScheme.defaultClass
			for pkg in collection.packages:
				schemeBuilder.tryToLabelPackage(pkg)
				if pkg.new_class is None:
					pkg.new_class = defaultClass

			schemeBuilder.resolveSubsets(db)

			numBadBuilds = 0
			badBuilds = {}
			for build in collection.builds:
				if build.isSynthetic:
					continue

				if build.layer is None:
					continue

				epic = build.new_epic
				if epic is None:
					warnmsg(f"build {build} is placed in layer {build.layer} but has no placement hints. should be in {build.new_epic}")
					numBadBuilds += 1

					epic = build.new_epic
					if epic not in badBuilds:
						badBuilds[epic] = set()
					badBuilds[epic].add(build)

			if numBadBuilds:
				for epic in sorted(badBuilds.keys(), key = str):
					print(f"{epic}:")
					print(f"   packages:")
					for build in sorted(badBuilds[epic], key = str):
						print(f"    - {build}")
				raise Exception(f"Found {numBadBuilds} with insufficent labelling")

		return productMediator


class LabellingApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		assert(self.opts)

	def run(self):
		db = self.loadNewDB()

		gadget = ClassificationGadget(db, self.modelDescription, self.traceMatcher)
		result = gadget.solve(self.productCodebase)

		if result.dependencyReport:
			infomsg(f"{len(result.dependencyReport)} package dependency inversions in model:")
			result.dependencyReport.render()
			infomsg(f"These are not fatal, but you may want to fix these.")

		self.codebaseData.saveClassification(result)

class ScenarioLabellingFacade(object):
	class ScenarioBinding(object):
		def __init__(self, var, version):
			self.var = var
			self.version = version
			self.builds = set()
			self.errors = []

		def __str__(self):
			return f"{self.var}={self.version}"

		def fail(self, msg):
			self.errors.append(msg)

	def __init__(self, db, hints):
		self.db = db
		self.hints = hints
		self._bindings = {}

		for rpm in db.rpms:
			for scenarioName in rpm.controllingScenarios.common:
				sct = ScenarioTuple.parse(scenarioName)
				binding = self.getBinding(sct.variable, sct.value, create = True)
				binding.builds.add(rpm.new_build)

	def getBinding(self, name, version, create = False):
		key = f"{name}={version}"

		binding = self._bindings.get(key)
		if binding is None and create:
			binding = self.ScenarioBinding(name, version)
			self._bindings[key] = binding
		return binding
