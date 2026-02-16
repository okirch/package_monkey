##################################################################
#
# Handle different copies of the state directory.
# Used to represent the regular state dir under ~/.local,
# as well as snapshot dirs.
#
##################################################################

import sys
import os
import time
import shutil

from .util import infomsg, errormsg, warnmsg
from .newdb import *
from .policy import Policy
from .download import DownloadInfo
from .writers import CsvPackageWriter
from .csvio import CSVReader

__names__ = ['SnapshotFactory', 'Snapshot']

class Snapshot(object):
	def __init__(self, path):
		self.path = path

	def getCodebase(self, name):
		return CodebaseLocation(os.path.join(self.path, name))

	def getProduct(self, name):
		return ProductLocation(os.path.join(self.path, name))

	def publish(self, path):
		if not os.path.isdir(path):
			errormsg(f"Cannot publish to {path}: not a directory")
			return 1

		shutil.copytree(self.path, path, dirs_exist_ok = True)
		return 0

##################################################################
# Provide access to all data in ~/.local/package_monkey/$codebase
##################################################################
class CodebaseLocation(object):
	def __init__(self, path):
		self.path = path
		self._db = None
		self._extraDB = None
		self._policy = None

		if not os.path.isdir(path):
			os.makedirs(path)

	def getPath(self, basename):
		return os.path.join(self.path, basename)

	def saveDownloadInfo(self, info):
		info.save(self.getPath("download.info"))

	def loadDownloadInfo(self):
		info = DownloadInfo()
		info.load(self.getPath("download.info"))
		return info

	@property
	def dbPath(self):
		return os.path.join(self.path, 'codebase.db')

	def loadDB(self, *args, **kwargs):
		if self._db is None:
			self._db = NewDB(*args, **kwargs)
			self._db.load(self.dbPath)

		return self._db

	def saveDB(self, db):
		db.save(self.dbPath)

	@property
	def extraDbPath(self):
		return os.path.join(self.path, 'info.db')

	def loadExtraDB(self):
		if self._extraDB is None:
			self._extraDB = ExtraDB()

			path = self.extraDbPath
			if os.path.exists(path):
				self._extraDB.load(path)

		return self._extraDB

	def saveExtraDB(self, db):
		db.save(self.extraDbPath)

	@property
	def policyPath(self):
		return os.path.join(self.path, 'policy.db')

	def loadPolicy(self, labelFacade = None):
		if self._policy is None:
			infomsg(f"Loading policy information from {self.policyPath}")
			self._policy = Policy()
			self._policy.load(self.policyPath, labelFacade)

		return self._policy

	class SimplePolicyLabelFacade(object):
		def __init__(self, classificationScheme):
			self.classificationScheme = classificationScheme

		@property
		def epics(self):
			for epic in self.classificationScheme.allEpics:
				if not epic.maintainerID and not epic.lifecycleID:
					continue

				yield epic, epic.maintainerID, epic.lifecycleID

	def savePolicy(self, classificationScheme):
		infomsg(f"Writing policy information to {self.policyPath}")
		policy = classificationScheme.policy
		facade = self.SimplePolicyLabelFacade(classificationScheme)
		policy.save(self.policyPath, facade)

	def saveClassification(self, classificationResult):
		classificationResult.save(self.getPath("classification.db"))

	def savePackagesMinimal(self, classificationResult):
		writer = CsvPackageWriter(self.getPath("packages.csv"))
		writer.writeClassificationResult(classificationResult)

	def loadPackagesMinimal(self, rpmFacade):
		path = self.getPath("packages.csv")

		infomsg(f"Loading {path}")
		csv = CSVReader(path)
		while True:
			e = csv.readObject()
			if e is None:
				break

			if not e.src:
				if e.topic.endswith('-noship'):
					# This seems to happen on and off...
					rpmFacade.addIgnoredRpm(e.package, e.epic, e.topic, type = e.rpmtype)
					continue
				raise Exception(f"{e.package} has no build")

			build = rpmFacade.createBuild(e.src, e.epic)
			rpm = rpmFacade.createRpm(e.package, e.epic, e.topic, build = build, type = e.rpmtype)

	def savePackagesFull(self, classificationResult):
		raise Exception(f"implementation removed")

	def loadPackagesFull(self, classificationScheme):
		raise Exception(f"implementation removed")

	def saveComponentModel(self, classificationResult):
		raise Exception(f"implementation removed")

##################################################################
# Provide access to all data in ~/.local/package_monkey/$product
##################################################################
class ProductLocation(object):
	def __init__(self, path):
		self.path = path

		if not os.path.isdir(path):
			os.makedirs(path)

	def getPath(self, basename):
		return os.path.join(self.path, basename)

class SnapshotFactory(object):
	def __init__(self, rootDir):
		self.root = rootDir

	def createSnapshot(self, statePath):
		name = time.strftime("%Y%m%dT%H%M%S")

		snapDir = os.path.join(self.root, name)
		if os.path.exists(snapDir):
			errormsg(f"Cannot create snapshot, {snapDir} already exists")
			return None

		infomsg(f"Copy {statePath} to {snapDir}")
		shutil.copytree(statePath, snapDir)

		return Snapshot(snapDir)

	def load(self, slug):
		link = os.path.join(self.root, slug)
		infomsg(link)
		if os.path.islink(link) or os.path.isdir(link):
			return Snapshot(link)

	def remember(self, slug, snapshot):
		link = os.path.join(self.root, slug)
		if os.path.islink(link):
			os.unlink(link)

		name = os.path.basename(snapshot.path)

		os.symlink(name, link)

	def remove(self, slug):
		link = os.path.join(self.root, slug)

		if not os.path.exists(link):
			return

		name = os.readlink(link)
		if '/' in name:
			error(f"Cannot remove {slug}: seems to point outside of {self.path}")
			return

		snapDir = os.path.join(self.root, name)
		if not os.path.isdir(snapDir):
			error(f"Cannot remove {slug}: does not point to a directory")
			return

		infomsg(f"Remove {snapDir}")
		shutil.rmtree(snapDir)
