import time
import datetime

from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .util import ANSITreeFormatter

class PackageHistory(object):
	# name corresponds to build.canonicalName
	def __init__(self, name):
		self.name = name
		self.label = None

		self.incarnations = []

		# Internal, used by PackageHistoryChaserJob
		self.proxies = []

	def __str__(self):
		return self.name

	def createIncarnation(self, obsProject, obsBuild):
		result = BuildIncarnation(obsProject, obsBuild)
		self.incarnations.append(result)

		return result

	# Iterate over all incarnations, and yield a pair that describes a transition
	# from one incarnation to the next.
	# Assume the next glibc update we find is for SUSE:SLE-15-SP4:GA.
	#  a)	if there was a previous glibc update released to SUSE:SLE-15-SP4:GA,
	#	we return that as the one to compare against
	#  b)	otherwise, go to the latest update from another project, which should
	#	be the youngest sibling project that received an update.
	#	(IOW, if we had updates to SP3:GA and SP2:Update, we want the one
	#	from SP3:GA because that project is "younger")
	#	
	def __iter__(self):
		incarnations = self.incarnations.copy()
		if not incarnations:
			return

		# Sort incarnations by project order increasing build time
		# NB: the project order is currently a bit accidental, IOW it happens
		# to work with SLE-15 because
		#	"SLE-15" < "SLE-15-SP1"
		# and	"GA" < "Update"
		# The more robust approach would be to get the project order from the
		# product family data
		incarnations.sort(key = lambda i: (i.obsProject.name.split(':'), i.buildTime))

		first = incarnations.pop(0)
		youngestProject = first.obsProject

		# Track the most recent update per project.
		updateStreams = {}
		updateStreams[first.obsProject] = first

		# FIXME: this is probably useful to the caller, but
		# how do we properly report this?
		yield BuildReleaseReport(None, first)

		for current in incarnations:
			previous = updateStreams.get(current.obsProject)
			if previous is None:
				# we haven't seen this project before. 
				# Compare the current incarnation to the latest
				# one from the youngest project, and then make
				# this project the youngest
				previous = updateStreams[youngestProject]
				youngestProject = current.obsProject

			updateStreams[current.obsProject] = current

			yield BuildReleaseReport(previous, current)

	class ReleaseStats:
		def __init__(self, name):
			self.name = name
			self.releases = 0
			self.rpmsAdded = 0
			self.rpmsRemoved = 0
			self.releasesWithRequirementsChanges = 0
			self.versionUpdates = 0

		def update(self, other):
			self.releases += other.releases
			self.rpmsAdded += other.rpmsAdded
			self.rpmsRemoved += other.rpmsRemoved
			self.releasesWithRequirementsChanges += other.releasesWithRequirementsChanges
			self.versionUpdates += other.versionUpdates

	@property
	def stats(self):
		stats = self.ReleaseStats(self.name)

		for release in self:
			stats.releases += 1

			stats.rpmsAdded += len(release.addedRpms)
			stats.rpmsRemoved += len(release.droppedRpms)

			hasVersionUpdates = 0
			if release.updatedRpms:
				# If several packages in this release has a version update, still
				# count this as 1. Otherwise, monster packages like texlive will
				# be totally off the map
				for oldVersion, newVersion, rpms in release.versionUpdates:
					if oldVersion != newVersion:
						hasVersionUpdates = 1

				if len(list(release.requirementsChanges)) > 0:
					stats.releasesWithRequirementsChanges += 1

			stats.versionUpdates += hasVersionUpdates

		return stats


class BuildIncarnation(object):
	def __init__(self, obsProject, obsBuild):
		self.obsProject = obsProject
		self.obsBuild = obsBuild
		self.buildTime = int(obsBuild.buildTime)
		self._rpms = {}

	def __str__(self):
		return f"{self.obsProject}/{self.obsBuild}"

	@property
	def rpms(self):
		return self._rpms.values()

	@property
	def rpmNames(self):
		return set(self._rpms.keys())

	def addRpm(self, rpm):
		self._rpms[rpm.name] = rpm

	def getRpm(self, name):
		return self._rpms.get(name)

