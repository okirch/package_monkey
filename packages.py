#
# package and product handling classes
#

import gzip
import xml.etree.ElementTree as ET
import urllib.parse
import os.path
import os

from filter import Classification


optDebugDependency = 0

def debugDependency(*args, **kwargs):
	if optDebugDependency >= 1:
		print(*args, **kwargs)

def debugDependency2(*args, **kwargs):
	if optDebugDependency >= 2:
		print(*args, **kwargs)

class Versiontools:
	tested = False

	class ParsedVersion:
		def __init__(self, version, release, epoch = None):
			self.version = version
			self.release = release
			self.epoch = epoch or "0"

		def __str__(self):
			result = self.version
			if self.release:
				result += "-" + self.release
			if self.epoch != "0":
				result = self.epoch + ":" + result
			return result

	@staticmethod
	def test():
		if Versiontools.tested:
			return

		Versiontools.tested = True
		# assert(Versiontools.splitLabel("1.6pre1") == (1, 6, 'pre', 1))
		assert(Versiontools.compareLabels("1.4", "1.3") == 1)
		assert(Versiontools.compareLabels("1.6", "1.6pre1") == -1)

	# This is not pretty but hopefully fast
	@staticmethod
	def splitLabel(lbl):
		if lbl is None:
			return []

		convertString = lambda s : s
		convertInt = lambda s : int(s)
		result = []
		convert = convertString
		fn = None
		component = ""
		for c in lbl:
			if fn:
				if fn(c):
					component += c
					continue
				result.append(convert(component))
				component = ""
				convert = None
				fn = None
			if c.isdigit():
				fn = str.isdigit
				convert = convertInt
				component += c
			elif c.isalpha():
				fn = str.isalpha
				convert = convertString
				component += c
			elif component:
				result.append(convert(component))
				component = ""
		if component:
			result.append(convert(component))
		return result

	@staticmethod
	def cmp(a, b):
		if a == b:
			#print("%s == %s" % (a, b))
			return 0
		if a < b:
			#print("%s < %s" % (a, b))
			return -1
		#print("%s > %s" % (a, b))
		return 1

	@staticmethod
	def compareToken(t1, t2):
		if t1 == t2:
			#print("%s: same" % t1)
			return 0
		if type(t1) == type(t2):
			return Versiontools.cmp(t1, t2)
		if type(t1) == int:
			#print("int %d < str %s" % (t1, t2))
			return -1
		#print("str %s > int %d" % (t1, t2))
		return 1

	@staticmethod
	def compareLabels(l1, l2):
		l1 = Versiontools.splitLabel(l1)
		l2 = Versiontools.splitLabel(l2)
		for (c1, c2) in zip(l1, l2):
			r = Versiontools.compareToken(c1, c2)
			if r != 0:
				return r

		d = len(l1) - len(l2)
		#print("length diff=%d" % d)
		return Versiontools.cmp(d, 0)

	@staticmethod
	def compareLabelsShort(l1, l2):
		l1 = Versiontools.splitLabel(l1)
		l2 = Versiontools.splitLabel(l2)
		for (c1, c2) in zip(l1, l2):
			r = Versiontools.compareToken(c1, c2)
			if r != 0:
				return r

		# If l1 is less specific than l2, consider them equal
		# IOW, "Requires: python-abi(2.7)" is expected to match "2.7.14-4.19"
		d = len(l1) - len(l2)
		if d <= 0:
			return 0

		return 1

	@staticmethod
	def comparePackages(p1, p2):
		return Versiontools.compareParsedVersions(p1.parsedVersion, p2.parsedVersion)

	@staticmethod
	def compareParsedVersions(pv1, pv2):
		r = Versiontools.compareLabels(pv1.epoch, pv2.epoch)
		if r == 0:
			r = Versiontools.compareLabels(pv1.version, pv2.version)
		if r == 0:
			r = Versiontools.compareLabels(pv1.release, pv2.release)
		return r

	@staticmethod
	def compareParsedVersionsShort(pv1, pv2):
		r = Versiontools.compareLabelsShort(pv1.epoch, pv2.epoch)
		debugDependency2(f"  ? E {pv1.epoch} <> {pv2.epoch} -> {r}")
		if r == 0:
			r = Versiontools.compareLabelsShort(pv1.version, pv2.version)
			debugDependency2(f"  ? V {pv1.version} <> {pv2.version} -> {r}")
		if r == 0:
			r = Versiontools.compareLabelsShort(pv1.release, pv2.release)
			debugDependency2(f"  ? R {pv1.release} <> {pv2.release} -> {r}")
		return r

	@staticmethod
	def dependencySatisfiedByVersion(req, pkgVersion):
		# "Requires: foobar OP 1.2.3" never matches "Provides: foobar"
		if not pkgVersion:
			return False

		r = Versiontools.compareParsedVersionsShort(req.parsedVersion, pkgVersion)
		r = req.op(-r, 0)
		debugDependency2(f"  = {req.op} -> {r}")
		return r

