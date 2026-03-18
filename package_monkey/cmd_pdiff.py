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

class BuildRecordBase(RecordBase):
	def __init__(self, name, epic = None):
		self.name = name
		self.epic = epic
		self.buildChanges = []
		self.rpmChanges = []

	def __bool__(self):
		return bool(self.buildChanges or self.rpmChanges)

	def render(self, formatter, view):
		buildChanges = view.buildChanges
		rpmChanges = view.rpmChanges

		if not buildChanges and not rpmChanges:
			return

		if self.epic is None:
			epicTag = "- NO EPIC -"
		else:
			epicTag = str(self.epic)

		buildTag = f"build {self.name}"
		if buildChanges:
			m = "; ".join(map(str, buildChanges))
			buildTag += f" ({m})"

		if not rpmChanges:
			formatter.next(epicTag, buildTag, "all rpms otherwise unchanged")
		else:
			for rpmChange in rpmChanges:
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

		self.noteBuildChange(TrivialRecord(self.changeType, "newly added build"))
		if build.binaries:
			for rpm in build.binaries:
				self.noteRpmAddition(rpm)
		else:
			self.noteRpmChange(TrivialRecord(self.changeType, "(empty build)"))

class BuildRemoveRecord(BuildRecordBase):
	changeType = RecordBase.RECORD_REMOVE

	def __init__(self, build):
		super().__init__(build.name, epic = build.epic)

		self.noteBuildChange(TrivialRecord(self.changeType, "removed build"))
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

class AttributeChangeRecord(RecordBase):
	changeType = RecordBase.RECORD_CHANGE

	def __init__(self, type, oldValue, newValue):
		self.type = type
		self.oldValue = oldValue
		self.newValue = newValue

	def __str__(self):
		return f"{self.type}: {self.oldValue} -> {self.newValue}"

class SoversionChangeRecord(RecordBase):
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
	def __init__(self, filter = None):
		self.formatter = IndexFormatter(sort = True)
		self.caption = 'Changed packages'
		self.filter = RenderFilterAcceptAll()

		if filter == 'changed':
			self.filter = RenderFilterChangedOnly()
			self.caption = 'Showing changes only'
		elif filter == 'added':
			self.filter = RenderFilterAddedOnly()
			self.caption = 'Showing additions only'
		elif filter == 'removed':
			self.filter = RenderFilterRemovedOnly()
			self.caption = 'Showing removals only'
		elif filter == 'noversions':
			self.filter = RenderFilterNoVersionChanges()
			self.caption = 'Showing all changes except version updates'
		else:
			assert(filter is None)

	def __del__(self):
		self.flush()

	def flush(self):
		if not self.formatter:
			# nothing to show
			return False

		if self.caption is not None:
			print(f"{self.caption}:")
		self.formatter.flush()
		return True

	def processBuildRecord(self, buildRec):
		view = self.filter.apply(buildRec)
		if view is not None:
			buildRec.render(self.formatter, view)

# Check whether a change should be displayed or not
class FilteredBuildRecord(object):
	def __init__(self, rec, buildChanges = None, rpmChanges = None):
		self.record = rec

		if buildChanges is None:
			buildChanges = rec.buildChanges
		self.buildChanges = buildChanges

		if rpmChanges is None:
			rpmChanges = rec.rpmChanges
		self.rpmChanges = rpmChanges

class RenderFilter(object):
	def filterChangeList(self, changeList):
		return list(filter(self.acceptChange, changeList))

	def fullView(self, rec):
		return FilteredBuildRecord(rec)

	def partialView(self, rec):
		return FilteredBuildRecord(rec,
			buildChanges = self.filterChangeList(rec.buildChanges),
			rpmChanges = self.filterChangeList(rec.rpmChanges))

class RenderFilterAcceptAll(RenderFilter):
	def apply(self, rec):
		return self.fullView(rec)

class RenderFilterAddedOnly(RenderFilter):
	def apply(self, rec):
		if rec.changeType == RecordBase.RECORD_ADD:
			return self.fullView(rec)
		if rec.changeType == RecordBase.RECORD_CHANGE:
			return self.partialView(rec)
		return None

	def acceptChange(self, rec):
		return rec.changeType == RecordBase.RECORD_TRIVIAL or \
		       rec.changeType == RecordBase.RECORD_ADD

class RenderFilterRemovedOnly(RenderFilter):
	def apply(self, rec):
		if rec.changeType == RecordBase.RECORD_REMOVE:
			return self.fullView(rec)
		if rec.changeType == RecordBase.RECORD_CHANGE:
			return self.partialView(rec)
		return None

	def acceptChange(self, rec):
		return rec.changeType == RecordBase.RECORD_TRIVIAL or \
		       rec.changeType == RecordBase.RECORD_REMOVE

class RenderFilterChangedOnly(RenderFilter):
	def apply(self, rec):
		if rec.changeType == RecordBase.RECORD_CHANGE:
			return self.fullView(rec)
		return None

	def acceptChange(self, rec):
		return rec.changeType == RecordBase.RECORD_TRIVIAL or \
		       rec.changeType == RecordBase.RECORD_CHANGE

class RenderFilterNoVersionChanges(RenderFilter):
	def apply(self, rec):
		return FilteredBuildRecord(rec,
			buildChanges = self.suppressVersionChanges(rec.buildChanges),
			rpmChanges = self.suppressVersionChanges(rec.rpmChanges))

	def suppressVersionChanges(self, changeList):
		result = []
		for change in changeList:
			if change.changeType == RecordBase.RECORD_CHANGE:
				if isinstance(change, AttributeChangeRecord) and \
				   change.type == 'version':
					continue

				# soversion changes are hidden inside an RpmChangeRecord; so we need
				# to dig into that and potentially suppress those.
				# We don't modify the original record; we create a clone.
				if isinstance(change, RpmChangeRecord):
					change = self.filterAndCloneRpmChange(change, SoversionChangeRecord)
					if change is None:
						continue

			result.append(change)

		return result

	def filterAndCloneRpmChange(self, change, suppressClass):
		details = []
		found = False
		for d in change.details:
			if isinstance(d, suppressClass):
				found = True
			else:
				details.append(d)

		if found:
			if not details:
				return None
			change = RpmChangeRecord(change.name)
			change.details = details

		return change

class PackageDiffApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def load(self, arg):
		db = self.loadDBForSnapshot(arg)
		labelFacade = self.loadClassificationForSnapshot(arg)

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
				rpm.choice = None
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

		renderer = DiffRenderer(filter = self.opts.restrict)

		if not self.opts.quiet:
			oldPath = self.opts.oldPath or '@latest'
			newPath = self.opts.newPath or 'current'
			print(f"Showing codebase diff between {oldPath} and {newPath}")
			print(f"  {oldPath} codebase dated {old.downloadTimestamp or 'unknown'}")
			print(f"  {newPath} codebase dated {new.downloadTimestamp or 'unknown'}")

		for record in delta:
			renderer.processBuildRecord(record)

		if not renderer.flush():
			print("No changes")

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
					buildChange.noteRpmMove(old.lookupRpm(rpmName), f"moved to build {newRpm.new_build}")
				else:
					buildChange.noteRpmRemoval(old.lookupRpm(rpmName))

			for rpmName in rpmNames.addedNames:
				buildChange.noteRpmAddition(new.lookupRpm(rpmName))

			if buildChange:
				delta.addBuildChange(buildChange)

		return delta
