import argparse

from products import ProductCatalog, CacheLocation
from database import BackingStoreDB

class Application:
	def __init__(self, name):
		self.name = name
		self.args = argparse.ArgumentParser(name)

		self.args.add_argument('--db', default = 'productinfo.db')
		self.args.add_argument('--cache', default = '/work/projects/report/cache')
		self.args.add_argument('--family', default = 'dolomite')
		self.args.add_argument('--version', default = 'latest')
		self.args.add_argument('--arch', default = 'x86_64')
		
		self.opts = self.args.parse_args()

		self._cache = None
		self._store = None
		self._catalog = None

	@property
	def catalog(self):
		if self._catalog is None:
			self._catalog = ProductCatalog(cacheLocation = self.cache)
			# Make sure we have all the products that we use recorded in the database
			self._catalog.updateBackingStore(self.backingStore)
		return self._catalog

	@property
	def cache(self):
		if self._cache is None:
			self._cache = CacheLocation(self.opts.cache)
		return self._cache

	@property
	def backingStore(self):
		if self._store is None and self.opts.db:
			self._store = BackingStoreDB(self.opts.db)
		return self._store

	@property
	def architecture(self):
		return self.opts.arch

	def enumerateProducts(self):
		args = {}
		if self.opts.family:
			args['family'] = self.opts.family
			args['version'] = self.opts.version
			args['arch'] = self.opts.arch

		return self.catalog.enumerate(**args)
