#
# Track evolution of distro, such as the progression of a package's version,
# renaming/folding of RPMs etc
#

import re
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

genealogyLogger = loggingFacade.getLogger('genealogy')

def debugGenealogy(msg, *args, prefix = None, **kwargs):
	if prefix:              
		msg = f"[{prefix}] {msg}"               
	genealogyLogger.debug(msg, *args, **kwargs)

class Name(object):
	def __init__(self, stem, version, suffix, arch):
		self.stem = stem
		self.version = version
		self.suffix = suffix
		self.arch = arch

	def __str__(self):
		return f"{self.stem}{self.version}{self.suffix}.{self.arch}"

	def __repr__(self):
		return f"'{self.stem}' '{self.version}' '{self.suffix}' '{self.arch}'"

	@property
	def baseName(self):
		return f"{self.stem}{self.suffix}"

	@property
	def packageName(self):
		return f"{self.stem}{self.version}{self.suffix}"

	COMMON_SUFFIXES = (
		'-32bit',
		'-devel',
		'-static',
		'-macros',
		'-doc',
		'-testsuite',
		'-hmac',
		'-cavs',
		# icu
		'-ledata',
		'-bedata',
		# HPC
		'-gnu-hpc',
		'-gnu-openmpi-hpc',
		'-gnu-openmpi2-hpc',
		'-gnu-openmpi3-hpc',
		'-gnu-openmpi4-hpc',
		'-gnu-mpich-hpc',
		'-gnu-mvapich2-hpc',
		'-mvapich2',
		'-module',
	)
	COMMON_ARCHS = (
		'x86_64',
		'i686',
		'i586',
		'ppc64',
		'ppc64le',
		'aarch64',
		's390x',
		'riscv5',
		'noarch',
		'src',
		'nosrc',
	)

	@classmethod
	def parse(klass, name, packageName = None):
		# print(f"Name.parse({name})")

		if packageName is not None:
			packageName = klass.transformBuildName(packageName)

		name, arch = klass.detectArch(name)
		if arch is None:
			raise Exception(f"Bad package name {name}: no architecture")

		suffix = ""
		while name and not name[-1].isdigit():
			s = klass.detectSuffix(name)
			if s is None:
				break

			name = name[:-len(s)]
			suffix = s + suffix

		if not name:
			return Name(None, None, None, None)

		if not name[-1].isdigit():
			return Name(name, '', suffix, arch)

		stem0, name = klass.detectNameStem(name, packageName)

		if not stem0 and name.startswith("lib"):
			if packageName:
				stem0, name = klass.detectNameStem(name, "lib" + packageName)
			name, version = klass.processLibraryName(name)
		else:
			name, version = klass.processRegularName(name)

		return Name(stem0 + name, version, suffix, arch)

	@classmethod
	def transformBuildName(klass, buildName):
		# We use the buildName in parsing the name. If the build is called libpng16 (or libpng16:bla)
		# then we will never consider "16" as part of the version string, but always part of
		# the package name stem
		if ':' in buildName:
			buildName, dummy = buildName.rsplit(':', maxsplit = 1)

		# special handling for Egbert
		if buildName == 'openmpi2':
			buildName = 'openmpi_2'
		elif buildName == 'openmpi3':
			buildName = 'openmpi_3'
		elif buildName == 'openmpi4':
			buildName = 'openmpi_4'

		return buildName


	@classmethod
	def detectNameStem(self, name, potentialStem):
		# if the OBS package itself is called libpng16, we
		# do should not split the name into "libpng" and "16"
		if potentialStem and name.startswith(potentialStem):
			return potentialStem, name[len(potentialStem):]

		return '', name

	@classmethod
	def processRegularName(klass, name):
		chars = list(name)
		version = ''
		tentativeVersion = ''
		while chars:
			cc = chars.pop()

			# print(f"{''.join(chars)} | {cc} | {tentativeVersion} | {version}")
			if cc.isdigit() or cc == '.':
				tentativeVersion = cc + tentativeVersion
			elif cc == '-' or cc == '_':
				version = cc + tentativeVersion + version
				tentativeVersion = ''
			else:
				chars = chars + [cc] + list(tentativeVersion)
				break

		if not chars:
			# The OBS package name was something like libraw, and the
			# RPM was called libraw20. In this case, we enter whis
			# function with stem0="libraw" and name="20"
			version = tentativeVersion + version
			tentativeVersion = ''

		name = ''.join(chars) + tentativeVersion
		return name, version

	@classmethod
	def processLibraryName(klass, name):
		chars = list(name)
		version = ''
		while chars:
			cc = chars.pop()

			if cc.isdigit() or cc in ('.', '_', '-'):
				version = cc + version
			else:
				chars.append(cc)
				break

		name = ''.join(chars)
		return name, version

	@classmethod
	def detectArch(klass, name):
		if '.' not in name:
			raise Exception(f"Bad package name {name}: missing arch suffix")

		name, arch = name.rsplit('.', maxsplit = 1)
		if arch not in klass.COMMON_ARCHS:
			raise Exception(f"Bad package name {name}: invalid arch {arch}")

		return name, arch

	@classmethod
	def detectSuffix(klass, name):
		for s in klass.COMMON_SUFFIXES:
			if name.endswith(s):
				return s

	@classmethod
	def isValidVersion(klass, value):
		return value.strip("0123456789._") == ""

