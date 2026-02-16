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
from .util import IndexFormatter, OptionalCaption, loggingFacade
from .postprocess import *
from .arch import *

class RecordBase(object):
	RECORD_TRIVIAL	= 0
	RECORD_ADD	= 1
	RECORD_REMOVE	= 2
	RECORD_CHANGE	= 3

	def shouldDisplay(self, requestedType):
		if requestedType < 0:
			return True
		if self.changeType == requestedType:
			return True
		if self.changeType == self.RECORD_TRIVIAL:
			return True
		return False

class BuildRecordBase(RecordBase):
	def __init__(self, name, epic = None):
		self.name = name
		self.epic = epic
		self.buildChanges = []
		self.rpmChanges = []

	def __bool__(self):
		return bool(self.buildChanges or self.rpmChanges)

	def filter(self, changeType):
		if changeType == self.changeType:
			return self

		if not any(rec.changeType == changeType for rec in self.rpmChanges):
			return None

		ret = BuildRecordBase(self.name, epic = self.epic)
		for rec in self.rpmChanges:
			if rec.changeType == changeType:
				ret.rpmChanges.append(rec)

		return ret

	def render(self, formatter, requestedType = -1):
		if self.epic is None:
			epicTag = "- NO EPIC -"
		else:
			epicTag = str(self.epic)

		assert(bool(self))

		buildTag = f"build {self.name}"
		if self.buildChanges:
			m = "; ".join(map(str, self.buildChanges))
			buildTag += f" ({m})"

		if not self.rpmChanges:
			formatter.next(epicTag, buildTag, "all rpms otherwise unchanged")
		else:
			for rpmChange in self.rpmChanges:
				if not rpmChange.shouldDisplay(requestedType):
					continue

				msg = str(rpmChange)
				formatter.next(epicTag, buildTag, msg)

	def noteBuildChange(self, record):
		self.buildChanges.append(record)

	def noteRpmChange(self, rpmRecord):
		self.rpmChanges.append(rpmRecord)

	def noteRpmRemoval(self, oldRpm):
		self.noteRpmChange(RpmRemoveRecord(oldRpm))

	def noteRpmAddition(self, newRpm):
		self.noteRpmChange(RpmAddRecord(newRpm))

	def noteRpmMove(self, rpm, detail):
		self.noteRpmChange(RpmMoveRecord(rpm, detail))

class BuildChangeRecord(BuildRecordBase):
	changeType = RecordBase.RECORD_CHANGE

class BuildAddRecord(BuildRecordBase):
	changeType = RecordBase.RECORD_ADD

	def __init__(self, build):
		super().__init__(build.name, epic = build.epic)

		self.buildChanges.append("newly added build")
		if build.binaries:
			for rpm in build.binaries:
				self.noteRpmAddition(rpm)
		else:
			self.noteRpmChange(TrivialRecord(self.changeType, "(empty build)"))

class BuildRemoveRecord(BuildRecordBase):
	changeType = RecordBase.RECORD_REMOVE

	def __init__(self, build):
		super().__init__(build.name, epic = build.epic)

		self.buildChanges.append("removed build")
		if build.binaries:
			for rpm in build.binaries:
				self.noteRpmRemoval(rpm)
		else:
			self.noteRpmChange(TrivialRecord(self.changeType, "(empty build)"))

class RpmChangeRecord(RecordBase):
	changeType = RecordBase.RECORD_CHANGE

	def __init__(self, name):
		self.name = name
		self.details = []

	def __bool__(self):
		return bool(self.details)

	def __str__(self):
		msg = [self.name] + list(map(str, self.details))
		return "; ".join(msg)

	def noteAttributeChange(self, type, oldValue, newValue):
		self.details.append(AttributeChangeRecord(type, oldValue, newValue))

class RpmAddRemoveRecordBase(RecordBase):
	def __init__(self, rpm):
		self.rpm = rpm

	def toString(self, verb):
		msg = [f"{verb} rpm {self.rpm}"]
		if self.rpm.klass:
			msg.append(f"class {self.rpm.klass}")
		if self.rpm.choice:
			msg.append(f"choice {self.rpm.choice}")
		return "; ".join(msg)

class RpmAddRecord(RpmAddRemoveRecordBase):
	changeType = RecordBase.RECORD_ADD

	def __str__(self):
		return self.toString('added')

class RpmRemoveRecord(RpmAddRemoveRecordBase):
	changeType = RecordBase.RECORD_REMOVE

	def __str__(self):
		return self.toString('removed')

class RpmMoveRecord(RpmAddRemoveRecordBase):
	changeType = RecordBase.RECORD_CHANGE

	def __init__(self, rpm, detail):
		self.rpm = rpm
		self.detail = detail

	def __str__(self):
		return self.toString(self.detail)

class AttributeChangeRecord(object):
	changeType = RecordBase.RECORD_CHANGE

	def __init__(self, type, oldValue, newValue):
		self.type = type
		self.oldValue = oldValue
		self.newValue = newValue

	def __str__(self):
		return f"{self.type}: {self.oldValue} -> {self.newValue}"

