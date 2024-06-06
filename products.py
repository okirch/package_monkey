##################################################################
#
# Classes and functions related to product definitions
#  - name, version, architecture
#  - repo-md URLs
#  - OBS projects and URLs
#
##################################################################
import yaml
import os
from resolver import ResolverHints
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

from repos import Repo

productLogger = loggingFacade.getLogger('products')
productDebug = productLogger.debug

class CacheLocation:
	def __init__(self, path):
		self.path = path

class CacheStrategyStripURL:
	def __init__(self, repoURL, cacheLocation, urlRewriter = None):
		if repoURL.endswith('.repo'):
			i = repoURL.rfind('/')
			baseURL = repoURL[:i].rstrip('/')
		else:
			baseURL = repoURL

		productDebug(f"CacheStrategyStripURL: {repoURL} -> {baseURL}")
		self.baseURL = baseURL
		self.cacheLocation = cacheLocation
		self.urlRewriter = urlRewriter
	
	def cachePath(self, url):
		# infomsg(f"cachePath({url}) called")
		if self.urlRewriter is not None:
			url = self.urlRewriter.rewrite(url)
			# infomsg(f" rewrite {url}")
		if url.startswith(self.baseURL):
			url = url[len(self.baseURL):]
			url = url.lstrip('/')
		else:
			barf

		path = os.path.join(self.cacheLocation.path, url)
		# infomsg(f" => {path}")
		return path

class UrlRewriter:
	def __init__(self):
		self._rules = []

	def addRule(self, fromURL, toURL):
		self._rules.append([fromURL, toURL])

	def rewrite(self, url):
		for fromURL, toURL in self._rules:
			if url.startswith(fromURL):
				result = toURL
				if not result.endswith('/'):
					result += '/'
				result += url[len(fromURL) : ].lstrip('/')
				return result
		return url

class RepoCollection:
	def __init__(self, baseURL, urlpatterns):
		self.baseURL = baseURL
		self.urlpatterns = urlpatterns

	def getRepoURLs(self, obsname, version, arch):
		result = []

		for url in self.urlpatterns:
			url = url.replace('$OBSNAME', obsname).replace('$VERSION', version).replace('$ARCH', arch)

			if not url.startswith('https:') and not url.startswith('http:'):
				url = self.baseURL + url

			result.append(url)

		return result

class BuildServiceCollection:
	def __init__(self):
		self.sourceProjects = []
		self.buildProjects = []

	def expand(self, version):
		fn = lambda name: name.replace('$VERSION', version)

		result = BuildServiceCollection()
		result.sourceProjects = map(fn, self.sourceProjects)
		result.buildProjects = map(fn, self.buildProjects)
		return result

class RepoService:
	def __init__(self, baseURL, cacheLocation = None):
		self.baseURL = baseURL
		self.urlRewriter = UrlRewriter()

		productDebug(f"Using cache location {cacheLocation}")
		cacheStrategy = None
		if cacheLocation is not None:
			cacheStrategy = CacheStrategyStripURL(self.baseURL, cacheLocation, urlRewriter = self.urlRewriter)
		productDebug(f"Using cache strategy {cacheStrategy}")

		self.cacheStrategy = cacheStrategy

		self.repoCollection = {}
		self.obsCollection = {}

	def addVersion(self, version, repositories, projects):
		self.repoCollection[version] = repositories
		self.obsCollection[version] = projects

	def getRepoURLs(self, obsname, version, arch):
		collection = self.repoCollection.get(version)
		if collection is None:
			return []

		return collection.getRepoURLs(obsname, version, arch)

	def getOBSProjects(self, obsname, version, arch):
		obs = self.obsCollection.get(version)
		if obs is None:
			return BuildServiceCollection()

		return obs.expand(version)

