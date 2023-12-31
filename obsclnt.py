import osc
import os
import xmltree
import posix
import time

from packages import Package, PackageInfo, PackageInfoFactory
from util import ChunkingQueue, ThatsProgress
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

obsLogger = loggingFacade.getLogger('obs')
cacheLogger = loggingFacade.getLogger('cache')

def debugOBS(msg, *args, prefix = None, **kwargs):
	if prefix:
		msg = f"[{prefix}] {msg}"
	obsLogger.debug(msg, *args, **kwargs)

def debugCache(*args, **kwargs):
	cacheLogger.debug(*args, **kwargs)

class OBSSchema(object):
	class Dummy:
		pass

	def checkRootNodeTag(self, xmlnode, expectTag):
		if xmlnode is None:
			return False
		if xmlnode.tag != expectTag:
			errormsg("OBS: unexpected root node <%s> in response (expected <%s>)" % (xmlnode.tag, expectTag));
			return False

		return True

	def unexpectedElement(self, node, parent = None):
		if parent:
			infomsg("ignoring unexpected element <%s> as child of <%s>" % (node.tag, parent.tag))
		else:
			infomsg("ignoring unexpected element <%s>" % node.tag)


	# Process result of querying builddepinfo
	# /build/SUSE\:SLE-15\:GA/standard/x86_64/_builddepinfo
	# Note, builddepinfo for maintenance updates is found in places like
	# /build/SUSE:Maintenance:10229/SUSE_SLE-12-SP3_Update/x86_64/_builddepinfo
	def processBuildDepInfo(self, xmlnode):
		if not self.checkRootNodeTag(xmlnode, 'builddepinfo'):
			return None

		result = []

		for child in xmlnode:
			if child.tag == 'cycle':
				continue

			if child.tag != 'package':
				self.unexpectedElement(child, xmlnode)
				continue

			info = OBSBuildInfo(child.attrib['name'])
			for grandchild in child:
				value = grandchild.text.strip()
				if value and value.startswith('%'):
					# infomsg(f"_builddepinfo for package {info.name} contains invalid subpkg name \"{value}\"")
					info.needs_fixup = True
					continue

				if grandchild.tag == 'source':
					info.source = value
				elif grandchild.tag == 'subpkg':
					info.binaries.append(value)
				elif grandchild.tag == 'pkgdep':
					info.buildRequires.append(value)
				else:
					self.unexpectedElement(grandchild, child)

			result.append(info)
		return result

	# Process result of querying build results
	# /build/SUSE\:SLE-15\:GA/_result
	def processBuildResult(self, xmlnode):
		if not self.checkRootNodeTag(xmlnode, 'resultlist'):
			return None

		result = []
		for child in xmlnode:
			if child.tag != 'result':
				self.unexpectedElement(child, xmlnode)
				continue

			info = self.processSimpleXML(child, ('project', 'repository', 'arch'), ['code', 'state'])
			result.append(info)

			info.status_list = []
			info.binary_list = []

			for grandchild in child:
				if grandchild.tag == 'status':
					st = self.processSimpleXML(grandchild, ('package', ), ('code', 'details', ))
					info.status_list.append(st)
				elif grandchild.tag == 'binarylist':
					bl = self.processSimpleXML(grandchild, ('package', ), [])
					bl.files = self.processBinaryListing(grandchild)
					info.binary_list.append(bl)
				else:
					self.unexpectedElement(grandchild, child)

		return result

	def processBuildInfo(self, xmlnode):
		result = OBSSchema.Dummy()
		result.name = xmlnode.attrib['package']
		result.subpackages = []
		result.builddeps = []

		for child in xmlnode:
			value = None
			if child.text:
				value = child.text.strip()

			key = child.tag
			if key == 'subpack':
				# skip these for now
				if value.endswith('-debuginfo') or value.endswith('-debugsource'):
					continue

				result.subpackages.append(value)
			elif key == 'versrel':
				(version, release) = value.split('-')
				result.version = version
				result.release = release
			elif key == 'release':
				result.obsrelease = value
			elif key == 'bdep':
				bdep = self.processSimpleXML(child, ("name", "version", "release", "arch", "hdrmd5", ), ("notmeta",))
				result.builddeps.append(bdep)
			elif key in ('constraint', ):
				pass
			else:
				if getattr(result, key, None) is not None:
					oldValue = getattr(result, key)
					errormsg(f"Duplicate attribute {key}, changing value from \"{oldValue}\" to \"{value}\"")
					raise Exception()

				setattr(result, key, value)

		return result

	def processBinaryListing(self, xmlnode):
		if not self.checkRootNodeTag(xmlnode, 'binarylist'):
			return None

		result = []
		for child in xmlnode:
			if child.tag != 'binary':
				self.unexpectedElement(child, xmlnode)
				continue

			f = self.processSimpleXML(child, ('filename', 'mtime'), [])
			result.append(f)

		return result

	def processFileInfo(self, xmlnode):
		if not self.checkRootNodeTag(xmlnode, 'fileinfo'):
			return None

		result = OBSSchema.Dummy()
		result.filename = xmlnode.attrib['filename']
		result.provides = []
		result.requires = []
		result.recommends = []
		result.suggests = []
		result.conflicts = []
		result.supplements = []
		result.enhances = []
		result.provides_ext = []
		result.requires_ext = []
		result.recommends_ext = []
		result.suggests_ext = []
		result.conflicts_ext = []

		for child in xmlnode:
			value = None
			if child.text:
				value = child.text.strip()

			key = child.tag
			if key in ('provides', 'requires', 'recommends', 'suggests', 'conflicts', 'supplements', 'enhances'):
				getattr(result, key).append(value)
			elif key in ('provides_ext', 'requires_ext', 'recommends_ext', 'suggests_ext', 'conflicts_ext'):
				dep = OBSSchema.Dummy()
				dep.type = key[:-4]
				dep.expression = child.attrib['dep']
				dep.packages = []

				member = getattr(result, key)
				member.append(dep)

				for grandchild in child:
					assert(grandchild.tag in ('requiredby', 'providedby'))
					a = grandchild.attrib
					del a['project']
					del a['repository']
					pinfo = PackageInfo(**a, epoch = None, backingStoreId = None)

					dep.packages.append(pinfo)
			else:
				if getattr(result, key, None) is not None:
					oldValue = getattr(result, key)
					infomsg(f"Duplicate attribute {key}, changing value from \"{oldValue}\" to \"{value}\"")
					raise Exception()

				setattr(result, key, value)

		return result


	def processSimpleXML(self, node, requiredAttrs, optionalAttrs):
		result = OBSSchema.Dummy()
		attr = node.attrib

		for name in requiredAttrs:
			if name not in attr:
				suse.error("element <%s> lacks required attribute %s" % (node.tag, name))

				import xml.etree.ElementTree
				xml.etree.ElementTree.dump(xmlnode)

				raise ValueError("Bad XML mojo from OBS")

			setattr(result, name, attr[name])

		for name in optionalAttrs:
			value = attr.get(name, None)
			setattr(result, name, value)

		return result

