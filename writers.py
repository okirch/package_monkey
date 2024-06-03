#
# writing out the classification results
#

from csvio import CSVWriter
from xmltree import XMLTree
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from filter import Classification, ClassificationResult
from inversions import InversionMap

class BaseWriter:
	def __init__(self):
		self._excluded = set()
		self._writeEmptyGroups = False

	# FIXME remove
	def excludeLabel(self, labelName):
		self._excluded.add(labelName)

	# FIXME rename to writeClassificationResult
	def write(self, result):
		self.beginWrite()

		for label, requiredTopics in result.enumerateComponents():
			requiredPackages = result.getIncrementalBuildRequires(label)
			self.writeComponent(label, requiredTopics, requiredPackages)

			for buildConfig, requiredTopics in result.enumerateBuildConfigs(label):
				requiredPackages = result.getIncrementalBuildRequires(buildConfig)
				self.writeBuildConfig(buildConfig, requiredTopics, requiredPackages)

		for label, members in result.enumeratePackages():
			if label.name in self._excluded:
				continue

			if not members and not label.defined and not self._writeEmptyGroups:
				continue

			runtimeRequires = result.getMinimalRuntimeRequirements(label)
			inversions = result.getInversions(label)

			self.writeLabelDescription(label, runtimeRequires, inversions)
			self.writePackagesForGroup(label, members)

		for label, buildInfo in result.enumerateBuilds():
			if label is not None and label.name in self._excluded:
				continue

			self.writeBuild(label, buildInfo)

		for pkg, candidates in result.enumerateUnclassifiedPackages():
			self.writeUnclassified(pkg, candidates)

		self.flush()

	def writeComponents(self, result, componentLabels):
		self.beginWrite()

		for label, requiredTopics in result.enumerateComponents():
			if label not in componentLabels:
				continue

			self.writeComponent(label, requiredTopics)

			for buildConfig, requiredTopics in result.enumerateBuildConfigs(label):
				self.writeBuildConfig(buildConfig, requiredTopics)

		for label, members in result.enumeratePackages():
			if label.componentLabel not in componentLabels:
				continue

			if not members and not label.defined and not self._writeEmptyGroups:
				continue

			runtimeRequires = result.getMinimalRuntimeRequirements(label)
			inversions = result.getInversions(label)

			self.writeLabelDescription(label, runtimeRequires, inversions)
			self.writePackagesForGroup(label, members)

		for label, buildInfo in result.enumerateBuilds():
			if label is None or label.parent not in componentLabels:
				continue

			self.writeBuild(label, buildInfo)

		self.flush()

	def writeLabelHierarchy(self, classificationScheme):
		self.beginWrite()

		self.writeFingerprint(classificationScheme.fingerprint)

		componentOrder = classificationScheme.componentOrder()
		for componentLabel in componentOrder.bottomUpTraversal():
			self.writeComponent(componentLabel, componentLabel.runtimeRequires)

			for buildLabel in componentLabel.flavors:
				self.writeBuildConfig(buildLabel, buildLabel.buildRequires)

		topicOrder = classificationScheme.defaultOrder()
		for topic in topicOrder.bottomUpTraversal():
			runtimeRequires = topicOrder.maxima(topic.runtimeRequires)
			runtimeInversions = classificationScheme.inversionMap.get(topic)
			self.writeLabelDescription(topic, runtimeRequires, runtimeInversions)

		self.flush()

	def beginWrite(self):
		pass

	def flush(self):
		pass

	def writeComponent(self, label, requiredComponents, requiredPackages = None):
		pass

	def writeBuildConfig(self, label, requiredTopics, requiredPackages = None):
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

	def writeLabelDescription(self, label, runtimeRequires, inversions):
		print()
		print(f"== {label.type} group {label} ==")

	def writePackagesForGroup(self, label, packages):
		for pkg in sorted(packages, key = lambda p: (p.name, p.arch)):
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
		self.csv = CSVWriter(filename, fields = ['component', 'topic', 'package', 'src'])

		infomsg(f"Writing results to {filename}")

	# FIXME rename to writeClassificationResult
	def write(self, result):
		self.beginWrite()

		buildMap = {}
		for label, buildInfo in result.enumerateBuilds():
			if label is not None and label.name in self._excluded:
				continue

			for rpm in buildInfo.binaries:
				buildMap[rpm] = buildInfo.name

		for label, members in result.enumeratePackages():
			if label.name in self._excluded:
				continue

			if not members and not label.defined and not self._writeEmptyGroups:
				continue

			component = label.componentName
			topic = label.name

			for rpm in sorted(members, key = lambda p: p.name):
				buildName = buildMap.get(rpm, '')
				self.csv.write([component, topic, rpm.shortname, buildName])

		self.flush()

	def writeLabelDescription(self, label, runtimeRequires, inversions):
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

