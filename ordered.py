from util import CycleDetector, GenerationCounter, Timestamp

class OrderedSetMember(object):
	def __init__(self, key):
		self._timestamp = Timestamp()
		self.key = key
		self.below = []
		self.above = []
		self._rank = None
		self._upwardClosure = None
		self._downwardClosure = None
		self._defined = False

	def __str__(self):
		return str(self.key)

	def __hash__(self):
		return hash(self.key)

	@property
	def name(self):
		return str(self)

	@property
	def isCurrent(self):
		return self._timestamp.isCurrent(Label.GENERATION)

	@property
	def sortkey(self):
		return (self.rank, self.name)

	def __eq__(self, other):
		return self is other

	def __ne__(self, other):
		return self is not other

	def __le__(self, other):
		return self in other._downwardClosure

	def __ge__(self, other):
		return other in self._downwardClosure

class PartialOrder(object):
	def __init__(self, name):
		self.name = name
		self.guard = CycleDetector(name)
		self._unsorted = {}
		self._sorted = None
		self._timestamp = Timestamp()
		self._final = False

	def __contains__(self, name):
		return node in self._unsorted

	def add(self, key, below):
		assert(not self._final)

		node = self.createNode(key)

		if node._defined:
			raise Exception(f"Cannot add {key} to partial order twice")
		node._defined = True

		for req in below:
			self.establishRelation(node, self.createNode(req))

		self._sorted = None

	def createNode(self, key):
		try:
			return self._unsorted[key]
		except:
			self._unsorted[key] = OrderedSetMember(key)
		return self._unsorted[key]

	def getNode(self, key):
		node = self._unsorted.get(key)
		if node is None:
			raise Exception(f"Partial order {self.name} has no element {key}")
		return node

	def __getitem__(self, key):
		return self.getNode(key)

	def establishRelation(self, upper, lower):
		upper.below.append(lower)
		lower.above.append(upper)

	def bottomUpTraversal(self, subset = None):
		assert(self._final)
		nodes = self.getSubsetToTraverse(subset)
		return BottomUpTraversal(nodes)

	def topDownTraversal(self, subset):
		assert(self._final)
		nodes = self.getSubsetToTraverse(subset)
		return TopDownTraversal(nodes)

	def downwardClosureFor(self, key):
		node = self.getNode(key)
		return node._downwardClosure

	def upwardClosureFor(self, key):
		node = self.getNode(key)
		return node._upwardClosure

	def getSubsetToTraverse(self, subset):
		if subset is None:
			return self._sorted

		nodes = set(map(self.getNode, subset))
		return self.sortedSubset(nodes)

	def sortedSubset(self, subset):
		return sorted(subset, key = lambda node: (self.rank(node), str(node)))

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

	def rank(self, node):
		if node._rank is None:
			rank = 0
			with self.guard.protect(node.name) as guard:
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
	def __init__(self, keys):
		self.keys = reversed(list(map(lambda node: node.key, nodes)))

	def __iter__(self):
		return iter(self.keys)

