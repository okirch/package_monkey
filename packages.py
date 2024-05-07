#
# package and product handling classes
#

import gzip
import xml.etree.ElementTree as ET
import urllib.parse
import os.path
import os
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

from filter import Classification

dependencyLogger = loggingFacade.getLogger('dependency')
debugDependency = dependencyLogger.debug

# for now, not feeding into the logger
def debugDependency2(*args, **kwargs):
	# print(*args, **kwargs)
	pass

def isSourceArchitecture(arch):
	return arch in ('src', 'nosrc')

class Versiontools:
	tested = False

	class ParsedVersion:
		def __init__(self, version, release, epoch = None):
			self.version = version
			self.release = release
			self.epoch = epoch or "0"
			self.tokens = []

			self.tokens += Versiontools.splitLabel(self.epoch)
			self.tokens.append(None)
			self.tokens += Versiontools.splitLabel(self.version)
			self.tokens.append(None)
			self.tokens += Versiontools.splitLabel(self.release)
			while self.tokens[-1] is None:
				self.tokens.pop()

		def __str__(self):
			result = self.version
			if self.release:
				result += "-" + self.release
			if self.epoch != "0":
				result = self.epoch + ":" + result
			return result

		def cmp(self, other):
			assert(isinstance(other, Versiontools.ParsedVersion))
			return Versiontools.compareParsedVersions(self, other)

		def __eq__(self, other):
			return self.cmp(other) == 0

		def __ne__(self, other):
			return self.cmp(other) != 0

		def __lt__(self, other):
			return self.cmp(other) < 0

		def __gt__(self, other):
			return self.cmp(other) > 0

		def __le__(self, other):
			return self.cmp(other) <= 0

		def __ge__(self, other):
			return self.cmp(other) >= 0

	TESTVECTORS = (
		("python", "EQ", ("2.7", None),		("2.7.1", "6"),		True),
		("python", "EQ", ("2.7", "12"),		("2.7.1", "6"),		False),
		("python", "GT", ("2.7.1", None),	("2.7.2", None),	True),
		("python", "LE", ("2.7", None),		("2.7.1", "6"),		False),
		("python", "LE", ("2.7", None),		("2.7", "6"),		True),
		("python", "LE", ("2.7.0", None),	("2.7.1", "6"),		False),
	)

	class FakeDep:
		def __init__(self, name, flags, pv):
			self.name = name
			self.flags = flags
			self.parsedVersion = pv
			self.op = Package.VersionedPackageDependency.compare[flags]

		def __str__(self):
			return f"{self.name} {self.flags} {self.parsedVersion}"

	@staticmethod
	def test():
		if Versiontools.tested:
			return True

		# assert(Versiontools.splitLabel("1.6pre1") == (1, 6, 'pre', 1))
		assert(Versiontools.compareLabels("1.4", "1.3") == 1)
		assert(Versiontools.compareLabels("1.6", "1.6pre1") == -1)

		success = True
		for name, flags, arg1, arg2, expected in Versiontools.TESTVECTORS:
			pv1 = Versiontools.ParsedVersion(*arg1)
			pv2 = Versiontools.ParsedVersion(*arg2)
			dep = Versiontools.FakeDep(name, flags, pv1)

			result = Versiontools.dependencySatisfiedByVersion(dep, pv2)
			if result != expected:
				errormsg(f"FAIL: {dep}: {pv2}: expected {expected} but got {result}")
				success = False
			else:
				infomsg(f"OK: {dep}: {pv2}: -> {expected}")

		Versiontools.tested = True
		return success

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
			#infomsg("%s == %s" % (a, b))
			return 0
		if a < b:
			#infomsg("%s < %s" % (a, b))
			return -1
		#infomsg("%s > %s" % (a, b))
		return 1

	@staticmethod
	def compareToken(t1, t2):
		if t1 == t2:
			# infomsg(f"{t1}: same")
			return 0
		if type(t1) == type(t2):
			return Versiontools.cmp(t1, t2)
		if t1 is None:
			# infomsg(f"sepa \"-\" < {t2}")
			return -1
		if t2 is None:
			# infomsg(f"{t1} < sepa \"-\"")
			return 1
		if type(t1) == int:
			# infomsg(f"int {t1} < other {t2}")
			return -1
		# infomsg(f"other {t1} > int {t2}")
		assert(type(t2) == int)
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
		#infomsg("length diff=%d" % d)
		return Versiontools.cmp(d, 0)

	@staticmethod
	def compareLabelsShort(l1, l2):
		l1 = Versiontools.splitLabel(l1)
		l2 = Versiontools.splitLabel(l2)
		return Versiontools.compareTokensShort(l1, l2)

	@staticmethod
	def compareTokensShort(l1, l2):
		for (c1, c2) in zip(l1, l2):
			r = Versiontools.compareToken(c1, c2)
			if r != 0:
				return r

		# If l1 is less specific than l2, consider them equal
		# IOW, "Requires: python-abi(2.7)" is expected to match "2.7.14-4.19"
		d = len(l1) - len(l2)
		if d == 0:
			return 0

		# Same prefix, but one of them is longer
		if d < 0:
			return -0.5
		return 0.5

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

		r = Versiontools.compareTokensShort(req.parsedVersion.tokens, pkgVersion.tokens)
		r = req.op(-r, 0)
		debugDependency2(f"  = {req.flags} -> {r}")
		return r

