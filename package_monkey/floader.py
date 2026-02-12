##################################################################
#
# Load package-monkey configuration from yaml files
#
##################################################################
import yaml
import os
import datetime

from .util import TimedExecutionBlock
from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .util import VariableExpander
from .filter import Classification, PackageFilter
from .filter import PackageLabelling
from .arch import *
from .compose import Composer
from .policy import Team

class MonkeyConfigLoader(object):
	def __init__(self):
		pass

	class DefiningFile(object):
		def __init__(self, filename):
			self.name = os.path.basename(filename)
			self._sequence = 0
			assert(filename)
			assert(self.name)

		def __str__(self):
			return self.name

		# in the absence of line number information from the yaml module, we
		# just assign a new sequence number when we encounter an epic.
		def newLocation(self):
			self._sequence += 1
			return MonkeyConfigLoader.DefiningFileLocation(self.name, self._sequence)

	class DefiningFileLocation(DefiningFile):
		def __init__(self, name, sequence):
			self.name = name
			self.sequence = sequence

		def __str__(self):
			return f"{self}, sequence {self.sequence}"

		@property
		def key(self):
			return (self.name, self.sequence)

	class Context(object):
		def __init__(self, name = None, filename = None, parent = None, expander = None, policy = None):
			self.name = name or filename
			self._filename = filename
			self.parent = parent

			if parent is not None and expander is None:
				expander = parent.expander
			self.expander = expander

			if parent is not None and policy is None:
				policy = parent.policy
			self.policy = policy

			self.defaultLayer = None
			if parent is not None:
				self.defaultLayer = parent.defaultLayer

			if filename is not None:
				self.location = FilterLoader.DefiningFile(filename)
			else:
				self.location = parent.location

			assert(self.name)

		def __str__(self):
			if self.parent is not None:
				return f"{self.parent} -> {self.name}"
			return self.name

		def descend(self, name):
			return self.__class__(name = name, parent = self)

		@property
		def filename(self):
			return self._filename or self.parent.filename

		def stringContext(self, key, value):
			return FilterLoader.StringContext(key, value, parent = self)

		def dictContext(self, key, value):
			if value is None:
				value = {}
			return FilterLoader.DictContext(key, value, parent = self)

		def listContext(self, key, value):
			if value is None:
				value = []
			return FilterLoader.ListContext(key, value, parent = self)

		def stringListContext(self, key, value):
			value = self.asStringList(key, value)
			return FilterLoader.ListContext(key, value, parent = self)

		def asString(self, key, value):
			if value is None:
				return None
			if type(value) in (int, ):
				value = str(value)
			if type(value) != str:
				raise Exception(f"{self}: {key} should be a str not a {type(value)}")
			return value

		def asBoolean(self, key, value):
			if value is None:
				return None
			if type(value) != bool:
				raise Exception(f"{self}: {key} should be a bool not a {type(value)}")
			return value

		def asInt(self, key, value):
			if value is None:
				return None
			if type(value) != int:
				raise Exception(f"{self}: {key} should be a int not a {type(value)}")
			return value

		def asDate(self, key, value):
			if value is None:
				return None
			if type(value) is datetime.date:
				return value
			if type(value) is str:
				return time.strtptime(value, "%Y-%m-%d")
			raise Exception(f"{self}: {key} should be a date not a {type(value)}")

		def asStringList(self, key, value):
			if value is None:
				return []
			if type(value) != list:
				raise Exception(f"{self}: {key} should be a list not a {type(value)}")
			if not all(type(i) is str for i in value):
				raise Exception(f"{self}: {key} should be a list of strings, but some values have an unexpected type")
			return value

		def asStringDict(self, key, value):
			if value is None:
				return {}
			if type(value) != dict:
				raise Exception(f"{self}: {key} should be a dict not a {type(value)}")

			if not all(type(i) is str for i in value.values()):
				for i in value.values():
					if type(i) != str:
						errormsg(f"{self} -> key: bad value in {key}={i} (type {type(i)})")
				raise Exception(f"{self}: {key} should be a dict of strings, but some values have an unexpected type")
			return value

		def asRelease(self, key, value):
			releaseID = self.asString(key, value)

			release = self.policy.getRelease(releaseID)
			if release is None:
				raise Exception(f"{self}: {key} specifies unknown release ID {releaseID}")

			return release

		def variableExpansion(self, data):
			if not self.expander or data is None:
				return data

			dataType = type(data)
			if dataType in (int, bool, float):
				return data
			if dataType is str:
				return self.expander.expand(data)
			if dataType is dict:
				return dict((self.expander.expand(key), self.variableExpansion(value)) for (key, value) in data.items())
			if dataType is list:
				return list(map(self.variableExpansion, data))
			if dataType is datetime.date:
				# yaml is nuts
				return data

			raise Exception(f"{self}: unexpected YAML data {dataType} in variableExpansion")

	class DataContext(Context):
		def __init__(self, expectedType, name, value, *args, **kwargs):
			super().__init__(name, *args, **kwargs)
			self.value = value

			if expectedType and type(value) != expectedType:
				raise Exception(f"{self} expected a {expectedType.__name__} not a {type(value)}")

	class StringContext(DataContext):
		def __init__(self, *args, **kwargs):
			super().__init__(str, *args, **kwargs)

		def __str__(self):
			return self.value

	class DictContext(DataContext):
		def __init__(self, *args, **kwargs):
			super().__init__(dict, *args, **kwargs)

		def items(self):
			return self.value.items()

		def __contains__(self, key):
			return key in self.value

		def keys(self):
			return self.value.keys()

		def get(self, key):
			return self.value.get(key)

		def popBoolean(self, key):
			value = self.value.pop(key, None)
			if value is not None:
				value = self.asBoolean(key, value)
			return value

		def popDict(self, key):
			value = self.value.pop(key, None)
			if value is not None:
				value = self.dictContext(key, value)
			return value

	class ListContext(DataContext):
		def __init__(self, *args, **kwargs):
			super().__init__(list, *args, **kwargs)

		def __iter__(self):
			return iter(self.value)

	class Processor(object):
		def __init__(self, context):
			self.context = context

		# The default processing function for a dict will just call
		# processKeyValue() for every dict item
		def process(self, data):
			for key, value in data.items():
				self.processKeyValue(key, value)
			self.processingComplete()

		def processKeyValue(self, key, value):
			raise Exception(f"{self.context}: unsupported keyword {key}")

		def processingComplete(self):
			pass

