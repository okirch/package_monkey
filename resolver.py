##################################################################
#
# Classes and functions related to dependency resolution
#
##################################################################
import fnmatch
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

resolvLogger = loggingFacade.getLogger('resolver')

def debugResolver(msg, *args, prefix = None, **kwargs):
        if prefix:
                msg = f"[{prefix}] {msg}"
        resolvLogger.debug(msg, *args, **kwargs)

class Disambiguation(object):
	class Requires:
		def __init__(self, name, packages):
			self._name = name
			self._packages = packages
			self.names = set(p.name for p in packages)

		@property
		def name(self):
			return self._name or "<unspec>"

		@property
		def resolved(self):
			return list(filter(lambda p: p.name in self.names, self._packages))

		def asOBSDependency(self):
			from obsclnt import OBSDependency

			result = OBSDependency(self._name)
			result.packages = self.resolved
			return result

	class RpmContext(object):
		def __init__(self, name, requires_ext):
			self.name = name

			self.ambiguous = []
			self.unambiguous = []

			for dep in requires_ext:
				if len(dep.packages) == 0:
					continue

				if len(dep.packages) == 1:
					self.unambiguous.append(Disambiguation.Requires(dep.expression, dep.packages))
				else:
					self.ambiguous.append(Disambiguation.Requires(dep.expression, dep.packages))

		@property
		def resolved(self):
			result = set()
			for dep in self.unambiguous:
				result.update(dep.resolved)
			return result

		def createUpdatedDependencies(self):
			if self.ambiguous:
				return None

			return [dep.asOBSDependency() for dep in self.unambiguous]

	class BuildContext:
		def __init__(self, ruleSet, obsBuild):
			self.ruleSet = ruleSet

			self.preferred = set()
			for rpm in obsBuild.binaries:
				if not rpm.isSourcePackage:
					self.preferred.add(rpm.name)

			self.nameToPkg = {}

		def uniqueDependencies(self, requires_ext):
			result = []

			for dep in requires_ext:
				if len(dep.packages) == 0:
					continue

				packages = set()
				for pinfo in dep.packages:
					uniq = self.nameToPkg.get(pinfo.name)
					if uniq:
						assert(uniq.fullname() == pinfo.fullname())
					else:
						self.nameToPkg[pinfo.name] = pinfo
						packages.append(pinfo)

		def inspect(self, rpm, requires_ext):
			if rpm.isSourcePackage:
				return None

			result = Disambiguation.RpmContext(rpm.shortname, requires_ext)
			if result.ambiguous:
				self.ruleSet.disambiguate(result, self.preferred)

			if result.ambiguous:
				self.ruleSet.verifyAcceptable(result)

			return result

	class CollapseRule:
		def __init__(self, target, collapsible):
			self.target = target
			self.collapsible = collapsible

	class AcceptRule:
		def __init__(self, acceptable):
			self.acceptable = acceptable


	def __init__(self):
		self.accept = []
		self.collapse = []

	def addAcceptableRule(self, *names):
		self.accept.append(self.AcceptRule(set(names)))

	def addCollapsingRule(self, target, aliases):
		collapsible = set(aliases)
		collapsible.add(target)

		self.collapse.append(self.CollapseRule(target, collapsible))

	def begin(self, obsPackage):
		return Disambiguation.BuildContext(self, obsPackage)

	def disambiguate(self, rpmContext, siblingNames):
		result = []

		for req in rpmContext.ambiguous:
			modified = False

			# This is "lex libomp16-devel"
			# libomp16-devel has some weird requirements that expand to libomp{15,16,17,...}-devel
			# Of course it makes no sense for libomp16-devel to pull in libomp17-devel, so pretend
			# they didn't say that.
			if rpmContext.name in req.names:
				req.names = set(rpmContext.name)
				rpmContext.unambiguous.append(req)
				continue

			# Another hack to deal with LLVM. requiring libclang13.so will be resolved by OBS as
			# llvm{13,14,15,16}-clang and libclang13. If this occurs while building eg llvm14, we
			# want to pick llvm14-clang. IOW, when we have an ambiguous requires, by default
			# pick the rpms that are produced by the same build
			common = req.names.intersection(siblingNames)
			if common:
				debugResolver(f"{rpmContext.name}: {req.name} can be resolved by sibling(s)")
				modified = True
				req.names = common

			for rule in self.collapse:
				if rule.collapsible.issubset(req.names):
					req.names.difference_update(rule.collapsible)
					req.names.add(rule.target)
					modified = True

			if modified:
				if len(req.names) <= 1:
					debugResolver(f"{rpmContext.name}: {req.name} is now unambiguous")
					rpmContext.unambiguous.append(req)
					continue

			result.append(req)

		rpmContext.ambiguous = result

	def verifyAcceptable(self, rpmContext):
		stillAmbiguous = []

		for req in rpmContext.ambiguous:
			acceptable = False

			for rule in self.accept:
				if req.names.issubset(rule.acceptable):
					debugResolver(f"{rpmContext.name}: ambiguous requirement {req.name} is acceptable")
					acceptable = True
					break

			if acceptable:
				rpmContext.unambiguous.append(req)
				continue

			stillAmbiguous.append(req)

		rpmContext.ambiguous = stillAmbiguous


