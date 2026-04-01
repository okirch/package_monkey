from ..filter import Classification
from ..queries import GenericQuery, GenericRenderer
from ..queries import PackageRequiresLabelsPredicate, RequirementsReport
from ..util import OptionalCaption

class Requirement(object):
	def __init__(self, requiringLabel, requiredLabel, requiredTopicSet = None):
		self.requiringLabel = requiringLabel
		self.requiredLabel = requiredLabel
		self.requiredTopicSet = requiredTopicSet or set()
		self.report = None

	def __str__(self):
		return str(self.requiringLabel)

##################################################################
# Query runtime dependencies of one component/topic vs another
##################################################################
class WhatRequiresRenderer(GenericRenderer):
	def __init__(self, *args, onlyRpms = False, **kwargs):
		super().__init__(*args, **kwargs)
		self.onlyRpms = onlyRpms
		self.showIrrelevant = False

	def renderRequirementsReport(self, requirement):
		if not requirement.report:
			if self.showIrrelevant:
				print(f"   {requirement}: nothing requires {requirement.requiredLabel}")
		elif not self.verbosityLevel:
			print(f"   {requirement}: the following topics require {requirement.requiredLabel}")
			for label in requirement.report.minimalLabels:
				print(f"      {label}")
		else:
			printfn = OptionalCaption(f"   {requirement}: the following topics and packages require {requirement.requiredLabel}", msgfunc = print)
			self.renderRequirementsReportDetails(requirement, printfn)

	def renderRequirementsReportDetails(self, requirement, print):
		requirementsReport = requirement.report

		alreadyReported = Classification.createLabelSet()
		for label in requirementsReport.allLabels:
			if requirementsReport.packagesForLabel(label):
				print(f"   {label}")
				self.renderRequirementsForLabel(requirement, label)
			elif self.onlyRpms:
				pass
			elif label.parent in alreadyReported:
				pass
			elif label.isIgnored:
				print(f"   {label} (IGNORED)")
			elif self.context.classification.getPackagesForLabel(label):
				print(f"   {label} (just label dependency)")
				self.renderRequirementsForLabel(requirement, label)
			else:
				print(f"   {label} (empty label)")

			alreadyReported.add(label)

	def renderRequirementsForLabel(self, requirement, label):
		requirementsReport = requirement.report

		projection = set()

		tf = requirementsReport.subtreeForLabel(label)
		if tf is not None and self.verbosityLevel >= 2:
			for prefix, topic in tf.render():
				s = f"{topic.componentName}:{topic}"
				if topic in requirement.requiredTopicSet:
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

			if self.verbosityLevel < 2:
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
	def __init__(self, context, what_requires, renderer = None, onlyRpms = False):
		if renderer is None:
			renderer = WhatRequiresRenderer(context, onlyRpms = onlyRpms)
		super().__init__(context, renderer)
		self.what_requires = what_requires

		visibleTopics = context.argumentsToTopics(context.application.opts.epics, buildClosure = True)
		self.relevantQueryScope = context.labelOrder.downwardClosureForSet(visibleTopics)

	def perform(self, queryNames, **kwargs):
		if queryNames:
			self.renderer.showIrrelevant = True
		super().perform(queryNames, **kwargs)

	def __call__(self, requiringLabel):
		renderer = self.renderer
		context = self.context

		# loop over all potentially required labels and inspect the topics associated with it
		for potentiallyRequired in context.enumerateLabelsForQuery(self.what_requires):
			if potentiallyRequired is requiringLabel:
				continue

			requirement = Requirement(requiringLabel, potentiallyRequired, self.topicsForLabel(potentiallyRequired))
			requirement.report = self.getTopicsRequiringLabelSet(requirement)
			renderer.renderRequirementsReport(requirement)

	def getTopicsRequiringLabelSet(self, requirement):
		classification = self.context.classification
		verbosityLevel = self.context.verbosityLevel
		labelOrder = self.context.labelOrder

		report = RequirementsReport(self.context)

		predicate = None
		if verbosityLevel:
			predicate = PackageRequiresLabelsPredicate(self.context.classification, requirement.requiredTopicSet)
			store = self.context.connectDatabase()

		# requiredTopicSet is the set of topics that correspond to label potentiallyRequired.
		# If potentiallyRequired is a topic, this set contains just potentiallyRequired itself.
		# If potentiallyRequired is an epiy, this set contains all topics associated with the epic.
		# inspectUpperCone is the set of all labels that require a topic associated with potentiallyRequired.
		inspectUpperCone = labelOrder.upwardClosureForSet(requirement.requiredTopicSet)

		for requiringTopic in labelOrder.topDownTraversal(self.topicsForLabel(requirement.requiringLabel)):
			# skip over labels that are completely empty
			if not classification.getPackagesForLabel(requiringTopic):
				continue

			# hits is a convex set that includes the subtree that is limited by requiringTopic above and
			# requiredTopicSet below. In other words, it contains all possible paths going from
			# requiringTopic to a topic associated with potentiallyRequired.
			hits = labelOrder.downwardClosureFor(requiringTopic).intersection(inspectUpperCone)
			if hits:
				report.add(requiringTopic, hits)

				if verbosityLevel:
					for orpm in classification.getPackagesForLabel(requiringTopic):
						if orpm.isSynthetic:
							continue

						# Retrieve rpm dependencies from the DB
						rpm = store.lookupRpm(orpm.name)

						if rpm is None:
							raise Exception(f"label {requiringTopic} references {orpm} which is not in the DB")

						if predicate(rpm):
							report.addPackage(requiringTopic, rpm, predicate)

		return report
