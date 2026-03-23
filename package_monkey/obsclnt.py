##################################################################
#
# Query OBS for all sorts of wonderful stuff and tie it together.
# Much of this is now obsolete given that we extract dependency
# data from the rpm headers.
#
# FIXME: clean up this mess!
#
##################################################################
import osc
import os
import posix
import time
import io
import hashlib
import re
import xml.etree.ElementTree as ET

from .newdb import RpmInfo, UniquePackageInfoFactory
from .filter import Classification
from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .download import *
import package_monkey.xmltree as xmltree

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
				if grandchild.tag in ('scmsync', 'scminfo', 'scc'):
					pass
				elif grandchild.tag == 'status':
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

	def processBinaryVersionListing(self, xmlnode):
		if not self.checkRootNodeTag(xmlnode, 'binaryversionlist'):
			return None

		result = []
		for child in xmlnode:
			if child.tag != 'binary':
				self.unexpectedElement(child, xmlnode)
				continue

			f = self.processSimpleXML(child, ('name', 'sizek', 'hdrmd5'), [])
			f.filename = f.name
			result.append(f)

		return result

	def processFileInfo(self, xmlnode, infoFactory = None):
		if not self.checkRootNodeTag(xmlnode, 'fileinfo'):
			return None

		if infoFactory is None:
			infoFactory = UniquePackageInfoFactory()

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
				errormsg(f"ignoring <{child.tag}> inside directory listing")
				continue

			name = child.attrib.get('name')
			if name is not None:
				result.append(name)

		return result

	def processStagingProjects(self, node):
		if not self.checkRootNodeTag(node, 'staging_projects'):
			return None

		result = []
		for child in node:
			if child.tag != 'staging_project':
				errormsg(f"ignoring <{child.tag}> inside staging project listing")
				continue

			entry = OBSSchema.Dummy()
			entry.name = child.attrib['name']
			entry.state = child.attrib.get('state')

			# We're not yet interested in any other interesting bits of data that
			# OBS can give us, so ignore those for now.
			result.append(entry)

		return result

	def processSimpleXML(self, node, requiredAttrs, optionalAttrs):
		result = OBSSchema.Dummy()
		attr = node.attrib

		for name in requiredAttrs:
			if name not in attr:
				suse.error("element <%s> lacks required attribute %s" % (node.tag, name))

				xml.etree.ElementTree.dump(node)

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
	class Entry(object):
		def __init__(self, path):
			self.path = path
			self.valid = False
			self.data = None

		def __bool__(self):
			return os.path.exists(self.path)

		@property
		def exists(self):
			return os.path.exists(self.path)

		@property
		def age(self):
			mtime = posix.stat(self.path).st_mtime
			return time.time() - mtime

		def open(self, mode = "r"):
			if self.data is not None:
				return io.StringIO(self.data)

			return open(self.path, mode)

		def write(self, res):
			debugCache(f"Write cache entry to {self.path}")
			assert(type(res) is str)

			try:
				self.makedir(os.path.dirname(self.path))
				f = open(self.path, "w")
			except Exception as e:
				warnmsg(f"Cannot write cache entry {self.path}: {e}")
				f = None

			if f is not None:
				f.write(res)
			else:
				self.data = res

			self.valid = True

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
		self.cachePolicy = 'none'
		self._maxCacheAge = 0
		self._allowApiCalls = True

	def setCachePath(self, path):
		self._cache = HTTPCache(path)

	def setCacheTTL(self, ttl):
		if ttl <= 0:
			self.cachePolicy = 'none'
			self._maxCacheAge = 0
		else:
			self.cachePolicy = 'default'
			self._maxCacheAge = ttl

	@property
	def cachingEnabled(self):
		return self._maxCacheAge != 0

	def getHTTPCacheEntry(self, *args, **kwargs):
		if self._cache is None or self._maxCacheAge == 0:
			return None

		cacheEntry = self._cache.getEntry(*args, **kwargs)
		if cacheEntry is not None  and cacheEntry.exists and (self._maxCacheAge is None or cacheEntry.age < self._maxCacheAge):
			cacheEntry.valid = True

		return cacheEntry

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
		if method == "GET" and cacheEntry is None and not cachingOff:
			cacheEntry = self.getHTTPCacheEntry(fullUrl)
		if cacheEntry is not None and cacheEntry.valid and not cachingOff:
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

	def queryBuildArchitectures(self, project, repoName,  **params):
		xml = self.apiCallXML('build', project, repoName,  **params)
		if xml is None:
			errormsg(f"Cannot find build/{project}")
			return None

		return self._schema.processDirectoryListing(xml)

	def queryBuildResult(self, project, **params):
		xml = self.apiCallXML('build', project, "_result", **params)
		if xml is None:
			errormsg(f"Cannot find build/{project}/_result")
			return None

		return self._schema.processBuildResult(xml)

	def queryBuildRepository(self, project, repository, arch, **params):
		xml = self.apiCallXML("build", project, repository, arch, "_repository", **params)
		if xml is None:
			errormsg(f"Cannot find build/{project}/{repository}/{arch}/_repository")
			return None

		if xml.tag == 'binaryversionlist':
			return self._schema.processBinaryVersionListing(xml)
		return self._schema.processBinaryListing(xml)

	def getRepositoryArchState(self, project, repository, arch, **params):
		data = self.apiCallText("build", project, repository, arch, "_repository, **params")
		if data is None:
			errormsg(f"Cannot find build/{project}/{repository}/{arch}/_repository")
			return None

		return hashlib.sha1(data).hexdigest()[:7]

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

	def listStagings(self, project, **kwargs):
		res = self.apiCallXML('staging', project, 'staging_projects', **kwargs)
		if res is None:
			return None
		return self._schema.processStagingProjects(res)

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

