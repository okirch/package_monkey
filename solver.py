##################################################################
#
# Labelling solver algorithm, based on SolvingTree
#
#
#
#
#
##################################################################
from util import ExecTimer, TimedExecutionBlock
from util import IndexFormatterTwoLevels
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from filter import Classification
from filter import ClassificationResult
from buildspec import BuildSpecFactory
from functools import reduce
from profile import profiling

def intersectSets(a, b):
	if a is None:
		return b
	elif b is None:
		return a
	return a.intersection(b)

def generalSetContains(elem, gset):
	return (gset is None) or (elem in gset)

def renderLabelSet(name, labels, max = 6):
	if labels is None:
		return f"[unconstrained {name}]"

	if not labels:
		return f"[no {name}]"

	if len(labels) >= max:
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
	precedence = 100

class SourceHintsSolver(Solver):
	precedence = 20

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

class GlobalPurposeSolver(Solver):
	precedence = 15

	def __init__(self, purposeLabel, potentialClassification):
		self.purposeLabel = purposeLabel
		self.closure = potentialClassification.labelOrder.downwardClosureForSet(purposeLabel.runtimeRequires)

		if not self.closure:
			warnmsg(f"Created {self} with empty runtime requirements closure")

	def __str__(self):
		return f"component purpose solver for {self.purposeLabel}"

	def tryToSolve(self, buildPlacement):
		if buildPlacement.numSolved == 0:
			return

		component = None

		for packagePlacement in buildPlacement.stage2:
			if packagePlacement.isFinal:
				continue
			if not packagePlacement.isComponentLevelPurpose:
				continue

			# infomsg(f"   inspect {packagePlacement}")
			# infomsg(f"   purpose {packagePlacement.purpose}")

			if component is None:
				component = self.getComponentForBuild(buildPlacement)
				if component is None:
					infomsg(f"   {self} cannot solve {packagePlacement}: no unique component")
					continue

			purposeName = packagePlacement.purpose.name
			globalPurpose = component.globalPurposeLabel(purposeName)
			if globalPurpose is None:
				infomsg(f"   {self} cannot solve {packagePlacement}: {component} does not specify a global label for purpose {purposeName}")
				continue

			if packagePlacement.node.lowerCone is not None and globalPurpose not in packagePlacement.node.lowerCone:
				infomsg(f"   trying to place {packagePlacement} into {globalPurpose}, but it's not a valid candidate")
				continue

			infomsg(f"{packagePlacement} is placed into {globalPurpose} (global {purposeName} label for component {component})")
			packagePlacement.setSolution(globalPurpose)

		return buildPlacement.isFinal

	def getComponentForBuild(self, buildPlacement):
		result = None
		for placement in buildPlacement.solved:
			label = placement.label
			if label is None:
				continue

			label = label.componentLabel
			if result is label:
				continue

			if result is not None:
				return None

			result = label
		return result


class APISolver(Solver):
	precedence = 10

	def __init__(self, purposeLabel, potentialClassification):
		self.purposeLabel = purposeLabel
		self.classificationScheme = potentialClassification.classificationScheme

	def __str__(self):
		return f"API solver"

	def pkgIsLibrary(self, pkg):
		return pkg.name.startswith("lib") and pkg.name[-1].isdigit()

	def tryToSolve(self, buildPlacement):
		if buildPlacement.numSolved == 0:
			return

		candidates = Classification.createLabelSet()
		altCandidates = Classification.createLabelSet()
		for pkg, packagePlacement in buildPlacement.packageDict.items():
			if packagePlacement.label is not None and \
			   packagePlacement.label.purposeName is None and \
			   packagePlacement.label.correspondingAPI:
				if self.pkgIsLibrary(pkg):
					candidates.add(packagePlacement.label.correspondingAPI)
				else:
					altCandidates.add(packagePlacement.label.correspondingAPI)

		if not candidates:
			if len(altCandidates) == 0:
				infomsg(f"  {buildPlacement}: no siblings that have an API")
				return
			candidates = altCandidates

		infomsg(f"{buildPlacement} candidates APIs: {' '.join(map(str, candidates))}")
		for packagePlacement in buildPlacement.stage2:
			if packagePlacement.isFinal:
				continue
			if not packagePlacement.isComponentLevelPurpose:
				continue

			targets = intersectSets(candidates, packagePlacement.candidates)

			if not targets:
				infomsg(f"  {packagePlacement}: no compatible APIs")
				continue

			if len(targets) > 1:
				infomsg(f"  {packagePlacement}: ambiguous APIs {' '.join(map(str, targets))}")
				continue

			apiLabel = next(iter(targets))

			infomsg(f"{packagePlacement} is placed into {apiLabel} (API label for sibling library package(s))")
			packagePlacement.setSolution(apiLabel)

		return

	def getComponentForBuild(self, buildPlacement):
		result = None
		for placement in buildPlacement.solved:
			label = placement.label
			if label is None:
				continue

			label = label.componentLabel
			if result is label:
				continue

			if result is not None:
				return None

			result = label
		return result