class FilterLoader(MonkeyConfigLoader):
	class Processor(MonkeyConfigLoader.Processor):
		def __init__(self, packageFilter, context, settings):
			super().__init__(context)
			self.packageFilter = packageFilter
			self.classificationScheme = packageFilter.classificationScheme
			self.settings = settings

			self.labelHints = None

		def createLabelHints(self, label, **kwargs):
			return Classification.LabelHints(self.labelHints, label = label, **kwargs)

		def processLabel(self, label, data, **kwargs):
			labelHints = self.createLabelHints(label)
			return self.processLabelWithHints(labelHints, data, **kwargs)

		def processLabelWithHints(self, labelHints, data, processorFactory = None):
			label = labelHints.label

			# infomsg(f"{self.context}: processLabelWithHints {label}")

			# In yaml, it's legal to describe an empty dict - which
			# may result in data == None
			if data is None:
				data = {}

			if type(data) is not dict:
				raise Exception(f"{label}: data should be a dict")

			priority = data.get('priority')
			if priority is not None:
				labelHints.priority = self.context.asInt(label.name, priority)

			if processorFactory is None:
				processorFactory = FilterLoader.LabelProcessor

			labelProcessor = processorFactory(labelHints, self)
			labelProcessor.process(data)

		def definePromise(self, name):
			self.packageFilter.definePromise(name)

		# Should this be Context.asMonthPeriod()?
		def parseTimeValueUnit(self, key, value):
			try:
				number, unit = value.split()

				if unit.startswith('month'):
					return int(number)
				if unit.startswith('year'):
					return int(12 * float(number))
			except:
				pass

			raise Exception(f"Unable to parse time specification {key}=\"{value}\"")

		# Parse a realname + mail address. Should be good enough even if it's not RFC822.
		def parsePersonOrTeam(self, key, value, agent = None):
			value = self.context.asString(key, value)
			if value.startswith('team_'):
				return value

			mailAddress = None

			words = value.split()
			for token in words:
				if '@' not in token:
					continue

				if mailAddress is not None:
					raise Exception(f"Duplicate email address in {key}=\"{value}\"")
				mailAddress = token

			if mailAddress is None:
				raise Exception(f"Expect fullname and email address in {key}=\"{value}\"")

			fullName = value.replace(mailAddress, ' ')
			fullName = fullName.replace('"', '')
			fullName = ' '.join(fullName.split())

			mailAddress = mailAddress.strip("<>")

			if agent is None:
				id = f"user_{mailAddress.split('@')[0].replace('-', '_')}"
				if '_maintainers' in id:
					warnmsg(f"Found {key} {mailAddress}. Please consider defining a team for this")

				agent = self.context.policy.createAgent(id)

			agent.update(fullName, mailAddress)
			return agent.id

	class CommonFileProcessor(Processor):
		def processCommonFileDirective(self, key, value):
			raise Exception(f"{self.context}: unsupported keyword {key}")

		def processInclude(self, includeFile):
			import os

			# infomsg(f"{self.context}: including {includeFile}")
			referencingFile = self.context.filename

			includeBaseDir = os.path.dirname(referencingFile)
			if includeBaseDir:
				includeFile = os.path.join(includeBaseDir, includeFile)

			with open(includeFile) as f:
				data = yaml.full_load(f)

			if not data:
				errormsg(f"{includeFile} seems to be empty")
				return

			# recursively expand all ${variables}
			data = self.context.variableExpansion(data)

			includeProcessor = FilterLoader.IncludeFileProcessor(includeFile, self)
			try:
				includeProcessor.process(data)
			except Exception as e:
				prefix = f"{self.context} -> {includeFile}"
				msg = str(e)
				if not msg.startswith(prefix):
					msg = f"{prefix}: {msg}"
				raise Exception(msg)

		def processKeyValue(self, key, value):
			if key == 'epics':
				context = self.context.dictContext(key, value)
				for epicName, epicData in context.items():
					self.processEpicNew(epicName, epicData)
			elif key == 'layers':
				context = self.context.dictContext(key, value)
				for layerName, layerData in context.items():
					self.processLayer(layerName, layerData)
			elif key == 'policy_defaults':
				self.processPolicyDefaults(self.context.dictContext(key, value))
			elif key == 'default_layer':
				layer = self.packageFilter.defineLayer(self.context.asString(key, value))
				self.context.defaultLayer = layer
			elif key == 'reviewers':
				self.processMaintainers(self.context.dictContext(key, value))
			elif key == 'lifecycles':
				self.processLifeCycles(self.context.dictContext(key, value))
			elif key == 'releases':
				self.processReleases(self.context.dictContext(key, value))
			else:
				super().processKeyValue(key, value)

		def processPolicyDefaults(self, data):
			# infomsg(f"processPolicyDefaults({self.settings.scope})")
			processor = FilterLoader.PolicyProcessor(self)
			processor.process(data)

		def processMaintainers(self, d):
			for id, data in d.items():
				team = self.packageFilter.createTeam(id)

				if type(data) is str:
					self.parsePersonOrTeam(id, data, team)
					continue

				processor = FilterLoader.TeamProcessor(team, self)
				processor.process(self.context.asStringDict(id, data))

		def processLifeCycles(self, d):
			for id, data in d.items():
				lifecycle = self.packageFilter.createLifeCycle(id)

				baseId = data.pop('inherit', None)
				if baseId is not None:
					baseLifecycle = self.packageFilter.getLifeCycle(baseId)
					if baseLifecycle is None:
						raise Exception(f"Bad definition of lifecycle {id}: cannot inherit from {baseId} - not defined")
					lifecycle.inherits = baseLifecycle

				baseId = data.pop('implement', None)
				if baseId is not None:
					baseLifecycle = self.packageFilter.getLifeCycle(baseId)
					if baseLifecycle is None:
						raise Exception(f"Bad definition of lifecycle {id}: cannot implement {baseId} - not defined")
					lifecycle.implements = baseLifecycle

				processor = FilterLoader.LifeCycleProcessor(lifecycle, self)
				processor.process(self.context.dictContext(id, data))

		def processReleases(self, d):
			for id, data in d.items():
				release = self.context.policy.createRelease(id)

				processor = FilterLoader.ReleaseProcessor(release, self)
				processor.process(self.context.dictContext(id, data))

		def processEpicNew(self, labelName, data):
			epic = self.packageFilter.defineEpic(labelName)

			# apply the per-file default policy settings to all epics defined in that file
			self.packageFilter.setEpicPolicyDefaults(epic, self.settings)

			if 'layer' in data:
				id = data.pop('layer')
				epic.setLayer(self.packageFilter.defineLayer(id))
			elif self.context.defaultLayer is not None:
				# If the context specifies a default layer, assign our new epic to this layer
				epic.setLayer(self.context.defaultLayer)
			else:
				raise Exception(f"Trying to define epic {epic} without layer");

			if 'default_disposition' in data:
				id = data.pop('default_disposition')
				epic.defaultDisposition = self.packageFilter.defineDisposition(id)

			labelHints = self.createLabelHints(epic, layer = epic.layer, epic = epic)

			self.processLabelWithHints(labelHints, data, FilterLoader.EpicProcessor)

		def processLayer(self, labelName, data):
			layer = self.packageFilter.defineLayer(labelName)

			labelHints = self.createLabelHints(layer, layer = layer)
			self.processLabelWithHints(labelHints, data, processorFactory = FilterLoader.LayerProcessor)

	class MainFileProcessor(CommonFileProcessor):
		def __init__(self, packageFilter, filename):
			context = FilterLoader.Context(filename = filename,
						expander = VariableExpander(),
						policy = packageFilter.policy)

			super().__init__(packageFilter, context, packageFilter.globalPolicySettings)

			# FIXME: the expander should be internal to Context
			packageFilter.expander = self.context.expander

		def process(self, data):
			for key, value in data.items():
				if key == 'defines':
					d = self.context.asStringDict(key, value)
					for varName, varValue in d.items():
						self.context.expander.update(varName, varValue)
					continue

				# recursively expand all ${variables}
				value = self.context.variableExpansion(value)

				if key == 'classes':
					self.processClasses(self.context.dictContext(key, value))
				elif key == 'roles':
					context = self.context.dictContext(key, value)
					self.processRoles(context)
				elif key == 'include':
					nameList = self.context.asStringList(key, value)
					for name in nameList:
						self.processInclude(name)
				elif key == 'promises':
					for name in self.context.asStringList(key, value):
						self.definePromise(name)
				else:
					self.processKeyValue(key, value)

		def processClasses(self, context):
			for name, data in context.items():
				label = self.packageFilter.defineLabel(name, Classification.TYPE_CLASS)
				if data is not None:
					labelHints = self.createLabelHints(label, klass = label)
					self.processLabelWithHints(labelHints, data)

		def processRoles(self, context):
			for roleName, roleData in context.items():
				klassName = roleData.get('class')
				optionName = roleData.get('option')
				if klassName and optionName:
					raise Exception(f"role {roleName}: you cannot specify class and option at the same time")

				if klassName is not None:
					klass = self.packageFilter.getTopicClass(klassName)
					labelHints = self.createLabelHints(klass, klass = klass)
				elif optionName is not None:
					buildOption = self.packageFilter.defineOption(optionName)
					labelHints = self.createLabelHints(buildOption, buildOption = buildOption)
				else:
					labelHints = self.createLabelHints(None)

				roleProcessor = FilterLoader.RoleProcessor(roleName, labelHints, self)
				roleProcessor.process(roleData)

	class IncludeFileProcessor(CommonFileProcessor):
		def __init__(self, filename, parent):
			context = FilterLoader.Context(filename = filename, parent = parent.context)
			clonedSettings = parent.settings.clone(filename)
			super().__init__(parent.packageFilter, context, clonedSettings)

	class TeamProcessor(Processor):
		def __init__(self, team, parent):
			super().__init__(parent.packageFilter, parent.context, parent.settings)
			self.team = team

		def processKeyValue(self, key, value):
			if key == 'full_name':
				self.team.fullName = self.context.asString(key, value)
			elif key == 'email':
				self.team.email = self.context.asString(key, value)
			else:
				super().processKeyValue(key, value)

	class LifeCycleProcessor(Processor):
		def __init__(self, lifecycle, parent):
			super().__init__(parent.packageFilter, parent.context, parent.settings)
			self.lifecycle = lifecycle

		def processKeyValue(self, key, value):
			lifecycle = self.lifecycle

			# check if the key is something like "lts"
			contract = lifecycle.getContract(key)
			if contract is not None:
				data = self.context.dictContext(key, value)
				for key, value in data.items():
					self.updateContract(contract, key, value)
				return

			if key == 'description':
				lifecycle.description = self.context.asString(key, value)
			elif key == 'url':
				lifecycle.url = self.context.asString(key, value)
			elif key == 'mode':
				if value == 'sequential':
					lifecycle.mode = lifecycle.MODE_SEQUENTIAL
				elif value == 'versioned':
					lifecycle.mode = lifecycle.MODE_VERSIONED
				else:
					raise Exception(f"Unsupported life cycle {key} \"{value}\"")
			elif key == 'stability':
				lifecycle.stability = self.context.asString(key, value)
			elif key == 'releasedate':
				lifecycle.releaseDate = self.context.asDate(key, value)
			elif key == 'first_release':
				release = self.context.asRelease(key, value)
				lifecycle.releaseDate = release.date
			elif key == 'last_release':
				lifecycle.lastRelease = self.context.asRelease(key, value)
			elif key == 'cadence':
				lifecycle.cadence = self.context.asString(key, value)
			else:
				super().processKeyValue(key, value)

		def processingComplete(self):
			self.lifecycle.finalize()

		def updateContract(self, contract, key, value):
			if key == 'cadence':
				contract.cadence = self.parseCadence(key, value)
			elif key == 'stability':
				contract.stability = self.context.asString(key, value)
				# FIXME: make sure stability is valid
			elif key == 'duration':
				contract.duration = self.parseDuration(key, value)
			elif key == 'concurrent_versions':
				contract.concurrentVersions = self.context.asInt(key, value)
			elif key == 'enabled':
				contract.enabled = self.context.asBoolean(key, value)
			elif key == 'eol':
				contract.endOfSupport = self.context.asDate(key, value)
			else:
				raise Exception(f"Unsupported settings in {self.lifecycle}.{contract}: {key}={value}")

		def parseCadence(self, key, value):
			if value == 'minor_release':
				return self.lifecycle.CADENCE_MINOR_RELEASE
			return self.parseTimeValueUnit(key, value)

		def parseDuration(self, key, value):
			if value == 'minor_release':
				return self.lifecycle.DURATION_MINOR_RELEASE
			return self.parseTimeValueUnit(key, value)

	class PolicyProcessor(Processor):
		def __init__(self, parent):
			super().__init__(parent.packageFilter, parent.context, parent.settings)
			self.settings = parent.settings

		def processKeyValue(self, key, value):
			if key in ('maintainer', 'reviewer'):
				self.settings.maintainer = self.parsePersonOrTeam(key, value)
			elif key == 'lifecycle':
				# For now, just the id
				self.settings.defaultLifecycle = self.context.asString(key, value)
			elif key == 'support':
				# For now, just the id
				self.settings.defaultSupport = self.context.asString(key, value)
			elif key == 'contracts':
				self.processContracts(self.context.dictContext(key, value))
			elif key == 'default_disposition':
				disposition = self.packageFilter.defineDisposition(self.context.asString(key, value))
				self.settings.defaultDisposition = disposition
			else:
				super().processKeyValue(key, value)

		# We put the list of defined contracts under policy_defaults, but they
		# really exist globally.
		def processContracts(self, context):
			for key, value in context.items():
				contractDef = self.packageFilter.createContract(key)
				contractDef.enabled = True
				self.processContractSettings(contractDef, context.dictContext(key, value))

		def processContractSettings(self, contractDef, context):
			for cKey, cValue in context.items():
				if cKey == 'name':
					contractDef.name = context.asString(cKey, cValue)
				elif cKey == 'enabled':
					contractDef.enabled = context.asBoolean(cKey, cValue)
				elif cKey == 'base':
					contractDef.baseContract = context.asString(cKey, cValue)
				elif cKey == 'stability':
					contractDef.stability = context.asString(cKey, cValue)
				else:
					raise Exception(f"{context}: unsupported {cKey}={cValue}")

	class ReleaseProcessor(Processor):
		def __init__(self, release, parent):
			super().__init__(parent.packageFilter, parent.context, parent.settings)
			self.release = release

		def processingComplete(self):
			release = self.release

			for attr in ('major', 'minor', 'date'):
				if getattr(release, attr) is None:
					raise Exception(f"Invalid release definition \"{release}\": missing value {attr}")

			for contract in release.contracts:
				contract.computeEndDate(release.date)

		def processKeyValue(self, key, value):
			if key == 'major':
				self.release.major = self.context.asString(key, value)
			elif key == 'minor':
				self.release.minor = self.context.asString(key, value)
			elif key == 'date':
				self.release.date = self.context.asDate(key, value)
			elif key == 'lifecycle':
				self.release.lifecycle = self.context.asString(key, value)
			elif key == 'ticktock':
				self.release.ticktock = self.context.asString(key, value)
			elif key == 'contracts':
				self.processContracts(self.context.dictContext(key, value))
			else:
				super().processKeyValue(key, value)

		def processContracts(self, context):
			release = self.release
			for key, value in context.items():
				contract = release.getContract(key)
				if contract is None:
					raise Exception(f"Release {release}: unknown contract {key}")

				self.processContractSettings(contract, context.dictContext(key, value))

		def processContractSettings(self, contract, context):
			for key, value in context.items():
				if key == 'duration':
					contract.duration = self.parseTimeValueUnit(key, value)
				elif key == 'enabled':
					contract.enabled = context.asBoolean(key, value)
				else:
					raise Exception(f"{context}: unsupported {key}={value}")

	class RoleProcessor(Processor):
		def __init__(self, name, labelHints, parent):
			super().__init__(parent.packageFilter, FilterLoader.Context(name, parent = parent.context), parent.settings)

			self.name = name
			self.labelHints = labelHints

			self.stringMatcher = self.packageFilter.stringMatcher

		def process(self, data):
			labelName = None
			labelType = None
			suffixList = []
			prefixList = []
			binaryList = []

			labelHints = self.labelHints
			labelHints.priority = 6

			for fieldName, fieldValue in data.items():
				if fieldName == 'class':
					assert(type(fieldValue) is str)
					labelName = fieldValue
					labelType = Classification.TYPE_CLASS
				elif fieldName == 'option':
					assert(type(fieldValue) is str)
					labelName = fieldValue
					labelType = Classification.TYPE_AUTOFLAVOR
				elif fieldName == 'priority':
					labelHints.priority = int(fieldValue)
				elif fieldName == 'packagesuffixes':
					suffixList = fieldValue
				elif fieldName == 'packageprefixes':
					prefixList = fieldValue
				elif fieldName in ('binaries', 'rpms'):
					binaryList = fieldValue
				else:
					raise Exception(f"Bad field '{fieldName}' in definition of role {self.name}")

			if labelName is None and labelHints.priority < 10:
				raise Exception(f"Definition of role {self.name} does not specify a label but has priority != 10")

			for suffix in suffixList:
				self.addRoleMatch(f"*-{suffix}", labelHints)
			for prefix in prefixList:
				self.addRoleMatch(f"{prefix}-*", labelHints)

			for pattern in binaryList:
				labelHints = self.labelHints

				words = pattern.split()
				if len(words) > 1:
					pattern = words.pop(0)
					while words:
						w = words.pop(0)
						if '=' not in w:
							raise Exception(f"bad modifier syntax in definition of {role}: {w} (expecting key=value)")
						key, value = w.split('=')
						if key == 'priority':
							labelHints = self.labelHints.clone(priority = int(value))
						else:
							raise Exception(f"unsupported modifier in definition of {role}: {w}")

				self.addRoleMatch(pattern, labelHints)

		def addRoleMatch(self, pattern, labelHints):
			m = self.stringMatcher.createRoleMatch(pattern, labelHints)
			if m.parameters:
				raise Exception(f"parameters not allowed in definition of pattern match \"{pattern}\" for role {role}")

	class LabelProcessorBase(Processor):
		def __init__(self, labelHints, parent):
			assert(isinstance(labelHints, Classification.LabelHints))
			label = labelHints.label

			super().__init__(parent.packageFilter, FilterLoader.Context(str(label), parent = parent.context), parent.settings)

			self.stringMatcher = self.packageFilter.stringMatcher

			self.label = label
			self.labelHints = labelHints
			self.priority = labelHints.priority

			location = self.context.location
			if location is None or location.name is None:
				raise Exception(f"{self.context}: no file location when defining {label}")
			label.definingLocation = location.newLocation()

			# for the time being, do not protect against redefining a label
			label.defined = True

		def updateStringAttribute(self, key, value, attr_name = None):
			value = self.context.asString(key, value)
			if value is not None:
				setattr(self.label, attr_name or key, value)

		def handlePragma(self, words):
			for kwd in words:
				if kwd == 'api':
					if self.label.type is not Classification.TYPE_CLASS:
						raise Exception(f"Only class labels can be marked as '{kwd}'")
					# quietly ignore this pragma for now
				elif kwd == 'ignore':
					self.label.isIgnored = True
				elif kwd == 'compiler':
					if self.label.type is not Classification.TYPE_EPIC:
						raise Exception(f"Only epic labels can be marked as '{kwd}'")
					# quietly ignore this pragma for now
				elif kwd == 'unresolvable':
					if self.label.type is not Classification.TYPE_CLASS:
						raise Exception(f"Only class labels can be marked as '{kwd}'")
					self.packageFilter.setUnresolvableClass(self.label)
				elif kwd == 'no-default-requires':
					# quietly ignore this pragma for now
					pass
				else:
					warnmsg(f"{self.context}: ignoring unsupported pragma \"{kwd}\"")

		def updateBooleanAttribute(self, key, value, attr_name = None):
			value = self.context.asBoolean(key, value)
			if value is not None:
				setattr(self.label, attr_name or key, value)

		def processKeyValue(self, key, value):
			if key == 'name':
				# we have already handled this
				return
			if key in ('description', 'compatibility', ):
				self.updateStringAttribute(key, value)
			elif key == 'requires':
				self.processRequires(self.context.asStringList(key, value))
			elif key == 'requires_options':
				self.processRequiresOptions(self.context.asStringList(key, value))
			elif key == 'architectures':
				self.processArchitectures(self.context.asStringList(key, value))
			elif key == 'exclude_architectures':
				self.processExcludeArchitectures(self.context.asStringList(key, value))
			elif key == 'pragma':
				value = self.context.asString(key, value)
				self.handlePragma(value.split())
			else:
				super().processKeyValue(key, value)

		def processRequires(self, nameList):
			if self.label.type is Classification.TYPE_EPIC:
				for name in nameList:
					self.packageFilter.addLateRequiredEpicBinding(self.label, name, self.context)
			elif self.label.type is Classification.TYPE_CLASS:
				for name in nameList:
					self.packageFilter.addLateRequiredClassBinding(self.label, name, self.context)
			else:
				raise Exception(f"{self.context}: requires only valid in epic or class context, not for label {self.label.describe()}")

		def processRequiresOptions(self, nameList):
			for name in nameList:
				if self.label.type is Classification.TYPE_AUTOFLAVOR:
					# the mapping between build options and their corresponding flavor(s) must be
					# established immediately.
					buildOption = self.packageFilter.defineOption(name)
					self.label.addBuildOptionDependency(buildOption)
				else:
					self.packageFilter.addLateRequiredOptionBinding(self.label, name, self.context)

		def processArchitectures(self, data):
			archSet = ArchSet(data)
			if not self.label.restrictArchitectures(archSet):
				errormsg(f"{self.label}: ignoring architecture specification")

		def processExcludeArchitectures(self, data):
			archSet = archRegistry.fullset.difference(ArchSet(data))
			if not self.label.restrictArchitectures(archSet):
				errormsg(f"{self.label}: ignoring exclude_architecture specification")
			for arch in data:
				assert(arch not in self.label._archSet)

		def addBinaryPackageFilter(self, pattern):
			if pattern.startswith('promise:') and '?' not in pattern and '*' not in pattern:
				name = pattern[8:]
				self.definePromise(name)

			self.packageFilter.addLateRpmFilterRuleBinding(pattern, self.labelHints)

		def addOBSPackageFilter(self, pattern):
			self.packageFilter.addLateBuildFilterRuleBinding(pattern, self.labelHints)

	class LabelProcessor(LabelProcessorBase):
		def __init__(self, labelHints, parent):
			super().__init__(labelHints, parent)

		def processKeyValue(self, key, value):
			if key == 'enabled':
				self.updateBooleanAttribute(key, value, 'isEnabled')
			elif key == 'priority':
				assert(self.priority == self.context.asInt(key, value))
			else:
				super().processKeyValue(key, value)

		def processPackages(self, nameList):
			for name in nameList:
				self.addOBSPackageFilter(name)

	class LayerProcessor(LabelProcessor):
		def processKeyValue(self, key, value):
			if key == 'requires':
				self.processRequires(self.context.asStringList(key, value))
			else:
				super().processKeyValue(key, value)

		def processRequires(self, nameList):
			assert(self.label.type is Classification.TYPE_LAYER)

			for name in nameList:
				self.packageFilter.addLateRequiredLayerBinding(self.label, name, self.context)

	class TopicScopeProcessor(LabelProcessorBase):
		def processKeyValue(self, key, value):
			# FIXME: rename to 'builds'
			if key == 'packages':
				self.processPackages(self.context.asStringList(key, value))
			elif key == 'hints':
				self.processHints(self.context.asStringList(key, value))
			else:
				super().processKeyValue(key, value)

		def processPackages(self, nameList):
			for name in nameList:
				self.addOBSPackageFilter(name)

		def processHints(self, data):
			for pattern in data:
				if pattern.startswith('promise:') and '?' not in pattern and '*' not in pattern:
					name = pattern.split()[0]
					self.definePromise(name[8:])

				self.packageFilter.addLateHintsFilterRuleBinding(pattern, self.labelHints)

	class OptionProcessor(TopicScopeProcessor):
		def __init__(self, labelHints, parent):
			super().__init__(labelHints, parent)

			self.epic = self.label.epic
			self.buildOption = self.label.definingBuildOption

			assert(self.buildOption)

		def processRequires(self, nameList):
			label = self.label

			# Requirements at the build option level should always be epic labels,
			# and they should be attached to the build option itself rather than the
			# $optionTopic label.
			buildOption = label.definingBuildOption
			assert(buildOption.mainTopic is label)

			for name in nameList:
				self.packageFilter.addLateRequiredEpicBinding(buildOption, name, self.context)

	class EpicProcessor(TopicScopeProcessor):
		def __init__(self, labelHints, parent):
			super().__init__(labelHints, parent)

		def processKeyValue(self, key, value):
			if key == 'options':
				context = self.context.dictContext(key, value)
				self.processNewSubset(context, Classification.TYPE_BUILD_OPTION)
			elif key == 'extras':
				context = self.context.dictContext(key, value)
				self.processNewSubset(context, Classification.TYPE_AUTOFLAVOR)
			elif key in ('enable_options', 'allow_options'):
				warnmsg(f"{self.context}: ignore obsolete {key}")
			elif key == 'layer':
				self.processLayer(self.context.asString(key, value))
			elif key == 'default_disposition':
				# already processed
				pass
			elif key == 'rpms':
				raise Exception(f"obsolete: use _class/rpms: instead of the other way around")
			elif key == 'lifecycle':
				self.label.lifecycleID = self.context.asString(key, value)
			elif key in ('maintainer', 'reviewer'):
				self.label.maintainerID = self.parsePersonOrTeam(key, value)
			elif key == 'support':
				self.label.supportID = self.context.asString(key, value)
			elif key == 'decisionlog':
				self.processDecisionLog(self.context.asString(key, value))
			elif key == 'implement_scenario':
				self.processImplementScenario(self.context.asString(key, value))
			elif key == 'releasedate':
				self.processReleaseDate(self.context.asInt(key, value))
			elif key == 'catchall':
				self.packageFilter.setCatchAllEpic(self.label)
			else:
				super().processKeyValue(key, value)

		def processNewSubset(self, context, labelType):
			for subsetName, subsetData in context.items():
				if labelType is Classification.TYPE_BUILD_OPTION:
					label = self.packageFilter.defineOption(subsetName, epic = self.label)
				elif labelType is Classification.TYPE_AUTOFLAVOR:
					label = self.packageFilter.defineEpicFlavorByName(subsetName, epic = self.label)
				else:
					raise Exception(f"processNewSubset: unsupported label type {labelType}")

				subset = self.packageFilter.defineSubset(label)

				subsetData = context.dictContext(subsetName, subsetData)

				for key, value in subsetData.items():
					if key == 'requires':
						subset.addIncludes(context.asStringList(key, value))
					elif key == 'builds':
						self.processSubsetPatterns(context.asStringList(key, value), subset.addBuildMatch)
					elif key == 'rpms':
						self.processSubsetPatterns(context.asStringList(key, value), subset.addRpmMatch)
					elif key == 'description':
						pass
					else:
						raise Exception(f"{context}: unknown attr {key} in subset definition {subsetName}")

		def processSubsetPatterns(self, patternList, addfn):
			for pattern in patternList:
				params = []
				if ' ' in pattern:
					params = pattern.split()
					pattern = params.pop(0)
				m = addfn(pattern)
				for p in params:
					if not p.startswith('class='):
						raise Exception(f"cannot parse param {p} for subset pattern {pattern}")
					klassName = p[6:]
					klass = self.packageFilter.getTopicClass(klassName)
					m.addClass(klass)

		def processLayer(self, name):
			self.packageFilter.addLateLayerBinding(self.label, name, self.context)

		def processPackages(self, nameList):
			for name in nameList:
				self.addOBSPackageFilter(name)

		def processDecisionLog(self, values):
			self.label.decisionLog.append(values)

		def processImplementScenario(self, scenarioSpec):
			name, version = scenarioSpec.split('=')
			self.packageFilter.implementScenario(self.label, name, version)

		def processReleaseDate(self, date):
			self.packageFilter.setReleaseDate(self.label, date)

	def load(self, filename = 'filter.yaml', **kwargs):
		filter = PackageFilter(**kwargs)

		mainProcessor = self.MainFileProcessor(filter, filename)

		with open(filename) as f:
			data = yaml.full_load(f)

		with TimedExecutionBlock(f"loading definition from {filename}"):
			mainProcessor.process(data)

		filter.bindAll()

		return filter

