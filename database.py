import sqlite3
from packages import Package, PackageInfo

optSqlDebug = 0

def splitDictKeyValues(d):
	keys = []
	values = []
	for k, v in d.items():
		keys.append(k)
		values.append(v)

	return keys, values


class DB(object):
	class CommitLock:
		def __init__(self):
			self.holderCount = 0
			self.deferCount = 0

		def acquire(self):
			self.holderCount += 1

		def release(self):
			assert(self.holderCount > 0)
			self.holderCount -= 1

		def allowImmediateCommit(self):
			if self.holderCount == 0:
				return True
			self.deferCount += 1
			return False

		def getAndResetCommitCount(self):
			count = self.deferCount
			self.deferCount = 0
			return count

		def __bool__(self):
			return self.holderCount > 0

	class DeferredCommit:
		def __init__(self, db, lock):
			self.db = db

			lock.acquire()
			self.lock = lock

		def __del__(self):
			self.commit()

		def commit(self):
			if self.lock is not None:
				count = self.lock.getAndResetCommitCount()
				self.lock.release()
				self.lock = None

				if count:
					print(f"Applying {count} deferred commits")
					self.db.commit()

	def __init__(self):
		self.conn = None

		self.commitLock = self.CommitLock()

	def connect(self, db_file):
		try:
			self.conn = sqlite3.connect(db_file)
		except sqlite3.Error as e:
			print(f"Failed to connect to sqlite3 DB {db_file}: {e}")

		return self.conn

	def commit(self):
		if self.commitLock.allowImmediateCommit():
			self.conn.commit()

	def deferCommit(self):
		return self.DeferredCommit(self, self.commitLock)

class ObjectHandle(object):
	def __init__(self, db, tableName, id):
		self.db = db
		self.tableName = tableName
		self.id = id

	def fetch(self, memoryObject):
		conn = self.db.conn
		sql = f"SELECT * from {self.tableName} where id=?"

		c = conn.cursor()
		c.execute(sql, (self.id, ))

		names = [field[0] for field in c.description]
		for r in c.fetchall():
			for name, value in zip(names, r):
				print(name, value)

class ObjectTemplate(object):
	def __init__(self, name, objectToDB, ctorArgs = None):
		self.name = name
		self._toDB = objectToDB

		if ctorArgs is None:
			ctorArgs = set(objectToDB.values())
		elif not isinstance(ctorArgs, set):
			ctorArgs = set(ctorArgs)
		self._ctorArguments = ctorArgs

		self._fromDB = {(v, k) for k, v in objectToDB.items()}

	def mapObjectToDB(self, obj):
		d = {}
		for attrName, fieldName in self._toDB.items():
			value = getattr(obj, attrName)
			if value is not None:
				d[fieldName] = value

		# print(f"{obj} -> {d}")
		return d

	def constructObjectFromDB(self, klass, d):
		args = {}
		for attrName, fieldName in self._toDB.items():
			if attrName in self._ctorArguments:
				args[attrName] = d.get(fieldName)

		obj = klass(**args)

		for attrName, fieldName in self._toDB.items():
			value = d.get(fieldName)
			if value is not None and attrName not in self._ctorArguments:
				setattr(obj, attrName, value)

		return obj