class ResolverWorker:
	class UnexpectedDependency:
		def __init__(self, desc):
			self.desc = desc
			self.reasons = []

		def add(self, reason):
			self.reasons.append(reason)

		def show(self):
			print(f"Unexpected dependency {self.desc}:")
			for reason in self.reasons:
				indent = "   "
				for why in reason.chain():
					print(f"{indent}{why}")
					indent += "  "

	class Problems:
		def __init__(self):
			self._dependencies = {}

		def addUnexpectedDependency(self, fm, to, reason):
			key = f"{fm}/{to}"
			ud = self._dependencies.get(key)
			if ud is None:
				ud = ResolverWorker.UnexpectedDependency(f"{fm} -> {to}")
				self._dependencies[key] = ud
			ud.add(reason)

		def addUnableToResolve(self, pkg, dep):
			print(f"{pkg.fullname()}: cannot resolve dependency {dep}")
			pass

		def __bool__(self):
			return bool(self._dependencies)

		def show(self):
			for key, ud in sorted(self._dependencies.items()):
				ud.show()

	def __init__(self, resolver, processfn = None):
		self._resolver = resolver
		self._queue = []
		self._packages = set()
		self._process = processfn
		self._problems = self.Problems()

		self.debugMsg = debugDependency

	def add(self, pkg):
		if not pkg in self._packages:
			self._packages.add(pkg)
			self._queue.append(pkg)

	def next(self):
		try:
			return self._queue.pop(0)
		except: pass
		return None

	def update(self, packages):
		self._packages.update(packages)
		self._queue += list(packages)

	@property
	def problems(self):
		return self._problems

	def selectPreferredCandidate(self, candidates):
		best = None
		for cand in candidates:
			if cand in self._packages:
				return cand

			if best is None or Versiontools.comparePackages(best, cand) < 0:
				best = cand

		return best

	def resolveRequires(self, req):
		candidates = req.enumerateCandidateSolutions(self._resolver)
		return self.selectPreferredCandidate(candidates)

