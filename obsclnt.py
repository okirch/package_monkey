import osc
import os
import xmltree
import posix
import time
import xml.etree.ElementTree as ET

from packages import Package, PackageInfo, PackageInfoFactory, UniquePackageInfoFactory
from evolution import PackageEvolutionLog, Genealogy
from util import ThatsProgress, SimpleQueue, TimedExecutionBlock
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

obsLogger = loggingFacade.getLogger('obs')
cacheLogger = loggingFacade.getLogger('cache')

def debugOBS(msg, *args, prefix = None, **kwargs):
	if prefix:
		msg = f"[{prefix}] {msg}"
	obsLogger.debug(msg, *args, **kwargs)

def debugCache(*args, **kwargs):
	cacheLogger.debug(*args, **kwargs)

def logXML(message, xmldoc, msgfunc = infomsg):
	msgfunc(message)
	with loggingFacade.temporaryIndent():
		for line in xmltree.toString(xmldoc).split('\n'):
			msgfunc(line)

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


	# Process a <status> document; usually part of a HTTP code 400 response
	def processStatusDocument(self, xmlnode):
		if not self.checkRootNodeTag(xmlnode, 'status'):
			return None

		code = xmlnode.attrib.get('code')
		if code is None:
			return None

		result = OBSError(code)
		for child in xmlnode:
			if child.tag == 'summary':
				result.summary = child.text.strip()
			else:
				self.unexpectedElement(child, xmlnode)

		return result


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
				bdep = self.processSimpleXML(child, ("name", "version", "release", "arch", "hdrmd5", ), ("notmeta", "preinstall", "vminstall", "project", "repository", ))
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

	def processFileInfo(self, xmlnode, infoFactory = None):
		if not self.checkRootNodeTag(xmlnode, 'fileinfo'):
			return None

		if infoFactory is None:
			infoFactory = PackageInfoFactory()

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
		result.supplements_ext = []

		for child in xmlnode:
			value = None
			if child.text:
				value = child.text.strip()

			key = child.tag
			if key in ('provides', 'requires', 'recommends', 'suggests', 'conflicts', 'supplements', 'enhances'):
				getattr(result, key).append(value)
			elif key in ('provides_ext', 'requires_ext', 'recommends_ext', 'suggests_ext', 'conflicts_ext', 'supplements_ext'):
				dep = OBSDependency(expression = child.attrib['dep'], type = key[:-4])

				member = getattr(result, key)
				member.append(dep)

				for grandchild in child:
					assert(grandchild.tag in ('requiredby', 'providedby'))
					a = grandchild.attrib
					del a['project']
					del a['repository']
					pinfo = infoFactory(**a)

					dep.packages.add(pinfo)

				# infomsg(f" NEW  {dep.type} {dep.expression}: {' '.join(map(str, dep.packages))}")
			else:
				if getattr(result, key, None) is not None:
					oldValue = getattr(result, key)
					infomsg(f"Duplicate attribute {key}, changing value from \"{oldValue}\" to \"{value}\"")
					raise Exception()

				setattr(result, key, value)

		return result

	def processDirectoryListing(self, xmlnode):
		if not self.checkRootNodeTag(xmlnode, 'directory'):
			return None

		result = []
		for child in xmlnode:
			if child.tag != 'entry':
				error(f"ignoring <{child.tag}> inside directory listing")
				continue

			name = child.attrib.get('name')
			if name is not None:
				result.append(name)

		return result

	def processSimpleXML(self, node, requiredAttrs, optionalAttrs):
		result = OBSSchema.Dummy()
		attr = node.attrib

		for name in requiredAttrs:
			if name not in attr:
				suse.error("element <%s> lacks required attribute %s" % (node.tag, name))

				xml.etree.ElementTree.dump(xmlnode)

				raise ValueError("Bad XML mojo from OBS")

			setattr(result, name, attr[name])

		for name in optionalAttrs:
			value = attr.get(name, None)
			setattr(result, name, value)

		return result

	def processProjectMeta(self, root):
		if not self.checkRootNodeTag(root, 'project'):
			return None

		name = root.attrib['name']
		result = OBSProjectMeta(name)

		for child in root:
			if child.tag == "repository":
				repoNode = child

				name = repoNode.attrib.get('name')

				repo = result.addRepository(name)
				repo.block = repoNode.attrib.get('block')
				for pathNode in repoNode.findall("path"):
					repo.addPath(pathNode.attrib['project'], pathNode.attrib['repository'])

				for archNode in repoNode.findall("arch"):
					repo.addArch(archNode.text.strip())
			elif child.tag == "person":
				result.addPerson(child.attrib.get('userid'), child.attrib.get('role'))
			elif child.tag == "title":
				if child.text is not None:
					result.title = child.text.strip()
			elif child.tag == "description":
				if child.text is not None:
					result.description = child.text.strip()
			elif child.tag == "scmsync":
				if child.text is not None:
					result.scmsync = child.text.strip()
			elif child.tag == "build":
				for buildNode in child:
					name = buildNode.attrib.get('repository')
					arch = buildNode.attrib.get('arch')

					if name is None:
						# change global state
						assert(buildNode.tag == 'enable')
						continue

					bs = result.addBuildState(name)
					if buildNode.tag == 'disable':
						assert(arch is None)
						bs.disableAll()
					elif arch is None:
						bs.enableAll()
					else:
						bs.enableArch(arch)
			else:
				raise Exception(f"unexpected <{child.tag}> element in prjconf")

		return result

	def buildProjectMeta(self, prjconf):
		xmldoc = xmltree.XMLTree('project')
		root = xmldoc.root

		root.setAttribute('name', prjconf.name)
		root.addField('title', prjconf.title or prjconf.name)
		root.addField('description', prjconf.description or "...")

		if prjconf.scmsync is not None:
			root.addField('scmsync', prjconf.scmsync)

		if not prjconf.persons:
			raise Exception(f"Cannot generate XML document for prjconf {prjconf}: no users and roles")

		for person in prjconf.persons:
			personNode = root.addChild('person')
			personNode.setAttribute('userid', person.user)
			personNode.setAttribute('role', person.role)

		buildGroupNode = None
		for repo in prjconf.repositories:
			bs = prjconf.addBuildState(repo.name)
			if bs.enabled:
				continue

			if buildGroupNode is None:
				buildGroupNode = root.addChild('build')

			if bs.enabledForArch is None:
				buildNode = buildGroupNode.addChild('disable')
				buildNode.setAttribute('repository', bs.name)
			else:
				for arch in bs.enabledForArch:
					buildNode = buildGroupNode.addChild('enable')
					buildNode.setAttribute('repository', bs.name)
					buildNode.setAttribute('arch', arch)

		for repo in prjconf.repositories:
			repoNode = root.addChild('repository')
			repoNode.setAttribute('name', repo.name)
			if repo.block is not None:
				repoNode.setAttribute('block', repo.block)

			for projectName, repoName in repo.paths:
				pathNode = repoNode.addChild("path")
				pathNode.setAttribute('project', projectName)
				pathNode.setAttribute('repository', repoName)

			for archName in repo.archs:
				pathNode = repoNode.addField("arch", archName)

		return root

	def createProjectMetaDoc(self, projectName, userName):
		doc = ET.Element("project")
		doc.set('name', projectName)
		ET.SubElement(doc, 'title')
		ET.SubElement(doc, 'description')
		personNode = ET.SubElement(doc, 'person')
		personNode.set('userid', userName)
		personNode.set('role', 'maintainer')
		return doc

	def createSimplePackageMetaDoc(self, projectName, packageName, title = None, description = None):
		tree = xmltree.XMLTree("package")

		doc = tree.root
		doc.setAttribute('project', projectName)
		doc.setAttribute('name', packageName)
		doc.addField('title', title or '.')
		doc.addField('description', description or '.')
		return doc

	# <link project="SUSE:ALP:Source:Standard:1.0" package="zstd" rev="2"/>
	def createLinkDoc(self, targetProject, targetPackage, targetRevision = None):
		tree = xmltree.XMLTree("link")

		doc = tree.root
		doc.setAttribute('project', targetProject)
		doc.setAttribute('package', targetPackage)
		if targetRevision is not None:
			doc.setAttribute('rev', targetRevision)
		# infomsg(f"Created link doc: {doc.encode()}")
		return doc

	def createAggregateDoc(self, sourceProjectName, sourcePackageNames, mapping = None):
		tree = xmltree.XMLTree("aggregatelist")

		doc = tree.root
		aggNode = doc.addChild('aggregate')
		aggNode.setAttribute('project', sourceProjectName)

		for name in sourcePackageNames:
			aggNode.addField('package', name)

		for sourceRepoName, targetRepoName in mapping or []:
			repoNode = aggNode.addChild('repository')
			repoNode.setAttribute('source', sourceRepoName)
			repoNode.setAttribute('target', targetRepoName)

		return doc

	def createAggregateDocFromObject(self, obsAggregate):
		tree = xmltree.XMLTree("aggregatelist")

		doc = tree.root

		for entry in obsAggregate.entries:
			aggNode = doc.addChild('aggregate')
			aggNode.setAttribute('project', entry.sourceProjectName)

			for name in entry.sourcePackageNames:
				aggNode.addField('package', name)

			for name in entry.artefactNames:
				aggNode.addField('binary', name)

			for sourceRepoName, targetRepoName in entry.mapping or []:
				repoNode = aggNode.addChild('repository')
				repoNode.setAttribute('source', sourceRepoName)
				repoNode.setAttribute('target', targetRepoName)

		return doc

	def createMultiAggregateDoc(self, sourceProjectName, sourceRepositoryName,
				targetProjectName, targetRepositoryName,
				sourcePackageList, targetPackageName,
				mapping):
		tree = xmltree.XMLTree("aggregatelist")

		doc = tree.root
		aggNode = doc.addChild('aggregate')
		aggNode.setAttribute('project', sourceProjectName)

		for sourcePackageName in sourcePackageList:
			aggNode.addField('package', sourcePackageName)

		for sourceRepoName, targetRepoName in mapping or []:
			repoNode = aggNode.addChild('repository')
			repoNode.setAttribute('source', sourceRepoName)
			repoNode.setAttribute('tatget', targetRepoName)

		return doc

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