class SoversionChangeRecord(object):
	changeType = RecordBase.RECORD_CHANGE

	def __init__(self, oldName):
		self.oldName = oldName

	def __str__(self):
		return f"soversion change (was {self.oldName})"

class TrivialRecord(RecordBase):
	def __init__(self, changeType, msg):
		self.changeType = changeType
		self.msg = msg
	
	def __str__(self):
		return self.msg

class Renames(object):
	def __init__(self):
		self.packages = {}

	def add(self, oldPackage, newPackage):
		self.packages[oldPackage] = newPackage

class LibraryPackageMap(object):
	def __init__(self, nameSet):
		self.mapping = {}

		ambiguous = set()
		for name in nameSet:
			if not name.startswith('lib'):
				continue

			stem = name
			stemplus = ""

			for suffix in ("-32bit", "-x86-64-v3"):
				if stem.endswith(suffix):
					stemplus = f"{suffix}{stemplus}"
					stem = stem[:-len(suffix)]

			while stem[-1].isdigit():
				stem = stem.rstrip("0123456789")
				for suffix in ('_alpha', '_beta', '_rc'):
					if stem.endswith(suffix):
						stem = stem[:-len(suffix)]
						break

				if stem[-1] in ('_', '-'):
					stem = stem[:-1]

			# glue suffixes back on
			stem += stemplus

			if stem in self.mapping:
				ambiguous.add(stem)

			self.mapping[stem] = name

		self.stems = set(self.mapping.keys()).difference(ambiguous)
		self.names = set(self.mapping[stem] for stem in self.stems)

	def get(self, stem):
		return self.mapping[stem]

class RpmNameClassification(object):
	def __init__(self, oldNames, newNames):
		self.oldNames = set(oldNames)
		self.newNames = set(newNames)

		self.commonNames = self.oldNames.intersection(self.newNames)

		self.oldLibraries = LibraryPackageMap(self.oldNames.difference(self.commonNames))
		self.newLibraries = LibraryPackageMap(self.newNames.difference(self.commonNames))
		self.sharedLibraryNames = self.oldLibraries.stems.intersection(self.newLibraries.stems)

		self.removedNames = self.oldNames.difference(self.commonNames).difference(self.oldLibraries.names)
		self.addedNames = self.newNames.difference(self.commonNames).difference(self.newLibraries.names)

	@property
	def soversionChanges(self):
		for name in self.sharedLibraryNames:
			yield self.oldLibraries.get(name), self.newLibraries.get(name)

class CodebaseDelta(object):
	def __init__(self):
		self.buildChanges = []

	def addBuildChange(self, rec):
		self.buildChanges.append(rec)

	def __iter__(self):
		return iter(self.buildChanges)

