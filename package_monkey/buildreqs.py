##################################################################
#
# Various classes for handling build requirements.
# This used to work nicely, but I let it fall into disrepair when
# I stopped working on splitting SLFO:Main into different projects.
#
##################################################################

import fastset as fastsets
from .ordered import PartialOrder

__names__ = ['BuildRequiresMap']

##################################################################
# This defines sets of build requirements, used to create a more
# compact representation in the XML file
##################################################################
class BuildRequiresMap(object):
	domain = fastsets.Domain("profiles")

	class BuildProfile(domain.member):
		def __init__(self, name, kernel):
			super().__init__()

			self.name = name
			self.kernel = kernel
			self.nodeSet = None

		def __lt__(self, other):
			if self is other:
				return False
			if self.nodeSet is None:
				return other.nodeSet is not None
			if other.nodeSet is None:
				return False
			return self.nodeSet.issubset(other.nodeSet)

		def __str__(self):
			return self.name

		def maybeUpdate(self, buildRequires):
			if self.kernel.issubset(buildRequires):
				if self.nodeSet is None:
					self.nodeSet = buildRequires.copy()
				else:
					self.nodeSet.intersection_update(buildRequires)

	class BuildReport(object):
		def __init__(self, name, requires):
			self.name = name
			self.requires = requires
			self.profilesUsed = BuildRequiresMap.domain.set()
			self.residual = requires.copy()

		def __len__(self):
			return len(self.requires)

		def maybeUpdate(self, profile):
			if profile.nodeSet.issubset(self.requires):
				# infomsg(f"{build} -> {profile}")
				self.residual.difference_update(profile.nodeSet)
				self.profilesUsed.add(profile)

	def __init__(self):
		self._map = {}
		self._order = None
		self._buildReports = {}

	@classmethod
	def fromSolvingTree(klass, solvingTree, profileDefinitions = None):
		result = klass()
		for build in solvingTree.allBuilds:
			# skip fake builds like "environment_with_systemd"
			if build.isSynthetic:
				continue

			requires = None
			for rpm in build.sources:
				node = solvingTree.getPackageNoCreate(rpm)
				if node is None:
					raise Exception(f"build {build} requires unknown rpm {rpm}")
				if requires is None:
					requires = node.lowerNeighbors
				else:
					requires = requires.union(node.lowerNeighbors)

			if not requires:
				# warnmsg(f"Weird, build {build} has empty build requirements")
				continue

			result._buildReports[build] = klass.BuildReport(build.name, requires)

		if profileDefinitions is None:
			profileDefinitions = DefaultBuildProfileDefinitions
		result.buildDefaultProfiles(solvingTree, profileDefinitions)

		return result

	@property
	def buildReports(self):
		return self._buildReports.values()

	@property
	def profiles(self):
		return self._map.values()

	def buildDefaultProfiles(self, solvingTree, profileDefinitions):
		rpmNameToTreeNode = {}
		for rpm, node in solvingTree._packageTreeDescriptor.items():
			if not rpm.isSourcePackage:
				assert(node is not None)
				rpmNameToTreeNode[rpm.name] = node

		for profileName, rpmNames in profileDefinitions:
			kernel = self.solvingTree.createPackageNodeSet(
				filter(bool, map(rpmNameToTreeNode.get, rpmNames)))
			self.createProfile(profileName, kernel)

	def createProfile(self, name, nodeSet = None):
		if nodeSet is None:
			nodeSet = SolvingTree.createPackageNodeSet()

		profile = self.BuildProfile(name, nodeSet)
		self._map[name] = profile

		# The new profile is defined to include all SolvingTree nodes
		# that show up in /every/ build that uses profile.kernel.
		#
		# This is probably similar to the runtime dependency closure of
		# the kernel, but not always - because the OBS project will
		# (a) disambiguate dependencies differently from what we do,
		# and (b) decide to ignore certain dependencies.
		for buildReport in self.buildReports:
			profile.maybeUpdate(buildReport.requires)

		return profile

	def finalize(self):
		self._order = PartialOrder(self.domain, "build profile hierarchy")

		for profile in self.profiles:
			below = set()
			for other in self.profiles:
				if other is profile:
					continue
				infomsg(f"compare {other} to {profile}")
				if other < profile:
					below.add(other)

			self._order.add(profile, below)

		self._order.finalize()

	def describeBuildRequirements(self, build):
		buildReport = self._buildReports.get(build)
		if not buildReport:
			return self.BuildReport(build.name, [])

		for profile in self.profiles:
			buildReport.maybeUpdate(profile)

		if len(buildReport.profilesUsed) > 1:
			buildReport.profilesUsed = self._order.maxima(buildReport.profilesUsed)

		return buildReport

	def describeProfile(self, targetProfile):
		buildProfiles = self.domain.set()
		remaining = targetProfile.nodeSet.copy()
		for profile in self.profiles:
			if profile.nodeSet == targetProfile.nodeSet:
				continue
			if profile.nodeSet.issubset(targetProfile.nodeSet):
				remaining.difference_update(profile.nodeSet)
				buildProfiles.add(profile)

		if len(buildProfiles) > 1:
			buildProfiles = self._order.maxima(buildProfiles)

		return buildProfiles, remaining

	def findClosestProfile(self, profile):
		best = None
		bestDelta = 666
		for other in self.profiles:
			if other.nodeSet.issubset(profile.nodeSet):
				delta = len(profile.nodeSet.difference(other.nodeSet))
				if best is None or delta < bestDelta:
					best = other
					bestDelta = delta

		return best, bestDelta

	def guessCandidates(self, numResiduals = 30, numNodes = 20):
		freq = FrequencyCounter(lambda node: node.name)
		for buildReport in self.buildReports:
			freq.addEvent(buildReport.residual)

		candidateProfiles = []
		for node, freq in freq:
			if freq <= 2:
				continue

			# Create a new profile with just this candidate
			kernel = SolvingTree.createPackageNodeSet([node])
			profile = self.BuildProfile(node.name, kernel)

			for buildReport in self.buildReports:
				profile.maybeUpdate(buildReport.requires)

			bestApprox, delta = self.findClosestProfile(profile)
			if delta <= 1:
				continue

			reduction = 0
			for buildReport in self.buildReports:
				if not profile.nodeSet.issubset(buildReport.requires):
					continue

				delta = buildReport.residual.intersection(profile.nodeSet)
				if delta:
					if False:
						if profile.name in ('aqute-bnd', ):
							infomsg(f" {profile.name} {buildReport.name}: reduction {len(delta)}")

					reduction += len(delta) - 1

			if reduction < 10:
				continue

			profile.reduction = reduction
			candidateProfiles.append(profile)

			if len(candidateProfiles) >= numNodes and False:
				break

		candidateProfiles.sort(key = lambda p: p.reduction, reverse = True)

		infomsg(f"Candidates for buildprofiles:")
		for profile in candidateProfiles[:10]:
			bestApprox, delta = self.findClosestProfile(profile)
			infomsg(f" {profile.reduction:5}  {profile.name:40}; differs from {bestApprox} by {delta} packages")