class OBSError(Exception):
	def __init__(self, code):
		self.code = code
		self.request = None
		self.document = None
		self.summary = ''

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

		self._apiUser = osc.conf.config['user']

		infomsg(f"Created OBS client for {self._apiurl}, user {self._apiUser}")
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

	@property
	def cachingEnabled(self):
		return self._maxCacheAge != 0

	def getHTTPCacheEntry(self, *args, **kwargs):
		if self._cache is None:
			return None
		return self._cache.getEntry(*args, **kwargs)

	def apiCallRaw(self, path, method = "GET", cachingOff = False, cacheEntry = None, progressMeter = None, data = None, xmldoc = None, quiet = False, **params):
		assert(method in ('GET', 'POST', 'PUT', 'DELETE'))

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

		extra_args = {}
		if method == "PUT":
			if data is None and xmldoc is not None:
				data = xmltree.toString(xmldoc)
			extra_args['data'] = data

		fullUrl = self._apiurl + "/" + path
		if method == "GET" and cacheEntry is None and not cachingOff and self._maxCacheAge != 0:
			cacheEntry = self.getHTTPCacheEntry(fullUrl)
		if cacheEntry is not None and cacheEntry.exists and (self._maxCacheAge is None or cacheEntry.age < self._maxCacheAge):
			debugOBS(f"OBS API Call {path} [cached]", prefix = progressMeter)
			debugCache(f"Loading cache object {cacheEntry.path}")
			return cacheEntry.open()

		if not self._allowApiCalls:
			raise Exception(f"Cannot perform API call to {path} (denied by user)")

		from osc.core import http_request
		from urllib.error import HTTPError, URLError
		from http.client import RemoteDisconnected

		debugOBS(f"OBS API {method} {path}", prefix = progressMeter)

		numRetries = 3
		while True:
			numRetries -= 1

			try:
				res = http_request(method, fullUrl, **extra_args)
				break
			except HTTPError as e:
				if e.code != 404 or not quiet:
					errormsg(f"OBS: Unable to {method} {path}: HTTP error {e.code}: {e.reason}")
					infomsg(f"  OBS error code={e.headers.get('x-opensuse-errorcode')}")

				# Handle "400 Bad Request" from OBS
				if e.code == 400:
					tree = xmltree.parse(e)
					error = self._schema.processStatusDocument(tree.getroot())
					if error is not None:
						logXML("OBS status document", tree.getroot())

						# could be an incomplete maintenance incident
						if 'no source uploaded' in error.summary:
							return None

						error.request = f"{method} {path}"
						error.document = extra_args.get('data')
						raise error

				if e.code == 404:
					return None
				raise e
			except URLError as e:
				errormsg(f"OBS: Unable to {method} {path}: URL error {e}")
				if numRetries == 0:
					raise e
			except http.client.RemoteDisconnected as e:
				errormsg(f"OBS: Unable to {method} {path}: URL error {e}")
				if numRetries == 0:
					raise e

			infomsg("Retrying...")

		if res and cacheEntry is not None:
			cacheEntry.write(res.read().decode('utf-8'))
			res = cacheEntry.open()

		return res

	def apiMakePath(self, function, *args):
		path = [function]
		for arg in args:
			if type(arg) == list:
				path += arg
			else:
				path.append(arg)
		path = "/".join(path)
		return path

	def apiCallXML(self, function, *args, **params):
		path = self.apiMakePath(function, *args)

		res = self.apiCallRaw(path, **params)
		if not res:
			return res

		tree = xmltree.parse(res)
		if not tree:
			raise ValueError(f"OBS: cannot parse response to GET {path}")

		return tree.getroot()

	def apiCallText(self, function, *args, **params):
		path = self.apiMakePath(function, *args)
		res = self.apiCallRaw(path, **params)
		if res is None:
			errormsg(f"API call {path} returns None")
			return None
		return res.read().decode('utf-8')

	def apiCallPUT(self, function, *args, **params):
		path = self.apiMakePath(function, *args)

		res = self.apiCallRaw(path, method = 'PUT', **params)
		if not res:
			errormsg(f"Failed to PUT {path}")
			errormsg(f"{res}")
			return False

		# FIXME: inspect the result?
		return True

	def apiCallPOST(self, function, *args, **params):
		path = self.apiMakePath(function, *args)

		res = self.apiCallRaw(path, method = 'POST', **params)
		if not res:
			errormsg(f"Failed to POST {path}")
			errormsg(f"{res}")
			return False

		# FIXME: inspect the result?
		return True

	def apiCallDELETE(self, function, *args, **params):
		path = self.apiMakePath(function, *args)

		res = self.apiCallXML(path, method = 'DELETE', **params)
		if not res:
			errormsg(f"Failed to DELETE {path}")
			errormsg(f"{res}")
			return False

		err = self._schema.processStatusDocument(res)
		if err.code == "ok":
			return True

		errormsg(f"DELETE {path} failed: OBS status code {err.code}")
		logXML("OBS status document", res)
		return False

	def queryAllProjects(self, **params):
		xml = self.apiCallXML('source', **params)
		if xml is None:
			errormsg(f"Cannot list OBS projects - something is very wrong")
			return None

		return self._schema.processDirectoryListing(xml)

	def deleteProject(self, projectName, dryRun = False):
		if dryRun:
			infomsg(f"Would delete source/{projectName}")
			return True

		return self.apiCallDELETE('source', projectName)

	def querySourcePackages(self, project, **params):
		xml = self.apiCallXML('source', project, **params)
		if xml is None:
			errormsg(f"Cannot find source/{project}")
			return None

		return self._schema.processDirectoryListing(xml)

	def queryBuildResult(self, project, **params):
		xml = self.apiCallXML('build', project, "_result", **params)
		if xml is None:
			errormsg(f"Cannot find build/{project}/_result")
			return None

		return self._schema.processBuildResult(xml)

	def getBuildDepInfo(self, project, repository, arch):
		res = self.apiCallXML("build", project, repository, arch, "_builddepinfo")
		return self._schema.processBuildDepInfo(res)

	def queryPackageBuild(self, project, repository, package, arch, **params):
		xml = self.apiCallXML('build', project, repository, arch, package, **params)
		if xml is None:
			errormsg(f"Cannot find build/{project}/{repository}/{arch}/{package}")
			return None

		return self._schema.processBinaryListing(xml)

	def getBuildEnvironment(self, project, repository, package, arch, **kwargs):
		res = self.apiCallXML("build", project, repository, arch, package, "_buildenv", **kwargs)
		if res is None:
			return None
		return self._schema.processBuildInfo(res)

	def getBuildInfo(self, project, repository, package, arch, **kwargs):
		res = self.apiCallXML("build", project, repository, arch, package, "_buildinfo", **kwargs)
		if res is None:
			return None
		return self._schema.processBuildInfo(res)

	def getFileInfoExt(self, project, repository, package, arch, filename, infoFactory = None, **kwargs):
		res = self.apiCallXML("build", project, repository, arch, package, filename, view = 'fileinfo_ext', **kwargs)
		if res is None:
			return None
		return self._schema.processFileInfo(res, infoFactory)

	def putSourcePackageXML(self, project, package, file, body):
		return self.apiCallPUT("source", project, package, file, xmldoc = body)

	def getSourcePackageBuffer(self, project, package, file):
		return self.apiCallText("source", project, package, file)

	def putSourcePackageBuffer(self, project, package, file, body):
		return self.apiCallPUT("source", project, package, file, data = body)

	def getSourcePackageXML(self, project, package, **params):
		info = self.apiCallXML("source", project, package, **params)
		if info is None:
			return None
		if info.tag != "directory":
			raise Exception(f"{project}/{package} expected pkg info to contain a <directory> element")
		return info

	def getMetaXML(self, project, package = None, **params):
		if package is not None:
			meta = self.apiCallXML("source", project, package, "_meta", **params)
			if meta is not None and meta.tag != "package":
				raise Exception(f"{project}/{package}: expected _meta to contain a <package> element")
		else:
			meta = self.apiCallXML("source", project, "_meta", **params)
			if meta is not None and meta.tag != "project":
				raise Exception(f"{project}: expected _meta to contain a <project> element")
		return meta

	def putMetaXML(self, project, package, metaDoc):
		return self.putSourcePackageXML(project, package, "_meta", metaDoc)

	def putLinkXML(self, project, package, linkDoc):
		return self.putSourcePackageXML(project, package, "_link", linkDoc)

	def putAggregateXML(self, project, package, aggregateDoc):
		return self.putSourcePackageXML(project, package, "_aggregate", aggregateDoc)

	def listSourcePackages(self, project):
		res = self.apiCallXML("source", "project")

	def buildInitialProjectMeta(self, projectName, title = None, description = None):
		meta = OBSProjectMeta(projectName)
		meta.addPerson(self._apiUser, 'maintainer')
		meta.title = title or 'No title provided'
		meta.description = description or 'No description provided'
		return meta

	# FIXME: there should be a way to disable caching while we're modifying OBS content
	# Right now, the individual get*XML methods enforce cachingOff=True
	def linkpac(self, sourceProject, sourcePackage, destProject, destPackage = None, freezeRevision = False, dryRun = False):
		if self.cachingEnabled:
			raise Exception(f"linkpac: you must disable caching for this operation")

		if destPackage is None:
			destPackage = sourcePackage

		# check that the dest package does not exist
		info = self.getSourcePackageXML(destProject, destPackage, quiet = True)
		if info is not None:
			raise Exception(f"linkpac: {destProject}/{destPackage} already exists")

		info = self.getSourcePackageXML(sourceProject, sourcePackage, rev = "latest")
		if info is None:
			errormsg(f"{sourceProject}/{sourcePackage} does not exist!");
			return False

		sourceRevision = None
		if freezeRevision:
			sourceRevision = info.attrib.get('rev')
			if sourceRevision is None:
				raise Exception(f"linkpac: {sourceProject}/{sourcePackage} has no rev")

		# <link project="SUSE:ALP:Source:Standard:1.0" package="zstd" rev="2"/>
		xmldoc = self._schema.createLinkDoc(sourceProject, sourcePackage, sourceRevision)

		if dryRun:
			logXML(f"Would create {sourceProject}/{sourcePackage}/_link", xmldoc)
			return True

		meta = self.getMetaXML(sourceProject, sourcePackage)
		meta.attrib['project'] = destProject
		meta.attrib['name'] = destPackage
		self.putMetaXML(destProject, destPackage, meta)

		return self.putLinkXML(destProject, destPackage, xmldoc)

	def createAggregate(self, targetProjectName, obsAggregate, dryRun = False):
		xmldoc = self._schema.createAggregateDocFromObject(obsAggregate)

		if dryRun:
			logXML(f"Would create {targetProjectName}/{obsAggregate.targetPackageName}/_aggregate", xmldoc)
			return True

		packageName = obsAggregate.targetPackageName
		meta = None

		# When we're aggregating a single source package from somewhere, try
		# to get its description.
		singleSource = obsAggregate.singleSourcePackage()
		if singleSource:
			meta = self.getMetaXML(*singleSource)

			if meta is not None:
				meta.attrib['project'] = targetProjectName
				meta.attrib['name'] = packageName

		if meta is None:
			meta = self._schema.createSimplePackageMetaDoc(targetProjectName, packageName)

		self.putMetaXML(targetProjectName, packageName, meta)

		return self.putAggregateXML(targetProjectName, obsAggregate.targetPackageName, xmldoc)

	def updatePackage(self, targetProjectName, packageName, files, dryRun = False, title = None, description = None):
		if dryRun:
			filenames = list(files.keys())
			infomsg(f"Would update {targetProjectName}/{packageName}: {', '.join(filenames)}")
			return True

		modified = False

		meta = self.getMetaXML(targetProjectName, packageName)
		if meta is None:
			meta = self._schema.createSimplePackageMetaDoc(targetProjectName, packageName,
						title = title, description = description)
			if not self.putMetaXML(targetProjectName, packageName, meta):
				return False
			modified = True

		for fileName, content in files.items():
			currentContent = self.getSourcePackageBuffer(targetProjectName, packageName, fileName)
			if currentContent == content:
				continue

			infomsg(f"Updating {targetProjectName}/{packageName}/{fileName} because its contents have changed")
			if not self.putSourcePackageBuffer(targetProjectName, packageName, fileName, content):
				errormsg(f"failed to write {targetProjectName}/{packageName}/{fileName}")
				return False
			modified = True

		if not modified:
			infomsg(f"Package remains unchanged")
			return True

		# call commit to update the revision number
		return self.apiCallPOST('source', targetProjectName, packageName, cmd = 'commit')

	def aggregatepac(self, sourceProjectName, packageName, targetProjectName, dryRun = False, **kwargs):
		xmldoc = self._schema.createAggregateDoc(sourceProjectName, [packageName], **kwargs)

		if dryRun:
			logXML(f"Would create {targetProjectName}/{packageName}/_aggregate", xmldoc)
			return True

		meta = self.getMetaXML(sourceProjectName, packageName)
		meta.attrib['project'] = targetProjectName
		meta.attrib['name'] = packageName

		self.putMetaXML(targetProjectName, packageName, meta)

		return self.putAggregateXML(targetProjectName, packageName, xmldoc)

	def aggregatemulti(self, sourceProjectName, sourceRepositoryName,
				targetProjectName, targetRepositoryName,
				sourcePackageList, targetPackageName,
				dryRun = False, **kwargs):
		xmldoc = self._schema.createAggregateDoc(sourceProjectName, sourcePackageList,
				mapping = [(sourceRepositoryName, targetRepositoryName)])

		if dryRun:
			logXML(f"Would create {targetProjectName}/{targetPackageName}/_aggregate", xmldoc)
			return True

		meta = self._schema.createSimplePackageMetaDoc(targetProjectName, targetPackageName, **kwargs)
		self.putMetaXML(targetProjectName, targetPackageName, meta)

		return self.putAggregateXML(targetProjectName, targetPackageName, xmldoc)

	def deletepac(self, projectName, packageName, dryRun = False):
		if dryRun:
			infomsg(f"Would delete {projectName}/{packageName}")
			return True

		return self.apiCallDELETE('source', projectName, packageName)

	def getProjectConfig(self, projectName):
		return self.apiCallText("source", projectName, "_config")

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