class Table(object):
	def __init__(self, db, name):
		self.db = db
		self.name = name
		self.objectTemplate = None

	def setObjectTemplate(self, templ):
		self.objectTemplate = templ

	def execute(self, sqlStatement, values = []):
		try:
			c = self.db.conn.cursor()

			if optSqlDebug:
				print(f"SQL: {sqlStatement}")
				if values:
					print(f"     {values}")
			c.execute(sqlStatement, values)
		except sqlite3.Error as e:
			print(f"SQL Error: {e} - {type(e)}")
			print(f"The offending statement was: {sqlStatement}")
			return None

		return c

	def create(self, sqlStatement):
		if not self.execute(sqlStatement):
			print(f"Failed to create table {self.name}")
			return False

		print(f"Created table {self.name}")
		return True

	def createIndex(self, name, fields):
		onClause = ", ".join(fields)
		sqlStatement = f"""CREATE INDEX IF NOT EXISTS {name} ON {self.name} ({onClause});"""
		if not self.execute(sqlStatement):
			print(f"Failed to create table index {name} for table {self.name}")
			return False

		print(f"Created table index {name} for table {self.name}")
		return True

	def createUniqueIndex(self, name, fields):
		onClause = ", ".join(fields)
		sqlStatement = f"""CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {self.name} ({onClause});"""
		if not self.execute(sqlStatement):
			print(f"Failed to create table index {name} for table {self.name}")
			return False

		print(f"Created table index {name} for table {self.name}")
		return True

	def insert(self, **kwargs):
		return self.insertDict(kwargs)

	def insertObject(self, obj):
		assert(self.objectTemplate)

		d = self.objectTemplate.mapObjectToDB(obj)
		return self.insertDict(d)

	def insertDict(self, d):
		keys, values = splitDictKeyValues(d)
		return self.insertKeysAndValues(keys, values)

	def insertKeysAndValues(self, keys, values):
		db = self.db

		c = db.conn.cursor()
		nameFmt = ",".join(keys)
		valueFmt = ",".join(["?"] * len(values))
		sql = f"INSERT INTO {self.name}({nameFmt}) VALUES ({valueFmt})"
		c.execute(sql, values)
		db.commit()

		return ObjectHandle(db, self.name, c.lastrowid)

	def updateObject(self, obj, **selector):
		assert(self.objectTemplate)

		# FIXME: shouldn't mapObjectToDB return keys/values lists right away?
		d = self.objectTemplate.mapObjectToDB(obj)

		keys, values = splitDictKeyValues(d)
		return self.updateKeysAndValues(keys, values, **selector)

	def updateKeysAndValues(self, keys, values, **selector):
		setFmt = ", ".join([f"{name}=?" for name in keys])

		whereClause, whereValues = self.buildWhereStatement(selector)
		sql = f"UPDATE {self.name} SET {setFmt} {whereClause}"

		c = self.execute(sql, values + whereValues)
		self.db.commit()

		return ObjectHandle(self.db, self.name, c.lastrowid)

	def fetchAll(self, fields = None, **selector):
		c = self.selectFrom(fields, selector)
		return self.cursorFetchAll(c)

	def fetchOne(self, **selector):
		c = self.selectFrom(None, selector)
		if c is None:
			return None

		names = [field[0] for field in c.description]
		row = c.fetchone()
		if row is None:
			return None
		return dict(zip(names, row))

	def fetchColumn(self, fieldName, **selector):
		c = self.selectFrom((fieldName, ), selector)
		if c is None:
			return None

		return [row[0] for row in c.fetchall()]

	def selectFrom(self, fields, selector):
		if fields:
			fieldClause = ",".join(fields)
			sql = f"SELECT {fieldClause} from {self.name}"
		else:
			sql = f"SELECT * from {self.name}"

		where, values = self.buildWhereStatement(selector)
		if where:
			sql += " " + where

		return self.execute(sql, values)

	def buildWhereStatement(self, selector):
		if not selector:
			return "", []

		names = []
		values = []
		for k, v in selector.items():
			names.append(f"{k}=?")
			values.append(v)

		if len(names) == 1:
			where = names[0]
		else:
			where = "(" + " AND ".join(names) + ")"
		return f" where {where}", values

	def selectMultiple(self, fields, whereField, values):
		result = []
		while len(values) > 100:
			result += self.selectMultipleWork(fields, whereField, values[:100])
			del values[:100]

		result += self.selectMultipleWork(fields, whereField, values)
		return result

	def selectMultipleWork(self, fields, whereField, values):
		if len(values) == 0:
			return []

		if fields:
			fieldClause = ",".join(fields)
			sql = f"SELECT {fieldClause} from {self.name}"
		else:
			sql = f"SELECT * from {self.name}"

		count = len(values)
		sql += " WHERE (" + " OR ".join([f"{whereField}=?"] * count) + ")"

		c = self.execute(sql, values)
		return self.cursorFetchAll(c)

	def cursorFetchAll(self, c):
		if c is None:
			return None

		result = []

		names = [field[0] for field in c.description]
		for row in c.fetchall():
			d = dict(zip(names, row))
			result.append(d)

		if optSqlDebug:
			print(f"SQL: found {len(result)} matches")

		return result

	def constructObject(self, klass, d):
		return self.objectTemplate.constructObjectFromDB(klass, d)