class ResolverHints:
	class ExactMatch:
		def __init__(self, name, *args):
			self.name = name

		def match(self, name):
			return self.name == name

	class NameMatch:
		def __init__(self, pattern, *args):
			self.pattern = pattern

		def match(self, name):
			return fnmatch.fnmatch(name, self.pattern)

	class OrderRule(object):
		def greaterThan(self, nameA, nameB):
			indexA = self.getIndex(nameA)
			indexB = self.getIndex(nameB)

			if indexA is None or indexB is None:
				return False

			return indexA < indexB

		def replaceEmptyString(self, words):
			def replaceEmpty(s):
				if s == "''" or s == ".":
					s = ""
				return s

			return list(map(replaceEmpty, words))

	class NameOrderRule(OrderRule):
		def __init__(self, names):
			self.matches = []
			for name in names:
				self.matches.append(self.intern(name))

		def intern(self, name):
			if '*' in name or '?' in name or '[' in name:
				match = ResolverHints.NameMatch(name)
			else:
				match = ResolverHints.ExactMatch(name)
			return match

		def getIndex(self, name):
			for i in range(len(self.matches)):
				if self.matches[i].match(name):
					return i
			return None

	class PrefixOrderRule(OrderRule):
		def __init__(self, words):
			self.prefixes = self.replaceEmptyString(words)

		def greaterThan(self, nameA, nameB):
			maxIndex = len(self.prefixes)
			for indexA in range(maxIndex):
				prefixA = self.prefixes[indexA]

				stemA = self.stripPrefix(nameA, prefixA)
				if not stemA:
					continue

				for prefixB in self.prefixes[indexA + 1:]:
					stemB = self.stripPrefix(nameB, prefixB)
					if stemA == stemB:
						return True

			return False

		def stripPrefix(self, word, prefix):
			if prefix:
				if not word.startswith(prefix):
					return None
				word = word[len(prefix):]
			return word

	class SuffixOrderRule(OrderRule):
		def __init__(self, words):
			self.suffixes = self.replaceEmptyString(words)

		def greaterThan(self, nameA, nameB):
			maxIndex = len(self.suffixes)
			for indexA in range(maxIndex):
				suffixA = self.suffixes[indexA]

				stemA = self.stripSuffix(nameA, suffixA)
				if not stemA:
					continue

				for suffixB in self.suffixes[indexA + 1:]:
					stemB = self.stripSuffix(nameB, suffixB)
					if stemA == stemB:
						return True

			return False

		def stripSuffix(self, word, suffix):
			if suffix:
				if not word.endswith(suffix):
					return None
				word = word[:-len(suffix)]
			return word

	def __init__(self):
		self._rules = []
		self._cache = {}

		# These are for dependency transformation
		self._warnings = {}
		self._ignoredDependencies = {}
		self._ignoredTargets = None
		self._rewriteDependencies = {}

		self.fakeDependencies = set()

		self.disambiguation = Disambiguation()

	def addNameOrder(self, words):
		self._rules.append(self.NameOrderRule(words))

	def addSuffixOrder(self, words):
		self._rules.append(self.SuffixOrderRule(words))

	def addPrefixOrder(self, words):
		self._rules.append(self.PrefixOrderRule(words))

	def finalize(self):
		pass

	def getPreferred(self, setOfNames):
		key = '|'.join(sorted(setOfNames))

		# SLE has just a very small number of unique ambiguous dependencies
		result = self._cache.get(key)
		if result is None:
			isBelow = set()
			for a in setOfNames:
				for b in setOfNames:
					if a is b:
						continue
					for edge in self._rules:
						if edge.greaterThan(a, b):
							isBelow.add(b)
							break

			result = setOfNames.difference(isBelow)
			self._cache[key] = result

		return result

	##########################################################
	# disambiguation of requirements
	##########################################################
	def addAcceptableRule(self, nameList):
		self.disambiguation.addAcceptableRule(*nameList)

	def addCollapsingRule(self, target, aliases):
		self.disambiguation.addCollapsingRule(target, aliases)

	def createDisambiguationContext(self, obsPackage):
		return self.disambiguation.begin(obsPackage)

	##########################################################
	# inspect dependency
	##########################################################
	class DependencyTransform(object):
		COPY = 0
		REWRITE = 1
		IGNORE = 2

		def __init__(self, action, sourceName = None, targetName = None, warning = None, rewriteTo = None):
			self.action = action
			self.sourceName = sourceName
			self.targetName = targetName
			self.rewriteTo = rewriteTo
			self.warning = warning

		@classmethod
		def createCopyTransform(klass, sourceName, targetName):
			return klass(klass.COPY, sourceName = sourceName, targetName = targetName)

		@classmethod
		def createIgnoreTransform(klass, sourceName, targetName):
			return klass(klass.IGNORE, sourceName = sourceName, targetName = targetName)

		@classmethod
		def createRewriteTransform(klass, sourceName, targetName, rewriteTo):
			return klass(klass.REWRITE, sourceName = sourceName, targetName = targetName, rewriteTo = rewriteTo)

		@property
		def key(self):
			return self.makekey(self.sourceName, self.targetName)

		@staticmethod
		def makekey(sourceName, targetName):
			if sourceName is None:
				sourceName = '*'

			return f"{sourceName}:{targetName}"

	def transformDependency(self, sourceName, targetName):
		result = None

		if self.isIgnoredDependency(sourceName, targetName):
			result = self.DependencyTransform.createIgnoreTransform(sourceName, targetName)

			key = f"{sourceName or '*'}:{targetName}"
			result.warning = self._warnings.get(key)
		else:
			rewriteTo = self.rewriteDependency(targetName)
			if rewriteTo:
				result = self.DependencyTransform.createRewriteTransform(sourceName, targetName, rewriteTo = rewriteTo)

		return result

	##########################################################
	# Handling of ignored dependencies
	##########################################################
	def addIgnoredDependency(self, packageName, targetName, warning = None):
		if packageName == '*':
			if self._ignoredTargets is None:
				self._ignoredTargets = set()
			self._ignoredTargets.add(targetName)
		else:
			if targetName not in self._ignoredDependencies:
				self._ignoredDependencies[targetName] = set()
			self._ignoredDependencies[targetName].add(packageName)

		if warning is not None:
			key = f"{packageName}:{targetName}"
			self._warnings[key] = warning

	def isIgnoredDependency(self, packageName, targetName):
		if self._ignoredTargets is not None and targetName in self._ignoredTargets:
			return True

		ignoredNames = self._ignoredDependencies.get(targetName)
		return ignoredNames is not None and packageName in ignoredNames

	##########################################################
	# define fake depdendency targets
	##########################################################
	def addFakeDependency(self, name):
		self.fakeDependencies.add(name)

	##########################################################
	# Handling of dependency rewrites
	##########################################################
	def addDependencyRewrite(self, fromName, toName):
		exist = self._rewriteDependencies.get(fromName)
		if exist is not None:
			raise Exception(f"Duplicate dependency rewrite rule for {fromName}")

		self._rewriteDependencies[fromName] = toName

	def rewriteDependency(self, name):
		return self._rewriteDependencies.get(name)

	# not really a self test yet
	def selfTest(self):
		for input in (('gettext-its-gtk4', 'gettext-its-gtk3'),
				('libQt5Core-devel', 'libQt5Core-devel-32bit'),
				('systemd', 'systemd-mini'),
				('systemd-devel', 'systemd-mini-devel'),
				('foo', 'bar-mini'),
				('libfoo-32bit', 'libfoo'),
				('java-1_8_0-openjdk-headless', 'java-1_8_0-openjdk-devel'),
				('openmpi2-config', 'openmpi4-config', 'openmpi3-config'),
				('llvm15-libclang16', 'libclang16'),
				('glib2-devel', 'glib2-devel-32bit'),
				('libopenssl-1_1-devel', 'libopenssl-1_1-devel-32bit', 'libopenssl-3-devel', 'libopenssl-3-devel-32bit', 'libopenssl-devel'),
				):
			input = set(input)
			solved = self.getPreferred(input)
			print(input, " -> ", solved)

		fail

