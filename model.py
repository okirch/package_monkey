##################################################################
#
# Definitions for mapping the component model to OBS
#
##################################################################

import yaml
from util import warnmsg

class Model:
	COMPONENT_MODE_BOOTSTRAP	= 0
	COMPONENT_MODE_REBUILD		= 1
	COMPONENT_MODE_BOOTSTRAP_SELF	= 2

	BOOTSTRAP_STRATEGY_MULTI	= 'multi-aggregate'
	BOOTSTRAP_STRATEGY_SINGLE	= 'single-aggregate'
	VALID_BOOTSTRAP_STRATEGIES = (
		BOOTSTRAP_STRATEGY_MULTI,
		BOOTSTRAP_STRATEGY_SINGLE,
	)
	BUILD_CONFIG_MODEL		= 'model'
	BUILD_CONFIG_SINGLE		= 'single'
	VALID_BUILD_CONFIG_STRATEGIES = (
		BUILD_CONFIG_MODEL,
		BUILD_CONFIG_SINGLE,
	)

class ProjectSettingsMixin(object):
	def __init__(self):
		self.mode = None
		self.generation = None
		self.bootstrapRepository = None
		self.bootstrapStrategy = None
		self.projectConfigSnippet = None
		self.gitProjectUrl = None
		self.buildConfigStrategy = None

	@property
	def bootstrapSelf(self):
		return self.mode == Model.COMPONENT_MODE_BOOTSTRAP_SELF

	@property
	def bootstrapOnly(self):
		return self.mode == Model.COMPONENT_MODE_BOOTSTRAP

class ComponentMapping(ProjectSettingsMixin):
	class Export:
		def __init__(self, name):
			self.name = name
			self.topics = set()

		def add(self, topic):
			self.topics.add(topic)

	def __init__(self, name):
		super().__init__()

		self.name = name
		self._exports = {}

	def __str__(self):
		return self.name

	@property
	def exports(self):
		return iter(self._exports.values())

	def addExport(self, name):
		export = self._exports.get(name)
		if export is None:
			export = self.Export(name)
			self._exports[name] = export
		return export

class ProjectMapping(ProjectSettingsMixin):
	def __init__(self, name):
		super().__init__()

		self.name = name
		self.componentNames = None

	def __str__(self):
		return self.name

class WorkbenchDefinition(ComponentMapping):
	def __init__(self):
		super().__init__('Workbench')
		self.includeNames = set()
		self.excludeNames = set()

class GenericProjectLocation(object):
	def __init__(self, name):
		self.name = name
		self.obsRepositoryName = None
		self.gitProjectUrl = None
		self.gitPackageUrl = None