class OBSDependency(object):
	def __init__(self, expression, backingStoreId = None, type = None):
		self.expression = expression
		self.type = type
		self.backingStoreId = backingStoreId
		self.packages = set()

# This should really be called OBSBuild, for several reasons
#  - multibuilds. One source package foo may give rise to several builds,
#    named foo, foo:flavor1, foo:flavor2, etc
#  - maintenance. When foo gets built in a maintenance project (*:Update),
#    the first build will be called foo, but subsequent rebuilds will have
#    the maintenance incident attached (as in foo.12345)
#  - maintenance updates of multibuilds are currently built as
#    foo.12345:flavor1 etc
#
# Naming related members of OBSPackage objects:
#  name:	This is the name of the build, such as foo.12345:flavor1
#  canonicalName:
#		name, with maintenance incident removed, ie
#		foo.12345 -> foo
#		foo.12345:flavor1 -> foo:flavor1
#  basePackageName:
#		source package name, ie "foo"
#  multibuildFlavor:
#		"flavor1", or None
#  maintenanceIncident:
#		12345, or 0
class OBSPackage:
	STATUS_SUCCEEDED = 1
	STATUS_FAILED = 2
	STATUS_EXCLUDED = 3
	STATUS_UNKNOWN = 4

	def __init__(self, name, canonicalName = None, maintenanceIncident = 0):
		self.name = name

		self.updateCanonicalName(canonicalName or name, maintenanceIncident)

		self.buildStatus = None
		self._buildStatusString = None
		self.backingStoreId = None
		self.buildTime = None
		self.rpmsUsedForBuild = None
		self._buildRequires = set()
		self._source = None
		self._binaries = []

		self.baseLabel = None
		self.baseLabelReason = None
		self.config = None
		self.trace = False


		self.requires = None

	def __str__(self):
		return self.name

	def addBinary(self, pinfo):
		self._binaries.append(pinfo)

	def updateCanonicalName(self, canonicalName, maintenanceIncident):
		self.canonicalName = canonicalName
		self.maintenanceIncident = maintenanceIncident

		self.basePackageName = None
		self.multibuildFlavor = None

		if ':' in canonicalName:
			name, flavor = canonicalName.rsplit(':', maxsplit = 1)
			if '.' in flavor:
				warnmsg(f"Space oddities: strange multibuild name {canonicalName}")

			self.basePackageName = name
			self.multibuildFlavor = flavor

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

	REVERSE_BUILD_STATUS_TABLE = {
		"succeeded"	: STATUS_SUCCEEDED,
		"failed"	: STATUS_FAILED,
		"unresolvable"	: STATUS_FAILED,
		"excluded"	: STATUS_EXCLUDED,
	}

	@property
	def buildStatusString(self):
		if self._buildStatusString is not None:
			return self._buildStatusString

		return self.BUILD_STATUS_TABLE.get(self.buildStatus) or "undefined"

	def setBuildStatus(self, value):
		self.buildStatus = self.stringToBuildStatus(value)
		self._buildStatusString = value

	@classmethod
	def stringToBuildStatus(klass, value):
		return klass.REVERSE_BUILD_STATUS_TABLE.get(value) or klass.STATUS_UNKNOWN

