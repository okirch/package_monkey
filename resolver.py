##################################################################
#
# Classes and functions related to dependency resolution
#
##################################################################
import fnmatch

class ResolverHints:
	class ExactMatch:
		def __init__(self, name, *args):
			self.name = name

		def match(self, name):
			return self.name == name

	class NameMatch:
		def __init__(self, pattern, *args):
			self.pattern = pattern

		def match(self, name):
			return fnmatch.fnmatch(name, self.pattern)

	class OrderRule(object):
		def greaterThan(self, nameA, nameB):
			indexA = self.getIndex(nameA)
			indexB = self.getIndex(nameB)

			if indexA is None or indexB is None:
				return False

			return indexA < indexB

		def replaceEmptyString(self, words):
			def replaceEmpty(s):
				if s == "''" or s == ".":
					s = ""
				return s

			return list(map(replaceEmpty, words))

	class NameOrderRule(OrderRule):
		def __init__(self, names):
			self.matches = []
			for name in names:
				self.matches.append(self.intern(name))

		def intern(self, name):
			if '*' in name or '?' in name or '[' in name:
				match = ResolverHints.NameMatch(name)
			else:
				match = ResolverHints.ExactMatch(name)
			return match

		def getIndex(self, name):
			for i in range(len(self.matches)):
				if self.matches[i].match(name):
					return i
			return None

	class PrefixOrderRule(OrderRule):
		def __init__(self, words):
			self.prefixes = self.replaceEmptyString(words)

		def greaterThan(self, nameA, nameB):
			maxIndex = len(self.prefixes)
			for indexA in range(maxIndex):
				prefixA = self.prefixes[indexA]

				stemA = self.stripPrefix(nameA, prefixA)
				if not stemA:
					continue

				for prefixB in self.prefixes[indexA + 1:]:
					stemB = self.stripPrefix(nameB, prefixB)
					if stemA == stemB:
						return True

			return False

		def stripPrefix(self, word, prefix):
			if prefix:
				if not word.startswith(prefix):
					return None
				word = word[len(prefix):]
			return word

	class SuffixOrderRule(OrderRule):
		def __init__(self, words):
			self.suffixes = self.replaceEmptyString(words)

		def greaterThan(self, nameA, nameB):
			maxIndex = len(self.suffixes)
			for indexA in range(maxIndex):
				suffixA = self.suffixes[indexA]

				stemA = self.stripSuffix(nameA, suffixA)
				if not stemA:
					continue

				for suffixB in self.suffixes[indexA + 1:]:
					stemB = self.stripSuffix(nameB, suffixB)
					if stemA == stemB:
						return True

			return False

		def stripSuffix(self, word, suffix):
			if suffix:
				if not word.endswith(suffix):
					return None
				word = word[:-len(suffix)]
			return word

	def __init__(self):
		self._rules = []
		self._cache = {}

	def addNameOrder(self, words):
		self._rules.append(self.NameOrderRule(words))

	def addSuffixOrder(self, words):
		self._rules.append(self.SuffixOrderRule(words))

	def addPrefixOrder(self, words):
		self._rules.append(self.PrefixOrderRule(words))

	def finalize(self):
		pass

	def getPreferred(self, setOfNames):
		key = '|'.join(sorted(setOfNames))

		# SLE has just a very small number of unique ambiguous dependencies
		result = self._cache.get(key)
		if result is None:
			isBelow = set()
			for a in setOfNames:
				for b in setOfNames:
					if a is b:
						continue
					for edge in self._rules:
						if edge.greaterThan(a, b):
							# print(f"{a} >= {b}")
							isBelow.add(b)
							break

			result = setOfNames.difference(isBelow)
			self._cache[key] = result

		return result

	# not really a self test yet
	def selfTest(self):
		for input in (('gettext-its-gtk4', 'gettext-its-gtk3'),
				('libQt5Core-devel', 'libQt5Core-devel-32bit'),
				('systemd', 'systemd-mini'),
				('systemd-devel', 'systemd-mini-devel'),
				('foo', 'bar-mini'),
				('libfoo-32bit', 'libfoo'),
				('java-1_8_0-openjdk-headless', 'java-1_8_0-openjdk-devel'),
				('openmpi2-config', 'openmpi4-config', 'openmpi3-config'),
				('llvm15-libclang16', 'libclang16'),
				('glib2-devel', 'glib2-devel-32bit'),
				('libopenssl-1_1-devel', 'libopenssl-1_1-devel-32bit', 'libopenssl-3-devel', 'libopenssl-3-devel-32bit', 'libopenssl-devel'),
				):
			input = set(input)
			solved = self.getPreferred(input)
			print(input, " -> ", solved)

		fail

