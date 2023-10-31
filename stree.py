##################################################################
#
# SolvingTree
#
# This converts the graph of packages and their dependencies as
# a cycle-free, partially ordered set of nodes. Dependencies get
# collapsed into one single node.
#
# It then computes, for each node, a convex set of labels that
# are valid solutions for the package (or cycle of packages) that
# this node represents.
#
# This tree can then be used by a solving algorithm to find a
# (hopefully) good solution to the labelling problem.
#
##################################################################

from util import ExecTimer
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from ordered import PartialOrder
from functools import reduce

# hack until I'm packaging fastsets properly
import fastsets.fastsets as fastsets

Classification = None

cycleLogger = loggingFacade.getLogger('cycles')
debugPackageCycles = cycleLogger.debug

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

def renderLabelSet(name, labels):
	if labels is None:
		return "[unconstrained]"

	if not labels:
		return f"[no {name}]"

	if len(labels) >= 6:
		return f"[{len(labels)} {name}]"

	return f"[{name} {' '.join(map(str, labels))}]";

def displayLabelSetFull(candidates, indent = ""):
	def renderPurposes(label, labelIdent):
		purposeSet = set(label.objectPurposes)
		if purposeSet.issubset(candidates):
			return [f"{labelIdent}-*"]

		purposeSet.intersection_update(candidates)
		return sorted(f"{labelIdent}-{purpose.purposeName}" for purpose in purposeSet)

	if candidates is None:
		infomsg(f"{indent}ALL")
		return

	baseLabels = set(label.baseLabel for label in candidates)
	for baseLabel in sorted(baseLabels, key = str):
		subs = []
		if baseLabel in candidates:
			subs.append('.')

		for flavor in baseLabel.flavors:
			if flavor in candidates:
				subs.append(f"+{flavor.flavorName}")
			subs += renderPurposes(flavor, f"+{flavor.flavorName}")

		subs += renderPurposes(baseLabel, f"")

		if subs:
			subs = ' '.join(subs)
			infomsg(f"{indent}{baseLabel}: {subs}")
		else:
			infomsg(f"{indent}{baseLabel}")

class SolvingTree(object):
	domain = fastsets.Domain("nodes")

	# Not really an interval but a convex set
	class LabelInterval(domain.member):
		def __init__(self, order, name, package = None, cycle = None):
			assert(package or cycle)

			super().__init__()

			self.name = name
			self.package = package
			self.siblings = None
			self._cycle = cycle
			self._order = order
			self._lowerNeighbors = SolvingTree.createNodeSet()
			self._upperNeighbors = SolvingTree.createNodeSet()
