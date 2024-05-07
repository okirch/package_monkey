##################################################################
#
# This class performs a large number of string matches in
# parallel. It performs best for matches that are either literal,
# contain only '?' wildcards or are "*-suffix" or "prefix-*"
# matches
#
# Other matches are supported as well, but fall back to python's
# native fnmatch engine behind the scenes.
#
##################################################################

from util import loggingFacade, infomsg, warnmsg, debugmsg
from util import ExecTimer
import fnmatch

if False:
	debug = infomsg
	debug2 = None
else:
	debug = None
	debug2 = None

class ParallelStringMatcher:
	class Result:
		def __init__(self):
			self._string = ""
			self._matches = []

		def restart(self):
			self._string = ""

		def shift(self, cc):
			self._string += cc

		def add(self, values, rest = ''):
			if values:
				self._matches.append((self._string + rest, values))

		@property
		def matches(self):
			return iter(self._matches)

		def __bool__(self):
			return bool(self._matches)

		def __len__(self):
			return len(self._matches)

		def __iter__(self):
			for match, values in self._matches:
				for s in values:
					yield s

	class State:
		def __init__(self, table):
			self.tables = [table]

		def shift(self, cc):
			nextTables = []
			for table in self.tables:
				next = table.get(cc)
				if next is not None:
					nextTables.append(next)
				if table._any:
					nextTables.append(table._any)
			self.tables = nextTables

			if debug2:
				debug2(f" {cc} [{', '.join(map(str, nextTables))}]")
			return bool(nextTables)

		@property
		def solutions(self):
			solutions = []
			for table in self.tables:
				solutions += table.solutions
			return solutions

	class Table:
		def __init__(self, name = None):
			self.name = name
			self.solutions = []
			self._any = None
			self.next = [None] * 256

		def __str__(self):
			return self.name or self

		def get(self, cc):
			n = ord(cc)
			assert(n < 256)
			return self.next[n]

		def add(self, cc):
			if cc == '?':
				return self.addAny()

			n = ord(cc)
			assert(n < 256)

			next = self.next[n]
			if next is None:
				next = self.__class__(self.name + cc)
				self.next[n] = next
			return next

		def addAny(self):
			if self._any is None:
				self._any = self.__class__(self.name + "?")
			return self._any

		def lookup(self, string, res):
			if debug:
				debug(f"{self} look up of \"{string}\"")

			state = ParallelStringMatcher.State(self)
			res.restart()

			table = self
			for cc in string:
				if not state.shift(cc):
					return

				res.shift(cc)

			res.add(state.solutions)

		def shortLookup(self, string, res):
			if debug:
				debug(f"{self} short look up of \"{string}\"")

			state = ParallelStringMatcher.State(self)
			res.restart()

			# if anything matches for "*", this is the place to report them:
			res.add(self.solutions, '*')

			for cc in string:
				if not state.shift(cc):
					break
				res.shift(cc)
				res.add(state.solutions, '*')
			return

		def fnmatchLookup(self, string, res):
			if debug:
				debug(f"{self} look up of \"{string}\"")

			state = ParallelStringMatcher.State(self)
			res.restart()

			string = list(string)
			while True:
				remainder = None
				for pattern, values in state.solutions:
					if remainder is None:
						# convert remainder of array of chars back into a string
						remainder = ''.join(string)

					if debug2:
						debug2(f"  match {remainder} against {pattern}")
					if fnmatch.fnmatchcase(remainder, pattern):
						res.add(values, pattern)

				if not string:
					break

				cc = string.pop(0)
				if not state.shift(cc):
					return

				res.shift(cc)

	def __init__(self):
		self.literalTable = self.Table("literal:")
		self.prefixTable = self.Table("prefix:")
		self.suffixTable = self.Table("suffix:")
		self.fullmatchTable = self.Table("fnmatch:")

	@staticmethod
	def containsWildcards(string):
		return '*' in string

	def add(self, string, value):
		if not self.containsWildcards(string):
			return self.addToTable(self.literalTable, string, value)

		parts = string.split("*")
		if len(parts) == 2 and parts[1] == "":
			# prefix match "foo-*" -> ['foo-', '']
			prefix = parts[0]
			return self.addToTable(self.prefixTable, prefix, value)

		if len(parts) == 2 and parts[0] == "":
			# suffix match "*-bar" -> ['', '-bar']
			suffix = parts[1]
			return self.addToTable(self.suffixTable, reversed(suffix), value)

		# patterns like "foo*bar" are harder. Rather than implementing a full LALR logic
		# here, let's just add a prefix match for "foo", then do the rest via a regular
		# fnmatch call.
		i = string.find('*')
		prefix = string[:i]
		rest = string[i:]
		self.addToTable(self.fullmatchTable, prefix, (rest, [value]))

	def addToTable(self, table, string, match):
		for cc in string:
			table = table.add(cc)

		table.solutions.append(match)

	def match(self, string):
		res = self.Result()

		self.literalTable.lookup(string, res)
		self.prefixTable.shortLookup(string, res)
		self.suffixTable.shortLookup(reversed(string), res)
		self.fullmatchTable.fnmatchLookup(string, res)
		return res

