##################################################################
#
# represent classes of arch strings efficiently
#
##################################################################

from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

__names__ = ['archRegistry', 'ArchSet']

class ArchRegistry(object):
	canonicalArchList = (
		'x86_64',
		's390x',
		'ppc64le',
		'aarch64',
	)

	_instance = None

	def __init__(self):
		self.archCount = len(self.canonicalArchList)

	@classmethod
	def instance(klass):
		if klass._instance is None:
			klass._instance = klass()
		return klass._instance

	@property
	def fullset(self):
		return ArchSet(self.canonicalArchList)

	def isValidArchitecture(self, name):
		return name in self.canonicalArchList

	def nameToID(self, name):
		try:
			return self.canonicalArchList.index(name)
		except:
			pass

		raise Exception(f"Invalid architecture name {name}")

	def nameToMask(self, name):
		return (1 << self.nameToID(name))

	def maskToNameSet(self, mask):
		result = set()

		for name in self.canonicalArchList:
			if mask & 1:
				result.add(name)
			mask >>= 1
		return result

	def maskToString(self, mask):
		result = []

		for name in self.canonicalArchList:
			if mask & 1:
				result.append(name)
			mask >>= 1
		return ' '.join(result)

	def nameSetToMask(self, names):
		mask = 0
		for name in names:
			mask |= self.nameToMask(name)
		return mask


archRegistry = ArchRegistry()

class ArchSet(object):
	registry = None

	def __init__(self, mask_or_names = []):
		if type(mask_or_names) is int:
			self.mask = mask_or_names
		else:
			self.mask = archRegistry.nameSetToMask(mask_or_names)

	def __eq__(self, other):
		return self.mask == other.mask

	def __ne__(self, other):
		return self.mask != other.mask

	def __str__(self):
		names = self.names
		if not names:
			return "[no architectures]"
		return ", ".join(names)

	@property
	def names(self):
		return archRegistry.maskToNameSet(self.mask)

	def add(self, arch):
		self.mask = self.mask | archRegistry.nameToMask(arch)

	def remove(self, arch):
		self.mask = self.mask & ~archRegistry.nameToMask(arch)

	def discard(self, arch):
		self.mask = self.mask & ~archRegistry.nameToMask(arch)

	def union(self, other):
		return self.__class__(self.mask | other.mask)

	def intersection(self, other):
		return self.__class__(self.mask & other.mask)

	def difference(self, other):
		return self.__class__(self.mask & ~other.mask)

	def update(self, other):
		self.mask |= other.mask

	def intersection_update(self, other):
		self.mask &= other.mask

	def difference_update(self, other):
		self.mask &= ~other.mask

	def issubset(self, other):
		return not (self.mask & ~other.mask)

	def copy(self):
		return self.__class__(self.mask)

	def __contains__(self, name):
		id = archRegistry.nameToID(name)
		return bool(self.mask & (1 << id))

	def __bool__(self):
		return bool(self.mask)

	def __len__(self):
		# we could also count the bits
		return len(archRegistry.maskToNameSet(self.mask))

	def __iter__(self):
		return iter(archRegistry.maskToNameSet(self.mask))

	def __eq__(self, other):
		return self.mask == other.mask

	def __ne__(self, other):
		return self.mask != other.mask

	def __str__(self):
		return archRegistry.maskToString(self.mask)
