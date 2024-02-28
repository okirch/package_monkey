#
# writing out the classification results
#

from csvio import CSVWriter
from xmltree import XMLTree
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from filter import Classification, ClassificationResult

class BaseWriter:
	def __init__(self):
		self._excluded = set()
		self._writeEmptyGroups = False

	def excludeLabel(self, labelName):
		self._excluded.add(labelName)

	def write(self, result):
		# FIXME:
		# - write the component hierarchy and its base labels
		# - write build configs for each component

		for label, requires in result.enumerateComponents():
			self.writeComponent(label, requires)

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

		for pkg, candidates in result.enumerateUnclassifiedPackages():
			self.writeUnclassified(pkg, candidates)

		self.flush()

	def flush(self):
		pass

	def writeComponent(self, label, requires):
		pass

	def writeUnclassified(self, pkg, candidates):
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
		self.unclassified = None

		self._labels = {}

		infomsg(f"Writing results to {filename}")

	def flush(self):
		self.xmltree.write(self.filename)

	def writeLabelDescription(self, label, runtimeRequires):
		labelNode = self.xmltree.root.addChild('topic')

		# for now; could also have separate attrs for base name, option, pkgclass
		labelNode.setAttribute('label', label.name)

		if label.disposition != Classification.DISPOSITION_SEPARATE:
			labelNode.setAttribute('disposition', label.disposition)

		if label.numImports:
			labelNode.setAttribute('api', True)

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
			reqNode.setAttribute('label', req.name)
			if req.sourceProject is not None:
				reqNode.setAttribute('component', req.sourceProject.name)

		self._labels[label.name] = labelNode

	def writePackagesForGroup(self, label, packages):
		labelNode = self._labels[label.name]

		for rpm in sorted(packages, key = lambda p: p.name):
			rpmNode = self.writeRPM(labelNode, rpm)

	def writeBuild(self, label, buildInfo):
		buildNode = self.xmltree.root.addChild('build')
		buildNode.setAttribute('name', buildInfo.name)

		if label is not None:
			buildNode.setAttribute('component', label)

		buildConfig = buildInfo.buildConfig
		if buildConfig is not None:
			buildNode.addField('buildconfig', buildConfig.name)

		for rpm in buildInfo.sources:
			self.writeRPM(buildNode, rpm)

		for rpm in buildInfo.binaries:
			rpmNode = self.writeRPM(buildNode, rpm)
			self.writeRequirements(rpmNode, rpm, label)

		if buildInfo.buildRequires:
			bdepNode = buildNode.addChild('buildrequires')
			for rpm in buildInfo.buildRequires:
				self.writeRPM(bdepNode, rpm)

	def writeComponent(self, label, requires):
		compNode = self.xmltree.root.addChild('component')
		compNode.setAttribute('name', str(label))

		if requires:
			reqNode = compNode.addChild('requires')
			for req in requires:
				reqNode.addChild('label').setAttribute('name', str(req))

		imports = label.imports
		if imports:
			reqNode = compNode.addChild('imports')
			for req in imports:
				reqNode.addChild('label').setAttribute('name', str(req))

		exports = label.exports
		if exports:
			reqNode = compNode.addChild('exports')
			for req in exports:
				reqNode.addChild('label').setAttribute('name', str(req))

		# bclistNode = compNode.addChild('buildconfig')

	def writeRPM(self, parent, rpm):
		rpmNode = parent.addChild('rpm')
		rpmNode.setAttribute('name', rpm.name)
		rpmNode.setAttribute('arch', rpm.arch)
		return rpmNode

	def writeRequirements(self, rpmNode, rpm, componentLabel):
		references = []
		for dep, required in rpm.resolvedRequires:
			if required.label is None:
				references.append(required)
				continue

			if required.label.sourceProject is componentLabel:
				continue

			references.append(required)

		def sourceProjectName(rpm):
			if rpm.label and rpm.label.sourceProject:
				return rpm.label.sourceProject.name
			return "-"

		references = sorted(references, key = sourceProjectName)
		if not references:
			return

		reqNode = rpmNode.addChild('requires')
		for required in references:
			childRpmNode = self.writeRPM(reqNode, required)
			if required.label:
				childRpmNode.setAttribute('label', required.label.name)
				childRpmNode.setAttribute('component', required.label.componentName)

	def writeUnclassified(self, pkg, candidates):
		if self.unclassified is None:
			self.unclassified = self.xmltree.root.addChild('unclassified')

		rpmNode = self.writeRPM(self.unclassified, pkg)
		if candidates is None:
			# rpmNode.addField('candidate', 'anywhere')
			rpmNode.addChild('anywhere')
		else:
			candNode = rpmNode.addChild('candidates')
			componentNames = set(map(lambda label: label.componentName, candidates))
			componentNames = sorted(filter(bool, componentNames))
			for label in componentNames:
				if label is None:
					errormsg(f"bad candidate label {label} for package {pkg}")
					continue
				candNode.addField('n', label)

	def writeProblems(self, problemLog):
		raise Exception("XML writer does not support problem log")