class GenericFileCache(object):
	class Entry:
		def __init__(self, path):
			self.path = path

		def __bool__(self):
			return os.path.exists(self.path)

		@property
		def exists(self):
			return os.path.exists(self.path)

		@property
		def age(self):
			mtime = posix.stat(self.path).st_mtime
			return time.time() - mtime

		def read(self):
			if not os.path.exists(self.path):
				return None

			try:
				with open(self.path) as f:
					return f.read()
			except:
				pass
			return None

		def open(self, mode = "r"):
			return open(self.path, mode)

		def write(self, res):
			debugCache(f"Write cache entry to {self.path}")
			assert(type(res) is str)

			self.makedir(os.path.dirname(self.path))
			with open(self.path, "w") as f:
				f.write(res)

		def makedir(self, dirpath):
			if os.path.isdir(dirpath):
				return

			parent = os.path.dirname(dirpath)
			if parent != "/" and not os.path.exists(parent):
				self.makedir(parent)
			os.mkdir(dirpath, 0o755)

	def __init__(self, path):
		self.path = path

class HTTPCache(GenericFileCache):
	def getEntry(self, url):
		assert(type(url) is str)

		if url.startswith("http:"):
			path = url[5:]
		elif url.startswith("https:"):
			path = url[6:]
		else:
			return None
		path = path.lstrip("/")

		return self.Entry(os.path.join(self.path, path))

