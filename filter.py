import yaml
import fnmatch

from util import CycleDetector, GenerationCounter, Timestamp


class Classification:
	TYPE_BINARY = 'binary'
	TYPE_SOURCE = 'source'
	TYPE_AUTOFLAVOR = 'autoflavor'

	class Label:
		GENERATION = GenerationCounter()
		RUNTIME_CYCLE_GUARD = CycleDetector("runtime dependency")
		BUILD_CYCLE_GUARD = CycleDetector("build dependency")

		def __init__(self, name, type, id):
			self.name = name
			self.type = type
			self.id = id
			# FIXME: rename to runtimeRequires
			self.uses = set()
			self.buildRequires = set()

			self.flavorBase = None
			self._flavors = {}

			self._timestamp = Timestamp()

			# the closure comprises all packages of this label plus the ones
			# referenced by subordinate labels
			self._closure = None

			# the build closure comprises the labels that were listed as
			# build requirements, recursively.
			self._buildClosure = None

			self.sourceProject = None

		@classmethod
		def hierarchyNeedsUpdate(klass):
			klass.GENERATION.tick()

		def addRuntimeDependency(self, other):
			assert(other is not None)
			self.uses.add(other)
			self.hierarchyNeedsUpdate()

		def addBuildDependency(self, other):
			assert(other is not None)
			self.buildRequires.add(other)
			self.hierarchyNeedsUpdate()

		@property
		def flavors(self):
			return map(lambda pair: pair[1], sorted(self._flavors.items()))

		def getBuildFlavor(self, name):
			return self._flavors.get(name)

		def addBuildFlavor(self, flavorName, otherLabel):
			if self.getBuildFlavor(flavorName) is not None:
				raise Exception(f"Duplicate definition of flavor {flavorName} for {self.name}")

			self._flavors[flavorName] = otherLabel

			# flavors inherit the parent's build project by default
			if self.sourceProject and not otherLabel.sourceProject:
				otherLabel.setSourceProject(self.sourceProject)

			# This creates a circular reference that kills garbage collection, but
			# we'll live with this for now
			otherLabel.flavorBase = self

			self.hierarchyNeedsUpdate()

		def setSourceProject(self, sourceLabel):
			if self.sourceProject is sourceLabel:
				return
			if self.sourceProject is not None:
				raise Exception(f"Duplicate source group for {self}: {self.sourceProject} vs {sourceLabel}")
			self.sourceProject = sourceLabel

			self.hierarchyNeedsUpdate()

		@property
		def closure(self):
			self.maybeInvalidateClosures()
			if self._closure is None:
				with self.RUNTIME_CYCLE_GUARD.protect(self.name) as guard:
					self.updateClosure()

			return self._closure

		@property
		def buildClosure(self):
			self.maybeInvalidateClosures()
			if self._buildClosure is None:
				with self.BUILD_CYCLE_GUARD.protect(self.name) as guard:
					self.updateBuildClosure()

			return self._buildClosure

		def maybeInvalidateClosures(self):
			if not self._timestamp.isCurrent(self.GENERATION):
				self._closure = None
				self._buildClosure = None

		def maybeUpdateClosures(self):
			if self._timestamp.isCurrent(self.GENERATION):
				return

			self._closure = None
			self._buildClosure = None

		def updateClosure(self):
			self._closure = None

			result = set()
			if self.flavorBase:
				result.update(self.flavorBase.closure)
			for label in self.uses:
				result.update(label.closure)

			if self.type is Classification.TYPE_BINARY:
				result.add(self)

			self._closure = result

		def updateBuildClosure(self):
			self._buildClosure = None

			result = set()
			result.update(self.closure)
			for label in self.uses:
				result.update(label.buildClosure)
				develFlavor = label.getBuildFlavor('devel')

				# If we have a runtime requirement on @Foobar, 
				# assume that we'll have a build requirement on
				# @Foobar+devel in addition
				if develFlavor is not None:
					result.add(develFlavor)

			for label in self.buildRequires:
				result.update(label.closure)

			if self.flavorBase:
				result.update(self.flavorBase.buildClosure)

			if self.type is Classification.TYPE_BINARY:
				result.add(self)

			self._buildClosure = result

		GUARD = "guard"

		def updateClosureWork(self, chain = []):
			def circularDependencyError(chain):
				cycle = " -> ".join(_.name for _ in chain)
				raise Exception(f"Circular dependency of labels while resolving {cycle}")

			if self._closure is self.GUARD:
				circularDependencyError(chain)

			self._closure = self.GUARD

			chain = chain + [self]
			# print("update", " -> ".join(_.name for _ in chain))

			result = set()
			if self.flavorBase:
				result.update(self.flavorBase.closure)
			for label in self.uses:
				if label._closure is self.GUARD:
					circularDependencyError(chain + [label])
				if label._closure is None:
					label.updateClosureWork(chain)
				result.update(label._closure)

			if self.type is Classification.TYPE_BINARY:
				result.add(self)

			self._closure = result

		def isKnownDependency(self, other):
			if other in self.closure:
				return True
			if self.sourceProject == other.sourceProject:
				return True
			return False

		def __str__(self):
			return self.name

	class Scheme:
		def __init__(self):
			self._labels = {}
			self._nextLabelId = 0

		def createLabel(self, name, type):
			label = self._labels.get(name)
			if label is None:
				label = Classification.Label(name, type, self._nextLabelId)
				self._labels[name] = label
				self._nextLabelId += 1
			elif label.type != type:
				raise Exception(f"Conflicting types for label {name}!")
			return label

		@property
		def allLabels(self):
			return sorted(self._labels.values(), key = lambda _: _.name)

		def finalize(self):
			for label in self._labels.values():
				label.closure
				label.buildClosure

			if False:
				self.showLabel("@CoreLibraries")
				#self.showLabel("@CoreLibraries+odbc")
				self.showLabel("@CryptoLibraries")
				self.showLabel("@ApplicationLibraries")
				#self.showLabel("@Boot")
				#self.showLabel("@Kernel")

		def getExtendedClosure(self, name):
			label = self._labels.get(name)
			if label is None:
				raise Exception(f"Unknown label {name}")

			return label._buildClosure

		def show(self):
			for label in self.allLabels:
				self.showLabel(label)

		def showLabel(self, label):
			if type(label) == str:
				label = self._labels[label]

			print(f"Label {label.name}")
			if label.sourceProject:
				print(f"  source project {label.sourceProject}")
			for name, lset in (("requires", label.uses), ("buildrequires", label.buildRequires), ("closure", label._closure), ("build closure", label._buildClosure)):
				if not lset:
					continue
				print(f"  {name}")

				if lset is not label._closure and label._closure.issubset(lset):
					if lset == label._closure:
						print(f"    (same as closure)")
						continue

					print(f"    closure plus:")
					lset = lset.difference(label._closure)

				for c in lset:
					print(f"    {c.name}")
				print()

	class Reason(object):
		def __init__(self, pkg):
			self.package = pkg

		def reasonChain(self, package):
			if package is None or package.labelReason is None:
				result = ["<divine intervention>"]
			else:
				result = package.labelReason.chain()
			return result + [self]

		@property
		def originPackage(self):
			return self.package

	class ReasonFilter(Reason):
		def __init__(self, pkg, filterDesc):
			super().__init__(pkg)
			self.filterDesc = filterDesc

		@property
		def type(self):
			return 'filter'

		def chain(self):
			return [self]

		def __str__(self):
			return f"{self.package.fullname()} identified by {self.filterDesc}"

	class ReasonRequires(Reason):
		def __init__(self, pkg, dependant, req):
			super().__init__(pkg)
			self.dependant = dependant
			self.req = req

		@property
		def type(self):
			return 'dependency'

		def chain(self):
			return self.reasonChain(self.dependant)

		@property
		def originPackage(self):
			return self.dependant.labelReason.originPackage

		def __str__(self):
			return f"{self.package.fullname()} required by {self.dependant.fullname()} via {self.req}"

	class ReasonSourcePackage(Reason):
		def __init__(self, pkg, binary):
			super().__init__(pkg)
			self.binary = binary

		@property
		def type(self):
			return 'source package'

		def chain(self):
			return self.reasonChain(self.binary)

		def __str__(self):
			return f"{self.package.fullname()} is the source of {self.binary.fullname()}"

	class ReasonSiblingPackage(Reason):
		def __init__(self, pkg, sibling):
			super().__init__(pkg)
			self.sibling = sibling

		@property
		def type(self):
			return 'sibling package'

		def chain(self):
			return self.reasonChain(self.sibling)

		def __str__(self):
			return f"{self.package.fullname()} is a sibling package of {self.sibling.fullname()}"

	class ReasonRelatedPackage(ReasonSiblingPackage):
		def __init__(self, relationName, pkg, sibling):
			super().__init__(pkg, sibling)
			self.relation = relationName

		@property
		def type(self):
			return f"{self.relation} package"

		def chain(self):
			return self.reasonChain(self.sibling)

		def __str__(self):
			return f"{self.package.fullname()} is a {self.relation} package related to {self.sibling.fullname()}"

	class ReasonBuildDependency(Reason):
		def __init__(self, pkg, parentReason):
			super().__init__(pkg)
			self.parentReason = parentReason

		@property
		def type(self):
			return 'build requirement'

		def chain(self):
			return [self.parentReason]

		def __str__(self):
			return f"{self.package.fullname()} builds {self.parentReason.package.fullname()}"

	class ReasonSourceClosure(Reason):
		def __init__(self, pkg, sibling):
			super().__init__(pkg)
			self.sibling = sibling

		@property
		def type(self):
			return 'source'

		def chain(self):
			return self.reasonChain(self.sibling)

		def __str__(self):
			return f"{self.package.fullname()} built from the same source as {self.sibling.fullname()}"

	class Classifier(object):
		def __init__(self, label):
			self.label = label
			self.result = set()

	class BuildPackageClosure(Classifier):
		def __init__(self, problems, label, store, **kwargs):
			super().__init__(label, **kwargs)
			self.problems = problems
			self.store = store

		def handleSourceProjectConflict(self, build):
			self.problems.addSourceProjectConflict(build)

		def handleUnexpectedBuildDependency(self, pkg, build, required):
			self.problems.addUnexpectedBuildDependency(pkg, build.name, required)

		def enumerate(self, packages):
			alreadySeen = set()
			for rpm in packages:
				buildId = rpm.obsBuildId
				if buildId is None:
					print(f"No OBS package for {rpm.shortname}")
					continue

				if buildId in alreadySeen:
					continue
				alreadySeen.add(buildId)

				build = self.store.retrieveOBSPackageById(buildId)
				if build is None:
					print(f"Could not find OBS package {buildId} for {rpm.shortname}")
					continue

				yield rpm, build

	class SiblingPackageClosure(BuildPackageClosure):
		def classify(self, packages):
			label = self.label
			sourceLabel = self.label.sourceProject

			result = set()

			for rpm, build in self.enumerate(packages):
				problematic = False

				for other in build.binaries:
					if other.isSourcePackage:
						# We're not going to label the source package for now
						continue

					if other.label is None:
						other.label = label
						other.labelReason = Classification.ReasonSiblingPackage(other, rpm)
						result.add(other)
					elif other.label is not rpm.label and \
					     (other.label.sourceProject is not rpm.label.sourceProject):
						# report the problem once when we're done with processing all packages
						problematic = True

						if False:
							print(f"Source project conflict for {build.name}")
							print(f"  {rpm.shortname} was labelled as {rpm.label}, built by {rpm.label.sourceProject}")
							print(f"  {other.shortname} was labelled as {other.label}, built by {other.label.sourceProject}")

				if problematic:
					# print(f"Adding SourceProjectConflict for {build.name}")
					self.handleSourceProjectConflict(build)

			self.result.update(result)
			return result

	class AutoflavorPackageClosure(BuildPackageClosure):
		def __init__(self, problemLog, store):
			super().__init__(problemLog, None, store)
			self.flavors = {}

		def addFlavor(self, name):
			if self.flavors.get(name) is None:
				self.flavors[name] = set()
			return self.flavors[name]

		def getFlavor(self, name):
			return self.flavors.get(name)

		def classify(self, packages):
			result = set()
			for rpm, build in self.enumerate(packages):
				for other in build.binaries:
					if other.label is None:
						continue
					if other.label.type == Classification.TYPE_AUTOFLAVOR:
						# print(f"### identified {other.shortname} as a {other.label.name} package")
						self.addFlavor(other.label.name).add((rpm, other))

		def labelFlavoredPackages(self, flavorName, label):
			result = set()

			matching = self.getFlavor(flavorName)
			if matching:
				for rpm, other in matching:
					# print(f"::: label {other.shortname} as {label}")
					other.label = label
					other.labelReason = Classification.ReasonRelatedPackage(flavorName, other, rpm)
					result.add(other)
			return result

	class RelatedPackageClosure(BuildPackageClosure):
		def classify(self, packages):
			relation = self.RELATION
			label = self.label

			result = set()
			for rpm, build in self.enumerate(packages):
				if relation.checkPackage(rpm):
					print(f"Found {relation.NAME} package {rpm.shortname} in non-{relation.NAME} group {rpm.label}")
					continue

				for other in build.binaries:
					if other.label is None and relation.checkPackage(other):
						print(f"### identified {other.shortname} as a {relation.NAME} package")
						other.label = label
						other.labelReason = Classification.ReasonRelatedPackage(relation, other, rpm)
						result.add(other)

			self.result.update(result)
			return result

	class BuildRequiresClosure(BuildPackageClosure):
		def __init__(self, problemLog, label, store):
			super().__init__(problemLog, label, store)
			self.flavors = {}
			self.unlabelledPackages = None

		def classify(self, packages):
			buildClosure = self.label.buildClosure
			unlabelledPackages = set()
			result = set()

			seen = set()
			for rpm, build in self.enumerate(packages):
				if build in seen:
					continue
				seen.add(build)

				for req in build.buildRequires:
					if req.label is None:
						unlabelledPackages.add((rpm, build.name, req))
					elif req.label not in buildClosure:
						self.handleUnexpectedBuildDependency(rpm, build, req)
					else:
						result.add(req)

			self.unlabelledPackages = unlabelledPackages
			return result

	class DependencyClassifier(Classifier):
		def __init__(self, worker, arch, preferences, label):
			super().__init__(label)

			self.worker = worker
			self.context = worker.contextForArch(arch)
			self.preferences = preferences

		def handleUnresolvableDependency(self, pkg, dep):
			self.worker.problems.addUnableToResolve(pkg, dep)

		def handleUnexpectedDependency(self, pkg, reason):
			self.worker.problems.addUnexpectedDependency(self.label.name, reason, pkg)

		def handleUnlabelledBuildDependency(self, originPackage, buildName, requiredPackage):
			self.worker.problems.addUnlabelledBuildDependency(originPackage, buildName, requiredPackage)

		def handleMissingSource(self, pkg, reason):
			self.worker.problems.addMissingSource(pkg, reason)

		def debugMsg(self, msg):
			self.worker.debugMsg(msg)

	class DownwardClosure(DependencyClassifier):
		def __init__(self, *args):
			super().__init__(*args)
			self.transform = None

		def edges(self, pkg):
			result = []
			for dep, target in self.context.resolveDownward(self.preferences, pkg):
				result.append(Classification.ReasonRequires(target, pkg, dep))

			return result

		def followEdge(self, edge):
			if self.transform:
				edge = self.transform(edge)
				if edge is None:
					return None

			pkg = edge.package
			if pkg.label is self.label:
				return None

			if pkg.label is not None:
				if not self.label.isKnownDependency(pkg.label):
					self.handleUnexpectedDependency(pkg, edge)

				# Do not recurse into this package
				return None

			# print(f"Label {self.label}: classify {edge}")
			pkg.label = self.label
			pkg.labelReason = edge

			self.result.add(pkg)
			return pkg

		def classify(self, packages):
			for pkg in packages:
				assert(pkg.label is self.label)

			self.result.update(set(packages))

			worker = self.worker
			worker.update(packages)
			while True:
				pkg = worker.next()
				if pkg is None:
					break

				edges = self.edges(pkg)
				for e in edges:
					tgt = self.followEdge(e)
					if tgt is not None:
						worker.add(tgt)

			return True

	class UnknownPackageClassifier(DownwardClosure):
		def __init__(self, *args):
			super().__init__(*args)
			self.suggested = {}

		def classify(self, problematicItems):
			worker = self.worker
			for originPackage, buildName, unlabelledPackage in problematicItems:
				if unlabelledPackage.label:
					continue

				worker.add(unlabelledPackage)
				labelClosure = set()
				incrementalPackageClosure = set()

				while True:
					pkg = worker.next()
					if pkg is None:
						break
					for dep, target in self.context.resolveDownward(self.preferences, pkg):
						if target.label is None:
							incrementalPackageClosure.add(target)
							# why are we recursing?
							worker.add(target)
						else:
							labelClosure.add(target.label)

				suggestedLabel = None

				if incrementalPackageClosure:
					# package has dependencies that have not been labelled, either
					# we check for a "good" label recursively, but I'm shying away
					# from the complexity. Or we report the issue.
					self.handleUnlabelledBuildDependency(originPackage, buildName, unlabelledPackage)
				else:
					suggestedLabel = self.findTopmostLabel(labelClosure)

				if suggestedLabel is not None:
					self.suggestLabel(unlabelledPackage, suggestedLabel)
				else:
					pass
					# FIXME: add a problem report

				continue
				names = sorted(_.name for _ in labelClosure)
				print(f"{unlabelledPackage.shortname} -> {' '.join(names)}")
				for pkg in incrementalPackageClosure:
					print(f"  {pkg.shortname} [{pkg.sourceName}]")

		@property
		def suggestions(self):
			result = []
			for label in sorted(self.suggested.keys(), key = lambda _: _.name):
				result.append((label, self.suggested[label]))
			return result

		def suggestLabel(self, pkg, label):
			try:
				suggestions = self.suggested[label]
			except:
				suggestions = []
				self.suggested[label] = suggestions
			suggestions.append(pkg)

		def findTopmostLabel(self, labels):
			from functools import reduce

			if len(labels) == 1:
				return next(iter(labels))

			closure = reduce(set.union, [_.closure for _ in labels], set())
			for lbl in labels:
				if closure.issubset(lbl.closure):
					return lbl
			return None


	class BuildRequireClosure(DownwardClosure):
		def __init__(self, *args):
			super().__init__(*args)
			self.transform = self.transformSource

		def classify(self, packages):
			label = self.label

			sources = set()
			for binary in packages:
				assert(binary.arch != 'src')
				src = binary.sourcePackage
				if src is None:
					# add problem to worker
					print(f"Warning, no source for {binary.fullname()} {binary.arch}")
					continue

				if src.label and src.label is not label:
					# add problem to worker
					print(f"Problem with {src.fullname()}: label {label} vs {src.label}")
					continue

				# print(f"label {src.name} as {label}")
				src.label = label
				src.labelReason = Classification.ReasonSourcePackage(src, binary)

				sources.add(src)

			return super().classify(sources)

		def transformSource(self, arg):
			if isinstance(arg, Classification.Reason):
				reason = arg

				binary = reason.package
				src = binary.sourcePackage
				if src is None:
					print(f"No source for {binary.fullname()}")
					self.handleMissingSource(binary, reason)
					return None

				src.label = self.label
				src.labelReason = Classification.ReasonBuildDependency(src, reason)
				return src.labelReason

			raise Exception()