class UniqueTable(Table):
	NAME = None
	OBJECT_TEMPLATE = None

	_instance = None

	def __init__(self, db):
		assert(self.__class__.NAME)
		super().__init__(db, self.__class__.NAME)

	@classmethod
	def instantiate(klass, db):
		tbl = klass(db)

		sql = getattr(klass, 'createTableSQL', None)
		if sql is None:
			fields = klass.TABLE_FIELDS.strip()
			sql = f"""CREATE TABLE IF NOT EXISTS {klass.NAME} (
					{fields}
				);"""
		if not tbl.create(sql):
			return None

		if klass.OBJECT_TEMPLATE:
			tbl.setObjectTemplate(klass.OBJECT_TEMPLATE)

		klass._instance = tbl
		return tbl

	@staticmethod
	def instance():
		assert(self._instance)
		return self._instance

class ProductCache:
	class CacheEntry:
		def __init__(self, name, version, arch, id = None, key = None, object = None):
			self.id = id
			self.name = name
			self.version = version
			self.arch = arch
			self.object = object

			if key is None:
				key = ProductCache.makeKey(name, version, arch)
			self.key = key

	def __init__(self):
		self._id2Product = {}
		self._key2Product = {}

	def entryByName(self, name, version, arch, key = None, id = None):
		if key is None:
			key = self.makeKey(name, version, arch)

		entry = self._key2Product.get(key)
		if entry is None:
			entry = self.CacheEntry(name, version, arch, key = key)
			self._key2Product[key] = entry
		if id is not None:
			if entry.id is None:
				self.updateCacheEntry(entry, id)

		return entry

	def entryById(self, id):
		return self._id2Product.get(id)

	def updateCacheEntry(self, entry, id):
		assert(entry.id is None or entry.id == id)
		assert(id is not None)

		self._id2Product[id] = entry
		entry.id = id

	@staticmethod
	def makeKey(name, version, arch):
		return f"{name}:{version}:{arch}"


class ProductTable(UniqueTable):
	NAME = "products"

	createTableSQL = """CREATE TABLE IF NOT EXISTS products (
				id integer PRIMARY KEY,
				key text NOT NULL,
				name text NOT NULL,
				version text NOT NULL,
				arch text NOT NULL
			);"""

	def __init__(self, *args):
		super().__init__(*args)

	def populateCache(self, cache):
		for d in self.fetchAll():
			entry = cache.entryByName(**d)

	def addProduct(self, name, version, arch):
		h = self.insert(name = name, version = version, arch = arch,
				key = ProductCache.makeKey(name, version, arch))
		if h is None:
			return None
		return h.id

class PackageTable(UniqueTable):
	NAME = "packages"
	OBJECT_TEMPLATE = ObjectTemplate("package", {
				'backingStoreId' : 'id',
				'name' : 'name',
				'epoch' : 'epoch',
				'version' : 'version',
				'release' : 'release',
				'arch' : 'arch',
				'pkgid' : 'repoPackageID',
				'group' : 'rpmGroup',
				'sourceName' : 'sourceName',
				'sourcePackageHash' : 'sourceHash',
				'productId' : 'productId',
				},
				ctorArgs = (
					'name', 'epoch', 'version', 'release', 'arch',
				))

	createTableSQL = """CREATE TABLE IF NOT EXISTS packages (
				id integer PRIMARY KEY,
				productId integer NOT NULL,
				name text NOT NULL,
				epoch text,
				version text NOT NULL,
				release text NOT NULL,
				arch text NOT NULL,
				sourceName text,
				sourceHash text,
				repoPackageID text,
				rpmGroup text NOT NULL
			);"""

	def __init__(self, db):
		super().__init__(db)

		self.knownPackageIDs = dict()

	def updateKnownIDs(self):
		c = self.db.conn.cursor()
		c.execute("SELECT id, repoPackageID FROM packages;")
		for row in c.fetchall():
			self.knownPackageIDs[row[1]] = row[0]
		print(f"Found {len(self.knownPackageIDs)} packages in database")

	def addPackageObject(self, obj):
		if obj.pkgid is None:
			print(f"addPackageObject: {obj.fullname()} has no pkgid")
			if obj.arch != 'nosrc':
				print(f"Error: cannot add package {obj.fullname()} without pkgid")
			return obj.backingStoreId

		id = self.knownPackageIDs.get(obj.pkgid)
		if id is not None:
			# print(f"{obj.pkgid}: already known as {id}")
			return id

		h = self.insertObject(obj)
		if h is None:
			print(f"Failed to insert {obj.fullname()} into database")
			return None

		self.knownPackageIDs[obj.pkgid] = h.id
		# print(f"{obj.pkgid}: {obj.fullname()} -> {h.id}")
		return h.id