class OBSCache(GenericFileCache):
	def getEntry(self, objectName, project, repository = None, arch = None, package = None, rpm = None):
		path = [project]
		if repository:
			path.append(repository)
		if arch:
			path.append(arch)
		if package:
			path.append(package)
		if rpm:
			path.append(rpm)
		path.append(objectName)

		path = "/".join(path)
		return self.Entry(os.path.join(self.path, path))

class OBSClient(object):
	def __init__(self, hostname, cache = None):
		if hostname is None:
			raise Exception("Cannot create OBS client: missing hostname argument")

		self.hostname = hostname
		self._apiurl = f"https://{hostname}"
		self._schema = OBSSchema()
		self._cache = cache

		import osc.conf
		osc.conf.get_config()

		infomsg(f"Created OBS client for {self._apiurl}")
		self.setCacheStrategy('default')

	def setCachePath(self, path):
		self._cache = HTTPCache(path)

	def setCacheStrategy(self, name):
		self._allowApiCalls = True
		if name == 'none':
			# always go to OBS. Slow
			self._maxCacheAge = 0
		elif name == 'opportunistic':
			# avoid calling OBS where possible
			self._maxCacheAge = None
		elif name == 'exclusive':
			self._maxCacheAge = None
			self._allowApiCalls = False
		elif name == 'default':
			self._maxCacheAge = 3600
		else:
			raise Exception(f"Unknown OBS cache strategy {name}")
		self.cachePolicy = name
		infomsg(f"Setting OBS cache strategy to {name}")

	def getHTTPCacheEntry(self, *args, **kwargs):
		if self._cache is None:
			return None
		return self._cache.getEntry(*args, **kwargs)

	def apiCallRaw(self, path, method = "GET", cachingOff = False, cacheEntry = None, progressMeter = None, **params):
		assert(method in ('GET', 'POST'))

		if type(path) == list:
			path = "/".join(path)

		if params:
			param_list = []
			for (key, value) in params.items():
				if type(value) in (list, tuple):
					param_list += list(f"{key}={vi}" for vi in value)
				else:
					param_list.append(f"{key}={value}")
			param_string = "&".join(param_list)
			path += "?" + param_string

		fullUrl = self._apiurl + "/" + path
		if method == "GET" and cacheEntry is None and not cachingOff:
			cacheEntry = self.getHTTPCacheEntry(fullUrl)
		if cacheEntry is not None and cacheEntry.exists and (self._maxCacheAge is None or cacheEntry.age < self._maxCacheAge):
			debugOBS(f"OBS API Call {path} [cached]", prefix = progressMeter)
			debugCache(f"Loading cache object {cacheEntry.path}")
			return cacheEntry.open()

		if not self._allowApiCalls:
			raise Exception(f"Cannot perform API call to {path} (denied by user)")

		from osc.core import http_request
		from urllib.error import HTTPError

		debugOBS(f"OBS API Call {path}", prefix = progressMeter)

		try:
			res = http_request(method, fullUrl)
		except HTTPError as e:
			# We could try to catch the HTTPError exception
			# (apparently defined in urllib.error) but why
			# bother
			errormsg("OBS: Unable to get %s - HTTP error %d" % (path, e.code))
			if e.code == 404:
				return None
			raise e

		if res and cacheEntry is not None:
			cacheEntry.write(res.read().decode('utf-8'))
			res = cacheEntry.open()

		return res

	def apiCallXML(self, function, *args, **params):
		path = [function]
		for arg in args:
			if type(arg) == list:
				path += arg
			else:
				path.append(arg)
		path = "/".join(path)

		res = self.apiCallRaw(path, **params)
		if not res:
			return res

		tree = xmltree.parse(res)
		if not tree:
			raise ValueError(f"OBS: cannot parse response to GET {path}")

		return tree.getroot()

	def queryBuildResult(self, project, **params):
		xml = self.apiCallXML('build', project, "_result", **params)
		if xml is None:
			errormsg(f"Cannot find build/{project}/_result")
			return None

		return self._schema.processBuildResult(xml)

	def getBuildDepInfo(self, project, repository, arch):
		res = self.apiCallXML("build", project, repository, arch, "_builddepinfo")
		return self._schema.processBuildDepInfo(res)

	def getBuildInfo(self, project, repository, package, arch, **kwargs):
		res = self.apiCallXML("build", project, repository, arch, package, "_buildinfo", **kwargs)
		return self._schema.processBuildInfo(res)

	def getFileInfoExt(self, project, repository, package, arch, filename, **kwargs):
		res = self.apiCallXML("build", project, repository, arch, package, filename, view = 'fileinfo_ext', **kwargs)
		return self._schema.processFileInfo(res)

