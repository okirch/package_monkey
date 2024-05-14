from util import CycleDetector, LoggingCycleDetector
from functools import reduce

class OrderedSetMember(object):
	def __init__(self, key):
		self.key = key
		self.below = []
		self.above = []
		self._rank = None
		self._upwardClosure = None
		self._downwardClosure = None
		self._defined = False
		self._hidden = False

	def __str__(self):
		return str(self.key)

	def __hash__(self):
		return hash(self.key)

	@property
	def name(self):
		return str(self)

	@property
	def sortkey(self):
		return (self.rank, self.name)

	def __eq__(self, other):
		return self is other

	def __ne__(self, other):
		return self is not other

	def __le__(self, other):
		return self.key in other._downwardClosure

	def __ge__(self, other):
		return other.key in self._downwardClosure

##################################################################
# Base class for cones, convex sets etc
##################################################################
class PartiallyOrderedSet(object):
	def __init__(self, order, members):
		self.order = order
		self.members = members

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
	def __init__(self, domain, name, allowUnknownKeys = False):
		self.domain = domain
		self.name = name
		self.guard = CycleDetector(name)
		self._unsorted = {}
		self._sorted = None
		self._final = False
		self._allowUnknownKeys = allowUnknownKeys

		self._hidden = None

		self._setClass = domain.set

	def __contains__(self, name):
		return node in self._unsorted

	@property
	def allkeys(self):
		return self._setClass(self._unsorted.keys())

	def add(self, key, below):
		assert(not self._final)

		node = self.createNode(key)

		if node._defined:
			raise Exception(f"Cannot add {key} to partial order twice")
		node._defined = True

		for req in below:
			self.establishRelation(node, self.createNode(req))

		self._sorted = None

	def hide(self, hiddenSet):
		if self._hidden is None:
			self._hidden = self._setClass()

		self._hidden.update(hiddenSet)
		for key in hiddenSet:
			self.getNode(key)._hidden = True

	def createNode(self, key):
		try:
			return self._unsorted[key]
		except:
			self._unsorted[key] = OrderedSetMember(key)
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

	def establishRelation(self, upper, lower):
		upper.below.append(lower)
		lower.above.append(upper)

	def lowerNeighbors(self, key):
		node = self.getNode(key)
		return self._setClass(neigh.key for neigh in node.below)

	def upperNeighbors(self, key):
		node = self.getNode(key)
		return self._setClass(neigh.key for neigh in node.above)

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
		return self.filterKeys(node._downwardClosure)

	def upwardClosureFor(self, key):
		node = self.getNode(key)
		if node is None:
			return None
		return self.filterKeys(node._upwardClosure)

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
				nodeClosure = node._upwardClosure.copy()
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
				nodeClosure = node._downwardClosure.copy()
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
			return self.filterNodes(self._sorted)

		subset = self.filterKeys(subset)
		nodes = self.getNodesForSet(subset)
		return self.sortedSubset(nodes)

	def sortedSubset(self, subset):
		return sorted(subset, key = lambda node: (self.rank(node), str(node)))

	def filterKeys(self, keySet):
		if self._hidden:
			# FIXME shouldn't this be .difference?!
			keySet = keySet.intersection(self._hidden)
		return keySet

	def filterNodes(self, nodeList):
		if self._hidden:
			nodeList = list(filter(lambda node: not node._hidden, nodeList))
		return nodeList

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
		from util import ANSITreeFormatter

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
			nodes = node.below
		else:
			nodes = node.above

		for neighbor in nodes:
			if neighbor.key in seen or neighbor.key not in convexScope:
				continue

			seen.add(neighbor.key)

			self.treeFormatterWork(neighbor, topDown, convexScope, tfNode, seen)

	class CollapsedCycle:
		def __init__(self, members):
			self.members = set(members)
			self._name = None

		def __str__(self):
			if self._name is None:
				self._name = ' '.join(map(str, self.members))
			return self.name

		def update(self, other):
			assert(isinstance(other, self.__class__))
			self.members.update(other.members)
			self._name = None

		def __len__(self):
			return len(self.members)

	def getCollapsibleCycles(self):
		guard = LoggingCycleDetector(self.name)
		seen = set()

		for node in self._unsorted.values():
			self.randomWalk(node, guard, seen)

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

		groups = set(collapse.values())
		return groups

	def randomWalk(self, node, guard, seen):
		if node in seen:
			return

		with guard.protect(node.key) as ticket:
			if not ticket.valid:
				return

			for lower in node.below:
				self.randomWalk(lower, guard, seen)

			# Add the node to the set of visited nodes *after*
			# descending into it. Otherwise we wouldn't catch
			# the cycles.
			seen.add(node)

	def finalize(self):
		if self._final:
			return

		self._sorted = self.sortedSubset(self._unsorted.values())

		# Update the downward closure of each node
		# The closure is not a set of OrderedSetMembers, but a set of keys
		for node in self._sorted:
			closure = self._setClass()
			closure.add(node.key)
			for lower in node.below:
				closure.update(lower._downwardClosure)
			node._downwardClosure = closure

		# Update the upward closure of each node
		# The closure is not a set of OrderedSetMembers, but a set of keys
		for node in reversed(self._sorted):
			closure = self._setClass()
			closure.add(node.key)
			for lower in node.above:
				closure.update(lower._upwardClosure)
			node._upwardClosure = closure

		self._final = True
		return True

	def rank(self, node):
		if node._rank is None:
			rank = 0
			with self.guard.protect(node.key) as ticket:
				for lower in node.below:
					lowerRank = self.rank(lower)
					if lowerRank >= rank:
						rank = lowerRank + 1
#			print(f"{rank:4} {node}")
			node._rank = rank


		return node._rank

	@property
	def sorted(self):
		assert(self._final)
		return self._sorted

class RuntimeOrdering(PartialOrder):
	def __init__(self):
		super().__init__()

		self.guard = Label.RUNTIME_CYCLE_GUARD

	def labelOrderNode(self, label):
		if label._runtimeOrderNode is None:
			label._runtimeOrderNode = OrderedSetMember(label)
		return label._runtimeOrderNode

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