class LatestPackageTable(UniqueTable):
	NAME = "latest"
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			name text NOT NULL,
			epoch text,
			version text NOT NULL,
			release text NOT NULL,
			arch text NOT NULL,
			productId integer,
			pkgId integer NOT NULL
		"""

	OBJECT_TEMPLATE = ObjectTemplate("package", {
				'name' : 'name',
				'epoch' : 'epoch',
				'version' : 'version',
				'release' : 'release',
				'arch' : 'arch',
				'productId' : 'productId',
				'backingStoreId' : 'pkgId',
				},
				ctorArgs = (
					'name', 'epoch', 'version', 'release', 'arch',
				))


	class Latest:
		UNCHANGED = 0
		CREATED = 1
		UPDATED = 2

		def __init__(self, name):
			self.name = name
			self.id = None
			self.pinfo = None
			self.changed = False

		def update(self, pkg):
			if self.pinfo and pkg.parsedVersion <= self.pinfo.parsedVersion:
				return False
			self.pinfo = pkg
			return True

	def __init__(self, db):
		super().__init__(db)
		self._buckets = {}

	def fetchKnownPackages(self, store):
		for d in self.fetchAll():
			pinfo = store.constructPackageInfo(d)
			bucket = self.getBucket(pinfo.name)
			bucket.pinfo = pinfo

	def update(self, pkg):
		assert(pkg.backingStoreId)

		b = self.getBucket(pkg.name)
		if b.update(pkg):
			if b.id is None:
				# print(f"latest: insert object {pkg.fullname()}")
				h = self.insertObject(pkg)
				assert(h)
				b.id = h.id
			else:
				# print(f"latest: update object {pkg.fullname()} (id {b.id})")
				self.updateObject(pkg, id = b.id)

	def getBucket(self, name):
		b = self._buckets.get(name)
		if b is None:
			b = self.Latest(name)
			self._buckets[name] = b
		return b


class FilesTable(UniqueTable):
	NAME = "files"

	createTableSQL = """CREATE TABLE IF NOT EXISTS files (
				id integer PRIMARY KEY,
				pkgId integer,
				path text
			);"""

	def addFiles(self, pathList, pkgID):
		keys = ('pkgId', 'path')
		for path in pathList:
			self.insertKeysAndValues(keys, (pkgID, path))

class DependencyTable(UniqueTable):
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			pkgId integer,
			name text,
			op text,
			epoch text,
			version text,
			release text
		"""

	def addDependency(self, dep, pkgId):
		d = dict()
		d['pkgId'] = pkgId
		d['name'] = dep.name

		op = getattr(dep, 'flags', None)
		if op is not None:
			d['op'] = op
			pv = dep.parsedVersion
			d['epoch'] = pv.epoch
			d['version'] = pv.version
			d['release'] = pv.release

		return self.insert(**d)

	def addDependencies(self, depList, pkgId):
		if not depList:
			return
		assert(pkgId is not None)
		for dep in depList:
			self.addDependency(dep, pkgId)

	def retrieveDependenciesById(self, pkgId):
		result = []

		fields = ('name', 'op', 'epoch', 'version', 'release',)
		for d in self.fetchAll(fields = fields, pkgId = pkgId):
			result.append(self.constructDependency(d))
		return result

	def retrieveDependenciesByName(self, name):
		result = []

		fields = ('name', 'op', 'epoch', 'version', 'release', 'pkgId',)
		for d in self.fetchAll(fields = fields, name = name):
			result.append((self.constructDependency(d), d['pkgId']))
		return result

	@classmethod
	def constructDependency(klass, d):
		# translate the fields
		args = {'name' : d['name']}

		op = d['op']
		if op is not None:
			args['flags'] = op
			args['ver'] = d['version']
			args['rel'] = d['release']
			# FIXME
			# args['epoch'] = d['epoch']

		# print(args)
		return Package.createDependency(**args)

