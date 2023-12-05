##################################################################
#
# Labelling solver algorithm, based on SolvingTree
#
#
#
#
#
##################################################################
from util import ExecTimer
from util import filterHighestRanking
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from filter import Classification
from filter import ClassificationResult

def intersectSets(a, b):
	if a is None:
		return b
	elif b is None:
		return a
	return a.intersection(b)

def renderLabelSet(name, labels):
	if labels is None:
		return f"[unconstrained {name}]"

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

##################################################################
# Solvers - for now, these are used only during stage 2
##################################################################
class Solver(object):
	pass

class SourceHintsSolver(Solver):
	def __init__(self, componentLabel, potentialClassification):
		self.componentLabel = componentLabel
		self.closure = potentialClassification.labelOrder.downwardClosureForSet(componentLabel.buildRequires)

		if not componentLabel.buildRequires:
			warnmsg(f"Created {self} with empty build requirements set")
		if not self.closure:
			warnmsg(f"Created {self} with empty build requirements closure")

	def __str__(self):
		return f"source component solver for {self.componentLabel}"

	def tryToSolve(self, buildPlacement):
		return buildPlacement.solveWithConstraints(self.closure)

class SolverFactory(object):
	def __init__(self, potentialClassification):
		self.potentialClassification = potentialClassification
		self.componentSolvers = {}

	def sourceHintsSolver(self, label):
		try:
			return self.componentSolvers[label]
		except:
			pass

		solver = SourceHintsSolver(label, self.potentialClassification)
		self.componentSolvers[label] = solver
		return solver

