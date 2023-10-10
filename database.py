import sqlite3
from packages import Package, PackageInfo
from obsclnt import OBSPackage

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
					# print(f"Applying {count} deferred commits")
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

		if optSqlDebug:
			print(f"Created table {self.name}")
		return True

	def createIndex(self, name, fields):
		onClause = ", ".join(fields)
		sqlStatement = f"""CREATE INDEX IF NOT EXISTS {name} ON {self.name} ({onClause});"""
		if not self.execute(sqlStatement):
			print(f"Failed to create table index {name} for table {self.name}")
			return False

		if optSqlDebug:
			print(f"Created table index {name} for table {self.name}")
		return True

	def createUniqueIndex(self, name, fields):
		onClause = ", ".join(fields)
		sqlStatement = f"""CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {self.name} ({onClause});"""
		if not self.execute(sqlStatement):
			print(f"Failed to create table index {name} for table {self.name}")
			return False

		if optSqlDebug:
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

	def updateDict(self, d, **selector):
		keys, values = splitDictKeyValues(d)
		return self.updateKeysAndValues(keys, values, **selector)

	def updateKeysAndValues(self, keys, values, **selector):
		setFmt = ", ".join([f"{name}=?" for name in keys])

		whereClause, whereValues = self.buildWhereStatement(selector)
		sql = f"UPDATE {self.name} SET {setFmt} {whereClause}"

		c = self.execute(sql, values + whereValues)
		self.db.commit()

		return ObjectHandle(self.db, self.name, c.lastrowid)

	def replaceObject(self, obj):
		assert(self.objectTemplate)

		d = self.objectTemplate.mapObjectToDB(obj)
		return self.replaceDict(d)

	def replaceDict(self, d):
		keys, values = splitDictKeyValues(d)
		return self.replaceKeysAndValues(keys, values)

	def replaceKeysAndValues(self, keys, values):
		db = self.db

		c = db.conn.cursor()
		nameFmt = ",".join(keys)
		valueFmt = ",".join(["?"] * len(values))
		sql = f"REPLACE INTO {self.name}({nameFmt}) VALUES ({valueFmt})"
		c.execute(sql, values)
		db.commit()

		return ObjectHandle(db, self.name, c.lastrowid)

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
			sql = f"SELECT {fieldClause} FROM {self.name}"
		else:
			sql = f"SELECT * FROM {self.name}"

		count = len(values)
		sql += " WHERE (" + " OR ".join([f"{whereField}=?"] * count) + ")"

		c = self.execute(sql, values)
		return self.cursorFetchAll(c)

	def deleteMultiple(self, whereField, values):
		while len(values) > 100:
			self.deleteMultipleWork(whereField, values[:100])
			del values[:100]

		self.deleteMultipleWork(whereField, values)

		# we currently do not commit the deletion right away.
		# not sure whether this is a good idea or not.
		# self.db.commit()

	def deleteMultipleWork(self, whereField, values):
		if len(values) == 0:
			return

		sql = f"DELETE FROM {self.name}"

		count = len(values)
		sql += " WHERE (" + " OR ".join([f"{whereField}=?"] * count) + ")"

		self.execute(sql, values)

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
				'buildTime' : 'buildTime',
				'pkgid' : 'repoPackageID',
				'group' : 'rpmGroup',
				'sourceName' : 'sourceName',
				'sourcePackageHash' : 'sourceHash',
				'sourceBackingStoreId' : 'sourceId',
				'productId' : 'productId',
				'obsBuildId' : 'buildId',
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
				buildId integer,
				buildTime integer,
				sourceName text,
				sourceHash text,
				sourceId integer,
				repoPackageID text,
				rpmGroup text NOT NULL
			);"""

	def __init__(self, db):
		super().__init__(db)

		self.knownPackageIDs = dict()
		self.knownBuilds = dict()

	@staticmethod
	def makeKey(name, version, release, arch):
		return f"{name}-{version}-{release}.{arch}"

	def updateKnownIDs(self):
		c = self.db.conn.cursor()
		c.execute("SELECT id, name, version, release, arch, buildId FROM packages;")
		for row in c.fetchall():
			(id, name, version, release, arch, buildId) = row
			key = self.makeKey(name, version, release, arch)
			self.knownPackageIDs[key] = id

			try:
				self.knownBuilds[buildId].append(id)
			except:
				self.knownBuilds[buildId] = [id]

		print(f"Found {len(self.knownPackageIDs)} packages in database")

	def isKnownPackageObject(self, obj):
		key = self.makeKey(obj.name, obj.version, obj.release, obj.arch)
		return self.knownPackageIDs.get(key)

	def addPackageObject(self, obj):
		key = self.makeKey(obj.name, obj.version, obj.release, obj.arch)

		id = self.knownPackageIDs.get(key)
		if id is not None:
			# print(f"{key}: already known as {id}")
			return id

		h = self.insertObject(obj)
		if h is None:
			print(f"Failed to insert {obj.fullname()} into database")
			return None

		self.knownPackageIDs[key] = h.id
		# print(f"{key}: {obj.fullname()} -> {h.id}")
		return h.id

	def getPackagesForBuild(self, buildId):
		return self.knownBuilds.get(buildId) or []

##################################################################
# This table tracks the latest known version of any (non-source)
# package.
# FIXME: we should probably distinguish by name+version rather
# than just the name. Some architectures have 32bit and 64bit
# versions of the same package.
##################################################################
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
		self._duplicates = []

	@property
	def knownPackages(self):
		for id, b in sorted(self._buckets.items()):
			yield b.pinfo

	@property
	def duplicatePackages(self):
		return iter(self._duplicates)

	def fetchKnownPackages(self, store):
		for d in self.fetchAll():
			pinfo = store.constructPackageInfo(d)
			bucket = self.getBucket(pinfo.name)

			if bucket.pinfo is not None:
				self._duplicates.append(bucket.pinfo)

			bucket.pinfo = pinfo

	# FIXME: something is wrong here; we end up with duplicate entries in the DB
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

	def prune(self):
		idsToDrop = []
		for pinfo in self._duplicates:
			print(f"Table \"{self.name}\" contains redundant entry {pinfo}")
			idsToDrop.append(pinfo.backingStoreId)

		if not idsToDrop:
			print(f"Table \"{self.name}\" does not contain any redundant entries")
			return

		print(f"Dropping {len(idsToDrop)} redundant entries from table {self.name}")
		self.deleteMultiple('id', idsToDrop)
		self.db.commit()

		self._duplicates = []


class BuildTable(UniqueTable):
	NAME = "builds"
	OBJECT_TEMPLATE = ObjectTemplate("obsPackage", {
				'backingStoreId' : 'id',
				'name' : 'name',
#				'sourcePackageId' : 'sourceId',
				'buildTime' : 'buildTime',
				},
				ctorArgs = (
					'name',
				))
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			name text NOT NULL,
			sourcePackageId integer,
			buildTime integer
		"""

	class Entry:
		def __init__(self, name, id = None):
			self.id = id
			self.name = name
			self.buildTime = None

		def update(self, buildTime):
			if self.buildTime == buildTime:
				return False
			self.buildTime = buildTime
			return True

	def __init__(self, db):
		super().__init__(db)
		self._entries = {}

	def fetchKnownPackages(self, store):
		for d in self.fetchAll():
			entry = self.addEntry(d['name'])
			entry.id = d['id']
			entry.buildTime = d['buildTime']

	def hasChanged(self, name, buildTime):
		entry = self._entries.get(name)
		if entry is None:
			return True
		return (entry.buildTime != buildTime)

	def nameToId(self, name):
		entry = self.addEntry(name)
		if not entry.id:
			h = self.replaceDict({'name': name})
			if h is None:
				return None

			entry.id = h.id

		return entry.id

	def update(self, name, buildTime):
		entry = self.addEntry(name)
		if not entry.update(buildTime):
			return entry.id

		if entry.id is not None:
			self.updateDict({'name': name, 'buildTime': buildTime}, id = entry.id)
		else:
			h = self.insertDict({'name': name, 'buildTime': buildTime})
			entry.id = h.id

		return entry.id

	def addEntry(self, name):
		entry = self._entries.get(name)
		if entry is None:
			entry = self.Entry(name)
			self._entries[entry.name] = entry
		return entry


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

	class Cache(dict):
		def put(self, id, dep):
			self[id] = dep

	def __init__(self, *args):
		super().__init__(*args)
		self._cache = self.Cache()
		self._knownPackages = []

	def fetchKnownPackages(self):
		self._knownPackages = set(self.fetchColumn('pkgId'))
		print(f"We have dependencies for {len(self._knownPackages)} packages")

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
		self._knownPackages.add(pkgId)

	def removeDependenciesForList(self, idList):
		self.deleteMultiple('pkgId', idList)
		self._knownPackages.difference_update(set(idList))

	def haveDependencies(self, pkgId):
		assert(pkgId is not None)
		return pkgId in self._knownPackages

	def retrieveDependencyById(self, id):
		d = self.fetchOne(id = id)
		if d is None:
			return None
		return self.constructDependency(d)

	def retrieveDependenciesByPkgId(self, pkgId):
		result = []

		for d in self.fetchAll(pkgId = pkgId):
			result.append(self.constructDependency(d))
		return result

	def retrieveDependenciesByPkgName(self, name):
		result = []

		for d in self.fetchAll(name = name):
			result.append((self.constructDependency(d), d['pkgId']))
		return result

	def constructDependency(self, d):
		id = d['id']

		dep = self._cache.get(id)
		if dep is not None:
			return dep

		# translate the fields
		args = {'name' : d['name'], 'backingStoreId' : id}

		op = d['op']
		if op is not None:
			args['flags'] = op
			args['ver'] = d['version']
			args['rel'] = d['release']
			# FIXME
			# args['epoch'] = d['epoch']

		# print(args)
		dep = Package.createDependency(**args)
		self._cache.put(id, dep)
		return dep

