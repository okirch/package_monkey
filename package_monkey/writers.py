#
# writing out the classification results
#

import os, datetime

from .csvio import CSVWriter
from .xmltree import XMLTree
from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .filter import Classification
from .policy import Policy
from .arch import ArchSet
from .newdb import GenericRpm

def sortByLabel(item): return str(item[0])

class BaseWriter(object):
	def __init__(self):
		self._writeEmptyGroups = False

	def writeClassificationResult(self, result):
		self.beginWrite()

		self.writeLabelHierarchyWork(result.classificationScheme)

		for label, members in result.enumeratePackages():
			if not members and not label.defined and not self._writeEmptyGroups:
				continue

			runtimeRequires = result.getMinimalRuntimeRequirements(label)

			self.writeTopic(label, runtimeRequires)
			self.writePackagesForGroup(label, members)

		for label, buildInfo in result.enumerateBuilds():
			self.writeBuild(label, buildInfo, allArchitectures = result.classificationScheme.defaultArchitectures)

		for entry in result.enumerateUnclassifiedPackages():
			self.writeUnclassified(entry)

		self.flush()

	def writeComponents(self, result, componentLabels):
		self.beginWrite()

		for label, requiredTopics in result.enumerateComponents():
			if label not in componentLabels:
				continue

			self.writeComponent(label, requiredTopics)

		for label, members in result.enumeratePackages():
			if label.epic not in componentLabels:
				continue

			if not members and not label.defined and not self._writeEmptyGroups:
				continue

			runtimeRequires = result.getMinimalRuntimeRequirements(label)

			self.writeTopic(label, runtimeRequires)
			self.writePackagesForGroup(label, members)

		for label, buildInfo in result.enumerateBuilds():
			if label is None or label.parent not in componentLabels:
				continue

			self.writeBuild(label, buildInfo)

		self.flush()

	def writeLabelHierarchy(self, classificationScheme):
		self.beginWrite()
		self.writeLabelHierarchyWork(classificationScheme)
		self.flush()

	def writeLabelHierarchyWork(self, classificationScheme):
		# self.writeFingerprint(classificationScheme.fingerprint)

		self.writeDefaultArchitectures(classificationScheme.defaultArchitectures)
		self.writePolicy(classificationScheme.policy)

		for role in sorted(classificationScheme.packageRoles, key = str):
			self.writeRole(role)

		self.writeGenericLabels('classes', 'class', classificationScheme.allTopicClasses)
		self.writeGenericLabels('flavors', 'flavor', classificationScheme.allAutoFlavors)
		self.writeGenericLabels('buildoptions', 'option', classificationScheme.allBuildOptions)

		layerOrder = classificationScheme.layerOrder()
		for layer in layerOrder.bottomUpTraversal():
			self.writeLayer(layer, layer.runtimeRequires)

		componentOrder = classificationScheme.componentOrder()
		for componentLabel in componentOrder.bottomUpTraversal():
			self.writeComponent(componentLabel, componentLabel.runtimeRequires)

	def beginWrite(self):
		pass

	def flush(self):
		pass

	def writeGenericLabels(self, groupTag, labelTag, labels):
		pass

	def writeComponent(self, label, requiredComponents):
		pass

	def writeUnclassified(self, entry):
		pass

	def writeRole(self, role):
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

	def writeTopic(self, label, runtimeRequires):
		print()
		print(f"== {label.type} group {label} ==")

	def writePackagesForGroup(self, label, packages):
		for pkg in sorted(packages, key = lambda p: (p.name, p.arch)):
			print(f"  {pkg}")

	def writeBuild(self, *args, **kwargs):
		pass

	def indentingWriter(self):
		return StandardWriter.IndentingWriter()

class CsvPackageWriter(BaseWriter):
	def __init__(self, filename = None):
		super().__init__()
		self.csv = CSVWriter(filename, fields = ['epic', 'topic', 'role', 'package', 'src', 'rpmtype'])

		infomsg(f"Writing results to {filename}")

	def writeClassificationResult(self, result):
		self.beginWrite()

		buildMap = {}
		for label, buildInfo in result.enumerateBuilds():
			for rpm in buildInfo.binaries:
				buildMap[rpm] = buildInfo

		for label, members in result.enumeratePackages():
			if not members and not label.defined and not self._writeEmptyGroups:
				continue

			component = label.epicName
			topic = label.name

			for rpm in sorted(members, key = lambda p: p.name):
				build = buildMap.get(rpm)
				if build is not None:
					buildName = build.name
					if build.isSplitBuild and label.epic is not build.label:
						infomsg(f"Suppress build info for {rpm}: {label} does not match {build} epic {build.label}")
						buildName = f"{build.name}@{label.epic}"
				else:
					if not rpm.isIgnored:
						warnmsg(f"{rpm}: did not find a corresponding build")
					buildName = ''

				roleName = None
				if rpm.labelHints and rpm.labelHints.role:
					roleName = rpm.labelHints.role.name

				self.csv.write([component, topic, roleName, rpm.name, buildName, rpm.type])

		for entry in result.enumerateUnclassifiedPackages():
			for rpm in entry.rpms:
				epic = None
				if entry.minCandidateEpics is not None and \
				   entry.minCandidateEpics == entry.maxCandidateEpics and \
				   len(entry.minCandidateEpics) == 1:
					epic = next(iter(entry.minCandidateEpics))

				if epic is None and rpm in buildMap:
					epic = buildMap[rpm].label

				self.csv.write([epic, None, None, rpm.name, entry.buildName])

		self.flush()

	def writeTopic(self, label, runtimeRequires):
		pass

	def writePackagesForGroup(self, label, packages):
		component = label.epicName
		topic = label.name
		for pkg in sorted(packages, key = lambda p: p.name):
			self.csv.write([component, topic, pkg.shortname])

	def writeBuild(self, *args, **kwargs):
		pass