class ResolverCache(dict):
	# needs to be different from None
	NEGATIVE_ENTRY = 42

	class Stats:
		def __init__(self):
			self.numHits = 0
			self.numLookups = 0

		def record(self, result):
			if result is not None:
				self.numHits += 1
			self.numLookups += 1

		def format(self):
			if self.numLookups != 0:
				ratio = int(100 * self.numHits / self.numLookups)
			else:
				ratio = 0
			return f"{self.numLookups} lookups ({ratio}% hit rate)";

	# all instances share a common stats object
	stats = Stats()

	def __init__(self):
		super().__init__()

	def put(self, dep, pkg):
		key = str(dep)

		if pkg is None:
			pkg = self.NEGATIVE_ENTRY
		self[key] = pkg

	def get(self, dep):
		key = str(dep)
		result = super().get(key)
		self.stats.record(result)
		return result

	def formatStats(self):
		return self.stats.format()

class ResolverChoice:
	def __init__(self):
		self.bestPackage = None
		self.bestProductRating = -1
		self.secondBest = None

	@property
	def result(self):
		return self.bestPackage or self.secondBest

	def update(self, preferences, cand):
		if cand is Package.ExpandedToNothing:
			self.secondBest = cand
			return False

		productRating = preferences.rateProduct(cand.product)
		if self.preferCandidate(preferences, cand, productRating):
			self.bestPackage = cand
			self.bestProductRating = productRating
			return True

		return False

	def preferCandidate(self, preferences, cand, productRating):
		best = self.bestPackage
		if best is None:
			return True

		if cand.name == best.name:
			return Versiontools.comparePackages(best, cand) < 0

		# FIXME: the preferences stuff is all gone, need to use ResolverHints instead
		r = preferences.comparePackages(cand.name, best.name)
		# infomsg(f"Prefer {cand.name} over {best.name}? result={r}")
		if r == 0:
			r = preferences.preferProductRating(productRating, self.bestProductRating)

		if r > 0:
			return True
		if r < 0:
			return False

		# We haven't recorded a preference one way or the other
		infomsg(f"Ambiguous resolution: {cand.name} and {best.name}")
		return False

class ResolverContext:
	def __init__(self, worker, arch):
		self.worker = worker
		self._resolver = worker._resolver
		# FIXME: obsolete
		self._preferences = None
		self._problems = worker._problems

		if arch in ('src', 'nosrc'):
			altArch = 'nosrc'
		else:
			altArch = 'noarch'
		self.arch = [arch, altArch]

		self._cache = ResolverCache()

		self.suppressedDependencies = []
		self.debugMsg = debugDependency

	def acceptable(self, pkg):
		if pkg.arch not in self.arch:
			return False

		return True

	def selectPreferredCandidate(self, preferences, candidates):
		choice = ResolverChoice()
		for cand in candidates:
			#if cand in self._packages:
			#	return cand

			if cand is Package.ExpandedToNothing:
				choice.update(preferences, cand)
				continue

			if self.acceptable(cand):
				# infomsg(f"  {cand.fullname()}")
				choice.update(preferences, cand)
			else:
				# infomsg(f"  {cand.fullname()} not acceptable")
				pass

		# Expand PackageInfo to full-blown Package object
		found = self._resolver.expand(choice.result)
		return found

	def resolveRequires(self, req):
		try:
			candidates = req.enumerateCandidateSolutions(self._resolver)
			found = self.selectPreferredCandidate(self._preferences, candidates)
		except Exception as e:
			infomsg(f"Caught exception while resolving {req}: {e}")
			found = None
		return found

	def resolveDownward(self, pkg):
		result = []

		if pkg.isSourcePackage and not pkg.resolvedRequires:
			warnmsg(f"{pkg.fullname()} has no dependencies")

		if pkg.resolvedRequires is not None:
			self.debugMsg(f"{pkg.fullname()} - {len(pkg.resolvedRequires)} dependencies already resolved")
			return pkg.resolvedRequires

		if not pkg.requires:
			self.debugMsg(f"{pkg.fullname()} has no dependencies")
			pkg.resolvedRequires = result
			return result

		self.debugMsg(f"{pkg.fullname()} - resolving requirements")
		for dep in pkg.requires:
			self.debugMsg(f"  inspecting {pkg.fullname()} req {dep}")
			found = self._cache.get(dep)

			if found is None:
				# No cache entry found, take the slow path and resolve it
				found = self.resolveRequires(dep)
				# ... and stick the result into the cache
				self._cache.put(dep, found)
			elif found is ResolverCache.NEGATIVE_ENTRY:
				# it's a negative entry, translate to None
				found = None

			# If it's a valid entry, add it to the result
			if found is None:
				self._problems.addUnableToResolve(pkg, dep)
			elif found is not Package.ExpandedToNothing:
				result.append((dep, found))

		pkg.resolvedRequires = result
		return result

	def transformDependencies(self, pkg):
		if self._resolver.hints is None:
			return

		if pkg.isSourcePackage:
			return

		hints = self._resolver.hints
		filtered = []

		for dep, target in self.resolveDownward(pkg):
			xfrm = hints.transformDependency(pkg.name, target.name)

			if xfrm is None:
				pass
			elif xfrm.action == xfrm.IGNORE:
				self.suppressedDependencies.append((pkg, target))
				if xfrm.warning:
					warnmsg(f"Ignoring dependency {pkg} -> {target}: {xfrm.warning}")
				else:
					infomsg(f"Ignoring dependency {pkg} -> {target}")
				continue
			elif xfrm.action == xfrm.REWRITE:
				assert(xfrm.rewriteTo)
				newTarget = self.worker.getKnownPackage(xfrm.rewriteTo)
				if newTarget is None:
					raise Exception(f"cannot translate dependency {target} to {xfrm.rewriteTo}: no such package")

				debugmsg(f"{pkg}: rewrite dependency {target} -> {newTarget}")
				target = newTarget
			elif xfrm.action == xfrm.COPY:
				pass

			filtered.append((dep, target))

		pkg.resolvedRequires = filtered

