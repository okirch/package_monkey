#
# package and product handling classes
#

import xml.etree.ElementTree as ET
import urllib.parse
import os.path
import os
from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg
from .arch import *
from .newdb import RpmBase, RpmInfo
from .filter import Classification

class ProductMediator(object):
	def __init__(self, productCodebase, packageCollection):
		self.productCodebase = productCodebase
		self.packageCollection = packageCollection

	# Generate all synthetic builds and packages
	def generateSyntheticBuilds(self, db):
		collection = self.packageCollection

		for rpm in db.rpms:
			if not rpm.isSynthetic:
				continue

			build = db.createBuild(rpm.name)
			build.isSynthetic = True
			build.addRpm(rpm)
			collection.addBuild(build)

	def loadAndVerifyPackages(self, store):
		collection = self.packageCollection
		for build in store.builds:
			collection.addBuild(build)

		return True

	def generatePromise(self, name, db):
		rpm = db.createRpm(f"promise:{name}", type = RpmBase.TYPE_PROMISE)

		if rpm.new_build is not None:
			assert(rpm.new_build in self.packageCollection.builds)
			return

		build = db.createBuild(rpm.name)
		build.isSynthetic = True
		build.addRpm(rpm)

		self.packageCollection.addBuild(build)
		return rpm

# FIXME: should this move to newdb.py?
class PackageCollection(object):
	def __init__(self):
		self._packages = set()
		self._builds = []
		self._sources = set()
		self._packageDict = {}
		self._archDict = {}

	def copy(self):
		result = self.__class__()

		result._packages = self._packages.copy()
		result._builds = self._builds.copy()
		result._sources = self._sources.copy()
		result._packageDict = self._packageDict.copy()
		result._archDict = self._archDict.copy()

		return result

	def __len__(self):
		return len(self._packageDict)

	def __iter__(self):
		return iter(self.packages)

	def rpmsWithArch(self):
		for rpm in self:
			yield rpm, self._archDict.get(rpm.name)

	def get(self, name):
		return self._packageDict.get(name)

	def getArch(self, arg):
		if type(arg) is not str:
			arg = str(arg)
		return self._archDict.get(arg)

	# Add an RPM to the collection.
	# If the package is already present, update its arch set
	def add(self, rpm, archSet = None, overwriteArch = False):
		self._packages.add(rpm)
		self._packageDict[rpm.name] = rpm

		if archSet is not None:
			if not archSet.issubset(rpm.architectures):
				raise Exception(f"Invalid arch set {archSet} for {rpm}: rpm does not support {archSet.difference(rpm.architectures)}")

			if overwriteArch or rpm.name not in self._archDict:
				self._archDict[rpm.name] = archSet.copy()
			else:
				self._archDict[rpm.name].update(archSet)

	def discard(self, rpm, archSet = None):
		if rpm not in self._packages:
			return

		resultingArchSet = None
		if archSet is not None:
			existingArchSet = self._archDict.get(rpm.name)
			if existingArchSet is None:
				existingArchSet = rpm.architectures
			resultingArchSet = existingArchSet.difference(archSet)

		if resultingArchSet:
			self._archDict[rpm.name] = resultingArchSet
		else:
			self._packages.discard(rpm)
			del self._packageDict[rpm.name]
			try:
				del self._archDict[rpm.name]
			except: pass

	def addBuild(self, build):
		self._builds.append(build)
		for rpm in build.binaries:
			if rpm.name.endswith('-debuginfo') or rpm.name.endswith('-debugsource'):
				continue
			if not rpm.isSourcePackage:
				self.add(rpm)
			else:
				self._sources.add(rpm)

	@property
	def builds(self):
		return iter(self._builds)

	@property
	def packages(self):
		return iter(self._packages.union(self._sources))

	# set operations
	def update(self, other):
		assert(isinstance(other, self.__class__))

		for rpm, archSet in other.rpmsWithArch():
			if archSet is None:
				archSet = rpm.architectures
			self.add(rpm, archSet)

	def difference_update(self, other):
		assert(isinstance(other, self.__class__))
		for rpm, archSet in other.rpmsWithArch():
			self.discard(rpm, archSet)

	def union(self, other):
		ret = self.copy()
		ret.update(other)
		return ret

	def difference(self, other):
		ret = self.copy()
		ret.difference_update(other)
		return ret

	def enablePackageTracing(self, traceMatcher):
		for rpm in self.packages:
			if rpm.isSourcePackage:
				continue
			if traceMatcher.match(rpm.name):
				infomsg(f"Tracing rpm {rpm}")
				rpm.trace = True

		for build in self.builds:
			if traceMatcher.match(build.name):
				infomsg(f"Tracing build {build}")
				build.trace = True

##################################################################
# This is used in various places during classification and
# composition to deal with RPMs that should be there but aren't,
# or as a quick-n-dirty override of composer decisions.
##################################################################
class RpmOverrideList(object):
	class Entry(object):
		def __init__(self, name, archSet = None):
			self.name = name
			self.archSet = archSet

		def __str__(self):
			if self.archSet is not None:
				return f"{self.name}: [{self.archSet}]"
			return self.name

	def __init__(self):
		self.items = {}

	def __bool__(self):
		return bool(self.items)

	def __len__(self):
		return len(self.items)

	def __iter__(self):
		return iter(sorted(self.items.values(), key = lambda i: i.name))

	def __contains__(self, item):
		if type(item) is str:
			return item in self.items
		return item.name in self.items

	def add(self, item):
		assert(isinstance(item, self.Entry))
		self.items[item.name] = item

	def discard(self, name):
		try:
			del self.items[name]
			return True
		except:
			pass
		return False

	# Note, entries from "other" do not overwrite entries in self that have the same key
	def update(self, other):
		assert(isinstance(other, self.__class__))

		for item in other:
			if item not in self:
				self.add(item)

	def difference_update(self, other):
		assert(isinstance(other, self.__class__))

		for item in other:
			self.discard(item.name)

	def union(self, other):
		assert(isinstance(other, self.__class__))

		result = self.__class__()
		result.items = self.items.copy()
		result.update(other)
		return result

	def difference(self, other):
		assert(isinstance(other, self.__class__))

		result = self.__class__()
		result.items = self.items.copy()
		result.difference_update(other)
		return result

	def toRpms(self, db, create = False):
		result = PackageCollection()
		nerrors = 0

		for item in self:
			rpm = db.lookupRpm(item.name)
			if rpm is None and create:
				# and item.archSet is not None:
				assert(item.archSet is not None)
				rpm = db.createRpm(item.name)
				rpm.architectures.update(item.archSet)

				rpm.new_build = db.createBuild("__external__")
				infomsg(f"XXX Created {rpm}")

			if rpm is None:
				errormsg(f"override_rpms specifies unknown rpm {item.name}")
				nerrors += 1
				continue

			if rpm.trace and item.archSet is not None:
				infomsg(f"Override {rpm}: {item.archSet}")

			result.add(rpm, item.archSet or rpm.architectures)

		if nerrors:
			raise Exception(f"unknown rpm names in override_rpms")

		return result

	@classmethod
	def build(klass, yamlList, defaultArchSet = None):
		result = klass()
		for entry in yamlList:
			item = None
			if type(entry) is dict:
				if len(entry) == 1:
					for key, value in entry.items():
						if type(value) is list:
							item = klass.Entry(key, ArchSet(value))
			elif type(entry) is str:
				item = klass.Entry(entry, defaultArchSet)
				assert(item)

			if item is None:
				raise Exception(f"entries in override_rpms must be either string or 'name: [arch, ...]': found {entry} (type {type(entry)})")

			result.add(item)
		return result