class PackagePreferences:
	def __init__(self):
		self.neverPreferPatterns = []
		self._comparison = {}

	def prefer(self, preferredName, otherName):
		if preferredName is None:
			self.neverPreferPatterns.append(otherName)
		else:
			self._comparison[preferredName, otherName] = 1
			self._comparison[otherName, preferredName] = -1

	def neverPrefer(self, pattern):
		self.neverPreferPatterns.append(pattern)

	def isNeverPreferred(self, name):
		for pattern in self.neverPreferPatterns:
			if fnmatch.fnmatchcase(name, pattern):
				return True
		return False

	def compare(self, name1, name2):
		try:
			return self._comparison[name1, name2]
		except: pass

		bad1 = self.isNeverPreferred(name1)
		bad2 = self.isNeverPreferred(name2)
		if bad1 == bad2:
			r = 0
		elif bad1:
			r = -1
		else:
			r = 1

		self._comparison[name1, name2] = r
		self._comparison[name2, name1] = -r
		return r


class PackageGroup:
	def __init__(self, name):
		self.name = name

		self.defined = False
		self.matchCount = 0
		self.expand = True
		self.label = None
		self.description = None
		self._packages = []
		self._buildFlavors = {}
		self._runtimeRequires = {}
		self._buildRequires = {}

	def track(self, pkg):
		self._packages.append(pkg)
		self.matchCount += 1

		if pkg.label is None:
			pkg.label = self.label
		elif pkg.label is self.label:
			pass
		else:
			print(f"Package {pkg.fullname()} cannot change label from {pkg.label} to {self.label}")

	@property
	def type(self):
		return self.label.type

	@property
	def packages(self):
		return set(self._packages)

	@property
	def packageNames(self):
		return set(_.name for _ in self._packages)

	@property
	def groupNames(self):
		return set(_.group for _ in self._packages)

	@property
	def runtimeRequires(self):
		return set(self._runtimeRequires.values())

	@property
	def buildRequires(self):
		return set(self._buildRequires.values())

	@property
	def isFlavor(self):
		return self.label.flavorBase is not None

	def addRequires(self, otherGroup):
		if otherGroup.label is None:
			print(f"Group {otherGroup.name} has a NULL label")
		self.label.addRuntimeDependency(otherGroup.label)
		self._runtimeRequires[otherGroup.name] = otherGroup

	def addBuildRequires(self, otherGroup):
		self.label.addBuildDependency(otherGroup.label)
		self._buildRequires[otherGroup.name] = otherGroup

	@property
	def flavors(self):
		return map(lambda pair: pair[1], sorted(self._buildFlavors.items()))

	def addBuildFlavor(self, flavorName, otherGroup):
		if self._buildFlavors.get(flavorName):
			raise Exception(f"Duplicate definition of build flavor {flavorName} for {self.name}")
		self._buildFlavors[flavorName] = otherGroup

		self.label.addBuildFlavor(flavorName, otherGroup.label)

	def getBuildFlavor(self, name):
		return self._buildFlavors.get(name)