class OBSProjectMeta:
	class BuildState:
		def __init__(self, name):
			self.name = name
			self.enabled = True
			self.enabledForArch = None

		def enableAll(self):
			self.enabled = True
			self.enabledForArch = None

		def disableAll(self):
			self.enabled = False
			self.enabledForArch = None

		def enableArch(self, arch):
			if self.enabledForArch is None:
				self.enabledForArch = set()

			# the default is disabled
			self.enabled = False
			self.enabledForArch.add(arch)

	class Repository:
		def __init__(self, name):
			self.name = name
			self.block = None
			self.paths = []
			self.archs = []
			self.modified = False

		def setPaths(self, pathList):
			if self.paths != pathList:
				self.paths = pathList
				self.modified = True

		def addPath(self, projectName, repoName):
			self.paths.append((projectName, repoName))
			self.modified = True

		def setArchitectures(self, archList):
			if self.archs != archList:
				self.archs = archList
				self.modified = True

		def addArch(self, archName):
			if archName not in self.archs:
				self.archs.append(archName)
				self.modified = True

	class Person:
		def __init__(self, userName, role):
			self.user = userName
			self.role = role

	def __init__(self, name, meta = None):
		self.name = name
		self.title = None
		self.description = None
		self.repositories = []
		self.persons = []
		self.scmsync = None

		# keep the build enable/disable info separate from the list of repos
		# so that we can recreate the list of repos without losing this information
		self.buildstate = []

		self.xmldoc = meta

		self.modified = False

	def beginUpdate(self):
		self.modified = False
		for repo in self.repositories:
			repo.modified = False

	@property
	def isModified(self):
		return self.modified or any(repo.modified for repo in self.repositories)

	def updateTitle(self, value):
		if self.title != value:
			self.title = value
			self.modified = True

	def updateDescription(self, value):
		if self.description != value:
			self.description = value
			self.modified = True

	def updateScmSync(self, value):
		if self.scmsync != value:
			self.scmsync = value
			self.modified = True

	def clearAllRepositories(self):
		self.repositories = []

	def addRepository(self, name):
		for repo in self.repositories:
			if repo.name == name:
				return repo

		repo = self.Repository(name)
		self.repositories.append(repo)
		self.modified = True
		return repo

	def addPerson(self, userName, role):
		self.persons.append(self.Person(userName, role))

	def addBuildState(self, name):
		for bs in self.buildstate:
			if bs.name == name:
				return bs

		bs = self.BuildState(name)
		self.buildstate.append(bs)
		return bs

class OBSAggregate(object):
	class Entry:
		def __init__(self, sourceProjectName):
			self.sourceProjectName = sourceProjectName

			self.mapping = set()
			self.sourcePackageNames = set()
			self.artefactNames = set()

		def update(self, sourcePackageName, mapping, artefactNames = None):
			self.sourcePackageNames.add(sourcePackageName)
			if artefactNames is not None:
				self.artefactNames.update(set(artefactNames))

			self.mapping.update(mapping)

	def __init__(self, targetPackageName):
		self.targetPackageName = targetPackageName

		self.entries = []
		self.defaultEntry = None

	def createEntry(self, sourceProjectName):
		entry = self.Entry(sourceProjectName)
		self.entries.append(entry)
		return entry

	def addPackage(self, sourceProjectName, sourcePackageName, mapping, artefactNames = None):
		entry = None
		if artefactNames is None:
			if self.defaultEntry is None:
				entry = self.createEntry(sourceProjectName, asDefault = True)
			elif self.defaultEntry.sourceProjectName == sourceProjectName:
				entry = self.defaultEntry

		if entry is None:
			entry = self.createEntry(sourceProjectName)

		entry.sourcePackageNames.add(sourcePackageName)
		if artefactNames is not None:
			entry.artefactNames.update(set(artefactNames))

		entry.mapping.update(mapping)

	def singleSourcePackage(self):
		# This doesn't do what I wanted it to do, so disable it for now
		return None

