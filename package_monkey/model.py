##################################################################
#
# Definitions for mapping the component model to OBS
# OBSOLETE
#
##################################################################

import yaml
import os.path
from .util import warnmsg, infomsg

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
		self.description = None
		self.contract = None
		self.bootstrapRepository = None
		self.bootstrapStrategy = None
		self.projectConfigSnippet = None
		self.gitPackageUrl = None
		self.gitProjectUrl = None
		self.buildConfigStrategy = None
		self.workbench = None

		# os far, this is still ignored by obs-create-model:
		self.buildRequires = set()

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
		self.extraPackages = None

		self.imports = []
		self.requires = []
		self.requiresNames = []
		self.buildRequires = []
		self.buildRequiresNames = []

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

class ProductDefinition(object):
	TYPE_PRODUCT	= 'product'
	TYPE_EXTENSION	= 'extension'

	VALID_PRODUCT_TYPES = (
		'product', 'extension',
	)

	def __init__(self, name, projectNames, type = None):
		if type is None:
			type = self.TYPE_PRODUCT

		assert(type in self.VALID_PRODUCT_TYPES)

		self.name = name
		self.type = type
		self.extends = None
		self.usesProjectNames = projectNames.copy()
		self.patterns = []
		self.exclude = []

	def __str__(self):
		return self.name

	def addPattern(self, pattern):
		if pattern in self.patterns:
			return

		self.patterns.append(pattern)

class ProductMapping(GenericProjectLocation):
	def __init__(self):
		super().__init__('products')
		self._products = {}

	def addProduct(self, productDefinition):
		if productDefinition.name in self._products:
			raise Exception(f"Duplicate definition of product {productDefinition}")
		self._products[productDefinition.name] = productDefinition

	@property
	def products(self):
		return iter(self._products.values())

	def resolve(self):
		def resolveRecursively(productDefinition, seen):
			baseProduct = self._products.get(productDefinition.extends)
			if baseProduct is None:
				raise Exception(f"Product {productDefinition} extends {productDefinition.extends}, but we don't know about it")

			if baseProduct in seen:
				raise Exception(f"Product {productDefinition} extends {productDefinition.extends}: circular dependencies")

			resolved = baseProduct.usesProjectNames.copy()
			for projectName in productDefinition.usesProjectNames:
				if projectName not in resolved:
					resolved.append(projectName)

			productDefinition.extends = baseProduct
			productDefinition.usesProjectNames = resolved

		for productDefinition in self.products:
			if type(productDefinition.extends) is str:
				resolveRecursively(productDefinition, set())

class PatternDefinitionBase(object):
	NAME_LISTS = ('requires', 'recommends', 'suggests')

	def __init__(self, name):
		self.name = name
		self.requires = []
		self.recommends = []
		self.suggests = []

	def __str__(self):
		return self.name

	def getList(self, name):
		return getattr(self, name, [])

class PatternDefinition(PatternDefinitionBase):
	def __init__(self, name):
		super().__init__(name)

		self.summary = ""
		self.description = ""

		self.verbatim = None
		self._conditionals = {}

		self.requiresPatternNames = []
		self.requiresPatterns = []

		self.category = "SLFO"
		self.icon = "pattern-generic"
		self.order = 666

	def __str__(self):
		return self.name

	def addConditional(self, key, value):
		cond = ConditionalPatternDefinition(self.name, key, value)
		if cond.name in self._conditionals:
			raise Exception(f"Duplicate definition of conditional pattern {cond}")

		self._conditionals[cond.name] = cond
		return cond

	@property
	def conditionals(self):
		return iter(self._conditionals.values())

class ConditionalPatternDefinition(PatternDefinition):
	def __init__(self, patternName, key, value):
		super().__init__(f"{patternName}/{key}={value}")

		self.key = key
		self.value = value

class PatternMapping(object):
	def __init__(self):
		self._patterns = {}

	def addPattern(self, patternDefinition):
		if patternDefinition.name in self._patterns:
			raise Exception(f"Duplicate definition of pattern {patternDefinition}")
		self._patterns[patternDefinition.name] = patternDefinition

	def getPattern(self, name):
		return self._patterns.get(name)

	@property
	def patterns(self):
		return iter(self._patterns.values())

	def resolve(self):
		for pattern in self.patterns:
			if not pattern.requiresPatternNames:
				continue

			pattern.requiresPatterns = []
			for name in pattern.requiresPatternNames:
				other = self.getPattern(name)
				if other is None:
					raise Exception(f"Pattern {pattern} requires unknown pattern {name}")
				pattern.requiresPatterns.append(other)

