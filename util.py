#
# Miscellaneous utility classes
#
import time
import fnmatch

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

##################################################################
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
##################################################################
class CycleException(Exception):
	def __init__(self, msg, cycle):
		super().__init__(msg)
		self.cycle = cycle

class CycleDetector(object):
	class Ticket:
		def __init__(self, detector, key):
			self.detector = detector
			self.key = key
			self.valid = False

#		def __bool__(self):
#			return self.valid

		def __enter__(self):
			self.valid = self.detector.acquire(self.key)
			return self

		def __exit__(self, *args):
			if self.valid:
				self.detector.release(self.key)
				self.valid = False

		def drop(self):
			if self.valid:
				self.detector.release(self.key)
				self.valid = False

	def __init__(self, name):
		self.name = name
		self.chain = []

	def protect(self, key):
		return self.Ticket(self, key)

	def acquire(self, key):
		# for very deep trees, we would need something more efficient than a linear search.
		# O(n^2) is good enough for the shallow trees we're dealing with here.
		if key in self.chain:
			i = self.chain.index(key)
			cycle = self.chain[i:] + [key]
			names = map(str, cycle)
			raise CycleException(f"Detected {self.name} loop in {' -> '.join(names)}", cycle)

		self.chain.append(key)
		return True

	def release(self, key):
		if not self.chain or self.chain[-1] != key:
			raise Exception(f"Out-of-sequence release of key {key} in Cycle detector")

		self.chain.pop()

class LoggingCycleDetector(CycleDetector):
	def __init__(self, *args):
		super().__init__(*args)
		self.cycles = []

	def acquire(self, key):
		# for very deep trees, we would need something more efficient than a linear search.
		# O(n^2) is good enough for the shallow trees we're dealing with here.
		if key in self.chain:
			i = self.chain.index(key)
			self.cycles.append(self.chain[i:] + [key])
			return False

		if len(self.chain) > 1000:
			raise Exception("Looks like a cycle but you ignored all warnings")

		self.chain.append(key)
		return True

##################################################################
#
# filter a collection of things by rank
# A rank of None indicates no ranking at all, and is "less than" any other rank
#
##################################################################
def filterRanking(items, getRank, isBetterThan):
	bestRank = None
	found = []
	for item in items:
		rank = getRank(item)
		if rank != bestRank:
			if rank is None:
				continue

			if type(rank) != int:
				raise Exception(f"ranking function returns non-integer value {rank}")

			if bestRank is not None and isBetterThan(bestRank, rank):
				continue

			bestRank = rank
			found = []
		found.append(item)
	
	return found

def filterLowestRanking(items, getRank):
	return filterRanking(items, getRank, int.__lt__)

def filterHighestRanking(items, getRank):
	return filterRanking(items, getRank, int.__gt__)

##################################################################
#
# Utility class for execution timing
#
##################################################################
class ExecTimer:
	def __init__(self):
		self.t0 = time.time()

	@property
	def elapsed(self):
		return time.time() - self.t0

	def __str__(self):
		return f"{self.elapsed:.3} sec"

class LoggingExecTimer(ExecTimer):
	def __str__(self):
		elapsed = self.elapsed
		min = int(elapsed / 60)
		sec = int(elapsed) % 60
		return f"[{min:02}:{sec:02}]"

##################################################################
#
# A simple progress tracker
#
##################################################################
class ThatsProgress:
	def __init__(self, total):
		self.count = 0
		self.total = total

	@property
	def percent(self):
		if self.total == 0:
			return 100
		return 100.0 * self.count / self.total

	def __str__(self):
		return f"{self.percent:3.1f}%"

	def tick(self):
		self.count += 1

##################################################################
#
# A simple name matches
#
##################################################################
class NameMatcher:
	def __init__(self, names = []):
		self.patterns = []
		self.names = []

		for name in names:
			if '*' in name or '?' in name:
				self.patterns.append(name)
			else:
				self.names.append(name)

	def match(self, candidate):
		for name in self.names:
			if name == candidate:
				return True

		for pattern in self.patterns:
			if fnmatch.fnmatchcase(candidate, pattern):
				return True

		return False

##################################################################
#
# Interfacing with python's logging class
#
##################################################################
import logging

class LoggingFacade:
	DEFAULT_FORMAT = '%(asctime)s: %(prefix)s%(message)s'

	class RelativeTimeFormatter(logging.Formatter):
		class Indent:
			def __init__(self):
				self.value = 0

			@property
			def whitespace(self):
				return " " * self.value

		class TI:
			def __init__(self, indent, width):
				self.indent = indent
				self.width = width
				self.active = False

			def __enter__(self):
				if not self.active:
					self.indent.value += self.width
					self.active = True
				return self

			def __exit__(self, *args):
				if self.active:
					self.indent.value -= self.width
					self.active = False

			def __del__(self):
				assert(not self.active)

		def __init__(self, *args, **kwargs):
			super().__init__(*args, **kwargs)
			self.timer = LoggingExecTimer()
			self.indent = self.Indent()

		def formatTime(self, record, datefmt = None):
			return str(self.timer)

		def format(self, record):
			if record.levelname == 'ERROR':
				record.prefix = "Error: "
			elif record.levelname == 'WARNING':
				record.prefix = "Warning: "
			else:
				record.prefix = self.indent.whitespace
			return super().format(record)

		def temporaryIndent(self, width = 3):
			return self.TI(self.indent, width)

	def __init__(self):
		self.root = logging.getLogger()
		self.root.setLevel(logging.INFO)

		self.default = self.getLogger('default')

		self.defaultFormat = self.RelativeTimeFormatter(self.DEFAULT_FORMAT)

	def addRootHandler(self, handler):
		handler.setFormatter(self.defaultFormat)
		self.root.addHandler(handler)

	def enableStdout(self):
		self.addRootHandler(logging.StreamHandler())

	def addLogfile(self, filename):
		self.addRootHandler(logging.FileHandler(filename, mode = "w"))

	def setLogLevel(self, name, levelName):
		levelName = levelName.upper()
		if name == 'all':
			logger = self.root
		else:
			logger = self.getLogger(name)

		logger.setLevel(levelName)
		logger.debug(f"Enabled {name} debugging messages")

	def isDebugEnabled(self, name):
		level = self.getLogger(name).getEffectiveLevel()
		return level <= logging.DEBUG

	def temporaryIndent(self, width = 3):
		return self.defaultFormat.temporaryIndent(width)

	def getLogger(self, name = None):
		return logging.getLogger(name)

loggingFacade = LoggingFacade()

debugmsg = loggingFacade.default.debug
infomsg = loggingFacade.default.info
warnmsg = loggingFacade.default.warning
errormsg = loggingFacade.default.error