class ResolverWorker:
	class Problem(object):
		def __init__(self):
			pass

		def showProof(self, reason, writer):
			for why in reason.chain():
				writer = writer.indentingWriter()
				writer.infomsg(why)

	class UnexpectedDependency(Problem):
		def __init__(self, fromLabel, toLabel):
			super().__init__()
			self.fromLabel = fromLabel
			self.toLabel = toLabel

			self.conflicts = []

		@property
		def desc(self):
			return f"{self.fromLabel} -> {self.toLabel}"

		def add(self, conflict):
			self.conflicts.append(conflict)

	class UnexpectedRuntimeDependency(UnexpectedDependency):
		def show(self, report):
			report.addUnexpectedRuntimeDependency(self)

	class UnexpectedBuildDependency(UnexpectedDependency):
		def show(self, report):
			report.addUnexpectedBuildDependency(self)

	class UnlabelledBuildDependency(Problem):
		# we record only one reason why a package build is part of a component
		class Build:
			def __init__(self, name, reason):
				self.name = name
				self.reason = reason
				self.packages = []

		def __init__(self, fromLabel):
			super().__init__()
			self.fromLabel = fromLabel
			self.builds = {}

		@property
		def desc(self):
			return str(self.fromLabel)

		def add(self, wantedReason, buildName, pkg):
			assert(type(buildName) is str)
			try:
				build = self.builds[buildName]
			except:
				build = self.Build(buildName, wantedReason)
				self.builds[buildName] = build

			build.packages.append(pkg)

		def show(self, report):
			report.addUnlabelledBuildDependency(self)

	class UnresolvedDependency(Problem):
		def __init__(self, dependency):
			super().__init__()

			self.dependency = dependency
			self.requiredby = set()

		@property
		def desc(self):
			return str(self.dependency)

		def add(self, pkg):
			self.requiredby.add(pkg)

		def show(report):
			report.addUnresolvedDependency(self)

	class MissingSource(Problem):
		def __init__(self, package):
			super().__init__()
			self.package = package
			self.requiredby = set()

		@property
		def desc(self):
			return str(self.package)

		def add(self, pkg, reason):
			self.requiredby.add((pkg, reason))

		def show(self, report):
			report.addMissingSource(self)

	class SourceProjectConflict(Problem):
		def __init__(self, build, *args):
			super().__init__()
			self.build = build

		@property
		def desc(self):
			return self.build.name

		def show(self, report):
			report.addSourceProjectConflict(self)

	class Problems:
		def __init__(self):
			self._unexpectedRuntime = {}
			self._unexpectedBuild = {}
			self._unlabelledBuild = {}
			self._unresolved = {}
			self._nosource = {}
			self._projectconf = {}
			self._ignoreLabels = set()

		@property
		def categories(self):
			return (self._unexpectedRuntime, self._unlabelledBuild, self._unexpectedBuild, self._unresolved, self._nosource, self._projectconf)

		def ignore(self, label):
			self._ignoreLabels.add(label)

		def isIgnored(self, fromLabel, toLabel):
			if fromLabel in self._ignoreLabels:
				return True
			if fromLabel.parent and fromLabel.parent in self._ignoreLabels:
				return True
			return None

		def addUnexpectedDependency(self, fromLabel, fromReason, toPackage):
			toLabel = toPackage.label.name
			if self.isIgnored(fromLabel, toLabel):
				return

			key = f"{fromLabel}/{toLabel}"
			ud = self._unexpectedRuntime.get(key)
			if ud is None:
				ud = ResolverWorker.UnexpectedRuntimeDependency(fromLabel, toLabel)
				self._unexpectedRuntime[key] = ud
			ud.add((fromReason, toPackage))

		def addUnexpectedBuildDependency(self, fromPackage, buildName, toPackage):
			fromLabel = fromPackage.label
			toLabel = toPackage.label

			if self.isIgnored(fromLabel, toLabel):
				return

			key = f"{fromLabel}/{toLabel}"
			ud = self._unexpectedBuild.get(key)
			if ud is None:
				ud = ResolverWorker.UnexpectedBuildDependency(fromLabel, toLabel)
				self._unexpectedBuild[key] = ud
			ud.add((buildName, fromPackage.labelReason, toPackage))

		def addUnlabelledBuildDependency(self, fromPackage, buildName, toPackage):
			if self.isIgnored(fromPackage.label, None):
				return

			key = str(fromPackage.label)
			ud = self._unlabelledBuild.get(key)
			if ud is None:
				ud = ResolverWorker.UnlabelledBuildDependency(fromPackage.label)
				self._unlabelledBuild[key] = ud
			ud.add(fromPackage.labelReason, buildName, toPackage)

		def addUnableToResolve(self, pkg, dep):
			if self.isIgnored(pkg.label, None):
				return

			key = str(dep)
			ur = self._unresolved.get(key)
			if ur is None:
				ur = ResolverWorker.UnresolvedDependency(dep)
				self._unresolved[key] = ur
			ur.add(pkg)

		def addMissingSource(self, pkg, reason):
			if self.isIgnored(pkg.label, None):
				return

			key = f"{pkg.fullname()}"
			problem = self._nosource.get(key)
			if problem is None:
				problem = ResolverWorker.MissingSource(pkg)
				self._nosource[key] = problem
			problem.add(pkg, reason)

		def addSourceProjectConflict(self, build):
			key = build.name
			problem = self._projectconf.get(key)
			if problem is None:
				problem = ResolverWorker.SourceProjectConflict(build)
				self._projectconf[key] = problem

		def __bool__(self):
			return any(self.categories)

		def show(self, writer):
			for category in self.categories:
				categoryReport = writer.addProblemCategory(None)
				for key, problem in sorted(category.items()):
					problem.show(categoryReport)

	def __init__(self, resolver, packageCollection = None):
		self._resolver = resolver
		self._queue = []
		self._packages = set()
		self._problems = self.Problems()
		self._contexts = {}
		self._packageCollection = packageCollection

		self.debugMsg = debugDependency

	def contextForArch(self, arch):
		if isSourceArchitecture(arch):
			raise Exception(f"Invalid architecture {arch} in ResolverWorker.contextForArch()")

		ctx = self._contexts.get(arch)
		if ctx is None:
			ctx = ResolverContext(self, arch)
			self._contexts[arch] = ctx
		return ctx

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

	def formatCacheStats(self):
		return ResolverCache.stats.format()

	def getKnownPackage(self, name):
		if self._packageCollection is None:
			return None
		return self._packageCollection.get(name)