class Resolver:
	class NameBucket:
		class Candidate:
			def __init__(self, prov, pkg):
				self.provides = prov
				self.pkg = pkg

		def __init__(self, name):
			self.name = name
			self._candidates = []
		
		def add(self, provides, pkg):
			self._candidates.append(self.Candidate(provides, pkg))

		@property
		def candidatePackages(self):
			return set(_.pkg for _ in self._candidates)

	def __init__(self):
		self._buckets = {}

	def addPackage(self, pkg):
		# print(f"Adding {pkg.name} to resolver")

		b = self._createBucket(pkg.name)
		provides = Package.VersionedPackageDependency(pkg.name, flags = 'EQ', ver = pkg.version, rel = pkg.release)
		b.add(provides, pkg)

		for dep in pkg.provides:
			b = self._createBucket(dep.name)

			b.add(dep, pkg)

		for path in pkg.files:
			self.addFileProvides(path, pkg)

	def addFileProvides(self, path, pkg):
		if path in ("/sbin/ldconfig", "/bin/sh"):
			print(f"DEP {pkg.fullname()} provides {path}")

		b = self._createBucket(path)
		provides = Package.FileDependency(path)
		b.add(provides, pkg)

	def _createBucket(self, name):
		b = self._buckets.get(name)
		if b is None:
			b = Resolver.NameBucket(name)
			self._buckets[b.name] = b
		return b

	def enumerateCandidatesFileDependency(self, name):
		bucket = self._buckets.get(name)
		if bucket is None:
			return []
		return bucket.candidatePackages

	def enumerateCandidatesUnversionedPackageDependency(self, name):
		bucket = self._buckets.get(name)
		if bucket is None:
			print(f"Nothing provides {name}")
			return []
		return bucket.candidatePackages

	def enumerateCandidatesVersionedPackageDependency(self, req):
		bucket = self._buckets.get(req.name)
		if bucket is None:
			return []

		candidates = []
		for cand in bucket._candidates:
			provides = cand.provides
			if isinstance(provides, Package.UnversionedPackageDependency):
				providedVersion = cand.pkg.parsedVersion
			elif isinstance(provides, Package.VersionedPackageDependency):
				providedVersion = provides.parsedVersion
			else:
				debugDependency(f" {provides} not a versioned dependency")
				continue

			pkg = cand.pkg
			debugDependency(f"Checking {req} vs {provides}")

			if provides.flags == 'EQ':
				if Versiontools.dependencySatisfiedByVersion(req, providedVersion):
					debugDependency(f"    {pkg.fullname()} is a match")
					candidates.append(pkg)
				else:
					debugDependency(f"    {pkg.parsedVersion} does not match")
			else:
				print(f"Don't know how to handle Provides: {provides}")

		return candidates

	def selectMostRecent(self, candidates):
		if not candidates:
			return None

		best = None
		for cand in candidates:
			if best is None or Versiontools.comparePackages(best, cand) < 0:
				best = cand
		return best


