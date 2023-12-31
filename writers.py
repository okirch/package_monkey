#
# writing out the classification results
#

from csvio import CSVWriter
from xmltree import XMLTree
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

class BaseWriter:
	def __init__(self):
		self._excluded = set()
		self._writeEmptyGroups = False

	def excludeLabel(self, labelName):
		self._excluded.add(labelName)

	def write(self, result):
		for label, members in result.enumeratePackages():
			if label.name in self._excluded:
				continue

			if not members and not self._writeEmptyGroups:
				continue

			runtimeRequires = result.getMinimalRuntimeRequirements(label)

			self.writeLabelDescription(label, runtimeRequires)
			self.writePackagesForGroup(label, members)

		for label, buildInfo in result.enumerateBuilds():
			if label is not None and label.name in self._excluded:
				continue

			self.writeBuild(label, buildInfo)

		self.flush()

	def flush(self):
		pass

class StandardWriter(BaseWriter):
	class IndentingWriter:
		def __init__(self, indent = ""):
			self.indent = ""

		def print(self, msg = None, endl = None):
			if msg is None:
				print()
				return

			print(f"{self.indent}{msg}")

		def indentingWriter(self):
			return StandardWriter.IndentingWriter(self.indent + "   ")

	def writeLabelDescription(self, label, runtimeRequires):
		print()
		print(f"== {label.type} group {label} ==")

	def writePackagesForGroup(self, label, packages):
		for pkg in sorted(packages, key = lambda p: p.name):
			print(f"  {pkg}")

	def writeBuild(self, label, buildInfo):
		pass

	def indentingWriter(self):
		return StandardWriter.IndentingWriter()

	# Problem reporting
	def showProof(self, reason, indent = "  "):
		for why in reason.chain():
			print(f"{indent}{why}")
			indent += "  "

	def addProblemCategory(self, name):
		print()
		return self

	def addUnexpectedRuntimeDependency(self, problem):
		print(f"Unexpected dependency {problem.desc}:")

		for (wantedReason, badPackage) in problem.conflicts:
			origin = wantedReason.originPackage
			print(f" * {origin} -> {badPackage}")
			self.showProof(wantedReason, indent = "      ")
			print(f"      Package {badPackage} was labeled as {badPackage.label} due to:")
			self.showProof(badPackage.labelReason, indent = "         ")

	def addUnexpectedBuildDependency(self, problem):
		print(f"Unexpected build dependency {problem.desc}:")

		for (buildName, wantedReason, badPackage) in problem.conflicts:
			origin = wantedReason.originPackage
			print(f" * {origin} -> {badPackage}")
			self.showProof(wantedReason, indent = "      ")
			print(f"   building {buildName} required {badPackage}")
			print(f"      Package {badPackage} was labeled as {badPackage.label} due to:")
			self.showProof(badPackage.labelReason, indent = "         ")

	def addUnlabelledBuildDependency(self, problem):
		print(f"Unlabelled build dependencies of {problem.desc}:")

		for build in sorted(problem.builds.values(), key = lambda b: b.name):
			origin = build.reason.originPackage
			print(f" * {origin} -> {build.name}")
			self.showProof(build.reason, indent = "      ")

			print(f"   building {build.name} required the following package(s) which have not been labelled yet")
			for badPackage in build.packages:
				print(f"      {badPackage}")

	def addSourceProjectConflict(self, problem):
		map = {}
		for rpm in problem.build.binaries:
			if not rpm.isSourcePackage:
				label = rpm.label
				if label is None:
					key = "unclassified"
				elif not label.sourceProject:
					key = f"{label} (no source project)"
				else:
					key = label.sourceProject.name

				dest = map.get(key)
				if dest is None:
					map[key] = []
					dest = map[key]
				dest.append(rpm)

		print(f"Conflicting source projects for OBS package {problem.desc}")
		for key, packages in sorted(map.items()):
			names = (rpm.shortname for rpm in packages)
			print(f" * {key}: {', '.join(names)}")
			# FIXME: if we want to be verbose, we could display the label reason for each rpm

	def addUnresolvedDependency(self, problem):
		print(f"Unlabelled build dependencies of {problem.desc}:")

		writer.print(f"Unresolved dependency {self.desc}, required by:")
		for pkg in self.requiredby:
			print(f" * {pkg}")

	def addMissingSource(self, problem):
		print(f"Unlabelled build dependencies of {problem.desc}:")

		writer.print(f"Missing source package {self.desc}, required by:")
		for (pkg, reason) in self.requiredby:
			print(f" * {pkg}")
			self.showProof(reason, indent = "      ")


	def writeProblems(self, problemLog):
		print()
		print("==================================================================")
		print("The resolver flagged the following problems")
		problemLog.show(self)

