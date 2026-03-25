##################################################################
#
# For a given codebase (like slfo), mirror all rpm headers for each architecture
# and prepare a solver file.
#
##################################################################

import os
import rpm
import subprocess

from .options import ApplicationBase
from .util import errormsg, warnmsg, infomsg
from .util import ThatsProgress
from .libsolv import *
from .newdb import *
from .obsclnt import OBSClient
from .download import DownloadInfo

class SolverDownloadApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.repoCollection = None
		self.infoGadget = RpmInfoUpdateGadget(self.codebaseData)

	def createOBSClient(self):
		apiURL = self.opts.api_url
		if apiURL is None:
			apiURL = self.productCodebase.apiURL
		if apiURL is None:
			infomsg(f"No api url given for this codebase, using {OBSClient.DEFAULT_API_URL}")

		obs = OBSClient(apiURL)
		obs.setCachePath(self.defaultHttpPath)
		if self.opts.http_cache_ttl:
			obs.setCacheTTL(60 * int(self.opts.http_cache_ttl))
		return obs

	def run(self):
		solverDir = self.getCachePath('solve')
		cacheRoot = self.getCachePath("rpmhdrs")

		client = self.createOBSClient()

		self.repoCollection = SolverRepositoryCollection.fromCodebase(self.productCodebase, solverDir)

		with loggingFacade.temporaryIndent():
			self.repoCollection.discoverStagingProjects(client, self.productCodebase)
			self.repoCollection.saveStagingList()

		if self.opts.staging:
			stagingIds = self.opts.staging.split(',')
			self.repoCollection.enableStagingsForDownload(stagingIds)

		totalCount = 0

		obsNameFilter = self.productCodebase.nameFilter

		infomsg(f"Checking projects for new rpms:")
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

			if len(downloadQueue) == 0 and repository.isUptodate():
				continue

			obsProject.performDownload(client, downloadQueue, progressMeter)

			downloadQueue.purgeCache()

			if self.infoGadget is not None:
				for path in downloadQueue.downloadedFiles:
					self.infoGadget.maybeUpdate(path, obsProject.buildArch)

			if repository.isUptodate():
				infomsg(f"{repository}: local cache is up-to-date")
				continue

			files = set(downloadQueue.downloadedFiles)
			repository.produceSolver(files)

			# Associate OBS builds with the rpms they produce.
			# We save the build information to a secondary DB, to be merged
			# during the libsolv processing step into a single DB later.
			infomsg(f"{repository}: updating build results")

			db = NewDB()
			self.queryBuildResults(db, client, obsProject, nameFilter = obsNameFilter)
			repository.saveBuilds(db.builds)

			repository.commitState()

		info = DownloadInfo()
		info.setTimestampNow()

		self.codebaseData.saveDownloadInfo(info)

		if self.infoGadget is not None:
			self.infoGadget.commit()

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
		solverDir = self.getCachePath('solve')
		cacheRoot = self.getCachePath("rpmhdrs")

		self.repoCollection = SolverRepositoryCollection.fromCodebase(self.productCodebase, solverDir)

		infoGadget = RpmInfoUpdateGadget(self.codebaseData, batching = True)
		for repository in self.repoCollection:
			obsProject = repository.obsProject
			downloadManager = obsProject.createDownloadManager(cacheRoot)

			for name in downloadManager.localFilenames:
				path = downloadManager.fullpath(name)
				infoGadget.maybeUpdate(path, obsProject.buildArch)

		totalCount = len(infoGadget)

		if totalCount == 0:
			infomsg(f"No updates.")
		else:
			infomsg(f"Extracting information from {totalCount} updated rpms")

			progressMeter = ThatsProgress(totalCount, withETA = True)
			for args in infoGadget:
				if (progressMeter.count % 1000) == 0:
					infomsg(f"   {progressMeter}: {args[0]}")

				infoGadget.update(*args)
				progressMeter.tick()

			infomsg(f"   {progressMeter}: Done.")

		infoGadget.commit()

class RpmInfoUpdateGadget(object):
	def __init__(self, codebaseData, batching = False):
		self.codebaseData = codebaseData
		self.extraDB = codebaseData.loadExtraDB()
		self.batching = batching
		self.queue = []
		self.modified = False

	def __len__(self):
		return len(self.queue)

	def __iter__(self):
		return iter(self.queue)

	def commit(self):
		if self.modified:
			self.extraDB.removeStaleEntries()
			self.codebaseData.saveExtraDB(self.extraDB)

	def maybeUpdate(self, path, buildArch):
		dirname, name = os.path.split(path)

		hash, rpmName = name.split('-', maxsplit = 1)
		stem, suffix = os.path.splitext(rpmName)
		if suffix == '.rpm':
			rpmName = stem

		if rpmName.endswith('-debuginfo') or \
		   rpmName.endswith('-debugsource'):
			return

		rpmInfo = self.extraDB.maybeUpdate(rpmName, buildArch, hash)
		if rpmInfo is None:
			return

		if self.batching:
			self.queue.append((rpmInfo, path, hash))
		else:
			self.update(rpmInfo, path, hash)

	def update(self, rpmInfo, path, hash):
		# infomsg(f"update {rpmInfo}")
		hdr = self.extractRpmHeaderFields(path, ('name', 'version', 'release', 'summary', 'buildtime', 'description'))
		if hdr is None:
			errormsg(f"Failed to update {rpmInfo}: unable to extract header fields from {path}")

		rpmInfo.update(hdr, hash)
		self.modified = True

	def extractRpmHeaderFields(self, path, fieldNames):
		ts = rpm.TransactionSet()

		ts.setVSFlags(rpm.RPMVSF_NOHDRCHK | rpm.RPMVSF_MASK_NOSIGNATURES | rpm.RPMVSF_NOPAYLOAD)

		fdno = os.open(path, os.O_RDONLY)
		hdr = ts.hdrFromFdno(fdno)
		os.close(fdno)

		if hdr[rpm.RPMTAG_SOURCEPACKAGE]:
			# infomsg(f"{path}: source package")
			return None

		result = {}
		for key in fieldNames:
			value = hdr[key]
			if type(value) is bytes:
				value = value.decode('utf-8')
			result[key] = value

		return result