class DependencyWorker:
	def __init__(self, client, backingStore):
		self.project = None
		self.client = client
		self.backingStore = backingStore

		# self.resolverHints = project.resolverHints
		# self.product = project.product

		self.knownPackages = {}
		for storedBuild in backingStore.enumerateOBSPackages():
			self.knownPackages[storedBuild.name] = storedBuild

	def __bool__(self):
		return False

	def buildToBackingStore(self, obsPackage):
		for rpm in obsPackage.binaries:
			self.rpmToBackingStore(rpm, updateLatest = True)
		# update build->package relation etc, but do not yet update the
		# time stamp for this build
		self.backingStore.updateOBSPackage(obsPackage, obsPackage.binaries)

	def rpmToBackingStore(self, rpm, **kwargs):
		if not self.backingStore.isKnownPackageObject(rpm):
			# beware, this will currently update the entry in 'latest' as well, which is
			# probably not what we want here
			if rpm.productId is None:
				rpm.productId = self.product.productId

		src = rpm.sourcePackage
		if src is not None:
			# KMPs now have a funky versioning scheme where the kernel version
			# is appended to the version number of the actual package,
			# as in
			# drbd-9.1.16-3.20.src.rpm
			# drbd-kmp-default-9.1.16_k6.4.0_11-3.20.x86_64.rpm
			if (src.version != rpm.version and not rpm.version.startswith(src.version)) or \
			   src.release != rpm.release:
				errormsg(f"{rpm.fullname()} does not match version string of source package {src.fullname()}")
				# raise Exception(f"bad {rpm.fullname()} src={src.fullname()}")

		self.backingStore.addPackageObject(rpm, **kwargs)

	def resolvePackageInfo(self, product, pinfoList):
		result = set()

		if not pinfoList:
			return result

		for pinfo in pinfoList:
			if self.ignorePackage(pinfo):
				continue

			dependent = product.findPackage(pinfo.name, pinfo.version, pinfo.release, pinfo.arch)
			if dependent is None:
				dependent = Package.fromPackageInfo(pinfo)
				product.addPackage(dependent)

			self.rpmToBackingStore(dependent)

			assert(dependent.backingStoreId)
			result.add(dependent)

		return result

	def updateDependencies(self, product, rpm, info):
		self.rpmToBackingStore(rpm)

		assert(info.requires_ext is not None)

		requires = info.requires_ext
		for dep in requires:
			rpms = set()
			for pinfo in dep.packages:
				requiredRpm = Package.fromPackageInfo(pinfo)
				requiredRpm.productId = product.productId
				self.rpmToBackingStore(requiredRpm)
				rpms.add(requiredRpm)
			dep.packages = rpms

		self.backingStore.updateDependencyTree(rpm, requires)
		rpm.updateResolvedRequires(requires)

		# .src.rpms are never required by anything
		if rpm.isSourcePackage:
			return

		assert(info.provides_ext is not None)

		requiredby = []
		for dep in info.provides_ext:
			requiredby += dep.packages

		requiredby = self.resolvePackageInfo(product, requiredby)

		rpm.updateResolvedProvides(requiredby)

	def ignorePackage(self, pinfo):
		name = pinfo.name

		for suffix in ("-debuginfo", "-debugsource"):
			if name.endswith(suffix):
				return True

		return False

