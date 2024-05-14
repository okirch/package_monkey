#
# Miscellaneous utility classes
#
import time
import fnmatch

##################################################################
# A simple class for batched processing
# You can use this when you have a long-running data processing loop
# and you do not want to lose all progress when you hit a bug during
# development.
##################################################################
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

class TimedExecutionBlock:
	def __init__(self, desc):
		self.description = desc
		self.timer = None

	def __enter__(self):
		if self.timer is None:
			infomsg(f"Starting to {self.description}")
			self.timer = ExecTimer()

	def __exit__(self, *args):
		if self.timer is not None:
			infomsg(f"Completed to {self.description}, {self.timer} elapsed")
			self.timer = None

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
	def __init__(self, total, withETA = False):
		self.count = 0
		self.total = total

		self.timer = None
		if withETA:
			self.timer = ExecTimer()

	@property
	def percent(self):
		if self.total == 0:
			return 100
		return 100.0 * self.count / self.total

	def __str__(self):
		return f"{self.percent:3.1f}%"

	@property
	def eta(self):
		if not self.timer:
			return None
		if not self.count:
			return 0

		elapsed = self.timer.elapsed
		secRemaining = int(elapsed / self.count * (self.total - self.count))

		minRemaining = int(secRemaining / 60)
		secRemaining %= 60
		if minRemaining == 0:
			return f"{secRemaining:02}s"

		hrsRemaining = int(minRemaining / 60)
		minRemaining %= 60
		if hrsRemaining == 0:
			return f"{minRemaining:02}:{secRemaining:02}"

		return f"{hrsRemaining:02}:{minRemaining:02}:{secRemaining:02}"

	def tick(self):
		self.count += 1

##################################################################
#
# A simple name matches
#
##################################################################
class NameMatcher:
	class ExactMatch(object):
		def __init__(self, pattern):
			self.pattern = pattern
			self.hit = False

		def match(self, name):
			return self.pattern == name

	class ShellMatch(object):
		def __init__(self, pattern):
			self.pattern = pattern
			self.hit = False

		def match(self, name):
			return fnmatch.fnmatchcase(name, self.pattern)

	def __init__(self, names = []):
		self.matches = []
		for name in names:
			if '*' in name or '?' in name:
				m = self.ShellMatch(name)
			else:
				m = self.ExactMatch(name)
			self.matches.append(m)

	def match(self, candidate):
		for m in self.matches:
			if m.match(candidate):
				m.hit = True
				return True

		return False

	def reportUnmatched(self):
		result = []
		for m in self.matches:
			if not m.hit:
				result.append(m.pattern)
		return result

##################################################################
##################################################################
class CountingDict(object):
	def __init__(self):
		self._count = {}

	def increment(self, key, count):
		try:
			self._count[key] += count
		except:
			self._count[key] = count

	def __getitem__(self, key):
		return self._count.get(key, 0)

##################################################################
#
# Format sorted triples of (tag1, tag2, message) so that
# recurring tags are hidden
#
##################################################################
class IndexFormatterBase(object):
	def __init__(self, msgfunc = print, sort = False):
		self.print = msgfunc
		self.sort = sort
		self.queue = []

	def __del__(self):
		if self.sort and self.queue:
			self.sort = False

			for entry in sorted(self.queue):
				self.next(*entry)
			self.queue = None

