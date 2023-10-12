#
# repo handling classes
#

import gzip
import xml.etree.ElementTree as ET
import urllib.parse
import os.path
import os
from packages import Package
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

cacheLogger = loggingFacade.getLogger('cache')
debugCache = cacheLogger.debug

class xmlNamespaceSchema:
	def __init__(self, ns, *tags):
		for t in tags:
			setattr(self, t + 'Tag', ns + t)

repomdSchema = xmlNamespaceSchema('{http://linux.duke.edu/metadata/repo}', 'data', 'location')
primarySchema = xmlNamespaceSchema('{http://linux.duke.edu/metadata/common}', 'package', 'name', 'version', 'arch', 'time', 'format', 'checksum')
otherSchema = xmlNamespaceSchema('{http://linux.duke.edu/metadata/other}', 'package', 'version', 'changelog')
filesSchema = xmlNamespaceSchema('{http://linux.duke.edu/metadata/filelists}', 'package', 'version', 'file')
rpmSchema = xmlNamespaceSchema('{http://linux.duke.edu/metadata/rpm}', 'sourcerpm', 'group', 'requires', 'provides', 'recomments', 'suggests', 'conflicts', 'entry')


def retrieveURLRaw(url):
	import urllib3

	urllib3.disable_warnings()

	http = urllib3.PoolManager()
	r = http.request('GET', url)
	if r.status != 200:
		raise ValueError("HTTP status %d when retrieving %s" % (r.status, url))

	return r

class UrlCacheStrategy:
	def __init__(self, basedir, includeScheme = False, includeHost = True, stripDirs = 0):
		self.basedir = basedir
		self.includeScheme = includeScheme
		self.includeHost = includeHost
		self.stripDirs = stripDirs

	def cachePath(self, url):
		o = urllib.parse.urlparse(url)

		result = self.basedir
		if result:
			result += '/'

		if self.includeHost:
			if self.includeScheme:
				result += o.scheme + ':'
			result += o.netloc

		if self.stripDirs == 0:
			return result + o.path

		path = o.path.lstrip('/').split('/', self.stripDirs)
		if len(path) != self.stripDirs + 1:
			raise ValueError("UrlCacheStrategy: cannot build cache path for \"%s\" - not enough directory levels" % url)

		return result + path[-1]

class RepoDict:
	def __init__(self, cacheStrategy):
		self._cacheStrategy = cacheStrategy
		self._repos = {}

	def get(self, url):
		r = self._repos.get(url)
		if r is None:
			r = Repo(url, self._cacheStrategy)
			self._repos[url] = r
		return r

class RepoCache:
	class CachedObject:
		def __init__(self, url, localPath):
			self.url = url
			self.localPath = localPath
			self.valid = False
			self.data = None

			self._children = []
			self._childrenTagDict = {}

		def addChild(self, child, tag = None):
			self._children.append(child)
			if tag:
				self._childrenTagDict[tag] = child

		def getChildByTag(self, tag):
			return self._childrenTagDict.get(tag)

		def tags(self):
			return self._childrenTagDict.keys()

		def dropTagged(self, tag):
			if not tag in self._childrenTagDict:
				return False

			o = self._childrenTagDict[tag]
			del self._childrenTagDict[tag]

			o.invalidate()
			del self._children[self._children.index(o)]
			return True

		def invalidate(self):
			# Drop the local file
			if self.localPath:
				debugCache(f"Dropping {self.localPath} from cache")
				try:
					os.remove(self.localPath)
				except: pass
			self.valid = False

		def updateUrl(self, url, localPath):
			if self.url != url or self.localPath != localPath:
				debugCache("URL change %s -> %s" % (self.url, url))
				self.invalidate()
				self.url = url
				self.localPath = localPath

		def retrieveFromCache(self):
			path = self.localPath
			if path is None:
				return None
			if not os.path.isfile(path):
				return None

			debugCache("Loading %s from cache" % self.localPath)
			if path.endswith(".gz"):
				f = gzip.open(path, "r")
			else:
				f = open(path, "r")
			self.data = f.read()
			self.valid = True

			return self.data

		def retrieve(self):
			if not self.valid:
				self.retrieveFromCache()
			if not self.valid:
				debugCache("Retrieving %s" % self.url)

				response = retrieveURLRaw(self.url)
				data = response.data
				if not data:
					fart

				if self.localPath:
					debugCache("Write to cache file %s" % self.localPath)
					dirname = os.path.dirname(self.localPath)
					if not os.path.isdir(dirname):
						os.makedirs(dirname)

					with open(self.localPath, "wb") as f:
						f.write(data)

				if response.headers.get('content-type') == 'application/x-gzip':
					data = gzip.decompress(data)

				self.data = data.decode("utf-8")
			return self.data

		def retrieveText(self, splitLines = False):
			result = self.retrieve()
			if splitLines:
				result = result.split('\n')
			return result

		def retrieveXML(self):
			return ET.fromstring(self.retrieve())

	def __init__(self, cacheStrategy = None):
		self._cacheStrategy = cacheStrategy
		self._rootObject = None

	def setRootObject(self, object):
		self._rootObject = object
		return self._rootObject

	def rootObject(self):
		return self._rootObject