class PackageArchMap(object):
	VALID_ARCH_NAMES = set((
		'x86_64', 's390x', 'aarch64', 'ppc64le'
	))

	def __init__(self):
		self._names = {}

	def add(self, name, values):
		badNames = set(values).difference(self.VALID_ARCH_NAMES)
		if badNames:
			raise Exception(f"Bad architecture(s) for {name}: {', '.join(badNames)}")

		self._names[name] = values

	def getRestrictions(self, name):
#		if '-32bit' in name or 'x86-64-v3' in name:
#			return ['x86_64']

		return self._names.get(name)

class ContractDefinition(object):
	class Clause(object):
		def __init__(self, key):
			self.key = key
			self._subclauses = {}
			self._values = {}

		def set(self, key, value):
			self._values[key] = value

		def getValue(self, key):
			return self._values.get(key)

		def subClause(self, key, create = False):
			result = self._subclauses.get(key)
			if result is None and create:
				result = self.__class__(key)
				self._subclauses[key] = result
			return result

	def __init__(self):
		self.root = self.Clause(None)

	def copy(self):
		import copy
		return copy.deepcopy(self)

class ComponentModelMapping(object):
	def __init__(self, name, type):
		self.name = name
		self.type = type

		self.source = GenericProjectLocation('source')
		self.targetProjectBase = None
		self.targetArchitectures = []
		self.useFallback = False
		self.sharedGroupsFile = False
		self._defaultComponent = None
		self.ignorePackages = []
		self.workbench = None
		self.projects = []
		self.workingDir = None
		self.gitBaseUrl = None

		self.products = None
		self.patterns = PatternMapping()
		self.archMap = PackageArchMap()

		# TBD
		self.exportsSubProjectName = 'exports'

	@property
	def sourceRepository(self):
		return self.source.obsRepositoryName

	@property
	def defaultProjectSettings(self):
		if self._defaultComponent is None:
			component = ComponentMapping('default')
			self._defaultComponent = component

		return self._defaultComponent

	def addProject(self, project):
		self.projects.append(project)

	def getProject(self, name):
		for projectDefinition in self.projects:
			if projectDefinition.name == name:
				return projectDefinition
		return None

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
	def maybe_load_sibling_file(klass, model_path, sibling_fname):
		dn = os.path.dirname(model_path)
		sibling_path = os.path.join(dn, sibling_fname)
		if not os.path.isfile(sibling_path):
			return None

		print(f"Loading {sibling_path}")
		with open(sibling_path) as f:
			data = yaml.full_load(f)

		return data

	@classmethod
	def load(klass, path):
		with open(path) as f:
			data = yaml.full_load(f)

		cm = ComponentModelMapping(data['name'], data['type'])

		pd = klass.maybe_load_sibling_file(path, 'patterns.yaml')
		if pd is not None:
			for name, ppd in pd.items():
				cm.processPatternDefinition(name, ppd)

		# This is a hack to deal with the fact that we're currently
		# looking at x86-64 only
		pd = klass.maybe_load_sibling_file(path, 'arch-limits.yaml')
		if pd is not None:
			cm.processPackageArchInfo(pd)

		cd = data.get('source')
		if cd is not None:
			cm.processLocation(cm.source, cd)

		cm.targetProjectBase = klass.getYamlString(data, 'target_project_base')
		cm.gitBaseUrl = klass.getYamlString(data, 'git_base_url', default = None)
		cm.workingDir = klass.getYamlString(data, 'working_dir', default = 'work')
		cm.targetArchitectures = klass.getYamlStringList(data, 'target_architectures')
		cm.alwaysBuildRequires = klass.getYamlStringList(data, 'always_build_requires')
		cm.useFallback = klass.getYamlBool(data, 'use_fallback')
		cm.sharedGroupsFile = klass.getYamlBool(data, 'shared_groups_file', default = False)
		cm.ignorePackages = klass.getYamlStringList(data, 'ignore_packages')
		cm.rewritePackages = klass.getYamlDict(data, 'rewrite_packages')

		defaults = cm.defaultProjectSettings

		cd = data.get('defaults')
		if cd is not None:
			cm.processProjectSettings(defaults, cd)
		else:
			defaults.mode = Model.COMPONENT_MODE_BOOTSTRAP
			defaults.generation = 'bootstrap'
			defaults.bootstrapStrategy = Model.BOOTSTRAP_STRATEGY_MULTI

		projectData = klass.getYamlDict(data, 'projects')
		for name, cd in projectData.items():
			project = ProjectMapping(name)
			cm.processProject(project, cd)

			wb = cd.get('workbench')
			if wb is not None:
				project.workbench = ProjectMapping("{name}:workbench")
				cm.processProject(project.workbench, wb, parentProject = project)

			cm.addProject(project)

		for project in cm.projects:
			for name in project.requiresNames:
				reqProject = cm.getProject(name)
				if reqProject is None:
					raise Exception(f"Project {project.name} requires unknown project {name}")
				project.requires.append(reqProject)

			for name in project.buildRequiresNames:
				reqProject = cm.getProject(name)
				if reqProject is None:
					raise Exception(f"Project {project.name} build_requires unknown project {name}")
				# project.buildRequires.append(reqProject)

		wb = klass.getYamlDict(data, 'workbench', default = None)
		if wb is not None:
			workbench = WorkbenchDefinition()
			cm.processProjectSettings(workbench, wb)
			workbench.includeNames = set(klass.getYamlStringList(wb, 'include', default= []))
			workbench.excludeNames = set(klass.getYamlStringList(wb, 'exclude', default= []))
			cm.workbench = workbench

		pd = klass.getYamlDict(data, 'patterns', default = None)
		if pd is not None:
			for name, ppd in pd.items():
				cm.processPatternDefinition(name, ppd)

		pd = data.get('product')
		assert(pd is None)

		pd = klass.getYamlDict(data, 'products', default = None)
		if pd is not None:
			cm.products = ProductMapping()
			cm.processLocation(cm.products, klass.getYamlDict(pd, 'location'))

			for name, ppd in pd.items():
				if name == 'location':
					continue

				cm.processProductDefinition(name, ppd)

			cm.products.resolve()

		ad = klass.getYamlDict(data, 'architecture', default = {})
		for item in klass.getYamlList(ad, 'restrict', default = []):
			assert(type(item) is dict)
			assert(len(item) == 1)
			for name, values in item.items():
				assert(type(values) == list)
				cm.archMap.add(name, values)

		cm.patterns.resolve()

		return cm

	def processLocation(self, location, cd, prefix = None):
		name = location.name
		cmDefaults = self.defaultProjectSettings

		location.obsRepositoryName = self.getYamlString(cd, 'repository', default = None)

		git_url = self.getYamlString(cd, 'git_project_url', default = None)
		if git_url is not None:
			location.gitProjectUrl = self.processGitUrl(name, git_url, cmDefaults.gitProjectUrl)

		git_url = self.getYamlString(cd, 'git_package_url', default = None)
		if git_url is not None:
			location.gitPackageUrl = self.processGitUrl(name, git_url, cmDefaults.gitPackageUrl)

	def processProductDefinition(self, productName, cd):
		type = self.getYamlString(cd, 'type', default = None)
		nameList = self.getYamlStringList(cd, 'uses')

		product = ProductDefinition(productName, nameList, type = type)
		self.products.addProduct(product)

		product.extends = self.getYamlString(cd, 'extends', default = None)

		patternNames = self.getYamlStringList(cd, 'patterns', default = [])
		for name in patternNames:
			pattern = self.patterns.getPattern(name)
			if name is None:
				raise Exception(f"Product {productName} references non-existant pattern {name}")
			product.addPattern(pattern)

		product.exclude = self.getYamlStringList(cd, 'exclude', default = [])

	def processPatternDefinition(self, patternName, cd):
		pattern = PatternDefinition(patternName)
		pattern.summary = self.getYamlString(cd, 'summary')
		pattern.description = self.getYamlString(cd, 'description', default = "")
		self.patterns.addPattern(pattern)

		pattern.verbatim = self.getYamlString(cd, 'verbatim', default = None)

		self.processPatternPackageLists(pattern, cd)

		archData = self.getYamlDict(cd, 'architecture', default = {})
		for arch, archDef in archData.items():
			if archDef is None:
				continue
			condPattern = pattern.addConditional('arch', arch)
			self.processPatternPackageLists(condPattern, archDef)

	def processPatternPackageLists(self, pattern, cd):
		for type in pattern.NAME_LISTS:
			names = self.getYamlStringList(cd, type, default = [])
			setattr(pattern, type, names)

		names = self.getYamlStringList(cd, 'requires_patterns', default = [])
		pattern.requiresPatternNames = names

	def processProjectSettings(self, project, cd, parentProject = None):
		cmDefaults = self.defaultProjectSettings

		mode = cd.get('bootstrap')
		generation = self.getYamlString(cd, 'generation', default = None)
		bootstrap_repository = self.getYamlString(cd, 'bootstrap_repository', default = None)
		bootstrap_strategy = self.getYamlString(cd, 'bootstrap_strategy', default = None)
		prjconf = self.getYamlString(cd, 'prjconf', default = None)
		git_project = self.getYamlString(cd, 'git_project_url', default = None)
		git_package = self.getYamlString(cd, 'git_package_url', default = None)
		build_config = self.getYamlString(cd, 'build_config', default = 'model')
		build_requires = self.getYamlStringList(cd, 'build_requires', default = [])

		if mode is not None:
			if mode is True:
				mode = Model.COMPONENT_MODE_BOOTSTRAP
			elif mode is False:
				mode = Model.COMPONENT_MODE_REBUILD
			elif mode == 'self':
				mode = Model.COMPONENT_MODE_BOOTSTRAP_SELF
			else:
				raise Exception(f"Invalid setting bootstrap='{mode}' in definition of project {project}")

		if parentProject is not None:
			if mode is None:
				mode = parentProject.mode
			if generation is None:
				generation = parentProject.generation
			if bootstrap_repository is None:
				bootstrap_repository = parentProject.bootstrapRepository

		if project is not cmDefaults:
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
			git_project = self.processGitUrl(project, git_project, cmDefaults.gitProjectUrl)
		if git_package is not None:
			git_package = self.processGitUrl(project, git_package, cmDefaults.gitPackageUrl)

		if generation is None:
			raise Exception(f"Incomplete definition of OBS project {project}: missing generation")
		if bootstrap_repository is None:
			raise Exception(f"Incomplete definition of OBS project {project}: missing bootstrap_repository")
		if bootstrap_strategy not in Model.VALID_BOOTSTRAP_STRATEGIES:
			raise Exception(f"Incomplete definition of OBS project {project}: missing or invalid bootstrap_strategy={bootstrap_strategy}")
		if build_config not in Model.VALID_BUILD_CONFIG_STRATEGIES:
			raise Exception(f"Bad definition of OBS project {project}: invalid setting build_config={build_config}")

		project.mode = mode
		project.generation = generation
		project.bootstrapRepository = bootstrap_repository
		project.bootstrapStrategy = bootstrap_strategy
		project.projectConfigSnippet = prjconf
		project.gitProjectUrl = git_project
		project.gitPackageUrl = git_package
		project.buildConfigStrategy = build_config
		project.buildRequires = set(build_requires)

		project.description = cd.get('description')
		project.contract = self.processContract(cd.get('contract'))

		# print(f"Define {project} mode={mode} generation={generation} bsr={project.bootstrapRepository} bss={project.bootstrapStrategy} bcs={project.buildConfigStrategy} git={project.gitProjectUrl}")
		return project

	def processContract(self, data):
		defaultContract = None
		if self.defaultProjectSettings:
			defaultContract = self.defaultProjectSettings.contract

		if defaultContract is not None:
			contract = defaultContract.copy()
		else:
			contract = ContractDefinition()

		def processContractData(part, data):
			for key, value in data.items():
				if type(value) in (str, int, bool, float):
					part.set(key, value)
				elif type(value) is dict:
					processContractData(part.subClause(key, create = True), value)
				else:
					raise Exception(f"Invalid key/value type in contract: {key}={value}")

		if data is not None:
			processContractData(contract.root, data)
		return contract

	def processPackageArchInfo(self, data):
		for entry in data:
			for name, values in entry.items():
				assert(type(name) is str)
				assert(type(values) is list)
				self.archMap.add(name, values)

	@classmethod
	def xxx_processExports(klass, component, data):
		for name, values in data.items():
			export = component.addExport(name)
			if type(values) == str:
				export.add(values)
			elif type(values) == list:
				for topic in values:
					assert(type(topic) is str)
					export.add(topic)

	def processProject(self, project, cd, **kwargs):
		self.processProjectSettings(project, cd, **kwargs)

		componentNames = self.getYamlStringList(cd, 'components', default = [])
		project.componentNames = componentNames.copy()

		# FIXME: complain about a project w/o components unless it's a workbench project

		names = self.getYamlStringList(cd, 'extra_packages', default = [])
		project.extraPackages = names.copy()

		project.imports = self.getYamlStringList(cd, 'imports', default = [])
		project.requiresNames = self.getYamlStringList(cd, 'requires', default = [])
		project.buildRequiresNames = self.getYamlStringList(cd, 'build_requires', default = [])

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