class OBSBuildInfo(object):
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

	def __str__(self):
		return f"{self.expression} -> {{{', '.join(map(str, self.packages))}}}"

# This should really be called OBSBuild, for several reasons
#  - multibuilds. One source package foo may give rise to several builds,
#    named foo, foo:flavor1, foo:flavor2, etc
#  - maintenance. When foo gets built in a maintenance project (*:Update),
#    the first build will be called foo, but subsequent rebuilds will have
#    the maintenance incident attached (as in foo.12345)
#  - maintenance updates of multibuilds are currently built as
#    foo.12345:flavor1 etc
#
# Naming related members of OBSBuild objects:
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
class OBSBuildBase(object):
	STATUS_SUCCEEDED = 1
	STATUS_FAILED = 2
	STATUS_EXCLUDED = 3
	STATUS_UNKNOWN = 4

	def __init__(self, name, canonicalName = None, maintenanceIncident = 0, buildArch = None, synthetic = False):
		self.name = name
		self.buildArch = buildArch
		self.isSynthetic = synthetic

		self.updateCanonicalName(canonicalName or name, maintenanceIncident)

		self.projectHandle = None
		self.buildStatus = None
		self._buildStatusString = None
		self.backingStoreId = None
		self.buildTime = None
		self.rpmsUsedForBuild = None
		self._buildRequires = set()
		self._source = None
		self._binaries = []

		self.config = None
		self.trace = False

		self.requires = None

	def __str__(self):
		if self.buildArch:
			return f"{self.name}.{self.buildArch}"
		return self.name

	@property
	def projectId(self):
		if self.projectHandle is None:
			errormsg(f"{self}: projectId returns None")
			return None
		return self.projectHandle.backingStoreId

	def addBinary(self, pinfo):
		self._binaries.append(pinfo)

	def updateCanonicalName(self, canonicalName, maintenanceIncident):
		self.canonicalName = canonicalName
		self.maintenanceIncident = maintenanceIncident

		self.basePackageName = None
		self.multibuildFlavor = None

		if ':' in canonicalName:
			name, flavor = canonicalName.rsplit(':', maxsplit = 1)
			if '.' in flavor and not flavor.endswith('.spec'):
				warnmsg(f"Space oddities: strange multibuild name {canonicalName}")

			self.basePackageName = name
			self.multibuildFlavor = flavor

	@property
	def sourceVersion(self):
		pinfo = self.sourceRpm
		if pinfo is None:
			return None

		return pinfo.versionString

	@property
	def sourceRpm(self):
		if self._source is None:
			found = None
			for pinfo in self._binaries:
				if pinfo.arch in ('src', 'nosrc'):
					if found:
						infomsg(f"OBS build {self.name} provides more than one source package")
						infomsg(f"  {found.fullname}")
						infomsg(f"  {pinfo.fullname}")

					found = pinfo

			self._source = found

		return self._source

	@sourceRpm.setter
	def sourceRpm(self, value):
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

	def setLabel(self, **kwargs):
		raise Exception(f"Not supported any longer")

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