class SolverFactory(object):
	def __init__(self, potentialClassification):
		self.potentialClassification = potentialClassification
		self.componentSolvers = {}
		self.globalPurposeSolvers = {}
		self.apiSolvers = {}

	def sourceHintsSolver(self, label):
		try:
			return self.componentSolvers[label]
		except:
			pass

		solver = SourceHintsSolver(label, self.potentialClassification)
		self.componentSolvers[label] = solver
		return solver

	def globalPurposeSolver(self, label):
		try:
			return self.globalPurposeSolvers[label]
		except:
			pass

		solver = GlobalPurposeSolver(label, self.potentialClassification)
		self.globalPurposeSolvers[label] = solver
		return solver

	def apiSolver(self, label):
		try:
			return self.apiSolvers[label]
		except:
			pass

		solver = APISolver(label, self.potentialClassification)
		self.apiSolvers[label] = solver
		return solver

##################################################################
# Helper class that allows you to look up "favorite" siblings
# Use case:
#  - you have an OBS package x-foobar that builds several
#    binaries: libfoo1, python-foo, foo-utils, and foo-devel
#  - Assuming we're able to place libfoo1 in @FrobnicationLibraries
#    our goal is to place python-foo in @FrobnicationLibraries+python
#    and foo-utils near but not necessarily the same place
##################################################################
class FavoriteSiblingMap(object):
	def __init__(self, buildPlacement, classificationScheme):
		self.obsPackageName = buildPlacement.name
		self.classificationScheme = classificationScheme
		self.labelOrder = buildPlacement.labelOrder
		self.namesToPlacements = {}

		for pkg, packagePlacement in buildPlacement.packageDict.items():
			if packagePlacement.label is not None:
				self.inspect(pkg, packagePlacement)

	def inspect(self, pkg, packagePlacement):
		self.namesToPlacements[pkg.name] = packagePlacement

		label = packagePlacement.label

		# When we've already placed -devel, this rule helps placing -devel-static next to it
		if label.isPurpose:
			# find the underlying purpose label (which has the suffixes)
			purpose = self.classificationScheme.getLabel(label.purposeName)

			# then, see if the package name is of the form $stem-$suffix, and if
			# so, remove the suffix
			baseName = self.removePurposeSuffixFromPackageName(pkg, purpose)
		else:
			baseName = pkg.name

		if baseName is None:
			return

		self.namesToPlacements[baseName] = packagePlacement
		infomsg(f"    {baseName} -> {packagePlacement}")

		# FIXME: this relies on the SUSE lib package naming convention
		# shorten librsvg-2-2 to librsvg
		if baseName.startswith("lib"):
			baseName = baseName.rstrip("-0123456789_")
			self.namesToPlacements[baseName] = packagePlacement
			infomsg(f"    {baseName} -> {packagePlacement}")

	def findFavoriteSibling(self, pkg, packagePlacement):
		purpose = packagePlacement.purpose

		if purpose is None or not purpose.packageSuffixes:
			return None

		infomsg(f"{pkg} is a {purpose} package; look for favorite siblings")

		baseName = self.removePurposeSuffixFromPackageName(pkg, purpose)
		if baseName is None:
			return None

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
		# This is the "lex ffmpeg" where we have libfoo123 correspond to ffmpeg4-libfoo-devel
		if baseName.startswith(self.obsPackageName):
			strippedName = baseName[len(self.obsPackageName):].lstrip("-")
			if strippedName:
				transformedNames.append(strippedName)

		for tryName in transformedNames:
			favoriteSibling = self.namesToPlacements.get(tryName)
			if favoriteSibling is not None:
				infomsg(f"       try {tryName} -> {favoriteSibling}")
				return favoriteSibling

			infomsg(f"       try {tryName} -> [no match]")

		return None

	def chooseLabel(self, pkg, packagePlacement, favoriteSibling):
		purpose = packagePlacement.purpose
		label = favoriteSibling.label
		if label.isPurpose:
			label = label.parent

		choice = None

		# if we're trying to place a devel package, and our favorite sibling has an API specified,
		# see if we can place the devel package in that API label
		# FIXME: maybe this code needs to go into deriveChoiceFromBaseLabel() directly
		if purpose.name == 'devel':
			api = label.correspondingAPI
			if api is not None:
				choice = packagePlacement.deriveChoiceFromBaseLabel(api)
				if choice is not None:
					infomsg(f"{pkg} is placed in {choice} (based on favorite sibling {favoriteSibling} and its API {api})")
					return choice

				infomsg(f"{favoriteSibling} is in {label} with API {api}, but this API is not a valid candidate")

		choice = packagePlacement.deriveChoiceFromBaseLabel(label)
		if choice is not None:
			infomsg(f"{pkg} is placed in {choice} (based on favorite sibling {favoriteSibling} and purpose {purpose})")
			return choice

		infomsg(f"{purpose} package {pkg} has favorite sibling {favoriteSibling}, but {label} is not a good base label for it")
		return None


	# For a package like libfoo-devel, remove the suffix
	# This assumes that there is only one matching package suffix; as soon as someone
	# starts introducing ambiguous suffixes (like -32bit-devel vs -devel), we're in trouble
	def removePurposeSuffixFromPackageName(self, pkg, purpose):
		for suffix in purpose.packageSuffixes:
			if pkg.name.endswith(suffix):
				return pkg.name[:-len(suffix)].rstrip('-')
		return None


