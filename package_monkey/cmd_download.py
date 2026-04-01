#!/usr/bin/python3.11
#
# For a given codebase (like slfo), mirror all rpm headers for each architecture
# and prepare a solver file.
#

import os
import rpm
import subprocess

from .options import OBSApplicationBase, ApplicationBase
from .util import errormsg, warnmsg, infomsg
from .util import ThatsProgress
from .libsolv import *
from .newdb import *

class SolverDownloadApplication(OBSApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.repoCollection = None

	def run(self):
		solverDir = self.getCachePath('solve')
		cacheRoot = self.getCachePath("rpmhdrs")

		client = self.obsClient

		self.repoCollection = SolverRepositoryCollection.fromCodebase(self.productCodebase, solverDir)

		self.repoCollection.discoverStagingProjects(client, self.productCodebase)
		self.repoCollection.saveStagingList()

		if self.opts.staging:
			stagingIds = self.opts.staging.split(',')
			self.repoCollection.enableStagingsForDownload(stagingIds)

		totalCount = 0

		obsNameFilter = self.productCodebase.nameFilter

		for repository in sorted(self.repoCollection, key = str):
			obsProject = repository.obsProject

			downloadManager = obsProject.createDownloadManager(cacheRoot)
			downloadQueue = obsProject.prepareDownload(client, downloadManager, filter = obsNameFilter)
			repository.downloadQueue = downloadQueue

			packageCount = len(downloadQueue)
			totalCount += packageCount

			infomsg(f"  {packageCount:5} {repository}")

		progressMeter = ThatsProgress(totalCount, withETA = True)

		for repository in self.repoCollection:
			obsProject = repository.obsProject
			downloadQueue = repository.downloadQueue

			obsProject.performDownload(client, downloadQueue, progressMeter)

			downloadQueue.purgeCache()

			solver = RepositoryArchSolver(repository)
			if solver.isUptodate():
				infomsg(f"{repository}: solving file already up-to-date")
			else:
				files = set(downloadQueue.downloadedFiles)
				solver.produceSolver(files)

			# Associate OBS builds with the rpms they produce.
			# We save the build information to a secondary DB, to be merged
			# during the libsolv processing step into a single DB later.
			infomsg(f"{repository}: updating build results")

			db = NewDB()
			self.queryBuildResults(db, client, obsProject, nameFilter = obsNameFilter)
			repository.saveBuilds(db.builds)

	def queryBuildResults(self, db, client, project, nameFilter = None):
		resList = project.queryBuildResults(client)

		for result in resList:
			infomsg(f"{project}: found {len(result.status_list)} builds")
			for st in result.status_list:
				if nameFilter is not None and nameFilter.matchBuild(st.package):
					continue

				genericBuild = db.createBuild(st.package)
				genericBuild.status = st.code

			for p in result.binary_list:
				if nameFilter is not None and nameFilter.matchBuild(p.package):
					continue

				genericBuild = db.createBuild(p.package)
				self.updateBuildFromBinaryList(db, genericBuild, p.files, nameFilter = nameFilter)

	def updateBuildFromBinaryList(self, db, build, binaryList, nameFilter = None):
		source = None
		binaries = []
		buildTime = 0

		for f in binaryList:
			filename = f.filename

			if f.mtime is not None:
				fileBuildTime = int(f.mtime)
				if fileBuildTime > buildTime:
					buildTime = fileBuildTime

			buildArch = None # self.buildArch
			if filename.startswith("::"):
				words = filename[2:].split("::")
				if not words:
					raise Exception(filename)

				special = words.pop(0)
				if special == 'import' and len(words) == 2:
					buildArch, filename = words
				else:
					warnmsg(f"build results for {build} contain unexpected binary element {filename}")
					continue

			if not filename.endswith(".rpm"):
				continue

			rpmInfo = RpmInfo.parsePackageName(filename, buildArch = buildArch)

			if nameFilter and nameFilter.matchRpm(rpmInfo.name):
				continue

			rpm = db.createRpmFromInfo(rpmInfo)

			# This does not make sense any longer
			# rpm.buildTime = int(f.mtime)

			if rpmInfo.isSourcePackage:
				if source is not None:
					raise Exception(f"{self}/{build}: duplicate source rpms {rpm}, {source}")
				source = rpm

			build.addRpm(rpm)
			assert(rpm)

		# build.buildTime = buildTime

class RpmHeaderExtractorApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.repoCollection = None

	def run(self):
		db = self.codebaseData.loadExtraDB()

		solverDir = self.getCachePath('solve')
		cacheRoot = self.getCachePath("rpmhdrs")

		self.repoCollection = SolverRepositoryCollection.fromCodebase(self.productCodebase, solverDir)

		needsUpdate = []
		for repository in self.repoCollection:
			obsProject = repository.obsProject
			downloadManager = obsProject.createDownloadManager(cacheRoot)

			for name in downloadManager.localFilenames:
				hash, rpmName = name.split('-', maxsplit = 1)
				stem, suffix = os.path.splitext(rpmName)
				if suffix == '.rpm':
					rpmName = stem

				if rpmName.endswith('-debuginfo') or \
				   rpmName.endswith('-debugsource'):
					continue

				rpmInfo = db.maybeUpdate(rpmName, obsProject.buildArch, hash)
				if rpmInfo is None:
					continue

				path = downloadManager.fullpath(name)
				needsUpdate.append((rpmInfo, path, hash))

		totalCount = len(needsUpdate)

		if totalCount == 0:
			infomsg(f"No updates.")
		else:
			infomsg(f"Extracting information from {totalCount} updated rpms")

			progressMeter = ThatsProgress(totalCount, withETA = True)
			for rpmInfo, path, hash in needsUpdate:
				if (progressMeter.count % 1000) == 0:
					infomsg(f"   {progressMeter}: {rpmInfo.name}")

				self.loadRPM(rpmInfo, path, hash)
				progressMeter.tick()

			infomsg(f"   {progressMeter}: Done.")

		db.removeStaleEntries()

		self.codebaseData.saveExtraDB(db)

	def loadRPM(self, rpmInfo, path, hash):
		ts = rpm.TransactionSet()

		ts.setVSFlags(rpm.RPMVSF_NOHDRCHK | rpm.RPMVSF_MASK_NOSIGNATURES | rpm.RPMVSF_NOPAYLOAD)

		fdno = os.open(path, os.O_RDONLY)
		hdr = ts.hdrFromFdno(fdno)
		os.close(fdno)


		if hdr[rpm.RPMTAG_SOURCEPACKAGE]:
			# infomsg(f"{path}: source package")
			return

		rpmInfo.update(self.extractFields(hdr), hash)

	def extractFields(self, hdr):
		result = {}
		for key in 'name', 'version', 'release', 'summary', 'buildtime', 'description':
			value = hdr[key]
			if type(value) is bytes:
				value = value.decode('utf-8')
			result[key] = value
		return result
