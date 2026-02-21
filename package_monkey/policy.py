##################################################################
#
# Policy objects: maintainers, life cycle and support level
#
##################################################################

import datetime
from .util import infomsg, warnmsg, errormsg
from .util import relativeDate

__names__ = ['Policy']

class ClonableObject(object):
	def inheritFrom(self, other):
		raise NotImplementedError()

class ObjectWithAttributes(object):
	def __init__(self):
		for attrName in self.ATTRIBUTES:
			setattr(self, attrName, None)

	def update(self, key, value):
		if key not in self.ATTRIBUTES:
			raise Exception(f"{self}: unsupported setting {key}=\"{value}\"")

		infomsg(f"{self}: updating {key}={value}")
		setattr(self, key, value)

class PolicySettings(ObjectWithAttributes):
	ATTRIBUTES = (
		'maintainer',
		'maintainerName',
		'maintainerEmail',
		'defaultLifecycle',
	)

	def __init__(self, scope = 'global'):
		self.scope = scope
		super().__init__()

	def clone(self, scope):
		result = self.__class__(scope)
		for attrName in self.ATTRIBUTES:
			value = getattr(self, attrName)
			setattr(result, attrName, value)
		return result

class Release(object):
	class ReleaseContractInfo(object):
		def __init__(self, id, name = None):
			self.id = id
			self.name = name or id
			self.duration = None
			self.endOfSupport = None

		def __str__(self):
			return self.name

		def computeEndDate(self, releaseDate):
			duration = self.duration
			if duration is None:
				return

			endDate = relativeDate(releaseDate, duration, roundToEndOfMonth = True)
			if endDate is None:
				raise Exception(f"{self}: invalid duration {duration}")
			self.endOfSupport = endDate


	def __init__(self, id):
		self.id = id
		self.major = None
		self.minor = None
		self.date = None
		self.ticktock = None

		self._contracts = {}
		self.lifecycle = None

	def __str__(self):
		return self.id

	@property
	def contracts(self):
		return iter(self._contracts.values())

	def addContract(self, contractDef):
		contract = self.createContract(contractDef.id)
		# FIXME: copy duration?
		return contract

	def createContract(self, id):
		contract = self._contracts.get(id)
		if contract is None:
			contract = self.ReleaseContractInfo(id, name = f"{self}/{id}")
			self._contracts[id] = contract
		return contract

	def getContract(self, id):
		for contract in self.contracts:
			if contract.id == id:
				return contract
		return None

class Team(object):
	def __init__(self, id):
		self.id = id
		self.fullName = None
		self.email = None

	def __str__(self):
		if self.email and self.fullName:
			return f"{self.fullName} <{self.email}>"
		if self.email:
			return self.email
		if self.fullName:
			return self.fullName
		return self.id

	def update(self, fullName, email):
		if self.fullName is None:
			self.fullName = fullName
		elif fullName and self.fullName != fullName:
			errormsg(f"Conflicting fullname for {self.id}: \"{self.fullName}\" vs {fullName}")

		if self.email is None:
			self.email = email
		elif email and self.email != email:
			errormsg(f"Conflicting email for {self.id}: \"{self.email}\" vs {email}")

class ContractDefinition(object):
	def __init__(self, id):
		self.id = id
		self.name = id
		self.baseContract = None
		self.enabled = True

	def __str__(self):
		return self.id

