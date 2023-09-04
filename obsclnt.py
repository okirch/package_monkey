import osc
import os
import xmltree
import posix
import time
from packages import Package, PackageInfo, PackageInfoFactory

default_obs_apiurl = "https://api.opensuse.org"

optDebugOBS = 1

def debugOBS(*args, **kwargs):
	if optDebugOBS >= 1:
		print(*args, **kwargs)

class OBSSchema(object):
	class Dummy:
		pass

	def checkRootNodeTag(self, xmlnode, expectTag):
		if xmlnode is None:
			return False
		if xmlnode.tag != expectTag:
			print("OBS: unexpected root node <%s> in response (expected <%s>)" % (xmlnode.tag, expectTag));
			return False

		return True

	def unexpectedElement(self, node, parent = None):
		if parent:
			print("ignoring unexpected element <%s> as child of <%s>" % (node.tag, parent.tag))
		else:
			print("ignoring unexpected element <%s>" % node.tag)


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
					# print(f"_builddepinfo for package {info.name} contains invalid subpkg name \"{value}\"")
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
				if bdep.notmeta == "1":
					result.builddeps.append(bdep)
			elif key in ('constraint', ):
				pass
			else:
				if getattr(result, key, None) is not None:
					oldValue = getattr(result, key)
					print(f"Duplicate attribute {key}, changing value from \"{oldValue}\" to \"{value}\"")
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
				member = getattr(result, key)
				for grandchild in child:
					assert(grandchild.tag in ('requiredby', 'providedby'))
					a = grandchild.attrib
					del a['project']
					del a['repository']
					pinfo = PackageInfo(**a, epoch = None, backingStoreId = None)

					member.append(pinfo)
			else:
				if getattr(result, key, None) is not None:
					oldValue = getattr(result, key)
					print(f"Duplicate attribute {key}, changing value from \"{oldValue}\" to \"{value}\"")
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
			debugOBS(f"Write cache entry to {self.path}")
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
	def __init__(self, apiurl = None, cache = None):
		self._apiurl = apiurl or default_obs_apiurl
		# self._atoms = suse.utils.AtomStrings()
		self._schema = OBSSchema()
		self._cache = cache

		# self._deadmanSwitch = suse.utils.DeadmanSwitch(self._apiurl)

		import osc.conf
		osc.conf.get_config()

		print(f"Created OBS client for {self._apiurl}")
		if False:
			for k, v in osc.conf.config.items():
				if k.startswith('#'):
					continue
				print(f" {k}={v}")

	def setCachePath(self, path):
		self._cache = HTTPCache(path)

	def getHTTPCacheEntry(self, *args, **kwargs):
		if self._cache is None:
			return None
		return self._cache.getEntry(*args, **kwargs)

	def apiCallRaw(self, path, method = "GET", cachingOff = False, cacheEntry = None, maxCacheAge = 3600, **params):
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
		if cacheEntry is not None and cacheEntry.exists and (maxCacheAge is None or cacheEntry.age < maxCacheAge):
			debugOBS(f"Loading cache object {cacheEntry.path}")
			return cacheEntry.open()

		from osc.core import http_request
		from urllib.error import HTTPError

		try:
			res = http_request(method, fullUrl)
		except HTTPError as e:
			# We could try to catch the HTTPError exception
			# (apparently defined in urllib.error) but why
			# bother
			print("OBS: Unable to get %s - HTTP error %d" % (path, e.code))
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

		debugOBS(f"OBS API Call {path}")
		res = self.apiCallRaw(path, **params)
		if not res:
			return res

		# suse.debug("Parsing data returned by server...")
		tree = xmltree.parse(res)
		if not tree:
			raise ValueError(f"OBS: cannot parse response to GET {path}")

		return tree.getroot()

	def queryBuildResult(self, project, **params):
		xml = self.apiCallXML('build', project, "_result", **params)
		if xml is None:
			print(f"Cannot find build/{project}/_result")
			return None

		return self._schema.processBuildResult(xml)

	def getBuildDepInfo(self, project, repository, arch):
		res = self.apiCallXML("build", project, repository, arch, "_builddepinfo")
		return self._schema.processBuildDepInfo(res)

	def getBuildInfo(self, project, repository, package, arch, **kwargs):
		debugOBS(f">> retrieve buildinfo for {package}")
		res = self.apiCallXML("build", project, repository, arch, package, "_buildinfo", **kwargs)
		return self._schema.processBuildInfo(res)

	def getFileInfoExt(self, project, repository, package, arch, filename):
		res = self.apiCallXML("build", project, repository, arch, package, filename, view = 'fileinfo_ext', maxCacheAge = None)
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
		self._pinfo = None
		self.backingStoreId = None
		self.buildTime = None
		self._buildRequires = set()
		self._usedForBuild = set()
		self._source = None
		self._binaries = []

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
						print(f"OBS Package {self.name} provides more than one source package")
						print(f"  {found.fullname()}")
						print(f"  {pinfo.fullname()}")

					found = pinfo

			self._source = found

		return self._source

	@sourcePackage.setter
	def sourcePackage(self, value):
		self._source = value

	def addBuildRequires(self, obsPackage):
		assert(obsPackage.backingStoreId)
		self._buildRequires.add(obsPackage)

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

	def ignorePackage(self, pinfo):
		name = pinfo.name

		for suffix in ("-32bit", "-debuginfo", "-debugsource"):
			if name.endswith(suffix):
				return True

		return False

	def updateBinaryList(self, client, with_binaries = True):
		debugOBS(f"Getting build results for {self.name}")

		resList = client.queryBuildResult(
				project = self.name,
				repository = self.buildRepository,
				arch = self.buildArch,
				multibuild = 1,
				view = ('status', 'binarylist'))

		# Since we were pretty specific about the repo and arch, the result
		# should have exactly one element only
		assert(len(resList) == 1)

		result = []

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
		def resolveDependents(depList, verb):
			result = set()

			for pinfo in depList:
				if self.ignorePackage(pinfo):
					continue

				dependent = self.product.findPackage(pinfo.name, pinfo.version, pinfo.release, pinfo.arch)
				if dependent is None:
					print(f"{filename} {verb}s {pinfo.fullname()}, but I cannot find it")
					continue

				result.add(dependent)
			return result

		info = client.getFileInfoExt(self.name, self.buildRepository, packageName, self.buildArch, filename)
		if info is None:
			print(f"Unable to obtain fileinfo for {filename}")
			return False

		if info.requires_ext:
			rpm.updateResolvedRequires(resolveDependents(info.requires_ext, "require"))
		else:
			for expr in info.requires:
				dep = Package.processComplexDependency(expr)
				rpm.requires.append(dep)

		if info.provides_ext:
			rpm.updateResolvedProvides(resolveDependents(info.provides_ext, "provide"))
		else:
			for expr in info.provides:
				dep = Package.processComplexDependency(expr)
				rpm.provides.append(dep)

		return True

	def updateBuildInfo(self, client, obsPackage):
		sourceVersion = obsPackage.sourceVersion
		if sourceVersion is None:
			print(f"Cannot retrieve buildinfo for {obsPackage.name}: unable to identify version")
			return

		cacheObjectName = f"{sourceVersion}/buildInfo"
		cacheEntry = self.getCacheEntry(cacheObjectName, project = self.name,
				repository = self.buildRepository,
				package = obsPackage.name,
				arch = self.buildArch)
		print(cacheEntry.path)

		data = client.getBuildInfo(self.name, self.buildRepository, obsPackage.name, self.buildArch, cacheEntry = cacheEntry, maxCacheAge = None)
		if data is None:
			return

		obsPackage._usedForBuild = set()
		for used in data.builddeps:
			required = self.product.findPackage(used.name, used.version, used.release, used.arch)
			if required is None:
				print(f"WARNING: building {obsPackage.name} uses {used.name}, but I cannot find it")
				if False:
					raise Exception()
				continue

			obsPackage._usedForBuild.add(required)

	def updateBuildDependenciesOld(self, client, arch):
		for buildInfo in client.getBuildDepInfo(self.name, self.buildRepository, arch):
			pkg = self.addPackage(buildInfo.name)
			for bName in buildInfo.binaries:
				pkg.addBinary(bName)

			for bName in buildInfo.buildRequires:
				pkg.addBuildRequires(bName)

	def addPackage(self, name):
		pkg = self._packages.get(name)
		if pkg is None:
			pkg = OBSPackage(name)
			self._packages[pkg.name] = pkg
		return pkg

	class ChunkingQueue:
		def __init__(self, processingFunction, chunkSize = 20):
			self.processingFunction = processingFunction
			self.chunkSize = chunkSize
			self.processed = []

		def __del__(self):
			self.flush()

		def add(self, object):
			self.processed.append(object)
			if len(self.processed) >= self.chunkSize:
				self.flush()

		def flush(self):
			if self.processed:
				self.processingFunction(self.processed)
				self.processed = []

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
				print(f"Unable to process {pkg.name} - build failed")
				continue

			if pkg.buildStatus != OBSPackage.STATUS_SUCCEEDED:
				print(f"Unable to process {pkg.name} - unexpected build status {pkg.buildStatusString}")
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
				print(f"Warning: unable to determine source package version for {pkg.basePackageName}")
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
				print(f"Source package {sourcePackage.shortname} is not in DB")
				fail

			for rpm in pkg.binaries:
				rpm.obsBuildId = pkg.backingStoreId

				if rpm.sourceBackingStoreId is None and rpm is not sourcePackage:
					rpm.setSourcePackage(sourcePackage)
					needToUpdateSourcesFor.append(rpm)

				assert(rpm.resolvedRequires is None)

			binariesToBeAdded += pkg.binaries
			if store.obsPackageWasRebuilt(pkg):
				toBeAdded.append(pkg)

		print(f"About to update {len(binariesToBeAdded)} packages")
		store.addPackageObjectList(binariesToBeAdded, updateDependencies = False)
		store.updatePackageSourceObjectList(needToUpdateSourcesFor)
		print("Done.")

		print(f"About to update depdendencies for packages")
		queue = self.ChunkingQueue(lambda p: self.storeDependencies(store, p))
		for obsPackage in toBeAdded:
			rpmNames = ", ".join(_.shortname for _ in obsPackage.binaries)
			print(f"+ {obsPackage.name}: {rpmNames}")

			for rpm in obsPackage.binaries:
				if store.havePackageDependencies(rpm):
					continue

				if self.updateFileInfoExt(client, obsPackage.name, rpm.fullname(), rpm):
					queue.add(rpm)

		queue.flush()
		print("Done.")

		print(f"About to collect build depdendencies for packages")
		rpmToPackage = {}
		for obsPackage in toBeAdded:
			for rpm in obsPackage.binaries:
				assert(rpm.backingStoreId)
				rpmToPackage[rpm.backingStoreId] = obsPackage

		queue = self.ChunkingQueue(lambda p: self.storeBuilds(store, p))
		for obsPackage in toBeAdded:
			self.updateBuildInfo(client, obsPackage)
			for rpm in obsPackage._usedForBuild:
				assert(rpm.backingStoreId)

				requiredPackage = rpmToPackage.get(rpm.backingStoreId)
				if requiredPackage is None:
					print(f"Cannot determine the OBS package that {rpm.shortname} belongs to")
					continue

				obsPackage.addBuildRequires(requiredPackage)
			queue.add(obsPackage)

		print("Done.")

	def storePackages(self, store, processed):
		msg = ", ".join(_.name for _ in processed)
		print(f"Updating DB with package info for {msg}")
		store.addPackageObjectList(processed)

	def storeDependencies(self, store, processed):
		msg = ", ".join(_.shortname for _ in processed)
		print(f"Updating DB with build dependencies for {msg}")
		store.updatePackageDependenciesObjectList(processed)

	def storeBuilds(self, store, processed):
		msg = ", ".join(_.name for _ in processed)
		print(f"Updating DB with package builds for {msg}")
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
