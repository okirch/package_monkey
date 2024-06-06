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
#			if rpm.isSourcePackage:
#				return None

			result = Disambiguation.RpmContext(rpm.shortname, requires_ext)

			if False:
				infomsg(f"### {rpm}: ambig={result.ambiguous}")
				for dep in requires_ext:
					infomsg(f"  {dep.type} {dep.expression}: {' '.join(map(str, dep.packages))}")

			if result.ambiguous:
				self.ruleSet.disambiguate(result, self.preferred)

			if result.ambiguous:
				self.ruleSet.verifyAcceptable(result)

			return result

	# If a dependency was expanded to <target, alias1, alias2, ..>
	# replace it with <target>.
	# All aliases must be present in the expansion
	class CollapseRule(object):
		def __init__(self, target, aliases):
			self.target = target
			self.aliases = set(aliases)
			self.aliases.add(target)

		def apply(self, names):
			if not self.aliases.issubset(names):
				return False

			names.difference_update(self.aliases)
			names.add(self.target)
			return True

	# If a dependency was expanded to <target, aliasM, aliasN, ..>
	# replace it with <target>.
	# At least one alias must be present in the expansion
	class CollapseRuleAny(object):
		def __init__(self, target, aliases):
			self.target = target
			self.aliases = set(aliases)
			self.aliases.discard(target)

		def apply(self, names):
			if self.target not in names or not names.intersection(self.aliases):
				return False
			names.difference_update(self.aliases)
			return True

	class AcceptRule:
		def __init__(self, acceptable):
			self.acceptable = acceptable

	class HideRule(object):
		def __init__(self, names):
			self.names = names

		def apply(self, names):
			n = len(names)
			names.difference_update(self.names)
			# return True if we did hide one or more names
			return len(names) < n

	def __init__(self):
		self.hide = []
		self.accept = []
		self.collapse = []

	def addAcceptableRule(self, *names):
		self.accept.append(self.AcceptRule(set(names)))

	def addCollapsingRule(self, target, aliases, anyAlias = True):
		if anyAlias:
			newRule = self.CollapseRuleAny(target, aliases)
		else:
			newRule = self.CollapseRule(target, aliases)
		self.collapse.append(newRule)
		return newRule

	def addHideRule(self, names):
		self.hide.append(self.HideRule(names))

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
				if rule.apply(req.names):
					modified = True

			for rule in self.hide:
				if rule.apply(req.names):
					modified = True

			# if we failed, see if we can catch [foo, foo-32bit] ambiguities
			if len(req.names) > 1:
				rules = []

				for name in req.names:
					if name + '-32bit' in req.names:
						debugResolver(f"Auto-added new collapsing rule for {name} vs {name}-32bit")
						rules.append(self.addCollapsingRule(name, [name + '-32bit']))

				if rules:
					for rule in rules:
						if rule.apply(req.names):
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

	def __init__(self):
		self._cache = {}

		# These are for dependency transformation
		self._warnings = {}
		self._ignoredDependencies = {}
		self._ignoredTargets = None
		self._rewriteDependencies = {}

		self.fakeDependencies = set()

		self.disambiguation = Disambiguation()

	def finalize(self):
		pass

	##########################################################
	# disambiguation of requirements
	##########################################################
	def addAcceptableRule(self, nameList):
		self.disambiguation.addAcceptableRule(*nameList)

	def addHideRule(self, nameList):
		self.disambiguation.addHideRule(nameList)

	def addCollapsingRule(self, target, aliases, **kwargs):
		self.disambiguation.addCollapsingRule(target, aliases, **kwargs)

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