class BuildReleaseReport(object):
	# build arguments are actually BuildIncarnation instances
	def __init__(self, oldBuild, newBuild):
		self.oldBuild = oldBuild 
		self.newBuild = newBuild 

		self.droppedRpms = []
		self.addedRpms = []
		self.updatedRpms = []

		if oldBuild is None:
			return

		for name in oldBuild.rpmNames.union(newBuild.rpmNames):
			self.addChange(oldBuild.getRpm(name), newBuild.getRpm(name))

	@property
	def buildTime(self):
		if self.newBuild.buildTime is None:
			return None

		return time.ctime(self.newBuild.buildTime)

	@property
	def buildDate(self):
		if self.newBuild.buildTime is None:
			return None

		dt = datetime.date.fromtimestamp(self.newBuild.buildTime)
		return str(dt)

	def addChange(self, oldRpm, newRpm):
		if newRpm is None:
			# infomsg(f"  {oldRpm.name}: dropped")
			self.droppedRpms.append(oldRpm)
		elif oldRpm is None:
			# infomsg(f"  {newRpm.name}: added")
			self.addedRpms.append(newRpm)
		else:
			# infomsg(f"  {oldRpm.name}: possible dep change")
			self.updatedRpms.append((oldRpm, newRpm))

	@property
	def versionUpdates(self):
		oldVersions = set()
		newVersions = set()
		for oldRpm, newRpm in self.updatedRpms:
			oldVersions.add(oldRpm.version)
			newVersions.add(newRpm.version)

		if len(oldVersions) == 1 and len(newVersions) == 1:
			fromVersion = oldVersions.pop()
			toVersion = newVersions.pop()
			yield fromVersion, toVersion, None
			return

		for ov in oldVersions:
			for nv in newVersions:
				affected = []
				for oldRpm, newRpm in self.updatedRpms:
					if oldRpm.version == ov and newRpm.version == nv:
						affected.append(oldRpm.name)
				if affected:
					yield ov, nv, affected

	class ChangedRequirements(object):
		def __init__(self, oldRpm, oldRequirements, newRpm, newRequirements):
			self.oldRpm = oldRpm
			self.newRpm = newRpm

			self.dropped = oldRequirements.difference(newRequirements)
			self.added = newRequirements.difference(oldRequirements)

	@property
	def requirementsChanges(self):
		def extractRequires(rpm):
			from functools import reduce

			requires = rpm.resolvedRequires
			if not requires:
				return set()

			requires = reduce(set.union, (dep.packages for dep in requires))
			requires = set(map(str, requires))

			# ignore whether the rpm requires itself or not
			requires.discard(rpm.name)

			return requires

		for (oldRpm, newRpm) in self.updatedRpms:
			oldReqs = extractRequires(oldRpm)
			newReqs = extractRequires(newRpm)
			if oldReqs != newReqs:
				yield self.ChangedRequirements(oldRpm, oldReqs, newRpm, newReqs)


class ProductHistory(object):
	def __init__(self):
		self._packages = {}

	def addPackage(self, name):
		try:
			packageHistory = self._packages[name]
		except:
			packageHistory = PackageHistory(name)
			self._packages[name] = packageHistory
		return packageHistory

	def getPackage(self, name):
		return self._packages.get(name)

	def __iter__(self):
		return iter(sorted(self._packages.values(), key = str))

class HistoryRendererBase(object):
	def begin(self):
		pass

	def finish(self):
		pass

class SimplePackageHistoryRenderer(HistoryRendererBase):
	def __init__(self):
		pass

	def display(self, packageHistory):
		infomsg(f"{packageHistory}:")

		tf = ANSITreeFormatter()
		nodeMap = {}

		for release in packageHistory:
			releaseDesc = f"{release.buildDate} {release.newBuild.obsBuild}"

			projectName = release.newBuild.obsProject.name
			projectNode = nodeMap.get(projectName)
			if projectNode is None:
				if release.oldBuild is None:
					projectNode = tf.root
					projectNode.value = projectName
				else:
					oldNode = nodeMap[release.oldBuild]
					projectNode = oldNode.add(projectName)
				nodeMap[projectName] = projectNode

			buildNode = projectNode.add(releaseDesc)
			nodeMap[release.newBuild] = buildNode

			for rpm in release.addedRpms:
				buildNode.add(f" add:  {rpm}")
			if release.droppedRpms:
				buildNode.add(f" drop: {rpm}")

			if release.updatedRpms:
				for oldVersion, newVersion, rpms in release.versionUpdates:
					if oldVersion == newVersion:
						continue

					change = f"version update {oldVersion} -> {newVersion}"
					if rpms is None:
						buildNode.add(f" {change} for all packages")
					else:
						buildNode.add(f" {change} for {', '.join(rpms)}")

				for change in release.requirementsChanges:
					if change.dropped:
						buildNode.add(f" dropped requirements: {' '.join(change.dropped)}")
					if change.added:
						buildNode.add(f" added requirements: {' '.join(change.added)}")


		for prefix, value in tf.render():
			infomsg(f"{prefix}{value}")