class ComponentModelMapping(object):
	def __init__(self, name, type):
		self.name = name
		self.type = type

		self.source = GenericProjectLocation('source')
		self.targetProjectBase = None
		self.targetArchitectures = []
		self.useFallback = False
		self._components = {}
		self._defaultComponent = None
		self.ignorePackages = []
		self.workbench = None
		self.projects = []
		self.workingDir = None
		self.gitBaseUrl = None

		# TBD
		self.exportsSubProjectName = 'exports'

	@property
	def sourceRepository(self):
		return self.source.obsRepositoryName

	def addComponent(self, component):
		if self._components.get(component.name) is not None:
			raise Exception(f"Duplicate definition of component {component}")
		self._components[component.name] = component

	def getComponent(self, name):
		result = self._components.get(name)
		if result is None:
			result = self.defaultComponent
		return result

	@property
	def defaultComponent(self):
		if self._defaultComponent is None:
			component = ComponentMapping('default')
			self._defaultComponent = component

		return self._defaultComponent

	def addProject(self, project):
		self.projects.append(project)

	@property
	def bootstrapRepository(self):
		if self._defaultComponent is not None:
			return self._defaultComponent.bootstrapRepository
		return None

	def workingDirPath(self, relativeName):
		if self.workingDir is None:
			return relativeName
		return f"{self.workingDir}/{relativeName}"

	@classmethod
	def load(klass, path):
		with open(path) as f:
			data = yaml.full_load(f)

		cm = ComponentModelMapping(data['name'], data['type'])

		source = cm.source
		source.obsRepositoryName = klass.getYamlString(data, 'source_repository')
		source.gitProjectUrl = klass.getYamlString(data, 'source_git_project_url', default = None)
		source.gitPackageUrl = klass.getYamlString(data, 'source_git_package_url', default = None)

		cm.targetProjectBase = klass.getYamlString(data, 'target_project_base')
		cm.gitBaseUrl = klass.getYamlString(data, 'git_base_url', default = None)
		cm.workingDir = klass.getYamlString(data, 'working_dir', default = 'work')
		cm.targetArchitectures = klass.getYamlStringList(data, 'target_architectures')
		cm.alwaysBuildRequires = klass.getYamlStringList(data, 'always_build_requires')
		cm.useFallback = klass.getYamlBool(data, 'use_fallback')
		cm.ignorePackages = klass.getYamlStringList(data, 'ignore_packages')

		defaults = cm.defaultComponent

		cd = data.get('defaults')
		if cd is not None:
			cm.processProjectSettings(defaults, cd)
		else:
			defaults.mode = Model.COMPONENT_MODE_BOOTSTRAP
			defaults.generation = 'bootstrap'
			defaults.bootstrapStrategy = Model.BOOTSTRAP_STRATEGY_MULTI

		componentData = klass.getYamlDict(data, 'components')
		for name, cd in componentData.items():
			if cd is None:
				cd = {}

			component = ComponentMapping(name)
			cm.processProjectSettings(component, cd)

			exportData = klass.getYamlDict(cd, 'exports', default = {})
			klass.processExports(component, exportData)

			cm.addComponent(component)

		projectData = klass.getYamlDict(data, 'projects')
		for name, cd in projectData.items():
			project = ProjectMapping(name)
			cm.processProject(project, cd)

			cm.addProject(project)

		wb = klass.getYamlDict(data, 'workbench', default = None)
		if wb is not None:
			workbench = WorkbenchDefinition()
			cm.processProjectSettings(workbench, wb)
			workbench.includeNames = set(klass.getYamlStringList(wb, 'include', default= []))
			workbench.excludeNames = set(klass.getYamlStringList(wb, 'exclude', default= []))
			cm.workbench = workbench

		return cm

	def processProjectSettings(self, component, cd):
		cmDefaults = self.defaultComponent

		mode = cd.get('bootstrap')
		generation = self.getYamlString(cd, 'generation', default = None)
		bootstrap_repository = self.getYamlString(cd, 'bootstrap_repository', default = None)
		bootstrap_strategy = self.getYamlString(cd, 'bootstrap_strategy', default = None)
		prjconf = self.getYamlString(cd, 'prjconf', default = None)
		git_project = self.getYamlString(cd, 'git_project_url', default = None)
		git_package = self.getYamlString(cd, 'git_package_url', default = None)
		build_config = self.getYamlString(cd, 'build_config', default = 'model')

		if mode is not None:
			if mode is True:
				mode = Model.COMPONENT_MODE_BOOTSTRAP
			elif mode is False:
				mode = Model.COMPONENT_MODE_REBUILD
			elif mode == 'self':
				mode = Model.COMPONENT_MODE_BOOTSTRAP_SELF
			else:
				raise Exception(f"Invalid setting bootstrap='{mode}' in definition of component {component}")

		if component is not cmDefaults:
			if mode is None:
				mode = cmDefaults.mode
			if mode is None:
				mode = Model.COMPONENT_MODE_BOOTSTRAP
			if mode == Model.COMPONENT_MODE_BOOTSTRAP:
				generation = 'bootstrap'
			if generation is None:
				generation = cmDefaults.generation
			if bootstrap_repository is None:
				bootstrap_repository = cmDefaults.bootstrapRepository
			if bootstrap_strategy is None:
				bootstrap_strategy = cmDefaults.bootstrapStrategy
		else:
			if bootstrap_strategy is None:
				bootstrap_strategy = Model.BOOTSTRAP_STRATEGY_MULTI

		git_working_dir = None
		if git_project is not None:
			git_project = self.processGitUrl(component, git_project, cmDefaults.gitProjectUrl)
		if git_package is not None:
			git_package = self.processGitUrl(component, git_package, cmDefaults.gitPackageUrl)

		if generation is None:
			raise Exception(f"Incomplete definition of OBS component {component}: missing generation")
		if bootstrap_repository is None:
			raise Exception(f"Incomplete definition of OBS component {component}: missing bootstrap_repository")
		if bootstrap_strategy not in Model.VALID_BOOTSTRAP_STRATEGIES:
			raise Exception(f"Incomplete definition of OBS component {component}: missing or invalid bootstrap_strategy={bootstrap_strategy}")
		if build_config not in Model.VALID_BUILD_CONFIG_STRATEGIES:
			raise Exception(f"Bad definition of OBS component {component}: invalid setting build_config={build_config}")

		component.mode = mode
		component.generation = generation
		component.bootstrapRepository = bootstrap_repository
		component.bootstrapStrategy = bootstrap_strategy
		component.projectConfigSnippet = prjconf
		component.gitProjectUrl = git_project
		component.gitPackageUrl = git_package
		component.buildConfigStrategy = build_config

		# print(f"Define {component} mode={mode} generation={generation} bsr={component.bootstrapRepository} bss={component.bootstrapStrategy} bcs={component.buildConfigStrategy} git={component.gitProjectUrl}")
		return component

	@classmethod
	def processExports(klass, component, data):
		for name, values in data.items():
			export = component.addExport(name)
			if type(values) == str:
				export.add(values)
			elif type(values) == list:
				for topic in values:
					assert(type(topic) is str)
					export.add(topic)

	def processProject(self, project, cd):
		self.processProjectSettings(project, cd)

		componentNames = self.getYamlStringList(cd, 'components')
		project.componentNames = componentNames.copy()

	def processGitUrl(self, component, git_url, default_url):
		if git_url is None:
			return None

		# recognize these ase absolute:
		#  urlmethod://host/bla
		#  git@host:bla
		if '/' not in git_url and '@' not in git_url:
			base_url = default_url
			if base_url is None:
				base_url = self.gitBaseUrl
			if base_url is None:
				raise Exception(f"Invalid git project for {component}: relative project name but no git base url")
			git_url = f"{base_url}/{git_url}"

		return git_url

	NODEFAULT = type(None)

	@classmethod
	def getYamlField(klass, data, fieldName, expectedType, default = NODEFAULT):
		value = data.get(fieldName)
		if value is None:
			if default is not klass.NODEFAULT:
				return default
			raise Exception(f"Missing YAML field {fieldName}")
		if type(value) is not expectedType:
			raise Exception(f"Bad YAML field {fieldName}: expected {expectedType} but got {type(value)}")
		return value

	@classmethod
	def getYamlString(klass, data, fieldName, **kwargs):
		return klass.getYamlField(data, fieldName, str, **kwargs)

	@classmethod
	def getYamlBool(klass, data, fieldName, **kwargs):
		return klass.getYamlField(data, fieldName, bool, **kwargs)

	@classmethod
	def getYamlList(klass, data, fieldName, **kwargs):
		return klass.getYamlField(data, fieldName, list, **kwargs)

	@classmethod
	def getYamlDict(klass, data, fieldName, **kwargs):
		return klass.getYamlField(data, fieldName, dict, **kwargs)

	@classmethod
	def getYamlStringList(klass, data, fieldName, **kwargs):
		value = klass.getYamlList(data, fieldName, **kwargs)
		if not all(type(e) is str for e in value):
			raise Exception(f"Bad YAML field {fieldName}: expected list of strings but got {type(value)}")
		return value


cm = ComponentModelMapping.load('alp/model.yaml')
