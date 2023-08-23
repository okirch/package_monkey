import yaml
import os

from repos import Repo

optDebugProduct = False

def productDebug(*args, **kwargs):
	if optDebugProduct:
		print(*args, **kwargs)

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
		# print(f"cachePath({url}) called")
		if self.urlRewriter is not None:
			url = self.urlRewriter.rewrite(url)
			# print(f" rewrite {url}")
		if url.startswith(self.baseURL):
			url = url[len(self.baseURL):]
			url = url.lstrip('/')
		else:
			barf

		path = os.path.join(self.cacheLocation.path, url)
		# print(f" => {path}")
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

	def addVersion(self, version, repositories):
		self.repoCollection[version] = repositories

	def getRepoURLs(self, obsname, version, arch):
		collection = self.repoCollection.get(version)
		if collection is None:
			return []

		return collection.getRepoURLs(obsname, version, arch)

class ProductFamily:
	def __init__(self, name, cacheLocation):
		self.name = name
		self.cacheLocation = cacheLocation
		self.repoDef = None
		self.database = None
		self.loaded = False

	def load(self):
		if self.loaded:
			return

		assert(self.repoDef)
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

		self.versions = []
		for vd in data['versions']:
			name = vd['name']

			repositories = self.expandRepositories(data)
			if repositories is None:
				repositories = self.repositories
			if repositories is None:
				raise Exception("No repositories defined for {name}")

			self.service.addVersion(name, repositories)
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

		release = ProductRelease(self.obsname, version, arch, repoURLs, self.service)
		self._releases.append(release)

		return release

	def enumerate(self, **args):
		result = []
		for rel in self._releases:
			if rel.match(**args):
				result.append(rel)
		return result

class ProductRelease:
	def __init__(self, name, version, arch, repoURLs, service = None):
		self.name = name
		self.version = version
		self.arch = arch
		self.productId = None
		self.repoURLs = repoURLs
		self.service = service

		self.backingStoreId = None
		self.cachedRepos = None

	def __str__(self):
		return f"{self.name} {self.version} ({self.arch})"

	def createEmptyProduct(self):
		from packages import Product

		result = Product()
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

if False:
	cat = ProductCatalog()

	for rel in cat.enumerate(version = '15-SP2', arch = 'x86_64'):
		print(f"Found {rel}")