class PackageUpdateJob(object):
	class PackageProxy(object):
		def __init__(self, project, build):
			self.project = project
			self.build = build
			self.name = build.name
			self.previousBuild = None

			self.isMaintenance = self.project.name.endswith(':Update')

		def __str__(self):
			return f"{self.project.name}/{self.build.name}"

		@property
		def buildTime(self):
			return self.build.buildTime

		# Given a maintenance update, try to guess the "true" package name
		# from the maintenance build.
		# This is a bit fuzzy. We have proper package names like go1.22,
		# and we have maintenance updates named poppler.31251:qt5...
		# The approach we take here:
		#  1. make sure that we process projects and packages in order
		#      a) :GA is processed before :Update
		#      b) builds are processed in the order given in $project/_result
		#  2. split the build name as baseName.$incidentId:$mbuildFlavor
		#     and check whether we've seen a package named baseName before
		def guessTrueName(self, nameOracle):
			name = self.build.name
			if not self.isMaintenance or '.' not in name:
				return False

			mbuildFlavor = None
			if ':' in name:
				name, mbuildFlavor = name.rsplit(':', maxsplit = 1)

			if '.' not in name:
				return False

			baseName, incidentId = name.rsplit('.', maxsplit = 1)
			if not incidentId.isdigit():
				return False

			if baseName == 'patchinfo':
				pass
			elif not nameOracle.isKnownOBSPackage(baseName):
				return False

			name = baseName
			if mbuildFlavor is not None:
				name = f"{name}:{mbuildFlavor}"

				if not nameOracle.isKnownOBSPackage(name):
					infomsg(f"{self} introduces new mbuild flavor {name}")

			self.name = name

			# We need to update the OBSBuild to reflect the actual mbuild name
			# Otherwise we populate the DB with names that contain incident IDs,
			# which is not what we want.
			self.build.updateCanonicalName(name, int(incidentId))

			return True

		@property
		def isValidBuild(self):
			return bool(self.build.buildTime) and bool(self.build.binaries)

		def isMoreRecentThan(self, otherProxy):
			otherBuild = otherProxy.build

			# a build without binaries is considered a failed build, and can never be
			# "more recent" than anything we have
			if not otherProxy.isValidBuild:
				debugOBS(f"  -> {otherProxy} does not look like a successful build (mtime={otherBuild.buildTime}, {len(otherBuild.binaries)} rpms)")
				return True

			if self.build.buildTime is None:
				return False

			if self.build.buildTime == otherBuild.buildTime:
				return True

			if self.build.buildTime > otherBuild.buildTime:
				debugOBS(f"  -> {self} is a more recent build of {otherProxy}")
				return True

			return False

	def __init__(self, client, onlyPackages = None):
		self.client = client
		self.onlyPackages = onlyPackages
		self.evolutionLog = PackageEvolutionLog()

		self.online = True
		if client.cachePolicy == 'exclusive':
			infomsg(f"Skipping download of data from OBS - cache policy is {client.cachePolicy}")
			self.online = False

		self._validPackageNames = set()
		self._proxies = {}

		self.queue = None

		self.unresolved = Package('__unresolved__', "1.0", "0", "noarch")
		self.unresolved.productId = 0

	# naming oracle callback used by PackageProxy.guessTrueName()
	def isKnownOBSPackage(self, tentativeName):
		return tentativeName in self._validPackageNames

	# Note: the caller should invoke this in the order in which projects are layered,
	# ie in the case of SLE, the sequence should be
	# SLE-15:GA SLE-15:Update SLE-15-SP1:GA SLE-15-SP1:Update etc
	def addProject(self, obsProject):
		if not self.online:
			return

		infomsg(f"Inspecting builds in {obsProject.name}")
		with loggingFacade.temporaryIndent(3):
			self.addProjectWork(obsProject)

	def addProjectWork(self, obsProject):
		for obsBuild in sorted(obsProject.updateBinaryList(self.client), key = lambda b: b.buildTime):
			proxy = self.PackageProxy(obsProject, obsBuild)

			shouldInspect = True

			existingProxy = self._proxies.get(obsBuild.name)
			if existingProxy is None:
				if proxy.guessTrueName(self):
					if proxy.name == 'patchinfo':
						# debugOBS(f"  -> {obsBuild.name} is a patchinfo - ignore")
						continue

					# infomsg(f"::: {obsBuild.name} -> {proxy.name}")
					existingProxy = self._proxies.get(proxy.name)

			# FIXME: for a complete package genealogy, we also need to
			# record the first successful build of each package, along with
			# the RPMs it contained.

			if existingProxy is None:
				# debugOBS(f"  -> {proxy} not known yet")
				self._validPackageNames.add(proxy.name)
			elif proxy.isValidBuild:
				if existingProxy.isValidBuild:
					self.verifyRPMs(existingProxy, proxy)

				shouldInspect = True
			else:
				shouldInspect = False

			if shouldInspect:
				assert(proxy)
				self._proxies[proxy.name] = proxy

	def processUpdates(self, backingStore):
		infomsg(f"Processing {len(self._proxies)} OBS builds")

		worker = DependencyWorker(self.client, backingStore)
		self.queue = SimpleQueue(len(self._proxies))

		worker.rpmToBackingStore(self.unresolved, updateLatest = True)

		for proxy in self._proxies.values():
			if self.shouldUpdate(proxy, worker):
				infomsg(f"  {proxy.name:30} {proxy}")
				self.queue.append(proxy)

		for proxy in self.queue:
			with loggingFacade.temporaryIndent(3):
				with backingStore.deferCommit():
					if not proxy.isValidBuild:
						infomsg(f"{proxy} is not a valid build; skipped")
						continue

					obsPackage = proxy.build
					obsProject = proxy.project

					infomsg(f"Updating {obsPackage.canonicalName} from {proxy}")

					for rpm in proxy.build.binaries:
						infomsg(f" # {rpm}")
						id = backingStore.latest.allocateIdForRpm(rpm)

					# obsProject.updateOnePackageFromOBS(self.client, worker, obsPackage, backingStore)
					self.updateOnePackageFromOBS(worker, proxy, backingStore)

	def shouldUpdate(self, proxy, worker):
		obsBuild = proxy.build

		if not proxy.isValidBuild:
			debugOBS(f"{proxy} is not a valid build; skipped")
			return False

		storedBuild = worker.knownPackages.get(obsBuild.canonicalName)
		if storedBuild is None:
			debugOBS(f"  -> {obsBuild.name} never seen before")
			return True

		proxy.previousBuild == storedBuild

		if self.onlyPackages:
			return self.onlyPackages.match(obsBuild.canonicalName)

		if storedBuild.buildTime == obsBuild.buildTime:
			# debugOBS(f"  -> {obsBuild.name} unchanged")
			return False

		debugOBS(f"  -> {obsBuild.name} has been rebuilt")
		return True

	def updateOnePackageFromOBS(self, worker, proxy, backingStore):
		obsProject = proxy.project
		obsPackage = proxy.build

		worker.buildToBackingStore(obsPackage)

		# ensure we have a backing store ID for the source
		# Not all builds will have a source package; such as those
		# that merely import packages from a different buildarch
		# (eg glibc:686)
		src = obsPackage.sourcePackage
		if src is not None:
			assert(src.backingStoreId is not None)
			# worker.rpmToBackingStore(src, updateLatest = True)

		for rpm, info in obsProject.retrieveBuildExtendedInfo(self.client, obsPackage):
			rpm.product = obsProject.product

			# make sure we've set the source ID
			if not rpm.isSourcePackage and src is not None:
				rpm.setSourcePackage(src)

			if rpm.buildArch != obsProject.buildArch:
				infomsg(f"{rpm}: do not update dependencies, as it has been imported from a different build arch")
				worker.rpmToBackingStore(rpm)
				continue

			# debugOBS(f"{obsPackage}: updating dependencies for {rpm}")
			worker.updateDependencies(obsProject.product, rpm, info)

		backingStore.updateOBSPackage(obsPackage, obsPackage.binaries, updateTimestamp = True)

	# Compare list of RPMs between two builds of the same OBS package and mbuild flavor.
	# This is currently just for logging purposes
	def verifyRPMs(self, oldProxy, newProxy):
		oldNames = set(rpm.shortname for rpm in oldProxy.build.binaries)
		newNames = set(rpm.shortname for rpm in newProxy.build.binaries)

		if oldNames == newNames:
			return

		removed = oldNames.difference(newNames)
		added = newNames.difference(oldNames)

		hop = self.evolutionLog.add(newProxy.name, str(newProxy))
		hop.added = list(added)
		hop.removed = list(removed)

		w = []
		if removed:
			if removed == oldNames:
				w.append("all old rpms removed")
			else:
				w.append(f"{' '.join(removed)} removed")

		if added:
			if added == newNames:
				w.append("all rpms are NEW")
			else:
				w.append(f"{' '.join(added)} added")
		elif not newNames:
			w.append("no RPMs left")

		detail = "; ".join(w)
		debugOBS(f"RPM change {oldProxy} -> {newProxy}: {detail}")