class Resolver:
	class NameBucket(object):
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

	def __init__(self, backingStore = None, hints = None):
		self._buckets = {}
		self.backingStore = backingStore
		self.hints = hints
		self._nullBucket = self.NameBucket(None)
		self._conditionals = {}

	def addPackage(self, pkg):
		# infomsg(f"Adding {pkg.fullname()} to resolver (provides {len(pkg.provides)})")

		b = self._createBucket(pkg.name)
		provides = Package.VersionedPackageDependency(pkg.name, flags = 'EQ', ver = pkg.version, rel = pkg.release)
		b.add(provides, pkg)

		for dep in pkg.provides:
			b = self._createBucket(dep.name)

			b.add(dep, pkg)

		for path in pkg.files:
			self.addFileProvides(path, pkg)

	def addFileProvides(self, path, pkg):
		b = self._createBucket(path)
		provides = Package.FileDependency(path)
		b.add(provides, pkg)

	def _createBucket(self, name):
		b = self._buckets.get(name)
		if b is None:
			b = Resolver.NameBucket(name)
			self._buckets[b.name] = b
		return b

	def fetchBucket(self, name):
		bucket = self._buckets.get(name)
		if bucket is not None:
			return bucket

		if self.backingStore is None:
			infomsg(f"Nothing provides {name}")
			return self._nullBucket

		b = self._createBucket(name)
		for dep, pinfo in self.backingStore.enumerateProvidersOfName(name):
			b.add(dep, pinfo)

		return b

	def declareConditional(self, name, onoff):
		self._conditionals[name] = onoff

	def checkConditional(self, name):
		return bool(self._conditionals.get(name))

	def enumerateCandidatesFileDependency(self, name):
		bucket = self.fetchBucket(name)
		return bucket.candidatePackages

	def enumerateCandidatesUnversionedPackageDependency(self, name):
		bucket = self.fetchBucket(name)
		return bucket.candidatePackages

	def enumerateCandidatesVersionedPackageDependency(self, req):
		bucket = self.fetchBucket(req.name)

		candidates = []
		rejected = []
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
					rejected.append(cand)
			else:
				infomsg(f"Don't know how to handle Provides: {provides}")

		if rejected and not candidates:
			warnmsg(f"It looks like we're not able to satisfy dependency {req}")
			infomsg(" - Rejected versions:")
			for bad in rejected:
				infomsg(f"    {bad.provides} provided by package {bad.pkg.fullname()}")

		return candidates

	def expand(self, candidate):
		if isinstance(candidate, PackageInfo):
			if not self.backingStore:
				raise Exception("Cannot expand PackageInfo object - no database attached")
			return self.backingStore.retrievePackage(candidate)

		return candidate

	def selectMostRecent(self, candidates):
		if not candidates:
			return None

		best = None
		for cand in candidates:
			if best is None or Versiontools.comparePackages(best, cand) < 0:
				best = cand

		if best is not None:
			best = self.expand(best)

		return best