class RequiresTable(DependencyTable):
	NAME = "requires"

class ProvidesTable(DependencyTable):
	NAME = "provides"

class FakeProduct:
	def addPackageFromDB(self, **kwargs):
		print(kwargs)

class PackageCache(dict):
	def put(self, pkg):
		assert(pkg.backingStoreId is not None)
		self[pkg.backingStoreId] = pkg

class ProvidesCache(dict):
	def put(self, name, packages):
		try:
			self[name] += packages
		except:
			self[name] = [] + packages

class BackingStoreDB(DB):
	def __init__(self, path):
		super().__init__()

		if not self.connect(path):
			barf()

		self.productCache = ProductCache()
		self.packageProductLink = {}

		self.products = ProductTable.instantiate(self)
		self.products.createIndex("idx_prod_key", ["key"])
		self.products.populateCache(self.productCache)

		self.packages = PackageTable.instantiate(self)
		self.packages.createIndex("idx_pkg_name", ["name"])
		self.packages.createIndex("idx_pkg_product", ["productId"])
		# FIXME: rename to idx_pkg_hash
		self.packages.createUniqueIndex("id_pkg_hash", ["repoPackageID"])
		self.packages.updateKnownIDs()

		self.latest = LatestPackageTable.instantiate(self)
		self.latest.createIndex("idx_latest_name", ["name"])
		self.latest.fetchKnownPackages(self)

		# FIXME:
		# Instead of mapping strings to package ids, it might be
		# more compact if we create tables to map file names
		# and version/release strings to an ID, and have
		# files and dependency tables just refer to these strings.

		self.files = FilesTable.instantiate(self)
		self.files.createIndex("idx_file_package", ["pkgId"])

		self.requires = RequiresTable.instantiate(self)
		self.requires.createIndex("idx_req_package", ['pkgId'])

		self.provides = ProvidesTable.instantiate(self)
		self.provides.createIndex("idx_prov_package", ['pkgId'])

		self.packageCache = PackageCache()
		self.providesCache = ProvidesCache()

	# Complete product cache entry by attaching our in-memory release object to it
	# This allows us to create ProductInfo objects with a correct pinfo.product pointer
	def mapProduct(self, release):
		entry = self.productCache.entryByName(name = release.name, version = release.version, arch = release.arch)
		if entry.id is None:
			id = self.products.addProduct(name = release.name, version = release.version, arch = release.arch)
			self.productCache.updateCacheEntry(entry, id)
			print(f"Found new product {entry.key}, mapped to ID {id}")

		assert(self.productCache.entryById(entry.id) == entry)

		entry.object = release

		self.resolvePackageInfoProduct(entry.id, release)

		return entry.id

	def xaddProduct(self, **kwargs):
		return self.products.addProduct(**kwargs)

	def addPackage(self, **kwargs):
		assert('id' not in kwargs)
		return self.packages.insert(**kwargs)

	def addPackageObject(self, obj):
		# Never try to add nosrc packages
		if obj.arch == 'nosrc':
			return

		obj.backingStoreId = self.packages.addPackageObject(obj)
		if obj.backingStoreId is None:
			print(f"ALERT: {obj.fullname()} has no database id")
			return

		self.files.addFiles(obj.files, obj.backingStoreId)
		self.requires.addDependencies(obj.requires, obj.backingStoreId)
		self.provides.addDependencies(obj.provides, obj.backingStoreId)

		self.latest.update(obj)
		return obj.backingStoreId

	def addPackageObjectList(self, objList):
		defer = self.deferCommit()
		for obj in objList:
			self.addPackageObject(obj)
		defer.commit()

	def enumeratePackages(self, product):
		for d in self.packages.fetchAll(('id', 'name', 'version', 'release', 'epoch', 'arch'), productId = product.productId):
			fart
			yield PackageInfo(name = d['name'],
				version = d['version'],
				release = d['release'],
				epoch = d['epoch'],
				arch = d['arch'],
				backingStoreId = d['id'],
				product = product)

	def enumerateLatestPackages(self):
		latestPkgIds = list(self.latest.fetchColumn('pkgId'))
		return self.retrieveMultiplePackageInfos(latestPkgIds)

	def retrievePackage(self, pinfo):
		pkg = self.packageCache.get(pinfo.backingStoreId)
		if pkg is not None:
			return pkg

		d = self.packages.fetchOne(id = pinfo.backingStoreId)
		if d is None:
			return None

		src = self.retrieveSourcePackage(d)

		pkg = self.packages.constructObject(Package, d)
		pkg.sourcePackage = src
		pkg.product = pinfo.product

		# now do the files
		pkg.files = self.retrievePackageFiles(pinfo.backingStoreId)

		# and dependencies
		pkg.requires = self.requires.retrieveDependenciesById(pinfo.backingStoreId)
		pkg.provides = self.provides.retrieveDependenciesById(pinfo.backingStoreId)

		self.packageCache.put(pkg)
		return pkg

	def retrieveSourcePackage(self, d):
		src = None

		sourceId = d.get('sourceId')
		if sourceId is not None:
			del d['sourceId']
			sd = self.packages.fetchOne(id = sourceId)
			if sd is not None:
				src = self.packages.constructObject(Package, sd)

		if src is None:
			pass

		return src

	def retrievePackageFiles(self, pkgId):
		result = self.files.fetchColumn('path', pkgId = pkgId)
		return result

	def retrieveFileProviders(self, path):
		return self.files.fetchColumn('pkgId', path = path)

	def retrievePackageInfo(self, pkgId):
		d = self.packages.fetchOne(id = pkgId)
		if d is None:
			return None

		return self.constructPackageInfo(d)

	def retrieveMultiplePackageInfos(self, pkgIdList):
		result = []
		for d in self.packages.selectMultiple([], 'id', pkgIdList):
			result.append(self.constructPackageInfo(d))
		return result

	def constructPackageInfo(self, d):
		productId = d.get('productId')

		pinfo = PackageInfo(name = d['name'],
			version = d['version'],
			release = d['release'],
			epoch = d['epoch'],
			arch = d['arch'],
			backingStoreId = d['id'],
			productId = productId)

		if productId is not None:
			entry = self.productCache.entryById(productId)
			if entry is not None:
				pinfo.product = entry.object

		if pinfo.product is None:
			self.packageInfoLacksProduct(pinfo)

		return pinfo

	def packageInfoLacksProduct(self, pinfo):
		productId = pinfo.productId

		unresolved = self.packageProductLink.get(productId)
		if unresolved is None:
			unresolved = []
			self.packageProductLink[productId] = unresolved

		unresolved.append(pinfo)

	def resolvePackageInfoProduct(self, productId, object):
		try:
			unresolved = self.packageProductLink[productId]
		except:
			return

		del self.packageProductLink[productId]
		for pinfo in unresolved:
			assert(pinfo.productId == productId)
			pinfo.product = object

	def enumerateProvidersOfName(self, name):
		result = self.providesCache.get(name)
		if result is not None:
			return result

		result = []

		depList = []
		pkgIdList = []
		for dep, pkgId in self.provides.retrieveDependenciesByName(name):
			depList.append(dep)
			pkgIdList.append(pkgId)

		lastGoodIndex = 0
		for pinfo in self.retrieveMultiplePackageInfos(pkgIdList):
			if pinfo is None:
				badPkgId = pkgIdList[lastGoodIndex + 1]
				raise Exception(f"cannot get package with id {badPkgId}")
			result.append((dep, pinfo))

		if name.startswith('/'):
			dep = Package.createDependency(name = name)
			for pkgId in self.retrieveFileProviders(name):
				pinfo = self.retrievePackageInfo(pkgId)
				if pinfo is None:
					raise Exception(f"cannot get package with id {pkgId}")
				result.append((dep, pinfo))

		# print(f"Resolving {name}: found {len(result)} candidates")
		self.providesCache.put(name, result)

		return result