class XmlWriterCommon(BaseWriter):
	def __init__(self, filename = None, io = None):
		super().__init__()
		self.filename = filename
		self.io = io

		self.xmltree = None
		self.componentsElement = None

		self._labels = {}
		self._componentNodes = {}

	def flush(self):
		if self.filename is not None:
			infomsg(f"Writing results to {self.filename}")
			self.xmltree.write(self.filename)
		elif self.io is not None:
			self.xmltree.writeIO(self.io)
		else:
			raise Exception(f"Don't know where to write results to")

	def writeLabelDescription(self, label, runtimeRequires, inversions):
		labelNode = self.xmltree.root.addChild('topic')

		# for now; could also have separate attrs for base name, option, pkgclass
		labelNode.setAttribute('label', label.name)

		if label.disposition != Classification.DISPOSITION_SEPARATE:
			labelNode.setAttribute('disposition', label.disposition)

		if label.isAPI:
			labelNode.setAttribute('api', 1)

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

		# FIXME use writeRequiredTopics
		runtimeNode = labelNode.addChild('runtime')
		for req in sorted(runtimeRequires, key = lambda l: l.name):
			if inversions and req in inversions:
				reqNode = runtimeNode.addChild('inversion')
			else:
				reqNode = runtimeNode.addChild('requires')
			reqNode.setAttribute('label', req.name)
			if req.sourceProject is not None:
				reqNode.setAttribute('component', req.sourceProject.name)

		self._labels[label.name] = labelNode

	def writeComponent(self, label, requiredComponents, requiredPackages = None):
		compNode = self.componentsElement.addChild('component')
		compNode.setAttribute('name', str(label))

		self._componentNodes[label] = compNode

		for purposeName in label.globalPurposeLabelNames:
			purposeLabel = label.globalPurposeLabel(purposeName)
			if purposeLabel is not None:
				globalNode = compNode.addChild('global')
				globalNode.setAttribute('purpose', purposeName)
				globalNode.setAttribute('name', str(purposeLabel))

		if requiredComponents:
			reqNode = compNode.addChild('requires')
			for req in requiredComponents:
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

		if requiredPackages is not None:
			reqNode = compNode.addChild('commonbuild')
			self.writeRPMList(reqNode, requiredPackages)

	def writeRequiredTopics(self, parentNode, xmlTag, topicList, inversions = None):
		reqListNode = parentNode.addChild(xmlTag)
		for req in sorted(topicList, key = lambda l: l.name):
			if inversions and req in inversions:
				reqNode = reqListNode.addChild('inversion')
			else:
				reqNode = reqListNode.addChild('requires')
			reqNode.setAttribute('label', req.name)
			if req.sourceProject is not None:
				reqNode.setAttribute('component', req.sourceProject.name)

		return reqListNode

	def writeBuildConfig(self, label, requiredTopics, requiredPackages = None):
		compNode = self._componentNodes[label.parent]

		bcNode = compNode.addChild('buildconfig')
		bcNode.setAttribute('name', label.name)

		self.writeRequiredTopics(bcNode, 'build', requiredTopics)
		if requiredPackages is not None:
			self.writeCommonBuildRequires(bcNode, requiredPackages)