class NameFNMatch:
	def __init__(self, type, value, group):
		self.type = type
		self.value = value
		self.group = group

	def __str__(self):
		return f"{self.type} filter \"{self.value}\""

	def match(self, name):
		return fnmatch.fnmatchcase(name, self.value)

class NameEqual:
	def __init__(self, type, value, group):
		self.type = type
		self.value = value
		self.group = group

	def __str__(self):
		return f"{self.type} filter \"{self.value}\""

	def match(self, name):
		return name == self.value

class BaseFilterSet:
	class GlobMatch:
		def __init__(self, value, group):
			self.value = value
			self.group = group

		def __str__(self):
			return self.value

		@property
		def key(self):
			return (-len(self.value), self.value)

		def match(self, name):
			return fnmatch.fnmatchcase(name, self.value)

	class GenericPackageMatch:
		def __init__(self, name, arch, group):
			self.name = name
			self.arch = arch
			self.group = group

		def __str__(self):
			return f"{self.name}.{self.arch}"

		@property
		def key(self):
			return f"{self.name}/{self.arch}"

		def match(self, name, arch):
			if self.arch and self.arch != arch:
				return False

			return fnmatch.fnmatchcase(name, self.name)

	def __init__(self, type):
		self.type = type
		self._exactMatches = {}
		self._globMatches = []

	def addMatch(self, value, group):
		if '*' in value or '?' in value:
			self._globMatches.append(self.GlobMatch(value, group))
		else:
			if self._exactMatches.get(value):
				conflict = self._exactMatches[value]
				print(f"OOPS: {self.type} filter is ambiguous for {value} ({group.name} vs {conflict.name})")
				return
			self._exactMatches[value] = group

	def finalize(self):
		self._globMatches.sort(key = lambda m: m.key)

	def tryFastMatch(self, name):
		group = self._exactMatches.get(name)
		if group is not None:
			return PackageFilter.Verdict(group, f"{self.type} filter {name}")

		return None

	def trySlowNameMatch(self, name):
		for glob in self._globMatches:
			if glob.match(name):
				return PackageFilter.Verdict(glob.group, f"{self.type} filter {glob.key}")

		return None

	def applyName(self, name):
		verdict = self.tryFastMatch(name)
		if verdict is None:
			verdict = self.trySlowNameMatch(name)
		return verdict