class DependencyParser:
	class Lexer:
		EOL = 0
		LEFTB = 1
		RIGHTB = 2
		OPERATOR = 3
		IDENTIFIER = 4

		CHARCLASS_OPERATOR = ('<', '>', '=', '!')
		CHARCLASS_WORDBREAK = CHARCLASS_OPERATOR

		OPERATOR_IDENTIFIERS = ('EQ', 'NE', 'LT', 'GT', 'LE', 'GE')
		OPERATOR_TABLE = {
			'=':  'EQ',
			'==': 'EQ',
			'<=': 'LE',
			'>=': 'GE',
			'<':  'GT',
			'>':  'LT',
			'!=': 'NE',
		}


		def __init__(self, string):
			self.value = list(string)
			self.pos = 0

		@property
		def stringValue(self):
			return "".join(self.value)

		def getc(self):
			try:
				cc = self.value[self.pos]
			except:
				return None

			self.pos += 1
			return cc

		def ungetc(self, cc):
			assert(self.pos > 0)
			assert(self.value[self.pos - 1] == cc)
			self.pos -= 1

		def next(self):
			result = ""
			while True:
				cc = self.getc()
				if cc is None:
					break

				while cc and cc.isspace():
					cc = self.getc()

				if cc in self.CHARCLASS_OPERATOR:
					while cc in self.CHARCLASS_OPERATOR:
						result += cc
						cc = self.getc()
					# translate operator "<=" to "LE" and so on
					result = self.OPERATOR_TABLE[result]
					return (self.OPERATOR, result)

				if cc == '(':
					return (self.LEFTB, cc)
				if cc == ')':
					return (self.RIGHTB, cc)

				processingBracketedArgument = False
				while cc and not cc.isspace() and not cc in self.CHARCLASS_WORDBREAK:
					if cc == '(':
						if processingBracketedArgument:
							raise Exception("Dependency parser: nested brackets not allowed inside Identifier")
						processingBracketedArgument = True
					elif cc == ')':
						if not processingBracketedArgument:
							break
						processingBracketedArgument = False

					result += cc
					cc = self.getc()

				if cc:
					self.ungetc(cc)

				if not result:
					break

				if result in self.OPERATOR_IDENTIFIERS:
					return (self.OPERATOR, result)

				return (self.IDENTIFIER, result)

			return (self.EOL, result)

		def symbolicToStringOperator(self, op):
			return self.OPERATOR_TABLE[op]

	class ProcessedExpression(object):
		pass

	class DependencySingleton(ProcessedExpression):

		# The flags argument is a symbolic operator like EQ, LE etc
		def __init__(self, name, flags = None, version = None):
			self.name = name
			self.flags = flags

			release = None
			if version and '-' in version:
				version, release = version.split('-')

			self.version = version
			self.release = release

		def infomsg(self, ws = ""):
			if self.op:
				infomsg(f"{ws}{self.name} {self.op} {self.version}")
			else:
				infomsg(f"{ws}{self.name}")

		def build(self):
			if self.flags is None:
				return Package.createDependency(self.name)
			else:
				return Package.createDependency(self.name, flags = self.flags, ver = self.version, rel = self.release)

	class BracketedTerm(ProcessedExpression):
		def __init__(self, term):
			self.term = term

		def infomsg(self, ws = ""):
			self.term.infomsg(ws)

		def build(self):
			return self.term.build()

	class ConditionalExpression(ProcessedExpression):
		def __init__(self, inner, conditional = None):
			self.conditional = conditional
			self.inner = inner

		def add(self, child):
			assert(self.conditional is None)
			self.conditional = child

		def infomsg(self, ws = ""):
			infomsg(f"{ws}IF")
			if self.conditional:
				self.conditional.infomsg(ws + "  ")
			else:
				infomsg(f"{ws}ALWAYS FALSE")
			if self.inner:
				self.inner.infomsg(ws + "  ")
			else:
				infomsg(f"{ws}NO INNER TERM")

		def build(self):
			conditionalTerm = self.conditional.build()
			innerTerm = self.inner.build()
			return Package.createConditionalDependency(conditionalTerm, innerTerm)

	class AssociativeExpression(ProcessedExpression):
		def __init__(self, child):
			self.children = [child]

		def add(self, child):
			self.children.append(child)

		def buildTerms(self):
			result = []
			for child in self.children:
				result.append(child.build())
			return result

	class OrExpression(AssociativeExpression):
		def infomsg(self, ws = ""):
			infomsg(f"{ws}OR")
			for child in self.children:
				child.infomsg(ws + "  ")

		def build(self):
			return Package.createOrDependency(self.buildTerms())

	class AndExpression(AssociativeExpression):
		def infomsg(self, ws = ""):
			infomsg(f"{ws}AND")
			for child in self.children:
				child.infomsg(ws + "  ")

		def build(self):
			return Package.createAndDependency(self.buildTerms())

	def __init__(self, string):
		# infomsg(f"## Parsing \"{string}\"")
		self.lex = self.Lexer(string)

		self.lookahead = None

	def __str__(self):
		return self.lex.stringValue

	def nextToken(self):
		lookahead = self.lookahead
		if lookahead is not None:
			self.lookahead = None
			return lookahead

		type, value = self.lex.next()
		# infomsg(f"## -> type={type} value=\"{value}\"")
		return type, value

	def pushBackToken(self, *args):
		assert(self.lookahead is None)
		self.lookahead = args

	class BadExpressionException(Exception):
		def __init__(self, lexer):
			value = "".join(lexer.value)
			ws = " " * lexer.pos
			msg = f"Bad expression:\n{value}\n{ws}^--- HERE"
			super().__init__(msg)

	def BadExpression(self):
		return self.BadExpressionException(self.lex)

	def process(self, endToken = None):
		if endToken is None:
			endToken = self.Lexer.EOL

		leftTerm = None
		while True:
			type, value = self.nextToken()
			if type == endToken:
				break

			# infomsg("# About to process next term")
			if type == self.Lexer.RIGHTB or type == self.Lexer.EOL:
				infomsg(f"endToken={endToken}")
				raise self.BadExpression()

			groupClass = None

			if type == self.Lexer.IDENTIFIER:
				if value == "or":
					groupClass = self.OrExpression
				elif value == "and" or value == "with":
					groupClass = self.AndExpression
				elif value == "if":
					groupClass = self.ConditionalExpression

			if groupClass:
				if leftTerm is None:
					raise self.BadExpression()

				if not isinstance(leftTerm, self.AssociativeExpression):
					leftTerm = groupClass(leftTerm)
				elif leftTerm.__class__ != groupClass:
					infomsg("Cannot mix terms with different precendence")
					raise self.BadExpression()

				type, value = self.nextToken()

			if type == self.Lexer.LEFTB:
				term = self.process(endToken = self.Lexer.RIGHTB)
				term = self.BracketedTerm(term)
			else:
				if type != self.Lexer.IDENTIFIER:
					raise self.BadExpression()

				args = [value]

				type, value = self.nextToken()
				if type == self.Lexer.OPERATOR:
					args.append(value)

					type, value = self.nextToken()
					if type != self.Lexer.IDENTIFIER:
						raise self.BadExpression()

					args.append(value)
				else:
					self.pushBackToken(type, value)

				term = self.DependencySingleton(*args)

			if leftTerm:
				leftTerm.add(term)
			else:
				leftTerm = term

		return leftTerm

	@staticmethod
	def test():
		p = DependencyParser("(foobar == 1.0 if kernel)")
		while True:
			type, value = p.nextToken()
			if type is DependencyParser.Lexer.EOL:
				break

		inputs = (
			"(foobar == 1.0 if kernel)",
			"(foo or alternative(foo))",
			"((foo or bar))",
			"salt-transactional-update = 3006.0-150500.4.12.2 if read-only-root-fs",
			"(systemd GE 238 if systemd)",
		)

		resolver = Resolver()

		for s in inputs:
			infomsg(f"Processing {s}")
			p = DependencyParser(s)
			tree = p.process()
			tree.infomsg("   ")

			dep = tree.build()
			infomsg(f" => {dep}")
			infomsg("")

