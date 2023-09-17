#
# writing out the classification results
#


class BaseWriter:
	def __init__(self):
		self._excluded = set()
		self._writeEmptyGroups = False

	def excludeLabel(self, labelName):
		self._excluded.add(labelName)

	def write(self, packageFilter, order = None):
		if order is None:
			order = packageFilter.defaultOrder()

		for label in order:
			if label.name in self._excluded:
				continue

			members = packageFilter.getGroupPackages(label)
			if not members and not self._writeEmptyGroups:
				continue

			self.writeLabelDescription(label)
			self.writePackagesForGroup(label, members)


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

	def writeLabelDescription(self, label):
		print()
		print(f"== {label.type} group {label} ==")

	def writePackagesForGroup(self, label, packages):
		for pkg in sorted(packages, key = lambda p: p.name):
			print(f"  {pkg}")

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