class ProductFamily:
	def __init__(self, name, cacheLocation):
		self.name = name
		self.cacheLocation = cacheLocation
		self.repoDef = None
		self.database = None
		self.resolverHints = None
		self.loaded = False

	def __str__(self):
		return self.name

	def load(self):
		if self.loaded:
			return

		assert(self.repoDef)
		infomsg(f"Loading product definition for family {self.name} from {self.repoDef}")
		with open(self.repoDef) as f:
			data = yaml.full_load(f)

		baseurl = data['baseurl']
		if not baseurl.endswith('/'):
			baseurl += '/'
		self.baseurl = baseurl

		self.service = RepoService(baseurl, self.cacheLocation)
		if 'alternateurls' in data:
			rewriter = self.service.urlRewriter
			for altURL in data['alternateurls']:
				rewriter.addRule(altURL, baseurl)

		self.architectures = data['architectures']
		self.repositories = self.expandRepositories(data)
		self.projects = self.expandProjects(data)

		hints = data.get('resolverhints')
		if hints is not None:
			self.resolverHints = ResolverHints()
			fake = hints.get('fake')
			if fake is not None:
				self.expandFakeDependencies(fake)
			ignore = hints.get('ignore')
			if ignore is not None:
				self.expandIgnoredDependencies(ignore)
			rewrite = hints.get('rewrite')
			if rewrite is not None:
				self.expandDependencyRewrites(rewrite)
			packages = hints.get('packages')
			if packages is not None:
				self.expandPackageRules(packages)

			disambiguate = hints.get('disambiguate')
			if disambiguate is not None:
				self.expandDisambiguationRules(disambiguate)
			self.resolverHints.finalize()

		self.versions = []
		for vd in data['versions']:
			name = vd['name']

			repositories = self.expandRepositories(data)
			if repositories is None:
				repositories = self.repositories
			if repositories is None:
				raise Exception(f"No repositories defined for {name}")

			projects = self.expandProjects(data)
			if projects is None:
				projects = self.projects
			if projects is None:
				warnmsg(f"No repositories defined for {name}")

			self.service.addVersion(name, repositories, projects)
			self.versions.append(name)

		self._products = []
		for pd in data['products']:
			self._products.append(Product.fromYAML(pd, self))

		self.loaded = True

	def expandRepositories(self, data):
		urlpatterns = data.get('repositories')
		if urlpatterns is None:
			return None

		return RepoCollection(self.baseurl, urlpatterns)

	def expandProjects(self, data):
		data = data.get('buildservice')
		if data is None:
			return None

		info = BuildServiceCollection()
		info.sourceProjects = data.get('source') or []
		info.buildProjects = data.get('build') or []
		return info

	def expandFakeDependencies(self, data):
		for name in data:
			assert(type(name) is str)
			self.resolverHints.addFakeDependency(name)

	def expandResolverPreferences(self, data):
		xxx

	def expandIgnoredDependencies(self, data):
		for expr in data:
			if '->' in expr:
				(sourceNames, targetNames) = expr.split('->')
				sourceNames = sourceNames.split()
				targetNames = targetNames.split()
				for packageName in sourceNames:
					for targetName in targetNames:
						self.resolverHints.addIgnoredDependency(packageName, targetName)
			else:
				targetNames = expr.split()
				for targetName in targetNames:
					self.resolverHints.addIgnoredDependency('*', targetName)

	def expandDependencyRewrites(self, data):
		for expr in data:
			if '->' not in expr:
				raise Exception(expr)

			(fromName, toName) = expr.split('->')
			self.resolverHints.addDependencyRewrite(fromName.strip(), toName.strip())

	def expandPackageRules(self, data):
		for item in data:
			name = item['name']

			warning = item.get('warning')

			ignore = item.get('ignore')
			if ignore is not None:
				if type(ignore) is str:
					self.resolverHints.addIgnoredDependency(name, ignore)
				else:
					for targetName in ignore:
						self.resolverHints.addIgnoredDependency(name, targetName, warning = warning)

	def expandDisambiguationRules(self, ruleSetData):
		for pos in range(len(ruleSetData)):
			ruleData = ruleSetData[pos]
			if type(ruleData) is not dict:
				raise Exception(f"Invalid rule #{pos} in disambiguation resolver hints: not a dict")

			acceptData = ruleData.get('acceptable')
			collapseData = ruleData.get('collapse')
			hideData = ruleData.get('hide')

			n = 0
			for data in (acceptData, collapseData, hideData):
				if data:
					n += 1
			if n != 1:
				raise Exception(f"Invalid rule #{pos} in disambiguation resolver hints: expect exactly one of: accept, collapse, hide")

			if acceptData:
				if type(acceptData) is not list:
					raise Exception(f"Invalid rule #{pos} in disambiguation resolver hints: acceptable should be a list of strings")

				rule = self.resolverHints.addAcceptableRule(acceptData)

			if collapseData:
				if type(collapseData) is not str:
					raise Exception(f"Invalid rule #{pos} in disambiguation resolver hints: collapse should be a string")
				target = collapseData
				aliases = []
				anyAlias = False

				aliasData = ruleData.get('alias')
				if aliasData is not None:
					aliases.append(aliasData)
				aliasData = ruleData.get('aliases')
				if aliasData is not None:
					aliases += aliasData
				aliasData = ruleData.get('anyalias')
				if aliasData is not None:
					aliases += aliasData
					anyAlias = True

				rule = self.resolverHints.addCollapsingRule(target, aliases, anyAlias = anyAlias)

			if hideData:
				if type(hideData) is not list:
					raise Exception(f"Invalid rule #{pos} in disambiguation resolver hints: hide should be a list of strings")

				rule = self.resolverHints.addHideRule(hideData)

			rpmData = ruleData.get('context')
			if rpmData:
				rule.setContextPackage(rpmData)

	def enumerate(self, **args):
		if args.get('version') == 'latest':
			args['version'] = self.versions[-1]

		result = []
		for prod in self._products:
			result += prod.enumerate(**args)
		return result

	def enumerateLatest(self, **args):
		return self.enumerate(version = self.versions[-1], **args)

	def updateBackingStore(self, store):
		# Ensure we've loaded the product versions etc
		self.load()

		for release in self.enumerate():
			id = store.mapProduct(release)
			assert(id is not None)
			release.backingStoreId = id