# We create these two classes so that we can distinguish between builds
# that come from different tables in the DB (the build_history table, which records
# the state of a specific build in the context of a project, like SUSE:SLE-15-SP1:Update/glibc.12345:utils),
# and the regular "builds" table, which records the most recent build in the context of a product
# (ie "glibc:utils").
# IOW, build_history is indexed by (build.projectHandle, build.name) whereas the regular builds table
# is indexed by build.canonicalName.
class OBSBuild(OBSBuildBase):
	pass

class OBSProjectMeta(object):
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

class OBSProjectHandle(object):
	def __init__(self, name, buildArch, repository = 'standard'):
		self.name = name
		self.buildArch = buildArch
		self.repository = repository

		self.backingStoreId = None
		self.timestamp = None

	def __str__(self):
		return f"{self.name}/{self.buildArch}"

	@property
	def fullname(self):
		return f"{self.name}/{self.repository}/{self.buildArch}"

class OBSProject(object):
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

		self.handle = None
		if product:
			self.resolverHints = product.resolverHints
			self.buildArch = product.arch

			self.handle = OBSProjectHandle(self.name, self.buildArch, repository = self.buildRepository)

		self.infoFactory = UniquePackageInfoFactory(self.buildArch)

		self._obsCache = None

	def __str__(self):
		if self.buildArch:
			return f"{self.name}/{self.buildRepository}/{self.buildArch}"
		return self.name

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

	def queryBuildArchitectures(self, client):
		resList = client.queryBuildArchitectures(project = self.name)
		return set(resList)

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

	def queryBuildRepository(self, client):
		resList = client.queryBuildRepository(
				project = self.name,
				repository = self.buildRepository,
				arch = self.buildArch,
				multibuild = 1,
				view = ('binaryversions'), nometa = 1)

		# Since we were pretty specific about the repo and arch, the result
		# should have exactly one element only
		assert(len(resList) == 1)

		return resList

	def queryPackageBuildInfo(self, client, obsBuild, **params):
		sourceVersion = obsBuild.sourceVersion
		if sourceVersion is None:
			errormsg(f"Cannot retrieve buildinfo for {obsBuild.name}: unable to identify version")
			return False

		cacheObjectName = f"{sourceVersion}/buildInfo"
		cacheEntry = self.getCacheEntry(cacheObjectName, project = self.name,
				repository = self.buildRepository,
				package = obsBuild.name,
				arch = self.buildArch)

		return client.getBuildInfo(self.name, self.buildRepository, obsBuild.name, self.buildArch,
				cacheEntry = cacheEntry, **params)

	def createDownloadManager(self, cacheRoot):
		if cacheRoot is None:
			destdir = archState.arch
		else:
			destdir = os.path.join(cacheRoot, self.name, self.buildRepository, self.buildArch)

		return RepositoryRpmDownadloadManager(destdir)

	def prepareDownload(self, client, downloadManager, filter = None):
		from urllib.parse import quote_plus

		fileList = client.queryBuildRepository(self.name, self.buildRepository, self.buildArch, view = 'binaryversions', nometa = 1, cachingOff = True)

		sha1 = hashlib.new('sha1')

		localNames = set()
		remoteNames = set()
		fileMap = {}
		for f in fileList:
			if not f.name.endswith('.rpm'):
				continue

			rpmName = f.name[:-4]

			if filter is not None and filter.matchRpm(rpmName):
				continue

			name = f"{f.hdrmd5}-{rpmName}.rpm"
			localNames.add(name)

			fileMap[name] = quote_plus(rpmName)

			sha1.update(name.encode('utf-8'))

		downloadQueue = DownloadQueue(downloadManager, localNames, fileMap)

		downloadQueue.remoteHash = sha1.hexdigest()
		return downloadQueue

	def performDownload(self, client, downloadQueue, progressMeter = None):
		if not downloadQueue:
			infomsg(f"{self}: all packages present")
			return

		infomsg(f"{self}: downloading {len(downloadQueue)} new packages")
		downloadManager = downloadQueue.downloadManager

		while downloadQueue:
			binaries = downloadQueue.popChunk(50)

			path = client.apiMakePath("build", self.name, self.buildRepository, self.buildArch, "_repository")
			res = client.apiCallRaw(path, view = 'cpioheaders', binary = binaries, cachingOff = True)
			if not res:
				raise Exception(f"Download failed: {path}")

			downloadManager.storeFromCpio(res)

			if progressMeter is not None:
				progressMeter.tick(len(binaries))
			infomsg(f"{progressMeter} {progressMeter.eta}: {binaries[0]}")

	# For the time being, this will update builds for a single arch only, but
	# we should support other arches as well.
	def updateBuildResults(self, client):
		debugOBS(f"Getting build results for {self.name}")
		resList = self.queryBuildResults(client)

		builds = []
		for result in resList:
			buildArch = result.arch

			for st in result.status_list:
				obsBuild = self.addBuild(st.package, buildArch)
				obsBuild.setBuildStatus(st.code)

			for p in result.binary_list:
				obsBuild = self.addBuild(p.package, buildArch)
				self.updateBuildFromBinaryList(obsBuild, p.files)

				# ignore any builds that generate preinstall images, containers
				# and whatnot.
				if obsBuild.binaries:
					builds.append(obsBuild)

		# And now update build requirements for all builds
		self.updateAllBuildDependencies(client, builds)

		return builds

	def updateBuildFromBinaryList(self, build, binaryList):
		source = None
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
			pinfo = RpmInfo.parsePackageName(filename, buildArch = buildArch)
			pinfo.buildTime = int(f.mtime)

			if self.ignorePackage(pinfo):
				continue

			if self.product is not None:
				rpm = self.product.findPackageByInfo(pinfo, create = True)
			else:
				rpm = pinfo

			rpm.buildArch = buildArch
			binaries.append(rpm)

			if rpm.isSourcePackage:
				if source is not None:
					raise Exception(f"{self}/{build}: duplicate source rpms {rpm}, {source}")
				source = rpm

			assert(rpm)

		build.buildTime = buildTime
		build._binaries = binaries
		build._source = source

	def updateAllBuildDependencies(self, client, builds):
		res = client.getBuildDepInfo(self.name, self.buildRepository, self.buildArch)
		if res is None:
			raise Exception(f"{self}: unable to update build dependencies")

		if not res:
			warnmsg(f"{self} project does not provide _builddepinfo")
			return

		sourceMap = {}
		binaryMap = {}
		for obsBuild in builds:
			src = obsBuild.sourceRpm
			if src is not None:
				sourceMap[src.name] = src
			elif all(rpm.isImported for rpm in obsBuild.binaries):
				infomsg(f"{self}/{obsBuild}: no src (imported from different build arch)")
			else:
				warnmsg(f"{self}/{obsBuild}: no src")

			for rpm in obsBuild.binaries:
				if not rpm.isSourcePackage:
					binaryMap[rpm.name] = rpm

		numUpdated = 0

		# res is a list of OBSBuildInfo objects
		for buildInfo in res:
			# infomsg(f"Trying to update build depds for {buildInfo.name}")
			obsBuild = self.getBuild(buildInfo.name)
			if obsBuild is None:
				errormsg(f"{self}: _builddepinfo references {obsBuild} but I don't know about it")
				continue

			if not obsBuild.binaries:
				# Not an RPM build, don't warn
				continue

			sourceRpm = obsBuild.sourceRpm
			if sourceRpm is None:
				if buildInfo.source is not None:
					warnmsg(f"{self}/{obsBuild}: build has no source rpm, but builddepinfo says source is {buildInfo.source}")
				errormsg(f"{self}/{obsBuild}: cannot handle build dependencies; no source rpm")
				continue

			requires = []
			for rpmName in obsBuild.buildRequires:
				rpm = binaryMap.get(rpmName)
				if rpm is None:
					errormsg(f"{self}: _builddepinfo says {obsBuild} requires {rpmName}, but I don't know about it")
					continue

				req = OBSDependency(expression = f'obsbuild({rpmName})')
				req.packages.add(rpm)
				requires.append(req)

			# FIXME: this does not work any longer and needs updating
			fixme()
			# sourceRpm.resolvedRequires = requires

			numUpdated += 1

		infomsg(f"{self}: updated build dependencies for {numUpdated}/{len(builds)} builds")

	def updateOneBuildDependency(self, client, obsBuild):
		sourceRpm = obsBuild.sourceRpm
		if sourceRpm is None:
			return

		buildInfo = self.queryPackageBuildInfo(client, obsBuild)
		if buildInfo is None:
			errormsg(f"No build info for {obsBuild} - dependency information will be incomplete")
			return

		requires = []
		for dep in buildInfo.builddeps:
			if dep.preinstall == "1" or dep.vminstall == "1" or dep.notmeta == "1":
				# FIXME: record these somewhere
				# infomsg(f"{rpm}: ignoring build dependency {dep.name} preinstall={dep.preinstall} vminstall={dep.vminstall} notmeta={dep.notmeta}")
				pass

			requiredRpm = self.product.findPackage(name = dep.name, version = dep.version, release = dep.release, arch = dep.arch)
			if requiredRpm is None:
				previousBuild = self.product.findPackage(name = dep.name, version = dep.version, arch = dep.arch)
				if previousBuild is not None:
					# Inbetween the first query and here, the dependent package has been rebuilt.
					warnmsg(f"{self}: weird package {sourceRpm.fullname} requires unknown {dep.name}-{dep.version}-{dep.release}.{dep.arch}.rpm, looks like recent rebuild")
					requiredRpm = self.product.createPackage(dep.name, dep.version, dep.release, dep.arch,
									buildArch = previousBuild.buildArch,
									epoch = previousBuild.epoch)
				else:
					warnmsg(f"{self}: weird package {sourceRpm.fullname} requires unknown {dep.name}-{dep.version}-{dep.release}.{dep.arch}.rpm")
					requiredRpm = self.product.createPackage(dep.name, dep.version, dep.release, dep.arch)

			req = OBSDependency(expression = f'obsbuild({requiredRpm.name})')
			req.packages.add(requiredRpm)

			requires.append(req)

		# FIXME: this does not work any longer and needs updating
		fixme()

		infomsg(f"{sourceRpm}: extracting {len(requires)} dependencies from {obsBuild}/_buildinfo")
		# sourceRpm.updateResolvedRequires(requires, overwrite = True)

	def tryRetrieveBuildExtendedInfo(self, client, obsBuild):
		result = []

		self.updateOneBuildDependency(client, obsBuild)

		for rpm in obsBuild.binaries:
			if rpm.isSourcePackage:
				continue

			info = client.getFileInfoExt(self.name, self.buildRepository, obsBuild.name, rpm.buildArch, rpm.fullname, infoFactory = self.infoFactory)
			if info is None:
				raise Exception(f"Unable to obtain fileinfo for {rpm.fullname}")

			result.append((rpm, info))

		return result

	# refresh the binarylist for this build
	def refreshBuildResults(self, client, obsBuild):
		cacheEntry = self.getCacheEntry(".listing", project = self.name,
				repository = self.buildRepository,
				package = obsBuild.name,
				arch = self.buildArch)

		updatedListing = client.queryPackageBuild(self.name, self.buildRepository, obsBuild.name, self.buildArch, cacheEntry = cacheEntry)
		self.updateBuildFromBinaryList(obsBuild, updatedListing)

	def getBuild(self, name):
		return self._packages.get(name)

	def addBuild(self, name, buildArch = None):
		pkg = self._packages.get(name)
		if pkg is None:
			if buildArch is None:
				buildArch = self.buildArch
			pkg = OBSBuild(name, buildArch = buildArch)
			pkg.projectHandle = self.handle
			self._packages[pkg.name] = pkg
		return pkg

class OBSProjectCollection(object):
	def __init__(self, cachePath):
		self.cachePath = cachePath
		# use a list rather than a dict to retain order
		self._projects = []

	def addRelease(self, release):
		product = release.createEmptyProduct()
		for projectName in release.buildProjects:
			self.add(projectName, product)

	def add(self, projectName, product):
		for project in self._projects:
			if project.name == projectName and \
			   project.buildArch == product.arch:
				return project

		infomsg(f"  {projectName}/{product.arch}")
		project = OBSProject(projectName, product)
		project.setCachePath(f"{self.cachePath}/{product.arch}")
		self._projects.append(project)
		return project

	def __iter__(self):
		return iter(self._projects)

	def __len__(self):
		return len(self._projects)