class GenerationAnalyzer(object):
	class Bucket(object):
		def __init__(self, name):
			self.name = name
			self._mother = None
			self._daughter = None
			self._conflicts = []

		@property
		def mother(self):
			if self._mother is None:
				return None
			return self._mother

		@mother.setter
		def mother(self, value):
			self.updateName('_mother', value)

		@property
		def daughter(self):
			if self._daughter is None:
				return None
			return self._daughter

		@daughter.setter
		def daughter(self, value):
			self.updateName('_daughter', value)

		def updateName(self, attrName, name):
			assert(isinstance(name, Name))

			currentName = getattr(self, attrName)
			if currentName is not None:
				# In the HPC world, there's a bunch of python packages
				# where there's a version specific rpm (python-numpy_1_2_3_4-gnu-hpc)
				# and a generic python-numpy-gnu-hpc that pulls in the current version
				if not currentName.version:
					debugGenealogy(f"# favoring {name} over generic {currentName}")
				elif not name.version:
					debugGenealogy(f"# favoring {currentName} over generic {name}")
					return
				else:
					debugGenealogy(f"package name conflict: {currentName} vs {name}")
					self._conflicts.append(name)
					return
			setattr(self, attrName, name)

	def __init__(self, buildName):
		self.packageName = buildName

		self._buckets = {}

	def __iter__(self):
		for b in self._buckets.values():
			# suppress any entries with a conflic
			if b._conflicts:
				continue

			yield b.mother, b.daughter

	def addMother(self, name):
		if not isinstance(name, Name):
			name = self.parseName(name, "mother")
		b = self.createBucket(name)
		b.mother = name

	def addDaughter(self, name):
		if not isinstance(name, Name):
			name = self.parseName(name, "daughter")
		b = self.createBucket(name)
		b.daughter = name

	def parseName(self, name, role):
		parsedName = Name.parse(name, packageName = self.packageName)
		if parsedName is None:
			return

		# print(f"{role}: {name} => {repr(parsedName)}")
		return parsedName

	def createBucket(self, parsedName):
		n = parsedName.baseName
		# avoid conflict between libfoo.x86_64 and libfoo.src
		if parsedName.arch in ('src', 'nosrc'):
			n += ".src"

		result = self._buckets.get(n)
		if result is None:
			result = self.Bucket(n)
			self._buckets[n] = result
		return result