class Package:
	# This special value is returned when we encounter a conditional
	# dependency, and its condition evaluated to False.
	class EmptyExpansion:
		pass

	ExpandedToNothing = EmptyExpansion()

	class BaseDependency(object):
		def __init__(self):
			self.backingStoreId = None

	class SingleStringDependency(BaseDependency):
		def __init__(self, name):
			super().__init__()
			self.name = name

		def __str__(self):
			return self.name

	class FileDependency(SingleStringDependency):
		def enumerateCandidateSolutions(self, resolver):
			return resolver.enumerateCandidatesFileDependency(self.name)

	class UnversionedPackageDependency(SingleStringDependency):
		def enumerateCandidateSolutions(self, resolver):
			return resolver.enumerateCandidatesUnversionedPackageDependency(self.name)

	class FailingDependency(SingleStringDependency):
		def enumerateCandidateSolutions(self, resolver):
			return []

	class VersionedPackageDependency(BaseDependency):
		compare = {
			"EQ" : lambda a, b: (int(a) == int(b)),
			"NE" : lambda a, b: (int(a) != int(b)),
			"LE" : lambda a, b: (a <= b),
			"GE" : lambda a, b: (a >= b),
			"LT" : lambda a, b: (a < b),
			"GT" : lambda a, b: (a > b),
		}

		def __init__(self, name,  flags = None, epoch = None, ver = None, rel = None, pre = None):
			super().__init__()

			self.name = name
			self.flags = flags
			self.parsedVersion = Versiontools.ParsedVersion(ver, rel, epoch)
			self.pre = pre

			self.op = self.compare[flags]

		def __str__(self):
			return f"{self.name} {self.flags} {self.parsedVersion}"

		def enumerateCandidateSolutions(self, resolver):
			return resolver.enumerateCandidatesVersionedPackageDependency(self)

	class ConditionalDependency:
		def __init__(self, condition, inner):
			self.condition = condition
			self.inner = inner

		def __str__(self):
			return f"({self.inner} if {self.condition})";

		@property
		def name(self):
			return str(self)

		def enumerateCandidateSolutions(self, resolver):
			if isinstance(self.condition, Package.UnversionedPackageDependency) and \
			   resolver.checkConditional(self.condition.name):
				# A conditional we defined earlier is True
				pass
			elif not self.condition.enumerateCandidateSolutions(resolver):
				return [Package.ExpandedToNothing]

			return self.inner.enumerateCandidateSolutions(resolver)

	class OrDependency:
		def __init__(self, children):
			self.children = children

		def __str__(self):
			return "(" + " or ".join(str(_) for _ in self.children) + ")"

		@property
		def name(self):
			return str(self)

		def enumerateCandidateSolutions(self, resolver):
			result = []

			# Not quite right... but we'd have to change the algorithm to accomodate this
			for child in self.children:
				result += list(child.enumerateCandidateSolutions(resolver))
			return result

	class AndDependency:
		def __init__(self, children):
			self.children = children

		def __str__(self):
			return "(" + " with ".join(str(_) for _ in self.children) + ")"

		@property
		def name(self):
			return str(self)

		def enumerateCandidateSolutions(self, resolver):
			result = []

			# Not quite right... but we'd have to change the algorithm to accomodate this
			for child in self.children:
				term = list(child.enumerateCandidateSolutions(resolver))
				if not term:
					return []
				result += term
			return result

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
			infomsg("  %s: %s" % (self.date, self.author))

		def show(self, indent = ""):
			infomsg("%s%s %s" % (indent, self.date, self.author))
			for line in self.text.split("\n"):
				infomsg("%s %s" % (indent, line))

		@staticmethod
		def showChangeList(changes, indent = "", msg = None):
			if msg:
				infomsg("%s %s" % (indent, msg))

			for c in changes:
				c.show(indent + " ")

	def __init__(self, name, version, release, arch, epoch = None):
		self.name = name
		self.epoch = epoch
		self.version = version
		self.release = release
		self.arch = arch
		self._changes = []
		self.sourceName = None
		self.sourcePackage = None
		self.sourcePackageHash = None
		self._sourceBackingStoreId = None
		self.group = ''
		self.buildTime = None
		self.status = None
		self.pkgid = None
		self.productId = None
		self.product = None
		self.backingStoreId = None
		self.obsBuildId = None

		self.isSourcePackage = isSourceArchitecture(arch)

		self.requires = []
		self.provides = []
		self.recommends = []
		self.suggests = []
		self.conflicts = []
		self.resolvedRequires = None
		self.resolvedProvides = None

		self.files = []

		self._parsedVersion = None

		self._label = None
		self.labelReason = None

		self.trace = False

	@staticmethod
	def parseName(s):
		if s.endswith('.rpm'):
			s = s[:-4]
		(s, arch) = s.rsplit('.', 1)
		(s, release) = s.rsplit('-', 1)
		(name, version) = s.rsplit('-', 1)
		return name, version, release, arch

	@staticmethod
	def fromPackageInfo(pinfo):
		pkg = Package(pinfo.name, pinfo.version, pinfo.release, pinfo.arch, pinfo.epoch)
		pkg.backingStoreId = pinfo.backingStoreId
		pkg._parsedVersion = pinfo.parsedVersion
		pkg.product = pinfo.product
		pkg.productId = pinfo.productId
		pkg.buildTime = pinfo.buildTime
		return pkg

	@staticmethod
	def createDependency(name, backingStoreId = None, **kwd):
		assert(name is not None)

		if name.startswith('(') and name.endswith(')'):
			dep = Package.processComplexDependency(name, **kwd)
		elif name.startswith("/"):
			dep = Package.FileDependency(name)
		elif 'flags' not in kwd:
			dep = Package.UnversionedPackageDependency(name)
		else:
			dep = Package.VersionedPackageDependency(name, **kwd)

		dep.backingStoreId = backingStoreId
		return dep

	@staticmethod
	def createConditionalDependency(condition, inner):
		return Package.ConditionalDependency(condition, inner)

	@staticmethod
	def createOrDependency(children):
		return Package.OrDependency(children)

	@staticmethod
	def createAndDependency(children):
		return Package.AndDependency(children)

	@staticmethod
	def processComplexDependency(name, **kwd):
		if 'pre' in kwd:
			del kwd['pre']

		if kwd:
			infomsg(f"Cannot handle dependency {name} with {kwd}")
			return Package.FailingDependency(name)

		parser = DependencyParser(name)

		try:
			tree = parser.process()
		except Exception as e:
			infomsg(f"Failed to parse dependency expression: {name}")
			infomsg(f"  -> {e}")
			return Package.FailingDependency(name)

		return tree.build()

	def sourceID(self):
		if isSourceArchitecture(self.arch):
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

	def __str__(self):
		# make the default name reporting format configurable
		return self.shortname

	@property
	def shortname(self):
		return f"{self.name}.{self.arch}"

	@property
	def versionString(self):
		if not self.epoch:
			return f"{self.version}-{self.release}"
		return f"{self.epoch}:{self.version}-{self.release}"

	# FIXME: turn this into a property, too
	def fullname(self):
		return("%s-%s-%s.%s.rpm" % (self.name, self.version, self.release, self.arch))

	@property
	def isSynthetic(self):
		return self.obsBuildId == 'synthetic'

	def markSynthetic(self):
		self.obsBuildId = 'synthetic'

	def setSourcePackage(self, pkg):
		self.sourcePackage = pkg
		if pkg:
			self.sourcePackageHash = pkg.pkgid

	@property
	def sourceBackingStoreId(self):
		if self.sourcePackage is None:
			return self._sourceBackingStoreId
		return self.sourcePackage.backingStoreId

	@sourceBackingStoreId.setter
	def sourceBackingStoreId(self, value):
		if self.sourcePackage is not None:
			assert(self.sourcePackage.backingStoreId == value)
		self._sourceBackingStoreId = value

	def updateResolvedRequires(self, toAdd):
		if self.resolvedRequires is None:
			self.resolvedRequires = set()
		self.resolvedRequires.update(toAdd)

	def enumerateRequiredRpms(self):
		if self.resolvedRequires is not None:
			for dummy, target in self.resolvedRequires:
				yield target

	def updateResolvedProvides(self, toAdd):
		if self.resolvedProvides is None:
			self.resolvedProvides = set()
		self.resolvedProvides.update(toAdd)

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
			warnmsg("%s from %s-%s has no build time" % (self.fullname(), self.repo.name, self.repo.version))
			self.buildTime = 0
		return self.buildTime <= refTime

	def builtAfter(self, refTime):
		if self.buildTime is None:
			warnmsg(f"%s from %s-%s has no build time" % (self.fullname(), self.repo.name, self.repo.version))
			self.buildTime = 0
		return self.buildTime > refTime

	@property
	def parsedVersion(self):
		if self._parsedVersion is None:
			self._parsedVersion = Versiontools.ParsedVersion(self.version, self.release, self.epoch)
		return self._parsedVersion

	def show(self):
		infomsg(self.fullname())
		for c in self._changes:
			c.show()

	def showChanges(self):
		if self.sourcePackage:
			self.sourcePackage.showChanges()
		else:
			self.status.show(self.fullname())

	@property
	def label(self):
		return self._label

	def setLabel(self, label, reason):
		if self._label is None or self._label.type in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
			self._label = label
			self.labelReason = reason
			assert(reason)
		elif self._label is not label:
			raise Exception(f"Refusing to change {self.fullname()} label from {self.label} to {label}")

