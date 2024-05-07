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
from profile import profiling

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

class Tracer:
	def __init__(self, focusLabels = None):
		self.focusLabels = focusLabels

		emptySet = Classification.createLabelSet()
		self.labelSetClass = emptySet.__class__

	def focus(self, labels):
		if self.focusLabels is not None:
			return intersectSets(labels, self.focusLabels)
		return labels

	def renderLabelSet(self, labels):
		what = "labels"
		if self.focusLabels is not None and labels is not None:
			labels = intersectSets(labels, self.focusLabels)
			what = "focus labels"

		if labels is None:
			return "[unconstrained]"

		if not labels:
			return f"[no {what}]"

		if len(labels) >= 10:
			return f"[{len(labels)} {what}]"

		return f"[{what} {' '.join(map(str, labels))}]";

	def describeLabelSetChange(self, after, before):
		if self.focusLabels is not None:
			after = intersectSets(after, self.focusLabels)
			before = intersectSets(before, self.focusLabels)

		if after == before:
			return f"no change of labels"

		words = []

		removed = before.difference(after)
		if removed:
			words.append(f"removed {self.renderLabelSet(removed)}")

		added = after.difference(before)
		if added:
			words.append(f"added {self.renderLabelSet(added)}")

		words.append(f"resulting {self.renderLabelSet(after)}")

		return "; ".join(words)

	def labelSetMessage(self, object, labels, msg, indent = ''):
		change = self.renderLabelSet(labels)
		infomsg(f"{indent}{object}: {msg}: {change}")

	def updateCandidates(self, object, candidates, before = None, msg = None, indent = ''):
		if msg is None:
			msg = "updated candidates"

		if before is not None:
			change = self.describeLabelSetChange(candidates, before)
		else:
			change = self.renderLabelSet(candidates)

		infomsg(f"{indent}{object}: {msg}: {change}")

class NodeVersusLabelSetReport:
	class LabelSet:
		def __init__(self, key):
			self.key = key
			self.names = []

	def __init__(self):
		self.byLabels = {}

	def add(self, nodeName, labels):
		key = ' '.join(sorted(map(str, labels)))
		info = self.byLabels.get(key)
		if info is None:
			info = self.LabelSet(key)
			self.byLabels[key] = info
		info.names.append(nodeName)

	def display(self, indent = ""):
		output = lambda msg: infomsg(indent + msg)

		for key, info in self.byLabels.items():
			it = iter(info.names)
			if len(key) > 20:
				output(f"    {key}")
			else:
				first = next(it)
				output(f"    {key:20} {first}")

			for name in it:
				output(f"    {'':20} {name}")

class InaccessibleLabelReport:
	class LabelSet:
		def __init__(self, label):
			self.label = label
			self.paths = []

	class Path:
		def __init__(self, nodeList):
			self.names = list(map(str, nodeList))

		def __str__(self):
			return ' -> '.join(self.names)

	def __init__(self):
		self.byLabels = {}

	def add(self, label, path):
		info = self.byLabels.get(label)
		if info is None:
			info = self.LabelSet(label)
			self.byLabels[label] = info

		info.paths.append(self.Path(path))

	def display(self):
		for label, info in self.byLabels.items():
			key = str(label)
			it = iter(info.paths)
			if len(key) > 20:
				infomsg(f"    {key}")
			else:
				path = next(it)
				infomsg(f"    {key:20} {path}")

			for path in it:
				infomsg(f"    {'':20} {path}")

class BuildComponentsReport:
	def __init__(self, build):
		self.name = build.name
		self._labels = {}

		for pkg in build.packages:
			label = pkg.label
			if label is not None and label.sourceProject is not None:
				self.add(pkg, label.sourceProject)

	def add(self, pkg, label):
		key = str(label)

		if key not in self._labels:
			self._labels[key] = []
		self._labels[key].append(pkg)

	def display(self):
		for labelName, pkgList in sorted(self._labels.items()):
			infomsg(f"   {labelName}:")
			for pkg in pkgList:
				if pkg.labelReason is None:
					infomsg(f"     {pkg}")
				else:
					infomsg(f"     {pkg} {pkg.labelReason}")
			infomsg("")

