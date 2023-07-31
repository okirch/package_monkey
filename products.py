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

		self.urlpattern = {}

	def addVersion(self, version, urlpattern):
		self.urlpattern[version] = urlpattern

	def getVersionURL(self, version, arch):
		url = self.urlpattern[version]
		url = url.replace('$VERSION', version).replace('$ARCH', arch)

		if not url.startswith('https:') and not url.startswith('http:'):
			url = self.baseURL + url

		return url

class ProductCatalog:
	def __init__(self, filename = "products.yaml", cacheLocation = None):
		with open(filename) as f:
			data = yaml.load(f)

		baseurl = data['baseurl']
		if not baseurl.endswith('/'):
			baseurl += '/'
		self.service = RepoService(baseurl, cacheLocation)
		if 'alternateurls' in data:
			rewriter = self.service.urlRewriter
			for altURL in data['alternateurls']:
				rewriter.addRule(altURL, baseurl)

		self.architectures = data['architectures']

		self.versions = []
		for vd in data['versions']:
			name = vd['name']
			self.service.addVersion(name, vd['urlpattern'])
			self.versions.append(name)

		self._products = []
		for pd in data['products']:
			self._products.append(Product.fromYAML(pd, self))

	def enumerate(self, **args):
		result = []
		for prod in self._products:
			result += prod.enumerate(**args)
		return result

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
		url = self.service.getVersionURL(version, arch)
		url = url.replace('$OBSNAME', self.obsname)

		release = ProductRelease(self.obsname, version, arch, url, self.service)
		self._releases.append(release)

		return release

	def enumerate(self, **args):
		result = []
		for rel in self._releases:
			if rel.match(**args):
				result.append(rel)
		return result

class ProductRelease:
	def __init__(self, name, version, arch, repoURL, service = None):
		self.name = name
		self.version = version
		self.arch = arch
		self.repoURL = repoURL
		self.service = service

		self.cachedRepo = None

	def __str__(self):
		return f"{self.name} {self.version} ({self.arch})"

	def match(self, version = None, arch = None):
		if version is not None and self.version != version:
			return False
		if arch is not None and self.arch != arch:
			return False
		return True

	def getRepository(self):
		repo = self.cachedRepo
		if repo is None:
			repo = Repo(self.repoURL, self.service.cacheStrategy)
			repo.name = self.name
			repo.version = self.version
			self.arch = self.arch
			self.cachedRepo = repo

		return repo

	@property
	def cachePath(self):
		if self.service is None:
			return None

		return self.service.cacheStrategy.cachePath(self.repoURL)

if False:
	cat = ProductCatalog()

	for rel in cat.enumerate(version = '15-SP2', arch = 'x86_64'):
		print(f"Found {rel}")