# Hard-coded, should probably be defined in some yaml file of sorts
DefaultBuildProfileDefinitions = (
		('minimal', []),
		('common1', ('libaudit1', 'gettext-tools-mini', 'permissions-config')),
		('common2', ('chkstat', 'libaudit1', 'libcap-ng0', 'libfdisk1', 'permissions-config', 'systemd-rpm-macros',)),
		('common3', ('sysuser-shadow', )),
		('gcc', ('gcc-build-PIE', 'make', 'linux-glibc-devel', )),
		('c++', ('gcc-build-c++', 'make')),
		('rust', ('rust-bindgen', )),
		('cmake', ('cmake', )),
		('gtk3', ('typelib-1_0-Gtk-3_0', )),
		('texlive', ('texlive', )),
		('python', ('python-rpm-packaging', 'python311-setuptools', )),
		('python-pip', ('python-rpm-packaging', 'python311-pip', )),
		('python:requests', ('python311-requests', )),
		('python:pytest', ('python311-pytest', )),
		('cython', ('python311-Cython', )),
		('systemd', ('systemd-mini-devel', )),
		('bison', ('bison', 'flex', )),
		('meson', ('meson', 'ninja', )),
		('java', ('javapackages-local', )),
		('javacc', ('javacc', )),
		('libcups', ('libcups2', )),
		('libcurl', ('libcurl4', )),
		('libxcb', ('libxcb-devel', )),
		('libxml2', ('libxml2-devel', )),
		('libjpeg8', ('libjpeg8-devel', )),
		('selinux', ('libselinux-devel', )),
		('dbus-1-glib', ('dbus-1-glib-devel', )),
		('pango', ('pango-devel', )),
		('autoconf', ('autoconf', )),
		('automake', ('automake', 'libtool', )),
		('ImageMagick', ('ImageMagick', )),
		('itstool', ('itstool', )),
		('gpg2', ('gpg2', )),
		('libzypp', ('libzypp', )),
		('yast2', ('yast2', )),
		('ruby-rspec', ('ruby3.2-rubygem-rspec', )),
		('libstorage-ng1', ('libstorage-ng1', )),
		('gstreamer', ('typelib-1_0-Gst-1_0', )),
		('atk', ('typelib-1_0-Atk-1_0', )),

		('vulkan-devel', ('vulkan-devel', )),
		('qt6:gui', ('qt6-gui-devel', )),
		('qt5:gui', ('libQt5Gui-devel', )),
		('gtk3', ('gtk3-devel', )),
		('glib2', ('glib2-devel', )),
		('gstwayland', ('libgstwayland-1_0-0', )),
		('maven', ('maven-lib', )),
		('gstreamer:base', ('gstreamer-plugins-base', )),
	)