class DiffRenderer(object):
	def __init__(self, onlyType = -1):
		self.onlyType = onlyType

		self.formatter = IndexFormatter(sort = True)

	def processBuildRecord(self, buildRec):
		if buildRec.shouldDisplay(self.onlyType):
			self.processBuildRecordFull(buildRec)
		elif buildRec.changeType == RecordBase.RECORD_CHANGE:
			self.processBuildRecordFiltered(buildRec, self.onlyType)

	def processBuildRecordFull(self, buildRec):
		buildRec.render(self.formatter)

	def processBuildRecordFiltered(self, buildRec, requestedType):
		buildRec.render(self.formatter, requestedType)

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

		db = codebaseData.loadDB()

		labelFacade = TrivialLabelFacade(codebaseData.getPath("classification.db"))
		labelFacade.policy = codebaseData.loadPolicy(labelFacade)

		for build in db.builds:
			build.rpmNames = set(rpm.name for rpm in build.binaries)
			epic = labelFacade.getEpicForBuild(build)
			if epic is not None:
				build.epic = epic

			build.version = build.commonBuildVersion or "undefined"

		for rpm in db.rpms:
			hints = labelFacade.getHintsForRpm(rpm)
			if hints is None:
				rpm.topic = None
				rpm.epic = None
				rpm.klass = 'default'
				if rpm.new_build is not None:
					rpm.epic = rpm.new_build.new_epic
			else:
				topic = hints.epic
				if hints.choice is not None:
					topic = hints.choice
				rpm.topic = f"{topic}-{hints.klass}"
				rpm.epic = hints.epic
				rpm.klass = hints.klass
				rpm.choice = hints.choice

		db.names = set(rpm.name for rpm in db.rpms)

		return db

	def run(self):
		old = self.load(self.opts.oldPath or "@latest")
		new = self.load(self.opts.newPath)

		restrict = self.opts.restrict

		# Now that we've loaded everything (and avoided the debug chatter), enable logging to stdout:
		loggingFacade.enableStdout()

		delta = self.computeCodebaseChanges(old, new)

		if self.opts.restrict == 'changed':
			renderer = DiffRenderer(RecordBase.RECORD_CHANGE)
		elif self.opts.restrict == 'added':
			renderer = DiffRenderer(RecordBase.RECORD_ADD)
		elif self.opts.restrict == 'removed':
			renderer = DiffRenderer(RecordBase.RECORD_REMOVE)
		else:
			renderer = DiffRenderer()

		print(f"Changed packages:")
		for record in delta:
			renderer.processBuildRecord(record)

	def compareRpms(self, buildChange, rpmName, oldRpm, newRpm, extraRecords = []):
		# HACK: detect noship -> noship
		if newRpm.topic is None and oldRpm.topic is None:
			return

		rpmChange = RpmChangeRecord(rpmName)

		# add any records passed to us by the caller
		rpmChange.details += extraRecords

		if newRpm.topic is None:
			# HACK: detect * -> noship
			rpmChange.noteAttributeChange('class', oldRpm.klass, "noship")
		elif oldRpm.topic is None:
			# HACK: detect noship -> *
			rpmChange.noteAttributeChange('class', "noship", newRpm.klass)
		else:
			if oldRpm.choice != newRpm.choice:
				rpmChange.noteAttributeChange('choice', oldRpm.choice, newRpm.choice)
			if oldRpm.klass != newRpm.klass:
				rpmChange.noteAttributeChange('class', oldRpm.klass, newRpm.klass)

		if rpmChange:
			buildChange.noteRpmChange(rpmChange)

	def computeCodebaseChanges(self, old, new):
		delta = CodebaseDelta()

		oldBuildNames = set(build.name for build in old.builds)
		newBuildNames = set(build.name for build in new.builds)

		# show build additions
		for buildName in newBuildNames.difference(oldBuildNames):
			newBuild = new.lookupBuild(buildName)
			delta.addBuildChange(BuildAddRecord(newBuild))

		# show build removals
		for buildName in oldBuildNames.difference(newBuildNames):
			oldBuild = old.lookupBuild(buildName)
			delta.addBuildChange(BuildRemoveRecord(oldBuild))

		# loop over common builds and see if they moved between epics
		for buildName in oldBuildNames.intersection(newBuildNames):
			oldBuild = old.lookupBuild(buildName)
			newBuild = new.lookupBuild(buildName)

			buildChange = BuildChangeRecord(buildName, epic = newBuild.epic)

			if oldBuild.epic != newBuild.epic:
				buildChange.noteBuildChange(AttributeChangeRecord('epic', oldBuild.epic, newBuild.epic))

			if oldBuild.version != newBuild.version:
				buildChange.noteBuildChange(AttributeChangeRecord('version', oldBuild.version, newBuild.version))

			rpmNames = RpmNameClassification(oldBuild.rpmNames, newBuild.rpmNames)

			for rpmName in rpmNames.commonNames:
				oldRpm = old.lookupRpm(rpmName)
				newRpm = new.lookupRpm(rpmName)

				self.compareRpms(buildChange, rpmName, oldRpm, newRpm)

			for oldLibName, newLibName in rpmNames.soversionChanges:
				oldRpm = old.lookupRpm(oldLibName)
				newRpm = new.lookupRpm(newLibName)

				self.compareRpms(buildChange, newLibName, oldRpm, newRpm,
						extraRecords = [
							SoversionChangeRecord(oldRpm)
						])

			for rpmName in rpmNames.removedNames:
				newRpm = new.lookupRpm(rpmName)
				if newRpm is not None:
					buildChange.noteRpmMove(old.lookupRpm(rpmName), f"moved to build {newRpm.build}")
				else:
					buildChange.noteRpmRemoval(old.lookupRpm(rpmName))

			for rpmName in rpmNames.addedNames:
				buildChange.noteRpmAddition(new.lookupRpm(rpmName))

			if buildChange:
				delta.addBuildChange(buildChange)

		return delta

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

			newBuild = new.lookupBuild(oldBuild.name)
			if newBuild is None:
				if oldBuild.rpmNames.issubset(removedNames):
					bd.buildRemoved(oldBuild)
				else:
					for name in oldBuild.rpmNames.difference(removedNames):
						oldRpm = old.lookupRpm(name)
						newRpm = new.lookupRpm(name)
						bd.rpmMoved(oldRpm, newRpm, newRpm.new_build)
				continue

			rpmsRemovedFromThisBuild = oldBuild.rpms.intersection(removedRpms)

			versionChange = self.identifyVersionChange(oldBuild, newBuild)
			if versionChange is not None:
				for oldName, newName in versionChange.changedRpms:
					oldRpm = old.lookupRpm(oldName)
					newRpm = new.lookupRpm(newName)
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
		changedRpms = set(map(placement.lookupRpm, changedNames))
		assert(None not in changedRpms)

		changedRpms = set(filter(lambda rpm: not rpm.name.startswith('promise:'), changedRpms))

		changedBuilds = set(rpm.new_build for rpm in changedRpms)
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

	def displayChangedPackages(self, buildChanges):
		print("Changed packages")

		formatter = IndexFormatter(sort = True)
		for buildRec in buildChanges:
			buildRec.render(formatter)

		formatter.flush()