#			self.upperBounds = set()
#			self.lowerBounds = set()
			self._lowerCone = None
			self._upperCone = None
			self._candidates = None
			self._solution = None
			self._combinedRequirements = set()

			self.placement = None

			self._trace = False

		def zap(self):
			self._lowerNeighbors = None
			self._upperNeighbors = None

		def __str__(self):
			return self.name

		@property
		def nameWithLabelReason(self):
			for pkg in self.packages:
				if pkg.label != self._solution:
					# conflict, but hush! let's not talk about it here
					continue

				if pkg.labelReason:
					return str(pkg.labelReason)

			return str(self)

		@property
		def solution(self):
			return self._solution

		@solution.setter
		def solution(self, label):
			if self._solution and self._solution is not label:
				raise Exception(f"Conflicting solution for {self}: label {self._solution} vs {label}")
			if self._trace:
				infomsg(f" {self} set solution to {label}")
			self._solution = label

			# update lower and upper cone here?

		@property
		def solutionBaseLabel(self):
			if not self._solution:
				return None
			return self._solution.parent or self._solution

		@property
		def packages(self):
			if self.package:
				return set([self.package])
			return self._cycle

		@property
		def isCollapsedCycle(self):
			return bool(self._cycle)

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
				infomsg(f" {self}: add lower neighbor {lowerNeighbor}; lower cone:")
				displayLabelSetFull(self.lowerCone, indent = "   ")

		def updateFromAbove(self, upperNeighbor):
			# do NOT use update_intersection
			self._upperCone = self.intersectCones(self.upperCone, upperNeighbor.upperCone)

			if self._trace:
				infomsg(f" {self}: add upper neighbor {upperNeighbor}; upper cone:")
				displayLabelSetFull(self.upperCone, indent = "   ")

		def filterCandidateLabels(self, candidateLabels, quiet = False):
			candidateLabels = intersectSets(candidateLabels, self.lowerCone)
			if candidateLabels and len(candidateLabels) > 1:
				# Inspect the priority values of all labels involved and return
				# the ones that have a higher gravity
				filtered = Classification.filterLabelsByGravity(candidateLabels)
				if filtered != candidateLabels:
					if not quiet:
						infomsg(f"{self}: reduced set of candidates based on their gravity" + 
							f"  {' '.join(map(str, candidateLabels))} -> {' '.join(map(str, filtered))}")
					candidateLabels = filtered

				# disambiguate labels
				# a) If we have Foo-devel, Foo+python-devel, Foo+somethingelse-devel, we want to
				#    return Foo-devel
				if len(candidateLabels) > 1:
					baseLabels = Classification.createLabelSet()
					for label in candidateLabels:
						# For @Foo+flavor-purpose, look up @Foo-purpose
						label = label.findSibling(None, label.purposeName)
						if label is None:
							return candidateLabels
						baseLabels.add(label)

					if len(baseLabels) == 1:
						return baseLabels

				# b) If we have a single maximum in the subset, use that.
				#    This takes care of cases where we place libfoo in @BlahLibraries
				#    and foo-tools in @BlahUtils. In such a case, we put foo-devel
				#    into the (higher) @BlahUtils-devel bucket.
				if len(candidateLabels) > 1:
					candidateLabels = self._order.maxima(candidateLabels)
			return candidateLabels

		def constrainCandidatesFurther(self, permittedLabels):
			if permittedLabels is None:
				# no further constraints
				return

			if self._trace:
				before = renderLabelSet("candidates", self.candidates)

			self._candidates = intersectSets(self.candidates, permittedLabels)

			if self._trace:
				after = renderLabelSet("candidates", self.candidates)
				infomsg(f" {self}: constrained candidates from {before} to {after}")
				cons = renderLabelSet("permitted labels", permittedLabels);
				infomsg(f"  constraints: {cons}")

		@property
		def candidates(self):
			if self._candidates is None:
				self._candidates = self.intersectCones(self.lowerCone, self.upperCone)
			return self._candidates

		@property
		def candidateProjects(self):
			candidates = self.candidates
			if not candidates:
				return candidates

			projects = set()
			for label in candidates:
				if label.buildConfig:
					label = label.buildConfig
					if label.sourceProject:
						projects.add(label.sourceProject)
			return projects

		def anyPackageHasLabel(self):
			for p in self.packages:
				if p.label:
					return True
			return False

		def hasPurposeLabel(self):
			for p in self.packages:
				if p.label and p.label.isPurpose:
					return True
			return False

		def labelIsValidCandidate(self, label):
			cand = self.candidates
			if cand is None:
				return True
			return label in cand

		@property
		def allPackageLabels(self):
			result = []
			for p in self.packages:
				if p.label:
					result.append(p.label)
			return result

		@property
		def commonLabel(self):
			allLabels = self.allPackageLabels
			if len(allLabels) == 1:
				return allLabels[0]
			return None

		def recordDecision(self, label, reason = None):
			if self.solution is label:
				return

			assert(self.solution is None)
			self.solution = label

			if self.siblings is not None:
				self.siblings.recordDecision(node, label)

	class Traversal:
		class Cursor:
			def __init__(self, queue, node, depth):
				self.queue = queue
				self.node = node
				self.depth = depth

			def descend(self):
				entries = []
				for neigh in self.node.lowerNeighbors:
					entries.append(self.__class__(self.queue, neigh, self.depth + 1))
				self.queue[:0] = entries
				return True

			def __str__(self):
				result = (self.depth * "   ") + f" - {self.node}"
				if self.node.solution:
					result += f" [{self.node.solution}]"
				return result

		def __init__(self, node):
			self.queue = []
			self.seen = set()
			self.Cursor(self.queue, node, 0).descend()

		def __iter__(self):
			while self.queue:
				next = self.queue.pop(0)
				if next.node not in self.seen:
					self.seen.add(next.node)
					yield next

	class SiblingInfo:
		def __init__(self, build):
			self.name = build.name
			self.packages = []
			self.sources = []

			self.labels = Classification.createLabelSet()
			for rpm in build.binaries:
				if rpm.isSourcePackage:
					self.sources.append(rpm)
				else:
					self.packages.append(rpm)

					label = rpm.label
					if label and label.type == Classification.TYPE_BINARY:
						self.labels.add(label)

		def __str__(self):
			return self.name

		def __iter__(self):
			return iter(self.packages)

		def __len__(self):
			return len(self.packages)

		def recordDecision(self, node, label):
			self.labels.add(label)

		@property
		def sourceLabels(self):
			result = Classification.createLabelSet()
			for pkg in self.sources:
				if pkg.label:
					result.add(pkg.label)
			return result

		@property
		def baseLabels(self):
			if len(self.labels) > 20:
				result = Classification.createLabelSet(map(lambda label: label.parent or label, self.labels))
				result = Classification.createLabelSet(map(lambda label: label.parent or label, result))
				return result

			result = Classification.createLabelSet()
			for label in self.labels:
				while label.parent:
					label = label.parent
				result.add(label)

			return result

		@property
		def commonBaseLabel(self):
			labels = self.baseLabels
			if len(labels) == 1:
				return next(iter(labels))
			return None

		@property
		def commonLabel(self):
			if len(self.labels) == 1:
				return next(iter(self.labels))
			return None

		@property
		def preferredBaseLabel(self):
			return self.commonBaseLabel

		@property
		def preferredLabel(self):
			label = self.commonLabel
			if label is None:
				label = self.commonBaseLabel
			return label

		@property
		def allLabels(self):
			return self.labels

		@property
		def allBaseLabels(self):
			return self.baseLabels

	def __init__(self, classificationScheme, order = None):
		self._classificationScheme = classificationScheme

		if order is None:
			order = classificationScheme.createOrdering(Classification.TYPE_BINARY)
		self._order = order

		self._packages = {}
		self._builds = {}
		self._nodeOrder = None

	@classmethod
	def createNodeSet(klass, initialValues = None):
		return klass.domain.set(initialValues)

	def addEdge(self, requiringPackage, requiredPackage):
		# Do not add packages that are labeled with disposition ignored
		if requiringPackage.label and requiringPackage.label.disposition == Classification.DISPOSITION_IGNORE:
			return

		# A package that is not ignored MUST NOT require an ignored package
		if requiredPackage.label and requiredPackage.label.disposition == Classification.DISPOSITION_IGNORE:
			raise Exception(f"{requiringPackage} requires {requiredPackage} [requiredPackage.label] with disposition 'ignore'")

		assert(requiringPackage is not requiredPackage)
		upper = self.getPackage(requiringPackage)
		lower = self.getPackage(requiredPackage)
		upper.addLowerNeighbor(lower)
		lower.addUpperNeighbor(upper)

	def addBuild(self, build):
		if build in self._builds:
			return

		siblings = self.SiblingInfo(build)
		self._builds[build] = siblings
		for pkg in siblings.packages:
			self.getPackage(pkg).siblings = siblings

	def setSolution(self, pkg, label):
		node = self.getPackage(pkg)
		node.recordDecision(label)

	def getPackage(self, pkg):
		try:
			interval = self._packages[pkg]
		except:
			interval = self.LabelInterval(self._order, name = str(pkg), package = pkg)
			self._packages[pkg] = interval

			# Copy already assigned labels to the newly created node
			if pkg.label and pkg.label.type == Classification.TYPE_BINARY:
				interval.solution = pkg.label

			if pkg.trace:
				interval._trace = True

		return interval

	class UnsatisfiedLabelRequirementsReport:
		class Bucket:
			def __init__(self):
				self.offenses = []

		def __init__(self):
			self._labels = {}

		def add(self, label, offendingPackage, packageReport):
			key = str(label)

			bucket = self._labels.get(key)
			if bucket is None:
				bucket = self.Bucket()
				self._labels[key] = bucket
			bucket.offenses.append((offendingPackage, packageReport))

		def display(self):
			for labelName, bucket in sorted(self._labels.items()):
				infomsg(f"   {labelName} has unsatisfied requirements")
				for offendingPackage, packageReport in bucket.offenses:
					infomsg(f"     {offendingPackage}")
					packageReport.display("     ")
				infomsg("")

	def validateInitialPlacements(self, order):
		errors = 0

		infomsg("Validating initial package placements")
		report = self.UnsatisfiedLabelRequirementsReport()
		for node in order.bottomUpTraversal():
			for lower in node.lowerNeighbors:
				node._combinedRequirements.update(lower._combinedRequirements)

			if node.solution:
				# get the set of all labels below this node's label
				configuredRequirements = self._order.downwardClosureFor(node.solution)

				if not node._combinedRequirements.issubset(configuredRequirements):
					packageReport = self.NodeVersusLabelSetReport()
					for lower in node.lowerNeighbors:
						if not lower._combinedRequirements.issubset(configuredRequirements):
							missing = lower._combinedRequirements.difference(configuredRequirements)
							missing = self._order.maxima(missing)

							packageReport.add(lower.nameWithLabelReason, missing)
							errors += 1

					report.add(node.solution, node.nameWithLabelReason, packageReport)
					# errormsg(f"configuration problem: {node} has been labelled as {node.solution} but not all its requirements are covered")
					# report.display()

					# FIXME: we could check for some simple cases and make suggestions, such as
					# packages placed in @Foo that require @Foo+bar. Recommend moving them into
					# @Foo+bar (but check for any @Foo packages above and recommend moving them
					# as well).

				node._combinedRequirements = configuredRequirements

		if errors:
			errormsg(f"Detected {errors} configuration problem(s)")
			report.display()
			return False

		infomsg("OK, no conflicts detected in initial placement")
		return True

	def collapse(self, cycle):
		cycleSet = SolvingTree.createNodeSet(cycle)
		cyclePackages = reduce(set.union, (interval.packages for interval in cycle))
		cycleNames = list(map(str, cyclePackages))

		if len(cycle) > 2:
			names =  list(map(str, cycle))
			infomsg(f"Detected non-trivial cycle {' -> '.join(names)}")

		labels = Classification.createLabelSet()
		for node in cycle:
			if node.solution is not None:
				labels.add(node.solution)

		label = None
		if labels:
			if len(labels) > 1:
				warnmsg(f"Having a hard time collapsing cycle {' '.join(cycleNames)} because it has conflicting labels")
				for node in cycle:
					if node.solution:
						infomsg(f"  {node}: {node.solution}")

				infomsg("Picking a random label for now")
			label = labels.pop()

		above = reduce(set.union, (node._upperNeighbors for node in cycle), set())
		below = reduce(set.union, (node._lowerNeighbors for node in cycle), set())

		newInterval = self.LabelInterval(self._order, name = f"<{' '.join(cycleNames)}>", cycle = cyclePackages)
		newInterval._lowerNeighbors = below.difference(cycleSet)
		newInterval._upperNeighbors = above.difference(cycleSet)
		if label:
			newInterval.recordDecision(label)

		for lower in below:
			lower._upperNeighbors.difference_update(cycleSet)
			lower._upperNeighbors.add(newInterval)

		for upper in above:
			upper._lowerNeighbors.difference_update(cycleSet)
			upper._lowerNeighbors.add(newInterval)

		for pkg in cyclePackages:
			self._packages[pkg] = newInterval

		debugPackageCycles(f"Collapsed dependency cycle {newInterval}, label {label}")

	def createPartialOrder(self):
		order = PartialOrder(self.domain, "node runtime dependency")

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
			maxLen = max(map(len, cycles))
			debugPackageCycles(f"Detected {len(cycles)} runtime dependency cycles; longest cycle has {maxLen} elements")

			for cycle in cycles:
				self.collapse(cycle.members)

			# rinse and repeat
			return None

		order.finalize()
		return order

	def collapseAllCycles(self):
		assert(self._nodeOrder is None)

		# we may have to repeat this step several times, because
		# collapsing one cycle may introduce a new cycle.
		while self._nodeOrder is None:
			self._nodeOrder = self.createPartialOrder()

	def finalize(self):
		timing = ExecTimer()

		self.collapseAllCycles()

		if not self.validateInitialPlacements(self._nodeOrder):
			raise Exception(f"Unresolved conflicts in initial placement of packages, please fix filter rules")

		for node in self.bottomUpTraversal():
			for lower in node.lowerNeighbors:
				node.updateFromBelow(lower)

		for node in self.topDownTraversal():
			for lower in node.lowerNeighbors:
				lower.updateFromAbove(node)

		for node in self.randomWalk():
			if node.solution and node.siblings is not None:
				node.siblings.recordDecision(node, node.solution)

		infomsg(f"Computed candidates; {timing} elapsed")
		infomsg("")

	def randomWalk(self):
		return iter(self._packages.values())

	def topDownTraversal(self):
		return self._nodeOrder.topDownTraversal()

	def bottomUpTraversal(self):
		return self._nodeOrder.bottomUpTraversal()

	@property
	def allBuilds(self):
		return iter(self._builds.values())

