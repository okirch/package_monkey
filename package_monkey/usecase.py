####################################################################################################################################
#
# Helper classes for handling use case information as produced as part of the SLES16.0
# packaging workshop.
#
####################################################################################################################################

import yaml
from .util import loggingFacade, errormsg, warnmsg, infomsg

class UseCase(object):
	def __init__(self, slug, name = None):
		self.slug = slug
		self.name = name

		self._children = []

	def __str__(self):
		if not self.slug:
			return f"use case {self.name}"
		return f"use case {self.slug}"

	@property
	def implementations(self):
		return sorted(self._children, key = str)

	def addImplementation(self, uci):
		self._children.append(uci)

class UseCaseImpl(object):
	def __init__(self, slug, name, usecase):
		self.slug = slug
		self.name = name
		self.usecase = usecase
		self.packages = set()

	def __str__(self):
		if not self.slug:
			return f"use case {self.name}"
		return f"use case {self.slug}"

	def addPackage(self, pkg):
		self.packages.add(pkg)

class UseCaseCatalog(object):
	def __init__(self):
		self._usecases = {}
		self._implementations = {}

		self._buildMap = None

	@property
	def usecases(self):
		for slug, uc in sorted(self._usecases.items()):
			yield uc

	def getUsecase(self, slug):
		return self._usecases.get(slug)

	def addUsecase(self, uc):
		assert(uc.slug not in self._usecases)
		self._usecases[uc.slug] = uc

	def getUsecaseImplementation(self, slug):
		return self._implementations.get(slug)

	def addUsecaseImplementation(self, uci):
		assert(uci.slug not in self._implementations)
		self._implementations[uci.slug] = uci

	def lookupBuild(self, name):
		if self._buildMap is None:
			self._buildMap = {}
			for uci in self._implementations.values():
				for pkg in uci.packages:
					self._buildMap[pkg] = uci

		return self._buildMap.get(name)

	@classmethod
	def loadFromYaml(klass, path):
		catalog = klass()

		with open(path, "r") as f:
			data = yaml.full_load(f)

		for key, value in data.items():
			catalog.processUseCase(key, value)

		return catalog

	def processUseCase(self, slug, data):
		uc = UseCase(slug)
		self.addUsecase(uc)

		for key, value in data.items():
			if key == 'name':
				uc.name = value
			elif key == 'implementations':
				if value is None:
					continue
				for uciSlug, uciData in value.items():
					self.processUseCaseImplementation(uc, uciSlug, uciData)
			else:
				raise Exception(f"Invalid attribute in definition of {uc}")

	def processUseCaseImplementation(self, uc, slug, data):
		uci = UseCaseImpl(slug, None, uc.slug)
		self.addUsecaseImplementation(uci)
		uc.addImplementation(uci)

		for key, value in data.items():
			if key == 'name':
				uci.name = value
			elif key == 'packages':
				if value is None:
					continue
				assert(type(value) is list)
				for name in value:
					uci.addPackage(name)
			else:
				raise Exception(f"Invalid attribute in definition of {uci}")