class PostprocessingJob(object):
	def __init__(self, productFamily, onlyPackages = None, evolutionLog = None):
		self.name = productFamily.name
		self.resolverHints = productFamily.resolverHints
		self.onlyPackages = onlyPackages

		if self.resolverHints is None:
			raise Exception("Unable to post-process package dependencies: no resolver hints")

		self.genealogy = None
		if evolutionLog is not None and os.path.exists(evolutionLog):
			with TimedExecutionBlock(f"Loading genealogy data for {productFamily}"):
				self.genealogy = Genealogy.loadFromEvolutionLog(evolutionLog)

	def processDependencies(self, backingStore):
		with backingStore.deferCommit():
			self.processDependenciesWork(backingStore)

	# In OBS fileinfo_ext data, resolved requirements are represented showing all possible
	# candidates. In a few cases, it is important to retain this ambiguity when labelling
	# packages, but in many cases, this redundancy needs to be hidden (eg. OBS expands
	# requirements on the perl interpreter as "perl" and "perl-32bit".
	def processDependenciesWork(self, backingStore):
		infomsg(f"Postprocessing packages for {self.name}")

		unresolved = backingStore.recoverLatestPackageByName('__unresolved__', arch = "noarch")
		assert(unresolved)

		# FIXME: turn this into a report object that that caller can investigate
		# and, if necessary, fail on
		unresolvableAmbiguities = 0
		unresolvableDependencies = 0
		buildsWithFailedDependencies = set()

		allBuilds = list(backingStore.enumerateOBSPackages())
		if self.onlyPackages is not None:
			queue = SimpleQueue(len(allBuilds))
			for obsPackage in allBuilds:
				if self.onlyPackages.match(obsPackage.name):
					queue.append(obsPackage)
		else:
			queue = SimpleQueue(allBuilds)

		for obsPackage in queue:
			if self.onlyPackages and not self.onlyPackages.match(obsPackage.name):
				continue

			infomsg(f" - {obsPackage}")

			disambiguationContext = self.resolverHints.createDisambiguationContext(obsPackage)
			src = obsPackage.sourcePackage
			for rpm in obsPackage.binaries:
				if rpm.sourcePackage is None and not rpm.isSourcePackage:
					rpm.sourcePackage = src

				requires = backingStore.retrieveForwardDependenciesFullTree(rpm)

				infomsg(f"    - {rpm}")
				result = disambiguationContext.inspect(rpm, requires)
				if result.ambiguous:
					errormsg(f"{rpm}: unable to resolve ambiguous dependency")
					for req in result.ambiguous:
						names = sorted(req.names)
						errormsg(f"     {req.name:30} {names[0]}")
						for other in names[1:]:
							errormsg(f"     {' ':30} {other}")

					unresolvableAmbiguities += 1
					continue

				requires = result.createUpdatedDependencies()

				try:
					backingStore.updateSimplifiedDependencies(rpm, requires)
					continue
				except Exception as e:
					infomsg(f"   While updating dependencies for {rpm}: {e}")
					savedException = e

				hardFail = False
				for dep, missing in backingStore.getStaleDependencies(rpm, requires):
					with loggingFacade.temporaryIndent():
						infomsg(f"Missing dependencies for requires={dep.expression}: {' '.join(map(str, missing))}")
						packages = set(dep.packages)
						for missingRpm in missing:
							if missingRpm.name.endswith('-debuginfo'):
								infomsg(f"   Hiding debuginfo dependency {missingRpm}")
								packages.remove(missingRpm)
								continue

							replacementRpm = self.getEvolvedRPM(backingStore, missingRpm, dep.expression)
							if replacementRpm is None:
								errormsg(f"stale dependency {missingRpm} and no idea what it may have evolved into")
								if unresolved is None:
									hardFail = True
									continue
								replacementRpm = unresolved

							packages.remove(missingRpm)
							packages.add(replacementRpm)
							infomsg(f"   Evolved {missingRpm} -> {replacementRpm}")

						dep.packages = packages

				if hardFail:
					buildsWithFailedDependencies.add(obsPackage)
					unresolvableDependencies += 1
					continue

				# Try once more - this time around, we should succeed
				backingStore.updateSimplifiedDependencies(rpm, requires)

		if buildsWithFailedDependencies:
			infomsg(f"The following builds had packages with stale dependencies:")
			for buildName in sorted(map(str, buildsWithFailedDependencies)):
				infomsg(f" - {buildName}")

		if unresolvableAmbiguities or unresolvableDependencies:
			problems = []

			if unresolvableAmbiguities:
				problems.append(f"{unresolvableAmbiguities} unresolvable ambiguities")
			if unresolvableDependencies:
				problems.append(f"{unresolvableDependencies} unresolvable dependencies")

			raise Exception(f"Uncorrected problems while post-processing dependencies: {' '.join(problems)}")

	PackageRenames = {
		'libdebuginfod1-dummy':  'libdebuginfod1',
		'libglue2':              'cluster-glue-libs',
		'libgnome-desktop-3-12': 'libgnome-desktop-3-20',
		'systemd-sysvinit':      'systemd-sysvcompat',
		'dbus-1-glib':           'libdbus-glib-1-2',

		'libsmbclient0':         'samba-client-libs',
		'libdcerpc0':            'samba-client-libs',
		'libdcerpc-binding0':    'samba-client-libs',
		'libndr0':               'samba-client-libs',
		'libndr-krb5pac0':       'samba-client-libs',
		'libndr-nbt0':           'samba-client-libs',
		'libndr-standard0':      'samba-client-libs',
		'libnetapi0':            'samba-client-libs',
		'libsamba-credentials0': 'samba-client-libs',
		'libsamba-errors0':      'samba-client-libs',
		'libsamba-hostconfig0':  'samba-client-libs',
		'libsamba-passdb0':      'samba-client-libs',
		'libsamba-util0':        'samba-client-libs',
		'libsamdb0':             'samba-client-libs',
		'libsmbconf0':           'samba-client-libs',
		'libsmbldap2':           'samba-client-libs',
		'libtevent-util0':       'samba-client-libs',
		'libwbclient0':          'samba-client-libs',
		'libsmbclient-devel':	 'samba-devel',
		'kmod-compat':           'kmod',
		'hamcrest-core':         'hamcrest',
		'ImageMagick-config-7-upstream':
					 'ImageMagick-config-7-upstream-open',
		'libgnomekbd':           'libgnomekbd8',
		'texlive-pstools-bin':   'texlive-ps2eps-bin',
		'pipewire-modules':      'pipewire-modules_0_3',
		'libpacemaker3':         'pacemaker-libs',
		'libpacemaker-devel':	 'pacemaker-devel',
		'libSDL2-devel':	 'SDL2-devel',
		'libpolkit0':		 'libpolkit-agent-1-0',
		'libglslang-suse9':	 'libglslang14',

		'Mesa-libGLESv1_CM1':	 'libglvnd',
		'Mesa-libGLESv2-2':	 'libglvnd',
		'libwayland-egl-devel':	 'libglvnd-devel',
		'libwayland-egl-devel-32bit':
					 'libglvnd-devel-32bit',

		'libicu60_2':		 'libicu-suse65_1',
		'libudev-devel':	 'systemd-devel',
		'libudev-devel-32bit':	 'systemd-devel-32bit',

		'bind-devel':		 'bind',
		'libbind9-160':		 'bind',
		'libdns169':		 'bind',
		'libirs160':		 'bind',
		'libisc166':		 'bind',
		'libisccc160':		 'bind',
		'libisccfg160':		 'bind',
		'liblwres160':		 'bind',
		'bind-devel-32bit':	 'bind',
		'libbind9-160-32bit':	 'bind',
		'libdns169-32bit':	 'bind',
		'libirs160-32bit':	 'bind',
		'libisc166-32bit':	 'bind',
		'libisccc160-32bit':	 'bind',
		'libisccfg160-32bit':	 'bind',
		'liblwres160-32bit':	 'bind',
		'libbind9-1600':	 'bind',
		'libdns1605':		 'bind',
		'libirs1601':		 'bind',
		'libisc1606':		 'bind',
		'libisccc1600':		 'bind',
		'libisccfg1600':	 'bind',
		'liblvm2app2_2':	 'bind',
		'libns1604':		 'bind',

		'libnm-glib4':		 'NetworkManager',
		'libnm-glib-vpn1':	 'NetworkManager',
		'libnm-util2':		 'NetworkManager',

		# Pretend:
		'texlive-pstools':	 'texlive-scripts',
		'texlive-ifluatex':	 'texlive-scripts',
		'texlive-ifxetex':	 'texlive-scripts',
		'texlive-tetex-bin':	 'texlive-scripts',
		'texlive-tetex':	 'texlive-scripts',
		'texlive-texconfig-bin': 'texlive-scripts',
		'texlive-texconfig':	 'texlive-scripts',

		'libmysqld19':		 'libmariadbd19',
		'libmysqld-devel':	 'libmariadbd-devel',

		'rust-std':		 'rust',

		'libpmemobj++-devel':	 'pmdk-devel',
		'libwicked-0-6':	 'wicked',
		'openblas-devel':	 'openblas-common-devel',
		'openblas-devel-headers':'openblas-common-devel',

		# the update that renames libgrpc8 provides two new libraries
		# named librpc, unfortunately.
		'libgrpc8':		 'libgrpc1_60',

		# this actually got split in two, but finding one is enough
		'libiptc0':		 'libip6tc2',

		'libwebrtc_audio_processing1':
					 'libwebrtc-audio-processing-1-3',

		'xerces-j2-xml-apis':	 'xerces-j2-javadoc',
		'yast2-theme-SLE':	 'yast2-theme',
		'gcr-data':		 'libgcr-4-4',
		'gcr-prompter':		 'libgcr-4-4',

		'python-contextlib2':	 'python3-contextlib2',
		'sysprof-devel-static':	 'sysprof-devel',

	}


	def getEvolvedRPM(self, backingStore, missingRpm, depString):
		# if we have a depString, look at other packages that use the same depString
		# and check what OBS evolved it to
		if depString:
			pass # not yet

		found = None

		# We need to check our explicit rules first because the
		# genealogy checks cannot catch all package renames etc.
		# In that case, it returns the verdict "dropped", which we
		# consider final
		tryName = self.PackageRenames.get(missingRpm.name)
		if tryName is not None:
			found = backingStore.recoverLatestPackageByName(tryName, arch = missingRpm.arch)

		rpmName = missingRpm.name
		if found is None and rpmName.startswith('python3-'):
			found = backingStore.recoverLatestPackageByName('python311-' + rpmName[8:], arch = missingRpm.arch)

		# SLE15 has some packages that are still built with python2 (lua-lmod is an example)
		# Pretend that we would be able to rebuild these packages with python3.x
		if found is None and rpmName.startswith('python2-'):
			found = backingStore.recoverLatestPackageByName('python311-' + rpmName[8:], arch = missingRpm.arch)

		if found is None and rpmName.startswith('python-'):
			found = backingStore.recoverLatestPackageByName('python311-' + rpmName[7:], arch = missingRpm.arch)

		if found is not None:
			return found

		latest = None
		if self.genealogy:
			# The getLatestDescendant interface is not optimal.
			# We should probably have Genealogy generate a dict mapping of
			# older rpm shortnames to the final names.
			latest = self.genealogy.getLatestDescendant(missingRpm.shortname, None)

			if latest is None and "openmpi_" in missingRpm.name:
				# HPC hack. The package name is eg openmpi4, but the rpm
				# name is libopenmpi_4_x_y-blafasel-gnu-hpc
				i = missingRpm.name.index("openmpi_")
				majorVersion = missingRpm.name[i + 8]
				if majorVersion.isdigit():
					buildName = "openmpi" + majorVersion
					latest = self.genealogy.getLatestDescendant(missingRpm.shortname, buildName)

			if latest is not None:
				if not latest.valid:
					errormsg(f"Unable to evolve {missingRpm}: package was dropped from distribution")
					return None

				# FIXME: how do we ensure we get a package with the right arch?
				found = backingStore.recoverLatestPackageByName(latest.name, arch = latest.arch)
				if found is not None:
					return found

		return None

	def purgeStale(self, backingStore):
		pass