class CompositionLoader(MonkeyConfigLoader):
	class Processor(MonkeyConfigLoader.Processor):
		def __init__(self, context):
			super().__init__(context)

		def processInOrder(self, context):
			self.processInOrderMulti([context])

		def processInOrderMulti(self, contextList):
			for key in self.orderedKeys:
				for context in contextList:
					value = context.get(key)
					if value is not None:
						self.processKeyValue(key, value)

			for context in contextList:
				unknown = set(context.keys()).difference(set(self.orderedKeys))
				if unknown:
					raise Exception(f"{context.name} contains unknown entry/entries {' '.join(unknown)}")

	class MainFileProcessor(Processor):
		def __init__(self, composer, filename):
			super().__init__(FilterLoader.Context(filename = filename, expander = VariableExpander()))
			self.composer = composer

			self.orderedKeys = ('release', 'closure_rules', 'products', )

		def process(self, context):
			self.processInOrder(context)

		def processKeyValue(self, key, value):
			if key == 'products':
				self.processProducts(self.context.dictContext(key, value))
			elif key == 'closure_rules':
				self.processClosureRules(self.context.dictContext(key, value))
			elif key == 'release':
				self.composer.release = self.context.asString(key, value)
			else:
				super().processKeyValue(key, value)

		def processClosureRules(self, context):
			for key, value in context.items():
				rules = self.composer.createClosureRule(key)

				processor = CompositionLoader.ClosureRuleProcessor(self.composer, rules, self)
				processor.process(context.dictContext(key, value))

		def buildSettingStack(self, *args):
			return list(filter(bool, args))

		def processProducts(self, context):
			from .compose import ProductComposition

			typeBaseProduct = ProductComposition.TYPE_BASEPRODUCT
			typeExtension = ProductComposition.TYPE_EXTENSION

			defaultSettings = context.popDict('defaults')

			derivedProductSettings = []

			# First pass: process base product(s)
			for key, value in context.items():
				productSettings = self.context.dictContext(key, value)
				if 'extend' in productSettings or 'copy' in productSettings:
					derivedProductSettings.append(productSettings)
					continue

				self.processOneProduct(typeBaseProduct,
						self.buildSettingStack(defaultSettings, productSettings))

			for productSettings in derivedProductSettings:
				baseProductName = productSettings.get('extend')
				if baseProductName:
					type = typeExtension
				else:
					baseProductName = productSettings.get('copy')
					assert(baseProductName is not None)
					type = typeBaseProduct

				if self.composer.lookupProduct(baseProductName) is None:
					raise Exception(f"Invalid definition of product {productSettings.name}: unknown base product {baseProductName}")

				baseSettings = context.get(baseProductName)

				self.processOneProduct(type,
						self.buildSettingStack(defaultSettings, baseSettings, productSettings),
						baseProductName)

		def processOneProduct(self, type, settingsStack, baseProductName = None):
			productName = settingsStack[-1].name

			product = self.composer.createProduct(productName)
			product.type = type
			product.baseProductName = baseProductName

			processor = CompositionLoader.ProductProcessor(product, self)
			processor.processInOrderMulti(settingsStack)

			return product

	class ClosureRuleProcessor(MonkeyConfigLoader.Processor):
		def __init__(self, composer, rules, parent):
			context = FilterLoader.Context(f"closure rules {rules}", parent = parent.context)
			super().__init__(context)
			self.composer = composer
			self.rules = rules

		def processKeyValue(self, key, value):
			rules = self.rules

			if key == 'request_classes':
				self.processRequestClasses(self.context.asStringList(key, value))
			elif key == 'complement_classes':
				self.processComplementClasses(self.context.dictContext(key, value))
			else:
				super().processKeyValue(key, value)

		def processRequestClasses(self, nameList):
			for className in nameList:
				classLabel = self.composer.castToLabel(className, Classification.TYPE_CLASS)
				self.rules.requestClass(classLabel)

		def processComplementClasses(self, context):
			for key, value in context.items():
				sourceClass = self.composer.castToLabel(key, Classification.TYPE_CLASS)
				for name in context.asStringList(key, value):
					targetClass = self.composer.castToLabel(name, Classification.TYPE_CLASS)
					self.rules.createClassRule(targetClass).add(sourceClass)

	class ProductProcessor(Processor):
		def __init__(self, product, parent):
			context = FilterLoader.Context(f"product {product}", parent = parent.context)
			super().__init__(context)
			self.product = product

			self.orderedKeys = ('extend', 'copy',
						'architectures', 'name', 'contracts',
						'obs_composekey',
						'releasepkg',
						'release_epic',
						'override_rpms',
						'classes',
						'layers',
						'epics',
						'options')

		def processKeyValue(self, key, value):
			product = self.product

			if key in ('extend', 'copy'):
				return

			if key == 'name':
				product.name = self.context.asString(key, value)
			elif key == 'contracts':
				product.contractNames = self.context.asStringList(key, value)
			elif key == 'obs_composekey':
				product.obsComposeKey = self.context.asString(key, value)
			elif key == 'releasepkg':
				product.releasePackage = self.context.asString(key, value)
			elif key == 'release_epic':
				product.releaseEpic = self.context.asString(key, value)
			elif key == 'override_rpms':
				self.processOverrideRpms(self.context.dictContext(key, value))
			elif key == 'architectures':
				product.architectures = self.context.asStringList(key, value)
			elif self.tryProcessRules(key, value, self.product):
				pass
			else:
				super().processKeyValue(key, value)

		def tryProcessRules(self, key, value, container):
			# Try to handle things like:
			#  class.api: support=l3
			#  extra.graphviz: include
			#
			if '.' in key:
				scope, rest = key.split('.', maxsplit = 1)
				ruleCollection = container.getRuleCollection(scope)
				if ruleCollection is None:
					return False

				self.processOneRule(ruleCollection, rest, value)
			elif key == 'architectures':
				container.setArchitectures(self.context.asStringList(key, value))
			elif key == 'closure':
				container.setClosureRules(self.context.asString(key, value))
			else:
				ruleCollection = container.getRuleCollection(key)
				if ruleCollection is None:
					return False

				self.processRules(self.context.dictContext(key, value), ruleCollection)
			return True

		def processRules(self, context, ruleCollection):
			for key, value in context.items():
				if value is None:
					raise Exception(f"{context}: empty dict element {key}")
				self.processOneRule(ruleCollection, key, value)

		def processOneRule(self, ruleCollection, key, value):
			# this raises an exception if the name is not known
			rule = ruleCollection.getRuleByName(key)

			if type(value) is dict:
				if not hasattr(rule, 'getRuleCollection'):
					raise Exception(f"{rule}: cannot handle dict {key}={value} in this context ({type(rule)})")

				for attr, attrValues in value.items():
					if attr == 'self':
						self.updatePolicyFromString(rule, attrValues)
						continue

					# attrData = self.context.dictContext(attr, attrValues)
					if not self.tryProcessRules(attr, attrValues, rule):
						raise Exception(f"{rule}: cannot handle {attr}={attrValues}")

				return

			self.updatePolicyFromString(rule, value)

		def updatePolicyFromString(self, policy, value):
			for word in value.split():
				if '=' in word:
					attrName, attrValue = word.split('=')

					if attrName == 'support':
						policy.setSupportLevel(attrValue)
					else:
						raise Exception(f"invalid policy attr {attrName}")
				elif word == 'include':
					policy.include()
				elif word == 'exclude':
					policy.exclude()
				elif word == 'asneeded':
					policy.resetDecision()
				else:
					raise Exception(f"invalid policy attr {word}")

		def processOverrideRpms(self, context):
			for key, value in context.items():
				if key == 'include':
					self.product.overrideRpmInclude(context.listContext(key, value))
				elif key == 'exclude':
					self.product.overrideRpmExclude(context.listContext(key, value))
				else:
					raise Exception(f"{context}: unsupported keyword {key}")

	def load(self, composer, filename, **kwargs):
		mainProcessor = self.MainFileProcessor(composer, filename)

		with open(filename) as f:
			data = yaml.full_load(f)

		with TimedExecutionBlock(f"loading product composition from {filename}"):
			mainProcessor.process(data)