##################################################################
# The actual solving algorithm
##################################################################
class PotentialClassification(object):
	def __init__(self, solvingTree):
		self.solvingTree = solvingTree
		self._preferences = self.PlacementPreferences()

	@property
	def labelOrder(self):
		return self.solvingTree._order

	@property
	def classificationScheme(self):
		return self.solvingTree._classificationScheme

	def getPackageNode(self, pkg):
		return self.solvingTree.getPackage(pkg)

	class PlacementConstraints:
		def __init__(self):
			self.validComponents = None
			self.validBaseLabels = None

		def addValidComponent(self, name):
			if self.validComponents is None:
				self.validComponents = set()
			self.validComponents.add(name)

		def addValidBaseLabel(self, name):
			if self.validBaseLabels is None:
				self.validBaseLabels = Classification.createLabelSet()
			self.validBaseLabels.add(name)

		# currently unused
		def preFilterCandidateLabels(self, candidates, flavor = None, purpose = None):
			# everything goes
			if candidates is None:
				return candidates

			if flavor:
				candidates = Classification.createLabelSet(filter(lambda label: label.flavorName == flavor, candidates))
			if purpose:
				candidates = Classification.createLabelSet(filter(lambda label: label.purposeName == purpose, candidates))
			return candidates

		def constrainComponents(self, packagePlacement):
			if packagePlacement.candidates is not None and self.validComponents is not None:
				preferred = Classification.createLabelSet(filter(lambda label: label.componentName in self.validComponents, packagePlacement.candidates))
				if packagePlacement.tracer:
					compList = ' '.join(map(str, self.validComponents))
					packagePlacement.tracer.updateCandidates(packagePlacement, preferred,
							before = packagePlacement.candidates,
							msg = f"constrained by components {compList}",
							indent = '   ')
				packagePlacement.candidates = preferred

	class PlacementPreferences(object):
		class Hint:
			def __init__(self, preferredLabel, others):
				self.preferred = preferredLabel
				self.others = Classification.createLabelSet(others)
				if preferredLabel in self.others:
					self.others.remove(preferredLabel)

		def __init__(self):
			self._prefs = []

		def add(self, preferredLabel, others):
			self._prefs.append(self.Hint(preferredLabel, others))

		def filterCandidates(self, candidates):
			if not self._prefs:
				return candidates

			for hint in self._prefs:
				if hint.preferred in candidates:
					candidates = candidates.difference(hint.others)

			return candidates

	class PackagePlacement(object):
		def __init__(self, labelOrder, node, label = None):
			# maybe the node should refer to this placement, not the other way around
			self.labelOrder = labelOrder
			self.name = str(node)
			self.node = node
			self.label = label
			self.labelReason = None
			self.autoLabel = None
			self.failed = False
			self.trace = node._trace
			self.tracer = node._tracer

		def __str__(self):
			return str(self.node)

		@property
		def isSolved(self):
			return bool(self.label)

		@property
		def isFinal(self):
			return bool(self.label) or self.failed

		def propagateSolvers(self):
			pass

		def reportVerdict(self, node, result):
			# FIXME: this is really just the reporting stage, so no idea why we're trying to update
			# the node's solution here.
			if node.solution and node.solution is not self.label:
				errormsg(f"BUG: Placement algorithm is trying to change label for {node} from {node.solution} to {self.label}")
			node.solution = self.label

			for pkg in self.node.packages:
				if pkg.label is self.label:
					result.labelOnePackage(pkg, pkg.label, pkg.labelReason)
				else:
					result.labelOnePackage(pkg, self.label, self.labelReason)

	class DefinitivePackagePlacement(PackagePlacement):
		def __init__(self, labelOrder, node):
			super().__init__(labelOrder, node, label = node.solution)

		@property
		def baseLabels(self):
			return Classification.createLabelSet((self.label.baseLabel, ))

		def addSolver(self, solver):
			pass

	class TentativePackagePlacement(PackagePlacement):
		def __init__(self, labelOrder, node, preferences):
			super().__init__(labelOrder, node)

			self.preferences = preferences
			self.candidates = node.candidates
			self.flavor = None
			self.purpose = None

			self.constrainedAbove = bool(node.upperNeighbors)
			self.constrainedBelow = bool(node.lowerNeighbors)

			self.solvers = set()

		def fail(self, msg):
			errormsg(f"{self}: {msg}")
			self.failed = True
			return False

		def setSolution(self, label, labelReason = None):
			self.label = label
			self.labelReason = labelReason

		def setSolutionFromBaseLabel(self, choice, baseLabel):
			infomsg(f"{self} is placed in {choice} (optimal label based on base label {baseLabel})")
			self.setSolution(choice)

		@property
		def baseLabels(self):
			if self.candidates is None:
				return None

			return Classification.baseLabelsForSet(self.candidates)

		def applyConstraints(self, constraints):
			if not self.candidates:
				return

			constraints.constrainComponents(self)

		# After the first stage, we look at the build requirements of all source packages
		# and use the component label to constrain its build requirements
		def addSolver(self, solver):
			if self.label is None:
				self.solvers.add(solver)
				if self.trace:
					infomsg(f"{self} added {solver}")

		def propagateSolvers(self):
			if not self.solvers:
				return

			for lower in self.node.lowerNeighbors:
				buildPlacement = lower.placement
				if buildPlacement.label is None:
					lower.placement.solvers.update(self.solvers)

		# The node corresponds to a package that has been auto-labelled as "devel" (purpose)
		# or "python" (flavor). Reduce the list of candidates to those that have a matching
		# purpose or flavor
		def applyFlavorOrPurpose(self, label):
			candidates = self.candidates

			# if, originally, the set of candidates is completely unconstrained,
			# we now have to whittle those down to a specific subset
			if candidates is None:
				candidates = self.labelOrder.allkeys

			if label.disposition == Classification.DISPOSITION_MERGE:
				infomsg(f"Not constraining {self} by {label.type} label {label} due to disposition {label.disposition}")
				return

			if label.type == Classification.TYPE_AUTOFLAVOR:
				flavorName = label.name
				if self.flavor is not None and self.flavor is not label:
					return self.fail(f"conflicting purposes {self.flavor} and {flavorName} - this will never work")

				self.candidates = Classification.createLabelSet(filter(lambda label: label.flavorName == flavorName, candidates))

				# if the autoflavor has a disposition of maybe_merge, check for any base labels that
				# cover all requirements of the autoflavor, and add them back in
				if label.disposition == Classification.DISPOSITION_MAYBE_MERGE:
					# filter out those candidates that provide all the runtime requirements that this autoflavor needs
					# (which is the condition for merging this autoflavor).
					merged = Classification.createLabelSet(filter(
							lambda cand: label.runtimeRequires.issubset(self.labelOrder.downwardClosureFor(cand)),
							candidates))
					self.candidates.update(merged)
					if merged and self.tracer:
						self.tracer.labelSetMessage(self, merged, f"{label} can be merged into",
							indent = '   ')

				if self.tracer:
					self.tracer.updateCandidates(self, self.candidates,
						before = candidates,
						msg = f"constrained by autoflavor {label}",
						indent = '   ')

				self.flavor = label
			elif label.type == Classification.TYPE_PURPOSE:
				purposeName = label.name
				if self.purpose is not None and self.purpose is not label:
					return self.fail(f"conflicting purposes {self.purpose} and {purposeName} - this will never work")

				self.candidates = Classification.createLabelSet(filter(lambda label: label.purposeName == purposeName, candidates))
				self.purpose = label

				if self.tracer:
					self.tracer.updateCandidates(self, self.candidates,
						before = candidates,
						msg = f"constrained by purpose {label}",
						indent = '   ')
			else:
				raise Exception(f"{self}: Unexpected label {label} type {label.type}")

			debugmsg(f"{self} constrained by {label.type} {label}")
			return True

		def trivialChecks(self):
			if self.candidates is None:
				# can be placed anywhere
				return False

			numCandidates = len(self.candidates)
			if numCandidates == 1:
				label = next(iter(self.candidates))
				infomsg(f"{self.node} has exactly one candidate label, {label}")
				self.setSolution(label)
				return True

			if numCandidates == 0:
				infomsg(f"{self.node} cannot be placed; no candidate labels")
				self.failed = True
				return True

			return False

		def tryToPlaceTopDown(self, node):
			if self.label is not None:
				return

			if not node.upperNeighbors:
				if self.candidates is None:
					infomsg(f"{node} has no parent and no constraints; please provide a hint where to place it")
					return

				if not self.candidates:
					return

				max = self.labelOrder.maxima(self.candidates)
				if len(max) == 1:
					maxLabel = next(iter(max))
					infomsg(f"{self.node} has no upper neighbors; best candidate is {maxLabel}")
					infomsg(f"   {renderLabelSet('candidates', self.candidates)}")
					self.setSolution(maxLabel)
					return True

				baseLabels = Classification.createLabelSet(label.baseLabel for label in max)
				if len(baseLabels) == 1:
					maxLabel = next(iter(baseLabels))
					if maxLabel in self.candidates:
						choice = self.deriveChoiceFromBaseLabel(maxLabel)

						if choice:
							self.setSolutionFromBaseLabel(choice, maxLabel)
							return True

				infomsg(f"{node} has no parent and ambiguous constraints {renderLabelSet('max candidates', max)}")
				return

			labels = Classification.createLabelSet()
			for neigh in node.upperNeighbors:
				if neigh.placement is None:
					infomsg(f"Why on earth does {neigh} not have a placement object?")
					continue
				if neigh.placement.label is None:
					return

				labels.add(neigh.placement.label)

			# xxx

		# if a build produces exactly one package, we do not have to consider any
		# sibling constraints. Just place it
		# Returns True if the decision was final
		def onlyChildCheck(self):
			if self.candidates is None:
				infomsg(f"{self.node} has no siblings and can be placed anywhere. Please provide a hint in the configuration")
				return True

			candidates = self.preferences.filterCandidates(self.candidates)
			best = self.labelOrder.maxima(candidates)

			if not best:
				infomsg(f"{self.node}: we blew it; list of candidates reduced to empty set")
				return True

			if len(best) == 1:
				label = next(iter(best))
				infomsg(f"{self.node} has no siblings; best candidate is {label}")
				self.setSolution(label)
				return True

			return False

		def deriveChoiceFromBaseLabel(self, baseLabel):
			candidates = self.candidates

			if self.trace:
				infomsg(f"{self}: trying to derive from {baseLabel}")
				# displayLabelSetFull(candidates, indent = "   ")

			if candidates is None or baseLabel in candidates:
				# if the node can be placed anywhere, or if the base label is
				# a candidate, choose the base label
				if self.trace:
					infomsg(f"{self}: {baseLabel} is a valid candidate")
				return baseLabel
			else:
				# filter those candidates that have the chosen base label or parent
				if baseLabel.parent is None:
					# This is truly a base label
					candidates = Classification.createLabelSet(filter(lambda label: label.baseLabel == baseLabel, candidates))
				else:
					# We're trying to derive from a sibling package that has been placed in, say "@GraphicsLibraries+glib2"
					candidates = Classification.createLabelSet(filter(lambda label: label.parent == baseLabel, candidates))

				if self.trace:
					infomsg(f"### {self} candidates={' '.join(map(str, candidates))}")

				if not candidates:
					return None

				# Reduce [@Foo-doc, @Foo+flavor1-doc, @Foo+flavor2-doc, ...] to @Foo-doc
				if len(candidates) > 1:
					minimum = self.labelOrder.minimumOf(candidates)
					if minimum is not None:
						return minimum

					# If the package has not been labelled for a specific purpose, check if
					# we get better results by hiding all candidates that do have one
					if self.purpose is None:
						generic = Classification.createLabelSet(filter(lambda label: label.purposeName == None, candidates))
						if generic:
							candidates = generic

