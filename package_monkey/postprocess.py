##################################################################
#
# Helper classes for commands that operate on the data after
# the labelling step.
#
##################################################################

import os

__names__ = ['TrivialLabelFacade']

class TrivialLabelFacade(object):
	class FakeLayer(object):
		def __init__(self, name):
			self.name = name
			self.layer = None
			self.epics = set()

		def __str__(self):
			return self.name

		def __eq__(self, other):
			if other is None:
				return False
			return self.name == other.name

		def __hash__(self):
			return hash(self.name)

		def addEpic(self, epic):
			self.epics.add(epic)
			epic.layer = self

	class FakeEpic(object):
		def __init__(self, name):
			self.name = name
			self.layer = None
			self.ownerID = None
			self.lifecycleID = None

		def __str__(self):
			return self.name

		def __eq__(self, other):
			if other is None:
				return False
			return self.name == other.name

		def __hash__(self):
			return hash(self.name)

	class FakeLabelHints(object):
		def __init__(self, name, epic = None):
			self.name = name
			self.epic = epic
			self.choice = None
			self.option = None
			self.klass = None
			self.isIgnored = False

		def __str__(self):
			if self.choice is not None:
				result = str(self.choice)
			else:
				result = str(self.epic)
			if self.klass is not None:
				result += f"-{self.klass}"
			return result

	def __init__(self, dbPath):
		self._rpmHints = {}
		self._epics = {}
		self._layers = {}
		self._buildEpic = {}
		self.policy = None

		if os.path.isfile(dbPath):
			self.load(dbPath)

	def load(self, dbPath):
		currentEpic = None

		with open(dbPath) as f:
			for line in f.readlines():
				w = line.split()
				if not w:
					continue

				kwd = w.pop(0)
				if kwd == 'epic':
					currentEpic = self.createEpic(w.pop(0))
					for arg in w:
						assert('=' in arg)
						attr, value = arg.split('=')
						if attr == 'layer':
							layer = self.createLayer(value)
							layer.addEpic(currentEpic)
						else:
							raise Exception(f"{dbPath}: unknown attribute {arg} for epic {currentEpic}")
				elif kwd == 'build':
					self._buildEpic[w[0]] = currentEpic
				elif kwd == 'rpm':
					name = w.pop(0)
					self.buildLabelHints(name, currentEpic, w)

	def buildLabelHints(self, name, epic, params):
		entry = self.FakeLabelHints(name, epic)
		for p in params:
			key, value = p.split('=')
			if key == 'class':
				entry.klass = value
			else:
				setattr(entry, key, value)
		self._rpmHints[name] = entry

	def createLayer(self, name):
		if not name:
			return None
		layer = self._layers.get(name)
		if layer is None:
			layer = self.FakeLayer(name)
			self._layers[name] = layer
		return layer

	@property
	def layers(self):
		return iter(self._layers.values())

	def createEpic(self, name):
		if not name:
			return None
		epic = self._epics.get(name)
		if epic is None:
			epic = self.FakeEpic(name)
			self._epics[name] = epic
		return epic

	@property
	def epics(self):
		return iter(self._epics.values())

	# callback for Policy.load()
	def setOwner(self, epic, id):
		epic.ownerID = id

	# callback for Policy.load()
	def setLifecycle(self, epic, id):
		epic.lifecycleID = id

	def getEpicForBuild(self, build):
		if build is None:
			return None
		return self._buildEpic.get(build.name)

	def getHintsForRpm(self, rpm):
		return self._rpmHints.get(rpm.name)