class OBSBuildInfo:
	def __init__(self, name):
		self.name = name
		if ':' in name:
			# Result of a multibuild
			(self.obs_package, self.obs_build) = name.split(':')
		else:
			self.obs_package = name
			self.obs_build = None

		self.locked = False
		self.needs_fixup = False
		self.source = None
		self.rev = None
		self.binaries = []
		self.buildRequires = []

	@property
	def isMultibuild(self):
		return self.obs_build is not None

class BinaryMap(dict):
	class Entry:
		def __init__(self, name):
			self.name = name
			self.package = None

	def entry(self, name):
		e = self.get(name)
		if e is None:
			e = self.Entry(name)
			self[e.name] = e
		return e

class OBSPackage:
	STATUS_SUCCEEDED = 1
	STATUS_FAILED = 2
	STATUS_EXCLUDED = 3
	STATUS_UNKNOWN = 4

	def __init__(self, name):
		self.name = name
		self.buildStatus = None
		self.backingStoreId = None
		self.buildTime = None
		self.rpmsUsedForBuild = None
		self._buildRequires = set()
		self._source = None
		self._binaries = []

	def __str__(self):
		return self.name

	def addBinary(self, pinfo):
		self._binaries.append(pinfo)

	@property
	def basePackageName(self):
		name = self.name
		if ':' in name:
			return name.split(':')[0]
		return name

	@property
	def sourceVersion(self):
		pinfo = self.sourcePackage
		if pinfo is None:
			return None

		return pinfo.parsedVersion

	@property
	def sourcePackage(self):
		if self._source is None:
			found = None
			for pinfo in self._binaries:
				if pinfo.arch in ('src', 'nosrc'):
					if found:
						infomsg(f"OBS Package {self.name} provides more than one source package")
						infomsg(f"  {found.fullname()}")
						infomsg(f"  {pinfo.fullname()}")

					found = pinfo

			self._source = found

		return self._source

	@sourcePackage.setter
	def sourcePackage(self, value):
		self._source = value

	def addBuildRequires(self, rpm):
		if rpm.backingStoreId is None:
			raise Exception(f"{self}: cannot add requirement {rpm} because it doesn't have a DB id yet")

		self._buildRequires.add(rpm)

	@property
	def buildRequires(self):
		return sorted(self._buildRequires, key = lambda p: p.backingStoreId)

	@property
	def binaries(self):
		return self._binaries

	BUILD_STATUS_TABLE = {
		STATUS_SUCCEEDED : "succeeded",
		STATUS_FAILED : "failed",
		STATUS_EXCLUDED : "excluded",
		STATUS_UNKNOWN : "unknown",
		None : "not set",
	}

	@property
	def buildStatusString(self):
		s = self.BUILD_STATUS_TABLE.get(self.buildStatus)
		return s or "undefined"