class XmlSchemeWriter(XmlWriterCommon):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def beginWrite(self):
		assert(self.xmltree is None)

		self.xmltree = XMLTree('classificationscheme')
		self.componentsElement = self.xmltree.root.addChild('components')

	def writeFingerprint(self, fingerprint):
		self.xmltree.root.setAttribute('fingerprint', "{fingerprint:#x}")

# FIXME: rename XmlPackageWriter
class XmlWriter(XmlWriterCommon):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.unclassified = None

	def beginWrite(self):
		assert(self.xmltree is None)

		self.xmltree = XMLTree('componentmodel')
		self.componentsElement = self.xmltree.root.addChild('components')

	def writePackagesForGroup(self, label, packages):
		labelNode = self._labels[label.name]

		self.writeRPMList(labelNode, packages)

	def writeBuild(self, label, buildInfo):
		buildNode = self.xmltree.root.addChild('build')
		buildNode.setAttribute('name', buildInfo.name)

		if label is not None:
			assert(label.type == Classification.TYPE_SOURCE)
			buildNode.setAttribute('component', label)

		buildConfig = buildInfo.buildConfig
		if buildConfig is not None:
			buildNode.addField('buildconfig', buildConfig.name)

		self.writeRPMList(buildNode, buildInfo.sources)

		for rpm in buildInfo.binaries:
			rpmNode = self.writeRPM(buildNode, rpm)
			self.writeRequirements(rpmNode, rpm, label)

		if buildInfo.buildRequires or buildInfo.commonBuildRef:
			bdepNode = buildNode.addChild('buildrequires')
			if buildInfo.commonBuildRef:
				bdepNode.setAttribute('common', str(buildInfo.commonBuildRef))
			self.writeRPMList(bdepNode, buildInfo.buildRequires)

	def writeCommonBuildRequires(self, parentNode, requiredPackages):
		reqNode = parentNode.addChild('commonbuild')
		self.writeRPMList(reqNode, requiredPackages)

	def writeRPMList(self, parentNode, packages):
		if not packages:
			return

		for rpm in sorted(packages, key = lambda p: p.shortname):
			self.writeRPM(parentNode, rpm)

	def writeRPM(self, parent, rpm):
		rpmNode = parent.addChild('rpm')
		rpmNode.setAttribute('name', rpm.name)
		rpmNode.setAttribute('arch', rpm.arch)

		if rpm.isSynthetic:
			rpmNode.setAttribute('synthetic', 1)

		return rpmNode

	def writeRequirements(self, rpmNode, rpm, componentLabel):
		references = []
		for required in rpm.enumerateRequiredRpms():
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

