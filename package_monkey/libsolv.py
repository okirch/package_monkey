import os
import subprocess

from .util import errormsg, warnmsg, infomsg
from .obsclnt import OBSProject
from .arch import *

__names__ = ['SolverRepositoryCollection']

class RepositoryHandle(object):
	def __init__(self, projectName, repositoryName, arch, solverDir = None, stagingId = None, enabled = True):
		self.projectName = projectName
		self.repositoryName = repositoryName
		self.arch = arch
		self.solverDir = solverDir

		self.repoDirStateDir = os.path.join(solverDir, f"repo-{self.projectName}-{self.repositoryName}-{self.arch}")
		self.solverDataPath = os.path.join(self.repoDirStateDir, "rpms.solv")
		self.buildDataPath = os.path.join(self.repoDirStateDir, "builds")
		self.stateDataPath = os.path.join(self.repoDirStateDir, "state")

		if not os.path.isdir(self.repoDirStateDir):
			os.makedirs(self.repoDirStateDir)

		self.stagingId = stagingId

		self._obsProject = None
		self.downloadQueue = None

		# This is used in the prep stage to distinguish between stagings that we ignore and those we want to test
		self.enabled = enabled

	def __str__(self):
		return f"{self.projectName}/{self.repositoryName}/{self.arch}"

	@property
	def obsProject(self):
		if self._obsProject is None:
			project = OBSProject(self.projectName)
			project.buildRepository = self.repositoryName
			project.buildArch = self.arch

			self._obsProject = project
		return self._obsProject

	def produceSolver(self, files):
		infomsg(f"{self}: processing {len(files)} rpms")
		tempSolverPath = os.path.join(self.repoDirStateDir, f"rpms.{os.getpid()}.solv")
		with open(tempSolverPath, 'w') as fh:
			# -X	means "add auto patterns"
			# -m -	read manifest from stdin
			# -0	manifest entries separated by NUL characters
			p = subprocess.Popen(
				['rpms2solv', '-X', '-m', '-', '-0'], stdin=subprocess.PIPE, stdout=fh)
			p.communicate(bytes('\0'.join(files), 'utf-8'))
			fh.close()

		if p.wait() != 0:
			raise Exception("rpm2solv failed")

		os.rename(tempSolverPath, self.solverDataPath)
		infomsg(f"Created {self.solverDataPath}")

	# The remote state is just a hash of the "md5-rpmname" strings from server side
	# and helps us identify when there was a rebuild
	@property
	def remoteState(self):
		if self.downloadQueue is None:
			return None
		return self.downloadQueue.state

	def saveBuilds(self, builds):
		infomsg(f"Write {self.buildDataPath}")
		with open(self.buildDataPath, 'w') as f:
			for obsBuild in builds:
				print(f"{obsBuild.name} {obsBuild.status}", file = f)
				for rpm in obsBuild.rpms:
					print(f" - {rpm.name}", file = f)

	def loadBuilds(self, db):
		infomsg(f"Load {self.buildDataPath}")

		with open(self.buildDataPath, 'r') as f:
			build = None

			for line in f.readlines():
				w = line.split()
				if w[0] != '-':
					name = w[0]

					build = db.createBuild(name)
					if len(w) >= 2:
						build.setArchBuildStatus(self.arch, w[1])
				elif build is not None:
					rpm = db.createRpm(w[1])
					build.addRpm(rpm)

	def commitState(self):
		infomsg(f"Updating {self.stateDataPath}")
		with open(self.stateDataPath, "w") as f:
			print(f"{self.remoteState}", file = f)

	def isUptodate(self):
		if not os.path.isfile(self.solverDataPath) or \
		   not os.path.isfile(self.buildDataPath) or \
		   not os.path.isfile(self.stateDataPath):
			# this is our first run, or the user deleted one of our output files,
			# or the state file. It's all the same: re-run rpm2solv and update
			# the build file.
			return False

		with open(self.stateDataPath) as f:
			latestState = f.read().strip()
		return latestState == self.remoteState

