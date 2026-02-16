#
# This script compares the default.productcompose file produced by packagemonkey
# with a "reference" file, eg the one that lives in the SLES product git repo.
#

import sys
import yaml

from .util import infomsg, warnmsg, errormsg
from .arch import ArchSet, archRegistry
from .options import ApplicationBase

class ProductDiffApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, *kwargs)

	class Product(object):
		def __init__(self, name, composition):
			self.name = name
			self.composition = composition
			self.packages = {}
			self.architectures = archRegistry.fullset
			self.ignoreArchitectures = ArchSet()
			self.ignore = False

		def __str__(self):
			return self.name

		def empty(self):
			return not self.packages

		def createPackageSet(self, arch):
			result = self.packages.get(arch)
			if result is None:
				result = set()
				self.packages[arch] = result
			return result

		def getPackageSet(self, arch):
			return self.packages.get(arch)

		def addPackages(self, names, arch = None):
			if arch is None:
				if not names:
					warnmsg(f"{self.composition}: packageset {self} specifies no or empty packages")
					return

				for arch in self.architectures.difference(self.ignoreArchitectures):
					self.createPackageSet(arch).update(names)
			else:
				if arch in self.ignoreArchitectures:
					warnmsg(f"{self.composition}: {self} specifies package list for ignored architecture {arch}")
				elif not names:
					warnmsg(f"{self.composition}: packageset {self}_{arch} specifies no or empty packages")
					return

				self.createPackageSet(arch).update(names)

		def definedArchitectures(self):
			return set(self.packages.keys())

	class Composition(object):
		def __init__(self, path):
			self.path = path

			with open(path) as f:
				self.data = yaml.full_load(f)

			self.products = {}
			self.mainList = None

			# hard-coded for now
			self.createProduct('sles')
			self.createProduct('sles_ha')
			self.createProduct('sles_sap')
			self.createProduct('sles_offline')

			# currently hardcoded: ignore product/arch combinations we do not ship
			self.ignore('sles_ha', 'aarch64')
			self.ignore('sles_sap', 'aarch64')
			self.ignore('sles_sap', 's390x')

			self.processAllPackageSets(self.data)

		def __str__(self):
			return self.path

		@property
		def packages(self):
			return self.flavors

		def createProduct(self, name):
			product = self.products.get(name)
			if product is None:
				product = ProductDiffApplication.Product(name, self)
				self.products[name] = product
			return product

		def getProduct(self, name):
			return self.products.get(name)

		def ignore(self, productName, arch = None):
			product = self.getProduct(productName)
			if product is not None:
				if arch is None:
					product.ignore = True
				else:
					product.ignoreArchitectures.add(arch)

		@property
		def productNames(self):
			result = set()
			for id, product in self.products.items():
				if not product.empty():
					result.add(id)
			return result

		def processOnePackageSet(self, name, packageList, archList):
			allArch = archRegistry.fullset

			for arch in allArch:
				if name.endswith('_' + arch):
					productName = name[:-len(arch) - 1]

					product = self.getProduct(productName)
					if product is None:
						return False

					assert(archList == [arch])

					if not packageList:
						if arch not in product.ignoreArchitectures:
							warnmsg(f"{self}: packageset {name} specifies no or empty packages")
						return False

					product.addPackages(packageList, arch)
					return True

			product = self.getProduct(name)
			if product is None:
				return False

			assert(not archList)

			product.addPackages(packageList)
			return True

		def processAllPackageSets(self, data):
			if type(data) is dict:
				pkgSets = data.get('packagesets')
			else:
				pkgSets = data

			result = {}
			for p in pkgSets:
				name = p['name']

				if name == 'main':
					assert('add' in p)
					self.mainList = set(p['add'])
					continue

				self.processOnePackageSet(name, p.get('packages'), p.get('architectures'))

			return result

	def run(self):
		srcComposition = self.loadComposition(self.opts.srcfile)
		dstComposition = self.loadComposition(self.opts.dstfile)

		print(f"Inspecting difference {srcComposition} -> {dstComposition}")
		self.compositionDiff(srcComposition, dstComposition)

	def loadComposition(self, arg):
		return self.Composition(self.getComposerPath(arg))

	def compositionDiff(self, src, dst):
		ignoreProducts = set(self.opts.ignore_product)

		srcNames = src.productNames.difference(ignoreProducts)
		dstNames = dst.productNames.difference(ignoreProducts)

		for name in srcNames.difference(dstNames):
			print(f"Product {name} REMOVED")
		for name in dstNames.difference(srcNames):
			print(f"Product {name} ADDED")

		for name in sorted(srcNames.intersection(dstNames)):
			self.productDiff(src.getProduct(name), dst.getProduct(name))

		if src.mainList != dst.mainList:
			if not dst.mainList:
				print("List main REMOVED")
			elif not src.mainList:
				print("List main ADDED")
			else:
				print("List main CHANGES:")

				delta = dst.mainList.difference(src.mainList)
				if delta:
					print(f"  ADDED:")
					for name in sorted(delta):
						print(f"    {name}")

				delta = src.mainList.difference(dst.mainList)
				if delta:
					print(f"  REMOVED:")
					for name in sorted(delta):
						print(f"    {name}")

	def productDiff(self, srcProduct, dstProduct):
		productName = srcProduct.name

		srcNames = srcProduct.definedArchitectures()
		dstNames = dstProduct.definedArchitectures()

		for arch in srcNames.difference(dstNames):
			if arch in srcProduct.ignoreArchitectures:
				print(f"Product {productName}/{arch} REMOVED (ignored)")
			else:
				print(f"Product {productName}/{arch} REMOVED")
		for arch in dstNames.difference(srcNames):
			if arch in dstProduct.ignoreArchitectures:
				print(f"Product {productName}/{arch} ADDED (ignored)")
			else:
				print(f"Product {productName}/{arch} ADDED")

		for arch in sorted(srcNames.intersection(dstNames)):
			ignore = (arch in srcProduct.ignoreArchitectures)
			self.packageDiff(f"{productName}/{arch}", srcProduct.getPackageSet(arch), dstProduct.getPackageSet(arch), ignore)

	def packageDiff(self, tag, srcNames, dstNames, ignore = False):
		removed = srcNames.difference(dstNames)
		added = dstNames.difference(srcNames)

		if not removed and not added:
			return

		print(f"Product {tag} CHANGES{ignore and ' (ignored)' or ''}:")

		if removed:
			print(f"  removed {len(removed)} packages")
			if not ignore:
				for name in sorted(removed):
					print(f"    {name}")

		if added:
			print(f"  added {len(added)} packages")
			if not ignore:
				for name in sorted(added):
					print(f"    {name}")

	def getComposerPath(self, arg):
		if not arg.startswith('@'):
			return arg

		if arg == '@@':
			productData = self.productData
		else:
			data = self.getSnapshot(arg[1:])
			if data is None:
				raise Exception(f"Unknown snapshot {arg[1:]}")

			productData = data.getProduct(self.productRelease)

		return productData.getPath('default.productcompose')