class Package:
	class FileDependency:
		def __init__(self, name):
			self.name = name

		def __str__(self):
			return self.name

		def enumerateCandidateSolutions(self, resolver):
			return resolver.enumerateCandidatesFileDependency(self.name)

	class UnversionedPackageDependency:
		def __init__(self, name):
			self.name = name

		def __str__(self):
			return self.name

		def enumerateCandidateSolutions(self, resolver):
			return resolver.enumerateCandidatesUnversionedPackageDependency(self.name)

	class VersionedPackageDependency:
		compare = {
			"EQ" : int.__eq__,
			"NE" : int.__ne__,
			"LT" : int.__lt__,
			"GT" : int.__gt__,
			"LE" : int.__le__,
			"GE" : int.__ge__,
		}

		def __init__(self, name,  flags = None, epoch = None, ver = None, rel = None, pre = None):
			self.name = name
			self.flags = flags
			self.parsedVersion = Versiontools.ParsedVersion(ver, rel, epoch)
			self.pre = pre

			self.op = self.compare[flags]

		def __str__(self):
			return f"{self.name} {self.flags} {self.parsedVersion}"

		def enumerateCandidateSolutions(self, resolver):
			return resolver.enumerateCandidatesVersionedPackageDependency(self)

	class Change:
		def __init__(self, date, author, text):
			self.date = date
			self.author = author
			self.text = text

			# Clean up "Joe Doe <joedoe@some.org>" author
			if '<' in author:
				oa = author
				author = author.split('<', 1)[1]
				author = author.split('>', 1)[0]
			self.authorEmail = author

			if author.endswith("@suse.com") or \
			   author.endswith("@suse.de") or \
			   author.endswith("@suse.cz") or \
			   author.endswith("@novell.com") or \
			   author in ('git@opensuse.org', 'dimstar@opensuse.org'):
				self.isSUSE = True
			else:
				self.isSUSE = False

		def show(self):
			print("  %s: %s" % (self.date, self.author))

		def show(self, indent = ""):
			print("%s%s %s" % (indent, self.date, self.author))
			for line in self.text.split("\n"):
				print("%s %s" % (indent, line))

		@staticmethod
		def showChangeList(changes, indent = "", msg = None):
			if msg:
				print("%s %s" % (indent, msg))

			for c in changes:
				c.show(indent + " ")

	def __init__(self, name, version, release, arch):
		self.name = name
		self.epoch = None
		self.version = version
		self.release = release
		self.arch = arch
		self._changes = []
		self.sourcePackage = None
		self.group = ''
		self.buildTime = None
		self.status = None
		self.pkgid = None

		self.requires = []
		self.provides = []
		self.recommends = []
		self.suggests = []
		self.conflicts = []

		self.files = []

		self._parsedVersion = None

		self.label = None
		self.labelReason = None

	@staticmethod
	def parseName(s):
		if s.endswith('.rpm'):
			s = s[:-4]
		(s, arch) = s.rsplit('.', 1)
		(s, release) = s.rsplit('-', 1)
		(name, version) = s.rsplit('-', 1)
		return name, version, release, arch

	@staticmethod
	def createDependency(name, **kwd):
		assert(name is not None)

		if name.startswith("/"):
			return Package.FileDependency(name)
		if 'flags' not in kwd:
			return Package.UnversionedPackageDependency(name)
		return Package.VersionedPackageDependency(name, **kwd)

	def sourceID(self):
		if self.arch == 'src' or self.arch == 'nosrc':
			return self.fullname()
		if self.sourcePackage:
			return self.sourcePackage.sourceID()

		raise ValueError("%s: cannot determine sourceID" % self.fullname())

	def repoID(self):
		if not self.repo:
			raise ValueError("%s: repo not set" % self.fullname())
		return "%s-%s" % (self.repo.name, self.repo.version)

	def changes(self):
		import copy
		return copy.copy(self._changes)

	def setChanges(self, changes):
		self._changes = sorted(changes, key = lambda c : int(c.date))

	def fullname(self):
		return("%s-%s-%s.%s.rpm" % (self.name, self.version, self.release, self.arch))

	def setSourcePackage(self, name):
		self.sourcePackage = name

	def recordChanges(self, verdict, changes = None):
		global totalChanges, totalSuseChanges

		if self.sourcePackage:
			return self.sourcePackage.recordChanges(verdict, changes);

		self.status = Package.Status(verdict, changes)
		return self.status

	def isOlderThan(self, other):
		return Versiontools.comparePackages(self, other) < 0

	def isMoreRecentThan(self, other):
		return Versiontools.comparePackages(self, other) > 0

	def builtNoLaterThan(self, refTime):
		if self.buildTime is None:
			print("WARNING %s from %s-%s has no build time" % (self.fullname(), self.repo.name, self.repo.version))
			self.buildTime = 0
		return self.buildTime <= refTime

	def builtAfter(self, refTime):
		if self.buildTime is None:
			print("WARNING %s from %s-%s has no build time" % (self.fullname(), self.repo.name, self.repo.version))
			self.buildTime = 0
		return self.buildTime > refTime

	@property
	def parsedVersion(self):
		if self._parsedVersion is None:
			self._parsedVersion = Versiontools.ParsedVersion(self.version, self.release, self.epoch)
		return self._parsedVersion

	def show(self):
		print(self.fullname())
		for c in self._changes:
			c.show()

	def showChanges(self):
		if self.sourcePackage:
			self.sourcePackage.showChanges()
		else:
			self.status.show(self.fullname())