class XmlReaderCommon(object):
	from packages import Versiontools

	nullVersion = Versiontools.ParsedVersion(None, None)

	class LabelInfo(object):
		def __init__(self, label):
			self.label = label
			self.incrementalBuildRequires = None
			self.commonBuildRequires = None

		def updateBuildRequirements(self, underlyingLabelInfo):
			if self.incrementalBuildRequires is None:
				infomsg(f"{labelInfo.label} does not define a <commonbuild> element")
				self.incrementalBuildRequires = set()

			if self.commonBuildRequires is None:
				self.commonBuildRequires = self.incrementalBuildRequires.copy()

			self.commonBuildRequires.update(underlyingLabelInfo.commonBuildRequires)

		def finalizeBuildRequirements(self):
			if self.incrementalBuildRequires is None:
				infomsg(f"{labelInfo.label} does not define a <commonbuild> element")
				self.incrementalBuildRequires = set()

			if self.commonBuildRequires is None:
				self.commonBuildRequires = self.incrementalBuildRequires

	def __init__(self, filename, classificationScheme):
		self.filename = filename

		if classificationScheme is None:
			classificationScheme = Classification.Scheme()
		self.classificationScheme = classificationScheme

		self._packages = {}
		self._builds = []
		self._unclassified = {}
		self._inversions = {}
		self._labelInfo = {}

	def processAllComponents(self, root):
		componentListNode = root.find('components')
		if componentListNode is None:
			raise Exception(f"{self.filename} does not define a <components> element")

		for node in componentListNode:
			if node.tag == 'component':
				self.processComponent(node)
			else:
				raise Exception(f"{self.filename} unsupported element <{node.tag}> inside <components>")

		self.classificationScheme.freezeComponentOrder()

	def processComponent(self, node):
		name = node.attrib['name']
		component = self.validateLabel(name, Classification.TYPE_SOURCE)

		for globalNode in node.findall('global'):
			purposeName = globalNode.attrib['purpose']
			purposeLabel = self.validateLabel(globalNode.attrib['name'], Classification.TYPE_BINARY)
			component.setGlobalPurposeLabel(purposeName, purposeLabel)

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

		for buildConfigNode in node.findall('buildconfig'):
			self.processBuildConfig(buildConfigNode)

		return component

	def processAllTopics(self, root):
		# FIXME: put <topic> nodes in a <topics> element
		for node in root.findall('topic'):
			self.processTopic(node)

		if root.find('label') is not None:
			warnmsg(f"{self.filename} has obsolete <label> elements")
			for node in root.findall('label'):
				self.processTopic(node)

		self.classificationScheme.freezeBinaryOrder()

	def processTopic(self, labelNode):
		name = labelNode.attrib['label']

		label = self.validateLabel(name, Classification.TYPE_BINARY)
		self.validateComponent(label, labelNode.attrib.get('component'))

		if labelNode.attrib.get('api'):
			# for the time being, we don't establish the full API links, we just want .isAPI()
			# to return something useful
			label.isAPI = True

		runtime = labelNode.find('runtime')
		if runtime is not None:
			inversions = Classification.createLabelSet()
			for reqNode in runtime.findall('requires'):
				reqLabel = self.processRequires(reqNode)
				label.addRuntimeDependency(reqLabel)
			for reqNode in runtime.findall('inversion'):
				reqLabel = self.processRequires(reqNode)
				label.addRuntimeDependency(reqLabel)
				inversions.add(reqLabel)

			self._inversions[label] = inversions

		return label

	def processRequires(self, reqNode):
		labelName = reqNode.attrib.get('label') or reqNode.attrib.get('name')
		assert(labelName)
		reqLabel = self.validateLabel(labelName, Classification.TYPE_BINARY)
		componentName = reqNode.attrib.get('component')
		if componentName is not None:
			self.validateComponent(reqLabel, componentName)
		return reqLabel

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

	def processBuildConfig(self, node):
		name = node.attrib['name']
		label = self.validateLabel(name, Classification.TYPE_BUILDCONFIG)

		depListNode = node.find('build')
		if depListNode is not None:
			for reqNode in depListNode.findall('requires'):
				reqLabel = self.processRequires(reqNode)
				label.addBuildDependency(reqLabel)

		return label