class Repo:
	REFRESH_NOT = 0
	REFRESH_MAYBE = 1
	REFRESH_ALWAYS = 2

	def __init__(self, url, cacheStrategy = None):
		self._url = url
		self._cacheStrategy = cacheStrategy
		self._cache = RepoCache(self._cacheStrategy)
		self.clear()

		self.name = None
		self.version = None
		self.arch = None
		self.productId = None
		self.isUpdateRepo = False

		self.unresolvedSources = None

	def clear(self):
		self._name = None
		self._type = None
		self._baseurl = None

	def refresh(self, forceRefresh = False):
		self.refreshRepoInfo(forceRefresh)

	def cachePath(self, url):
		if self._cacheStrategy:
			return self._cacheStrategy.cachePath(url)
		return None

	def makeCachedObject(self, url):
		return RepoCache.CachedObject(url, self.cachePath(url))

	def makeAbsoluteUrl(self, relativePath):
		return self._baseurl + '/' + relativePath

	# [SUSE_Updates_SLE-Product-RT_15-SP1_x86_64]
	# name=SUSE:Updates:SLE-Product-RT:15-SP1:x86_64 (update)
	# type=rpm-md
	# baseurl=http://download.suse.de/ibs/SUSE/Updates/SLE-Product-RT/15-SP1/x86_64/update/
	# gpgcheck=1
	# gpgkey=http://download.suse.de/ibs/SUSE/Updates/SLE-Product-RT/15-SP1/x86_64/update/repodata/repomd.xml.key
	# enabled=1
	def refreshRepoInfo(self, forceRefresh = False):
		self.clear()

		if not self._url.endswith('.repo'):
			self._type = 'rpm-md';
			self._baseurl = self._url
			self._name = self._url
			self.refreshRepoMD(forceRefresh)
			return

		o = self.makeCachedObject(self._url)
		o.retrieveFromCache()

		if forceRefresh:
			o.invalidate()

		try:
			data = o.retrieveText(splitLines = True)
		except:
			infomsg("Repository %s does not seem to exist" % self._url)
			self._cache.setRootObject(None)
			return

		for l in data:
			if not l or l[0] == '[':
				continue

			if not '=' in l:
				continue

			# FIXME: use a dict instead
			(name, value) = l.split('=', 1)
			if name in ('name', 'type', 'baseurl'):
				setattr(self, '_' + name, value)

		if not self._name or not self._type or not self._baseurl:
			raise ValueError("%s: incomplete information in .repo file" % self._url)

		# FIXME: Introduce a repoBackend class that does all the type specific
		# refresh and parsing for us
		if self._type == 'rpm-md':
			self.refreshRepoMD(forceRefresh)
		else:
			raise ValueError("%s: unable to refresh repo of type %s" % (self._url, self._type))

	def refreshRepoMD(self, forceRefresh = False):
		rootObject = self.makeCachedObject(self.makeAbsoluteUrl('repodata/repomd.xml'))
		self._cache.setRootObject(rootObject)
		rootObject.retrieveFromCache()
		if forceRefresh:
			rootObject.invalidate()

		try:
			repomd = rootObject.retrieveXML()
		except:
			infomsg("%s does not seem to exist" % rootObject.url)
			self._cache.setRootObject(None)
			return

		updatedTags = []
		for node in repomd.findall(repomdSchema.dataTag):
			type = node.attrib['type']

			locNode = node.find(repomdSchema.locationTag)

			url = self.makeAbsoluteUrl(locNode.attrib['href'])
			o = rootObject.getChildByTag(type)
			if o is not None:
				o.updateUrl(url, self.cachePath(url))
			else:
				o = self.makeCachedObject(url)
				rootObject.addChild(o, type)

			if forceRefresh:
				o.valid = False

			updatedTags.append(type)

		for tag in rootObject.tags():
			if tag not in updatedTags:
				rootObject.dropTagged(tag)

	def load(self, product, refresh = REFRESH_MAYBE):
		if refresh != Repo.REFRESH_NOT:
			self.refresh(refresh == Repo.REFRESH_ALWAYS)

		# This product/module does not exist
		if self._cache.rootObject() == None:
			return

		self.loadPrimary(product)
		self.loadOther(product)
		self.loadFiles(product)

	def retrieveXML(self, tag):
		rootObject = self._cache.rootObject()
		o = rootObject.getChildByTag(tag)
		if not o:
			raise ValueError("%s: Cannot find %s data" % (self._url, tag, ));

		return o.retrieveXML()

	class DelayedSourceAttribution(dict):
		def add(self, name, binaryPkg):
			binaries = self.get(name)
			if binaries is None:
				binaries = []
				self[name] = binaries
			binaries.append(binaryPkg)

	def loadPrimary(self, product):
		product.setNameAndVersion(self.name, self.version, self.arch)

		if self.unresolvedSources is None:
			self.unresolvedSources = self.DelayedSourceAttribution()
		unresolved = self.unresolvedSources

		root = self.retrieveXML('primary')
		for node in root.findall(primarySchema.packageTag):
			name = node.find(primarySchema.nameTag).text.strip()
			arch = node.find(primarySchema.archTag).text.strip()
			(epoch, version, release) = Repo.processVersionNode(node.find(primarySchema.versionTag))

			pkg = Package(name, version, release, arch)
			pkg.repo = self;

			if epoch != "0":
				pkg.epoch = epoch;

			timeNode = node.find(primarySchema.timeTag)
			if timeNode is not None:
				pkg.buildTime = int(timeNode.attrib['build'])
			else:
				warnmsg("%s: build time not defined" % pkg.fullname())
				pkg.buildTime = 0

			pkgid = None
			for csumNode in node.findall(primarySchema.checksumTag):
				if csumNode.attrib.get('pkgid') == 'YES':
					pkgid = csumNode.text.strip()

			if pkgid is None:
				infomsg(f"No pkgid for {pkg.fullname()}")
			else:
				pkg.pkgid = pkgid

			fmtNode = node.find(primarySchema.formatTag)
			if pkg.arch != 'src' and pkg.arch != 'nosrc':
				srcNode = fmtNode.find(rpmSchema.sourcerpmTag)
				if srcNode is not None and srcNode.text is not None:
					pkg.sourceName = srcNode.text.strip()
					unresolved.add(pkg.sourceName, pkg)
				else:
					infomsg("%s: No sourcepackage defined" % pkg.fullname())

			depNode = fmtNode.find(rpmSchema.requiresTag)
			pkg.requires = Repo.processDepNode(pkg, depNode)

			depNode = fmtNode.find(rpmSchema.providesTag)
			pkg.provides = Repo.processDepNode(pkg, depNode)

			groupNode = fmtNode.find(rpmSchema.groupTag)
			if groupNode is not None and groupNode.text is not None:
				pkg.group = groupNode.text.strip()
			else:
				infomsg("--- %s: no pkg group" % pkg.fullname())

			# Add the package after having it fully parsed
			product.addPackage(pkg)

	def resolveSourcePackages(self, product):
		unresolved = self.unresolvedSources
		if unresolved is None:
			return

		for name, binaries in unresolved.items():
			src = product.findSource(name)
			if src is None:
				# Create a "fake" entry
				src = product.findSource(name, create = True)

				if src.arch == 'nosrc':
					continue

				infomsg(f"{product.fullname} does not provide source package {name}; faking it.")

				import hashlib

				h = hashlib.sha256()
				h.update(name.encode('utf-8'))
				src.pkgid = "fake:" + h.hexdigest()

			src.repo = self
			for pkg in binaries:
				pkg.setSourcePackage(src)

	def loadOther(self, product):
		root = self.retrieveXML('other')
		for node in root.findall(otherSchema.packageTag):
			name = node.attrib['name']
			arch = node.attrib['arch']

			(epoch, version, release) = Repo.processVersionNode(node.find(otherSchema.versionTag))

			pkg = product.findPackage(name, version, release, arch)
			if not pkg:
				infomsg("%s: cannot find pkg %s-%s-%s.%s.rpm" % (self, name, version, release, arch))
				continue

			changes = []
			for lognode in node.findall(otherSchema.changelogTag):
				author = lognode.attrib['author']
				date = lognode.attrib['date']
				changes.append(pkg.Change(date, author, lognode.text.strip()))

			pkg.setChanges(changes)

	def loadFiles(self, product):
		root = self.retrieveXML('filelists')
		for node in root.findall(filesSchema.packageTag):
			pkgid = node.attrib['pkgid']
			pkg = product.findPackageByID(pkgid)
			if pkg is None:
				name = node.attrib['name']
				arch = node.attrib['arch']

				(epoch, version, release) = Repo.processVersionNode(node.find(filesSchema.versionTag))

				pkg = product.findPackage(name, version, release, arch)
				if not pkg:
					infomsg("%s: cannot find pkg %s-%s-%s.%s.rpm" % (self, name, version, release, arch))
					continue

			files = []
			for fileNode in node.findall(filesSchema.fileTag):
				files.append(fileNode.text.strip())

			product.updatePackageFilesList(pkg, files)

	@staticmethod
	def processDepNode(pkg, dnode):
		result = []
		if dnode is not None:
			# Convert "{http://bla}requires" to "requires"
			i = dnode.tag.index('}')
			type = dnode.tag[i + 1:]

			for entry in dnode.findall(rpmSchema.entryTag):
				a = entry.attrib
				if type == 'provides' and 'flags' not in a:
					dep = Package.createDependency(name = a['name'], flags = 'EQ', ver = pkg.version, rel = pkg.release)
				else:
					dep = Package.createDependency(**a)
				result.append(dep)
		return result

	@staticmethod
	def processVersionNode(vnode):
		epoch = vnode.attrib['epoch']
		version = vnode.attrib['ver']
		release = vnode.attrib['rel']
		return (epoch, version, release)

	@staticmethod
	def readXML(path):
		if path.endswith(".gz"):
			f = gzip.open(path, "r")
		else:
			f = open(path, "r")

		tree = ET.parse(f)
		root = tree.getroot()

		return root

