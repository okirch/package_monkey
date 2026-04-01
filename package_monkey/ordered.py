from .util import Template
from .util import CycleDetector, LoggingCycleDetector, ANSITreeFormatter
from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .profile import profiling

from functools import reduce

import fastset as fastsets

##################################################################
# Faster cycle detector using fastsets
##################################################################
class FastsetCycleDetector(CycleDetector):
	def __init__(self, setClass, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.active = setClass()

	def push(self, key):
		if key in self.active:
			return False

		self.active.add(key)
		self.chain.append(key)
		return True

	def pop(self):
		key = super().pop()
		if key is not None:
			self.active.remove(key)
		return key

class OrderedSetMember(object):
	def __init__(self, key):
		self.domain = self.__class__.keyDomain

		self.key = key
		self._setClass = self.domain.set

		self.lowerNeighbors = None
		self.upwardClosure = None
		self.downwardClosure = None

		self._defined = False
		self._final = False

	def __str__(self):
		return str(self.key)

	def __hash__(self):
		return hash(self.key)

	@property
	def name(self):
		return str(self)

	def __eq__(self, other):
		return self is other

	def __ne__(self, other):
		return self is not other

	def __le__(self, other):
		return self.key in other.downwardClosure

	def __ge__(self, other):
		return other.key in self.downwardClosure

##################################################################
# Base class for cones, convex sets etc
##################################################################
class PartiallyOrderedSet(object):
	def __init__(self, order, members = None):
		self.order = order

		if members is None:
			members = order._setClass()
		self.members = members

	def preModifyCallback(self):
		pass

	def add(self, x):
		self.members.add(x)
		self.preModifyCallback()

	def update(self, s):
		self.members.update(s)
		self.preModifyCallback()

	def __contains__(self, x):
		return x in self.members

	def __iter__(self):
		return iter(self.members)

	def __len__(self):
		return len(self.members)

##################################################################
# a cone is a downward closure, ie if c is a member of cone C,
# then all x <= c are in C, too
# The generator G of a cone is its set of maxima. C is the
# downward closure of G.
#
# The union of two cones is a cone
# The intersection of two cones is a cone
# The difference of two cones is a convex set
##################################################################
class Cone(PartiallyOrderedSet):
	def __init__(self, order, members):
		super().__init__(order, members)
		self._generator = None

	@property
	def generator(self):
		if self._generator is None:
			self._generator = self.order.maxima(self.members)
		return self._generator

##################################################################
# a subset of a partially ordered set is convex if for every
# a, b in C, and every x with a <= x <= b, x is in C, too.
#
# Every convex set C is the difference of two cones:
#	Closure(C)
#	Closure(C).difference(C)
# The former is essentially a cone with C as the tip of the cone.
# The latter can be thought of as the supporting stump.
##################################################################
class ConvexSet(PartiallyOrderedSet):
	def __init__(self, order, members, support = None):
		super().__init__(order, members)
		self._closure = None
		self._support = None

		if support is not None:
			self._support = Cone(order, support)
			# FIXME: make sure it's a cone, and it is a super set of the
			# minimal support

	@property
	def closure(self):
		if self._closure is None:
			closure = self.order.downwardClosureForSet(self.members)
			self._closure = Cone(self.order, closure)
		return self._closure

	@property
	def support(self):
		if self._support is None:
			support = self.closure.members.difference(self.members)
			self._support = Cone(self.order, support)
		return self._support

##################################################################
# The partial order itself.
# This makes heavy use of fastsets to speed things up.
# (fastset are defined on top of a domain, such as "all labels",
# and represent subsets of that domain as bit vectors. Set operations
# can then be implemented as bit operations.
##################################################################
class PartialOrder(object):
	class LarvalNodeTemplate(Template):
		def __init__(self, node, keysBelow):
			# Templates have a different way of calling super()
			self.super().__init__()

			self.node = node
			self.key = node.key
			self.keysBelow = keysBelow

			self.rank = None
			self.below = None
			self.above = None

			# The imperfect rank can help to decide where to break cycles, if
			# the domain allows this.
			# After detecting all nodes that are part of a cycle, the 
			# "imperfect rank" of each node is computed like this:
			#  - no lower neighbors: rank 0
			#  - if there are lower neighbors that are not part of a
			#	cycle, call them lowerLoopFree:
			#	 rank = max(imperfectRank(lowerLoopFree)) + 1
			#  - if all lower neighbors are part of a cycle: rank = None,
			#	aka indeterminate
			# We do not set the rank here - computeImperfectRank() will use
			# this to detect whether to compute the value or not
			# self.imperfectRank = None

		def __int__(self):
			if self.rank is None:
				rank = 0
				if self.below:
					with self.guard.protect(self.key) as ticket:
						rank = max(map(int, self.below)) + 1
				self.rank = rank
			return self.rank

		def __str__(self):
			return str(self.key)

		def computeImperfectRank(self, loopies):
			try:
				return self.imperfectRank
			except:
				pass

			self.imperfectRank = None
			if not self.below:
				self.imperfectRank = 0
			else:
				max = -1
				for ln in self.below:
					n = ln.computeImperfectRank(loopies)
					if n is not None:
						if n > max:
							max = n
				if max >= 0:
					self.imperfectRank = max + 1

			return self.imperfectRank

	def __init__(self, domain, name, allowUnknownKeys = False):
		self.domain = domain
		self.name = name
		self.guard = FastsetCycleDetector(domain.set, name)
		self._unsorted = {}
		self._sorted = None
		self._final = False
		self._allowUnknownKeys = allowUnknownKeys

		self._setClass = domain.set

		self.nodeDomain = fastsets.Domain(f"{domain.name}")
		self.larvalNodeClass = self.LarvalNodeTemplate.instantiate(f"{domain.name}.Larval", self.nodeDomain.member, guard = self.guard)
		self._larval = {}

		self.nodeClass = type(f'PartialOrder<{domain.name}>.node', (OrderedSetMember, ), {'keyDomain': domain})

	def __str__(self):
		return f"PartialOrder({self.name})"

	def __contains__(self, name):
		return node in self._unsorted

	@property
	def allkeys(self):
		return self._setClass(self._unsorted.keys())

	@profiling
	def add(self, key, below):
		assert(not self._final)

		if key in self._larval:
			raise Exception(f"Cannot add {key} to partial order twice")

		node = self.nodeClass(key)
		node._defined = True
		self._unsorted[key] = node

		self._larval[key] = self.larvalNodeClass(node, below)

		self._sorted = None

	@profiling
	def createNode(self, key):
		try:
			return self._unsorted[key]
		except:
			assert(not self._final)
			self._unsorted[key] = self.nodeClass(key)
		return self._unsorted[key]

	def getNode(self, key):
		node = self._unsorted.get(key)
		if node is None:
			if self._allowUnknownKeys:
				return None
			raise Exception(f"Partial order {self.name} has no element {key} (type {type(key)})")
		return node

	def getNodesForSet(self, keySet):
		result = map(self.getNode, keySet)
		if self._allowUnknownKeys:
			result = filter(bool, result)
		return set(result)

	def __getitem__(self, key):
		return self.getNode(key)

	def lowerNeighbors(self, key):
		node = self.getNode(key)
		return self._setClass(neigh.key for neigh in node.lowerNeighbors)

	def upperNeighbors(self, key):
		node = self.getNode(key)
		return self._setClass(neigh.key for neigh in node.upperNeighbors)

	def bottomUpTraversal(self, subset = None):
		assert(self._final)
		nodes = self.getSubsetToTraverse(subset)
		return BottomUpTraversal(nodes)

	def topDownTraversal(self, subset = None):
		assert(self._final)
		nodes = self.getSubsetToTraverse(subset)
		return TopDownTraversal(nodes)

	def downwardClosureFor(self, key):
		node = self.getNode(key)
		if node is None:
			return None
		return node.downwardClosure

	def upwardClosureFor(self, key):
		node = self.getNode(key)
		if node is None:
			return None
		return node.upwardClosure

	def downwardClosureForSet(self, keySet):
		result = self._setClass()
		for key in keySet:
			if key not in result:
				result.update(self.downwardClosureFor(key))
		return result

	def upwardClosureForSet(self, keySet):
		result = self._setClass()
		for key in keySet:
			if key not in result:
				result.update(self.upwardClosureFor(key))
		return result

	def convexClosureForSet(self, keySet):
		return self.downwardClosureForSet(keySet).intersection(self.upwardClosureForSet(keySet))

	def isBelow(self, key1, key2):
		return key1 in self.downwardClosureFor(key2)

	def isAbove(self, key1, key2):
		return key1 in self.upwardClosureFor(key2)

	def subsetIsBelow(self, subset, key):
		return subset.issubset(self.downwardClosureFor(key))

	def subsetIsAbove(self, subset, key):
		return subset.issubset(self.upwardClosureFor(key))

	def maximumOf(self, subset):
		found = self.maxima(subset)
		if len(found) != 1:
			return None
		return found.pop()

	def minimumOf(self, subset):
		found = self.minima(subset)
		if len(found) != 1:
			return None
		return found.pop()

	def supremum(self, subset):
		if not subset:
			return None
		if len(subset) == 1:
			return next(iter(subset))

		# get the set of all elements y s.t. every y is above every x in subset
		above = reduce(self._setClass.intersection, map(self.upwardClosureFor, subset))

		# the return the minimum, if it exists
		return self.minimumOf(above)

	def infimum(self, subset):
		if not subset:
			return None
		if len(subset) == 1:
			return next(iter(subset))

		# get the set of all elements y s.t. every y is below every x in subset
		below = reduce(self._setClass.intersection, map(self.downwardClosureFor, subset))

		# the return the maximum, if it exists
		return self.maximumOf(below)

	def minima(self, subset):
		remaining = self.getNodesForSet(subset)

		aboveClosure = self._setClass()

		for node in remaining:
			# if the node is above one of the previous minima, ignore it
			if node.key not in aboveClosure:
				# get the set of all elements > this node
				nodeClosure = node.upwardClosure.copy()
				nodeClosure.discard(node.key)

				# enlarge the upward closure of result
				aboveClosure.update(nodeClosure)

		return subset.difference(aboveClosure)

	def oldMinima(self, subset):
		remaining = self.getNodesForSet(subset)

		result = set()
		while remaining:
			node = remaining.pop()

			ignore = False
			dropped = []
			for m in result:
				if node >= m:
					# node is above one of the minima we have so far
					ignore = True
					break
				if m >= node:
					# node is below one of the minima we have so far. replace the existing
					# minimum, and continue.
					dropped.append(m)

			if not ignore:
				result.add(node)
			for m in dropped:
				result.remove(m)

		return self._setClass(map(lambda node: node.key, result))

	def maxima(self, subset):
		remaining = self.getNodesForSet(subset)

		belowClosure = self._setClass()

		for node in remaining:
			# if the node is below one of the previous maxima, ignore it
			if node.key not in belowClosure:
				# get the set of all elements < this node
				nodeClosure = node.downwardClosure.copy()
				nodeClosure.discard(node.key)

				# enlarge the downward closure of result
				belowClosure.update(nodeClosure)

		return subset.difference(belowClosure)

	def unboundedElements(self, subset):
		result = self._setClass()

		for node in self.getNodesForSet(subset):
			if not node.above:
				result.add(node.key)
		return result

	def getSubsetToTraverse(self, subset):
		if subset is None:
			return self.sorted

		nodes = self.getNodesForSet(subset)
		return self.sortedSubset(nodes)

	def sortedSubset(self, subset):
		return sorted(subset, key = lambda node: (node.rank, str(node)))

	def findPath(self, sourceKey, destKey):
		if sourceKey == destKey:
			return [destKey]

		sourceNode = self.getNode(sourceKey)
		destNode = self.getNode(destKey)
		if not (sourceNode <= destNode):
			return None
		return self.findPathWork(sourceNode, destNode, [destKey])

	def findPathWork(self, sourceNode, destNode, path):
		for lowerNeighbor in destNode.below:
			if lowerNeighbor is sourceNode:
				return [sourceNode.key] + path
			if sourceNode <= lowerNeighbor:
				return self.findPathWork(sourceNode, lowerNeighbor, [lowerNeighbor.key] + path)
		assert(False)

	# subset must be a convex set in order for this to work
	def asTreeFormatter(self, convexScope = None, topDown = False):
		tf = ANSITreeFormatter()

		if topDown:
			if convexScope is None:
				nodes = []
				for node in self._unsorted:
					if not node.above:
						nodes.append(node)
			else:
				start = self.maxima(convexScope)
				nodes = list(map(self.getNode, start))
		else:
			if convexScope is None:
				nodes = []
				for node in self._unsorted:
					if not node.below:
						nodes.append(node)
			else:
				start = self.minima(convexScope)
				nodes = list(map(self.getNode, start))

		seen = self._setClass()
		for node in nodes:
			self.treeFormatterWork(node, topDown, convexScope, tf.root, seen)
		return tf

	def treeFormatterWork(self, node, topDown, convexScope, tfParent, seen):
		tfNode = tfParent.add(node.key)

		if topDown:
			nodes = node.lowerNeighbors
		else:
			nodes = node.upperNeighbors

		for neighbor in nodes:
			if neighbor.key in seen or neighbor.key not in convexScope:
				continue

			seen.add(neighbor.key)

			self.treeFormatterWork(neighbor, topDown, convexScope, tfNode, seen)

	class CollapsedCycle:
		def __init__(self, cycle):
			self.members = set(cycle)
			self.loops = [cycle]
			self._name = None

			self.breakpoint = None
			self.breakBelow = None
			self.breakAbove = None

		def __str__(self):
			if self._name is None:
				self._name = ' '.join(map(str, self.members))
			return self._name

		def update(self, other):
			assert(isinstance(other, self.__class__))
			self.members.update(other.members)

			for loop in other.loops:
				if loop not in self.loops:
					self.loops.append(loop)

			self._name = None

		def addBreakPoint(self, key, below, above):
			self.breakpoint = key
			self.breakBelow = below
			self.breakAbove = above

		def __len__(self):
			return len(self.members)

	def getCollapsibleCycles(self, detectBreak = False):
		def randomWalk(larvalNode):
			if larvalNode in seen:
				return

			assert((larvalNode in seen) == (larvalNode.below is not None))

			node = larvalNode.node
			with guard.protect(node.key) as ticket:
				if not ticket.valid:
					return

				below = self.nodeDomain.set()
				for key in larvalNode.keysBelow:
					lower = self._larval[key]
					below.add(lower)
					randomWalk(lower)

				# Add the node to the set of visited nodes *after*
				# descending into it. Otherwise we wouldn't catch
				# the cycles.
				seen.add(larvalNode)
				larvalNode.below = below

		# This is called before, or instead of, finalize()
		assert(not self._final)

		guard = LoggingCycleDetector(self.name)
		seen = set()

		for larvalNode in self._larval.values():
			randomWalk(larvalNode)

		# We may have detected multiple intersecting cycles, like
		#	A -> B -> C -> A
		#	D -> B -> E -> D
		# Return a single set of nodes that should be collapsed into one:
		#	(A, B, C, D, E)
		collapse = {}
		for cycle in guard.cycles:
			cc = self.CollapsedCycle(cycle)
			for key in cycle:
				if collapse.get(key) is not None:
					cc.update(collapse[key])
			for key in cc.members:
				collapse[key] = cc

		if not collapse:
			return []

		collapsedCycles = set(collapse.values())

		if detectBreak:
			# get the larval nodes that are part of a loop
			loopies = self.nodeDomain.set(map(self._larval.get, collapse.keys()))

			for larvalNode in self._larval.values():
				larvalNode.computeImperfectRank(loopies)

			for cc in collapsedCycles:
				self.findCollapsedCycleBreak(cc, loopies)

		return collapsedCycles

	# Having detected one or more intersecting cycles, we want to find a good
	# point where to suggest breaking the cycle.
	# One approach may be to break the "longest" edge, ie the one between a node
	# that is very low in the tree, and the one(s) that are very high in the tree.
	# Another approach could be to break directly above a node very low in the tree.
	#
	# Neither is perfect. In particular, this implementation is totally useless
	# if we're dealing with several intersecting cycles.
	def findCollapsedCycleBreak(self, cc, loopies):
		# Don't bother with trying to find a good break point for a cycle of 2.
		if len(cc.members) <= 2:
			return

		sortedNodes = []
		for key in cc.members:
			larvalNode = self._larval[key]

			# imperfectRank can be None if the element's dependencies are
			# all circular. In this case, it doesn't matter whether we break
			# the cycle here or elsewhere.
			if larvalNode.computeImperfectRank(loopies) is not None:
				sortedNodes.append(larvalNode)

		if not sortedNodes:
			return

		sortedNodes.sort(key = lambda ln: ln.imperfectRank)
		candidate = sortedNodes[0]

		cycleNodes = self.nodeDomain.set(map(self._larval.get, cc.members))

		loopiesBelow = candidate.below.intersection(cycleNodes)
		if len(loopiesBelow) == 1:
			breakable = next(iter(loopiesBelow))

		loopiesAbove = self.nodeDomain.set()
		for larvalNode in cycleNodes:
			if candidate in larvalNode.below:
				loopiesAbove.add(larvalNode)

		if len(loopiesAbove) == 1:
			breakable = next(iter(loopiesAbove))

		cc.addBreakPoint(candidate.key,
				self._setClass(ln.key for ln in loopiesBelow),
				self._setClass(ln.key for ln in loopiesAbove))

	def finalize(self):
		if self._final:
			return

		# infomsg(f"Finalize partial order {self}")

		for ln in self._larval.values():
			below = self.nodeDomain.set(self._larval[key] for key in ln.keysBelow)
			ln.below = below

			ln.above = self.nodeDomain.set()

		for ln in self._larval.values():
			for lower in ln.below:
				lower.above.add(ln)

		larval = sorted(self._larval.values(), key = int)
		if not larval:
			self.height = 0
		else:
			self.height = int(larval[-1])

		# Update the downward closure of each node
		# The closure is not a set of OrderedSetMembers, but a set of keys
		for ln in larval:
			# copy rank from larval to node
			ln.node.rank = ln.rank

			closure = self._setClass(lower.key for lower in ln.below)
			for lower in ln.below:
				closure.update(lower.node.downwardClosure)
			closure.add(ln.key)
			ln.node.downwardClosure = closure

			ln.node.lowerNeighbors = list(lower.node for lower in ln.below)
			ln.node._final = True

		# Update the upward closure of each node
		# The closure is not a set of OrderedSetMembers, but a set of keys
		for ln in reversed(larval):
			closure = self._setClass()
			for upper in ln.above:
				closure.update(upper.node.upwardClosure)
			closure.add(ln.key)
			ln.node.upwardClosure = closure

		self._sorted = self.sortedSubset(self._unsorted.values())

		for node in self._sorted:
			if not node._final:
				raise Exception(f"{self} {node.key} still larval after finalize()?!")

		self._larval = None
		self._final = True
		return True

	@property
	def sorted(self):
		assert(self._final)
		return self._sorted

class BottomUpTraversal:
	def __init__(self, nodes):
		self.keys = list(map(lambda node: node.key, nodes))

	def __iter__(self):
		return iter(self.keys)

class TopDownTraversal:
	def __init__(self, nodes):
		self.keys = reversed(list(map(lambda node: node.key, nodes)))

	def __iter__(self):
		return iter(self.keys)

if __name__ == '__main__':
	from util import ExecTimer
	import random
	# hack until I'm packaging fastsets properly
	import fastsets.fastsets as fastsets

	domain = fastsets.Domain("pairs")
	class Pair(domain.member):
		def __init__(self, a, b):
			super().__init__()
			self.a = a
			self.b = b

		def __hash__(self):
			return hash((self.a, self.b))

		def __eq__(self, other):
			return self is other

		def __le__(self, other):
			return self.a <= other.a and self.b <= other.b

		def __lt__(self, other):
			return (self is not other) and (self <= other)

		def __str__(self):
			return f"({self.a}, {self.b})"

	class TestLattice:
		def __init__(self, max):
			self.max = max

			self._pairs = []
			self._lookup = {}
			for a in range(max):
				for b in range(max):
					pair = Pair(a, b)
					self._lookup[a, b] = pair
					self._pairs.append(pair)
			self._numPairs = len(self._pairs)
			self._indexPermutation = list(range(self._numPairs))

			self.order = PartialOrder(domain, "lattice")
			for p1 in self._pairs:
				below = domain.set()
				for p2 in self._pairs:
					if p2 < p1:
						below.add(p2)
				self.order.add(p1, below)
			self.order.finalize()

			self.numFailures = 0
			self.numFailuresTotal = 0
			self.numTests = 0
			self.numTestsTotal = 0

		def supOfSet(self, input):
			if not input:
				return None
			aMax = max(p.a for p in input)
			bMax = max(p.b for p in input)
			return self._lookup[aMax, bMax]

		def infOfSet(self, input):
			if not input:
				return None
			aMax = min(p.a for p in input)
			bMax = min(p.b for p in input)
			return self._lookup[aMax, bMax]

		def createRandomSet(self, numElements = None):
			if numElements is None:
				numElements = random.randrange(self._numPairs)

			random.shuffle(self._indexPermutation)

			result = domain.set()
			for i in range(numElements):
				k = self._indexPermutation[i]
				result.add(self._pairs[k])

			return result

		def beginTest(self):
			self.testTimer = ExecTimer()

		def fail(self, msg):
			print(f"FAIL: {msg}")
			self.numFailures += 1

		def reportSingleTest(self, name):
			timer = self.testTimer
			del self.testTimer

			if self.numFailures:
				print(f"FAIL: {name} [{timer}]: {self.numFailures}/{self.numTests} tests failed")
				self.numFailuresTotal += self.numFailures
				self.numFailures = 0
			else:
				print(f"PASS: {name} [{timer}]: {self.numTests} tests passed")

			self.numTestsTotal += self.numTests
			self.numTests = 0

		def validateExtrema(self, testSet):
			for a in testSet:
				for b in testSet:
					if a is b:
						continue
					if a <= b or b <= a:
						print(a, b)
						return False
			return True

		def testMaxima(self, count, testSetSize = None):
			self.beginTest()
			for i in range(count):
				self.numTests += 1

				input = self.createRandomSet(numElements = testSetSize)
				# print(f"input={' '.join(map(str, input))}")

				maxes = self.order.maxima(input)
				# print(f"maxes={' '.join(map(str, maxes))}")

				if not maxes.issubset(input):
					self.fail("result of maxima() is not a subset of input")
					continue

				closure = self.order.downwardClosureForSet(maxes)
				if not input.issubset(closure):
					self.fail("result of maxima() does not contain all maxima")
					continue

				if not self.validateExtrema(maxes):
					self.fail("result of maxima() contains elements that are <= each other")
					continue

			self.reportSingleTest("maxima()")

		def testMaximum(self, count, testSetSize = None):
			self.beginTest()
			for i in range(count):
				self.numTests += 1

				input = self.createRandomSet(numElements = testSetSize)

				sup = self.supOfSet(input)
				max = self.order.maximumOf(input)

				if sup is None and max is not None:
					self.fail(f"result of maximumOf() should have been {sup} but was {max}")
					continue

				# print(f"input {' '.join(map(str, input))} max={max}; sup={sup}")
				if max is not None:
					if max not in input:
						self.fail(f"result of maximumOf() is not a member of input set")
						continue
					if max is not sup:
						self.fail(f"result of maximumOf() should have been {sup}")
						continue
				elif sup in input:
					self.fail(f"result of maximumOf() should have been {sup} but was {max}")
					continue

			self.reportSingleTest("maximumOf()")

		def testMinima(self, count, testSetSize = None):
			self.beginTest()
			for i in range(count):
				self.numTests += 1

				input = self.createRandomSet(numElements = testSetSize)
				# print(f"input={' '.join(map(str, input))}")

				mins = self.order.minima(input)
				# print(f"mins={' '.join(map(str, mins))}")

				if not mins.issubset(input):
					self.fail("result of minima() is not a subset of input")
					continue

				closure = self.order.upwardClosureForSet(mins)
				if not input.issubset(closure):
					self.fail("result of minima() does not contain all minima")
					continue

				if not self.validateExtrema(mins):
					self.fail("result of minima() contains elements that are <= each other")
					continue

			self.reportSingleTest("minima()")

		def testMinimum(self, count, testSetSize = None):
			self.beginTest()
			for i in range(count):
				self.numTests += 1

				input = self.createRandomSet(numElements = testSetSize)

				inf = self.infOfSet(input)
				min = self.order.minimumOf(input)

				if inf is None and min is not None:
					self.fail(f"result of minimumOf() should have been {inf} but was {min}")
					continue

				# print(f"input {' '.join(map(str, input))} min={min}; inf={inf}")
				if min is not None:
					if min not in input:
						self.fail(f"result of minimumOf() is not a member of input set")
						continue
					if min is not inf:
						self.fail(f"result of minimumOf() should have been {inf}")
						continue
				elif inf in input:
					self.fail(f"result of minimumOf() should have been {inf} but was {min}")
					continue

			self.reportSingleTest("minimumOf()")

		def testAll(self, count = 500):
			self.testMaxima(count)
			self.testMaximum(count, testSetSize = int(self.max / 6))
			self.testMinima(count)
			self.testMinimum(count, testSetSize = int(self.max / 6))

	test = TestLattice(47)
	test.testAll()
