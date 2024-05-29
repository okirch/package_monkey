from filter import Classification
from queries import GenericQuery, GenericRenderer, InversionsQueryMixin
from queries import PackageRequiresLabelsPredicate, BuildRequirementsReport
from queries import QueryContext, QuerySubjectComponent, QuerySubjectComponentOrTopic

##################################################################
# Query build dependencies of one component/topic vs another
##################################################################
class WhatBuildRequiresRenderer(GenericRenderer):
	def renderRequirementsReport(self, component, requirementsReport, requiredLabel):
		if not requirementsReport:
			print(f"   {component}: nothing requires {requiredLabel}")
		elif not requirementsReport.verbosityLevel:
			print(f"   {component}: the following packages buildrequire {requiredLabel}")
			for rpm in requirementsReport.allRequiredRpms:
				print(f"      {rpm}")
		else:
			print(f"   {component}: the following packages buildrequire {requiredLabel}")
			self.renderRequirementsReportDetails(component, requirementsReport)

	def renderRequirementsReportDetails(self, component, requirementsReport):
		if len(requirementsReport) <= 1:
			commonRpms = set()
		else:
			commonRpms = requirementsReport.commonRequiredRpms
			if commonRpms:
				print(f"     Packages required by all of the following builds:")
				self.renderPackages(commonRpms)
			print()

		verbosityLevel = self.context.verbosityLevel
		for obsBuild, requiredTopics, requiredRpms in requirementsReport.enumerate():
			if not requiredRpms:
				if verbosityLevel < 2:
					print(f"     {obsBuild} (only label dependency)")
				continue

			extraRpms = requiredRpms.difference(commonRpms)
			if not extraRpms:
				print(f"     {obsBuild} (only common packages)")
				continue

			print(f"     {obsBuild}:")

			if obsBuild.sources:
				srpm = obsBuild.sources[0]
				requirements = requirementsReport.requirementsForRpm(srpm)
				if requirements:
					tf = requirements.format()
					for prefix, node in tf.render():
						if node.label is not None:
							print(f"     {prefix}{node.rpm} ({node.label})")
						else:
							print(f"     {prefix}{node.rpm}")
					continue

			self.renderPackages(extraRpms)

		return

	def renderPackages(self, rpmSet):
		for rpm in sorted(rpmSet, key = lambda R: str(self.context.getLabelForPackage(R))):
			label = self.context.getLabelForPackage(rpm)
			print(f"       {rpm} ({label.componentName}:{label})")

class WhatBuildRequiresQuery(GenericQuery):
	def __init__(self, context, what_requires, renderer = None):
		if renderer is None:
			renderer = WhatBuildRequiresRenderer(context)
		super().__init__(context, renderer)
		self.what_requires = what_requires

	def __call__(self, queryTargetLabel):
		renderer = self.renderer
		context = self.context

		querySubject = QuerySubjectComponentOrTopic("what-build-requires", context, queryTargetLabel)

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

		result = BuildRequirementsReport(self.context, inspectLabelSet, verbosityLevel = verbosityLevel)

		predicate = None
		if verbosityLevel:
			predicate = PackageRequiresLabelsPredicate(self.context.classification, inspectLabelSet)
			store = self.context.connectDatabase()

		obsBuilds = set()
		for label in querySubject.binaryLabels:
			for rpm in self.context.getPackagesForLabel(label, fromDB = True):
				build = self.context.getBuildForPackage(rpm)
				if build is None:
					raise Exception(f"unable to find an OBS build for {rpm} ({label})")
				obsBuilds.add(build)

		labelOrder = self.context.labelOrder
		inspectUpperCone = labelOrder.upwardClosureForSet(inspectLabelSet)

		for obsBuild in sorted(obsBuilds, key = str):
			requiredTopics = Classification.createLabelSet()
			requiredRpms = set()
			for rpm, requiredLabel in self.context.enumerateBuildRequirements(obsBuild):
				if requiredLabel in inspectUpperCone:
					requiredTopics.update(inspectLabelSet.intersection(labelOrder.downwardClosureFor(requiredLabel)))
				if requiredLabel in inspectLabelSet:
					requiredRpms.add(rpm)

			if predicate and obsBuild.sources:
				assert(len(obsBuild.sources) == 1)
				srpm = obsBuild.sources[0]
				if predicate(srpm):
					result.addPackageRequirements(predicate, srpm)

			if requiredTopics:
				result.add(obsBuild, requiredRpms, requiredTopics)

		return result

##################################################################
# Query build inversions for a set of components
##################################################################
class BuildInversionsQuery(GenericQuery, InversionsQueryMixin):
	def __init__(self, context, ignore, renderer = None):
		if renderer is None:
			renderer = BuildInversionsRenderer(context)

		super().__init__(context, renderer)
		InversionsQueryMixin.__init__(self, context, ignore)

	def __call__(self, queryTargetLabel):
		renderer = self.renderer
		context = self.context

		querySubject = QuerySubjectComponentOrTopic("inversions", context, queryTargetLabel)

		report = self.getTopicsRequiringLabelSet(querySubject, self.inversionLabels)
		renderer.renderRequirementsReport(queryTargetLabel, report)
		return
		renderer = self.renderer
		context = self.context

		querySubject = QuerySubjectComponentOrTopic("what-build-requires", context, queryTargetLabel)

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

		result = BuildRequirementsReport(self.context, inspectLabelSet, verbosityLevel = verbosityLevel)

		predicate = None
		if verbosityLevel:
			predicate = PackageRequiresLabelsPredicate(self.context.classification, inspectLabelSet)
			store = self.context.connectDatabase()

		obsBuilds = set()
		for label in querySubject.binaryLabels:
			for rpm in self.context.getPackagesForLabel(label, fromDB = True):
				build = self.context.getBuildForPackage(rpm)
				if build is None:
					raise Exception(f"unable to find an OBS build for {rpm} ({label})")
				obsBuilds.add(build)

		labelOrder = self.context.labelOrder
		inspectUpperCone = labelOrder.upwardClosureForSet(inspectLabelSet)

		for obsBuild in sorted(obsBuilds, key = str):
			requiredTopics = Classification.createLabelSet()
			requiredRpms = set()
			for rpm, requiredLabel in self.context.enumerateBuildRequirements(obsBuild):
				if requiredLabel in inspectUpperCone:
					requiredTopics.update(inspectLabelSet.intersection(labelOrder.downwardClosureFor(requiredLabel)))
				if requiredLabel in inspectLabelSet:
					requiredRpms.add(rpm)

			if predicate and obsBuild.sources:
				assert(len(obsBuild.sources) == 1)
				srpm = obsBuild.sources[0]
				if predicate(srpm):
					result.addPackageRequirements(predicate, srpm)

			if requiredTopics:
				result.add(obsBuild, requiredRpms, requiredTopics)

		return result

class BuildInversionsRenderer(WhatBuildRequiresRenderer):
	def renderRequirementsReport(self, component, requirementsReport):
		if not requirementsReport:
			print(f"   {component}: no build inversions")
		elif not requirementsReport.verbosityLevel:
			print(f"   {component}: the following topics have build inversions")
			for label in requirementsReport.minimalLabels:
				print(f"      {label}")
		else:
			print(f"   {component}: the following topics and packages have build inversions")
			self.renderRequirementsReportDetails(component, requirementsReport)

