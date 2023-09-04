import yaml
import fnmatch

class Classification:
	class Label:
		def __init__(self, name, id):
			self.name = name
			self.id = id
			self.uses = set()
			self.builds = set()
			self.explicitClosure = set()
			self._closure = None

		def addRuntimeDependency(self, other):
			self._closure = None
			self.uses.add(other)

		def addBuildDependency(self, other):
			self.builds.add(other)

		def addToClosure(self, other):
			self.explicitClosure.add(other)

		@property
		def closure(self):
			if self._closure is None:
				self._closure = set()
				self._closure.update(self.uses)
				self._closure.update(self.explicitClosure)
				for req in self.uses:
					self._closure.update(req.closure)
			return self._closure

		def isKnownDependency(self, other):
			return other in self.closure

		def __str__(self):
			return self.name

	class Scheme:
		def __init__(self):
			self._labels = {}
			self._nextLabelId = 0

		def createLabel(self, name):
			label = self._labels.get(name)
			if label is None:
				label = Classification.Label(name, self._nextLabelId)
				self._labels[name] = label
				self._nextLabelId += 1
			return label

		@property
		def allLabels(self):
			return sorted(self._labels.values(), key = lambda _: _.name)

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

	class XXXSourceClassifier(Classifier):
		def classify(self, packages):
			label = self.label

			result = set()
			for binary in packages:
				src = binary.sourcePackage
				if src is None:
					# add problem to worker
					print(f"Warning, no source for {binary.fullname()}")
					continue

				if src.label and src.label is not label:
					print(f"Problem with {src.fullname()}: label {label} vs {src.label}")
					continue

				# print(f"label {src.name} as {label}")
				src.label = label
				src.labelReason = Classification.ReasonSourcePackage(src, binary)

				result.add(src)

			self.result.update(result)
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
			result = set()
			for pkg in arg:
				src = pkg.sourcePackage
				if src is None:
					print(f"Warning, no source for {pkg.fullname()}")
				elif src.label and src.label is not self.label:
					print(f"Cannot label {src.fullname()} as {self.label}; it is already labeled {src.label}")
				else:
					result.add(src)
			return result

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
	TYPE_BINARY = 'binary'
	TYPE_SOURCE = 'source'

	def __init__(self, name, type = None):
		if type is None:
			type = self.TYPE_BINARY

		self.name = name
		self.type = type

		self.defined = False
		self.matchCount = 0
		self.expand = True
		self.label = None
		self._packages = []

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
	def packages(self):
		return set(self._packages)

	@property
	def packageNames(self):
		return set(_.name for _ in self._packages)

	@property
	def groupNames(self):
		return set(_.group for _ in self._packages)

	def addRequires(self, otherGroup):
		if self.type == self.TYPE_BINARY:
			self.label.addRuntimeDependency(otherGroup.label)
		else:
			self.label.addBuildDependency(otherGroup.label)

	def addBuildRequires(self, otherGroup):
		assert(self.type == self.TYPE_SOURCE)
		self.label.addBuildDependency(otherGroup.label)

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

		@property
		def key(self):
			return self.value

		def match(self, name):
			return fnmatch.fnmatchcase(name, self.value)

	class GenericPackageMatch:
		def __init__(self, name, arch, group):
			self.name = name
			self.arch = arch
			self.group = group

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
				print(f"OOPS: {self.type} filter us anbiguous for {value} ({group.name} vs {conflict.name})")
				return
			self._exactMatches[value] = group

	def finalize(self):
		self._globMatches.sort(key = lambda m: m.key, reverse = True)

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
			pkg.label = self.label
			pkg.labelReason = Classification.ReasonFilter(pkg, self.reason)

			self.group.track(pkg)

	def __init__(self, filename = 'filter.yaml', scheme = None):
		self.classificationScheme = scheme or Classification.Scheme()
		self._filterGroups = []
		self._groups = {}
		self._preferences = PackagePreferences()

		self.defaultFilterGroup = self.makeFilterGroup(self.PRIORITY_DEFAULT)

		with open(filename) as f:
			data = yaml.full_load(f)

		for gd in data['groups']:
			self.parseGroup(PackageGroup.TYPE_BINARY, gd)

		for gd in data.get('build_groups') or []:
			self.parseGroup(PackageGroup.TYPE_SOURCE, gd)

		for pref in data.get('preferences') or []:
			preferred = pref.get('prefer')
			over = pref['over']
			if isinstance(over, str):
				self._preferences.prefer(preferred, over)
			else:
				for other in over:
					self._preferences.prefer(preferred, other)

		self.finalize()

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
		return self.makeGroupInternal(name, PackageGroup.TYPE_SOURCE)

	def makeGroupInternal(self, name, type):
		try:
			group = self._groups[name]
		except:
			group = None

		if group is None:
			group = PackageGroup(name, type)

			if self.classificationScheme:
				group.label = self.classificationScheme.createLabel(name)

			self._groups[name] = group
		elif type and group.type != type:
			raise Exception(f"Group {name} does not match expected type ({group.type} vs {type})")
		return group

	@property
	def groups(self):
		return sorted(self._groups.values(), key = lambda grp: grp.matchCount)

	@property
	def packagePreferences(self):
		return self._preferences

	def parseGroup(self, groupType, gd):
		group = self.makeGroupInternal(gd['name'], groupType)

		if group.defined:
			raise Exception(f"Duplicate definition of group \"{group.name}\" in filter yaml")
		group.defined = True

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
				otherGroup = self.makeGroupInternal(name, groupType)
				group.addRequires(otherGroup)

			nameList = gd.get('explicitClosure') or []
			for name in nameList:
				group.label.addToClosure(self.classificationScheme.createLabel(name))

			nameList = gd.get('builds') or []
			for name in nameList:
				otherGroup = self.makeGroupInternal(name, PackageGroup.TYPE_BINARY)
				group.addBuildRequires(otherGroup)

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