class RpmGroupFilterSet(BaseFilterSet):
	def __init__(self):
		super().__init__('rpmgroup')

	def apply(self, pkg, product):
		return self.applyName(pkg.group)

class ProductFilterSet(BaseFilterSet):
	def __init__(self):
		super().__init__('product')

	def apply(self, pkg, product):
		if product is None:
			return None
		return self.applyName(product.name)

class PackageFilterSet(BaseFilterSet):
	def __init__(self):
		super().__init__('package')

	def apply(self, pkg, product):
		return self.applyName(pkg.name)

class SourcePackageFilterSet(BaseFilterSet):
	def __init__(self):
		super().__init__('source package')

	def apply(self, pkg, product):
		src = pkg.sourcePackage
		if src is None:
			return None
		return self.applyName(src.name)

class PackageFilterGroup:
	def __init__(self, prio):
		self.priority = prio
		self._productFilters = None
		self._binaryPkgFilters = None
		self._sourcePkgFilters = None
		self._rpmGroupFilters = None
		self._applicableFilters = None

	@property
	def productFilters(self):
		if not self._productFilters:
			self._productFilters = ProductFilterSet()
		return self._productFilters

	@property
	def binaryPkgFilters(self):
		if not self._binaryPkgFilters:
			self._binaryPkgFilters = PackageFilterSet()
		return self._binaryPkgFilters

	@property
	def sourcePkgFilters(self):
		if not self._sourcePkgFilters:
			self._sourcePkgFilters = SourcePackageFilterSet()
		return self._sourcePkgFilters

	@property
	def rpmGroupFilters(self):
		if not self._rpmGroupFilters:
			self._rpmGroupFilters = RpmGroupFilterSet()
		return self._rpmGroupFilters

	def finalize(self):
		self._applicableFilters = []
		if self._productFilters:
			self._applicableFilters.append(self._productFilters)
		if self._binaryPkgFilters:
			self._applicableFilters.append(self._binaryPkgFilters)
		if self._sourcePkgFilters:
			self._applicableFilters.append(self._sourcePkgFilters)
		if self._rpmGroupFilters:
			self._applicableFilters.append(self._rpmGroupFilters)

		for filterSet in (self._productFilters, self._binaryPkgFilters, self._sourcePkgFilters, self._rpmGroupFilters):
			if filterSet is not None:
				filterSet.finalize()
				self._applicableFilters.append(filterSet)

	def addProductFilter(self, name, group):
		self.productFilters.addMatch(name, group)

	def addBinaryPackageFilter(self, name, group):
		self.binaryPkgFilters.addMatch(name, group)

	def addSourcePackageFilter(self, name, group):
		self.sourcePkgFilters.addMatch(name, group)

	def addRpmGroupFilter(self, name, group):
		self.rpmGroupFilters.addMatch(name, group)

	def apply(self, pkg, product):
		for filterSet in self._applicableFilters:
			verdict = filterSet.apply(pkg, product)
			if verdict is not None:
				break
		return verdict

