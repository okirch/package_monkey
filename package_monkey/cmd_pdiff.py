##################################################################
#
# Subcommand for showing the difference between the results of
# two labelling runs.
#
##################################################################

import sys
import os
from functools import reduce

from .options import ApplicationBase
from .csvio import CSVReader
from .util import IndexFormatterTwoLevels, OptionalCaption
from .arch import *

class Placement(object):
	class Rpm(object):
		def __init__(self, name, component, topic, type):
			self.name = name
			self.epic = component
			self.topic = topic
			self.type = type

			if '.' in name:
				name, suffix = os.path.splitext(name)
				if suffix in ('.x86_64', '.s390x', '.ppc64le', '.aarch64', '.noarch',):
					self.name = name

			self.baseName = self.name

		def __str__(self):
			name = self.name
			# FIXME: should really check for GenericRpm.TYPE_RERGULAR here
			if self.type and self.type != 'rpm':
				name = f"{self.name}[{self.type}]"
			return f"{name} ({self.epic}/{self.topic})"

		def getSortKey(self):
			return (self.epic, self.topic, self.name)

		def isLike(self, other):
			return self.name == other.name and \
				self.epic == other.epic and \
				self.topic == other.topic

	class Build(object):
		def __init__(self, name, epic):
			self.name = name
			self.epic = epic

			self._packages = {}

		def __str__(self):
			return self.name

		def addRpm(self, rpm):
			assert(rpm.name not in self._packages)
			self._packages[rpm.name] = rpm

		@property
		def rpmNames(self):
			return set(self._packages.keys())

		@property
		def rpms(self):
			return set(self._packages.values())

	def __init__(self):
		self._packages = {}
		self._builds = {}
		self._rpmToBuild = {}
		self._ignoredRpms = set()

	@property
	def names(self):
		return set(self._packages.keys())

	def createRpm(self, name, epic, topic, build = None, type = None):
		assert(name not in self._packages)
		rpm = self.Rpm(name, epic, topic, type)
		self._packages[name] = rpm

		if build is not None:
			self._rpmToBuild[rpm.name] = build
			build.addRpm(rpm)

		return rpm

	def getRpm(self, name):
		return self._packages.get(name)

	def addIgnoredRpm(self, name, epic, topic, type = None):
		self._ignoredRpms.add(self.Rpm(name, epic, topic, type = type))

	@property
	def rpms(self):
		return self._packages.values()

	def createBuild(self, name, epic):
		build = self._builds.get(name)
		if build is None:
			build = self.Build(name, epic)
			self._builds[name] = build
		elif build.epic != epic:
			raise Exception(f"{build} changes epic from {build.epic} -> {epic}")

		return build

	def getBuild(self, name):
		return self._builds.get(name)

	@property
	def builds(self):
		return self._builds.values()

	def getBuildForRpm(self, rpm):
		return self._rpmToBuild.get(rpm.name)

	def displayNameList(self, title, names):
		if not names:
			return

		print(f"{title}:")

		packages = map(self.getRpm, names)
		packages = sorted(packages, key = self.Rpm.getSortKey)

		formatter = IndexFormatterTwoLevels()
		for pkg in packages:
			formatter.next(pkg.epic, pkg.topic, f"{pkg.name}")
		print()

class Renames(object):
	def __init__(self):
		self.packages = {}

	def add(self, oldPackage, newPackage):
		self.packages[oldPackage] = newPackage

class PackageDiffApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.renames = Renames()
		self.versionChanges = {}
		self._deltas = {}

	class BuildDelta(object):
		def __init__(self, buildName):
			self.buildName = buildName

			self.typeBuildRemoved = False
			self.typeBuildAdded = False
			self.someRpmsMoved = False
			self.someRpmsChangedVersions = False
			self.someRpmsRemoved = False
			self.someRpmsAdded = False

			self.rpmChanges = set()

		def __str__(self):
			return self.buildName

		def addChangedRpm(self, rpm, how = None):
			if how is not None:
				self.rpmChanges.add(f"{rpm} {how}")
			else:
				self.rpmChanges.add(f"{rpm}")

		def buildRemoved(self, oldBuild):
			self.typeBuildRemoved = True
			for rpm in oldBuild.rpms:
				self.addChangedRpm(f"{rpm} removed")

		def buildAdded(self, newBuild):
			self.typeBuildAdded = True
			for rpm in newBuild.rpms:
				self.addChangedRpm(f"{rpm} added")

		def rpmMoved(self, oldRpm, newRpm, newBuild):
			self.addChangedRpm(oldRpm, f"moved to {newBuild}")
			self.someRpmsMoved = True

		def rpmVersionChanged(self, oldRpm, newRpm):
			if oldRpm.topic == newRpm.topic:
				self.addChangedRpm((f"{oldRpm.name} -> {newRpm.name}"))
			elif oldRpm.epic == newRpm.epic:
				self.addChangedRpm((f"{oldRpm.name} ({oldRpm.topic}) -> {newRpm} ({newRpm.topic})"))
			else:
				self.addChangedRpm((f"{oldRpm} -> {newRpm}"))
			self.someRpmsChangedVersions = True

		def rpmRemoved(self, oldRpm):
			self.addChangedRpm(oldRpm, f"removed")
			self.someRpmsRemoved = True

		def rpmAdded(self, oldRpm):
			self.addChangedRpm(oldRpm, f"added")
			self.someRpmsAdded = True

		def shouldDisplay(self, restrict):
			if restrict is None:
				return True
			if restrict == 'added':
				return self.typeBuildAdded or self.someRpmsAdded
			if restrict == 'removed':
				return self.typeBuildRemoved or self.someRpmsRemoved
			if restrict == 'changed':
				return self.someRpmsMoved or self.someRpmsChangedVersions
			return False

		def display(self):
			how = []
			if self.typeBuildRemoved:
				how.append('build removed')
			if self.typeBuildAdded:
				how.append('build added')
			if self.someRpmsMoved:
				how.append('some rpms moved to other builds')
			if self.someRpmsChangedVersions:
				how.append('some rpms had a version change')
			if self.someRpmsRemoved:
				how.append('some rpms were removed')
			if self.someRpmsAdded:
				how.append('some rpms were added')
			if not how:
				how = 'something weird happened to me'

			print(f" - {self.buildName} ({', '.join(how)})")

			for desc in sorted(self.rpmChanges):
				print(f"    - {desc}")

	def createBuildDelta(self, build):
		buildName = build.name
		bd = self._deltas.get(buildName)
		if bd is None:
			bd = self.BuildDelta(buildName)
			self._deltas[buildName] = bd
		return bd

	@property
	def deltas(self):
		return sorted(self._deltas.values(), key = str)

	def load(self, path):
		if path is None:
			data = self.data
		elif path.startswith('@'):
			data = self.getSnapshot(path[1:])
			if data is None:
				raise Exception(f"Unknown snapshot {path}")
		else:
			return Placement.load(path)

		codebaseData = data.getCodebase(self.opts.codebase)

		placement = Placement()
		codebaseData.loadPackagesMinimal(placement)
		return placement

	def run(self):
		old = self.load(self.opts.oldPath or "@latest")
		new = self.load(self.opts.newPath)

		restrict = self.opts.restrict

		self.reportAdditionsRemovals(old, new, restrict)
		self.reportChangedPlacement(old, new, restrict)

	def reportChangedPlacement(self, old, new, restrict):
		if restrict not in (None, 'changed'):
			return

		oldNames = old.names
		newNames = new.names

		# display changes
		same = oldNames.intersection(newNames)
		changed = []
		for name in same:
			oldPkg = old.getRpm(name)
			newPkg = new.getRpm(name)
			if not oldPkg.isLike(newPkg):
				changed.append((oldPkg, newPkg))

		if changed:
			changed = sorted(changed, key = lambda pair: pair[0].getSortKey())
			self.displayChangedPackages(changed)

	def reportAdditionsRemovals(self, old, new, restrict):
		if restrict not in (None, 'added', 'removed', 'changed'):
			return

		oldNames = old.names
		newNames = new.names

		removedNames = oldNames.difference(newNames)
		removedRpms, buildsWithRemovals = self.processChangedNames(old, removedNames)

		addedNames = newNames.difference(oldNames)
		addedRpms, buildsWithAdditions = self.processChangedNames(new, addedNames)

		for oldBuild in buildsWithRemovals:
			bd = self.createBuildDelta(oldBuild)

			newBuild = new.getBuild(oldBuild.name)
			if newBuild is None:
				if oldBuild.rpmNames.issubset(removedNames):
					bd.buildRemoved(oldBuild)
				else:
					for name in oldBuild.rpmNames.difference(removedNames):
						oldRpm = old.getRpm(name)
						newRpm = new.getRpm(name)
						bd.rpmMoved(oldRpm, newRpm, new.getBuildForRpm(newRpm))
				continue

			rpmsRemovedFromThisBuild = oldBuild.rpms.intersection(removedRpms)

			versionChange = self.identifyVersionChange(oldBuild, newBuild)
			if versionChange is not None:
				for oldName, newName in versionChange.changedRpms:
					oldRpm = old.getRpm(oldName)
					newRpm = new.getRpm(newName)
					bd.rpmVersionChanged(oldRpm, newRpm)
					rpmsRemovedFromThisBuild.discard(oldRpm)
					addedRpms.discard(newRpm)

			for oldRpm in rpmsRemovedFromThisBuild:
				bd.rpmRemoved(oldRpm)

		for newBuild in buildsWithAdditions:
			rpmsAddedToThisBuild = newBuild.rpms.intersection(addedRpms)
			if not rpmsAddedToThisBuild:
				continue

			bd = self.createBuildDelta(newBuild)
			if rpmsAddedToThisBuild == newBuild.rpms:
				bd.buildAdded(newBuild)
			else:
				for newRpm in rpmsAddedToThisBuild:
					bd.rpmAdded(newRpm)

		if self._deltas:
			if restrict == 'added':
				caption = OptionalCaption("Added packages:")
			elif restrict == 'removed':
				caption = OptionalCaption("Removed packages:")
			elif restrict == 'changed':
				caption = OptionalCaption("Packages with version changes:")
			else:
				caption = OptionalCaption(f"Additions and removals of packages:")

			for bd in self.deltas:
				if not bd.shouldDisplay(restrict):
					continue

				caption()
				bd.display()

			print()

	def processChangedNames(self, placement, changedNames):
		changedRpms = set(map(placement.getRpm, changedNames))
		assert(None not in changedRpms)

		changedRpms = set(filter(lambda rpm: not rpm.name.startswith('promise:'), changedRpms))

		changedBuilds = set(map(placement.getBuildForRpm, changedRpms))
		if None in changedBuilds:
			for rpm in changedRpms:
				if placement.getBuildForRpm(rpm) is None:
					raise Exception(f"no build for rpm {rpm}?!")
			should_not_happen

		return changedRpms, changedBuilds

	def identifyLibPackages(self, nameSet):
		ret = {}
		for name in nameSet:
			if not name.startswith('lib'):
				continue

			stem = name
			while stem[-1].isdigit():
				stem = stem.rstrip("0123456789")
				for suffix in ('_alpha', '_beta', '_rc'):
					if stem.endswith(suffix):
						stem = stem[:-len(suffix)]
						break

				if stem[-1] == '_':
					stem = stem[:-1]

			if stem[-1] == '-':
				stem = stem[:-1]
			ret[stem] = name
		return ret

	class BuildVersionChange(object):
		def __init__(self, oldBuild, newBuild):
			self.oldBuild = oldBuild
			self.newBuild = newBuild
			self.change = {}

		def __str__(self):
			return str(self.oldBuild)

		@property
		def changedRpms(self):
			return sorted(self.change.items(), key = lambda p: str(p[0]))

	def identifyVersionChange(self, oldBuild, newBuild):
		removedLibs = self.identifyLibPackages(oldBuild.rpmNames.difference(newBuild.rpmNames))
		addedLibs = self.identifyLibPackages(newBuild.rpmNames.difference(oldBuild.rpmNames))

		if not removedLibs:
			return None

		idsChanged = set(removedLibs.keys()).intersection(set(addedLibs.keys()))
		if not idsChanged:
			return None

		result = self.BuildVersionChange(oldBuild, newBuild)
		for id in idsChanged:
			oldRpm = removedLibs[id]
			result.change[oldRpm] = addedLibs[id]
		return result

	def displayChangedPackages(self, listOfPairs):
		print("Changed packages")

		formatter = IndexFormatterTwoLevels()
		for old, new in listOfPairs:
			oldEpic = old.epic or "(no epic)"
			oldTopic = old.topic or "(no topic)"

			msg = f"{old.name} ->"
			if old.name != new.name:
				msg += f" {new.name}"
				if old.epic == new.epic and old.topic == new.topic:
					# special case: change of arch
					formatter.next(oldEpic, oldTopic, msg)
					continue

				msg += ";"

			if new.topic:
				msg += f" {new.epic}/{new.topic}"
			elif new.epic:
				msg += f" {new.epic} (no topic)"
			else:
				msg += " (no epic)"
			formatter.next(oldEpic, oldTopic, msg)
		print()