class Genealogy(object):
	EVENT_INTRODUCED	= 0
	EVENT_CHANGE		= 1
	EVENT_DROPPED		= 2

	CHANGE_OF_VERSION	= 10
	CHANGE_OF_ARCH		= 11
	CHANGE_OF_BUILD		= 12

	class Package(object):
		def __init__(self, name):
			self.name = name
			self.events = []

	class Event(object):
		def __init__(self, type, hop, rpmName):
			self.type = type
			self.hop = hop
			self.rpmName = rpmName
			self.changes = []

		def maybeAddDetail(self, type, oldValue, newValue):
			if oldValue != newValue:
				self.changes.append(Genealogy.Change(type, oldValue, newValue))

	class Change(object):
		def __init__(self, type, oldValue, newValue):
			self.type = type
			self.oldValue = oldValue
			self.newValue = newValue

	def __init__(self):
		self._buckets = {}

	@classmethod
	def loadFromEvolutionLog(klass, path):
		log = PackageEvolutionLog.read(path)

		result = Genealogy()
		for hop in log.hops:
			for mother, daughter in hop.generations:
				result.addEvent(hop, mother, daughter)

		return result

	def __iter__(self):
		for name, pkg in sorted(self._buckets.items()):
			yield pkg

	class Descendant(object):
		def __init__(self, name, arch):
			self.name = name
			self.arch = arch

		@property
		def valid(self):
			return bool(self.name)

		def __str__(self):
			return f"{self.name}.{self.arch}"

	def getLatestDescendant(self, name, buildName):
		lineage = self.lookupBucket(name, buildName)

		if lineage is None and "openmpi_" in name and buildName is None:
			# HPC hack. The package name is eg openmpi4, but the rpm
			# name is libopenmpi_4_x_y-blafasel-gnu-hpc
			i = name.index("openmpi_")
			majorVersion = name[i + 8]
			if majorVersion.isdigit():
				buildName = "openmpi" + majorVersion
				lineage = self.lookupBucket(name, buildName)

		# Check for packages that have a buildName like "libgit2" or "libpng16"
		if lineage is None and buildName is None:
			m = re.match("[a-z]*[0-9]+", name)
			if m is None:
				m = re.match("lib([a-z]*[0-9]+)", name)
			if m is not None:
				lineage = self.lookupBucket(name, m[0])

		if lineage is None:
			return None

		lastEvent = lineage.events[-1]
		if lastEvent.type == self.EVENT_DROPPED:
			return self.Descendant(None, None)

		if lastEvent.type == self.EVENT_CHANGE or \
		   lastEvent.type == self.EVENT_INTRODUCED:
			return self.Descendant(lastEvent.rpmName.packageName, lastEvent.rpmName.arch)

		errormsg(f"Freak event in genealogy for {name}")
		return None

	def addEvent(self, hop, mother, daughter):
		baseName = None

		if daughter is None:
			type = self.EVENT_DROPPED
			baseName = mother.baseName
		elif mother is None:
			type = self.EVENT_INTRODUCED
			baseName = daughter.baseName
		else:
			type = self.EVENT_CHANGE
			baseName = daughter.baseName

		assert(baseName)
		b = self.createBucket(baseName)

		lastEvent = None
		if b.events:
			lastEvent = b.events[-1]

		event = self.Event(type, hop, daughter or mother)
		if type == self.EVENT_CHANGE:
			if lastEvent and lastEvent.type == self.EVENT_DROPPED:
				# cancel the drop event
				b.events.pop()

			event.maybeAddDetail(self.CHANGE_OF_VERSION, mother.version, daughter.version)
			event.maybeAddDetail(self.CHANGE_OF_ARCH, mother.arch, daughter.arch)

			if lastEvent:
				previousBuild = lastEvent.hop.buildName
				event.maybeAddDetail(self.CHANGE_OF_BUILD, previousBuild, hop.buildName)

		if type == self.EVENT_DROPPED:
			if lastEvent and lastEvent.type == self.EVENT_DROPPED:
				debugGenealogy(f"{hop}: duplicate drop event for {baseName} - previously dropped in {lastEvent.hop}")
				return

		b.events.append(event)

	def createBucket(self, name):
		if name.startswith('python311-'):
			name = 'python3-' + name[10:]

		b = self._buckets.get(name)
		if b is None:
			b = self.Package(name)
			self._buckets[name] = b
		return b

	def lookupBucket(self, name, buildName):
		assert(type(name) is str)

		parsedName = Name.parse(name, packageName = buildName)
		return self._buckets.get(parsedName.baseName)

class EvolutionHop(object):
	def __init__(self, buildName, id):
		self.buildName = buildName
		self.id = id
		self.added = []
		self.removed = []

	def __str__(self):
		return self.id

	@property
	def generations(self):
		ga = GenerationAnalyzer(self.buildName)
		for name in self.removed:
			ga.addMother(name)
		for name in self.added:
			ga.addDaughter(name)

		return iter(ga)

