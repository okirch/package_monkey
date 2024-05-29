from filter import Classification
from queries import GenericQuery, GenericRenderer, InversionsQueryMixin
from queries import PackageRequiresLabelsPredicate, RequirementsReport
from queries import QueryContext, QuerySubjectComponent, QuerySubjectComponentOrTopic

##################################################################
# Query runtime dependencies of one component/topic vs another
##################################################################
class WhatRequiresRenderer(GenericRenderer):
	def renderRequirementsReport(self, component, requirementsReport, requiredLabel):
		if not requirementsReport:
			print(f"   {component}: nothing requires {requiredLabel}")
		elif not requirementsReport.verbosityLevel:
			print(f"   {component}: the following topics require {requiredLabel}")
			for label in requirementsReport.minimalLabels:
				print(f"      {label}")
		else:
			print(f"   {component}: the following topics and packages require {requiredLabel}")
			self.renderRequirementsReportDetails(component, requirementsReport)

	def renderRequirementsReportDetails(self, component, requirementsReport):
		alreadyReported = Classification.createLabelSet()
		for label in requirementsReport.allLabels:
			packages = sorted(requirementsReport.packagesForLabel(label), key = str)
			if packages:
				print(f"      {label}")
				self.renderRequirementsForLabel(requirementsReport, label)
			elif label.parent in alreadyReported:
				pass
			elif self.context.getPackagesForLabel(label):
				print(f"      {label} (just label dependency)")
				self.renderRequirementsForLabel(requirementsReport, label)
			else:
				print(f"      {label} (empty label)")

			alreadyReported.add(label)

		dropAdvice = requirementsReport.dropRequirementsFrom
		if dropAdvice:
			print()
			print("      Consider dropping explicit requirements from the following label(s)")
			for label in sorted(dropAdvice, key = str):
				print(f"       - {label}")

	def renderRequirementsForLabel(self, requirementsReport, label):
		projection = set()

		tf = requirementsReport.subtreeForLabel(label)
		if tf is not None and requirementsReport.verbosityLevel >= 2:
			for prefix, topic in tf.render():

				s = f"{topic.componentName}:{topic}"
				if topic in requirementsReport.inspectLabelSet:
					s = tf.standout(s)
				print(f"          {prefix}{s}")

		packages = sorted(requirementsReport.packagesForLabel(label), key = str)
		if not packages:
			return

		for rpm in packages:
			print(f"         {rpm}")

			rpmReqs = requirementsReport.requirementsForRpm(rpm)
			if not rpmReqs:
				continue

			tf = rpmReqs.format()

			if requirementsReport.verbosityLevel < 2:
				for prefix, node in tf.render():
					if node.label:
						projection.add(node)
			else:
				for prefix, node in tf.render():
					if node.label is not None:
						print(f"          {prefix}{node.rpm} ({node.label})")
					else:
						print(f"          {prefix}{node.rpm}")

		if projection:
			print()
			print(f"        The following packages are used:")
			for node in sorted(projection, key = str):
				print(f"         {node.rpm} ({node.label})")
			print()

	def reportImportedPackages(self, component, label, pkgs):
		if not pkgs:
			return

		print()
		print(f"   {component} imports the following packages from {label} (component {label.componentName})")
		for rpm in pkgs:
			print(f"       - {rpm}")

class WhatRequiresQuery(GenericQuery):
	def __init__(self, context, what_requires, renderer = None):
		if renderer is None:
			renderer = WhatRequiresRenderer(context)
		super().__init__(context, renderer)
		self.what_requires = what_requires

	def __call__(self, queryTargetLabel):
		renderer = self.renderer
		context = self.context

		querySubject = QuerySubjectComponentOrTopic("what-requires", context, queryTargetLabel)

		for req in self.what_requires:
			topic = context.getLabel('label', req)

			if topic.type is Classification.TYPE_BINARY:
				inspectLabelSet = Classification.createLabelSet([topic])
			elif topic.type is Classification.TYPE_SOURCE:
				inspectLabelSet = context.getLabelsForComponent(topic)
			else:
				raise Exception(f"{self.__class__.__name__}: label type {topic.type} not supported")

			report = self.getTopicsRequiringLabelSet(querySubject, inspectLabelSet)

			renderer.renderRequirementsReport(queryTargetLabel, report, topic)

	def getTopicsRequiringLabelSet(self, querySubject, inspectLabelSet):
		verbosityLevel = self.context.verbosityLevel
		labelOrder = self.context.labelOrder

		result = RequirementsReport(self.context, inspectLabelSet, verbosityLevel = verbosityLevel)

		predicate = None
		if verbosityLevel:
			predicate = PackageRequiresLabelsPredicate(self.context.classification, inspectLabelSet)
			store = self.context.connectDatabase()

		inspectUpperCone = labelOrder.upwardClosureForSet(inspectLabelSet)
		for label in self.context.topDownTraversal(querySubject.binaryLabels):
			# skip over labels that are completely empty
			if not self.context.getPackagesForLabel(label):
				continue

			# hits is a convex set that includes the subtree that is limited by label above and
			# inspectLabelSet below.
			hits = labelOrder.downwardClosureFor(label).intersection(inspectUpperCone)
			if hits:
				result.add(label, hits)

				if verbosityLevel:
					for orpm in self.context.getPackagesForLabel(label):
						if orpm.isSynthetic:
							continue

						# Retrieve rpm dependencies from the DB
						rpm = store.recoverLatestPackageByName(orpm.name)

						if rpm is None:
							raise Exception(f"label {label} references {orpm} which is not in the DB")

						if predicate(rpm):
							result.addPackage(label, rpm, predicate)

					explicitRequirement = label.configuredRuntimeRequires.intersection(inspectLabelSet)
					if explicitRequirement and not result.packagesForLabel(label):
						result.adviseDropRequirements(label, explicitRequirement)

		return result


##################################################################
# Query runtime inversions for a set of components
##################################################################
class InversionsRenderer(WhatRequiresRenderer):
	def renderRequirementsReport(self, component, requirementsReport):
		if not requirementsReport:
			print(f"   {component}: no inversions")
		elif not requirementsReport.verbosityLevel:
			print(f"   {component}: the following topics have inversions")
			for label in requirementsReport.minimalLabels:
				print(f"      {label}")
		else:
			print(f"   {component}: the following topics and packages have inversions")
			self.renderRequirementsReportDetails(component, requirementsReport)

class InversionsQuery(WhatRequiresQuery, InversionsQueryMixin):
	def __init__(self, context, ignore):
		renderer = InversionsRenderer(context)
		super().__init__(context, [], renderer = renderer)

		InversionsQueryMixin.__init__(self, context, ignore)

	def __call__(self, queryTargetLabel):
		renderer = self.renderer
		context = self.context

		querySubject = QuerySubjectComponentOrTopic("inversions", context, queryTargetLabel)

		report = self.getTopicsRequiringLabelSet(querySubject, self.inversionLabels)
		renderer.renderRequirementsReport(queryTargetLabel, report)

class InversionRenderer(GenericRenderer):
	def __init__(self, context):
		self.context = context
		self.indexFormatter = IndexFormatterTwoLevels()

	def render(self, componentName, invTopic, requiredBy):
		inversionName = f"{self.renderTopic(invTopic)} required by:"
		for label in requiredBy:
			packageCount = self.context.getPackageCountForLabel(label)
			self.indexFormatter.next(componentName, inversionName, self.renderTopic(label))

	def renderTopic(self, label):
		packageCount = self.context.getPackageCountForLabel(label)
		return f"{label} ({packageCount} packages)"

