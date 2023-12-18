import argparse

from products import ProductCatalog, CacheLocation
from database import BackingStoreDB
from util import ExecTimer, loggingFacade
from util import debugmsg, infomsg, warnmsg, errormsg
from util import NameMatcher
from obsclnt import OBSClient

class Application:
	OBS_HOST_DEFAULT = "api.suse.de"

	def __init__(self, name, extra_args = []):
		self.name = name
		self.args = argparse.ArgumentParser(name)

		self.args.add_argument('--db', default = 'productinfo.db')
		self.args.add_argument('--cache', default = '/work/projects/report/cache')
		self.args.add_argument('--family', default = 'dolomite')
		self.args.add_argument('--version', default = 'latest')
		self.args.add_argument('--arch', default = 'x86_64')
		self.args.add_argument('--quiet', action = 'store_true', default = False)
		self.args.add_argument('--debug', action = 'append', default = [])
		self.args.add_argument('--trace', action = 'append', default = [])
		self.args.add_argument('--logfile', action = 'store')
		self.args.add_argument('--obs-host', default = Application.OBS_HOST_DEFAULT)
		self.args.add_argument('--obs-cache-strategy', default = None)

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
		self.args.add_argument(*args, **kwargs)

	def parseArguments(self):
		if self._opts is None:
			self._opts = self.args.parse_args()
			self.initializeLogging()

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

	def loadBackingStore(self, readonly = False, dependencyTreeLookups = False, sourceLookups = False):
		if self._store is None and self.opts.db:
			timing = ExecTimer()
			store = BackingStoreDB(self.opts.db)

			# FIXME: this should depend either on an argument to this function, or
			# on a command line switch
			if True:
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

			infomsg(f"Loaded database {self.opts.db}: {timing} elapsed")
			self._store = store
		return self._store


	@property
	def traceMatcher(self):
		if not self.opts.trace:
			return None

		return NameMatcher(self.opts.trace)

	@property
	def obsClient(self):
		obs = OBSClient(self.opts.obs_host)
		obs.setCachePath(self.defaultHttpPath)

		# control how much we talk to OBS directly, and how much we use the cache
		if self.opts.obs_cache_strategy is not None:
			obs.setCacheStrategy(self.opts.obs_cache_strategy)

		return obs

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
		args = {}
		if self.opts.family:
			args['family'] = self.opts.family
			args['version'] = self.opts.version
			args['arch'] = self.opts.arch

		return self.catalog.enumerate(**args)