class SummaryPackageHistoryRenderer(HistoryRendererBase):
	def __init__(self):
		pass

	def display(self, packageHistory):
		stats = packageHistory.stats

		infomsg(f"{packageHistory}: {stats.releases:4} releases")
		if stats.versionUpdates:
			infomsg(f"    version updates: {stats.versionUpdates:3}")
		if stats.rpmsAdded:
			infomsg(f"    added rpms:      {stats.rpmsAdded:3}")
		if stats.rpmsRemoved:
			infomsg(f"    removed rpms:    {stats.rpmsRemoved:3}")
		if stats.releasesWithRequirementsChanges:
			infomsg(f"    req changes:     {stats.releasesWithRequirementsChanges:3} releases with changes in requirements")

class TablePackageHistoryRenderer(HistoryRendererBase):
	def __init__(self):
		self.sortKeyFunc = None
		self.filters = None
		self.maxRowCount = None
		self.numReleases = 0
		self.rows = []

		self.mapping = None
		self.rowsByKey = {}

	def getFieldType(self, name):
		dummy = PackageHistory.ReleaseStats('dummy')
		if name not in dir(dummy):
			raise Exception(f"{name} does not seem to be a valid sort or filter field")

		return type(getattr(dummy, name))

	def setSortField(self, name):
		keyType = self.getFieldType(name)
		if keyType in (int, float):
			# highest number first
			self.sortKeyFunc = lambda stats: -getattr(stats, name)
		else:
			# default sort order (esp for strings)
			self.sortKeyFunc = lambda stats: getattr(stats, name)

	class PassFilter:
		def __init__(self, name, value, fn):
			self.check = lambda stats: fn(getattr(stats, name), value)

	FILTER_OPS = (
		('<=',	int.__le__),
		('>=',	int.__ge__),
		('<',	int.__lt__),
		('>',	int.__gt__),
		('=',	int.__eq__),
	)

	def addFilter(self, expr):
		filter = None

		for string, op in self.FILTER_OPS:
			if string in expr:
				name, value = expr.split(string)

				# throw an exception if name is not a valid key type
				self.getFieldType(name)

				filter = self.PassFilter(name, int(value), op)
				break

		if filter is None:
			raise Exception(f"cannot parse table filter \"{expr}\"")

		if self.filters is None:
			self.filters = []
		self.filters.append(filter)

	def applyMapping(self, mapping):
		assert(not self.rows)

		self.mapping = mapping

	def begin(self):
		infomsg("")
		infomsg(f"|{'Build':40}|{'Releases':>10}|{'Updates':>10}|{'Add rpms':>10}|{'Del rpms':>10}|{'Req chgs':>10}|")

		words = [
			"-" * 40,
			"-" * 10,
			"-" * 10,
			"-" * 10,
			"-" * 10,
			"-" * 10,
		]

		self.sepa = '+' + '+'.join(words) + '+'
		infomsg(self.sepa)

	def display(self, packageHistory):
		stats = packageHistory.stats
		self.numReleases += 1

		if self.filters is not None:
			for filter in self.filters:
				if not filter.check(stats):
					return

		if self.mapping is None:
			self.rows.append(stats)
		else:
			key = self.mapping.get(packageHistory.name)

			row = self.rowsByKey.get(key)
			if row is not None:
				row.update(stats)
			else:
				stats.name = key or "(none)"
				self.rows.append(stats)
				self.rowsByKey[key] = self.rows[-1]

			self.numReleases = len(self.rows)

	def finish(self):
		if self.sortKeyFunc is not None:
			self.rows.sort(key = self.sortKeyFunc)

		if self.maxRowCount is not None:
			self.rows = self.rows[:self.maxRowCount]

		for stats in self.rows:
			infomsg(f"|{stats.name:40}|{stats.releases:10}|{stats.versionUpdates:10}|{stats.rpmsAdded:10}|{stats.rpmsRemoved:10}|{stats.releasesWithRequirementsChanges:10}|")
		infomsg(self.sepa)

		infomsg("")
		infomsg(f"Displayed {len(self.rows)}/{self.numReleases} entries")
		infomsg(f"  Releases:     total number of rebuilds")
		infomsg(f"  Updates:      number of distinct version updates (updating 1.1 -> 1.2 in several SPs counts as one)")
		infomsg(f"  Add rpms:     total number of rpms added in a rebuild")
		infomsg(f"  Del rpms:     total number of rpms removed in a rebuild")
		infomsg(f"  Req chgs:     number of changes in runtime and build dependencies")
		infomsg("")