class LifeCycle(ClonableObject):
	MODE_SEQUENTIAL	= 'sequential'
	MODE_VERSIONED	= 'versioned'

	CADENCE_MINOR_RELEASE	= 'minor_release'
	CADENCE_TICKTOCK	= 'ticktock'
	DURATION_MINOR_RELEASE	= 'minor_release'

	class Contract(ClonableObject):
		def __init__(self, id, baseContract = None):
			self.id = id
			self.name = id
			self._enabled = None
			self._cadence = None
			self._duration = None
			self._concurrentVersions = None
			# FIXME: at some point, we should probably default endOfSupport to end of minor release
			self.endOfSupport = None
			self.stability = None
			self.baseContract = baseContract

		def __str__(self):
			return self.name

		def inheritFrom(self, other):
			assert(isinstance(other, self.__class__))
			if self._enabled is None:
				self._enabled = other._enabled
			if self._cadence is None:
				self._cadence = other._cadence
			if self._duration is None:
				self._duration = other._duration
			if self._concurrentVersions is None:
				self._concurrentVersions = other._concurrentVersions
			if self.stability is None:
				self.stability = other.stability

		@property
		def enabled(self):
			if self._enabled is not None:
				return self._enabled
			if self.baseContract is not None:
				return self.baseContract.enabled

			# we only ever add a contract if the underlying contract
			# definition is enabled.
			return True

		@enabled.setter
		def enabled(self, value):
			self._enabled = value

		@property
		def concurrentVersions(self):
			if self._concurrentVersions is not None:
				return self._concurrentVersions
			if self.baseContract is not None:
				return self.baseContract.concurrentVersions
			return None

		@concurrentVersions.setter
		def concurrentVersions(self, value):
			self._concurrentVersions = value

		@property
		def cadence(self):
			if self._cadence is not None:
				return self._cadence
			if self.baseContract is not None:
				return self.baseContract.cadence
			return None

		@cadence.setter
		def cadence(self, value):
			self._cadence = value

		@property
		def duration(self):
			if self._duration is not None:
				return self._duration

			numVersions = self.concurrentVersions
			if numVersions is not None:
				cadence = self.cadence
				if cadence is not None and (type(cadence) is int or cadence.isdigit()):
					return cadence * numVersions
			return None

		@duration.setter
		def duration(self, value):
			self._duration = value

		def computeEndDate(self, releaseDate, **kwargs):
			duration = self._duration
			if duration is None or duration == LifeCycle.DURATION_MINOR_RELEASE:
				return

			endDate = relativeDate(releaseDate, duration, **kwargs)
			if endDate is None:
				raise Exception(f"{self}: invalid duration {duration}")
			self.endOfSupport = endDate

	def __init__(self, id):
		self.id = id
		self.url = None
		self.description = None
		self._mode = None
		self.stability = None
		self._cadence = None
		# FIXME: rename to defaultContract
		self.generalSupport = None
		self._releaseDate = None
		self.lastRelease = None
		self._inherit = None
		self._implement = None
		self._contracts = {}

		# For implementations of a versioned lifecycle
		self.scenarioBinding = None
		self.epic = None

		# This indicates whether we're currently in the supported period of this life
		# cycle, and whether we think we have all rpms needed:
		self.valid = True

		self.implementations = None

	def __str__(self):
		return self.id

	# Called when we've defined the life cycle
	def finalize(self):
		if self.lastRelease is not None:
			self.finalizeLastRelease()

		if self._implement is not None:
			self.finalizeImplementation()

	def finalizeLastRelease(self):
		for contract in self.contracts:
			if not contract.enabled:
				continue

			if contract.endOfSupport is not None:
				continue

			otherContract = self.lastRelease.getContract(contract.id)
			if otherContract is None:
				raise Exception(f"Lifecycle {self} specifies last release \"{self.lastRelease}\", which does not support contract {contract.id}")

			contract.duration = otherContract.duration
			contract.computeEndDate(self.lastRelease.date, roundToEndOfMonth = True)

			if contract.endOfSupport != otherContract.endOfSupport:
				raise Exception(f"{contract} ends {contract.endOfSupport} while {otherContract} ends {otherContract.endOfSupport}")

	def finalizeImplementation(self):
		other = self._implement

		# What would it mean to implement a sequential life cycle?
		assert(other.mode == self.MODE_VERSIONED)

		self._mode = self.MODE_SEQUENTIAL

		for contract in self.contracts:
			otherContract = other.getContract(contract.id)
			if otherContract is None or not otherContract.enabled:
				contract.enabled = False
				continue

			if contract.endOfSupport is not None:
				continue

			duration = otherContract.duration
			if duration is None or duration == 'minor_release':
				pass
			else:
				if type(duration) is str and duration.isdigit():
					duration = int(duration)

				if type(duration) is not int:
					raise Exception(f"{contract}: unable to handle duration={duration}")

				nversions = otherContract.concurrentVersions
				if nversions is None:
					raise Exception(f"{self} cannot implement {otherContract}: number of versions not set")

				contract.concurrentVersions = nversions
				contract.duration = duration

				if self.releaseDate is not None:
					contract.computeEndDate(self.releaseDate)

	@property
	def inherits(self):
		return self._inherit

	@inherits.setter
	def inherits(self, other):
		assert(self._inherit is None)
		self.inheritFrom(other)
		self._inherit = other

	def inheritFrom(self, other):
		assert(isinstance(other, self.__class__))
		if self.url is None:
			self.url = other.url
		if self.description is None:
			self.description = other.description
		if self._mode is None:
			self._mode = other._mode
		if self.stability is None:
			self.stability = other.stability
		if self.generalSupport is None:
			self.generalSupport = other.generalSupport

		for contract in self.contracts:
			otherContract = other.getContract(contract.id)
			if otherContract is None or not otherContract.enabled:
				contract.enabled = False
			else:
				contract.inheritFrom(otherContract)

	@property
	def implements(self):
		return self._implement

	@implements.setter
	def implements(self, other):
		assert(self._implement is None)
		self._implement = other

		if other.implementations is None:
			other.implementations = set()
		other.implementations.add(self)

	@property
	def mode(self):
		return self._mode or self.MODE_SEQUENTIAL

	@mode.setter
	def mode(self, value):
		self._mode = value

	@property
	def cadence(self):
		return self._cadence

	@cadence.setter
	def cadence(self, value):
		self._cadence = value

	@property
	def releaseDate(self):
		return self._releaseDate

	@releaseDate.setter
	def releaseDate(self, value):
		self._releaseDate = value
		for contract in self.contracts:
			contract.releaseDate = value

	@property
	def maxConcurrentVersions(self):
		n = -1
		for contract in self.contracts:
			if contract.enabled and contract.concurrentVersions is not None:
				n = max(n, contract.concurrentVersions)
		if n < 0:
			return None
		return n

	def addContract(self, contractDef):
		baseContract = None
		if contractDef.baseContract is not None:
			baseContract = self._contracts[contractDef.baseContract]
		return self.createContract(contractDef.id, baseContract)

	def createContract(self, id, baseContract = None):
		contract = self.Contract(id, baseContract)
		contract.name = f"{self}/{id}"

		if self.generalSupport is None:
			self.generalSupport = contract
		self._contracts[id] = contract
		return contract

	def getContract(self, id):
		return self._contracts.get(id)

	@property
	def contracts(self):
		if self.generalSupport:
			yield self.generalSupport
		for contract in self._contracts.values():
			if contract is not self.generalSupport:
				yield contract

	def updateContractsFromRelease(self, release):
		self.releaseDate = release.date

		for lifecycleContract in self.contracts:
			releaseContract = release.getContract(lifecycleContract.id)
			if releaseContract is None:
				raise Exception(f"Lifecycle {self} uses contract {lifecycleContract} but release {release} does not define it")

			duration = lifecycleContract.duration
			if duration is None or duration == self.DURATION_MINOR_RELEASE:
				lifecycleContract.duration = releaseContract.duration
				lifecycleContract.endOfSupport = releaseContract.endOfSupport

			if lifecycleContract.endOfSupport is None:
				lifecycleContract.computeEndDate(release.date)