class XmlReader:
	from packages import Versiontools

	nullVersion = Versiontools.ParsedVersion(None, None)

	def __init__(self, classificationScheme):
		self.classificationScheme = classificationScheme

		self._packages = {}
		self._builds = []
		self._unclassified = {}

	def read(self, path):
		import xml.etree.ElementTree as ET
		from xmltree import parse as XmlLoad

		tree = ET.parse(path)
		root = tree.getroot()
		if root.tag != "components":
			raise Exception(f"invalid root element <{root.tag}> in {path}")

		for node in root:
			if node.tag == 'topic' or node.tag == 'label':
				self.processTopic(node)
			elif node.tag == 'build':
				self.processBuild(node)
			elif node.tag == 'component':
				self.processComponent(node)
			elif node.tag == 'unclassified':
				self.processUnclassified(node)
			else:
				raise Exception(f"unsupported element <{node.tag}> in {path}")

		result = ClassificationResult(self.classificationScheme.defaultOrder(), self.classificationScheme.componentOrder())
		for rpm in self._packages.values():
			result.labelOnePackage(rpm, rpm.label, None)
		for name, component, binaries, sources in self._builds:
			result.labelOneBuild(name, component, binaries, sources)
		for rpm, candidates in self._unclassified.items():
			result.addUnclassified(rpm, candidates)

		return result

	def processTopic(self, labelNode):
		name = labelNode.attrib['label']

		label = self.validateLabel(name, Classification.TYPE_BINARY)
		self.validateComponent(label, labelNode.attrib.get('component'))

		runtime = labelNode.find('runtime')
		if runtime is not None:
			for reqNode in runtime.findall('requires'):
				reqLabel = self.processRequires(reqNode)
				label.addRuntimeDependency(reqLabel)

		for rpm in self.processAllRpmChildren(labelNode):
			rpm.setLabel(label, "baseline classification")

	def processRequires(self, reqNode):
		labelName = reqNode.attrib.get('label') or reqNode.attrib.get('name')
		assert(labelName)
		reqLabel = self.validateLabel(labelName, Classification.TYPE_BINARY)
		componentName = reqNode.attrib.get('component')
		if componentName is not None:
			self.validateComponent(reqLabel, componentName)
		return reqLabel

	def processComponent(self, node):
		name = node.attrib['name']
		component = self.validateLabel(name, Classification.TYPE_SOURCE)

		requiresNode = node.find('requires')
		if requiresNode is not None:
			for otherNode in requiresNode:
				otherComponent = self.validateLabel(otherNode.attrib['name'], Classification.TYPE_SOURCE)
				component.addRuntimeDependency(otherComponent)

		importsNode = node.find('imports')
		if importsNode is not None:
			for apiNode in importsNode:
				api = self.validateLabel(apiNode.attrib['name'], Classification.TYPE_BINARY)
				component.addImport(api)

		exportsNode = node.find('exports')
		if exportsNode is not None:
			for apiNode in exportsNode:
				api = self.validateLabel(apiNode.attrib['name'], Classification.TYPE_BINARY)
				component.addExport(api)

		return component

	def processUnclassified(self, node):
		for rpmNode in node:
			rpm = self.processRpm(rpmNode)

			if rpmNode.find('anywhere') is not None:
				candidates = None
			else:
				candidates = set()

				listNode = rpmNode.find('candidates')
				for candNode in listNode:
					assert(candNode.tag == 'n')
					componentName = candNode.text.strip()
					component = self.validateLabel(componentName, Classification.TYPE_SOURCE)
					candidates.add(component)

			self._unclassified[rpm] = candidates

	def validateComponent(self, label, componentName):
		if componentName is None:
			return None

		component = self.validateLabel(componentName, Classification.TYPE_SOURCE)
		if label.sourceProject is None:
			label.sourceProject = component
		elif label.sourceProject != component:
			raise Exception(f"incompatible component {component} for topic {label} (already set to {label.sourceProject})")
		return component

	def validateLabel(self, name, type):
		label = self.classificationScheme.getLabel(name)
		if label is not None:
			if label.type is not type:
				raise Exception(f"incompatible {label.type} label {name} - expected {type}")
		elif self.classificationScheme.isFinal:
			raise Exception(f"unknown label {name}")
		else:
			label = self.classificationScheme.resolveLabel(name, type)
		return label

	def processBuild(self, buildNode):
		name = buildNode.attrib['name']

		component = None
		componentName = buildNode.attrib.get('component')
		if componentName is not None:
			component = self.validateLabel(componentName, Classification.TYPE_SOURCE)

		binaries = []
		sources = []
		for rpm in self.processAllRpmChildren(buildNode):
			if rpm.isSourcePackage:
				sources.append(rpm)
			else:
				binaries.append(rpm)

		buildReqNode = buildNode.find('buildrequires')
		if buildReqNode is not None and sources:
			for rpm in self.processAllRpmChildren(buildReqNode):
				sources[0].resolvedRequires.append((None, rpm))

		self._builds.append((name, component, binaries, sources))

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
			rpm.resolvedRequires = []
			self._packages[key] = rpm

		if False:
			for reqNode in rpmNode.findall('requires'):
				reqLabel = self.processRequires(reqNode)
				for required in self.processAllRpmChildren(reqNode):
					required.label = reqLabel
					rpm.resolvedRequires.append((None, required))

		return rpm