# FIXME: rename XmlPackageReader
class XmlReader(XmlReaderCommon):
	def read(self):
		import xml.etree.ElementTree as ET

		tree = ET.parse(self.filename)
		root = tree.getroot()
		if root.tag != 'componentmodel':
			raise Exception(f"invalid root element <{root.tag}> in {self.filename}")

		self.processAllComponents(root)
		self.processAllTopics(root)

		for node in root:
			if node.tag in ('components', 'topic', 'label'):
				# already processed above
				continue

			if node.tag == 'build':
				self.processBuild(node)
			elif node.tag == 'unclassified':
				self.processUnclassified(node)
			else:
				raise Exception(f"unsupported element <{node.tag}> in {self.filename}")

		componentOrder = self.classificationScheme.componentOrder()
		topicOrder = self.classificationScheme.defaultOrder()

		result = ClassificationResult(topicOrder, componentOrder)
		for rpm in self._packages.values():
			result.labelOnePackage(rpm, rpm.label, None)
		for name, component, buildConfig, binaries, sources in self._builds:
			buildTracking = result.labelOneBuild(name, component, binaries, sources)
			buildTracking.buildConfig = buildConfig
			if buildConfig is not None:
				result.buildConfigMembership(buildConfig).track(buildTracking)
		for rpm, candidates in self._unclassified.items():
			result.addUnclassified(rpm, candidates)

		result.inversionMap = InversionMap()
		for label, inversions in self._inversions.items():
			result.inversionMap.add(label, inversions)

		for info in self._labelInfo.values():
			project = result.projectMembership(info.label)
			project.commonBuildRequires = info.commonBuildRequires
			project.incrementalBuildRequires = info.incrementalBuildRequires

		# target is a BuildTracking object of the ClassificationResult
		def updateBuildTracking(target):
			labelInfo = self._labelInfo.get(target.label)
			if labelInfo is not None:
				target.commonBuildRequires = labelInfo.commonBuildRequires
				target.incrementalBuildRequires = labelInfo.incrementalBuildRequires

		for component in componentOrder.bottomUpTraversal():
			updateBuildTracking(result.projectMembership(component))
			for buildConfig in component.flavors:
				updateBuildTracking(result.buildConfigMembership(buildConfig))

		return result

	def processAllComponents(self, root):
		super().processAllComponents(root)

		componentOrder = self.classificationScheme.componentOrder()
		for componentLabel in componentOrder.bottomUpTraversal():
			assert(componentLabel.runtimeRequires == componentOrder.lowerNeighbors(componentLabel))
			componentInfo = self._labelInfo[componentLabel]

			# The build requirements of component Foo are the union of its own
			# <commonbuild> element and the <commonbuild> elements of the components
			# it depends on
			for lowerLabel in componentOrder.lowerNeighbors(componentLabel):
				componentInfo.updateBuildRequirements(self._labelInfo[lowerLabel])

			componentInfo.finalizeBuildRequirements()

			# The build requirements of buildconfig Foo/standard are the union of its own
			# <commonbuild> element and the <commonbuild> element of component Foo
			standardLabel = componentLabel.getBuildFlavor('standard')
			standardInfo = self._labelInfo.get(standardLabel)
			if standardInfo is not None:
				standardInfo.updateBuildRequirements(componentInfo)
				standardInfo.finalizeBuildRequirements()

			# The build requirements of buildconfig Foo/other are the union of its own
			# <commonbuild> element and the <commonbuild> element of Foo/standard
			for buildConfig in componentLabel.flavors:
				if standardInfo is None:
					raise Exception(f"{self.filename} defines buildconfig {buildConfig}, but not {standardLabel}")

				buildInfo = self._labelInfo.get(buildConfig)
				if buildInfo is not None:
					buildInfo.updateBuildRequirements(standardInfo)
					buildInfo.finalizeBuildRequirements()

					assert(len(buildInfo.commonBuildRequires) >= len(standardInfo.commonBuildRequires))
					assert(len(buildInfo.commonBuildRequires) > 0)

	def processComponent(self, node):
		componentLabel = super().processComponent(node)
		self.processCommonBuild(componentLabel, node)
		return componentLabel

	def processBuildConfig(self, node):
		buildLabel = super().processBuildConfig(node)
		self.processCommonBuild(buildLabel, node)
		return buildLabel

	def processTopic(self, labelNode):
		topic = super().processTopic(labelNode)
		for rpm in self.processAllRpmChildren(labelNode):
			rpm.setLabel(topic, "baseline classification")
		return topic

	# Some elements (like <component> and <buildconfig>) can have a <commonbuild> child
	# element. This is used to have more compact build requirements lists for <build>
	# elements.
	# For <component> or <buildconfig> element we process, we create a LabelInfo object.
	# If the element contains a <commonbuild> child, we process the list of rpms
	# it references, and stick it into labelInfo.incrementalBuildRequires
	# When done with processing all components, we traverse the label hierarchy and
	# update each labelInfo.commonBuildRequires
	def createLabelInfo(self, label):
		info = self._labelInfo.get(label)
		if info is None:
			info = self.LabelInfo(label)
			self._labelInfo[label] = info
		return info

	def processCommonBuild(self, label, node):
		labelInfo = self.createLabelInfo(label)

		commonBuildNode = node.find('commonbuild')
		if commonBuildNode is not None:
			labelInfo.incrementalBuildRequires = set(self.processAllRpmChildren(commonBuildNode))

	# FIXME: this could be a LabelInfo method
	def xxxupdateBuildRequirementsForLabel(self, labelInfo, underlyingLabels):
		incrementalRequires = labelInfo.incrementalBuildRequires
		if incrementalRequires is None:
			infomsg(f"{labelInfo.label} does not define a <commonbuild> element")
			incrementalRequires = set()

		if underlyingLabels:
			commonRequires = incrementalRequires.copy()
			for label in underlyingLabels:
				commonRequires.update(self._labelInfo[label].commonBuildRequires)
		else:
			commonRequires = incrementalRequires

		labelInfo.incrementalBuildRequires = incrementalRequires
		labelInfo.commonBuildRequires = commonRequires

	def processBuild(self, buildNode):
		name = buildNode.attrib['name']

		component = None
		componentName = buildNode.attrib.get('component')
		if componentName is not None:
			component = self.validateLabel(componentName, Classification.TYPE_SOURCE)

		buildConfig = None
		buildConfigNode = buildNode.find('buildconfig')
		if buildConfigNode is not None:
			buildConfig = self.validateLabel(buildConfigNode.text.strip(), Classification.TYPE_BUILDCONFIG)

		binaries = []
		sources = []
		for rpm in self.processAllRpmChildren(buildNode):
			if rpm.isSourcePackage:
				sources.append(rpm)
			else:
				binaries.append(rpm)

		buildReqNode = buildNode.find('buildrequires')
		if buildReqNode is not None and sources:
			allBuildRequires = set(self.processAllRpmChildren(buildReqNode))

			commonBuildReference = buildReqNode.attrib.get('common')
			if commonBuildReference is not None:

				commonBuildLabel = self.classificationScheme.getLabel(commonBuildReference)
				if commonBuildLabel is not None:
					info = self._labelInfo.get(commonBuildLabel)
				else:
					info = None

				if info is None:
					raise Exception(f"Build {name} references common buildreq set {commonBuildReference} but I can't find it")

				if info.commonBuildRequires is not None:
					allBuildRequires.update(info.commonBuildRequires)

			for rpm in allBuildRequires:
				sources[0].resolvedRequires.append((None, rpm))

		self._builds.append((name, component, buildConfig, binaries, sources))

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

	def processAllRpmChildren(self, node):
		for rpmNode in node.findall('rpm'):
			yield self.processRpm(rpmNode)

	def processRpm(self, rpmNode):
		from packages import Package, PackageInfo

		name = rpmNode.attrib['name']
		arch = rpmNode.attrib['arch']

		key = f"{name}.{arch}"
		rpm = self._packages.get(key)
		if rpm is None:
			pinfo = PackageInfo.fromNameAndParsedVersion(name, arch, self.nullVersion)
			rpm = Package.fromPackageInfo(pinfo)
			rpm.resolvedRequires = []
			self._packages[key] = rpm

		if rpmNode.attrib.get('synthetic'):
			rpm.markSynthetic()

		for reqNode in rpmNode.findall('requires'):
			for required in self.processAllRpmChildren(reqNode):
				rpm.resolvedRequires.append((None, required))

		return rpm

class XmlSchemeReader(XmlReaderCommon):
	def __init__(self, path):
		super().__init__(path, None)

	def read(self):
		import xml.etree.ElementTree as ET

		tree = ET.parse(self.filename)
		root = tree.getroot()
		if root.tag != 'classificationscheme':
			raise Exception(f"invalid root element <{root.tag}> in {self.filename}")

		self.processAllComponents(root)
		self.processAllTopics(root)

		for node in root:
			if node.tag in ('components', 'topic', 'label'):
				# already processed above
				continue

			raise Exception(f"unsupported element <{node.tag}> in {self.filename}")

		self.classificationScheme._final = True

		return self.classificationScheme
