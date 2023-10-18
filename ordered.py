from util import CycleDetector, LoggingCycleDetector

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

class PartialOrder(object):
	def __init__(self, name, allowUnknownKeys = False):
		self.name = name
		self.guard = CycleDetector(name)
		self._unsorted = {}
		self._sorted = None
		self._final = False
		self._allowUnknownKeys = allowUnknownKeys

		self._hidden = None

	def __contains__(self, name):
		return node in self._unsorted

	@property
	def allkeys(self):
		return set(self._unsorted.keys())

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
			self._hidden = set()

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
		result = set()
		for key in keySet:
			if key not in result:
				result.update(self.downwardClosureFor(key))
		return result

	def upwardClosureForSet(self, keySet):
		result = set()
		for key in keySet:
			if key not in result:
				result.update(self.upwardClosureFor(key))
		return result

	def subsetIsBelow(self, subset, key):
		return subset.issubset(self.downwardClosureFor(key))

	def subsetIsAbove(self, subset, key):
		return subset.issubset(self.upwardClosureFor(key))

	def maximumOf(self, subset):
		found = self.maxima(subset)
		if len(found) != 1:
			return None
		return found[0]

	def minimumOf(self, subset):
		found = self.minima(subset)
		if len(found) != 1:
			return None
		return found[0]

	def minima(self, subset):
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

		if False:
			for n1 in result:
				for n2 in result:
					if n1 is n2:
						continue
					assert(not (n1 >= n2))
					assert(not (n1 <= n2))

		# FIXME: why are we returning a list rather than a set?
		return list(map(lambda node: node.key, result))

	def maxima(self, subset):
		remaining = self.getNodesForSet(subset)

		result = set()
		while remaining:
			node = remaining.pop()

			ignore = False
			dropped = []
			for m in result:
				if node <= m:
					# node is below one of the maxima we have so far. ignore it.
					ignore = True
					break
				if m <= node:
					# node is above one of the maxima we have so far. replace the existing
					# maximum, and continue.
					dropped.append(m)

			if not ignore:
				result.add(node)
			for m in dropped:
				result.remove(m)

		# FIXME: why are we returning a list rather than a set?
		return list(map(lambda node: node.key, result))

	def unboundedElements(self, subset):
		result = set()

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
			closure = set()
			closure.add(node.key)
			for lower in node.below:
				closure.update(lower._downwardClosure)
			node._downwardClosure = closure

		# Update the upward closure of each node
		# The closure is not a set of OrderedSetMembers, but a set of keys
		for node in reversed(self._sorted):
			closure = set()
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

