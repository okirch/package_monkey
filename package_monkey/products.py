##################################################################
#
# Simple classes and functions related to codebase definitions
#
##################################################################
import yaml
import os
import re

from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .arch import *

class CacheLocation(object):
	def __init__(self, path):
		self.path = path

class BuildServiceCollection(object):
	def __init__(self):
		self.sourceProjects = []
		self.buildProjects = []

class OBSNameFilter(object):
	class Filter(object):
		def __init__(self):
			self.names = []
			self.prefixes = []
			self.suffixes = []

		def addPattern(self, pattern):
			if pattern.endswith("*"):
				pattern = pattern[:-1]
				assert('*' not in pattern and '?' not in pattern)
				self.prefixes.append(pattern)
			elif pattern.startswith("*"):
				pattern = pattern[1:]
				assert('*' not in pattern and '?' not in pattern)
				self.suffixes.append(pattern)
			else:
				assert('*' not in pattern and '?' not in pattern)
				self.names.append(pattern)

		def match(self, name):
			if name in self.names:
				return True

			if any(name.startswith(pattern) for pattern in self.prefixes):
				return True

			if any(name.endswith(pattern) for pattern in self.suffixes):
				return True

			return False

	def __init__(self):
		self.buildFilter = None
		self.rpmFilter = None

	def addBuildPattern(self, pattern):
		if self.buildFilter is None:
			self.buildFilter = self.Filter()
		self.buildFilter.addPattern(pattern)

	def matchBuild(self, name):
		if self.buildFilter is None:
			return False
		return self.buildFilter.match(name)

	def addRpmPattern(self, pattern):
		if self.rpmFilter is None:
			self.rpmFilter = self.Filter()
		self.rpmFilter.addPattern(pattern)

	def matchRpm(self, name):
		if self.rpmFilter is None:
			return False
		return self.rpmFilter.match(name)

class ProductCodebase(object):
	def __init__(self, name):
		self.name = name
		self.repoDef = None
		self.release = None

		self.architectures = ArchSet()
		self.projects = []

		self.nameFilter = None

	def __str__(self):
		return self.name

	@classmethod
	def load(klass, name, filename, *args, **kwargs):
		infomsg(f"Loading definition of codebase {name} from {filename}")

		codebase = klass(name, *args, **kwargs)
		with open(filename) as f:
			data = yaml.full_load(f)

		codebase.release = data.get('release')
		if codebase.release is None:
			raise Exception(f"{filename} does not specify the product release")

		codebase.architectures = ArchSet(data['architectures'])
		codebase.projects = codebase.expandProjects(data)

		filterDict = data.get('filter')
		if filterDict is not None:
			codebase.nameFilter = OBSNameFilter()

			for name in filterDict.get('builds', []):
				codebase.nameFilter.addBuildPattern(name)

			for name in filterDict.get('rpms', []):
				codebase.nameFilter.addRpmPattern(name)

		infomsg(f"   supported architectures: {codebase.architectures}")
		return codebase

	def expandProjects(self, data):
		data = data.get('buildservice')
		if data is None:
			return None

		info = BuildServiceCollection()
		info.sourceProjects = data.get('source') or []
		info.buildProjects = data.get('build') or []
		return info
