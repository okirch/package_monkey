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