class TableWriter(BaseWriter):
	def __init__(self, filename = None):
		super().__init__()
		self.csv = CSVWriter(filename, fields = ['component', 'topic', 'package'])

		infomsg(f"Writing results to {filename}")

	def writeLabelDescription(self, label, runtimeRequires):
		pass

	def writePackagesForGroup(self, label, packages):
		component = label.componentName
		topic = label.name
		for pkg in sorted(packages, key = lambda p: p.name):
			self.csv.write([component, topic, pkg.shortname])

	def writeBuild(self, label, buildInfo):
		pass

	def writeProblems(self, problemLog):
		raise Exception("CSV writer does not support problem log")

class XmlWriter(BaseWriter):
	def __init__(self, filename = None):
		super().__init__()
		self.filename = filename
		self.xmltree = XMLTree('components')

		self._labels = {}

		infomsg(f"Writing results to {filename}")

	def flush(self):
		self.xmltree.write(self.filename)

	def writeLabelDescription(self, label, runtimeRequires):
		labelNode = self.xmltree.root.addChild('topic')

		# for now; could also have separate attrs for base name, option, pkgclass
		labelNode.setAttribute('name', label.name)

		if label.disposition != 'separate':
			labelNode.setAttribute('disposition', label.disposition)

		componentName = label.componentName
		if componentName is not None:
			labelNode.setAttribute('component', componentName)

		if label.description:
			labelNode.addField('description', label.description.strip())

		# This may be None if we were unable to compute a minimal set of requirements
		# (which usually happens if we have labelled a package but couldn't place
		# all of its requirements due to a conflict).
		if runtimeRequires is None:
			runtimeRequires = label.runtimeRequires

		runtimeNode = labelNode.addChild('runtime')
		for req in sorted(runtimeRequires, key = lambda l: l.name):
			reqNode = runtimeNode.addChild('requires')
			reqNode.setAttribute('topic', req.name)

		self._labels[label.name] = labelNode

	def writePackagesForGroup(self, label, packages):
		labelNode = self._labels[label.name]

		for rpm in sorted(packages, key = lambda p: p.name):
			rpmNode = self.writeRPM(labelNode, rpm)

			# FIXME: would be good if we could add the OBS package name here

	def writeBuild(self, label, buildInfo):
		buildNode = self.xmltree.root.addChild('build')
		buildNode.setAttribute('name', buildInfo.name)

		# ignore label for now

		for rpm in buildInfo.sources + buildInfo.binaries:
			self.writeRPM(buildNode, rpm)

		if buildInfo.buildRequires:
			bdepNode = buildNode.addChild('buildrequires')
			for rpm in buildInfo.buildRequires:
				self.writeRPM(bdepNode, rpm)

	def writeRPM(self, parent, rpm):
		rpmNode = parent.addChild('rpm')
		rpmNode.setAttribute('name', rpm.name)
		rpmNode.setAttribute('arch', rpm.arch)
		return rpmNode

	def writeProblems(self, problemLog):
		raise Exception("XML writer does not support problem log")

class XmlReader:
	from filter import Classification

	from packages import Versiontools

	nullVersion = Versiontools.ParsedVersion(None, None)

	def __init__(self, classificationScheme):
		from filter import ClassificationResult

		self.classificationScheme = classificationScheme
		self.result = ClassificationResult(classificationScheme.defaultOrder())

		self._packages = {}

	def read(self, path):
		import xml.etree.ElementTree as ET
		from xmltree import parse as XmlLoad

		tree = ET.parse(path)
		root = tree.getroot()
		if root.tag != "components":
			raise Exception(f"invalid root element <{root.tag}> in {path}")

		for node in root:
			if node.tag == 'topic':
				self.processTopic(node)
			elif node.tag == 'build':
				self.processBuild(node)
			else:
				raise Exception(f"unsupported element <{node.tag}> in {path}")

		return self.result

	def processTopic(self, labelNode):
		name = labelNode.attrib['name']

		label = self.classificationScheme.getLabel(name)

		for rpm in self.processAllRpmChildren(labelNode):
			self.result.labelOnePackage(rpm, label, None)

	def processBuild(self, buildNode):
		name = buildNode.attrib['name']

		binaries = []
		sources = []
		for rpm in self.processAllRpmChildren(buildNode):
			if rpm.isSourcePackage:
				sources.append(rpm)
			else:
				binaries.append(rpm)
			rpm.resolvedRequires = []

		buildReqNode = buildNode.find('buildrequires')
		if buildReqNode is not None and sources:
			for rpm in self.processAllRpmChildren(buildReqNode):
				sources[0].resolvedRequires.append((None, rpm))

		self.result.labelOneBuild(name, None, binaries, sources)

	def processAllRpmChildren(self, node):
		for rpmNode in node.findall('rpm'):
			yield self.processRpm(rpmNode)

	def processRpm(self, rpmNode):
		from packages import PackageInfo

		name = rpmNode.attrib['name']
		arch = rpmNode.attrib['arch']

		key = f"{name}.{arch}"
		rpm = self._packages.get(key)
		if rpm is None:
			rpm = PackageInfo.fromNameAndParsedVersion(name, arch, self.nullVersion)
			self._packages[key] = rpm
		return rpm