class SolvingTreeBuilder(object):
	def __init__(self, classificationContext):
		global Classification

		if Classification is None:
			from filter import Classification as classificationImport
			Classification = classificationImport

		self.worker = classificationContext.worker
		self.context = self.worker.contextForArch(classificationContext.productArchitecture)
		self.classificationScheme = classificationContext.classificationScheme
		self.labelOrder = classificationContext.labelOrder
		self.store = classificationContext.store

	def edges(self, pkg):
		result = []
		for dep, target in self.context.resolveDownward(pkg):
			if target is not pkg:
				result.append(Classification.ReasonRequires(target, pkg, dep))

		return result

	def followEdge(self, edge, solvingTree):
		solvingTree.addEdge(edge.dependant, edge.package)

		for pkg in edge.package, edge.dependant:
			build = self.getBuildForPackage(pkg)
			if build is not None:
				solvingTree.addBuild(build)

		return edge.package

	def buildTree(self, packages):
		solvingTree = SolvingTree(self.classificationScheme, self.labelOrder)

		worker = self.worker
		worker.update(packages)
		seen = set()

		while True:
			pkg = worker.next()
			if pkg is None:
				break

			if pkg in seen:
				continue
			seen.add(pkg)

			if pkg.isSourcePackage:
				continue

			edges = self.edges(pkg)
			for e in edges:
				tgt = self.followEdge(e, solvingTree)
				if tgt is not None:
					worker.add(tgt)

		solvingTree.finalize()

		return solvingTree

	def getBuildForPackage(self, rpm):
		buildId = rpm.obsBuildId
		if buildId is None:
			infomsg(f"No OBS package for {rpm}")
			return None

		build = self.store.retrieveOBSPackageByBuildId(buildId)
		if build is None:
			infomsg(f"Could not find OBS package {buildId} for {rpm.shortname}")
		return build