class SolverRepositoryCollection(object):
	def __init__(self, architectures, solverDir):
		self.architectures = ArchSet(architectures)
		self.solverDir = solverDir
		self._projects = []

		if not os.path.isdir(solverDir):
			os.makedirs(solverDir)

	@classmethod
	def fromCodebase(klass, codebase, solverDir):
		result = klass(codebase.architectures, solverDir)

		for projectSpec in codebase.buildProjects:
			result.addProject(projectSpec)
		return result

	def createRepositoryHandle(self, *args, **kwargs):
		project = RepositoryHandle(*args, solverDir = self.solverDir, **kwargs)
		self._projects.append(project)
		return project

	def addProject(self, projectSpec):
		if '/' in projectSpec:
			projectName, repoName = projectSpec.split('/')
			self._addRepository(projectName, repoName)
		else:
			self._addRepository(projectSpec, 'standard')

	def _addRepository(self, projectName, repoName):
		for arch in sorted(self.architectures):
			self.createRepositoryHandle(projectName, repoName, arch)

	def discoverStagingProjects(self, client, codebase):
		for projectName in codebase.buildProjects:
			stagings = client.listStagings(projectName, status = 1, quiet = True) or []
			if stagings:
				infomsg(f"{projectName}: detected {len(stagings)} staging projects")
			for info in stagings:
				stagingId = info.name.split(':')[-1]
				self._addStaging(client, stagingId, info.name, enabled = False)

	def _addStaging(self, client, stagingId, projectName, repoName = 'standard', **kwargs):
		count = 0

		for arch in client.queryBuildArchitectures(projectName, repoName):
			if not archRegistry.isValidArchitecture(arch):
				# infomsg(f"  staging project {projectName}: ignore architecture {arch}")
				continue

			self.createRepositoryHandle(projectName, repoName, arch, stagingId = stagingId, **kwargs)
			count += 1

		return count

	def enableStagingsForDownload(self, stagingIds):
		if 'all' in stagingIds:
			for project in self._projects:
				if project.stagingId is not None:
					project.enabled = True
			return

		requestedIds = set(stagingIds)
		enabledIds = set()

		for project in self._projects:
			if project.stagingId in requestedIds:
				enabledIds.add(project.stagingId)
				project.enabled = True

		missing = requestedIds.difference(enabledIds)
		if missing:
			raise Exception(f"Cannot find staging project(s) {' '.join(missing)}")

	def enableStaging(self, stagingId):
		self.loadStagingList()

		stagingArchSet = ArchSet()
		for project in self._projects:
			if project.stagingId == stagingId:
				infomsg(f"Enable staging project {project}")
				stagingArchSet.add(project.arch)
				project.enabled = True

		if not stagingArchSet:
			raise Exception(f"Cannot enable staging {stagingId}: no project(s) found")

		if stagingArchSet != self.architectures:
			infomsg(f"Staging {stagingId} supports only these architecture(s): {stagingArchSet}")
			common = self.architectures.intersection(stagingArchSet)
			if not common:
				raise Exception(f"Cannot enable staging {stagingId}: no common architectures")

			delta = self.architectures.difference(stagingArchSet)

			infomsg(f"   Disabling {delta}")
			for project in self._projects:
				if project.arch not in common:
					project.enabled = False

			self.architectures.intersection_update(stagingArchSet)

	def __iter__(self):
		for project in self._projects:
			if project.enabled:
				yield project

	def saveStagingList(self):
		path = os.path.join(self.solverDir, 'stagings.txt')
		with open(path, "w") as f:
			for project in sorted(self._projects, key = str):
				if project.stagingId is not None:
					print(f"{project.stagingId} {project}", file = f)

	def loadStagingList(self):
		path = os.path.join(self.solverDir, 'stagings.txt')
		with open(path, "r") as f:
			for l in f.readlines():
				stagingId, projectSpec = l.split()
				projectName, repoName, arch = projectSpec.split('/')

				# By default, all stagings are disabled.
				self.createRepositoryHandle(projectName, repoName, arch, stagingId = stagingId, enabled = False)

