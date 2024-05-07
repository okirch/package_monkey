import argparse

from products import ProductCatalog, CacheLocation
from database import BackingStoreDB
from util import ExecTimer, loggingFacade
from util import debugmsg, infomsg, warnmsg, errormsg
from util import NameMatcher
from obsclnt import OBSClient
import os

class Application(object):
	def __init__(self, name, extra_args = []):
		self.name = name
		self.args = argparse.ArgumentParser(name)

		self.args.add_argument('--statedir', default = '.')
		self.args.add_argument('--db', default = None)
		self.args.add_argument('--cache', default = '/work/projects/report/cache')
		self.args.add_argument('--family', default = 'alp')
		self.args.add_argument('--version', default = 'latest')
		self.args.add_argument('--arch', default = 'x86_64')
		self.args.add_argument('--quiet', action = 'store_true', default = False)
		self.args.add_argument('--debug', action = 'append', default = [])
		self.args.add_argument('--trace', action = 'append', default = [])
		self.args.add_argument('--logfile', action = 'store')

		for extra in extra_args:
			self.args.add_argument(**extra)

		self._opts = None

		self._cache = None
		self._store = None
		self._catalog = None

	@property
	def opts(self):
		if self._opts is None:
			self.parseArguments()

		return self._opts

	def addArgument(self, *args, **kwargs):
		if self._opts is not None:
			raise Exception(f"You cannot define command line arguments after processing the command line")

		self.args.add_argument(*args, **kwargs)

	def parseArguments(self):
		if self._opts is None:
			self._opts = self.args.parse_args()
			self.initializeLogging()

	@property
	def statePath(self):
		return f"{self.opts.statedir}/{self.opts.family}"

	@property
	def backingStorePath(self):
		if self.opts.db:
			return self.opts.db
		return f"{self.statePath}/product.db"

	def getOutputPath(self, basename):
		return f"{self.statePath}/{basename}"

	@property
	def obsModelDefinition(self):
		if self.opts.model:
			return self.opts.model
		return f"{self.statePath}/model.yaml"

	def initializeLogging(self):
		if not self.opts.quiet:
			loggingFacade.enableStdout()
		if self.opts.logfile:
			loggingFacade.addLogfile(self.opts.logfile)

		infomsg(f"Starting {self.name}")

		# --debug <facility> enables debugging for a specific facility
		# default: log all messages logged through util.debugmsg()
		# all: log all messages logged through any logger's debug() method
		for facility in self.opts.debug:
			loggingFacade.setLogLevel(facility, 'debug')

		if loggingFacade.isDebugEnabled('obs'):
			debugmsg("obs debugging enabled")

	@property
	def catalog(self):
		return self.loadProductCatalog()

	def loadProductCatalog(self):
		if self._catalog is None:
			self._catalog = ProductCatalog(cacheLocation = self.cache)

			# FIXME: this is bogus; it should happen elsewhere
			# Make sure we have all the products that we use recorded in the database
			# self._catalog.updateBackingStore(self.backingStore)
		return self._catalog

	@property
	def cache(self):
		if self._cache is None:
			self._cache = CacheLocation(self.opts.cache)
		return self._cache

	@property
	def backingStore(self):
		return self.loadBackingStore()

	def reloadBackingStore(self, *args, **kwargs):
		self._store = None
		return self.loadBackingStore(*args, **kwargs)

	def loadBackingStore(self, readonly = False, dependencyTreeLookups = False, sourceLookups = False):
		if self._store is not None:
			return self._store

		# Force load the product family definition to enable mapping of product ID to product object
		self.loadProductCatalog()

		dbPath = self.backingStorePath
		if os.path.exists(dbPath):
			timing = ExecTimer()
			store = BackingStoreDB(dbPath)

			# FIXME: this should depend either on an argument to this function, or
			# on a command line switch
			if False:
				store.fixupLatest()
				store.fixupBuilds()
				store.fixupRequirements()
				# stop

			if dependencyTreeLookups:
				store.enableDependencyTreeLookups()
			if sourceLookups:
				store.enableSourceLookups()
			if readonly:
				store.setReadonly()

			infomsg(f"Loaded database {dbPath}: {timing} elapsed")
			self._store = store
		elif not readonly:
			self._store = BackingStoreDB(dbPath)
		else:
			raise Exception(f"Unable to find database!")

		return self._store

	def loadClassificationScheme(self):
		from filter import Classification
		from writers import XmlSchemeReader

		path = self.getOutputPath("hierarchy.xml")
		infomsg(f"Reading classification result from {path}")
		reader = XmlSchemeReader(path)
		return reader.read()

	def loadClassification(self, classificationScheme = None):
		from filter import Classification
		from writers import XmlReader

		if classificationScheme is None:
			classificationScheme = Classification.Scheme()

		path = self.getOutputPath("packages.xml")
		infomsg(f"Reading classification result from {path}")
		reader = XmlReader(path, classificationScheme)
		return reader.read()

	def loadModelMapping(self):
		from model import ComponentModelMapping

		path = self.getOutputPath("model.yaml")
		infomsg(f"Reading mapping description from {path}")
		return ComponentModelMapping.load(path)

	@property
	def traceMatcher(self):
		if not self.opts.trace:
			return None

		return NameMatcher(self.opts.trace)

	@property
	def architecture(self):
		return self.opts.arch

	@property
	def defaultHttpPath(self):
		return self.getCachePath("http")

	def getCachePath(self, subdir):
		# for the time being, we place everything in side a local ./cache directory
		return f"cache/{subdir}"

	def enumerateProducts(self):
		if self.opts.family is None:
			errormsg(f"You have to specify a product family using the --family option")

		return self.catalog.enumerate(family = self.opts.family,
				version = self.opts.version,
				arch = self.opts.arch)

	@property
	def productFamily(self):
		if self.opts.family is None:
			raise Exception("Cannot determine product family, please specify --family option")

		family = self.catalog.select(self.opts.family)
		if family is None:
			raise Exception(f"Unknown product family {self.opts.family}")
		return family

	def beginChapter(self, msg):
		infomsg("")
		infomsg(f"*** {msg} ***")
		infomsg("")

class OBSClientApplication(Application):
	OBS_HOST_DEFAULT = "api.suse.de"

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.args.add_argument('--obs-host', default = self.OBS_HOST_DEFAULT)
		self.args.add_argument('--obs-cache-strategy', default = None)

	@property
	def obsClient(self):
		obs = OBSClient(self.opts.obs_host)
		obs.setCachePath(self.defaultHttpPath)

		# control how much we talk to OBS directly, and how much we use the cache
		if self.opts.obs_cache_strategy is not None:
			obs.setCacheStrategy(self.opts.obs_cache_strategy)

		return obs