def selfTest():
	from functools import reduce

	patternVector = (
		"foozle",
		"foo*",
		"fooz*",
		"bar",
		"*bara",
		"python31?-frob",
		"foo?le",
		"python*-frob",
		"*-32bit-*",
	)

	argumentVector = (
		"blah",
		"foozle",
		"fooble",
		"foozy",
		"foo",
		"barbara",
		"python311-frob",
		"python312-frobnicator",
		"update-test-32bit-pkg",
	)

	pm = ParallelStringMatcher()
	for pattern, id in zip(patternVector, range(100)):
		pm.add(pattern, id)

	numFailures = 0
	for argument in argumentVector:
		expected = set()
		for pattern, id in zip(patternVector, range(100)):
			if fnmatch.fnmatchcase(argument, pattern):
				expected.add(id)

		m = pm.match(argument)
		# print(argument, list(m.matches))

		found = reduce(set.union, (set(id) for (expr, id) in m.matches), set())
		expected = set(expected)
		if found == expected:
			print(f"[OK] {argument}")
		else:
			print(f"[FAIL] {argument}")
			print(f"       expected [{' '.join(map(str, expected))}]")
			print(f"       found [{' '.join(map(str, found))}]")
			numFailures += 1

	if numFailures:
		raise Exception(f"Detected {numFailures} test failure(s)")

def benchmark():
	class Match:
		def __init__(self, pattern, type, priority, label):
			self.type = type # binary or source
			self.label = label

			assert(priority <= 10)
			precedence = (10 - priority) * 100

			if '?' not in pattern and '*' not in pattern:
				precedence += 1000

			precedence += len(pattern)

			self.precedence = precedence

		def __str__(self):
			return f"{self.label}/{self.precedence}"

	pm = ParallelStringMatcher()
	with open("tests/patterns.txt") as f:
		for line in f.readlines():
			type, pattern, priority, label = line.split()

			pm.add(pattern, Match(pattern, type, int(priority), label))

	with open("tests/arguments.txt") as f:
		numTests = 0
		numSucceeded = 0

		for line in f.readlines():
			type, name, label = line.split()

			matches = pm.match(name)
			# print(name, label, matches)

			matches = filter(lambda m: m.type == type, matches)

			found = sorted(matches, key = lambda m: m.precedence, reverse = True)

			id = f"{type} {name}"
			if not found:
				warnmsg(f"[FAIL] {id} did not yield a match")
			elif label != found[0].label:
				warnmsg(f"[FAIL] {id} should have yielded {label}")
				warnmsg(f"   found instead: {' '.join(map(str, found))}")
			else:
				# infomsg(f"[OK] {id} {label}")
				numSucceeded += 1

			numTests += 1

		print(f"{numTests} performed; {numSucceeded} okay; {numTests - numSucceeded} failed")


if __name__ == '__main__':
	loggingFacade.enableStdout()

	selfTest()

	timer = ExecTimer()
	benchmark()
	infomsg(f"benchmark completed in {timer}")

