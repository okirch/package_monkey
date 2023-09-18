import yaml
import fnmatch

from util import CycleDetector, LoggingCycleDetector, CycleException, GenerationCounter, Timestamp
from ordered import PartialOrder
from functools import reduce

def intersectSets(a, b):
	if a is None:
		return b
	elif b is None:
		return a
	return a.intersection(b)

def boundingSetIsEmpty(a):
	if a is None:
		return False
	assert(type(a) == set)
	return not bool(a)

class Classification:
	TYPE_BINARY = 'binary'
	TYPE_SOURCE = 'source'
	TYPE_AUTOFLAVOR = 'autoflavor'
	TYPE_BUILDCONFIG = 'buildconf'
	TYPE_BUILDCONFIG_FLAVOR = 'build-flavor'

	DISPOSITION_SEPARATE = 'separate'
	DISPOSITION_MERGE = 'merge'
	DISPOSITION_MAYBE_MERGE = 'maybe_merge'
	DISPOSITION_IGNORE = 'ignore'

	class Label:
		GENERATION = GenerationCounter()
		RUNTIME_CYCLE_GUARD = CycleDetector("runtime dependency")
		BUILD_CYCLE_GUARD = CycleDetector("build dependency")

		def __init__(self, name, type, id):
			self.name = name
			self.type = type
			self.id = id
			self.runtimeRequires = set()
			self.buildRequires = set()
			self.runtimeAugmentations = set()
			self.disposition = Classification.DISPOSITION_SEPARATE
			self.defined = False

			# This is populated for labels that represent a build flavor like @Core+devel
			self.flavorBase = None
			self.flavorName = None

			# This is populated for base flavors like @Core
			self._flavors = {}

			self.mergeableAutoFlavors = set()

			self._timestamp = Timestamp()

			# the closure comprises all packages of this label plus the ones
			# referenced by subordinate labels
			self._closure = None

			# the build closure comprises the labels that were listed as
			# build requirements, recursively.
			self._buildClosure = None

			# build config labels have a source project assigned
			self.sourceProject = None

			# binary labels have a build config assigned
			self.buildConfig = None

			# if autoSelect is true, then a group referencing a label
			# "@Foo" will automatically select all flavors "@Foo+bar"
			# if it supports all requirements of this flavor.
			# For instance, "@Core+systemd" may contain utilities that
			# need libsystemd. A label "@Bar" that requires both @Core
			# and @MinimalSystemd will automatically add @Foo+bar to its
			# closure
			self.autoSelect = True

		@classmethod
		def hierarchyNeedsUpdate(klass):
			klass.GENERATION.tick()

		def addRuntimeDependency(self, other):
			assert(isinstance(other, Classification.Label))
			self.runtimeRequires.add(other)
			self.hierarchyNeedsUpdate()

		def addRuntimeAugmentation(self, other):
			self.addRuntimeDependency(other)
			self.runtimeAugmentations.add(other)

		def addBuildDependency(self, other):
			assert(isinstance(other, Classification.Label))
			self.buildRequires.add(other)
			self.hierarchyNeedsUpdate()

		def addMergeableFlavor(self, autoFlavor):
			assert(autoFlavor.type == Classification.TYPE_AUTOFLAVOR)
			self.mergeableAutoFlavors.add(autoFlavor)

		def autoFlavorCanBeMerged(self, autoFlavor):
			if autoFlavor.disposition == Classification.DISPOSITION_MERGE:
				return True
			return autoFlavor in self.mergeableAutoFlavors

		def copyRequirementsFrom(self, other):
			self.runtimeRequires.update(other.runtimeRequires)
			self.runtimeAugmentations.update(other.runtimeAugmentations)
			self.buildRequires.update(other.buildRequires)
			self.hierarchyNeedsUpdate()

		@property
		def flavors(self):
			return map(lambda pair: pair[1], sorted(self._flavors.items()))

		def getBuildFlavor(self, name):
			return self._flavors.get(name)

		def addBuildFlavor(self, flavorName, otherLabel):
			if self.flavorBase is not None:
				print(f"WARNING: creating build flavor {self}+{flavorName} - that way lies madness")

			if self.getBuildFlavor(flavorName) is not None:
				raise Exception(f"Duplicate definition of flavor {flavorName} for {self.name}")

			self._flavors[flavorName] = otherLabel

			# flavors inherit the parent's build project by default
			if self.sourceProject and not otherLabel.sourceProject:
				otherLabel.setSourceProject(self.sourceProject)
			if self.buildConfig and not otherLabel.buildConfig:
				otherLabel.setBuildConfig(self.buildConfig)

			if self.sourceProject and otherLabel.sourceProject is not self.sourceProject:
				raise Exception(f"build flavor {otherLabel} uses source project {otherLabel.sourceProject}, but {self} uses {self.sourceProject}")

			# This creates a circular reference that kills garbage collection, but
			# we'll live with this for now
			otherLabel.flavorBase = self
			otherLabel.flavorName = flavorName

			self.hierarchyNeedsUpdate()

		def setSourceProject(self, sourceLabel):
			if self.sourceProject is sourceLabel:
				return
			if self.sourceProject is not None:
				raise Exception(f"Duplicate source group for {self}: {self.sourceProject} vs {sourceLabel}")

			assert(isinstance(sourceLabel, Classification.Label))
			self.sourceProject = sourceLabel

			self.hierarchyNeedsUpdate()

		def setBuildConfig(self, configLabel):
			if self.buildConfig is configLabel:
				return
			if self.buildConfig is not None:
				raise Exception(f"Duplicate source group for {self}: {self.buildConfig} vs {configLabel}")
			self.buildConfig = configLabel
			if self.sourceProject is None:
				self.sourceProject = configLabel.sourceProject

			self.copyRequirementsFrom(configLabel)
			self.hierarchyNeedsUpdate()

		def getBuildConfigFlavor(self, name):
			sourceProject = self.sourceProject
			if sourceProject is None:
				return None

			return sourceProject.getBuildFlavor(name)

		# FIXME: rename to runtimeClosure
		@property
		def closure(self):
			fail
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
			for label in self.runtimeRequires:
				result.update(label.closure)

			if self.type is Classification.TYPE_BINARY:
				result.add(self)

			self._closure = result

		def updateBuildClosure(self):
			self._buildClosure = None

			result = set()
			result.update(self.closure)
			for label in self.runtimeRequires:
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
			for label in self.runtimeRequires:
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
			if self.buildConfig and self.buildConfig == other.buildConfig:
				return True
			return False

		def mayAutoSelect(self, flavor):
			if flavor is self:
				return False
			if not flavor.autoSelect:
				return False
			if flavor.flavorBase is None:
				return False

			# this is somewhat tricky but important.
			# consider this:
			#	@Python+gnome, which requires @Gnome. it contains python
			#		modules that would be useful in a Gnome environment
			#	@Gnome+python, which requires @Python, and contains a
			#		(hypothetical) python utility named gnome-foo
			#
			# If we're not careful, we end up with @Python+gnome auto-selecting
			# @Gnome+python. But what we really want is the other way around
			if not self.runtimeAugmentations.isdisjoint(flavor.closure):
				return False

			return True

		def autoSelectCompatibleFlavors(self):
			print("autoSelectCompatibleFlavors disabled")
			return

			if not self.autoSelect:
				return

			availableFlavors = set()
			for requiredLabel in self.closure:
				for flavor in requiredLabel.flavors:
					if self.mayAutoSelect(flavor):
						availableFlavors.add(flavor)

			candidateFlavors = availableFlavors.difference(self.closure)

			# Two things to note: we avoid adding new flavors immediately, in order to
			# avoid some costly recomputations of label closures.
			# Second, we iterate because adding one flavor in round #1 may actually
			# provide the needed support to enable a second flavor in round #2.
			# A typical example would be @GnomeLibraries requiring @DBus and @GLib.
			# The first round would enable @GLib+dbus, and the second round would enable
			# @DBus+x11
			while candidateFlavors:
				# print(self, " try to select from ", [str(_) for _ in candidateFlavors])
				eligibleFlavors = set()
				for flavor in candidateFlavors:
					if flavor.allFlavorRequirementsSatisfied(self):
						# print(f"{self} auto-selected {flavor}")
						eligibleFlavors.add(flavor)

				if not eligibleFlavors:
					break

				for flavor in eligibleFlavors:
					self.addRuntimeDependency(flavor) 

				candidateFlavors.difference_update(eligibleFlavors)

		def allFlavorRequirementsSatisfied(self, referringLabel):
			if referringLabel in self.closure:
				return False
			if not referringLabel.autoSelect:
				return False
			if not self.autoSelect:
				return False
			if self.flavorBase is None:
				return False
			if referringLabel.flavorBase == self:
				return False

			missing = self.closure.difference(self.flavorBase.closure)
			missing.difference_update(referringLabel.closure)
			try:
				missing.remove(self)
			except: pass

			return not(missing)

		def __str__(self):
			return self.name

	class Scheme:
		def __init__(self):
			self._labels = {}
			self._nextLabelId = 0

		def getLabel(self, name):
			return self._labels.get(name)

		def createLabel(self, name, type):
			label = self._labels.get(name)
			if label is None:
				label = Classification.Label(name, type, self._nextLabelId)
				self._labels[name] = label
				self._nextLabelId += 1
			elif label.type != type:
				raise Exception(f"Conflicting types for label {name}. Already have a label of type {label.type}, now asked to create {type}")
			return label

		@property
		def allLabels(self):
			return sorted(self._labels.values(), key = lambda _: _.name)

		def createOrdering(self, labelType):
			if labelType != Classification.TYPE_BINARY:
				raise Exception(f"Unable to create an ordering for {labelType} labels")

			order = PartialOrder("runtime dependency")
			for label in self._labels.values():
				if label.type is labelType:
					order.add(label, label.runtimeRequires)

			order.finalize()
			return order

		def finalize(self):
			def inheritSourceProject(label):
				if label.sourceProject is None:
					if label.flavorBase:
						source = inheritSourceProject(label.flavorBase)
						if source:
							label.setSourceProject(source)
				return label.sourceProject

			def inheritBuildConfig(label):
				if label.buildConfig is None:
					if label.flavorBase:
						source = inheritBuildConfig(label.flavorBase)
						if source:
							label.setBuildConfig(source)
				return label.buildConfig

			for label in self._labels.values():
				if label.sourceProject is None:
					inheritSourceProject(label)
				if label.buildConfig is None:
					inheritBuildConfig(label)

				if label.type == Classification.TYPE_BINARY:
					if label.buildConfig is None and label.disposition != Classification.DISPOSITION_IGNORE:
						raise Exception(f"Label {label}: no buildconfig specified")
					elif not label.buildConfig.defined:
						raise Exception(f"Label {label} references buildconfig {label.buildConfig}, but it's not defined anywhere")

			return

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
			if label.buildConfig:
				print(f"  build config {label.buildConfig}")
			for name, lset in (("requires", label.runtimeRequires), ("buildrequires", label.buildRequires), ("closure", label._closure), ("build closure", label._buildClosure)):
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
			return f"{self.package} identified by {self.filterDesc}"

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
			result = f"{self.package} required by {self.dependant}"
			if self.req is not None:
				result += f" via {self.req}"
			return result

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
			return f"{self.package} is the source of {self.binary}"

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
			return f"{self.package} is a sibling package of {self.sibling}"

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
			return f"{self.package} is a {self.relation} package related to {self.sibling}"

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
			return f"{self.package} builds {self.parentReason.package}"

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
			return f"{self.package} built from the same source as {self.sibling}"

	class ClassificationContext:
		def __init__(self, worker, productArchitecture, labelOrder, store):
			self.worker = worker
			self.productArchitecture = productArchitecture
			self.labelOrder = labelOrder
			self.store = store

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
		def __init__(self, classificationContext, label):
			super().__init__(label)

			self.worker = classificationContext.worker
			self.context = self.worker.contextForArch(classificationContext.productArchitecture)
			self.labelOrder = classificationContext.labelOrder
			self.store = classificationContext.store

		def handleUnresolvableDependency(self, pkg, dep):
			self.worker.problems.addUnableToResolve(pkg, dep)

		def handleUnexpectedDependency(self, pkg, reason):
			self.worker.problems.addUnexpectedDependency(self.label, reason, pkg)

		def handleUnlabelledBuildDependency(self, originPackage, buildName, requiredPackage):
			self.worker.problems.addUnlabelledBuildDependency(originPackage, buildName, requiredPackage)

		def handleMissingSource(self, pkg, reason):
			self.worker.problems.addMissingSource(pkg, reason)

		# a is a known dependency of b iff a <= b in the label order
		def isKnownDependency(self, a, b):
			if a in self.labelOrder.downwardClosureFor(b):
				return True

			if a.buildConfig and a.buildConfig == b.buildConfig:
				return True

			if False:
				print(f"label {a} is not a good dependency of {b} [buildconf {a.buildConfig} vs {b.buildConfig}]")
				names = map(str, self.labelOrder[b]._downwardClosure)
				print(f" -> {' '.join(names)}")

			return False

		def debugMsg(self, msg):
			self.worker.debugMsg(msg)

	class DownwardClosure(DependencyClassifier):
		def __init__(self, *args):
			super().__init__(*args)
			self.transform = None

		def edges(self, pkg):
			result = []
			for dep, target in self.context.resolveDownward(pkg):
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
				# if the package belongs to a group that is within our closure,
				# we're fine. Otherwise, flag this dependency as bad
				if not self.isKnownDependency(pkg.label, self.label):
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

	class FlexibleDownwardClosure(DependencyClassifier):
		def __init__(self, context, potentialClassification):
			super().__init__(context, None)
			self.potentialClassification = potentialClassification
			self.transform = None

		def edges(self, pkg):
			result = []
			for dep, target in self.context.resolveDownward(pkg):
				if target is not pkg:
					result.append(Classification.ReasonRequires(target, pkg, dep))

			return result

		def followEdge(self, edge):
			if self.transform:
				edge = self.transform(edge)
				if edge is None:
					return None

			self.potentialClassification.addEdge(edge.dependant, edge.package)

			if True:
				build = self.getBuildForPackage(edge.package)
				if build is not None:
					self.potentialClassification.associateBuild(edge.package, build)

			return edge.package

		def classify(self, packages):
			worker = self.worker
			worker.update(packages)
			while True:
				pkg = worker.next()
				if pkg is None:
					break

				if pkg.isSourcePackage:
					continue

				edges = self.edges(pkg)
				for e in edges:
					tgt = self.followEdge(e)
					if tgt is not None:
						worker.add(tgt)

			return True

		def getBuildForPackage(self, rpm):
			buildId = rpm.obsBuildId
			if buildId is None:
				print(f"No OBS package for {rpm}")
				return None

			build = self.store.retrieveOBSPackageById(buildId)
			if build is None:
				print(f"Could not find OBS package {buildId} for {rpm.shortname}")

			return build

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