class ProductCatalog:
	def __init__(self, filename = "catalog.yaml", cacheLocation = None):
		with open(filename) as f:
			data = yaml.full_load(f)

		self.families = []
		for fe in data['product_families']:
			family = ProductFamily(fe['family'], cacheLocation)
			family.repoDef = fe.get('repos')
			family.database = fe.get('database')
			self.families.append(family)

	def enumerate(self, family = None, **args):
		familyName = family

		if familyName:
			family = self.select(familyName)
			if family is None:
				raise Exception(f"Unknown product family {familyName}")
			return family.enumerate(**args)

		result = []
		for family in self.families:
			family.load()
			result += family.enumerate(**args)
		return result

	def select(self, familyName):
		familyName = familyName.casefold()
		for family in self.families:
			if family.name.casefold() == familyName:
				family.load()
				return family

		return None

	def updateBackingStore(self, store):
		for family in self.families:
			family.updateBackingStore(store)

##################################################################
# We should rename this to something less generic, eg ProductVersion
# ProductRelease -> ProductReleaseArchitecture
# Product -> ProductRelease
##################################################################
class Product:
	def __init__(self, catalog, name, nickname, urlpattern = None, obsname = None, **ignore):
		self.name = name
		self.nickname = nickname
		self.obsname = obsname
		self._releases = []
		self.service = catalog.service
		self.resolverHints = catalog.resolverHints

	@staticmethod
	def fromYAML(pd, catalog):
		prod = Product(catalog, **pd)

		if pd.get('releases') is not None:
			for rd in pd['releases']:
				if rd.get('arch') is None:
					versions = [rd['version']]
					prod.addMultipleReleases(versions, catalog.architectures)
				else:
					prod.addRelease(**rd)
		else:
			versions = pd.get('versions') or catalog.versions
			architectures = pd.get('architectures') or catalog.architectures
			prod.addMultipleReleases(versions, architectures)

		return prod

	def addMultipleReleases(self, versions, architectures):
		for version in versions:
			for arch in architectures:
				rel = self.addRelease(version = version, arch = arch)

	def addRelease(self, version, arch):
		repoURLs = self.service.getRepoURLs(self.obsname, version, arch)

		release = ProductRelease(self.obsname, version, arch, repoURLs, service = self.service, resolverHints = self.resolverHints)
		self._releases.append(release)

		release.obsProjects = self.service.getOBSProjects(self.obsname, version, arch)

		return release

	def enumerate(self, **args):
		result = []
		for rel in self._releases:
			if rel.match(**args):
				result.append(rel)
		return result

class ProductRelease:
	def __init__(self, name, version, arch, repoURLs, service = None, resolverHints = None):
		self.name = name
		self.version = version
		self.arch = arch
		self.productId = None
		self.repoURLs = repoURLs
		self.obsProjects = None
		self.service = service
		self.resolverHints = resolverHints

		self.backingStoreId = None
		self.cachedRepos = None

	def __str__(self):
		return f"{self.name} {self.version} ({self.arch})"

	def createEmptyProduct(self):
		from packages import Product

		result = Product(resolverHints = self.resolverHints)
		result.setNameAndVersion(self.name, self.version, self.arch)
		result.productId = self.backingStoreId

		return result

	def match(self, version = None, arch = None):
		if version is not None and self.version != version:
			return False
		if arch is not None and self.arch != arch:
			return False
		return True

	def getRepositories(self):
		repoList = self.cachedRepos
		if repoList is None:
			repoList = []
			for url in self.repoURLs:
				repo = Repo(url, self.service.cacheStrategy)
				repo.name = self.name
				repo.version = self.version
				repo.arch = self.arch
				repo.productId = self.productId
				repoList.append(repo)

			self.cachedRepos = repoList

		return repoList

	@property
	def cachePath(self):
		if self.service is None:
			return None

		return self.service.cacheStrategy.cachePath(self.repoURL)

	@property
	def sourceProjects(self):
		if self.obsProjects is None:
			return []
		return self.obsProjects.sourceProjects

	@property
	def buildProjects(self):
		if self.obsProjects is None:
			return []
		return self.obsProjects.buildProjects

if False:
	cat = ProductCatalog()

	for rel in cat.enumerate(version = '15-SP2', arch = 'x86_64'):
		infomsg(f"Found {rel}")