class OBSProject:
	def __init__(self, name, product):
		self.name = name
		self.product = product
		self.resolverHints = product.resolverHints
		self.buildRepository = "standard"
		self.buildArch = product.arch
		self._packages = {}
		self._binaries = BinaryMap()

		self._obsCache = None

	def setCachePath(self, path):
		self._obsCache = OBSCache(path)

	def getCacheEntry(self, *args, **kwargs):
		if self._obsCache is None:
			return None
		return self._obsCache.getEntry(*args, **kwargs)

	@property
	def packages(self):
		return sorted(self._packages.values(), key = lambda p: p.name)

	def primeCache(self, client):
		if client.cachePolicy == 'exclusive':
			infomsg(f"Skipping download of data from OBS - cache policy is {client.cachePolicy}")
			return

		packages = self.updateBinaryList(client)

		progress = ThatsProgress(len(packages))

		failures = 0
		for obsPackage in packages:
			infomsg(f"[{progress}] retrieving OBS information for {obsPackage}")
			for rpm in obsPackage.binaries:
				info = client.getFileInfoExt(self.name, self.buildRepository, obsPackage.name, self.buildArch, rpm.fullname(),
						progressMeter = progress)
				if info is None:
					errormsg(f"Unable to obtain fileinfo for {rpm.fullname()}")
					failures += 1

			if obsPackage.buildStatus == OBSPackage.STATUS_SUCCEEDED and \
			   not self.queryPackageBuildInfo(client, obsPackage, progressMeter = progress):
				errormsg(f"Unable to obtain buildinfo for obs package {obsPackage}")
				failures += 1

			progress.tick()

		if failures:
			raise Exception(f"Encountered {failures} errors while trying to download project information on {self.name} from OBS")

	def ignorePackage(self, pinfo):
		name = pinfo.name

		for suffix in ("-debuginfo", "-debugsource"):
			if name.endswith(suffix):
				return True

		return False

	def queryBuildResults(self, client):
		resList = client.queryBuildResult(
				project = self.name,
				repository = self.buildRepository,
				arch = self.buildArch,
				multibuild = 1,
				view = ('status', 'binarylist'))

		# Since we were pretty specific about the repo and arch, the result
		# should have exactly one element only
		assert(len(resList) == 1)

		return resList

	def queryPackageBuildInfo(self, client, obsPackage, **params):
		sourceVersion = obsPackage.sourceVersion
		if sourceVersion is None:
			errormsg(f"Cannot retrieve buildinfo for {obsPackage.name}: unable to identify version")
			return False

		cacheObjectName = f"{sourceVersion}/buildInfo"
		cacheEntry = self.getCacheEntry(cacheObjectName, project = self.name,
				repository = self.buildRepository,
				package = obsPackage.name,
				arch = self.buildArch)

		return client.getBuildInfo(self.name, self.buildRepository, obsPackage.name, self.buildArch,
				cacheEntry = cacheEntry, **params)

	def updateBinaryList(self, client, with_binaries = True):
		debugOBS(f"Getting build results for {self.name}")
		resList = self.queryBuildResults(client)

		for st in resList[0].status_list:
			pkg = self.addPackage(st.package)

			status = st.code
			if status == "succeeded":
				pkg.buildStatus = pkg.STATUS_SUCCEEDED
			elif status == "failed" or status == "unresolvable":
				pkg.buildStatus = pkg.STATUS_FAILED
			elif status == "excluded":
				pkg.buildStatus = pkg.STATUS_EXCLUDED
			else:
				pkg.buildStatus = pkg.STATUS_UNKNOWN

		result = []
		for p in resList[0].binary_list:
			pkg = self.addPackage(p.package)

			binaries = []
			version = None

			for f in p.files:
				filename = f.filename

				if filename == "_statistics":
					pkg.buildTime = int(f.mtime)
					continue

				if filename.startswith("::"):
					continue
				if not filename.endswith(".rpm"):
					continue
				pinfo = PackageInfo.parsePackageName(filename)
				pinfo.buildTime = int(f.mtime)

				if self.ignorePackage(pinfo):
					continue

				rpm = Package.fromPackageInfo(pinfo)
				self.product.addPackage(rpm)
				pkg.addBinary(rpm)

			result.append(pkg)

		return result

	def updateBuildDependencies(self, client, arch, packageIterator):
		processed = []

		for pack in packageIterator:
			data = client.getBuildInfo(self.name, self.buildRepository, pack.name, arch)
			if data is None:
				continue

			src = Package(data.name, data.version, data.release, 'src')
			src.pkgid = data.srcmd5

			pack.sourcePackageInfo = src
			processed.append(pack)

		return processed

	def updateFileInfoExt(self, client, packageName, filename, rpm, includeLiteralDependencies = True):
		def resolvePackageInfo(pinfoList):
			result = set()

			if not pinfoList:
				return result

			for pinfo in pinfoList:
				if self.ignorePackage(pinfo):
					continue

				dependent = self.product.findPackage(pinfo.name, pinfo.version, pinfo.release, pinfo.arch)
				if dependent is None:
					# try a looser match - ignore version and release
					dependent = self.product.findPackage(pinfo.name, arch = pinfo.arch)
					if dependent:
						warnmsg(f"{filename} references unknown rpm {pinfo.fullname()}, using {dependent} instead")
					if dependent is None:
						if '32bit' not in pinfo.name:
							raise Exception(f"{filename} references {pinfo.fullname()}, but I cannot find it")

						errormsg(f"{filename} references {pinfo.fullname()}, but I cannot find it")
						continue

				result.add(dependent)

			return result

		def resolveDependents(depList):
			result = set()

			if depList is None:
				return result

			for dep in depList:
				for pinfo in dep.packages:
					if self.ignorePackage(pinfo):
						continue

					dependent = self.product.findPackage(pinfo.name, pinfo.version, pinfo.release, pinfo.arch)
					if dependent is None:
						errormsg(f"{filename} {dep.type} {pinfo.fullname()}, but I cannot find it")
						continue

					result.add(dependent)
			return result

		info = client.getFileInfoExt(self.name, self.buildRepository, packageName, self.buildArch, filename)
		if info is None:
			errormsg(f"Unable to obtain fileinfo for {filename}")
			return False

		if info.requires_ext:
			requires = self.disambiguateRequires(packageName, info.requires_ext)
			rpm.updateResolvedRequires(resolvePackageInfo(requires))
		else:
			for expr in info.requires:
				dep = Package.processComplexDependency(expr)
				rpm.requires.append(dep)

		if info.provides_ext:
			rpm.updateResolvedProvides(resolveDependents(info.provides_ext))
		else:
			for expr in info.provides:
				dep = Package.processComplexDependency(expr)
				rpm.provides.append(dep)

		return True

	# In OBS fileinfo_ext data, resolved requirements are represented showing all possible
	# candidates. However, for good results, we should use only _one_ resolution, which
	# is the one with fewer dependencies.
	def disambiguateRequires(self, packageName, requires):
		if self.resolverHints is None:
			raise Exception(f"No resolver hints to resolve ambiguity in requirements")

		result = []
		for dep in requires:
			if len(dep.packages) <= 1:
				result += dep.packages
				continue

			nameToPackage = dict((pinfo.name, pinfo) for pinfo in dep.packages)
			names = set(nameToPackage.keys())

			# libomp16-devel requires libomp.so which expands to either
			# libomp15-devel or libomp16-devel. Obviously, we should not
			# pick libomp15-devel in this case.
			if packageName in names:
				continue

			preferred = self.resolverHints.getPreferred(names)
			if len(preferred) == 1:
				bestName = next(iter(preferred))
				result.append(nameToPackage[bestName])
				continue

			names = sorted(names)
			raise Exception(f"Cannot resolve ambiguity in requirement {dep.expression}: {' '.join(names)}")

		return result

	def getRpmsUsedForBuild(self, client, obsPackage):
		data = self.queryPackageBuildInfo(client, obsPackage)
		if data is None:
			return

		result = set()
		for used in data.builddeps:
			required = self.product.findPackage(used.name, used.version, used.release, used.arch)
			if required is None:
				warnmsg(f"building {obsPackage.name} uses {used.name}.{used.arch}, but I cannot find it")
				if False:
					raise Exception()
				continue

			result.add(required)

		return result

	def addPackage(self, name):
		pkg = self._packages.get(name)
		if pkg is None:
			pkg = OBSPackage(name)
			self._packages[pkg.name] = pkg
		return pkg

	# In the face of a multibuild configuration, we are producing
	# a bunch of different rpms that belong to "package:build" 
	# style names.
	# However, only one of these builds will have a source rpm, so in order
	# to map all binaries to their correct source, we have to go through
	# the base package name
	def resolveAllSources(self):
		sourceMap = {}
		needToResolveSource = []
		validPackages = []
		for pkg in self.packages:
			if pkg.buildStatus == OBSPackage.STATUS_EXCLUDED:
				continue

			if pkg.buildStatus == OBSPackage.STATUS_FAILED:
				infomsg(f"Unable to process {pkg.name} - build failed")
				continue

			if pkg.buildStatus != OBSPackage.STATUS_SUCCEEDED:
				errormsg(f"Unable to process {pkg.name} - unexpected build status {pkg.buildStatusString}")
				continue

			validPackages.append(pkg)

			sourcePackage = pkg.sourcePackage
			if sourcePackage is not None:
				sourceMap[pkg.basePackageName] = sourcePackage
			else:
				needToResolveSource.append(pkg)

		for pkg in needToResolveSource:
			sourcePackage = sourceMap.get(pkg.basePackageName)
			if sourcePackage is None:
				warnmsg(f"unable to determine source package version for {pkg.basePackageName}")
			else:
				pkg.sourcePackage = sourcePackage

		return set(pkg.sourcePackage for pkg in validPackages)

	def updateBackingStore(self, store, client, chunkSize = 20):
		toBeAdded = []
		binariesToBeAdded = []

		sourcePackages = self.resolveAllSources()

		# Make sure all the source packages are in the DB so that we can later refer to them
		# by backingStoreId
		sourcesToBeAdded = list(sorted(sourcePackages, key = lambda p: p.name))
		store.addPackageObjectList(sourcesToBeAdded, updateDependencies = False)

		# Make sure each of these packages is in the database and has a backingStoreId
		if not store.lookupBuildIdsForList(self.packages):
			raise Exception("Unable to insert all OBS packages into DB")

		needToUpdateSourcesFor = []
		for pkg in self.packages:
			if pkg.buildStatus != OBSPackage.STATUS_SUCCEEDED:
				continue

			sourcePackage = pkg.sourcePackage
			if sourcePackage.backingStoreId is None:
				raise Exception(f"Source package {sourcePackage.shortname} is not in DB")

			for rpm in pkg.binaries:
				rpm.obsBuildId = pkg.backingStoreId

				if rpm.sourceBackingStoreId is None and rpm is not sourcePackage:
					rpm.setSourcePackage(sourcePackage)
					needToUpdateSourcesFor.append(rpm)

				assert(rpm.resolvedRequires is None)

			binariesToBeAdded += pkg.binaries
			if store.obsPackageWasRebuilt(pkg):
				toBeAdded.append(pkg)

		infomsg(f"About to update {len(binariesToBeAdded)} packages")
		store.addPackageObjectList(binariesToBeAdded, updateDependencies = False)
		store.updatePackageSourceObjectList(needToUpdateSourcesFor)
		infomsg("Done.")

		infomsg(f"About to update depdendencies for packages")
		queue = ChunkingQueue(lambda p: self.storeDependencies(store, p))
		for obsPackage in toBeAdded:
			rpmNames = ", ".join(_.shortname for _ in obsPackage.binaries)
			infomsg(f"+ {obsPackage.name}: {rpmNames}")

			for rpm in obsPackage.binaries:
				if store.havePackageDependencies(rpm):
					continue

				if self.updateFileInfoExt(client, obsPackage.name, rpm.fullname(), rpm):
					queue.add(rpm)

		queue.flush()
		infomsg("Done.")

		infomsg(f"About to collect build depdendencies for packages")
		rpmToPackage = {}
		for obsPackage in toBeAdded:
			for rpm in obsPackage.binaries:
				assert(rpm.backingStoreId)
				rpmToPackage[rpm.backingStoreId] = obsPackage

		queue = ChunkingQueue(lambda p: self.storeBuilds(store, p))
		for obsPackage in toBeAdded:
			obsPackage.rpmsUsedForBuild = self.getRpmsUsedForBuild(client, obsPackage)
			for rpm in obsPackage.rpmsUsedForBuild:
				if rpm.backingStoreId is None:
					# we can get here in rare circumstances. For example when the
					# build for an OBS package failed, then we will not have picked
					# up the binary RPMs it produces. However, there may still be
					# (build) dependencies on the rpms produced by a previous build.
					if not store.fetchPackageObjectId(rpm):
						raise Exception(f"{obsPackage} requires {rpm} for building, but I couldn't find it in the database")

				obsPackage.addBuildRequires(rpm)
			queue.add(obsPackage)

		infomsg("Done.")

	def storePackages(self, store, processed):
		msg = ", ".join(_.name for _ in processed)
		infomsg(f"Updating DB with package info for {msg}")
		store.addPackageObjectList(processed)

	def storeDependencies(self, store, processed):
		msg = ", ".join(_.shortname for _ in processed)
		infomsg(f"Updating DB with build dependencies for {msg}")
		store.updatePackageDependenciesObjectList(processed)

	def storeBuilds(self, store, processed):
		msg = ", ".join(_.name for _ in processed)
		infomsg(f"Updating DB with package builds for {msg}")
		store.updateBuilds(processed)


if __name__ == "__main__":
	obs = OBSClient("https://api.suse.de")
	obs.setCachePath("cache/http")

	project = OBSProject("SUSE:ALP:Source:Standard:Core:1.0:Build")
	project.updateBinaryList(obs, "x86_64")

	iter = project.chunkedPackageIterator()
	while iter:
		processed = project.updateBuildDependencies(obs, "x86_64", iter)
		if not processed:
			break
		flup