class PackageFilter:
	PRIORITY_MAX = 10
	PRIORITY_DEFAULT = 7

	class Verdict:
		def __init__(self, group, reason):
			self.group = group
			self.label = group.label
			self.reason = reason

		def labelPackage(self, pkg):
			# HACK
			# Do not group all devel packages, so that the
			# DevelClosure can pick them up later.
			if self.label.type == Classification.TYPE_AUTOFLAVOR:
				pass

			pkg.label = self.label
			pkg.labelReason = Classification.ReasonFilter(pkg, self.reason)

			self.group.track(pkg)

	def __init__(self, filename = 'filter.yaml', scheme = None):
		self.classificationScheme = scheme or Classification.Scheme()
		self._filterGroups = []
		self._groups = {}
		self._preferences = PackagePreferences()
		self._autoflavors = []

		self.defaultFilterGroup = self.makeFilterGroup(self.PRIORITY_DEFAULT)

		with open(filename) as f:
			data = yaml.full_load(f)

		for gd in data['groups']:
			self.parseGroup(Classification.TYPE_BINARY, gd)

		for gd in data.get('build_groups') or []:
			self.parseGroup(Classification.TYPE_SOURCE, gd)

		for gd in data.get('autoflavors') or []:
			group = self.parseGroup(Classification.TYPE_AUTOFLAVOR, gd)
			self._autoflavors.append(group)

		for pref in data.get('preferences') or []:
			preferred = pref.get('prefer')
			over = pref['over']
			if isinstance(over, str):
				self._preferences.prefer(preferred, over)
			else:
				for other in over:
					self._preferences.prefer(preferred, other)

		self.finalize()
		self.classificationScheme.finalize()

	def finalize(self):
		for filterGroup in self._filterGroups:
			filterGroup.finalize()

		self._filterGroups.sort(key = lambda fg: fg.priority)

	def apply(self, pkg, product):
		for filterGroup in self._filterGroups:
			verdict = filterGroup.apply(pkg, product)
			if verdict is not None:
				break

		return verdict

	def makeFilterGroup(self, priority):
		for filterGroup in self._filterGroups:
			if filterGroup.priority == priority:
				return filterGroup

		filterGroup = PackageFilterGroup(priority)
		self._filterGroups.append(filterGroup)

		return filterGroup

	def makeGroup(self, name, type = None):
		return self.makeGroupInternal(name, type)

	def makeSourceGroup(self, name):
		return self.makeGroupInternal(name, Classification.TYPE_SOURCE)

	def instantiateAutoFlavor(self, baseGroup, autoFlavor):
		# groupName = f"{baseGroup.name}+{autoFlavor.name}"
		flavor = baseGroup.getBuildFlavor(autoFlavor.name)
		if flavor is None:
			flavor = self.makeFlavorGroup(baseGroup, autoFlavor.name)

		for req in autoFlavor.runtimeRequires:
			flavor.addRequires(req)
		for req in autoFlavor.buildRequires:
			flavor.addBuildRequires(req)

		return flavor

	def makeFlavorGroup(self, baseGroup, flavorName):
		flavor = baseGroup.getBuildFlavor(flavorName)
		if flavor is not None:
			return flavor

		groupName = f"{baseGroup.name}+{flavorName}"
		flavor = self.makeGroupInternal(groupName, baseGroup.type)

		for buildReq in baseGroup.buildRequires:
			flavor.addBuildRequires(buildReq)

			# FIXME: this can go with autoflavors
			# If this build dependency is not a flavor, automatically
			# add the label's devel flavor
			if False:
				if not buildReq.isFlavor:
					subordinateDevelFlavor = self.makeFlavorGroup(buildReq, "devel")
					flavor.addBuildRequires(subordinateDevelFlavor)

		for runtimeReq in baseGroup.runtimeRequires:
			flavor.addRequires(runtimeReq)

		# Flavors are built from the same source project as their base group
		if flavor.type == Classification.TYPE_BINARY:
			flavor.label.sourceProject = baseGroup.label.sourceProject
			flavor.addRequires(baseGroup)

		names = flavor._runtimeRequires.keys()
		# print(f"Flavor {flavor.label} requires {' '.join(names)}")

		baseGroup.addBuildFlavor(flavorName, flavor)
		return flavor

	def makeGroupInternal(self, name, type):
		try:
			group = self._groups[name]
		except:
			group = None

		if group is None:
			if not type:
				raise Exception(f"Cannot create group {name} with no type")

			group = PackageGroup(name)
			self._groups[name] = group

		if type:
			if group.label is None:
				group.label = self.classificationScheme.createLabel(name, type)
			elif group.type != type:
				raise Exception(f"Group {name} does not match expected type ({group.type} vs {type})")

		return group

	@property
	def groups(self):
		return sorted(self._groups.values(), key = lambda grp: grp.matchCount)

	@property
	def packagePreferences(self):
		return self._preferences

	@property
	def autoFlavors(self):
		return self._autoflavors

	def parseGroup(self, groupType, gd):
		groupName = gd['name']
		group = self.makeGroupInternal(groupName, groupType)
		return self.processGroupDefinition(group, gd)

	def parseBuildFlavor(self, baseGroup, gd):
		group = self.makeFlavorGroup(baseGroup, gd['name'])
		return self.processGroupDefinition(group, gd)

	VALID_GROUP_FIELDS = set((
		'name',
		'description',
		'expand',
		'priority',
		'requires',
		'buildrequires',
		'products',
		'packages',
		'sources',
		'binaries',
		'rpmGroups',
		'buildflavors',
		'sourceproject',
	))

	def processGroupDefinition(self, group, gd):
		if group.defined:
			raise Exception(f"Duplicate definition of group \"{group.name}\" in filter yaml")
		group.defined = True

		for field in gd.keys():
			if field not in self.VALID_GROUP_FIELDS:
				raise Exception(f"Invalid field {field} in definition of group {group.name}")

		group.description = gd.get('description')

		value = gd.get('expand')
		if type(value) == bool:
			group.expand = value

		value = gd.get('priority')
		if value is not None:
			assert(type(value) == int)
			filterGroup = self.makeFilterGroup(value)
		else:
			filterGroup = self.defaultFilterGroup

		if group.label:
			nameList = gd.get('requires') or []
			for name in nameList:
				otherGroup = self.makeGroupInternal(name, Classification.TYPE_BINARY)
				group.addRequires(otherGroup)

			nameList = gd.get('buildrequires') or []
			for name in nameList:
				otherGroup = self.makeGroupInternal(name, Classification.TYPE_BINARY)
				group.addBuildRequires(otherGroup)

		name = gd.get('sourceproject')
		if name is not None:
			sourceProject = self.makeGroupInternal(name, Classification.TYPE_SOURCE)
			group.label.setSourceProject(sourceProject.label)

		nameList = gd.get('products') or []
		for name in nameList:
			filterGroup.addProductFilter(name, group)

		nameList = gd.get('packages') or []
		for name in nameList:
			filterGroup.addBinaryPackageFilter(name, group)
			filterGroup.addSourcePackageFilter(name, group)

		nameList = gd.get('sources') or []
		for name in nameList:
			filterGroup.addSourcePackageFilter(name, group)

		nameList = gd.get('binaries') or []
		for name in nameList:
			filterGroup.addBinaryPackageFilter(name, group)

		nameList = gd.get('rpmGroups') or []
		for name in nameList:
			filterGroup.addRpmGroupFilter(name, group)

		flavors = gd.get('buildflavors') or []
		for fd in flavors:
			self.parseBuildFlavor(group, fd)

		return group