class PotentialClassification(object):
	# Not really an interval but a convex set
	class LabelInterval:
		def __init__(self, order, name, package = None, cycle = None):
			assert(package or cycle)

			self.name = name
			self.package = package
			self.siblings = None
			self._cycle = cycle
			self._order = order
			self._lowerNeighbors = set()
			self._upperNeighbors = set()
			self.upperBounds = set()
			self.lowerBounds = set()
			self._lowerCone = None
			self._upperCone = None
			self._candidates = None
			self._solution = None

			if package:
				label = package.label
				if label and label.type == Classification.TYPE_BINARY:
					self._solution = label

			self._trace = False
			if self.name in []:
				self._trace = True

		def zap(self):
			self._lowerNeighbors = None
			self._upperNeighbors = None

		def __str__(self):
			return self.name

		@property
		def solution(self):
			return self._solution

		@solution.setter
		def solution(self, label):
			if self._solution and self._solution is not label:
				raise Exception(f"Conflicting solution for {self}: label {self._solution} vs {label}")
			if self._trace:
				print(f" {self} set solution to {label}")
			self._solution = label

			# update lower and upper cone here?

		@property
		def solutionBaseLabel(self):
			if not self._solution:
				return None
			return self._solution.flavorBase or self._solution

		@property
		def packages(self):
			if self.package:
				return set([self.package])
			return self._cycle

		def addLowerNeighbor(self, other):
			self._lowerNeighbors.add(other)

		def addUpperNeighbor(self, other):
			self._upperNeighbors.add(other)

		@property
		def lowerNeighbors(self):
			return self._lowerNeighbors

		@property
		def upperNeighbors(self):
			return self._upperNeighbors

		@property
		def lowerCone(self):
			if self._solution is not None:
				return self._order.upwardClosureFor(self._solution)
			return self._lowerCone

		@property
		def lowerBoundConflict(self):
			return self._solution is None and boundingSetIsEmpty(self._lowerCone)

		@property
		def upperBoundConflict(self):
			return self._solution is None and boundingSetIsEmpty(self._upperCone)

		@property
		def upperCone(self):
			if self._solution is not None:
				return self._order.downwardClosureFor(self._solution)
			return self._upperCone

		def intersectCones(self, a, b):
			return intersectSets(a, b)

		def updateFromBelow(self, lowerNeighbor):
			# do NOT use update_intersection
			self._lowerCone = self.intersectCones(self.lowerCone, lowerNeighbor.lowerCone)

			if self._trace:
				print(f" {self}: add lower neighbor {lowerNeighbor}")
				print(f"    lower cone is {self.describeCone(self.lowerCone)}")

		def updateFromAbove(self, upperNeighbor):
			# do NOT use update_intersection
			self._upperCone = self.intersectCones(self.upperCone, upperNeighbor.upperCone)

			if self._trace:
				print(f" {self}: add upper neighbor {upperNeighbor}")
				print(f"    upper cone is {self.describeCone(self.upperCone)}")

		def describeCone(self, cone):
			if cone is None:
				return "undefined"
			if not cone:
				return "empty"
			names = list(map(str, cone))
			return ' '.join(names)

		def x__update(self, limits):
			if self._candidates is None:
				self._candidates = limits
			else:
				self._candidates = self._candidates.intersection(limits)

		@property
		def candidates(self):
			return self.intersectCones(self.lowerCone, self.upperCone)

	class SiblingInfo:
		def __init__(self, build):
			self.packages = []

			for rpm in build.binaries:
				# We're not going to label the source package for now
				if not rpm.isSourcePackage:
					self.packages.append(rpm)

		def __iter__(self):
			return iter(self.packages)

		@property
		def preferredBaseLabel(self):
			result = set()
			for pkg in self.packages:
				label = pkg.label
				if label and label.type == Classification.TYPE_BINARY:
					if label.flavorBase:
						result.add(label.flavorBase)
					else:
						result.add(label)

			if len(result) == 1:
				return result.pop()
			return None

		@property
		def allLabels(self):
			result = set()
			for pkg in self.packages:
				label = pkg.label
				if label and label.type == Classification.TYPE_BINARY:
					result.add(label)
			return result

		@property
		def allBaseLabels(self):
			result = set()
			for pkg in self.packages:
				label = pkg.label
				if label and label.type == Classification.TYPE_BINARY and label.flavorBase:
					result.add(label.flavorBase)
			return result

		@property
		def preferredLabel(self):
			labels = set()
			baseLabels = set()
			for pkg in self.packages:
				label = pkg.label
				if label and label.type == Classification.TYPE_BINARY:
					labels.add(label)
					if label.flavorBase:
						baseLabels.add(label.flavorBase)

			if len(labels) == 1:
				return labels.pop()

			if len(baseLabels) == 1:
				return baseLabels.pop()

			return None

	def __init__(self, order):
		self._order = order
		self._packages = {}
		self._builds = {}

	def addEdge(self, requiringPackage, requiredPackage):
		assert(requiringPackage is not requiredPackage)
		upper = self.getPackage(requiringPackage)
		lower = self.getPackage(requiredPackage)
		upper.addLowerNeighbor(lower)
		lower.addUpperNeighbor(upper)

	def associateBuild(self, package, build):
		if build in self._builds:
			return

		siblings = self.SiblingInfo(build)
		self._builds[build] = siblings
		for pkg in siblings.packages:
			self.getPackage(pkg).siblings = siblings

	def setSolution(self, pkg, label):
		self.getPackage(pkg).solution = label

	def getPackage(self, pkg):
		try:
			interval = self._packages[pkg]
		except:
			interval = self.LabelInterval(self._order, name = str(pkg), package = pkg)
			self._packages[pkg] = interval
		return interval

	def collapse(self, cycle):
		cycleSet = set(cycle)
		cyclePackages = reduce(set.union, (interval.packages for interval in cycle))
		cycleNames = list(map(str, cyclePackages))

		labels = set()
		for node in cycle:
			if node.solution is not None:
				labels.add(node.solution)

		label = None
		if labels:
			if len(labels) > 1:
				print(f"Warning, unable to collapse cycle {' '.join(cycleNames)} because it has conflicting labels")
				for node in cycle:
					if node.solution:
						print(f"  {node}: {node.solution}")

				# labelNames = map(str, labels)
				# raise Exception(f"Cannot collapse cycle {' '.join(cycleNames)} because it has conflicting labels: {' '.join(labelNames)}")
				print("Picking a random label for now")
			label = labels.pop()

		above = reduce(set.union, (node._upperNeighbors for node in cycle), set())
		below = reduce(set.union, (node._lowerNeighbors for node in cycle), set())

		newInterval = self.LabelInterval(self._order, name = f"<{' '.join(cycleNames)}>", cycle = cyclePackages)
		newInterval._lowerNeighbors = below.difference(cycleSet)
		newInterval._upperNeighbors = above.difference(cycleSet)
		if label:
			newInterval.solution = label

		for lower in below:
			lower._upperNeighbors.difference_update(cycleSet)
			lower._upperNeighbors.add(newInterval)

		for upper in above:
			upper._lowerNeighbors.difference_update(cycleSet)
			upper._lowerNeighbors.add(newInterval)

		for pkg in cycle:
			self._packages[pkg] = newInterval

		print(f"Collapsed dependency cycle {newInterval}")

	def createPartialOrder(self):
		order = PartialOrder("runtime dependency")

		seen = set()
		for interval in self._packages.values():
			# we have to check for duplicate nodes because we may have collapsed
			# a dependency loop, so that we have several packages point to the same
			# LabelInterval
			if interval not in seen:
				order.add(interval, interval._lowerNeighbors)
				seen.add(interval)

		cycles = order.getCollapsibleCycles()
		if cycles:
			for cycle in cycles:
				self.collapse(cycle.members)
			# rinse and repeat
			return None

		order.finalize()
		return order

	def ignoreFlavoredPackages(self, order, tabooFlavorNames):
		hidden = set()
		for interval in self._packages.values():
			if interval.package and interval.package.label and interval.package.label.flavorName in tabooFlavorNames:
				hidden.add(interval)

		# un-hide any packages that are required by a visible package
		for interval in order.topDownTraversal():
			if interval not in hidden:
				hidden.difference_update(interval._lowerNeighbors)

		order.hide(hidden)

	def solve(self):
		order = None

		# we may have to repeat this step several times, because
		# collapsing one cycle may introduce a new cycle.
		while order is None:
			order = self.createPartialOrder()

		# on the first pass, hide any non-essential packages
		self.ignoreFlavoredPackages(order, ('devel', 'doc', 'i18n', 'man'))

		for interval in order.bottomUpTraversal():
			for lower in interval.lowerNeighbors:
				interval.updateFromBelow(lower)

			# Report those packages where the lowerCone collapses to an empty set
			# (All the packages above will naturally have an empty lower cone as well)
			if False and interval.lowerBoundConflict:
				if not any(neigh.lowerBoundConflict for neigh in interval.lowerNeighbors):
					print(f"{interval} has an actual conflict between its requirements")
					for lower in interval.lowerNeighbors:
						if lower.solution:
							print(f"    {lower} labelled {lower.solution}")
						elif lower._lowerCone is not None:
							# the lower cone is an intersection of N upward closures,
							# recover the original labels
							bounds = self._order.minima(lower.lowerCone)
							names = ' '.join(map(str, bounds))
							print(f"    {lower} bounded by {names}")
						else:
							print(f"    {lower} unbounded")


		for interval in order.topDownTraversal():
			for lower in interval.lowerNeighbors:
				lower.updateFromAbove(interval)

		self.reportUnplaceablePackages(order)

		for interval in order.bottomUpTraversal():
			# don't do anything if already solved
			if interval.solution:
				continue

			pkg = interval.package
			if interval.package is None:
				# will need to implement support for collapsed cycles here
				continue

			autoFlavor = None
			if pkg.label and pkg.label.type == Classification.TYPE_AUTOFLAVOR:
				# this package has been labeled with a generic autoflavor like "doc", "devel" etc
				autoFlavor = pkg.label

			baseLabel = self.commonSiblingBaseLabel(interval)
			if baseLabel is None:
				continue

			print(f"{interval} siblings have common base label {baseLabel}")
			choice = None
			if autoFlavor:
				# this package has been labeled with a generic autoflavor like "doc", "devel" etc
				flavor = baseLabel.getBuildFlavor(autoFlavor.name)
				if flavor is None:
					print(f"{interval} cannot be placed; common base label {baseLabel} has no flavor {autoFlavor}")
				elif self.isValidLabelForInterval(flavor, interval):
					choice = flavor
				else:
					print(f"{interval} cannot be placed into {flavor} because it's not a candidate label")
					print(f"   Conflicting lower and upper neighbors")
					for below in interval._lowerNeighbors:
						if below._lowerCone is not None and flavor not in below._lowerCone:
							print(f"    - {below} (requires)")
					for above in interval._upperNeighbors:
						if above._upperCone is not None and flavor not in above._upperCone:
							print(f"    - {above} (required by)")
			elif self.isValidLabelForInterval(baseLabel, interval):
				choice = baseLabel
			else:
				goodFlavors = set()
				for flavor in baseLabel.flavors:
					if self.isValidLabelForInterval(flavor, interval):
						goodFlavors.add(flavor)

				bestFlavors = intersectSets(goodFlavors, interval.candidates)
				if bestFlavors:
					names = ' '.join(map(str, bestFlavors))
					print(f"   found best flavor(s) {names}")
				elif goodFlavors:
					names = ' '.join(map(str, goodFlavors))
					print(f"   found good flavor(s) {names}")
					bestFlavors = goodFlavors

				if len(bestFlavors) > 1:
					bestFlavors = self._order.minima(bestFlavors)

				if len(bestFlavors) == 1:
					choice = bestFlavors.pop()

			if choice:
				print(f"{interval} is being placed into {choice} because its siblings are in {baseLabel}")
				choice = self.chooseLabelForInterval(interval, choice)

		for interval in order.topDownTraversal():
			# don't do anything if already solved
			if interval.solution:
				continue

			pkg = interval.package
			if interval.package is None:
				# will need to implement support for collapsed cycles here
				continue

			autoFlavor = None
			if pkg.label and pkg.label.type == Classification.TYPE_AUTOFLAVOR:
				# this package has been labeled with a generic autoflavor like "doc", "devel" etc
				autoFlavor = pkg.label

			candidates = interval.candidates
			if candidates is None:
				if autoFlavor:
					# this package has been labeled with a generic autoflavor like "doc", "devel" etc
					self.tryToPlaceWithSibling(interval)
				continue

			if len(candidates) == 1:
				uniqueLabel = candidates.pop()
				choice = self.chooseLabelForPackage(pkg, uniqueLabel)
				if choice:
					print(f"{interval} is being placed into {choice} because it's the unique choice")
					interval.solution = choice
			elif len(candidates) == 0:
				def showNeighbors(tag, neighbors, getSpan = None):
					if not neighbors:
						return
					print(f"    {tag}")

					found = set()
					for neigh in neighbors:
						if neigh.solution:
							print(f"      {neigh} [{neigh.solution}]")
							found.add(neigh.solution)
						elif neigh.candidates is not None:
							n = len(neigh.candidates)
							print(f"      {neigh} [{n} candidates]")
						else:
							print(f"      {neigh} [unsolveable]")
					if found:
						span = getSpan(found)
						names = ' '.join(map(str, span))
						print(f"     -> bounded by {names}")

				print(f"{interval} cannot be placed due to conflicts")
				if interval.package and interval.package.label:
					print(f"   {interval.package} has been labelled {interval.package.label}")
				showNeighbors("lower neighbors", interval._lowerNeighbors, self._order.maxima)
				showNeighbors("upper neighbors", interval._upperNeighbors, self._order.minima)
			elif self.tryToPlaceIntoCommonBase(interval):
				pass
			else:
				self.tryToPlaceWithSibling(interval)

		xxx

	def baseLabelsForSet(self, labels):
		if labels is None:
			return None
		return set(map(lambda label: label.flavorBase or label, labels))

	def isValidLabelForInterval(self, label, interval):
		if interval.candidates is None:
			return True
		return label in interval.candidates

	def reportAmbiguousLabels(self, interval, candidates):
		if len(candidates) < 6:
			return ", ".join(map(str, candidates))

		lowerBounds = None
		upperBounds = None

		if interval.upperCone is not None:
			upperBounds = self._order.maxima(candidates)
		if interval.lowerCone is not None:
			lowerBounds = self._order.minima(candidates)

		if upperBounds is None and lowerBounds is None:
			return "<all labels>"

		if upperBounds == lowerBounds:
			names = list(map(str, lowerBounds))
			return f"candidates {names}"

		msgs = []
		if lowerBounds:
			names = list(map(str, lowerBounds))
			msgs.append(f"lower bounds {names}")
		if upperBounds:
			names = list(map(str, upperBounds))
			msgs.append(f"upper bounds {names}")
		return '; '.join(msgs)

	def chooseLabelForInterval(self, interval, label):
		for pkg in interval.packages:
			self.chooseLabelForPackage(pkg, label)

	def chooseLabelForPackage(self, pkg, label):
		if pkg.label is None:
			choice = label
		elif pkg.label.type == Classification.TYPE_AUTOFLAVOR:
			# All candidates have been labelled as either @Core or @Core+something
			# The package has been labelled with an autoflavor like "doc"
			# Put it into @Core+doc

			if label.flavorBase:
				# cheat a little here. The chosen label we've been given is
				# actually a build flavor (eg @Core+qt), and our package has been labelled with
				# a (possibly different) auto flavor like "doc".
				# Move up to the base flavor; because there's no flavor @Core+qt+doc.
				label = label.flavorBase

			choice = label.getBuildFlavor(pkg.label.name)
		else:
			# shouldn't happen
			choice = None
		return choice

	def commonSiblingBaseLabel(self, interval):
		if interval.siblings is None:
			return None

		result = None
		for sib in interval.siblings:
			sibInterval = self.getPackage(sib)
			if sibInterval is None:
				continue

			baseLabel = sibInterval.solutionBaseLabel
			if baseLabel:
				if result is None:
					result = baseLabel
				elif result is not baseLabel:
					return None
		return result

	def tryToPlaceWithSibling(self, interval):
		pkg = interval.package

		if interval.siblings is None:
			return

		if pkg.label:
			assert(pkg.label.type == Classification.TYPE_AUTOFLAVOR)
			select = lambda label: label.flavorBase or label
		else:
			select = lambda label: label

		# loop over the package and all of its siblings
		# All packages resulting from the same OBS build are considered siblings
		labelledSiblings = []
		unlabelledSiblings = []

		for sib in interval.siblings:
			sibInterval = self.getPackage(sib)
			if sibInterval is None:
				print(f"Trying to place {pkg} but cannot find its sibling {sib}")
				continue
			if sibInterval.solution:
				labelledSiblings.append((sibInterval, sibInterval.solutionBaseLabel))
			elif sibInterval.candidates is not None:
				baseLabels = self.baseLabelsForSet(sibInterval.candidates)
				unlabelledSiblings.append((sibInterval, baseLabels))
			else:
				# candidates being None indicates no restrictions on the sibling
				pass

		# For all the labelled siblings, collect their base label (ie @Foo+something -> @Foo)
		sibLabels = set(label for interval, label in labelledSiblings)

		# For all the unlabelled siblings, intersect the candidate sets
		sibLabelScope = reduce(intersectSets, (baseLabels for interval, baseLabels in unlabelledSiblings), None)

		# First priority: try to select a (base) label from siblings that have already been labelled.
		# Second priority: if no siblings have been labelled, 
		if not sibLabels:
			if not sibLabelScope:
				print(f"{interval} cannot be placed with siblings (no common base labels found)")
				return
			sibLabels = sibLabelScope
		elif sibLabelScope is not None:
			if not sibLabels.issubset(sibLabelScope):
				print(f"{interval}: some siblings have been labelled already, but their labels conflict with other siblings")

				for interval, label in labelledSiblings:
					for unlabelledInterval, candidates in unlabelledSiblings:
						if label not in candidates:
							names = ' '.join(map(str, candidates))
							print(f"   {interval} has been labelled {interval.solution}, but is not in scope for sibling {unlabelledInterval}; candidates = {names}")

				return

		if len(sibLabels) == 1:
			label = next(iter(sibLabels))

			choice = self.chooseLabelForPackage(pkg, label)
			if choice:
				names = ' '.join(str(interval) for interval, label in labelledSiblings)
				print(f"{interval} is being placed into {choice} because its siblings {names} were placed into {label}")
				interval.solution = choice
				return True
		else:
			msg = self.reportAmbiguousLabels(interval, sibLabels)
			print(f"{interval}: ambiguous choice of sibling labels: {msg}")

	# Given a set of candidates like this:
	#  @CoreLibraries+hpc, @HPC+accounts, @HPC, @HPC+devel, @HPC+python, @HPC+x86-64-v3, @HPC+doc, @HPC+i18n
	# pick @HPC
	#
	# Do this by looking at the base flavors of all labels and finding the single one that
	# is "below" all these candidate labels. In the example above, this would be @HPC because
	#  - @HPC is always a requirement for @HPC+something
	#  - @CoreLibraries+hpc is a build flavor that augments or at least requires @HPC
	def tryToPlaceIntoCommonBase(self, interval):
		def isCommonBaseFlavor(base, candidates):
			for label in candidates:
				if base == label or label.flavorBase == base:
					continue

				if base in label.runtimeRequires:
					continue

				return False
			return True

		baseFlavors = set()
		for label in interval.candidates:
			if label.flavorBase is not None:
				label = label.flavorBase
			baseFlavors.add(label)

		if len(baseFlavors) == 0:
			return

		if len(baseFlavors) == 1:
			best = baseFlavors.pop()
		else:
			best = None
			for base in baseFlavors:
				if isCommonBaseFlavor(base, interval.candidates):
					if best:
						print(f"{interval} has at least two \"common\" base flavors - {best} and {base}")
						return
					best = base

		if best:
			choice = self.chooseLabelForPackage(interval.package, best)
			if choice:
				print(f"{interval} is being placed into {choice} because {best} is the common base flavor of all candidates")
				interval.solution = choice
				return True

		return False

	# There may be packages (or more often, clusters of packages) that belong together
	# but cannot be labeled because we haven't labeled any of them yet. Report these
	# together
	def reportUnplaceablePackages(self, order):
		found = set()
		for interval in order.topDownTraversal():
			# don't do anything if already solved
			if interval.solution:
				continue

			if interval.candidates is not None:
				continue

			if interval.package:
				assert(interval is self.getPackage(interval.package))
			if interval.package and interval.package.label:
				label = interval.package.label
				if label.type == Classification.TYPE_AUTOFLAVOR:
					pass
				# print(f"{interval} not completely unplaceable (labelled as {label})")
				continue

			found.add(interval)

		remaining = found.copy()
		while remaining:
			cluster = set()
			pivot = remaining.pop()

			queue = [pivot]
			while queue:
				interval = queue.pop(0)
				if interval in cluster:
					continue
				if interval not in found:
					continue

				cluster.add(interval)

				# inspect all upper and lower neighbors of this package
				# Not all of these will automatically have NULL candidates, too.
				queue += list(interval._lowerNeighbors)
				queue += list(interval._upperNeighbors)

			if len(cluster) > 1:
				print("Found a cluster of packages that should probably be labelled together:")
				for i in sorted(cluster, key = str):
					print(f"  {i}")
			else:
				# print(f"{pivot} can be placed anywhere")
				pass

			remaining.difference_update(cluster)

	def renderCandidates(self, order, interval):
		if not interval.lowerCone:
			if not interval.upperCone:
				return "[ALL LABELS]"


	def __iter__(self):
		for pkg, interval in self._packages.items():
			yield pkg, self.getBestCandidate(interval)

	def getBestCandidate(self, interval):
		candidates = interval.candidates
		if not candidates:
			return None

		return self._order.minimumOf(candidates)

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

		self.matchCount = 0
		self.expand = True
		self.label = None
		self.description = None
		self._packages = []
		self._buildFlavors = {}
		self._closure = set()

	def track(self, pkg):
		self._packages.append(pkg)
		self.matchCount += 1

		if pkg.label is None:
			pkg.label = self.label
		elif pkg.label is self.label:
			pass
		else:
			raise Exception(f"Package {pkg.fullname()} cannot change label from {pkg.label} to {self.label}")

		self._closure.add(pkg)

	@property
	def type(self):
		return self.label.type

	@property
	def packages(self):
		return set(self._packages)

	@property
	def closure(self):
		return set(self._closure)

	@property
	def packageNames(self):
		return set(_.name for _ in self._packages)

	@property
	def groupNames(self):
		return set(_.group for _ in self._packages)

	@property
	def runtimeRequires(self):
		raise Exception("obsolete method called")

	@property
	def buildRequires(self):
		raise Exception("obsolete method called")

	@property
	def isFlavor(self):
		return self.label.flavorBase is not None

	@property
	def defined(self):
		return self.label and self.label.defined

	@defined.setter
	def defined(self, value):
		self.label.defined = value

	def addRequires(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addRuntimeDependency(otherGroup.label)

	def addAugmentation(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addRuntimeAugmentation(otherGroup.label)

	def addBuildRequires(self, otherGroup):
		if otherGroup.label is None:
			raise Exception(f"Group {otherGroup.name} has a NULL label")
		self.label.addBuildDependency(otherGroup.label)

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

	# Having classified a set of packages, we add it to the group's closure
	def update(self, packages):
		self._closure.update(packages)

class GlobMatch:
	PRIORITY_DEFAULT = 5

	def __init__(self, value, group, priority = PRIORITY_DEFAULT):
		self.value = value
		self.group = group
		self.priority = priority

	def __str__(self):
		if self.priority == self.PRIORITY_DEFAULT:
			return self.value
		return f"{self.value} (priority {self.priority})"

	@property
	def key(self):
		return (self.priority, -len(self.value), self.value)

	def match(self, name):
		return fnmatch.fnmatchcase(name, self.value)


class FilterSetBuilder(object):
	def __init__(self, filterSet, group, priority = None):
		self.filterSet = filterSet
		self.group = group
		self.priority = priority

	def addProductFilter(self, name):
		self.addMatch(self.filterSet.productFilters, name)

	def addBinaryPackageFilter(self, name):
		self.addMatch(self.filterSet.binaryPkgFilters, name)

	def addSourcePackageFilter(self, name):
		self.addMatch(self.filterSet.sourcePkgFilters, name)

	def addRpmGroupFilter(self, name):
		self.addMatch(self.filterSet.rpmGroupFilters, name)

	def addMatch(self, filterSet, value):
		group = self.group
		priority = self.priority

		# A match may come with additional parameters, as in 
		#
		#	postgresql-* priority=8
		#
		if ' ' in value:
			words = value.split()
			value = words[0]
			for param in words[1:]:
				(argName, argValue) = param.split('=')
				if argName == 'priority':
					priority = int(argValue)
				else:
					raise Exception(f"Unknown match parameter {param} in {self.filterSet} expression \"{value}\" for group {group.name}");

		if priority is None:
			priority = GlobMatch.PRIORITY_DEFAULT

		filterSet.addMatch(value, group, priority)

class FilterType(object):
	def __init__(self, type):
		self.type = type
		self._exactMatches = {}
		self._globMatches = []

	def __str__(self):
		return f"{self.type} filter"

	def addMatch(self, value, group, priority):
		if '*' in value or '?' in value:
			self._globMatches.append(GlobMatch(value, group, priority))
		else:
			if self._exactMatches.get(value):
				conflict = self._exactMatches[value]
				print(f"OOPS: {self.type} filter is ambiguous for {value} ({group.name} vs {conflict.name})")
				return
			# NB: silently ignore any priority value for exact match
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
				return PackageFilter.Verdict(glob.group, f"{self.type} filter {glob}")

		return None

	def applyName(self, name):
		verdict = self.tryFastMatch(name)
		if verdict is None:
			verdict = self.trySlowNameMatch(name)
		return verdict

class RpmGroupFilters(FilterType):
	def __init__(self):
		super().__init__('rpmgroup')

	def apply(self, pkg, product):
		return self.applyName(pkg.group)

class ProductFilters(FilterType):
	def __init__(self):
		super().__init__('product')

	def apply(self, pkg, product):
		if product is None:
			return None
		return self.applyName(product.name)

class PackageFilters(FilterType):
	def __init__(self):
		super().__init__('package')

	def apply(self, pkg, product):
		return self.applyName(pkg.name)

class SourcePackageFilters(FilterType):
	def __init__(self):
		super().__init__('source package')

	def apply(self, pkg, product):
		src = pkg.sourcePackage
		if src is None:
			return None
		return self.applyName(src.name)

class PackageFilterSet:
	def __init__(self):
		self._productFilters = None
		self._binaryPkgFilters = None
		self._sourcePkgFilters = None
		self._rpmGroupFilters = None
		self._applicableFilters = None

	@property
	def productFilters(self):
		if not self._productFilters:
			self._productFilters = ProductFilters()
		return self._productFilters

	@property
	def binaryPkgFilters(self):
		if not self._binaryPkgFilters:
			self._binaryPkgFilters = PackageFilters()
		return self._binaryPkgFilters

	@property
	def sourcePkgFilters(self):
		if not self._sourcePkgFilters:
			self._sourcePkgFilters = SourcePackageFilters()
		return self._sourcePkgFilters

	@property
	def rpmGroupFilters(self):
		if not self._rpmGroupFilters:
			self._rpmGroupFilters = RpmGroupFilters()
		return self._rpmGroupFilters

	def finalize(self):
		self._applicableFilters = []
		for filterSet in (self._productFilters, self._binaryPkgFilters, self._sourcePkgFilters, self._rpmGroupFilters):
			if filterSet is not None:
				filterSet.finalize()
				self._applicableFilters.append(filterSet)

	def apply(self, pkg, product):
		for filterSet in self._applicableFilters:
			verdict = filterSet.apply(pkg, product)
			if verdict is not None:
				break
		return verdict

class PackageFilter:
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
		self._groups = {}
		self._preferences = PackagePreferences()
		self._autoflavors = []

		self.filterSet = PackageFilterSet()

		with open(filename) as f:
			data = yaml.full_load(f)

		# Parse autoflavors *before* everything else so that we can populate
		# newly created flavors from default settings.
		for gd in data.get('autoflavors') or []:
			group = self.parseGroup(Classification.TYPE_AUTOFLAVOR, gd)
			self._autoflavors.append(group)

		for gd in data.get('build_configs') or []:
			self.parseGroup(Classification.TYPE_BUILDCONFIG, gd)

		for gd in data.get('buildconfig_flavors') or []:
			self.parseGroup(Classification.TYPE_BUILDCONFIG_FLAVOR, gd)

		for gd in data.get('build_groups') or []:
			self.parseGroup(Classification.TYPE_SOURCE, gd)

		for gd in data['groups']:
			self.parseGroup(Classification.TYPE_BINARY, gd)

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
		def validateDependencies(dependencies):
			for req in dependencies:
				chase = req
				while True:
					other = self.getGroup(chase.name, chase.type)
					if other is None:
						raise Exception(f"could not find {chase.type} group {chase.name}")
					if not other.label.flavorBase:
						break
					chase = other.label.flavorBase

				if not other.defined:
					raise Exception(f"filter configuration issue: group {group.label} requires {other.label}, which is not defined anywhere")

		self.filterSet.finalize()

		for group in self._groups.values():
			label = group.label
			validateDependencies(label.buildRequires)

		for group in list(self._groups.values()):
			if group.label.flavorBase is not None:
				continue

			if group.label.type is not Classification.TYPE_BINARY:
				continue

			for autoFlavor in self.autoFlavors:
				if autoFlavor.label.disposition != Classification.DISPOSITION_SEPARATE:
					continue

				flavor = self.instantiateAutoFlavor(group, autoFlavor)

				# If we instantiated Foo+devel, automatically make Foo buildrequire Foo+devel
				if autoFlavor.name == "devel":
					group.addBuildRequires(flavor)

					if group.label.buildConfig:
						group.label.buildConfig.addBuildDependency(flavor.label)

		# Loop over all @Foo labels and look for auto flavors with dispotion maybe_merge, such as python
		# If @Foo requires everything that the auto flavor requires (in the case of python, this would
		# be @PythonCore), then mark the auto flavor for merging. Otherwise, create a separate
		# build flavor @Foo+python
		preliminaryOrder = self.classificationScheme.createOrdering(Classification.TYPE_BINARY)
		for group in list(self._groups.values()):
			if group.label.flavorBase is not None:
				continue

			if group.label.type is not Classification.TYPE_BINARY:
				continue

			baseLabel = group.label

			# get the closure of all requirements of @Foo
			baseDependencies = preliminaryOrder.downwardClosureFor(group.label)

			for autoFlavor in self.autoFlavors:
				if autoFlavor.label.disposition != Classification.DISPOSITION_MAYBE_MERGE:
					continue

				if autoFlavor.label.runtimeRequires.issubset(baseDependencies):
					flavor = baseLabel.getBuildFlavor(autoFlavor.name)
					if flavor is not None:
						print(f"{baseLabel}+{autoFlavor.label} packages could be merged into {baseLabel}, but {flavor} exists")
					else:
						print(f"{baseLabel}+{autoFlavor.label} packages will be merged into {baseLabel}")
						baseLabel.addMergeableFlavor(autoFlavor.label)
				else:
					self.instantiateAutoFlavor(group, autoFlavor)

		# if @Foo requires @Bar+something, then
		# @Foo+devel should require @Bar+devel
		for group in list(self._groups.values()):
			if group.type != Classification.TYPE_BINARY:
				continue

			baseLabel = group.label
			if baseLabel.flavorBase is not None:
				continue

			flavor = baseLabel.getBuildFlavor('devel')
			if flavor is None:
				print(f"Warning: {baseLabel} has no devel flavor")
				continue

			for baseReq in baseLabel.runtimeRequires:
				if baseReq.flavorBase:
					baseReq = baseReq.flavorBase
				develReq = baseReq.getBuildFlavor("devel")
				assert(develReq)
				if flavor in develReq.runtimeRequires:
					print(f"warning: {flavor} is already a runtime requirement of {develReq}")
					continue
				flavor.addRuntimeDependency(develReq)


		hpc = self.getGroup("@HPC", Classification.TYPE_BINARY)
		assert(hpc.getBuildFlavor("doc"))

	def apply(self, pkg, product):
		return self.filterSet.apply(pkg, product)

	def makeGroup(self, name, type = None):
		assert(type)
		return self.makeGroupInternal(name, type)

	def resolveGroupReference(self, name, type = None):
		if '+' not in name:
			return self.makeGroupInternal(name, type)

		words = name.split('+')
		group = self.makeGroupInternal(words.pop(0), type)

		for flavorName in words:
			group = self.makeFlavorGroup(group, flavorName)
		return group

	def resolveBuildReference(self, name, type = None):
		if '/' not in name:
			sourceProjectName = name
			flavorName = 'standard'
		else:
			(sourceProjectName, flavorName) = name.split('/')

		group = self.makeGroupInternal(sourceProjectName, Classification.TYPE_SOURCE)

		return self.makeFlavorGroup(group, flavorName, Classification.TYPE_BUILDCONFIG)

	def makeSourceGroup(self, name):
		return self.makeGroupInternal(name, Classification.TYPE_SOURCE)

	def makeBinaryGroup(self, name):
		return self.makeGroupInternal(name, Classification.TYPE_BINARY)

	def instantiateAutoFlavor(self, baseGroup, autoFlavor):
		# groupName = f"{baseGroup.name}+{autoFlavor.name}"
		flavor = baseGroup.getBuildFlavor(autoFlavor.name)
		if flavor is None:
			flavor = self.makeFlavorGroup(baseGroup, autoFlavor.name)

		flavor.label.copyRequirementsFrom(autoFlavor.label)
		return flavor

	def instantiateBuildConfigFlavor(self, sourceProject, name):
		if sourceProject is None:
			return None

		flavor = sourceProject.getBuildFlavor(name)
		if flavor is None:
			tmpl = self.getGroup(name, Classification.TYPE_BUILDCONFIG_FLAVOR)
			if tmpl is not None:
				sourceGroup = self.makeGroupInternal(sourceProject.name, Classification.TYPE_SOURCE);

				flavor = self.instantiateAutoFlavor(sourceGroup, tmpl)
				flavor.label.copyRequirementsFrom(tmpl.label)

		return flavor

	def makeFlavorGroup(self, baseGroup, flavorName, type = None):
		flavor = baseGroup.getBuildFlavor(flavorName)
		if flavor is not None:
			return flavor

		if type is None:
			if baseGroup.type == Classification.TYPE_SOURCE:
				type = Classification.TYPE_BUILDCONFIG
			else:
				type = baseGroup.type
		assert(type in (Classification.TYPE_BUILDCONFIG, Classification.TYPE_BINARY))

		if type == Classification.TYPE_BINARY:
			flavor = self.createBinaryFlavor(baseGroup, flavorName)
		elif type == Classification.TYPE_BUILDCONFIG:
			flavor = self.createBuildConfigFlavor(baseGroup, flavorName)
		else:
			raise Exception(f"Don't know how to create a {type} flavor for {baseGroup.label}+{flavorName}")

		baseGroup.addBuildFlavor(flavorName, flavor)
		return flavor

	def createBinaryFlavor(self, baseGroup, flavorName):
		groupName = f"{baseGroup.name}+{flavorName}"

		flavor = self.makeGroupInternal(groupName, Classification.TYPE_BINARY)
		flavor.label.copyRequirementsFrom(baseGroup.label)

		# Flavors are built from the same source project as their base group
		flavor.label.sourceProject = baseGroup.label.sourceProject

		# When creating @Foo+blah, and @Foo has a sourceProject of FooSource, check
		# whether there's a buildconfig for FooSource/blah
		buildLabel = self.getBuildConfigFlavor(baseGroup.label.sourceProject, flavorName)
		if buildLabel is None:
			buildLabel = baseGroup.label.buildConfig

		if buildLabel:
			flavor.label.setBuildConfig(buildLabel)

		# @Foo+blah always requires @Foo for runtime
		flavor.label.addRuntimeDependency(baseGroup.label)

		for flavorDef in self._autoflavors:
			if flavorDef.name == flavorName:
				# If there is a default flavor definition, copy its autoselect
				# setting.
				flavor.label.autoSelect = flavorDef.label.autoSelect

		return flavor

	def createBuildConfigFlavor(self, baseGroup, flavorName):
		groupName = f"{baseGroup.name}/{flavorName}"

		flavor = self.makeGroupInternal(groupName, Classification.TYPE_BUILDCONFIG)
		flavor.label.copyRequirementsFrom(baseGroup.label)

		# the source project for FooSource/blah is FooSource
		flavor.label.sourceProject = baseGroup.label

		# For the time being, make all buildconfigs auto-selectable.
		# Probably a useless gesture.
		flavor.autoSelect = True

		return flavor

	def getBuildConfigFlavor(self, sourceProject, flavorName):
		if sourceProject is None:
			return None
		assert(isinstance(sourceProject, Classification.Label))

		buildLabel = sourceProject.getBuildFlavor(flavorName)
		if buildLabel is None:
			buildGroup = self.instantiateBuildConfigFlavor(sourceProject, flavorName)
			if buildGroup is not None:
				buildLabel = buildGroup.label

		if buildLabel is not None:
			if False:
				print(f": {sourceProject} has flavor {buildLabel} type {buildLabel.type}")
				for req in buildLabel.runtimeRequires:
					print(f"  {buildLabel} -> {req}")

			# pretend that it's defined
			buildLabel.defined = True

		return buildLabel

	def getGroup(self, name, type = None):
		try:
			group = self._groups[type, name]
		except:
			return None

		if type and group.type != type:
			raise Exception(f"Group {name} does not match expected type (has {group.type}; expected {type})")

		return group

	def makeGroupInternal(self, name, type):
		try:
			group = self._groups[type, name]
		except:
			group = None

		if group is None:
			if not type:
				raise Exception(f"Cannot create group {name} with no type")

			group = PackageGroup(name)
			self._groups[type, name] = group

		if type:
			if group.label is None:
				group.label = self.classificationScheme.createLabel(name, type)
			elif group.type != type:
				raise Exception(f"Group {name} does not match expected type ({group.type} vs {type})")

		return group

	@property
	def groups(self):
		return sorted(self._groups.values(), key = lambda grp: grp.matchCount)

	def updateGroup(self, label, packages):
		group = self.getGroup(label.name, label.type)
		assert(group.label is label)

		group.update(packages)

	def getGroupPackages(self, label):
		group = self.getGroup(label.name, label.type)
		assert(group.label is label)

		return group.closure

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
		'augments',
		'products',
		'packages',
		'sources',
		'binaries',
		'rpmGroups',
		'buildflavors',
		'sourceproject',
		'buildconfig',
		'disposition',
		'autoselect',
	))

	def processGroupDefinition(self, group, gd):
		def getBoolean(gd, tag):
			value = gd.get(tag)
			if value is not None and type(value) is not bool:
				raise Exception(f"{group.label}: bad value {tag}={value} (expected boolean value not {type(value)})")
			return value

		if group.defined:
			raise Exception(f"Duplicate definition of group \"{group.name}\" in filter yaml")
		group.defined = True

		for field in gd.keys():
			if field not in self.VALID_GROUP_FIELDS:
				raise Exception(f"Invalid field {field} in definition of group {group.name}")

		group.description = gd.get('description')

		name = gd.get('sourceproject')
		if name is not None:
			sourceProject = self.makeGroupInternal(name, Classification.TYPE_SOURCE)
			group.label.setSourceProject(sourceProject.label)

		name = gd.get('buildconfig')
		if name is not None:
			buildConfig = self.resolveBuildReference(name)
			group.label.setBuildConfig(buildConfig.label)

		value = gd.get('expand')
		if type(value) == bool:
			group.expand = value

		value = gd.get('disposition')
		if value is not None:
			if value not in ('separate', 'merge', 'ignore', 'maybe_merge') or group.type != Classification.TYPE_AUTOFLAVOR:
				raise Exception(f"Invalid disposition={value} in definition of {group.type} group {group.name}")
			group.label.disposition = value

		value = getBoolean(gd, 'autoselect')
		if value is not None:
			group.label.autoSelect = value

		priority = gd.get('priority')
		if priority is not None:
			assert(type(priority) == int)

		if group.label:
			nameList = gd.get('requires') or []
			for name in nameList:
				otherGroup = self.resolveGroupReference(name, Classification.TYPE_BINARY)
				group.addRequires(otherGroup)

			# 'augments' are like runtime requirements, except they also flags the
			# group/flavor as an augmentation. Augmentations will never auto-select any
			# flavors that require any of the labels that are being augmented.
			#
			# Example:
			#  @Python+gnome contains a couple of python modules that are intended for use
			#	with gnome based frameworks. It requires @Gnome
			#  @Gnome+python contains python utilities that need these modules, and requires @Python
			#
			# If we work purely with requirements, we will end up with @Python+gnome
			# auto-selecting @Gnome+python.
			# By having @Python+gnome augment rather than require @Gnome, we end up with
			# @Gnome+python auto-selecting @Python+gnome rather than the other way around
			nameList = gd.get('augments') or []
			for name in nameList:
				otherGroup = self.resolveGroupReference(name, Classification.TYPE_BINARY)
				group.addAugmentation(otherGroup)

			nameList = gd.get('buildrequires') or []
			for name in nameList:
				otherGroup = self.resolveGroupReference(name, Classification.TYPE_BINARY)
				group.addBuildRequires(otherGroup)

		# The yaml file may specify per-group priorities for filters, but there is just
		# one global set of filters. Rather than passing the group and priority argument
		# into each add*Filter function, create a Builder object that does this transparently.
		filterSetBuilder = FilterSetBuilder(self.filterSet, group, priority)

		nameList = gd.get('products') or []
		for name in nameList:
			filterSetBuilder.addProductFilter(name)

		nameList = gd.get('packages') or []
		for name in nameList:
			filterSetBuilder.addBinaryPackageFilter(name)
			filterSetBuilder.addSourcePackageFilter(name)

		nameList = gd.get('sources') or []
		for name in nameList:
			filterSetBuilder.addSourcePackageFilter(name)

		nameList = gd.get('binaries') or []
		for name in nameList:
			filterSetBuilder.addBinaryPackageFilter(name)

		nameList = gd.get('rpmGroups') or []
		for name in nameList:
			filterSetBuilder.addRpmGroupFilter(name)

		flavors = gd.get('buildflavors') or []
		for fd in flavors:
			self.parseBuildFlavor(group, fd)

		return group