class SupportLevel(object):
	def __init__(self, id, rank, description):
		self.id = id
		self.rank = rank
		self.description = description

	def __str__(self):
		return self.id

	def __eq__(self, other):
		if other is None:
			return False
		return self.rank == other.rank
	
	def __lt__(self, other):
		return self.rank < other.rank

	def __le__(self, other):
		return self.rank <= other.rank

class SupportDictionary(object):
	def __init__(self):
		self.supportLevels = {}

	def create(self, id, rank, description):
		if id in self.supportLevels:
			raise Exception(f"Duplicate definition of support level \"{id}\"")
		level = SupportLevel(id, rank, description)
		self.supportLevels[id] = level
		return level

	def get(self, id):
		return self.supportLevels.get(id)

	# return the highest support level
	@property
	def defaultLevel(self):
		return max(self.supportLevels.values(), key = lambda s: s.rank)

class Policy(object):
	def __init__(self):
		self.teamsByID = {}
		self.lifecyclesByID = {}
		self.releasesByID = {}
		self.contracts = []
		self.supportDictionary = SupportDictionary()
		self.globalSettings = PolicySettings()

	def createContract(self, id):
		if id in (con.id for con in self.contracts):
			raise Exception(f"Refusing to overwrite contract definition for {id}")

		contract = ContractDefinition(id)
		self.contracts.append(contract)
		return contract

	def createSupportLevel(self, *args, **kwargs):
		return self.supportDictionary.create(*args, **kwargs)

	@property
	def teams(self):
		return iter(self.teamsByID.values())

	def createTeam(self, id):
		return self.createAgent(id)

	def createAgent(self, id):
		if id in self.teamsByID:
			raise Exception(f"Refusing to overwrite team definition for {id}")

		team = Team(id)
		self.teamsByID[id] = team
		return team

	def getTeam(self, id):
		return self.teamsByID.get(id)

	def matchOwner(self, id):
		team = self.getTeam(id)
		if team is not None:
			return team

		for team in self.teams:
			if team.email == id:
				return team
		return None

	@property
	def lifecycles(self):
		return iter(self.lifecyclesByID.values())

	def createLifeCycle(self, id):
		if id in self.lifecyclesByID:
			raise Exception(f"Refusing to overwrite lifecycle definition for {id}")

		lifecycle = LifeCycle(id)

		for contractDef in self.contracts:
			if contractDef.enabled:
				lifecycle.addContract(contractDef)

		self.lifecyclesByID[id] = lifecycle
		return lifecycle

	def getLifeCycle(self, id):
		return self.lifecyclesByID.get(id)

	@property
	def releases(self):
		return iter(self.releasesByID.values())

	def createRelease(self, id):
		if id in self.releasesByID:
			raise Exception(f"Refusing to overwrite release definition for {id}")

		release = Release(id)

		for contractDef in self.contracts:
			if contractDef.enabled:
				release.addContract(contractDef)

		self.releasesByID[id] = release
		return release

	def getRelease(self, id):
		return self.releasesByID.get(id)

	def getSubsequentRelease(self, release, ticktock = None):
		sortedReleases = sorted(self.releases, key = lambda r: str(r.date or "unspec"))
		if release not in sortedReleases:
			return None

		n = sortedReleases.index(release)
		for other in sortedReleases[n + 1:]:
			if ticktock is None or other.ticktock == ticktock:
				return other

	def save(self, path, labelFacade = None):
		def write(msg):
			print(msg, file = dbf)

		with open(path, "w") as dbf:
			for team in self.teams:
				write(f"team {team.id}")
				write(f"  name {team.fullName}")
				write(f"  email {team.email}")

			for contract in self.contracts:
				if not contract.enabled:
					continue

				write(f"contract {contract.id}")
				if contract.name != contract.id:
					write(f"   name {contract.name}")
				if contract.baseContract is not None:
					write(f"   base {contract.baseContract}")

			for lifecycle in self.lifecycles:
				write(f"lifecycle {lifecycle.id}")
				write(f"   mode {lifecycle.mode}")
				if lifecycle.stability:
					write(f"   stability {lifecycle.stability}")
				if lifecycle.cadence:
					write(f"   cadence {lifecycle.cadence}")
				if lifecycle.url:
					write(f"   url {lifecycle.url}")
				if lifecycle.generalSupport:
					write(f"   support {lifecycle.generalSupport.id}")
				if lifecycle.description:
					write(f"   description")
					for line in lifecycle.description.rstrip().split("\n"):
						write(f"   |{line}")
				if lifecycle.releaseDate is not None:
					write(f"   release {lifecycle.releaseDate}")

				for contract in lifecycle.contracts:
					if not contract.enabled:
						continue

					attributes = []
					for attrName in 'cadence', 'stability', 'duration', 'concurrentVersions', 'endOfSupport':
						attrValue = getattr(contract, attrName)
						if attrValue is not None:
							attributes.append(f"{attrName}={attrValue}")

					write(f"   contract {contract.id} {' '.join(attributes)}")

			for release in self.releases:
				write(f"release {release.id}")
				if release.major:
					write(f"   major {release.major}")
				if release.minor:
					write(f"   minor {release.minor}")
				if release.date:
					write(f"   date {release.date}")
				if release.lifecycle:
					write(f"   lifecycle {release.lifecycle}")

				for contract in release.contracts:
					attributes = []
					if contract.duration:
						attributes.append(f"duration={contract.duration}")
					if contract.endOfSupport:
						attributes.append(f"endOfSupport={contract.endOfSupport}")

					write(f"   contract {contract.id} {' '.join(attributes)}")

			if labelFacade is not None:
				for epic, maintainer, lifecycle in labelFacade.epics:
					write(f"epic {epic}")
					if maintainer:
						write(f"   maintainer {maintainer}")
					if lifecycle:
						write(f"   lifecycle {lifecycle}")

	class TeamContext(object):
		def __init__(self, team):
			self.team = team

		def process(self, cmd, rest):
			if cmd == 'name':
				self.team.fullName = rest
			elif cmd == 'email':
				self.team.email = rest

	class ContractContext(object):
		def __init__(self, contract):
			self.contract = contract

		def process(self, cmd, rest):
			if cmd == 'name':
				self.contract.name = rest
			elif cmd == 'base':
				self.contract.baseContract = rest

	class LifecycleContext(object):
		def __init__(self, lifecycle):
			self.lifecycle = lifecycle

		def process(self, cmd, rest):
			if cmd == 'name':
				self.lifecycle.name = rest
			elif cmd == 'url':
				self.lifecycle.url = rest
			elif cmd == 'mode':
				self.lifecycle.mode = rest
			elif cmd == 'stability':
				self.lifecycle.stability = rest
			elif cmd == 'cadence':
				self.lifecycle.cadence = rest
			elif cmd == 'description':
				self.lifecycle.description = ""
			elif cmd == '|':
				if rest is None:
					rest = "\n"
				self.lifecycle.description += rest
			elif cmd == 'contract':
				words = rest.split()
				id = words.pop(0)

				contract = self.lifecycle.getContract(id)
				for param in words:
					key, value = param.split('=')
					setattr(contract, key, value)

	class ReleaseContext(object):
		def __init__(self, release):
			self.release = release

		def process(self, cmd, rest):
			if cmd == 'major':
				self.release.major = rest
			elif cmd == 'minor':
				self.release.minor = rest
			elif cmd == 'date':
				self.release.date = datetime.date.fromisoformat(rest)
			elif cmd == 'lifecycle':
				self.release.lifecycle = rest
			elif cmd == 'contract':
				words = rest.split()
				id = words.pop(0)

				contract = self.release.getContract(id)
				for param in words:
					key, value = param.split('=')
					setattr(contract, key, value)

	class EpicContext(object):
		def __init__(self, labelFacade, name):
			self.labelFacade = labelFacade
			self.label = labelFacade.createEpic(name)

		def process(self, cmd, rest):
			if cmd == 'maintainer':
				self.labelFacade.setOwner(self.label, rest)
			elif cmd == 'lifecycle':
				self.labelFacade.setLifecycle(self.label, rest)

	class UnhandledCommandContext(object):
		def process(self, cmd, rest):
			pass

	def load(self, path, labelFacade = None):
		def write(msg):
			print(msg, file = dbf)

		context = None

		with open(path, "r") as dbf:
			line = None

			for line in dbf.readlines():
				w = line.strip().split(maxsplit = 1)
				if len(w) == 1:
					cmd, rest = w[0], None
				else:
					cmd, rest = w

				if line[0].isspace():
					if context is None:
						raise Exception(f"{path}: continuation line outside of context")
					context.process(cmd, rest)
					continue

				if cmd == 'team':
					team = self.createTeam(rest)
					context = self.TeamContext(team)
				elif cmd == 'contract':
					contract = self.createContract(rest)
					context = self.ContractContext(contract)
				elif cmd == 'lifecycle':
					lifecycle = self.createLifeCycle(rest)
					context = self.LifecycleContext(lifecycle)
				elif cmd == 'release':
					lifecycle = self.createRelease(rest)
					context = self.ReleaseContext(lifecycle)
				elif cmd == 'epic' and labelFacade is not None:
					context = self.EpicContext(labelFacade, rest)
				else:
					context = self.UnhandledCommandContext()

				assert(context is not None)