class IndexFormatterTwoLevels(IndexFormatterBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.lastTag1 = None
		self.lastTag2 = None

	def next(self, tag1, tag2, message):
		if self.sort:
			self.queue.append((tag1, tag2, message))
			return

		if self.lastTag1 != tag1:
			self.print(f"   {tag1}")
			self.lastTag1 = tag1
			self.lastTag2 = None

		if self.lastTag2 != tag2:
			self.print(f"      {tag2}")
			self.lastTag2 = tag2

		self.print(f"       - {message}")

class IndexFormatter(IndexFormatterBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.lastTag = None

	def next(self, tag, message):
		if self.sort:
			self.queue.append((tag, message))
			return

		if self.lastTag != tag:
			print(f"   {tag}")
			self.lastTag = tag

		print(f"    - {message}")

class TreeFormatter(object):
	LINE_DOWN = ' |'
	TEE_RIGHT = ' +->'
	ARROW_DOWN_RIGHT = ' \\->'
	NO_LINE = '  '
	HSPACE = ' '

	class Node(object):
		def __init__(self, value):
			self.value = value
			self.children = {}

		def add(self, value):
			name = str(value)
			child = self.children.get(name)
			if child is None:
				child = self.__class__(value)
				self.children[name] = child
			return child

	def __init__(self):
		self.root = self.Node(None)

	def render(self):
		return self.renderWork(self.root)

	def renderWork(self, node, prefix = '', seen = None):
		if seen is None:
			seen = set()
		seen.add(node)

		queue = sorted(node.children.items())

		prefix += ' '
		cc0 = self.TEE_RIGHT
		cc1 = self.LINE_DOWN
		while queue:
			name, child = queue.pop(0)
			if not queue:
				cc0 = self.ARROW_DOWN_RIGHT
				cc1 = self.NO_LINE

			yield (prefix + cc0 + self.HSPACE, child.value)

			# There's a cycle in the graph
			if child in seen:
				continue

			for pair in self.renderWork(child, prefix + cc1 + self.HSPACE):
				yield pair

	def standout(self, s):
		return s

class ANSITreeFormatter(TreeFormatter):
	LINE_DOWN = ' \u2502'
	TEE_RIGHT = ' \u251C\u2500>'
	ARROW_DOWN_RIGHT = ' \u2514\u2500>'
	NO_LINE = '  '
	HSPACE = ' '

	RED = '\u001b[31m'
	YELLOW = '\u001b[33m'
	NOCOL = '\u001b[0m'

	def standout(self, s):
		return self.YELLOW + str(s) + self.NOCOL

##################################################################
# Expand "foo${variable}bar" strings
##################################################################
class VariableExpander(object):
	def __init__(self, defines):
		import re

		self.defines = defines or {}
		self.regex = re.compile('([^$]*)\${([^}]*)}(.*)')

	def expand(self, s):
		if '$' not in s:
			return s

		orig = s
		result = ''
		while True:
			m = self.regex.match(s)
			if not m:
				result += s
				break

			before, name, after = m.groups()
			replace = self.defines.get(name)
			if replace is None:
				warnmsg(f"{name} expands to nothing while performinc variable expansion of \"{orig}\"")
				replace = ''

			result += before + str(replace)
			s = after


		debugmsg(f"variable expansion \"{orig}\" -> \"{result}\"")
		return result


##################################################################
#
# Classes to detect how often some object is referenced (ie
# determining the object's frequency), and for detecting which
# objects have been referenced how often (extracting the
# objects that fall into given frequency bands).
#
##################################################################
class FrequencyCounter(object):
	def __init__(self, objectToKeyFunc):
		self.eventCount = {}
		self.totalEvents = 0
		self.objects = {}
		self.objectToKeyFunc = objectToKeyFunc

	def addEvent(self, objects):
		for obj in objects:
			key = self.objectToKeyFunc(obj)
			if key not in self.objects:
				self.objects[key] = obj
			try:
				self.eventCount[key] += 1
			except:
				self.eventCount[key] = 1

		self.totalEvents += 1

	def frequencyBands(self, thresholds):
		filter = MultiBandFrequencyFilter(self.totalEvents, thresholds)
		for key, eventCount in sorted(self.eventCount.items(), key = lambda item: -item[1]):
			obj = self.objects[key]
			filter.add(obj, eventCount)
		return filter

	def __iter__(self):
		for key, count in sorted(self.eventCount.items(), key = lambda pair: pair[1], reverse = True):
			yield self.objects[key], count

# That's a fancy class name, isn't it? :-)
class MultiBandFrequencyFilter:
	class Item:
		def __init__(self, object, freq, relativeFreq):
			self.object = object
			self.freq = freq
			self.relativeFreq = relativeFreq

	class FrequencyBand:
		def __init__(self, threshold):
			self.threshold = threshold
			self.items = []

		@property
		def objects(self):
			for i in self.items:
				yield i.object

	def __init__(self, total, thresholds):
		self.total = total
		self.bands = []
		for n in sorted(thresholds, reverse = True):
			self.bands.append(self.FrequencyBand(n))

	def add(self, object, eventCount):
		relativeFreq = int(100.0 * eventCount / self.total)
		package = self.Item(object, eventCount, relativeFreq)
		for b in self.bands:
			if relativeFreq >= b.threshold:
				b.items.append(package)
				return

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
		logger.debug(f"Enabled {name} {levelName} messages")

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