#				if len(candidates) > 1:
#					candidates = self.labelOrder.maxima(candidates)

				if len(candidates) > 1:
					if self.trace:
						infomsg(f"{self.node} is still ambiguous [{' '.join(map(str, candidates))}]")
					return None

				choice = next(iter(candidates))
			return choice

		def solveToBaseLabel(self, baseLabel):
			if self.label:
				return True

			choice = self.deriveChoiceFromBaseLabel(baseLabel)
			if choice is None:
				return False

			self.setSolutionFromBaseLabel(choice, baseLabel)
			return True

	class TentativeBuildPlacement:
		def __init__(self, name, labelOrder, preferences):
			self.name = name
			self.labelOrder = labelOrder
			self.preferences = preferences
			self.constraints = PotentialClassification.PlacementConstraints()
			self.packageCount = 0

			self.children = []
			self.packageDict = {}

			# Do NOT initialize self._commonBaseLabels; the commonBaseLabels
			# property relies on this attribute not being present
			# Do NOT initialize self._compatibleBaseLabels either

			# FIXME: rename to baseLabelSolutions
			self.baseLabelSolutions = {}

			self.solvingBaseLabel = None

			self.trace = False

		def __str__(self):
			return self.name

		def addPackagePlacement(self, pkg, packagePlacement):
			self.children.append(packagePlacement)
			self.packageDict[pkg] = packagePlacement
			self.packageCount += 1

			if packagePlacement.trace:
				self.trace = True

		@property
		def isFinal(self):
			return all(placement.isFinal for placement in self.children)

		@property
		def isSolved(self):
			return all(placement.isSolved for placement in self.children)

		@property
		def solved(self):
			return list(filter(lambda p: p.isSolved, self.children))

		@property
		def unsolved(self):
			return list(filter(lambda p: not p.isSolved, self.children))

		@property
		def numPackages(self):
			return self.packageCount

		@property
		def numSolved(self):
			return self.packageCount - len(self.unsolved)

		def addDefinitivePlacement(self, pkg, node, label):
			component = label.componentName
			if component is not None:
				self.constraints.addValidComponent(component)

			# what is this supposed to do?
			# self.constraints.addValidBaseLabel(label.baseFlavors)

			# the node may represent a collapsed cycle, in which case node.placement may already have
			# been set. Just adopt the placement that is already there
			if node.placement is None:
				node.placement = PotentialClassification.DefinitivePackagePlacement(self.labelOrder, node)

			self.addPackagePlacement(pkg, node.placement)
			return node.placement

		def addTentativePlacement(self, pkg, node):
			# the node may represent a collapsed cycle, in which case node.placement may already have
			# been set. Just adopt the placement that is already there
			if node.placement is None:
				node.placement = PotentialClassification.TentativePackagePlacement(self.labelOrder, node, self.preferences)
				if pkg.label is not None:
					node.placement.applyFlavorOrPurpose(pkg.label)
					if pkg.label.type in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
						node.placement.autoLabel = pkg.label

			self.addPackagePlacement(pkg, node.placement)

			if node.upperNeighbors:
				self.constrainedAbove = True
			if node.lowerNeighbors:
				self.constrainedBelow = True

			if pkg.trace:
				node.placement.trace = True

			return node.placement

		def applyConstraints(self):
			for packagePlacement in self.unsolved:
				packagePlacement.applyConstraints(self.constraints)

		def solveTrivialCases(self):
			for packagePlacement in self.unsolved:
				packagePlacement.trivialChecks()