class Product:
	def __init__(self):
		self.packages = {}
		self.sources = {}
		self._byID = {}

		Versiontools.test()

		self.name = None
		self.version = None

		self._resolver = None

	def setNameAndVersion(self, name, version):
		self.name = name
		self.version = version

	def __repoArg(self, repoArg):
		if type(repoArg) == str:
			return Repo(repoArg)
		return repoArg

	def loadProductRepo(self, repoArg):
		try:
			repo = self.__repoArg(repoArg)
		except:
			print("%s does not seem to exist" % repoArg)
			return False

		repo.load(self);
		return True

	def loadUpdateRepo(self, repoArg):
		repo = self.__repoArg(repoArg)
		repo.load(self);

	def createPackage(self, name, version, release, arch):
		pkg = Package(name, version, release, arch)
		self.addPackage(pkg)

		return pkg

	def addPackage(self, pkg):
		if pkg.arch == 'src':
			self.sources[pkg.fullname()] = pkg
		else:
			self.packages[pkg.fullname()] = pkg
			if self._resolver:
				self._resolver.addPackage(pkg)

		pkgid = pkg.pkgid
		if pkgid is not None:
			self._byID[pkgid] = pkg

	def updatePackageFilesList(self, pkg, files):
		pkg.files += files
		if self._resolver:
			for path in files:
				self._resolver.addFileProvides(path, pkg)

	def findPackage(self, name, version = None, release = None, arch = None):
		pkgdict = self.packages
		if arch == 'src':
			pkgdict = self.sources

		if name and version and release and arch:
			fullname = "%s-%s-%s.%s.rpm" % (name, version, release, arch)
			return pkgdict.get(fullname)

		# slow path
		for p in pkgdict.values():
			if p.name != name:
				continue
			if version is not None and p.version != version:
				continue
			if release is not None and p.release != release:
				continue
			if arch is not None and p.arch != arch:
				continue

			return p

		return None

	def findPackageByID(self, pkgid):
		return self._byID.get(pkgid)

	def findPackagesBefore(self, refPackage):
		if not refPackage.buildTime:
			print("ERROR %s has no build time" % refPackage.fullname())
			die

		pkgdict = self.packages
		if refPackage.arch == 'src':
			pkgdict = self.sources

		result = []
		for p in pkgdict.values():
			if p.name == refPackage.name and p.arch == refPackage.arch and p.builtNoLaterThan(refPackage.buildTime):
				result.append(p)

		return sorted(result, key = lambda p: p.buildTime)

	def findMostRecentBefore(self, before):
		def debug(msg):
			if False:
				print(msg)

		pkgdict = self.packages
		if before.arch == 'src':
			pkgdict = self.sources

		debug("Looking for most recent update before %s" % (before.fullname()))
		best = None
		earliest = None
		for p in pkgdict.values():
			if p.name != before.name or p.arch != before.arch:
				continue

			if not earliest:
				earliest = p

			if Versiontools.comparePackages(p, before) > 0:
				debug("  %s is too recent" % (p.fullname()))
				continue

			if best and Versiontools.comparePackages(p, best) < 0:
				debug("  %s is too old" % (p.fullname()))
				continue

			debug("  %s is a candidate" % (p.fullname()))
			best = p

		if best:
			debug("  returning %s" % (best.fullname()))
		elif earliest:
			# This looks like a possible version downgrade from one product release to the next.
			# We leave it to the caller to detect and flag
			best = earliest

		return best

	def findSource(self, name, create = False):
		src = self.sources.get(name)
		if not src and create:
			(name, version, release, arch) = Package.parseName(name)
			src = Package(name, version, release, arch)
			assert(src.arch == 'src' or src.arch == 'nosrc')
			self.addPackage(src)
		return src

	def show(self):
		for pkg in self.packages.values():
			pkg.show();

	@property
	def resolver(self):
		if self._resolver is None:
			print("Creating resolver")
			self._resolver = Resolver()

			print(self.packages)
			for pkg in self.packages.values():
				self._resolver.addPackage(pkg)

		return self._resolver

	@resolver.setter
	def resolver(self, value):
		self._resolver = value
		if self._resolver and self.packages:
			for pkg in self.packages.values():
				self._resolver.addPackage(pkg)
