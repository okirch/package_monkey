#
# Miscellaneous utility classes
#

# A simple class for batched processing
# You can use this when you have a long-running data processing loop
# and you do not want to lose all progress when you hit a bug during
# development.
class ChunkingQueue:
	def __init__(self, processingFunction, chunkSize = 20):
		self.processingFunction = processingFunction
		self.chunkSize = chunkSize
		self.processed = []

	def __del__(self):
		self.flush()

	def add(self, object):
		self.processed.append(object)
		if len(self.processed) >= self.chunkSize:
			self.flush()

	def flush(self):
		if self.processed:
			self.processingFunction(self.processed)
			self.processed = []

# Timestamp and GenerationCounter implement a simple mechanism to track
# global modifications and trigger updates of an object. For example,
# a change in tree topology may require each node to recompute its closure.
#
#  class Foo:
#	GENERATION = GenerationCounter()
#
#	def __init__(self):
#		self.timestamp = Timestamp()
#
#	@classmethod
#	def topologyChanged(self):
#		self.GENERATION.tick()
#
#	def getClosure(self):
#		if not self.timestamp.isCurrent(self.GENERATION):
#			self.closure = self.recomputeClosure()
#		return self.closure
class GenerationCounter:
	def __init__(self):
		self._generation = 1

	def tick(self):
		self._generation += 1

	@property
	def value(self):
		return self._generation

class Timestamp:
	def __init__(self):
		self._lastUpdated = 0

	def isCurrent(self, generationCounter):
		if self._lastUpdated == generationCounter.value:
			return True

		self._lastUpdated = generationCounter.value
		return False

# Simple tool to detect cycles in a graph
#
# class TreeNode:
#	CYCLES = CycleDetector("tree node")
#
#	def __init__(self, label):
#		self.label = label
#
#	def traverse(self, visitor):
#		with self.CYCLES.protect(self.label) as guard:
#			for child in self.children:
#				self.traverse(visitor)
#			visitor.visit(self)
#
class CycleDetector:
	class Ticket:
		def __init__(self, detector, key):
			self.detector = detector
			self.key = key

		def __enter__(self):
			self.detector.acquire(self.key)

		def __exit__(self, *args):
			self.detector.release(self.key)

		def drop(self):
			if self.detector:
				self.detector(key)
				self.detector = None

	def __init__(self, name):
		self.name = name
		self.chain = []

	def protect(self, key):
		return self.Ticket(self, key)

	def acquire(self, key):
		# for very deep trees, we would need something more efficient than a linear search.
		# O(n^2) is good enough for the shallow trees we're dealing with here.
		if key in self.chain:
			names = self.chain + [key]
			raise Exception(f"Detected {self.name} loop in {' -> '.join(names)}")

		self.chain.append(str(key))

	def release(self, key):
		if not self.chain or self.chain[-1] != key:
			raise Exception(f"Out-of-sequence release of key {key} in Cycle detector")

		self.chain.pop()