# This is too aggressive; if at all, this should be a last resort
#			if self.packageCount == 1 and not self.isFinal:
#				packagePlacement = self.unsolved[0]
#				packagePlacement.onlyChildCheck()

			return self.isFinal

		class BaseLabelSolution:
			def __init__(self, baseLabel):
				self.baseLabel = baseLabel
				self.placements = []

			def __str__(self):
				return self.baseLabel.name

			def add(self, packagePlacement, label):
				self.placements.append((packagePlacement, label))

			def __iter__(self):
				return iter(self.placements)

			@property
			def isPureBaseLabelSolution(self):
				# return true iff this solution uses only @Label or @Label-purpose,
				# and no @Label+flavor* style labels
				if not self.placements:
					return False

				for (packagePlacement, label) in self.placements:
					if label.flavorName:
						return False
				return True

		def canSolveUsingBaseLabel(self, baseLabel):
			try:
				return self.baseLabelSolutions[baseLabel]
			except:
				pass

			result = self.BaseLabelSolution(baseLabel)
			potentialSolution = []
			for packagePlacement in self.unsolved:
				choice = packagePlacement.deriveChoiceFromBaseLabel(baseLabel)
				if choice is None:
					if self.trace:
						infomsg(f"{self}: incompatible base label {baseLabel} - no candidate for {packagePlacement}")
					self.baseLabelSolutions[baseLabel] = None
					return None

				result.add(packagePlacement, choice)

			self.baseLabelSolutions[baseLabel] = result
			return result

		def tryToSolveUsingBaseLabel(self, baseLabel, desc):
			infomsg(f"{self}: try to solve using {desc} {baseLabel}")

			potentialSolution = self.canSolveUsingBaseLabel(baseLabel)
			if not potentialSolution:
				return False

			for packagePlacement, choice in potentialSolution:
				packagePlacement.setSolutionFromBaseLabel(choice, baseLabel) 

			self.solvingBaseLabel = baseLabel
			return True

		# look for packages that have been labelled for a flavor with a defaultlabel or a list
		# of preferredlabels.
		# If so, check whether this could be a good base label for placing the entire package.
		# Do this only for packages that have none of their rpms labelled yet
		#
		# In order for this heuristic to deliver sane results, we ignore builds that have
		# packages without autolabel (for example, a libfoobar and some python3xx-foobar binding).
		# However, we do want to accept builds that have different autolabels as long as there's
		# a unique preferredLabel (for example, some python3xx-foobar module plus a -doc package
		# that goes along).
		def solveDefaultBaseLabel(self):
			if self.solved:
				return

			autoLabel = None
			for packagePlacement in self.unsolved:
				if packagePlacement.autoLabel is None:
					return False

				if packagePlacement.autoLabel.preferredLabels:
					if autoLabel is None:
						autoLabel = packagePlacement.autoLabel
					elif autoLabel is not packagePlacement.autoLabel:
						infomsg(f"{self} has packages with different auto labels {autoLabel} and {packagePlacement.autoLabel}")
						return False

			if autoLabel is None or not autoLabel.preferredLabels:
				return False

			for preferredLabel in autoLabel.preferredLabels:
				if self.tryToSolveUsingBaseLabel(preferredLabel, "preferred base label"):
					return True

			return False

		@property
		def commonBaseLabels(self):
			try:
				return self._commonBaseLabels
			except:
				pass

			commonBaseLabels = None
			for packagePlacement in self.children:
				commonBaseLabels = intersectSets(commonBaseLabels, packagePlacement.baseLabels)

			# Filter common base labels by preference
			# commonBaseLabels = self.preferences.filterCandidates(commonBaseLabels)

			self._commonBaseLabels = commonBaseLabels
			return self._commonBaseLabels

		@property
		def compatibleBaseLabels(self):
			try:
				return self._compatibleBaseLabels
			except:
				pass

			commonBaseLabels = self.commonBaseLabels
			self._compatibleBaseLabels = Classification.createLabelSet()

			if commonBaseLabels is None:
				return self._compatibleBaseLabels

			if not commonBaseLabels:
				infomsg(f"{self} has no common base labels");
				return self._compatibleBaseLabels

			infomsg(f"{self} has {renderLabelSet('common base labels', commonBaseLabels)}");
			for baseLabel in commonBaseLabels:
				if self.canSolveUsingBaseLabel(baseLabel):
					if self.trace:
						infomsg(f"   + {baseLabel}")
					self._compatibleBaseLabels.add(baseLabel)
				else:
					if self.trace:
						infomsg(f"   - {baseLabel}")

			if not self._compatibleBaseLabels:
				infomsg(f"{self} has common base labels, but none of them can solve");

			return self._compatibleBaseLabels

		@property
		def compatiblePureBaseLabels(self):
			try:
				return self._compatiblePureBaseLabels
			except:
				pass

			baseLabels = self.compatibleBaseLabels
			if not baseLabels:
				self._compatiblePureBaseLabels = None
			else:
				self._compatiblePureBaseLabels = set()
				for label in baseLabels:
					if self.baseLabelSolutions[label].isPureBaseLabelSolution:
						self._compatiblePureBaseLabels.add(label)

			return self._compatiblePureBaseLabels

		# we're dealing with several packages; see whether they share any common base label(s)
		# and try to determine the "best" choice
		def solveCommonBaseLabel(self):
			goodLabels = self.compatibleBaseLabels

			if not goodLabels:
				return False

			if len(goodLabels) == 1:
				bestLabel = next(iter(goodLabels))
			else:
				bestLabel = self.labelOrder.maximumOf(goodLabels)

			if bestLabel:
				return self.tryToSolveUsingBaseLabel(bestLabel, "common base label")

			infomsg(f"{self}: found several compatible base labels: {renderLabelSet('good', goodLabels)}")
			return False

		# for all the children that have been placed so far, loop over their base
		# labels and see if there's a common maximum. If so, try to place all remaining
		# packages with this base label
		def solveCompatibleBaseLabel(self):
			if not self.solved:
				return False

			compatibleBaseLabels = Classification.createLabelSet()
			for placement in self.solved:
				baseLabel = placement.label.baseLabel
				if self.canSolveUsingBaseLabel(baseLabel):
					compatibleBaseLabels.add(baseLabel)

			compatibleBaseLabels = self.preferences.filterCandidates(compatibleBaseLabels)
			self.compatibleBaseLabels.update(compatibleBaseLabels)

			infomsg(f"Trying to solve {self} using all base labels {' '.join(map(str, compatibleBaseLabels))}")

			maxBaseLabel = self.labelOrder.maximumOf(compatibleBaseLabels)
			if maxBaseLabel is None:
				if True:
					maxes = self.labelOrder.maxima(compatibleBaseLabels)
					infomsg(f"   no single maximum label; max={renderLabelSet('maxima', maxes)}")
				return False

			infomsg(f"    reduced list of all base labels to {maxBaseLabel}")
			return self.tryToSolveUsingBaseLabel(maxBaseLabel, "max base label")

		# For a package like libfoo-devel, remove the suffix
		# This assumes that there is only one matching package suffix; as soon as someone
		# starts introducing ambiguous suffixes (like -32bit-devel vs -devel), we're in trouble
		def removePurposeSuffixFromPackageName(self, pkg, purpose):
			for suffix in purpose.packageSuffixes:
				if pkg.name.endswith(suffix):
					return pkg.name[:-len(suffix)].rstrip('-')
			return None

		# place systemd-mini-devel close to systemd-mini
		def solvePurposeRelativeToSibling(self, classificationScheme):
			# if we have no solved siblings yet, don't even bother
			if self.numSolved == 0:
				return False

			namesToPlacements = {}
			toBeExamined = []
			for pkg, packagePlacement in self.packageDict.items():
				if packagePlacement.label is not None:
					namesToPlacements[pkg.name] = packagePlacement
					label = packagePlacement.label

					# When we've already placed -devel, this rule helps placing -devel-static next to it
					if label.isPurpose:
						# find the underlying purpose label (which has the suffixes)
						purpose = classificationScheme.getLabel(label.purposeName)

						# then, see if the package name is of the form $stem-$suffix, and if
						# so, remove the suffix
						baseName = self.removePurposeSuffixFromPackageName(pkg, purpose)
					else:
						baseName = pkg.name

					if baseName is not None:
						namesToPlacements[baseName] = packagePlacement
						infomsg(f"    {baseName} -> {packagePlacement}")

						# FIXME: this relies on the SUSE lib package naming convention
						# shorten librsvg-2-2 to librsvg
						if baseName.startswith("lib"):
							baseName = baseName.rstrip("-0123456789_")
							namesToPlacements[baseName] = packagePlacement
							infomsg(f"    {baseName} -> {packagePlacement}")

				elif packagePlacement.purpose and packagePlacement.purpose.packageSuffixes:
					toBeExamined.append((pkg, packagePlacement))

			if not toBeExamined:
				return False

			obsPackageName = self.name

			for pkg, packagePlacement in toBeExamined:
				purpose = packagePlacement.purpose
				infomsg(f"{pkg} is a {purpose} package; look for favorite siblings")

				baseName = self.removePurposeSuffixFromPackageName(pkg, purpose)
				if baseName is None:
					continue

				transformedNames = [baseName]
				if baseName.startswith("lib"):
					baseName = baseName.rstrip("-0123456789")
					transformedNames.append(baseName)

					# strip off "lib" prefix
					transformedNames.append(baseName[3:])
				else:
					# try with lib prefix
					transformedNames.append("lib" + baseName)

				# try with the obs package name prefixed
				# This is the "lex ffmpeg" where we hav libfoo123 correspond to ffmpeg4-libfoo-devel
				if baseName.startswith(obsPackageName):
					strippedName = baseName[len(obsPackageName):].lstrip("-")
					if strippedName:
						transformedNames.append(strippedName)

				for tryName in transformedNames:
					favoriteSibling = namesToPlacements.get(tryName)
					if favoriteSibling is not None:
						infomsg(f"       try {tryName} -> {favoriteSibling}")
						break

					infomsg(f"       try {tryName} -> [no match]")

				if favoriteSibling is None:
					continue

				infomsg(f"    {pkg} favorite sibling={favoriteSibling}")
				label = favoriteSibling.label
				if label.isPurpose:
					label = label.parent

				choice = packagePlacement.deriveChoiceFromBaseLabel(label)
				if choice is None:
					infomsg(f"{purpose} package {pkg} has favorite sibling {favoriteSibling}, but {label} is not a good base label for it")
					desiredCandidate = label.getObjectPurpose(purpose.name)
					if desiredCandidate is not None:
						closure = self.labelOrder.downwardClosureFor(desiredCandidate)
						for neigh in packagePlacement.node.lowerNeighbors:
							if desiredCandidate in neigh.lowerCone:
								continue
							infomsg(f"     {packagePlacement} requires {neigh}, which is not in scope of {desiredCandidate}")
					continue

				infomsg(f"{pkg} is placed in {choice} (based on favorite sibling {favoriteSibling} and purpose {purpose})")
				packagePlacement.setSolution(choice)

			return self.isFinal

		def solveWithConstraints(self, constrainingSet):
			# see if we have any base labels that produce a solution that
			# does not require @Foo+flavor style labes, but requires only
			# @Foo and @Foo-purpose labels
			baseLabels = self.compatiblePureBaseLabels

			if not baseLabels:
				# Nope, try all base labels
				baseLabels = self.compatibleBaseLabels

				if not baseLabels:
					return False

			baseLabels = baseLabels.intersection(constrainingSet)

			display = renderLabelSet("constrained base labels", baseLabels)
			infomsg(f"Trying to solve {self} using {display}")

			if not baseLabels:
				return False

			# it does not make sense to look at the maximum of these constrained base labels.
			# in way too many cases, the maximum ends up being @BaseBuildEnv, resulting
			# in countless bad decisions
			if len(baseLabels) != 1:
				return False

			bestLabel = next(iter(baseLabels))
			return self.tryToSolveUsingBaseLabel(bestLabel, "source constrained base label")

		def propagateSolvers(self):
			for packagePlacement in self.children:
				packagePlacement.propagateSolvers()

		def reportRemaining(self):
			remaining = self.unsolved

			infomsg(f" - {self}: {self.numSolved}/{self.numPackages} solved; {len(remaining)} remain")
			for packagePlacement in self.children:
				if packagePlacement.label:
					infomsg(f"    + {packagePlacement} (solved); labelled as {packagePlacement.label}")
					continue

				status = "unsolved"
				if packagePlacement.failed:
					status = "FAILED"

				if packagePlacement.constrainedAbove:
					extra = renderLabelSet("candidates", packagePlacement.candidates)

					maxBaseLabels = None
					if packagePlacement.baseLabels is not None:
						maxBaseLabels = self.labelOrder.maxima(packagePlacement.baseLabels)
					extra2 = renderLabelSet("max base labels", maxBaseLabels)
				else:
					extra = "nothing requires this package"

					minBaseLabels = None
					if packagePlacement.baseLabels is not None:
						minBaseLabels = self.labelOrder.minima(packagePlacement.baseLabels)
					extra2 = renderLabelSet("min base labels", minBaseLabels)

				infomsg(f"    - {packagePlacement} ({status}); {extra}")
				infomsg(f"         {extra2}")

			if self.baseLabelSolutions:
				extra = renderLabelSet('compatible base labels', sorted(map(str, self.baseLabelSolutions.keys())))
				infomsg(f"   {extra}")

	def definePreference(self, preferredName, otherNames):
		def getLabel(name):
			label = self.classificationScheme.getLabel(name)
			if label is None:
				raise Exception(f"Unknown label {name}")
			return label

		preferredLabel = getLabel(preferredName)
		others = set(map(getLabel, otherNames))
		if preferredLabel in others:
			others.remove(preferredLabel)

		self._preferences.add(preferredLabel, others)

	def createBuildPlacement(self, buildInfo):
		buildPlacement = self.TentativeBuildPlacement(buildInfo.name, self.labelOrder, self._preferences)

		# First, loop over all packages that this build produces, and add them to the
		# build placement
		for pkg in buildInfo:
			node = self.getPackageNode(pkg)
			if node.solution is not None:
				buildPlacement.addDefinitivePlacement(pkg, node, node.solution)
			else:
				buildPlacement.addTentativePlacement(pkg, node)

		# Then, apply constraints (right now, just the valid component name(s))
		buildPlacement.applyConstraints()
		return buildPlacement

	def solveBuildPlacementStage1(self, tentativePlacement):
		if tentativePlacement.isFinal:
			return

		infomsg(f"{tentativePlacement}: {tentativePlacement.numSolved}/{tentativePlacement.numPackages} solved")

		# it may be better to have the indent handling use "with"
		with loggingFacade.temporaryIndent(3):
			success = \
				tentativePlacement.solveTrivialCases() or \
				tentativePlacement.solveDefaultBaseLabel() or \
				tentativePlacement.solveCommonBaseLabel() or \
				tentativePlacement.solveCompatibleBaseLabel() or \
				tentativePlacement.solvePurposeRelativeToSibling(self.classificationScheme)

			if tentativePlacement.isFinal:
				infomsg(f"{tentativePlacement}: completely solved")
				return True

			infomsg(f"{tentativePlacement}: remains to be solved")

		return False

	# For a solved build placement, this tries to figure out the buildConfig and component
	# In addition, if the source package for this build has any build requirements that could
	# not be solved, attach a solver to it that tries to use the the buildConfig to find a
	# unique solving label
	def postProcessSolvedBuildPlacement(self, buildPlacement, build, solverFactory):
		if len(build.sources) != 1:
			warnmsg(f"{build} has {len(build.sources)} sources")

		buildLabel = None
		componentLabel = None
		if buildPlacement.solvingBaseLabel:
			buildLabel = buildPlacement.solvingBaseLabel.buildConfig
			componentLabel = buildLabel.baseLabel
		else:
			solutions = [p.label for p in buildPlacement.solved]

			buildConfigs = set(label.buildConfig for label in solutions)
			if len(buildConfigs) == 1:
				buildLabel = next(iter(buildConfigs))

			components = set(label.baseLabel for label in buildConfigs)
			if len(components) == 1:
				componentLabel = next(iter(components))

		if buildLabel and not componentLabel:
			componentLabel = buildLabel.baseLabel

		if componentLabel is None:
			warnmsg(f"{build} unable to determine unique component label")
			return False

		if buildLabel is None:
			warnmsg(f"{build} unable to determine unique build label")
			return False

		# buildPlacement.setSolution(buildLabel, componentLabel)
		if buildPlacement.trace:
			infomsg(f"{build} will be placed in component {componentLabel}")

		with loggingFacade.temporaryIndent(3):
			buildPlacement.buildConfig = buildLabel
			buildPlacement.componentLabel = componentLabel

			for rpm in build.sources:
				node = self.solvingTree.getPackage(rpm)
				if node is None:
					continue

				for lower in node.lowerNeighbors:
					if lower.placement is None:
						warnmsg("{lower} has no placement handle")
						continue

					solver = solverFactory.sourceHintsSolver(buildLabel)
					lower.placement.addSolver(solver)

			return True

	# We get here when we were unable to solve this OBS package during the first stage.
	# After stage 1, we look at the build requirements of solved packages, and use these
	# to put additional constraints on them. Such as the component (aka OBS project)
	def solveBuildPlacementStage2(self, buildPlacement):
		solvers = set()
		for packagePlacement in buildPlacement.unsolved:
			solvers.update(packagePlacement.solvers)

		if not solvers:
			return False

		infomsg(f"{buildPlacement}: {buildPlacement.numSolved}/{buildPlacement.numPackages} solved")
		with loggingFacade.temporaryIndent(3):
			for solver in solvers:
				infomsg(f"{buildPlacement}: trying to solve using {solver}")

				with loggingFacade.temporaryIndent(3):
					success = solver.tryToSolve(buildPlacement)

				if success:
					infomsg(f"{buildPlacement}: successfully solved")
					return True

			infomsg(f"{buildPlacement} remains to be solved")

		return False

	def constrainPackagesWithAutomaticLabels(self, order):
		flavorConstrained = {}
		purposeConstrained = {}

		flavorConstrained[None] = set()
		purposeConstrained[None] = set()
		for label in self.classificationScheme.allLabels:
			if label.type == Classification.TYPE_AUTOFLAVOR:
				flavorConstrained[label.name] = set()
			elif label.type == Classification.TYPE_PURPOSE:
				purposeConstrained[label.name] = set()

		for label in self.classificationScheme.allLabels:
			if label.type == Classification.TYPE_BINARY:
				cons = flavorConstrained.get(label.flavorName)
				if cons is not None:
					cons.add(label)

				purposeConstrained[label.purposeName].add(label)

		packagesToBeConstrained = []
		for node in self._packages.values():
			constrained = None
			for pkg in node.packages:
				label = pkg.label
				if label is None:
					continue

				if label.type == Classification.TYPE_BINARY:
					cons = flavorConstrained.get(label.flavorName)
					if cons is not None:
						constrained = intersectSets(constrained, cons)

					const = purposeConstrained[label.purposeName]
					constrained = intersectSets(constrained, cons)

			if constrained is None:
				# no further constraints
				pass

			if not constrained:
				errormsg(f"{node} not constraining the candidates")
				continue

			node.constrainCandidatesFurther(constrained)

	def reportUnsolved(self, placements):
		header = "Packages that have not been placed"
		numSolved = 0
		numSolvedPackages = 0
		totalPackages = 0
		for buildPlacement in placements:
			totalPackages += buildPlacement.numPackages
			numSolvedPackages += buildPlacement.numSolved

			if buildPlacement.isSolved:
				numSolved += 1
				continue

			if header is not None:
				infomsg(header)
				header = None

			buildPlacement.reportRemaining()

		infomsg("")
		infomsg(f"Solved {numSolved}/{len(placements)} builds")
		infomsg(f"Solved {numSolvedPackages}/{totalPackages} packages")
		infomsg("")

	def solve(self):
		infomsg("### PLACEMENT STAGE 1 ###")

		timing = ExecTimer()

		placements = []
		placementMap = {}
		for build in self.solvingTree.allBuilds:
			debugmsg(f"Create build placement for {build}")
			placement = self.createBuildPlacement(build)
			placements.append(placement)
			placementMap[build] = placement

		infomsg(f"Created placement objects; {timing} elapsed")
		infomsg("")

		if False:
			for node in self.solvingTree.topDownTraversal():
				buildPlacement = node.placement
				if buildPlacement is None or buildPlacement.label is not None:
					continue

				buildPlacement.tryToPlaceTopDown(node)

		for placement in placements:
			self.solveBuildPlacementStage1(placement)

		infomsg("### PLACEMENT STAGE 2 ###")
		solverFactory = SolverFactory(self)
		for build in self.solvingTree.topDownBuildTraversal():
			buildPlacement = placementMap[build]

			if not buildPlacement.isFinal:
				self.solveBuildPlacementStage2(buildPlacement)

			if buildPlacement.isFinal:
				self.postProcessSolvedBuildPlacement(buildPlacement, build, solverFactory)
			else:
				# if we were still unable to place this build, we should at least propagate
				# any solvers to unsolved lower neighbors
				buildPlacement.propagateSolvers()

		self.reportUnsolved(placements)

		# FIXME: should these two loops actually move into SolvingTree?
		result = ClassificationResult(self.solvingTree._order)
		for node in self.solvingTree.bottomUpTraversal():
			if node.placement:
				node.placement.reportVerdict(node, result)

		for build in self.solvingTree.builds:
			# We do not place the builds in components yet
			label = None
			result.labelOneBuild(build.name, label, build.packages, build.sources)

		return result

	def baseLabelsForSet(self, labels):
		if labels is not None:
			return Classification.baseLabelsForSet(labels)

	def __iter__(self):
		for pkg, interval in self._packages.items():
			yield pkg, self.getBestCandidate(interval)

	def getBestCandidate(self, interval):
		candidates = interval.candidates
		if not candidates:
			return None

		return self._order.minimumOf(candidates)