class RequiresTable(DependencyTable):
	NAME = "requires"

class ProvidesTable(DependencyTable):
	NAME = "provides"

class DirectedGraphTable(UniqueTable):
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			requiringPkgId integer NOT NULL,
			requiredPkgId integer NOT NULL,
			dependencyId integer
		"""

	def __init__(self, *args):
		super().__init__(*args)
		self._knownPackages = []

	def fetchKnownPackages(self):
		self._knownPackages = set(self.fetchColumn('requiringPkgId'))
		print(f"{self.name}: we have dependencies for {len(self._knownPackages)} packages")

	def removeDependenciesForList(self, idList):
		self.deleteMultiple('requiringPkgId', idList)
		self._knownPackages.difference_update(set(idList))

	def haveDependencies(self, pkgId):
		assert(pkgId is not None)
		return pkgId in self._knownPackages

	def addEdge(self, **kwargs):
		self.insert(**kwargs)

	def retrieveRequired(self, pkgId):
		c = self.selectFrom(fields = ('dependencyId', 'requiredPkgId'), selector = {'requiringPkgId':  pkgId})
		if c is None:
			return None

		result = []
		for row in c.fetchall():
			result.append((row[0], row[1]))

		return result

class TreeTable(DirectedGraphTable):
	NAME = "tree"

class BuildTreeTable(DirectedGraphTable):
	NAME = "builddep"

class KeyValueTable(UniqueTable):
	NAME = "keyvalue"

	TABLE_FIELDS = """
			key string NOT NULL,
			value string
		"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._data = {}

	def onLoad(self):
		for d in self.fetchAll():
			key = d['key']
			self._data[key] = d['value']

	def set(self, key, value):
		self.replaceDict({'key': key, 'value': value})
		self._data[key] = value

	def get(self, key):
		return self._data.get(key)

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

		self.builds = BuildTable.instantiate(self)
		self.builds.createUniqueIndex("idx_build_name", ["name"])
		self.builds.fetchKnownPackages(self)

		# FIXME:
		# Instead of mapping strings to package ids, it might be
		# more compact if we create tables to map file names
		# and version/release strings to an ID, and have
		# files and dependency tables just refer to these strings.

		self.files = FilesTable.instantiate(self)
		self.files.createIndex("idx_file_package", ["pkgId"])

		self.requires = RequiresTable.instantiate(self)
		self.requires.createIndex("idx_req_package", ['pkgId'])
		self.requires.fetchKnownPackages()

		self.provides = ProvidesTable.instantiate(self)
		self.provides.createIndex("idx_prov_package", ['pkgId'])
		self.provides.fetchKnownPackages()

		self.tree = TreeTable.instantiate(self)
		self.tree.createIndex("idx_tree_down", ['requiringPkgId'])
		self.tree.createIndex("idx_tree_up", ['requiredPkgId'])
		self.tree.fetchKnownPackages()

		self.buildDep = BuildTreeTable.instantiate(self)
		self.buildDep.createIndex("idx_bdep_down", ['requiringPkgId'])

		self.keyValueStore = KeyValueTable.instantiate(self)
		self.keyValueStore.onLoad()

		self.packageCache = PackageCache()
		self.obsPackageCache = PackageCache()
		self.providesCache = ProvidesCache()

		self._allowDepTreeLookups = False
		self._requireSourceLookups = False

	def putProperty(self, name, value):
		self.keyValueStore.set(name, value)

	def getProperty(self, name):
		return self.keyValueStore.get(name)

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

	def isKnownPackageObject(self, obj):
		id = self.packages.isKnownPackageObject(obj)
		if id is not None:
			obj.backingStoreId = id
			return True
		return False

	def havePackageDependencies(self, obj):
		return self.provides.haveDependencies(obj.backingStoreId) or \
			self.tree.haveDependencies(obj.backingStoreId)

	def addPackageObject(self, obj):
		obj.backingStoreId = self.packages.addPackageObject(obj)
		if obj.backingStoreId is None:
			print(f"ALERT: {obj.fullname()} has no database id")
			return

		if obj.arch not in ('src', 'nosrc'):
			self.latest.update(obj)

		return obj.backingStoreId

	def fetchPackageObjectId(self, obj):
		obj.backingStoreId = self.packages.addPackageObject(obj)
		return bool(obj.backingStoreId)

	def updatePackageDependencies(self, obj):
		self.requires.addDependencies(obj.requires, obj.backingStoreId)
		self.provides.addDependencies(obj.provides, obj.backingStoreId)
		self.files.addFiles(obj.files, obj.backingStoreId)

		if obj.resolvedRequires is not None:
			edges = set()
			for required in obj.resolvedRequires:
				if required.backingStoreId is None:
					print(f"Cannot add dependency to DB: {obj.fullname()} requires {required.fullname()}, but the latter has no ID set")
					continue

				# print(f"{obj.fullname()} -> {required.fullname()}")
				edges.add((obj.backingStoreId, required.backingStoreId, None))

			for source, target, depId in edges:
				self.tree.addEdge(requiringPkgId = source, requiredPkgId = target, dependencyId = depId)

	def updatePackageSource(self, obj):
		sourceId = obj.sourceBackingStoreId
		if sourceId is None:
			fail

		if obj.backingStoreId is None:
			raise Exception(f"Cannot update sourceId for {obj.fullname()} - backingStoreId is not set")

		keys = ['sourceId']
		values = [sourceId]
		self.packages.updateKeysAndValues(keys, values, id = obj.backingStoreId)

	def updatePackageDependenciesWork(self, objList):
		# Clean out all files and dependencies that belong to this package
		pkgIdList = list(set(_.backingStoreId for _ in objList))

		self.requires.removeDependenciesForList(pkgIdList)
		self.provides.removeDependenciesForList(pkgIdList)
		self.tree.removeDependenciesForList(pkgIdList)
		self.files.deleteMultiple('pkgId', pkgIdList)

		for obj in objList:
			if obj.backingStoreId:
				self.updatePackageDependencies(obj)

	def addPackageObjectList(self, objList, updateDependencies = True):
		defer = self.deferCommit()

		for obj in objList:
			self.addPackageObject(obj)

		self.updatePackageDependenciesWork(objList)
		defer.commit()

	def updatePackageDependenciesObjectList(self, objList):
		defer = self.deferCommit()
		self.updatePackageDependenciesWork(objList)
		defer.commit()

	def updatePackageSourceObjectList(self, objList):
		defer = self.deferCommit()
		for obj in objList:
			print(f"Updating source for {obj.shortname}")
			self.updatePackageSource(obj)
		defer.commit()

	def obsPackageWasRebuilt(self, obsPackage):
		if obsPackage.buildTime is None:
			return False
		return self.builds.hasChanged(obsPackage.name, obsPackage.buildTime)

	def lookupBuildId(self, name):
		return self.builds.nameToId(name)

	def lookupBuildIdsForList(self, obsPackageList):
		defer = self.deferCommit()

		success = True

		for obsPackage in obsPackageList:
			obsPackage.backingStoreId = self.lookupBuildId(obsPackage.name)
			if obsPackage.backingStoreId is None:
				print(f"Unable to add OBS Build {obsPackage.name} to DB")
				success = False

		defer.commit()
		return success

	def updateBuilds(self, obsPackageList):
		defer = self.deferCommit()

		# Update build dependencies, first
		edges = set()
		for obsPackage in obsPackageList:
			for rpm in obsPackage.buildRequires:
				edges.add((obsPackage.backingStoreId, rpm.backingStoreId))

		for source, target in edges:
			self.buildDep.addEdge(requiringPkgId = source, requiredPkgId = target)

		updated = []
		for obsPackage in sorted(obsPackageList, key = lambda p: p.name):
			sourceId = None
			if obsPackage.sourcePackage:
				sourceId = obsPackage.sourcePackage.backingStoreId

			id = self.builds.update(obsPackage.name, obsPackage.buildTime)
			if id is None:
				print(f"Unable to update table {self.builds.name} for OBS package {obsPackage.name}")
				continue

			obsPackage.backingStoreId = id
			updated.append(obsPackage)

		defer.commit()
		return updated

	def xxx_updateBuildDependencies(self, obsPackageList):
		defer = self.deferCommit()

		for obsPackage in obsPackageList:
			requiringPackage = obsPackage.sourcePackage
			if requiringPackage.backingStoreId is None:
				print(f"Warning: obs package {obsPackage.name} refers to {requiringPackage.fullname()} which has no DB ID")

			for used in obsPackage._usedForBuild:
				requiredPackage = used.sourcePackage
				self.addBuildDependency(requiringPackage, requiredPackage)

			# then update build times
			self.builds.update(obsPackage.name, obsPackage.buildTime)

		defer.commit()

	def addBuildDependency(self, requiringPkg, requiredPkg):
		# print(f"build of {requiringPkg.name} uses {requiredPkg.name}")
		self.buildDep.add(requiringPkgId = requiringPkg.backingStoreId,
				requiredPkgId = requiredPkg.backingStoreId)

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
		return self.retrievePackageById(pinfo.backingStoreId, pinfo.product)

	def retrievePackageById(self, pkgId, product = None):
		pkg = self.packageCache.get(pkgId)
		if pkg is not None:
			return pkg

		d = self.packages.fetchOne(id = pkgId)
		if d is None:
			return None

		return self.constructPackage(pkgId, d, product)

	def constructPackage(self, pkgId, d, product = None):
		if product is None:
			product = self.getCachedProductInfo(d.get('productId'))

		assert(d['id'] == pkgId)

		# As we're recursing below in the case of self._allowDepTreeLookups,
		# it can happen that we've already created an object for this ID.
		# Return that if it exists.
		pkg = self.packageCache.get(pkgId)
		if pkg is not None:
			return pkg

		pkg = self.packages.constructObject(Package, d)
		self.packageCache.put(pkg)

		if d['arch'] not in ('src', 'nosrc'):
			pkg.sourcePackage = self.retrieveSourcePackage(d, product)

		pkg.product = product

		# now do the files
		pkg.files = self.retrievePackageFiles(pkgId)

		# and dependencies
		pkg.requires = self.requires.retrieveDependenciesByPkgId(pkgId)
		pkg.provides = self.provides.retrieveDependenciesByPkgId(pkgId)

		if self._allowDepTreeLookups:
			self.chasePackageDependencies(pkg)

		if self._requireSourceLookups and pkg.sourceBackingStoreId is not None:
			src = self.retrievePackageById(pkg.sourceBackingStoreId, product)
			if src:
				pkg.setSourcePackage(src)

		return pkg

	def chasePackageDependencies(self, pkg):
		resolved = set()

		# this returns a list of (dependencyId, packageId) pairs
		rawRequired = self.tree.retrieveRequired(pkg.backingStoreId)

		# make sure we have proper Package objects in our cache for each requisite
		self.loadPackagesIntoCache(list(pkgId for depId, pkgId in rawRequired))

		for depId, targetId in rawRequired:
			if False:
				# workaround - for what?!
				if depId is None:
					continue

			target = self.packageCache.get(targetId)
			dep = self.requires.retrieveDependencyById(depId)
			assert(depId is None or dep)

			resolved.add((dep, target))

		# really list? or better set?
		pkg.resolvedRequires = list(resolved)

	def loadPackagesIntoCache(self, pkgIdList):
		result = set()

		missing = set()
		for pkgId in pkgIdList:
			pkg = self.packageCache.get(pkgId)
			if pkg is None:
				missing.add(pkgId)
			else:
				result.add(pkg)

		if missing:
			for d in self.packages.selectMultiple([], 'id', list(missing)):
				pkg = self.constructPackage(d['id'], d)
				assert(pkg)
				result.add(pkg)

		return result

	def retrieveSourcePackage(self, d, product = None):
		src = None

		sourceId = d.get('sourceId')
		if sourceId is not None:
			del d['sourceId']
			sd = self.packages.fetchOne(id = sourceId)
			if sd is not None:
				src = self.constructPackage(sourceId, sd, product)

		if src is None:
			pass

		return src

	def enumerateOBSPackages(self):
		result = []
		for d in self.builds.fetchAll():
			buildId = d['id']

			obsPackage = self.obsPackageCache.get(buildId)
			if obsPackage is None:
				obsPackage = self.constructOBSPackage(d)

			result.append(obsPackage)

		return result

	def retrieveOBSPackageById(self, buildId):
		obsPackage = self.obsPackageCache.get(buildId)
		if obsPackage is not None:
			return obsPackage

		d = self.builds.fetchOne(id = buildId)
		if d is None:
			return None

		return self.constructOBSPackage(d)

	def constructOBSPackage(self, d):
		obsPackage = self.builds.constructObject(OBSPackage, d)

		# For now, only the packages that succeed make it into the DB
		obsPackage.buildStatus = OBSPackage.STATUS_SUCCEEDED

		pkgIdList = self.packages.getPackagesForBuild(obsPackage.backingStoreId)
		obsPackage._binaries = list(self.loadPackagesIntoCache(pkgIdList))

		names = [_.shortname for _ in obsPackage._binaries]
		# print(f"{obsPackage.name} -> {', '.join(names)}")

		# discover the build dependencies if desired
		assert(self._allowDepTreeLookups)
		if self._allowDepTreeLookups:
			# this returns a list of (dependencyId, packageId) pairs
			rawRequired = self.buildDep.retrieveRequired(obsPackage.backingStoreId)
			# discard the dependency ID, we didn't store anything useful to begin with
			rawRequired = list(map(lambda pair: pair[1], rawRequired))

			# make sure we have proper Package objects in our cache for each requisite
			self.loadPackagesIntoCache(rawRequired)

			for pkgId in rawRequired:
				target = self.packageCache.get(pkgId)
				if target is None:
					raise Exception(f"{obsPackage.name}: cannot resolve pkgId {pkgId}")
				obsPackage.addBuildRequires(target)

		self.obsPackageCache.put(obsPackage)

		return obsPackage

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
			pinfo = self.constructPackageInfo(d)
			if pinfo is None:
				badPkgId = d.get('id')
				raise Exception(f"cannot build package info for id {badPkgId}")

			result.append(pinfo)
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

		pinfo.product = self.getCachedProductInfo(productId)
		if pinfo.product is None:
			self.packageInfoLacksProduct(pinfo)

		return pinfo

	def getCachedProductInfo(self, productId):
		if productId is None:
			return None

		entry = self.productCache.entryById(productId)
		if entry is not None:
			return entry.object

		return None

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

		# This list contains pairs of PackageDependency, pkgId
		providesList = self.provides.retrieveDependenciesByPkgName(name)

		pkgIdList = list(pair[1] for pair in providesList)

		relevantPkgs = {}
		for pinfo in self.retrieveMultiplePackageInfos(pkgIdList):
			relevantPkgs[pinfo.backingStoreId] = pinfo

		for dep, pkgId in providesList:
			pinfo = relevantPkgs.get(pkgId)
			if pinfo is None:
				print(f" ERROR: {name} {dep} references unknown package {pkgId}")
				continue

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

	def enableDependencyTreeLookups(self):
		self._allowDepTreeLookups = True

	def disableDependencyTreeLookups(self):
		self._allowDepTreeLookups = False

	def enableSourceLookups(self):
		self._requireSourceLookups = True

	def disableSourceLookups(self):
		self._requireSourceLookups = False

	def dependencyTreeExcise(self, pkgIdList):
		self.tree.deleteMultiple('requiringPkgId', pkgIdList)

	def addEdgeToTree(self, **kwargs):
		self.tree.addEdge(**kwargs)

	def addEdgeSetToTree(self, edges):
		defer = self.deferCommit()
		for (source, target, dep) in edges:
			self.tree.addEdge(requiringPkgId = source, requiredPkgId = target, dependencyId = dep)
		defer.commit()

	def fixupLatest(self):
		# when the database is somewhat hosed it's faster to fix it up than rebuilding it from scratch
		self.latest.prune()