class OBSProject:
	def __init__(self, name, product = None):
		self.name = name
		self.product = product
		self.resolverHints = None
		self.buildRepository = "standard"
		self.buildArch = None
		self._projectConfig = None
		self._projectMeta = None
		self._packages = {}
		self._binaries = BinaryMap()

		self._dryRunProjectMeta = False

		if product:
			self.resolverHints = product.resolverHints
			self.buildArch = product.arch

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

	@property
	def projectMeta(self):
		if self._projectMeta is None:
			self._projectMeta = OBSProjectMeta(self.name)
		return self._projectMeta

	def ignorePackage(self, pinfo):
		name = pinfo.name

		for suffix in ("-debuginfo", "-debugsource"):
			if name.endswith(suffix):
				return True

		return False

	def queryProjectMeta(self, client, force = True):
		if self._projectMeta is not None:
			if self._dryRunProjectMeta or not force:
				return self._projectMeta

		meta = client.getMetaXML(self.name, quiet = True)
		if meta is not None:
			result = client._schema.processProjectMeta(meta)
		else:
			result = None

		self._projectMeta = result
		return result

	def buildInitialProjectMeta(self, client):
		return client.buildInitialProjectMeta(self.name)

	def updateProjectMeta(self, client, projectMeta, dryRun = False):
		xmldoc = client._schema.buildProjectMeta(projectMeta)

		if dryRun:
			logXML(f"Would update {self.name}/_meta", xmldoc)
			self._projectMeta = projectMeta
			self._dryRunProjectMeta = True
			return True

		if not client.apiCallPUT("source", self.name, "_meta", xmldoc = xmldoc):
			errormsg(f"{self.name}: failed to update project meta")
			infomsg(f"The rejected document was:")
			for line in xmltree.toString(xmldoc).split('\n'):
				infomsg(f"> {line}")
			return False

		self._projectMeta = projectMeta
		return True

	def queryProjectConfig(self, client):
		return client.apiCallText('source', self.name, '_config')

	def updateProjectConfig(self, client, projectConfig, dryRun = False):
		if dryRun:
			infomsg(f"Would update {self.name}/_config\n{projectConfig}")
			return True

		return client.apiCallPUT("source", self.name, "_config", data = str(projectConfig))

	def querySourcePackages(self, client):
		result = client.querySourcePackages(project = self.name);
		if result is None:
			if self._dryRunProjectMeta:
				return []

		return result

	def updateSourceList(self, client):
		debugOBS(f"Getting source packages for {self.name}")
		names = self.querySourcePackages(client)

		for name in names:
			self.addPackage(name)

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


	def updateBinaryList(self, client):
		debugOBS(f"Getting build results for {self.name}")
		resList = self.queryBuildResults(client)

		result = []
		for st in resList[0].status_list:
			pkg = self.addPackage(st.package)
			pkg.setBuildStatus(st.code)
			result.append(pkg)

		for p in resList[0].binary_list:
			pkg = self.addPackage(p.package)
			self.updateBuildFromBinaryList(pkg, p.files)

		return result

	def updateBuildFromBinaryList(self, build, binaryList):
		binaries = []
		buildTime = 0

		for f in binaryList:
			filename = f.filename

			if f.mtime is not None:
				fileBuildTime = int(f.mtime)
				if fileBuildTime > buildTime:
					buildTime = fileBuildTime

			buildArch = self.buildArch
			if filename.startswith("::"):
				words = filename[2:].split("::")
				if not words:
					raise Exception(filename)

				special = words.pop(0)
				if special == 'import' and len(words) == 2:
					buildArch, filename = words
				else:
					warnmsg(f"build results for {self.name} contain unexpected binary element {filename}")
					continue

			if not filename.endswith(".rpm"):
				continue
			pinfo = PackageInfo.parsePackageName(filename)
			pinfo.buildTime = int(f.mtime)

			if self.ignorePackage(pinfo):
				continue

			rpm = self.product.findPackageByInfo(pinfo, create = True)
			rpm.buildArch = buildArch
			binaries.append(rpm)
			assert(rpm)

		build.buildTime = buildTime
		build._binaries = binaries
		build._source = None

	def retrieveBuildExtendedInfo(self, client, obsPackage):
		try:
			return self.tryRetrieveBuildExtendedInfo(client, obsPackage)
		except Exception as e:
			infomsg(f"Exception while trying to get depdency info for {obsPackage}: {e}")
			infomsg("Retrying...")

		# refresh the binarylist for this build, do NOT cache
		updatedListing = client.queryPackageBuild(self.name, self.buildRepository, obsPackage.name, self.buildArch, cachingOff = True)
		self.updateBuildFromBinaryList(obsPackage, updatedListing)

		try:
			return self.tryRetrieveBuildExtendedInfo(client, obsPackage)
		except Exception as e:
			errormsg(f"Exception while trying to get depdency info for {obsPackage}: {e}")

		# FIXME: flag this package as failed
		return []

	def tryRetrieveBuildExtendedInfo(self, client, obsPackage):
		result = []

		infoFactory = UniquePackageInfoFactory()

		buildInfo = self.queryPackageBuildInfo(client, obsPackage)
		for rpm in obsPackage.binaries:
			info = client.getFileInfoExt(self.name, self.buildRepository, obsPackage.name, rpm.buildArch, rpm.fullname(), infoFactory = infoFactory)
			if info is None:
				raise Exception(f"Unable to obtain fileinfo for {rpm.fullname()}")

			if rpm.isSourcePackage:
				requires_ext = []
				for dep in buildInfo.builddeps:
					if dep.preinstall == "1" or dep.vminstall == "1" or dep.notmeta == "1":
						# FIXME: record these somewhere
						# infomsg(f"{rpm}: ignoring build dependency {dep.name} preinstall={dep.preinstall} vminstall={dep.vminstall} notmeta={dep.notmeta}")
						continue

					pinfo = infoFactory(name = dep.name, version = dep.version, release = dep.release, arch = dep.arch)

					req = OBSDependency(expression = f'obsbuild({pinfo.name})')
					req.packages.add(pinfo)

					requires_ext.append(req)

				infomsg(f"{rpm}: extracting {len(requires_ext)} dependencies from {obsPackage}/_buildinfo")
				info.requires_ext = requires_ext

			result.append((rpm, info))

		return result

	def getPackage(self, name):
		return self._packages.get(name)

	def addPackage(self, name):
		pkg = self._packages.get(name)
		if pkg is None:
			pkg = OBSPackage(name)
			self._packages[pkg.name] = pkg
		return pkg
