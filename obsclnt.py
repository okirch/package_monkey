import osc
import os
import xmltree
import posix
import time
import xml.etree.ElementTree as ET

from packages import Package, PackageInfo, PackageInfoFactory, UniquePackageInfoFactory
from util import ThatsProgress
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

class OBSPackage:
	STATUS_SUCCEEDED = 1
	STATUS_FAILED = 2
	STATUS_EXCLUDED = 3
	STATUS_UNKNOWN = 4

	def __init__(self, name):
		self.name = name
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

		if ':' in name:
			self._basePackageName = name.split(':')[0]
		else:
			self._basePackageName = None

		self.requires = None

	def __str__(self):
		return self.name

	def addBinary(self, pinfo):
		self._binaries.append(pinfo)

	@property
	def basePackageName(self):
		return self._basePackageName

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
	def __init__(self, project, client, backingStore):
		self.project = project
		self.client = client
		self.backingStore = backingStore

		self.resolverHints = project.resolverHints
		self.product = project.product

		self.knownPackages = {}
		for storedBuild in backingStore.enumerateOBSPackages():
			self.knownPackages[storedBuild.name] = storedBuild

		self.progressMeter = None

		self.previousIncarnation = {}

		self.inProgress = None
		self.queue = []

	def maybeQueueForInspection(self, build, nameMatcher):
		shouldInspect = True

		storedBuild = self.knownPackages.get(build.name)
		if storedBuild is None:
			debugOBS(f"  -> {build.name} not known yet")
		elif nameMatcher:
			shouldInspect = nameMatcher.match(build.name)
		elif storedBuild.buildTime != build.buildTime:
			debugOBS(f"  -> {build.name} has been rebuilt")
		else:
			# debugOBS(f"  -> {build.name} unchanged")
			shouldInspect = False

		if shouldInspect:
			self.queue.append(build)

		if storedBuild is not None:
			self.previousIncarnation[build] = storedBuild
			build.backingStoreId = storedBuild.backingStoreId
		else:
			# fixme add the build to the DB
			pass

		for rpm in build.binaries:
			if not rpm.isSourcePackage:
				storedRpm = self.backingStore.recoverLatestPackageByName(rpm.name)
				if storedRpm is not None:
					self.previousIncarnation[rpm] = storedRpm

	def __bool__(self):
		return bool(self.queue)

	def startProgressMeter(self, numTotal):
		if self.progressMeter is not None:
			return

		numRebuilt = len(self.queue)
		infomsg(f"{numRebuilt}/{numTotal} packages have been rebuilt")
		self.progressMeter = ThatsProgress(numRebuilt, withETA = True)

	def next(self):
		if self.progressMeter is not None:
			self.done()

		if not self.queue:
			return None

		obsPackage = self.queue.pop(0)

		storedBuild = self.previousIncarnation.get(obsPackage)

		if storedBuild is None:
			versionInfo = f"NEW {obsPackage.sourceVersion}"
		else:
			versionInfo = f"{storedBuild.sourceVersion} -> {obsPackage.sourceVersion}"

		infomsg(f"Inspecting {obsPackage} {versionInfo} (status={obsPackage.buildStatusString})")

		self.inProgress = obsPackage
		return obsPackage

	def done(self):
		if self.inProgress is None:
			return

		self.progressMeter.tick()
		self.inProgress = None

		if len(self.queue) == 0:
			infomsg("Completed.")
		else:
			infomsg(f"{self.progressMeter} complete, {self.progressMeter.eta} remaining")

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

	def resolvePackageInfo(self, pinfoList):
		result = set()

		if not pinfoList:
			return result

		for pinfo in pinfoList:
			if self.ignorePackage(pinfo):
				continue

			dependent = self.product.findPackage(pinfo.name, pinfo.version, pinfo.release, pinfo.arch)
			if dependent is None:
				dependent = Package.fromPackageInfo(pinfo)
				self.product.addPackage(dependent)

			self.rpmToBackingStore(dependent)

			assert(dependent.backingStoreId)
			result.add(dependent)

		return result

	def updateDependencies(self, disambiguationContext, rpm, info):
		self.rpmToBackingStore(rpm)

		assert(info.requires_ext is not None)

		requires = info.requires_ext
		for dep in requires:
			rpms = set()
			for pinfo in dep.packages:
				requiredRpm = Package.fromPackageInfo(pinfo)
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

		requiredby = self.resolvePackageInfo(requiredby)

		rpm.updateResolvedProvides(requiredby)

	def ignorePackage(self, pinfo):
		name = pinfo.name

		for suffix in ("-debuginfo", "-debugsource"):
			if name.endswith(suffix):
				return True

		return False


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

	def updateEverything(self, client, backingStore, onlyPackages = None):
		if client.cachePolicy == 'exclusive':
			infomsg(f"Skipping download of data from OBS - cache policy is {client.cachePolicy}")
			return

		packages = self.updateBinaryList(client)

		numTotal = len(packages)

		worker = DependencyWorker(self, client, backingStore)

		for obsPackage in packages:
			if obsPackage.buildTime is not None:
				worker.maybeQueueForInspection(obsPackage, onlyPackages)

		if onlyPackages is not None:
			badNames = onlyPackages.reportUnmatched()
			if badNames:
				errormsg(f"Bad package name(s) given on command line: {' '.join(badNames)}")
				raise Exception(f"Package names not found")

		worker.startProgressMeter(numTotal)

		packages = self.updatePackagesFromOBS(client, worker, backingStore)
		infomsg(f"{len(packages)} out of {numTotal} packages were updated")

	def processDependencies(self, backingStore, onlyPackages = None):
		with backingStore.deferCommit():
			self.processDependenciesWork(backingStore, onlyPackages)

	# In OBS fileinfo_ext data, resolved requirements are represented showing all possible
	# candidates. In a few cases, it is important to retain this ambiguity when labelling
	# packages, but in many cases, this redundancy needs to be hidden (eg. OBS expands
	# requirements on the perl interpreter as "perl" and "perl-32bit".
	def processDependenciesWork(self, backingStore, onlyPackages):
		# for rpm in backingStore.enumerateLatestPackages():
		if self.resolverHints is None:
			raise Exception("Unable to post-process package dependencies: no resolver hints")

		worker = DependencyWorker(self, None, backingStore)

		unresolvableAmbiguities = 0
		for obsPackage in backingStore.enumerateOBSPackages():
			if onlyPackages and not onlyPackages.match(obsPackage.name):
				continue

			disambiguationContext = self.resolverHints.createDisambiguationContext(obsPackage)
			src = obsPackage.sourcePackage
			for rpm in obsPackage.binaries:
				if rpm.sourcePackage is None and not rpm.isSourcePackage:
					rpm.sourcePackage = src

				requires = backingStore.retrieveForwardDependenciesFullTree(rpm)

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

				backingStore.updateSimplifiedDependencies(rpm, requires)

		if unresolvableAmbiguities:
			raise Exception(f"Detected {unresolvableAmbiguities} unresolvable ambiguities while post-processing dependencies")

	def purgeStale(self, backingStore):
		pass

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

		for st in resList[0].status_list:
			pkg = self.addPackage(st.package)
			pkg.setBuildStatus(st.code)

		result = set()
		for p in resList[0].binary_list:
			pkg = self.addPackage(p.package)

			self.updateBuildFromBinaryList(pkg, p.files)
			result.add(pkg)

		return result

	def updateBuildFromBinaryList(self, build, binaryList):
		binaries = []
		buildTime = None

		for f in binaryList:
			filename = f.filename

			if f.mtime is not None:
				fileBuildTime = int(f.mtime)
				if buildTime is None or fileBuildTime > buildTime:
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

	def updatePackagesFromOBS(self, client, worker, backingStore):

		result = []
		while worker:
			obsPackage = worker.next()

			with loggingFacade.temporaryIndent(3):
				with backingStore.deferCommit():
					worker.buildToBackingStore(obsPackage)

					# ensure we have a backing store ID for the source
					# Not all builds will have a source package; such as those
					# that merely import packages from a different buildarch
					# (eg glibc:686)
					src = obsPackage.sourcePackage
					if src is not None:
						assert(src.backingStoreId is not None)
						# worker.rpmToBackingStore(src, updateLatest = True)

					context = None
					if self.resolverHints is not None:
						context = self.resolverHints.createDisambiguationContext(obsPackage)

					for rpm, info in self.retrieveBuildExtendedInfo(client, obsPackage):
						# make sure we've set the source ID
						if not rpm.isSourcePackage and src is not None:
							rpm.setSourcePackage(src)

						if rpm.buildArch != self.buildArch:
							infomsg(f"{rpm}: do not update dependencies, as it has been imported from a different build arch")
							worker.rpmToBackingStore(rpm)
							continue

						# debugOBS(f"{obsPackage}: updating dependencies for {rpm}")
						worker.updateDependencies(context, rpm, info)

					backingStore.updateOBSPackage(obsPackage, obsPackage.binaries, updateTimestamp = True)

			result.append(obsPackage)

		return result

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