##################################################################
# The actual solving algorithm
##################################################################
class PotentialClassification(object):
	def __init__(self, solvingTree):
		self.solvingTree = solvingTree

		mergeMap = self.MergeabilityMap(self.classificationScheme, self.labelOrder)
		catchAllMap = self.CatchAllLabelMap(self.classificationScheme, self.labelOrder)

		solverFactory = SolverFactory(self)

		self._preferences = self.PlacementPreferences(mergeMap, catchAllMap, solverFactory)

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
			self.validBaseLabels = None

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

	class PlacementPreferences(object):
		class Hint:
			def __init__(self, preferredLabel, others):
				self.preferred = preferredLabel
				self.others = Classification.createLabelSet(others)
				if preferredLabel in self.others:
					self.others.remove(preferredLabel)

		def __init__(self, mergeabilityMap, catchAllMap, solverFactory):
			self._mergeabilityMap = mergeabilityMap
			self._catchAllLabelMap = catchAllMap
			self._prefs = []
			self.solverFactory = solverFactory

		def add(self, preferredLabel, others):
			self._prefs.append(self.Hint(preferredLabel, others))

		def filterCandidates(self, candidates):
			if not self._prefs:
				return candidates

			for hint in self._prefs:
				if hint.preferred in candidates:
					candidates = candidates.difference(hint.others)

			return candidates

		def filterCandidatesForAutoFlavor(self, flavor, candidates):
			x = self._mergeabilityMap.getCandidatesForAutoFlavor(flavor)
			if x is None:
				raise Exception(f"mergeabilityMap lacks {flavor}")
			return candidates.intersection(x)

		def filterCandidatesForPurpose(self, purpose, candidates):
			return self._catchAllLabelMap.constrainCandidates(purpose, candidates)

	class MergeabilityMap(object):
		@profiling
		def __init__(self, classificationScheme, labelOrder):
			self._map = {}

			for flavor in classificationScheme.allAutoFlavors:
				self.addAutoFlavor(flavor, labelOrder)

		def addAutoFlavor(self, flavor, labelOrder):
			assert(flavor.type == Classification.TYPE_AUTOFLAVOR)

			candidates = Classification.createLabelSet()
			stop = Classification.createLabelSet()
			for label in labelOrder.topDownTraversal():
				if label in stop:
					continue

				below = labelOrder.downwardClosureFor(label)
				if flavor.runtimeRequires.issubset(below):
					candidates.add(label)
				else:
					# if the label couldn't satisfy all requirements of this auto flavor,
					# then the labels below it will not, either
					stop.update(below)

			self._map[flavor] = candidates

		def getCandidatesForAutoFlavor(self, flavor):
			return self._map.get(flavor)

	class CatchAllLabelMap(object):
		@profiling
		def __init__(self, classificationScheme, labelOrder):
			self._map = {}
			self._globalClosure = Classification.createLabelSet()

			purposes = {}
			for componentLabel in classificationScheme.allComponents:
				for purposeName in componentLabel.globalPurposeLabelNames:
					purposeLabel = purposes.get(purposeName)
					if purposeLabel is None:
						purposeLabel = classificationScheme.getLabel(purposeName)
						assert(purposeLabel and purposeLabel.type == Classification.TYPE_PURPOSE and purposeLabel.disposition == Classification.DISPOSITION_COMPONENT_WIDE)
						purposes[purposeName] = purposeLabel

					catchAllLabel = componentLabel.globalPurposeLabel(purposeName)
					if catchAllLabel is not None:
						self.add(purposeLabel, labelOrder.upwardClosureFor(catchAllLabel))

			# Check all binary labels that represent an API, and add them here
			develPurpose = classificationScheme.getLabel('devel')
			for binary in classificationScheme.allBinaryLabels:
				if binary.isAPI:
					self.add(develPurpose, labelOrder.upwardClosureFor(binary))

			for labelSet in self._map.values():
				self._globalClosure.update(labelSet)

		def add(self, purposeLabel, closure):
			labelSet = self._map.get(purposeLabel)
			if labelSet is None:
				labelSet = Classification.createLabelSet()
				self._map[purposeLabel] = labelSet
			labelSet.update(closure)

		def constrainCandidates(self, purposeLabel, candidates):
			if purposeLabel is None:
				if candidates is not None:
					candidates = candidates.difference(self._globalClosure)
				return candidates

			if purposeLabel.disposition != Classification.DISPOSITION_COMPONENT_WIDE:
				purposeName = purposeLabel.name
				return Classification.createLabelSet(filter(lambda label: label.purposeName == purposeName, candidates))

			labelSet = self._map.get(purposeLabel)
			if labelSet is None:
				return Classification.createLabelSet()

			return labelSet.intersection(candidates)

	class PackagePlacement(object):
		def __init__(self, labelOrder, node, label = None, stage = None):
			# maybe the node should refer to this placement, not the other way around
			self.labelOrder = labelOrder
			self.name = str(node)
			self.node = node
			self.label = label
			self.stage = stage
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
			if self.label is None:
				if self.candidates is None:
					baseLabels = None
				else:
					# This is bollocks. We need to recompute the set of candidates,
					# taking into account the current placement of lower and
					# upper neighbors.
					baseLabels = filter(lambda l: l.isBaseLabel, self.candidates)
					baseLabels = Classification.createLabelSet(baseLabels)

				for pkg in self.node.packages:
					result.addUnclassified(pkg, baseLabels)

				result.labelOnePackage(pkg, None, None)
				return

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
		@profiling
		def __init__(self, labelOrder, node):
			super().__init__(labelOrder, node, label = node.solution, stage = 0)

		@property
		def baseLabels(self):
			return Classification.createLabelSet((self.label.baseLabel, ))

		def addSolver(self, solver):
			pass

	class TentativePackagePlacement(PackagePlacement):
		@profiling
		def __init__(self, labelOrder, node, preferences):
			super().__init__(labelOrder, node, stage = 1)

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
		def isComponentLevelPurpose(self):
			return self.autoLabel and self.autoLabel.disposition == Classification.DISPOSITION_COMPONENT_WIDE

		@property
		def baseLabels(self):
			if self.candidates is None:
				return None

			return Classification.baseLabelsForSet(self.candidates)

		def constrainComponent(self, componentConstraint):
			if not self.candidates or not componentConstraint.componentLabel:
				return

			componentName = componentConstraint.componentLabel.name

			preferred = Classification.createLabelSet(filter(lambda label: label.componentName == componentName, self.candidates))
			if self.tracer:
				self.tracer.updateCandidates(self, preferred,
						before = self.candidates,
						msg = f"constrained by component {componentName}",
						indent = '   ')
			self.candidates = preferred

		def constrainGlobalLabels(self):
			if self.purpose is None:
				candidates = self.preferences.filterCandidatesForPurpose(None, self.candidates)

				if self.tracer:
					self.tracer.updateCandidates(self, candidates,
						before = self.candidates,
						msg = f"filter out global purpose labels",
						indent = '   ')
				self.candidates = candidates


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
		@profiling
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
					merged = self.preferences.filterCandidatesForAutoFlavor(label, candidates)
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
				self.purpose = label

				self.candidates = self.preferences.filterCandidatesForPurpose(self.purpose, candidates)
				if self.tracer:
					self.tracer.updateCandidates(self, self.candidates,
						before = candidates,
						msg = f"constrained by purpose {self.purpose.describe()}",
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
				if self.autoLabel:
					exactMatch = None
					if self.autoLabel.type == Classification.TYPE_PURPOSE:
						exactMatch = baseLabel.getObjectPurpose(self.autoLabel.name)
					elif self.autoLabel.type == Classification.TYPE_AUTOFLAVOR:
						exactMatch = baseLabel.getBuildFlavor(self.autoLabel.name)
					if exactMatch and generalSetContains(exactMatch, candidates):
						# infomsg(f"### {self} exact match {exactMatch}")
						return exactMatch

				# filter those candidates that have the chosen base label or parent
				if baseLabel.parent is None:
					# This is truly a base label
					candidates = Classification.createLabelSet(filter(lambda label: label.baseLabel == baseLabel, candidates))
				else:
					# We're trying to derive from a sibling package that has been placed in, say "@GraphicsLibraries+glib2"
					candidates = Classification.createLabelSet(filter(lambda label: label.parent == baseLabel, candidates))

				if self.trace:
					infomsg(f"### {self} candidates={' '.join(map(str, candidates))}")

				# if the package has an auto label (ie a purpose like 'doc' or a flavor like 'python310', whittle down the
				# list of candidates to those matching this purpose/flavor label
				if len(candidates) > 1 and self.autoLabel:
					name = self.autoLabel.name
					if self.autoLabel.type == Classification.TYPE_PURPOSE:
						match = filter(lambda cand: cand.purposeName == name, candidates)
					else:
						match = filter(lambda cand: cand.flavorName == name, candidates)
					match = Classification.createLabelSet(match)

					if self.trace:
						infomsg(f"### {self} constrained candidates by auto label {self.autoLabel} to {' '.join(map(str, match))}")

					candidates = match

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
		def __init__(self, build, labelOrder, preferences):
			self.name = build.name
			self.componentConstraint = build.componentConstraint
			self.labelOrder = labelOrder
			self.preferences = preferences
			self.constraints = PotentialClassification.PlacementConstraints()
			self.packageCount = 0

			self.children = []
			self.packageDict = {}

			# Do NOT initialize self._commonBaseLabels; the commonBaseLabels
			# property relies on this attribute not being present
			# Do NOT initialize self._compatibleBaseLabels either

			self.baseLabelSolutions = {}

			self.solvingBaseLabel = None
			self.solvingSourceLabel = None

			self.stage1 = []
			self.stage2 = []
			self.queue = self.stage1

			# used when the filter matches a build
			self._constrainedBaseLabels = None

			self._favoriteSiblingsMap = None

			self.trace = False

		def __str__(self):
			return self.name

		@profiling
		def addPackagePlacement(self, pkg, packagePlacement):
			self.children.append(packagePlacement)
			self.packageDict[pkg] = packagePlacement
			self.packageCount += 1

			if packagePlacement.trace:
				self.trace = True

			stage = packagePlacement.stage
			if stage == 1:
				self.stage1.append(packagePlacement)
			if stage == 2:
				self.stage2.append(packagePlacement)

			if packagePlacement.trace:
				infomsg(f"   {self} added {packagePlacement} stage {stage}")

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
			return list(filter(lambda p: not p.isSolved, self.queue))

		@property
		def numPackages(self):
			return self.packageCount

		@property
		def numSolved(self):
			return len(self.solved)

		@property
		def uniqueSourceProject(self):
			if self.solvingSourceLabel is not None:
				return self.solvingSourceLabel

			result = None
			for placement in self.children:
				label = placement.label
				if label is None:
					# errormsg(f"{self}: BAD MOJO: solver says this build has been placed, but {placement} has no label")
					continue

				label = label.sourceProject
				if result is label:
					continue

				if result is not None:
					errormsg(f"{self}: BAD MOJO: solver placed this package in two separate source projects - {result} and {label}")
					self.reportComponentPlacements()
					return None

				result = label

			self.solvingSourceLabel = result
			return result

		# verbose error reporting
		def reportComponentPlacements(self):
			components = {}
			for placement in self.children:
				label = placement.label
				if label is None or label.sourceProject is None:
					continue

				componentName = label.sourceProject.name
				if componentName not in components:
					components[componentName] = set()
				components[componentName].add(placement.name)

			for name in sorted(components.keys()):
				infomsg(f"  {name}: {' '.join(sorted(components[name]))}")

		@profiling
		def addDefinitivePlacement(self, pkg, node, label):
			component = label.componentLabel
			if component is not None:
				if not self.componentConstraint.setLabel(component, self.name):
					errormsg(f"BUG: we placed {pkg} in component {component} (via {label}) but that conflicts with given constraints {self.componentConstraint}")

			# what is this supposed to do?
			# self.constraints.addValidBaseLabel(label.baseFlavors)

			# the node may represent a collapsed cycle, in which case node.placement may already have
			# been set. Just adopt the placement that is already there
			if node.placement is None:
				node.placement = PotentialClassification.DefinitivePackagePlacement(self.labelOrder, node)

			if pkg.trace:
				node.placement.trace = True
			self.addPackagePlacement(pkg, node.placement)

			return node.placement

		@profiling
		def addTentativePlacement(self, pkg, node):
			# the node may represent a collapsed cycle, in which case node.placement may already have
			# been set. Just adopt the placement that is already there
			if node.placement is None:
				node.placement = PotentialClassification.TentativePackagePlacement(self.labelOrder, node, self.preferences)
				if pkg.label is not None:
					node.placement.applyFlavorOrPurpose(pkg.label)
					if pkg.label.type in (Classification.TYPE_AUTOFLAVOR, Classification.TYPE_PURPOSE):
						node.placement.autoLabel = pkg.label
						if pkg.label.disposition == Classification.DISPOSITION_COMPONENT_WIDE:
							node.placement.stage = 2

							# This package will not be touched in stage1, install solvers for stage2

							if pkg.label.name == 'devel':
								solver = self.preferences.solverFactory.apiSolver(pkg.label)
								node.placement.addSolver(solver)

							solver = self.preferences.solverFactory.globalPurposeSolver(pkg.label)
							node.placement.addSolver(solver)

			if pkg.trace:
				node.placement.trace = True
			self.addPackagePlacement(pkg, node.placement)

			if node.upperNeighbors:
				self.constrainedAbove = True
			if node.lowerNeighbors:
				self.constrainedBelow = True

			return node.placement

		@profiling
		def applyConstraints(self):
			for packagePlacement in self.unsolved:
				packagePlacement.constrainComponent(self.componentConstraint)
				packagePlacement.constrainGlobalLabels()

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
		def getAutoLabels(self):
			result = []
			for packagePlacement in self.unsolved:
				if packagePlacement.autoLabel is None:
					return None

				if packagePlacement.autoLabel.preferredLabels:
					autoLabel = packagePlacement.autoLabel
					if autoLabel not in result:
						result.append(autoLabel)

			return result

		def solveAutoflavorPreferredLabel(self):
			if self.solved:
				return

			if self._constrainedBaseLabels:
				return

			autoLabels = self.getAutoLabels()
			if not autoLabels:
				return False

			if len(autoLabels) == 1:
				autoLabel = autoLabels[0]
				for preferredLabel in autoLabel.preferredLabels:
					if self.tryToSolveUsingBaseLabel(preferredLabel, f"preferred label of {autoLabel}"):
						return True

			# If that fails, see if we're dealing with the Python case, where we have separate auto labels
			# for python310 and python311, which list different preferredLabels - but these preferred labels
			# are both instantiated from the same PythonXXX template
			infomsg(f"{self} has packages with different auto labels {renderLabelSet('auto', autoLabels)}")

			packageSolutions = []
			remaining = []
			failed = []

			validTemplates = None
			for packagePlacement in self.unsolved:
				if not packagePlacement.autoLabel.preferredLabels:
					remaining.append(packagePlacement)
					continue

				validChoices = []
				for pref in packagePlacement.autoLabel.preferredLabels:
					if pref in packagePlacement.candidates and pref.instanceOfTemplate:
						infomsg(f"   {packagePlacement} + {pref} template {pref.instanceOfTemplate}")
						validChoices.append(pref)
					else:
						infomsg(f"   {packagePlacement} - {pref} template {pref.instanceOfTemplate}")

				if not validChoices:
					infomsg(f"   no match for {packagePlacement}")
					failed.append(packagePlacement)
					continue

				packageSolutions.append((packagePlacement, validChoices))
				validTemplates = intersectSets(validTemplates, set(_.instanceOfTemplate for _ in validChoices))

			if failed:
				return False

			if not validTemplates:
				infomsg("   alas, no common templates found")
				return False

			packageChoices = []
			selectedTemplate = None

			for packagePlacement, validChoices in packageSolutions:
				myChoice = None
				for choice in validChoices:
					template = choice.instanceOfTemplate

					if selectedTemplate is not None:
						if template != selectedTemplate:
							continue
					else:
						if template not in validTemplates:
							continue
						selectedTemplate = template

					myChoice = choice
					break

				if myChoice is None:
					errormsg(f"Weird, failed to choose a label for {packagePlacement} (using template {selectedTemplate}")
					failed.append(packagePlacement)
				else:
					infomsg(f"   {packagePlacement} could go into {myChoice}")
					packageChoices.append((packagePlacement, myChoice))

			if remaining:
				# get the minimum base label that bounds all choices from above (eg if we placed the packages into
				# Python310 and Python311, see if there's a base label that combines these two.
				chosenLabels = set(label for (pp, label) in packageChoices)
				supremum = self.chooseUpperBoundingBaseLabel(chosenLabels)

				for packagePlacement in remaining:
					myChoice = self.handlePythonSiblingPackage(packagePlacement, packageChoices, supremum)

					if myChoice is not None:
						packageChoices.append((packagePlacement, myChoice))
					else:
						infomsg(f"   TBD: place {packagePlacement} autolabel {packagePlacement.autoLabel}")
						failed.append(packagePlacement)

			if failed:
				return False

			for packagePlacement, choice in packageChoices:
				packagePlacement.setSolution(choice)

			return True

		def chooseUpperBoundingBaseLabel(self, labelSet):
			supremum = None
			commonAbove = None
			for label in labelSet:
				commonAbove = intersectSets(commonAbove, self.labelOrder.upwardClosureFor(label))
			if commonAbove is not None:
				commonAbove = set(filter(lambda l: l.parent is None, commonAbove))
			if commonAbove:
				minima = self.labelOrder.minima(commonAbove)
				if len(minima) == 1:
					supremum = minima.pop()
				else:
					directParents = set(filter(lambda label: labelSet.issubset(label.runtimeRequires), minima))
					if len(directParents) == 1:
						supremum = directParents.pop()

			infomsg(f"base label sup of {renderLabelSet('input labels', labelSet)} is {supremum}")
			return supremum

		# This function knows a lot about SUSE package naming conventions.
		# It handles -doc and -devel packages that go with python3XX-Blah packages,
		# but also other packages that follow the same pattern
		def handlePythonSiblingPackage(self, packagePlacement, packageChoices, boundingBaseLabel):
			autoLabel = packagePlacement.autoLabel

			# Look for -devel and -doc packages
			suffix = f"-{autoLabel}"

			# handle the following types of names:
			#  - python-Foo-doc (autolabel doc)
			#  - python-Foo (common stuff shared by all pythonXXX-Foo packages)
			#  - python-Foo-common (for any suffix that is not recognized by an autolabel pattern)
			if (packagePlacement.name == self.name or packagePlacement.name.startswith(self.name + "-")) \
			   and boundingBaseLabel is not None:
				myChoice = packagePlacement.deriveChoiceFromBaseLabel(boundingBaseLabel)
				if myChoice is not None:
					infomsg(f"   {packagePlacement} is {self.name} plus suffix; base label {boundingBaseLabel} - place in {myChoice}")
					return myChoice

			if not packagePlacement.name.endswith(suffix):
				return None

			# handle pythonXXX-Foo-doc (ie each pythonXXX-Foo has its own -doc package)
			infomsg(f"   {packagePlacement} is a {autoLabel} package")
			for other, choice in packageChoices:
				if packagePlacement.name == f"{other}{suffix}":
					myChoice = packagePlacement.deriveChoiceFromBaseLabel(choice)
					if myChoice is not None:
						infomsg(f"   {packagePlacement} is {other.name} plus {suffix}; base label {choice} - place in {myChoice}")
						return myChoice

			return None

		def constrainToBaselabel(self, label):
			assert(label.parent is None)

			self._constrainedBaseLabels = Classification.createLabelSet()
			self._constrainedBaseLabels.add(label)

			self._commonBaseLabels = self._constrainedBaseLabels

		@property
		def commonBaseLabels(self):
			try:
				return self._commonBaseLabels
			except:
				pass

			# We do not iterate over self.children, because that would include packages scheduled for
			# later stages, too
			commonBaseLabels = None
			for packagePlacement in self.solved + self.unsolved:
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

		def solveCommonFeatureLabel(self):
			allRequires = Classification.createLabelSet()
			for packagePlacement in self.unsolved:
				node = packagePlacement.node

				autoLabelClosure = None
				if packagePlacement.autoLabel:
					autoLabelClosure = Classification.createLabelSet()
					for areq in packagePlacement.autoLabel.runtimeRequires:
						autoLabelClosure.update(self.labelOrder.downwardClosureFor(areq))

				for req in node.lowerNeighbors:
					reqPlacement = req.placement
					if reqPlacement is None:
						raise Exception(f"{node} invalid lower neighbor {req} - no placement")
					if reqPlacement.failed:
						infomsg(f"   {packagePlacement} requires {req} which we failed to solve")
						return False

					reqLabel = reqPlacement.label
					if not reqLabel:
						infomsg(f"   {packagePlacement} requires {req} which has not been solved")
						return False

					if autoLabelClosure and reqLabel in autoLabelClosure:
						continue

					allRequires.add(reqLabel.baseLabel)

			maxima = self.labelOrder.maxima(allRequires)
			featureLabels = Classification.createLabelSet(filter(lambda label: label.isFeature, maxima))
			if not featureLabels:
				return False

			infomsg(f"{self}: reduced base label set to {renderLabelSet('feature', featureLabels)}")
			if len(featureLabels) != 1:
				return False

			choice = next(iter(featureLabels))
			return self.tryToSolveUsingBaseLabel(choice, f"based on required feature {choice}")

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

		def getFavoriteSiblingsMap(self, classificationScheme):
			if self._favoriteSiblingsMap is None:
				self._favoriteSiblingsMap = FavoriteSiblingMap(self, classificationScheme)
			return self._favoriteSiblingsMap

		# place systemd-mini-devel close to systemd-mini
		def solvePurposeRelativeToSibling(self, classificationScheme):
			# if we have no solved siblings yet, don't even bother
			if self.numSolved == 0:
				return False

			map = self.getFavoriteSiblingsMap(classificationScheme)

			toBeExamined = []
			for pkg, packagePlacement in self.packageDict.items():
				if packagePlacement.label is None and \
				   packagePlacement.purpose and packagePlacement.purpose.packageSuffixes:
					toBeExamined.append((pkg, packagePlacement))

			if not toBeExamined:
				return False

			for pkg, packagePlacement in toBeExamined:
				favoriteSibling = map.findFavoriteSibling(pkg, packagePlacement)
				if favoriteSibling is None:
					continue

				infomsg(f"    {pkg} favorite sibling={favoriteSibling}")
				choice = map.chooseLabel(pkg, packagePlacement, favoriteSibling)
				if choice is not None:
					packagePlacement.setSolution(choice)
				else:
					purpose = packagePlacement.purpose
					label = favoriteSibling.label
					desiredCandidate = label.getObjectPurpose(purpose.name)
					if desiredCandidate is not None:
						closure = self.labelOrder.downwardClosureFor(desiredCandidate)
						for neigh in packagePlacement.node.lowerNeighbors:
							if neigh.lowerCone is None or desiredCandidate in neigh.lowerCone:
								continue
							infomsg(f"     {packagePlacement} requires {neigh}, which is not in scope of {desiredCandidate}")

			return self.isFinal

		# Place packages such as python bindings close to the sibling(s) they depend upon.
		# For instance, place python3-pwquality alongside libpwquality1
		#
		# Inspect each unsolved node P in turn.
		# Check whether P depends on one or more siblings. If all these siblings have
		# been labeled, and they share the same base label, check if that base label
		# can be used to place P as well.
		def solveRelativeToSiblingDependencies(self, classificationScheme):
			# To be implemented
			return False

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

	@profiling
	def createBuildPlacement(self, buildInfo):
		buildPlacement = self.TentativeBuildPlacement(buildInfo, self.labelOrder, self._preferences)

		if buildInfo.baseLabelConstraint is not None:
			# infomsg(f"{buildInfo} is constrained to base label {buildInfo.baseLabelConstraint}")
			buildPlacement.constrainToBaselabel(buildInfo.baseLabelConstraint)

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
				tentativePlacement.solveAutoflavorPreferredLabel() or \
				tentativePlacement.solveCommonBaseLabel() or \
				tentativePlacement.solveCommonFeatureLabel() or \
				tentativePlacement.solveCompatibleBaseLabel() or \
				tentativePlacement.solvePurposeRelativeToSibling(self.classificationScheme) or \
				tentativePlacement.solveRelativeToSiblingDependencies(self.classificationScheme)

			if tentativePlacement.isFinal:
				infomsg(f"{tentativePlacement}: completely solved - component {tentativePlacement.uniqueSourceProject}")
				return True

			infomsg(f"{tentativePlacement}: remains to be solved")

		return False

	# For a solved build placement, this tries to figure out the buildConfig and component
	# In addition, if the source package for this build has any build requirements that could
	# not be solved, attach a solver to it that tries to use the the buildConfig to find a
	# unique solving label
	def postProcessSolvedBuildPlacement(self, buildPlacement, build):
		if len(build.sources) != 1:
			warnmsg(f"{build} has {len(build.sources)} sources")

		buildLabel = None
		componentLabel = None
		if buildPlacement.solvingBaseLabel:
			componentLabel = buildPlacement.solvingBaseLabel.sourceProject
		else:
			solutions = [p.label for p in buildPlacement.solved]

			components = set(label.sourceProject for label in solutions)
			if len(components) == 1:
				componentLabel = next(iter(components))

		if componentLabel is None:
			warnmsg(f"{build} unable to determine unique component label")
			return False

		if buildLabel is None:
			buildLabel = componentLabel.getBuildFlavor('standard')

		if buildLabel is None:
			warnmsg(f"{build} unable to determine unique build label")
			return False

		# buildPlacement.setSolution(buildLabel, componentLabel)
		if buildPlacement.trace:
			infomsg(f"{build} will be placed in component {componentLabel}")

		with loggingFacade.temporaryIndent(3):
			buildPlacement.componentLabel = componentLabel

			for rpm in build.sources:
				node = self.solvingTree.getPackage(rpm)
				if node is None:
					continue

				for lower in node.lowerNeighbors:
					if lower.placement is None:
						warnmsg("{lower} has no placement handle")
						continue

					solver = self._preferences.solverFactory.sourceHintsSolver(buildLabel)
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

		infomsg(f"{buildPlacement}: {buildPlacement.numSolved}/{buildPlacement.numPackages} solved (stage 2)")
		with loggingFacade.temporaryIndent(3):
			for solver in sorted(solvers, key = lambda s: s.precedence):
				infomsg(f"{buildPlacement}: trying to solve using {solver}")

				with loggingFacade.temporaryIndent(3):
					success = solver.tryToSolve(buildPlacement)

				if buildPlacement.isFinal:
					infomsg(f"{buildPlacement}: successfully solved - component {buildPlacement.uniqueSourceProject}")
					return True

			infomsg(f"{buildPlacement} remains to be solved")

		return False

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

	def reportMissingBuildRequirements(self, buildSpecs):
		formatter = IndexFormatterTwoLevels(msgfunc = infomsg, sort = True)
		for spec in sorted(buildSpecs, key = lambda spec: spec.component.name):
			buildLabel = str(spec.component)
			buildEnv = spec.buildEnvironment
			for requiredLabel, packages in sorted(spec.unsatisfied, key = lambda pair: str(pair[0])):
				if requiredLabel:
					for required in packages:
						formatter.next(buildLabel, f"{requiredLabel.componentName}:{requiredLabel}",
							f"building {spec} using {buildEnv} requires {required} labelled {requiredLabel}")
				else:
					for required in packages:
						formatter.next(buildLabel, "(unlabelled)",
							f"building {spec} using {buildEnv} requires {required} which has not been labelled")

	def solve(self):
		infomsg("### PLACEMENT STAGE 1 ###")

		timing = ExecTimer()

		placements = []
		placementMap = {}
		for build in self.solvingTree.bottomUpBuildTraversal():
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

		# placements are sorted in order of build dependency - ie if any rpm
		# from OBS build A requires some package from build B, then we visit
		# B before A.
		for placement in placements:
			self.solveBuildPlacementStage1(placement)

		infomsg("### PLACEMENT STAGE 2 ###")
		for placement in placements:
			placement.queue = placement.stage2

		for build in self.solvingTree.topDownBuildTraversal():
			buildPlacement = placementMap[build]

			if not buildPlacement.isFinal:
				self.solveBuildPlacementStage2(buildPlacement)

			if buildPlacement.isFinal:
				self.postProcessSolvedBuildPlacement(buildPlacement, build)
			else:
				# if we were still unable to place this build, we should at least propagate
				# any solvers to unsolved lower neighbors
				buildPlacement.propagateSolvers()

		self.reportUnsolved(placements)

		result = ClassificationResult(self.solvingTree._order, self.classificationScheme.componentOrder())
		result.inversionMap = self.classificationScheme.inversionMap

		for node in self.solvingTree.bottomUpTraversal():
			if node.placement:
				node.placement.reportVerdict(node, result)

		# Create a BuildSpec factory. BuildSpecs convey information about how a certain OBS
		# package was built: packages required, buildconfig used, etc.
		buildSpecFactory = BuildSpecFactory.create(self.classificationScheme, self.labelOrder)

		allBuildSpecs = []
		for build in self.solvingTree.allBuilds:
			buildSpec = None
			buildConfig = None

			buildPlacement = placementMap.get(build)
			if buildPlacement is None:
				errormsg(f"{build} has not been handled by solver (because it has no packages?!)")
				continue

			# Get the component label even for builds that have not been placed completely
			componentLabel = buildPlacement.uniqueSourceProject

			if componentLabel:
				# This could be done even for completely unclassified builds:

				# build the list of labels/packages this build requires
				buildRequires = []
				for srpm in build.sources:
					srcNode = self.solvingTree.getPackage(srpm)
					for required in srcNode.lowerNeighbors:
						# required is a SolvingTree node, extract the label we assigned and the
						# list of rpms it represents
						requiredLabel = required.placement.label
						buildRequires.append((requiredLabel, required.packages))

				buildSpec = buildSpecFactory.createBuildSpec(build.name, componentLabel, buildRequires, trace = buildPlacement.trace)

				# This could be done for partially unclassified builds:
				if buildSpec is not None:
					allBuildSpecs.append(buildSpec)
					buildConfig = buildSpec.buildEnvironment

					if buildSpec.requiresUnlabelledPackages:
						infomsg(f"{build} requires unlabelled package(s) for building")

			result.labelOneBuild(build.name, componentLabel, build.packages, build.sources, buildSpec)

		result.compactBuildRequires()

		# could we wrap this into the ClassificationResult constructor?
		componentOrder = self.classificationScheme.componentOrder()
		for componentLabel in componentOrder.bottomUpTraversal():
			result.addComponent(componentLabel)

		self.reportMissingBuildRequirements(allBuildSpecs)

		return result

	def baseLabelsForSet(self, labels):
		if labels is not None:
			return Classification.baseLabelsForSet(labels)

	def __iter__(self):
		for pkg, node in self._packages.items():
			yield pkg, self.getBestCandidate(node)

	def getBestCandidate(self, packageNode):
		candidates = packageNode.candidates
		if not candidates:
			return None

		return self._order.minimumOf(candidates)