class MultiBuildComponentsReport:
	def __init__(self, build, selectedComponent):
		self.name = build.name
		self.build = build
		self.componentLabel = selectedComponent

	def display(self):
		cc = self.build.componentConstraint
		infomsg(f"   {cc.name} is placed in {cc.componentLabel} via {cc.origin}")
		infomsg(f"   {self.build} would be placed in {self.componentLabel}")
		# FIXME: show which rpms cause each package to be placed in which component

class SolvingTree(object):
	domain = fastsets.Domain("nodes")

	# lowerCone, upperCone and candidates are all convex sets
	class PackageNode(domain.member):
		def __init__(self, order, name, package = None, cycle = None):
			assert(package or cycle)

			super().__init__()

			self.name = name
			self.package = package
			# FIXME: rename siblings to buildInfo
			self.siblings = None
			# FIXME: rename _cycle to _cyclePackages?
			self._cycle = cycle
			# This is usually None, except when representing a cycle. In which case _cycleBuilds is a list
			self._cycleBuilds = None
			self._order = order
			self._lowerNeighbors = SolvingTree.createNodeSet()
			self._upperNeighbors = SolvingTree.createNodeSet()
#			self.upperBounds = set()
#			self.lowerBounds = set()
			self._lowerCone = None
			self._upperCone = None
			self._candidates = None
			self._solution = None
			self._reason = None
			self._combinedRequirements = set()

			self.placement = None

			self._trace = False
			self._tracer = None

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

			if self._reason:
				return f"{self} {self._reason}"

			return str(self)

		@property
		def solution(self):
			return self._solution

		@solution.setter
		def solution(self, label):
			if self._solution is label:
				return

			if self._solution:
				raise Exception(f"Conflicting solution for {self}: label {self._solution} vs {label}")
			if self._trace:
				infomsg(f" {self} set solution to {label}")
			self._solution = label
			self._reason = "(called solution.setter directly)"

			# update lower and upper cone here?

		@property
		def packages(self):
			if self.package:
				return set([self.package])
			return self._cycle

		@property
		def isCollapsedCycle(self):
			return bool(self._cycle)

		@property
		def builds(self):
			if self._cycleBuilds is not None:
				return self._cycleBuilds
			if self.siblings:
				return [self.siblings]
			return []

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
			if not self._trace:
				# do NOT use update_intersection
				self._lowerCone = self.intersectCones(self.lowerCone, lowerNeighbor.lowerCone)
			else:
				before = self.lowerCone
				self._lowerCone = self.intersectCones(self.lowerCone, lowerNeighbor.lowerCone)
				self.traceConeUpdate(lowerNeighbor, "lower", before, self._lowerCone)

		def updateFromAbove(self, upperNeighbor):
			if not self._trace:
				# do NOT use update_intersection
				self._upperCone = self.intersectCones(self.upperCone, upperNeighbor.upperCone)
			else:
				before = self._upperCone
				self._upperCone = self.intersectCones(self.upperCone, upperNeighbor.upperCone)
				self.traceConeUpdate(upperNeighbor, "upper", before, self._upperCone)

		def traceConeUpdate(self, neighbor, howRelated, beforeCone, afterCone):
			if self._focusLabels is None:
				infomsg(f" {self}: add {howRelated} neighbor {neighbor}; {howRelated} cone:")
				displayLabelSetFull(afterCone, indent = "   ")
			else:
				inFocusBefore = intersectSets(beforeCone, self._focusLabels)
				if afterCone is None or inFocusBefore.issubset(afterCone):
					infomsg(f" {self}: add {howRelated} neighbor {neighbor}; {howRelated} cone no change of focus labels {' '.join(map(str, inFocusBefore))}")
				else:
					lost = inFocusBefore.difference(afterCone)
					infomsg(f" {self}: add {howRelated} neighbor {neighbor}; {howRelated} cone loses focus labels {' '.join(map(str, lost))}")

					# chase up/down the tree to find out where they get lost
					self.chaseConeUpdates(neighbor, howRelated, inFocusBefore, indent = "      ")

		def chaseConeUpdates(self, neighbor, howRelated, focusLabels, indent):
			neighAttrName = f"{howRelated}Neighbors"
			coneAttrName = f"{howRelated}Cone"

			queue = [neighbor]
			scapegoats = set()
			while queue:
				node = queue.pop(0)

				if node._solution:
					if howRelated == 'upper':
						cone = self._order.downwardClosureFor(node._solution)
					else:
						cone = self._order.upwardClosureFor(node._solution)
					if not focusLabels.issubset(cone):
						scapegoats.add(node)

				nodeNeighbors = getattr(node, neighAttrName)
				if not nodeNeighbors:
					scapegoats.add(node)
					continue

				for neigh in nodeNeighbors:
					cone = getattr(neigh, coneAttrName)
					if cone is not None and not focusLabels.issubset(cone):
						queue.append(neigh)

			infomsg(f"{indent}due to the following {howRelated} relative(s)")
			for node in scapegoats:
				cone = getattr(node, coneAttrName)
				if cone is None:
					continue
				missing = focusLabels.difference(cone)
				if node._solution:
					infomsg(f"{indent} {node} ({node._solution}) lacks {' '.join(map(str, missing))}")
				else:
					infomsg(f"{indent} {node} lacks {' '.join(map(str, missing))}")

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
				componentLabel = label.componentLabel
				if componentLabel is not None:
					projects.add(componentLabel)
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
			self._reason = reason

			if self.siblings is not None:
				self.siblings.recordDecision(node, label, reason)

	class SiblingInfo:
		def __init__(self, build):
			self.name = build.name
			self.basePackageName = build.basePackageName
			self.packages = []
			self.sources = []

			self.labels = Classification.createLabelSet()
			for rpm in build.binaries:
				label = rpm.label

				# Do not add packages that are labeled with disposition ignored
				if label and label.disposition == Classification.DISPOSITION_IGNORE:
					continue

				if rpm.isSourcePackage:
					self.sources.append(rpm)
				else:
					self.packages.append(rpm)

					if label and label.type == Classification.TYPE_BINARY:
						self.labels.add(label)

			self.componentConstraint = SolvingTree.ComponentConstraint(self.name)
			self.baseLabelConstraint = build.baseLabel

		def __str__(self):
			return self.name

		def __iter__(self):
			return iter(self.packages)

		def __len__(self):
			return len(self.packages)

		def recordDecision(self, node, label, reason):
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

		def validate(self):
			# in case we want to add more checks
			return self.validateUniqueComponent()

		def validateUniqueComponent(self):
			uniqueComponent = None
			for pkg in self.packages:
				label = pkg.label
				if label is None or label.sourceProject is None:
					continue
				if uniqueComponent is None:
					uniqueComponent = label.sourceProject
				elif uniqueComponent is not label.sourceProject:
					return BuildComponentsReport(self)

			if uniqueComponent is not None:
				if not self.componentConstraint.setLabel(uniqueComponent, self):
					return MultiBuildComponentsReport(self, uniqueComponent)

			return None

	class ComponentConstraint:
		def __init__(self, name):
			self.name = name
			self.componentLabel = None
			self.origin = None

		def setLabel(self, componentLabel, origin):
			if self.componentLabel is not None and self.componentLabel is not componentLabel:
				# raise Exception(f"Conflicting choice of component for {self.name}: {self.componentLabel} vs {componentLabel}")
				errormsg(f"cannot set {self.name} to {componentLabel}; already in {self.componentLabel} via {self.origin}")
				return False

			self.componentLabel = componentLabel
			if self.origin is None:
				self.origin = origin
			return True

		def __str__(self):
			if self.componentLabel:
				return f"{self.componentLabel} via {self.name}"
			return f"* via {self.name}"

		def __bool__(self):
			return bool(self.componentLabel)

	def __init__(self, classificationScheme, order = None, focusLabels = None):
		self._classificationScheme = classificationScheme

		if order is None:
			order = classificationScheme.createOrdering(Classification.TYPE_BINARY)
		self._order = order

		self._packages = {}
		self._builds = {}
		self._buildsByName = {}
		self._nodeOrder = None

		self._focusLabels = None
		if focusLabels:
			self._focusLabels = Classification.createLabelSet(focusLabels)

		self._tracer = Tracer(self._focusLabels)

	@property
	def numPackages(self):
		return len(self._packages)

	@property
	def numBuilds(self):
		return len(self._builds)

	@classmethod
	def createNodeSet(klass, initialValues = None):
		return klass.domain.set(initialValues)

	def addPackage(self, pkg):
		# Do not add packages that are labeled with disposition ignored
		if pkg.label and pkg.label.disposition == Classification.DISPOSITION_IGNORE:
			if pkg.trace:
				infomsg(f"{pkg} is label {pkg.label} with disposition {pkg.label.disposition}")
			return None
		return self.getPackage(pkg)

	def addEdge(self, requiringNode, requiredNode):
		assert(requiringNode is not requiredNode)
		assert(isinstance(requiringNode, self.PackageNode))
		assert(isinstance(requiredNode, self.PackageNode))
		requiringNode.addLowerNeighbor(requiredNode)
		requiredNode.addUpperNeighbor(requiringNode)

	def addBuild(self, build):
		if build in self._builds:
			return

		siblings = self.SiblingInfo(build)
		self._builds[build] = siblings
		for pkg in siblings.packages:
			self.getPackage(pkg).siblings = siblings

		assert(build.name not in self._buildsByName)
		self._buildsByName[build.name] = siblings

		return siblings

	@property
	def builds(self):
		warnmsg(f"{self.__class__.__name__}.builds - please use allBuilds property instead")
		return iter(self._builds.values())

	def setSolution(self, pkg, label):
		node = self.getPackage(pkg)
		node.recordDecision(label)

	@profiling
	def getPackage(self, pkg):
		try:
			packeNode = self._packages[pkg]
		except:
			packeNode = self.PackageNode(self._order, name = pkg.name, package = pkg)
			self._packages[pkg] = packeNode

			# Copy already assigned labels to the newly created node
			if pkg.label and pkg.label.type == Classification.TYPE_BINARY:
				packeNode.recordDecision(pkg.label, pkg.labelReason or "initial placement")

			if pkg.trace:
				infomsg(f" {packeNode} [{pkg.label}] added to solving tree")
				packeNode._trace = True
				packeNode._tracer = self._tracer
				packeNode._focusLabels = self._focusLabels

		return packeNode

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
					with loggingFacade.temporaryIndent(3):
						packageReport.display()
				infomsg("")

	class ConflictingComponentsReport:
		def __init__(self):
			self._builds = []

		def add(self, buildReport):
			self._builds.append(buildReport)

		def display(self):
			for buildReport in self._builds:
				infomsg(f"   {buildReport.name} has been spread across separate components")
				buildReport.display()
				infomsg("")

	def validateInitialPlacements(self, order):
		def findOffendingNode(node, missing):
			if node.solution is not None and node.solution in missing:
				return [node]
			for lower in node.lowerNeighbors:
				bad = findOffendingNode(lower, missing)
				if bad is not None:
					return [node] + bad

			return None

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
					packageReport = InaccessibleLabelReport()
					for lower in node.lowerNeighbors:
						if lower._combinedRequirements.issubset(configuredRequirements):
							continue

						missing = lower._combinedRequirements.difference(configuredRequirements)

						offenders = findOffendingNode(lower, missing)
						assert(offenders)

						infomsg(f"{node} -> {lower} -> {' '.join(map(str, offenders))}")

						label = offenders[-1].solution
						packageReport.add(label, offenders)
						errors += 1

					report.add(node.solution, node.nameWithLabelReason, packageReport)
					# errormsg(f"configuration problem: {node} has been labelled as {node.solution} but not all its requirements are covered")
					# report.display()

					# FIXME: we could check for some simple cases and make suggestions, such as
					# packages placed in @Foo that require @Foo+bar. Recommend moving them into
					# @Foo+bar (but check for any @Foo packages above and recommend moving them
					# as well).

				node._combinedRequirements = configuredRequirements

		report2 = self.ConflictingComponentsReport()
		for build in self.allBuilds:
			error = build.validate()
			if error is not None:
				report2.add(error)
				errors += 1

		if errors:
			errormsg(f"Detected {errors} configuration problem(s)")
			report.display()
			report2.display()
			return False

		infomsg("OK, no conflicts detected in initial placement")
		return True

	def collapse(self, cycle):
		cycleSet = SolvingTree.createNodeSet(cycle)
		cyclePackages = reduce(set.union, (node.packages for node in cycle))
		cycleNames = list(map(str, cyclePackages))

		if len(cycle) > 2:
			names = list(map(str, cycle))
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

		# FIXME: put cycle information into a Cycle object and pass that to the constructor
		newPackageNode = self.PackageNode(self._order, name = f"<{' '.join(cycleNames)}>", cycle = cyclePackages)
		newPackageNode._lowerNeighbors = below.difference(cycleSet)
		newPackageNode._upperNeighbors = above.difference(cycleSet)
		if label:
			newPackageNode.recordDecision(label)

		for lower in below:
			lower._upperNeighbors.difference_update(cycleSet)
			lower._upperNeighbors.add(newPackageNode)

		for upper in above:
			upper._lowerNeighbors.difference_update(cycleSet)
			upper._lowerNeighbors.add(newPackageNode)

		for pkg in cyclePackages:
			self._packages[pkg] = newPackageNode

		cycleBuilds = set()
		for oldNode in cycleSet:
			if oldNode._cycleBuilds is not None:
				cycleBuilds.update(set(oldNode._cycleBuilds))
			elif oldNode.siblings:
				cycleBuilds.add(oldNode.siblings)
		newPackageNode._cycleBuilds = list(cycleBuilds)

		debugPackageCycles(f"Collapsed dependency cycle {newPackageNode}, label {label}")

	def createPartialOrder(self):
		order = PartialOrder(self.domain, "node runtime dependency")

		seen = set()
		for packageNode in self._packages.values():
			# we have to check for duplicate nodes because we may have collapsed
			# a dependency loop, so that we have several packages point to the same
			# PackageNode
			if packageNode not in seen:
				order.add(packageNode, packageNode._lowerNeighbors)
				seen.add(packageNode)

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

	def resolveMultiBuilds(self):
		absentBasePackageConstraints = {}

		splitMultibuilds = self._classificationScheme.options.splitMultiBuilds
		splitReport = dict((name, set()) for name in splitMultibuilds)

		for build in self.multiBuilds:
			baseName = build.basePackageName

			if baseName in splitMultibuilds:
				splitReport[baseName].add(build)
				continue

			baseBuild = self._buildsByName.get(baseName)
			if baseBuild is not None:
				cc = baseBuild.componentConstraint
			else:
				if baseName not in absentBasePackageConstraints:
					debugmsg(f"{build}: cannot find base build {baseName} - faking a ComponentConstraint")
					absentBasePackageConstraints[baseName] = SolvingTree.ComponentConstraint(baseName)
				cc = absentBasePackageConstraints[baseName]

			build.componentConstraint = cc

		if splitReport:
			infomsg(f"We allow the following builds to be spread across distinct components:")
			for baseName in sorted(splitReport.keys()):
				infomsg(f"   {baseName}:")

				baseBuild = self._buildsByName.get(baseName)
				if baseBuild is not None:
					splitReport[baseName].add(baseBuild)

				for build in sorted(splitReport[baseName], key = lambda b: b.name):
					infomsg(f"    - {build}")

		# preserve this report so that we can later write it to some problem log
		self.splitMultibuildReport = splitReport

	# Ensure that each build has its .packages list sorted in order of dependency
	def sortBuildInfos(self, verify = False):
		siblingMap = {}
		for buildInfo in self.allBuilds:
			for pkg in buildInfo.packages:
				if pkg in siblingMap:
					raise Exception(f"Package {pkg} is referenced by more than one build")
				siblingMap[pkg] = buildInfo

			buildInfo.savedPackages = buildInfo.packages
			buildInfo.packages = []

		for node in self.bottomUpTraversal():
			for pkg in node.packages:
				if pkg.isSourcePackage:
					continue

				buildInfo = siblingMap.get(pkg)
				if buildInfo is None:
					errormsg(f"No build info for {pkg}")
					continue
				buildInfo.packages.append(pkg)

		if verify:
			for buildInfo in self.allBuilds:
				a = sorted(buildInfo.savedPackages, key = lambda build: build.name)
				b = sorted(buildInfo.packages, key = lambda build: build.name)
				if a != b:
					infomsg(f"{buildInfo}: changed list of packages after sorting")
					infomsg(f"  before: {' '.join(map(str, a))}")
					infomsg(f"  after:  {' '.join(map(str, b))}")
					raise Exception("consistency problem")

	def finalize(self):
		timing = ExecTimer()

		self.collapseAllCycles()

		self.resolveMultiBuilds()

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
				node.siblings.recordDecision(node, node.solution, f"copied from {node}")

		self.reportFocusNodesAndLabels()

		# Now make sure that each build has its .packages list sorted in order of dependency
		self.sortBuildInfos()

		infomsg(f"Computed candidates; {timing} elapsed")
		infomsg("")

	def reportFocusNodesAndLabels(self):
		if self._focusLabels:
			infomsg("")
			infomsg(f"Traced nodes and focus labels")
			for node in self.randomWalk():
				if not node._trace:
					continue

				if node.candidates is not None:
					focusCandidates = self._focusLabels.intersection(node.candidates)
					infomsg(f" {node}: {' '.join(map(str, focusCandidates))}")
				else:
					infomsg(f" {node}: unconstrained")
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

	@property
	def multiBuilds(self):
		return filter(lambda b: b.basePackageName, self._builds.values())

	def topDownBuildTraversal(self):
		for build in self.commonBuildTraversal(self.topDownTraversal()):
			yield build

	def bottomUpBuildTraversal(self):
		for build in self.commonBuildTraversal(self.bottomUpTraversal()):
			yield build

	def commonBuildTraversal(self, nodeTraversal):
		seen = set()
		for node in nodeTraversal:
			# ignore .src rpms, they are not really part of the ordering as they're
			# never dominated from above
			if node.package and node.package.isSourcePackage:
				continue

			# for a regular single-package node, this returns a list containing
			# just the SiblingInfo object that represents the OBS build from
			# which this package originates. In the case of a collapsed cycle,
			# this returns the SiblingInfo objects of all packages that belong
			# top this cycle
			builds = node.builds
			if not builds:
				warnmsg(f"package {node} - cannot determine OBS build")
				continue

			for build in builds:
				if build not in seen:
					seen.add(build)
					yield build

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

		self.pkgToNode = {}
		self.ignoredPackages = set()
		self.builds = set()

		infomsg("Validating Component tree")
		with loggingFacade.temporaryIndent(3):
			self.validateComponentTree()

	# level determines how strict we are.
	#  0: validate base labels only
	#  1: validate @Baselabel-purpose in addition, but no @Baselabel+option
	#  2: validate all labels
	def validateComponentTree(self, level = 0):
		componentOrder = self.classificationScheme.componentOrder()
		issues = 0

		baseLabels = Classification.createLabelSet()
		for label in sorted(self.classificationScheme.allBinaryLabels, key = str):
			if level == 0 and label.parent is not None:
				continue

			if level <= 1 and label.flavorName:
				continue

			if label.componentLabel is None:
				errormsg(f"base label {label} is not assigned to any component")
				continue

			# Get the set of all components in view of this component
			componentClosure = componentOrder.downwardClosureFor(label.componentLabel)

			# Get the transitive closure of everything this base label
			# requires
			required = self.labelOrder.downwardClosureFor(label)
			for req in label.runtimeRequires:
				if req.componentLabel is None:
					# not classified yet
					continue

				if label.canAccessDirectly(req, componentOrder):
					continue

				if not req.isExported:
					# we require a label that is in an inaccessible component, and not exported. This is always a problem
					infomsg(f"{label} [{label.componentLabel}] requires {req} which is in inaccessible component {req.componentLabel}")
					issues += 1

				# we require a label exported by some other component
				debugmsg(f"{label} [{label.componentLabel}] requires {req} which is exported by component {req.componentLabel}")
				req.numImports += 1

		if issues:
			# raise Exception(f"Found {issues} problems with the component tree; please fix first")
			errormsg(f"Found {issues} problems with the component tree; continuing despite these problems")
		else:
			infomsg(f"Component tree checks out OK")

	def addPackage(self, solvingTree, pkg, build):
		node = self.pkgToNode.get(pkg)

		if node is None:
			node = solvingTree.addPackage(pkg)
			if node is not None:
				self.pkgToNode[pkg] = node
			else:
				self.ignoredPackages.add(pkg)

		return node

	def getPackage(self, pkg):
		return self.pkgToNode.get(pkg)

	def buildTree(self, buildList, **kwargs):
		solvingTree = SolvingTree(self.classificationScheme, order = self.labelOrder, **kwargs)

		seen = set()

		# Loop over all builds and try to add their rpms.
		# An rpm may be labelled with a label that has disposition=ignore.
		# Do not add these.
		# Only add the build object itself if it has at least one binary rpm
		# that we did not ignore.
		for build in buildList:
			addBuild = False
			src = build.sourcePackage
			for rpm in build.binaries:
				if rpm is src:
					continue
				if self.addPackage(solvingTree, rpm, build):
					addBuild = True
			if not addBuild:
				debugmsg(f"Ignoring build {build} because it has no binary RPMs")
				continue

			if src is not None:
				self.addPackage(solvingTree, src, build)
			solvingTree.addBuild(build)
			self.builds.add(build)

		# In a second step, loop over those rpms we added and add edges for their
		# dependencies
		for rpm, node in self.pkgToNode.items():
			self.addEdgesForPackage(solvingTree, rpm, node)

		hiddenEdges = self.context.suppressedDependencies
		if hiddenEdges:
			infomsg(f"Hiding the following package dependencies (based on resolver hints)")
			with loggingFacade.temporaryIndent(3):
				for pkg, target in hiddenEdges:
					infomsg(f"{pkg} -> {target}")

		solvingTree.finalize()
		return solvingTree

	def addEdgesForPackage(self, solvingTree, requiringPkg, requiringNode):
		# The product family yaml file specifies a list of dependencies to be ignored,
		# for instance on systemd-mini
		self.context.transformDependencies(requiringPkg)

		for dep, target in self.context.resolveDownward(requiringPkg):
			if target is requiringPkg:
				continue

			if target in self.ignoredPackages:
				# A package that is not ignored MUST NOT require an ignored package
				raise Exception(f"{requiringPkg} requires {target} [{target.label}] with disposition 'ignore'")

			requiredNode = self.getPackage(target)
			if requiredNode is None:
				errormsg(f"{requiringPkg.fullname()} requires unknown package {target.fullname()}")
				errormsg(f"{target} does not seem to be part of any build")
				raise Exception(f"Unknown dependency {target.fullname()}")

			solvingTree.addEdge(requiringNode, requiredNode)