class PackageEvolutionLog(object):
	def __init__(self):
		self.hops = []

	def add(self, buildName, id):
		hop = EvolutionHop(buildName, id)
		self.hops.append(hop)
		return hop

	def write(self, path):
		with open(path, "w") as f:
			out = lambda m: print(m, file = f)
			if not self.hops:
				out("# No evolution for this distribution (yet)")
				return

			for hop in self.hops:
				out(f"{hop.buildName} {hop.id}")
				for name in hop.removed:
					out(f" - {name}")
				for name in hop.added:
					out(f" + {name}")

	class Reader(object):
		def __init__(self, log, path):
			self.log = log
			self.path = path
			self.currentHop = None
			self.fp = open(path)

		def process(self):
			lineno = 0
			for line in self.fp.readlines():
				lineno += 1
				if not self.processLine(line):
					raise Exception(f"{self.path} line {lineno}: unable to parse line {line.rstrip()}")

		def processLine(self, line):
			if line[0] == '#':
				return True

			if line[0] == ' ':
				if self.currentHop is None:
					errormsg(f"garbage before first build")
					return False

				ind, name = line.split()
				if ind == '-':
					self.currentHop.removed.append(name)
				elif ind == '+':
					self.currentHop.added.append(name)
				else:
					return False
				return True

			build, id = line.split()
			self.currentHop = self.log.add(build, id)
			return True

	@classmethod
	def read(klass, path):
		log = klass()
		klass.Reader(log, path).process()
		return log

if __name__ == '__main__':
	loggingFacade.setLogLevel('genealogy', 'debug')
	loggingFacade.enableStdout()

	log = PackageEvolutionLog()

	hop = log.add("bash", "SUSE:SLE-15:GA/bash")
	hop.added.append("bash.x86_64")
	hop.removed.append("sh.x86_64")

	hop = log.add("vala", "SUSE:SLE-15-SP2:GA/vala")
	hop.removed = ["libvala-0_38-0.x86_64", "libvaladoc-0_38-0.x86_64", "libvala-0_38-devel.x86_64"]
	hop.added = ["libvaladoc-0_46-devel.x86_64", "libvala-0_46-devel.x86_64", "libvala-0_46-0.x86_64", "libvaladoc-0_46-0.x86_64"]

	log.write("evolution-test.log")

	log = PackageEvolutionLog.read("sle15/evolution.log")
	# log = PackageEvolutionLog.read("test/evolution.log")

	genealogy = Genealogy()
	for hop in log.hops:
		for mother, daughter in hop.generations:
			# print(f" {hop.buildName}: {mother} -> {daughter}")
			genealogy.addEvent(hop, mother, daughter)

	print("Genealogy:")
	for lineage in genealogy:
		print(f"  {lineage.name}:")

		for event in lineage.events:
			if event.type == Genealogy.EVENT_INTRODUCED:
				description = f"introduced as {event.rpmName}"
			elif event.type == Genealogy.EVENT_DROPPED:
				description = f"dropped {event.rpmName}"
			elif event.type == Genealogy.EVENT_CHANGE:
				details = []

				for c in event.changes:
					detail = "unknown-attribute"
					if c.type == genealogy.CHANGE_OF_VERSION:
						detail = "version"
					elif c.type == genealogy.CHANGE_OF_ARCH:
						detail = "arch"
					elif c.type == genealogy.CHANGE_OF_BUILD:
						detail = "build"

					details.append(f"change {detail} from {c.oldValue} to {c.newValue}")

				if not details:
					details.append(f"unclear change")

				description = f"{event.rpmName}: {'; '.join(details)}"
			else:
				fail

			print(f"      {event.hop} {description}")

	testNames = [
		["libopenmpi_4_0_5-gnu-hpc.x86_64", "openmpi4:gnu-hpc"],
		["libraw20.x86_64", None],
		["libpoppler73.x86_64", None],
		["libpoppler126.x86_64", None],
		["AppStream-doc.x86_64", None],
		["libdav1d5.x86_64", None],
		["libfido2-1_0_0.x86_64", None],
		# this one actually evolved into libecpg6
		["libecpg.x86_64", None],
		["mvapich2_2_3_6-gnu-hpc-macros-devel.x86_64", None],
		["libglslang-suse9.x86_64", None],
		["libhdf5hl_fortran_1_10_7-gnu-openmpi2-hpc.x86_64", None],
		["libhdf5_1_10_7-gnu-openmpi2-hpc.x86_64", "hdf5"],
		["libhdf5_1_10_7-gnu-openmpi2-hpc.x86_64", None],
		["libgrpc8.x86_64", None],
		["libpurple0.x86_64", None],
	]

	print(f"Testing genealogy.getLatestDescendant()")
	for args in testNames:
		latest = genealogy.getLatestDescendant(*args)

		if latest is None:
			found = None
		elif not latest.valid:
			found = "dropped"
		else:
			found = f"{latest.name}.{latest.arch}"
		print(f" {args[0]} -> {found}")