class Product:
	def __init__(self, resolverHints = None):
		self.resolverHints = resolverHints

		self.packages = {}
		self.sources = {}
		self._byID = {}

		# Versiontools.test()

		self.name = None
		self.version = None
		self.arch = None
		self.productId = None

		self._resolver = None

	@property
	def fullname(self):
		return f"{self.name}-{self.version}-{self.arch}"

	def setNameAndVersion(self, name, version, arch):
		self.name = name
		self.version = version
		self.arch = arch

	def __repoArg(self, repoArg):
		if type(repoArg) == str:
			return Repo(repoArg)
		return repoArg

	def loadProductRepo(self, repoArg):
		try:
			repo = self.__repoArg(repoArg)
		except:
			infomsg("%s does not seem to exist" % repoArg)
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

	def packageTableForArch(self, arch):
		if isSourceArchitecture(arch):
			return self.sources
		return self.packages

	def addPackage(self, pkg):
		packageTable = self.packageTableForArch(pkg.arch)
		packageTable[pkg.fullname()] = pkg

		pkgid = pkg.pkgid
		if pkgid is not None:
			self._byID[pkgid] = pkg

		pkg.productId = self.productId

	def updateBackingStore(self, backingStore):
		backingStore.addPackageObjectList(self.sources.values())
		backingStore.addPackageObjectList(self.packages.values())

	def updatePackageFilesList(self, pkg, files):
		pkg.files += files
		if self._resolver:
			for path in files:
				self._resolver.addFileProvides(path, pkg)

	def findPackage(self, name, version = None, release = None, arch = None):
		packageTable = self.packageTableForArch(arch)

		if name and version and release and arch:
			fullname = "%s-%s-%s.%s.rpm" % (name, version, release, arch)
			return packageTable.get(fullname)

		# slow path
		for p in packageTable.values():
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

	def findPackageByInfo(self, pinfo, create = False):
		rpm = self.findPackage(pinfo.name, pinfo.version, pinfo.release, pinfo.arch)
		if rpm is None and create:
			rpm = Package.fromPackageInfo(pinfo)
			self.addPackage(rpm)
		return rpm

	def findPackagesBefore(self, refPackage):
		if not refPackage.buildTime:
			infomsg("ERROR %s has no build time" % refPackage.fullname())
			die

		packageTable = self.packageTableForArch(refPackage.arch)

		result = []
		for p in packageTable.values():
			if p.name == refPackage.name and p.arch == refPackage.arch and p.builtNoLaterThan(refPackage.buildTime):
				result.append(p)

		return sorted(result, key = lambda p: p.buildTime)

	def findMostRecentBefore(self, before):
		def debug(msg):
			if False:
				infomsg(msg)

		packageTable = self.packageTableForArch(before.arch)

		debug("Looking for most recent update before %s" % (before.fullname()))
		best = None
		earliest = None
		for p in packageTable.values():
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
			assert(isSourceArchitecture(src.arch))
			self.addPackage(src)
		return src

	def show(self):
		for pkg in self.packages.values():
			pkg.show();

	@property
	def resolver(self):
		if self._resolver is None:
			infomsg("Creating resolver")
			self._resolver = Resolver()

			infomsg(self.packages)
			for pkg in self.packages.values():
				self._resolver.addPackage(pkg)

		return self._resolver

	@resolver.setter
	def resolver(self, value):
		self._resolver = value
		if self._resolver and self.packages:
			for pkg in self.packages.values():
				self._resolver.addPackage(pkg)

class PackageInfo:
	def __init__(self, name, epoch, version, release, arch, backingStoreId, productId = None, parsedVersion = None):
		if parsedVersion is None:
			parsedVersion = Versiontools.ParsedVersion(version, release, epoch)

		self.name = name
		self.epoch = epoch
		self.version = version
		self.release = release
		self.arch = arch
		self.buildTime = None
		self.backingStoreId = backingStoreId
		self.product = None
		self.productId = productId
		self.productName = None
		self.parsedVersion = parsedVersion

		self.isSourcePackage = isSourceArchitecture(arch)

		self._label = None
		self.labelReason = None

	@staticmethod
	def fromNameAndParsedVersion(name, arch, parsedVersion, **kwd):
		return PackageInfo(name, parsedVersion.epoch, parsedVersion.version, parsedVersion.release, arch, None, **kwd)

	@staticmethod
	def parsePackageName(pkgName):
		assert(pkgName.endswith('.rpm'))

		try:
			(n, arch, suffix) = pkgName.rsplit(".", maxsplit = 2)
			(name, version, release) = n.rsplit("-", maxsplit = 2)
		except:
			raise ValueError(f"Unable to parse RPM package name {pkgName}")

		return PackageInfo(name, None, version, release, arch, None)

	@property
	def shortname(self):
		return f"{self.name}.{self.arch}"

	@property
	def key(self):
		return f"{self.name}/{self.arch}"

	def __str__(self):
		return self.shortname

	def fullname(self):
		return f"{self.name}-{self.version}-{self.release}.{self.arch}.rpm"

	@property
	def label(self):
		return self._label

	def setLabel(self, label, reason):
		if self._label is None or self._label.type in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
			self._label = label
			self.labelReason = reason
			assert(reason)
		elif self._label is not label:
			raise Exception(f"Refusing to change {self.fullname()} label from {self.label} to {label}")


class PackageInfoFactory(object):
	def __call__(self, name, version, release, arch):
		return PackageInfo(name = name, version = version, release = release, arch = arch, epoch = None, backingStoreId = None)

class UniquePackageInfoFactory(PackageInfoFactory):
	def __init__(self):
		self._map = dict()

	def __call__(self, name, version, release, arch):
		key = f"{name}-{version}-{release}.{arch}"
		pinfo = self._map.get(key)
		if pinfo is None:
			pinfo = PackageInfo(name = name, version = version, release = release, arch = arch, epoch = None, backingStoreId = None)
			self._map[key] = pinfo
		return pinfo

class PackageCollection:
	def __init__(self):
		self._packages = []
		self._sources = set()
		self._arches = set()
		self._packageDict = {}

	def get(self, name):
		return self._packageDict.get(name)

	def add(self, pkg):
		self._packages.append(pkg)
		self._packageDict[pkg.name] = pkg
		if pkg.arch not in ('src', 'nosrc', 'noarch'):
			self._arches.add(pkg.arch)

			src = pkg.sourcePackage
			if src:
				self._sources.add(src)

	def addSynthetic(self, name, version = "0.0", release = "0", arch = "noarch"):
		pkg = Package(name, version, release, arch)
		pkg.markSynthetic()
		pkg.resolvedRequires = []
		pkg.resolvedProvides = []
		self.add(pkg)
		return pkg

	def __iter__(self):
		return iter(self._packages + list(self._sources))

	def __len__(self):
		return len(self._packages) + len(self._sources)

	@property
	def uniqueArch(self):
		if not self._arches:
			return None
		if len(self._arches) > 1:
			raise Exception(f"Unable to determine architecture - found {self._arches}")
		return next(iter(self._arches))

if __name__ == '__main__':
	if Versiontools.test():
		infomsg("Version comparison seems to work as expected")
	else:
		infomsg("Version comparison is not working as expected")

	DependencyParser.test()
