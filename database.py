import sqlite3
from packages import Package, PackageInfo, isSourceArchitecture
from obsclnt import OBSPackage, OBSDependency
from util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

sqlLogger = loggingFacade.getLogger('sql')
sqlDebug = sqlLogger.debug
dbLogger = loggingFacade.getLogger('database')
debugDB = dbLogger.debug

def splitDictKeyValues(d):
	keys = []
	values = []
	for k, v in d.items():
		keys.append(k)
		values.append(v)

	return keys, values

class GenericCache(dict):
	def put(self, id, dep):
		self[id] = dep

class GenericSetCache(dict):
	def put(self, id, dep):
		try:
			self[id].add(dep)
		except:
			self[id] = set()
			self[id].add(dep)

	def get(self, id, default = None):
		res = super().get(id, default)
		return res or []

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

		def __enter__(self):
			pass

		def __exit__(self, *args):
			self.commit()

		def commit(self):
			if self.lock is not None:
				count = self.lock.getAndResetCommitCount()
				self.lock.release()
				self.lock = None

				if count:
					# infomsg(f"Applying {count} deferred commits")
					self.db.commit()

	def __init__(self):
		self.conn = None

		self.commitLock = self.CommitLock()

	def connect(self, db_file):
		try:
			self.conn = sqlite3.connect(db_file)
		except sqlite3.Error as e:
			errormsg(f"Failed to connect to sqlite3 DB {db_file}: {e}")

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

	# FIXME: is this used anywhere?!
	def fetch(self, memoryObject):
		conn = self.db.conn
		sql = f"SELECT * from {self.tableName} where id=?"

		c = conn.cursor()
		c.execute(sql, (self.id, ))

		names = [field[0] for field in c.description]
		for r in c.fetchall():
			for name, value in zip(names, r):
				infomsg(name, value)

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
		self.readonly = False

	def setObjectTemplate(self, templ):
		self.objectTemplate = templ

	def setReadonly(self):
		self.readonly = True

	def execute(self, sqlStatement, values = []):
		if self.readonly:
			verb = sqlStatement.split(maxsplit = 1)[0].upper()
			if verb not in ('SELECT', ):
				raise Exception(f"operation refused on read-only table: {sqlStatement}")

		try:
			c = self.db.conn.cursor()

			sqlDebug(f"SQL: {sqlStatement}")
			if values:
				sqlDebug(f"     {values}")

			c.execute(sqlStatement, values)
		except sqlite3.Error as e:
			errormsg(f"SQL Error: {e} - {type(e)}")
			errormsg(f"The offending statement was: {sqlStatement}")
			raise Exception(f"SQL Error: {e}")

		return c

	def create(self, sqlStatement):
		if not self.execute(sqlStatement):
			errormsg(f"Failed to create table {self.name}")
			return False

		sqlDebug(f"Created table {self.name}")
		return True

	def createIndex(self, name, fields):
		onClause = ", ".join(fields)
		sqlStatement = f"""CREATE INDEX IF NOT EXISTS {name} ON {self.name} ({onClause});"""
		if not self.execute(sqlStatement):
			errormsg(f"Failed to create table index {name} for table {self.name}")
			return False

		sqlDebug(f"SQL Created table index {name} for table {self.name}")
		return True

	def createUniqueIndex(self, name, fields):
		onClause = ", ".join(fields)
		sqlStatement = f"""CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {self.name} ({onClause});"""
		if not self.execute(sqlStatement):
			errormsg(f"Failed to create table index {name} for table {self.name}")
			return False

		sqlDebug(f"Created table index {name} for table {self.name}")
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

		nameFmt = ",".join(keys)
		valueFmt = ",".join(["?"] * len(values))
		sql = f"INSERT INTO {self.name}({nameFmt}) VALUES ({valueFmt})"
		c = self.execute(sql, values)
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

	def delete(self, **selector):
		whereClause, whereValues = self.buildWhereStatement(selector)

		sql = f"DELETE FROM {self.name} {whereClause}"
		self.execute(sql, whereValues)
		self.db.commit()

	def deleteMultiple(self, whereField, values):
		while len(values) > 100:
			self.deleteMultipleWork(whereField, values[:100])
			del values[:100]

		self.deleteMultipleWork(whereField, values)

		# we currently do not commit these changes right away.
		# not sure whether this is a good idea or not.
		# self.db.commit()

	def deleteMultipleWork(self, whereField, values):
		if len(values) == 0:
			return

		sql = f"DELETE FROM {self.name}"

		count = len(values)
		sql += " WHERE (" + " OR ".join([f"{whereField}=?"] * count) + ")"

		self.execute(sql, values)

	def deleteAll(self):
		self.execute(f"DELETE FROM {self.name}")

	def updateMultiple(self, keys, values, whereField, whereValues):
		result = []
		while len(whereValues) > 100:
			self.updateMultipleWork(keys, values, whereField, whereValues[:100])
			del whereValues[:100]

		self.updateMultipleWork(keys, values, whereField, whereValues)

		# we currently do not commit these changes right away.
		# not sure whether this is a good idea or not.
		# self.db.commit()

	def updateMultipleWork(self, keys, values, whereField, whereValues):
		setFmt = ", ".join([f"{name}=?" for name in keys])

		sql = f"UPDATE {self.name} SET {setFmt}"

		count = len(whereValues)
		sql += " WHERE (" + " OR ".join([f"{whereField}=?"] * count) + ")"

		c = self.execute(sql, values + whereValues)
		self.db.commit()

	def clearColumn(self, field):
		sql = f"UPDATE {self.name} SET {field}=NULL"
		self.execute(sql)
		self.db.commit()

	def cursorFetchAll(self, c):
		if c is None:
			return None

		result = []

		names = [field[0] for field in c.description]
		for row in c.fetchall():
			d = dict(zip(names, row))
			result.append(d)

		sqlDebug(f"SQL: found {len(result)} matches")
		return result

	def constructObject(self, klass, d):
		return self.objectTemplate.constructObjectFromDB(klass, d)

class NamedTable(Table):
	OBJECT_TEMPLATE = None

	def __init__(self, db, tableName):
		super().__init__(db, tableName)

		klass = self.__class__

		sql = getattr(klass, 'createTableSQL', None)
		if sql is None:
			fields = klass.TABLE_FIELDS.strip()
			sql = f"""CREATE TABLE IF NOT EXISTS {self.name} (
					{fields}
				);"""
		if not self.create(sql):
			return None

		if klass.OBJECT_TEMPLATE:
			self.setObjectTemplate(klass.OBJECT_TEMPLATE)

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


class ProductTable(NamedTable):
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

# FIXME: the buildId should go away
class PackageTable(NamedTable):
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

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

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

		infomsg(f"Found {len(self.knownPackageIDs)} packages in database")

	def isKnownPackageObject(self, obj):
		key = self.makeKey(obj.name, obj.version, obj.release, obj.arch)
		return self.knownPackageIDs.get(key)

	def addPackageObject(self, obj):
		key = self.makeKey(obj.name, obj.version, obj.release, obj.arch)

		id = self.knownPackageIDs.get(key)
		if id is not None:
			# infomsg(f"{key}: already known as {id}")
			return id

		h = self.insertObject(obj)
		if h is None:
			errormsg(f"Failed to insert {obj.fullname()} into database")
			return None

		self.knownPackageIDs[key] = h.id
		# infomsg(f"{key}: {obj.fullname()} -> {h.id}")
		return h.id

	# FIXME nuke
	def getPackagesForBuild(self, buildId):
		return self.knownBuilds.get(buildId) or []

	def enumeratePackagesAndBuildIds(self):
		c = self.db.conn.cursor()
		c.execute("SELECT id, name, epoch, version, release, arch, buildId FROM packages;")
		for row in c.fetchall():
			(id, name, epoch, version, release, arch, buildId) = row
			pinfo = PackageInfo(name, epoch, version, release, arch, id)
			yield (pinfo, buildId)

	def enumeratePackagesAndBuildTimes(self):
		c = self.db.conn.cursor()
		c.execute("SELECT id, name, epoch, version, release, arch, buildTime FROM packages;")
		for row in c.fetchall():
			(id, name, epoch, version, release, arch, buildTime) = row
			pinfo = PackageInfo(name, epoch, version, release, arch, id)
			yield (pinfo, buildTime)

	def updateBuildMembership(self, buildId, rpmIds):
		# Clear any RPMs referencing this build
		# UPDATE packages SET buildId=None WHERE buildId=NNN
		self.updateKeysAndValues(['buildId'], [None], buildId = buildId)

		# Insert new references to this build
		# UPDATE packages SET buildId=NNN WHERE id=[rpmIds]
		if rpmIds:
			self.updateMultiple(['buildId'], [buildId], 'id', rpmIds)

		self.knownBuilds[buildId] = rpmIds.copy()

	@property
	def allValidIds(self):
		return set(self.knownPackageIDs.values())

##################################################################
# This table tracks the latest known version of any (non-source)
# package.
# FIXME: we should probably distinguish by name+version rather
# than just the name. Some architectures have 32bit and 64bit
# versions of the same package.
##################################################################
class LatestPackageTable(NamedTable):
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
		def __init__(self, name):
			self.name = name
			self.id = None
			self.pinfo = None

		def update(self, pkg):
			if self.pinfo is not None and self.pinfo.backingStoreId == pkg.backingStoreId:
				return False
			self.pinfo = pkg
			return True

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._binaries = {}
		self._sources = {}
		self._id2bucket = {}
		self._duplicates = []
		self._latestIds = None

	@property
	def knownPackages(self):
		for id, b in sorted(self._binaries.items()):
			yield b.pinfo

	@property
	def duplicatePackages(self):
		return iter(self._duplicates)

	def fetchKnownPackages(self, store):
		for d in self.fetchAll():
			pinfo = store.constructPackageInfo(d, d['pkgId'])
			bucket = self.createBucket(pinfo.name, pinfo.arch)
			if bucket.pinfo is not None:
				self._duplicates.append(bucket.pinfo)

			bucket.pinfo = pinfo
			self.setBucketId(bucket, d['id'])

		self._latestIds = None

	def setBucketId(self, bucket, id):
		bucket.id = id
		self._id2bucket[id] = bucket

	@staticmethod
	def lookupPackageInfo(packageDict, name):
		b = packageDict.get(name)
		if b is not None:
			return b.pinfo
		return None

	def getPackageByName(self, name):
		return self.lookupPackageInfo(self._binaries, name)

	def getBinaryPackageByName(self, name):
		return self.lookupPackageInfo(self._binaries, name)

	def getBinaryPackagesByMatch(self, match):
		for name, b in self._binaries.items():
			if match.match(name):
				yield b.pinfo

	def getSourcePackageByName(self, name):
		return self.lookupPackageInfo(self._sources, name)

	# FIXME: something is wrong here; we end up with duplicate entries in the DB
	def update(self, pkg):
		assert(pkg.backingStoreId)

		b = self.createBucket(pkg.name, pkg.arch)
		if b.update(pkg):
			sqlDebug(f"updating latest {pkg} from id={b.id} to {pkg.fullname()} id={pkg.backingStoreId}")

			if b.id is None:
				h = self.insertObject(pkg)
				assert(h)
				self.setBucketId(b, h.id)
			else:
				self.updateObject(pkg, id = b.id)

			# data changed, invalidate the cached list of current package ids
			self._latestIds = None
		return b

	@property
	def currentPackageIds(self):
		if self._latestIds is None:
			self._latestIds = set(bucket.pinfo.backingStoreId for bucket in self._binaries.values())
		return self._latestIds

	def xxx_packageIsLatest(self, pinfo):
		return pinfo.backingStoreId in self.currentPackageIds

	def getBucket(self, name, arch):
		if isSourceArchitecture(arch):
			pkgDict = self._sources
		else:
			pkgDict = self._binaries

		return pkgDict.get(name)

	def createBucket(self, name, arch):
		if isSourceArchitecture(arch):
			pkgDict = self._sources
		else:
			pkgDict = self._binaries

		b = pkgDict.get(name)
		if b is None:
			b = self.Latest(name)
			pkgDict[name] = b
		return b

	def allocateIdForRpm(self, rpm):
		b = self.getBucket(rpm.name, rpm.arch)
		if b is None:
			unversionedRpm = PackageInfo(name = rpm.name, version = 'any', release = 'any', arch = rpm.arch, epoch = None, backingStoreId = -1)
			b = self.update(unversionedRpm)

		return b.id

	def getIdForRpm(self, rpm):
		b = self.getBucket(rpm.name, rpm.arch)
		if b is None:
			# XXX FIXME HACK: temporarily tolerate inconsistent 'latest' information for src rpms
			if rpm.isSourcePackage:
				return None
			raise Exception(f"No entry in table '{self.name}' for {rpm.fullname()}")

		if False:
			# Does package X require a version of Y that is not the latest?
			# This is not an error; it happens a lot due to the way we update the DB
			if b.pinfo.backingStoreId != rpm.backingStoreId:
				raise Exception(f"Conflicting entry in table 'latest' for {rpm.fullname()}: found {b.pinfo.fullname()} instead")

		return b.id

	def getPackageInfoForId(self, id):
		b = self._id2bucket.get(id)
		if b is not None:
			return b.pinfo

	def prune(self):
		idsToDrop = []
		for pinfo in self._duplicates:
			infomsg(f"  \"{self.name}\" contains redundant entry {pinfo}")
			idsToDrop.append(pinfo.backingStoreId)

		if not idsToDrop:
			infomsg(f"Table \"{self.name}\" does not contain any redundant entries")
			return

		infomsg(f"Dropping {len(idsToDrop)} redundant entries from table {self.name}")
		self.deleteMultiple('pkgId', idsToDrop)
		self.db.commit()

		self._duplicates = []


class BuildTable(NamedTable):
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
			if buildTime and self.buildTime == buildTime:
				return False
			self.buildTime = buildTime
			return True

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
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


class FilesTable(NamedTable):
	createTableSQL = """CREATE TABLE IF NOT EXISTS files (
				id integer PRIMARY KEY,
				pkgId integer,
				path text
			);"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._cacheByPkgId = None
		self._cacheByPath = None

	def setReadonly(self):
		super().setReadonly()

		self._cacheByPkgId = GenericSetCache()
		self._cacheByPath = GenericSetCache()

		for d in self.fetchAll():
			self._cacheByPkgId.put(d['pkgId'], d['path'])
			self._cacheByPath.put(d['path'], d['pkgId'])

	def addFiles(self, pathList, pkgID):
		keys = ('pkgId', 'path')
		for path in pathList:
			self.insertKeysAndValues(keys, (pkgID, path))

	def pathsForPackage(self, pkgId):
		if self._cacheByPkgId is not None:
			return self._cacheByPkgId.get(pkgId)

		return self.fetchColumn('path', pkgId = pkgId)

	def packagesForPath(self, path):
		if self._cacheByPath is not None:
			return self._cacheByPath.get(path)

		return self.fetchColumn('pkgId', path = path)

class DependencyTable(NamedTable):
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			pkgId integer,
			name text,
			op text,
			epoch text,
			version text,
			release text
		"""

	Cache = GenericCache

	def __init__(self, *args):
		super().__init__(*args)
		self._cacheById = self.Cache()
		self._cacheByPkgId = None
		self._cacheByPkgName = None
		self._knownPackages = []
		self._allCached = False

	def setReadonly(self):
		super().setReadonly()

		for d in self.fetchAll():
			self.constructDependency(d)

		self._cacheByPkgId = GenericSetCache()
		for dep in self._cacheById.values():
			self._cacheByPkgId.put(dep.pkgId, dep)

		self._cacheByPkgName = GenericSetCache()
		for dep in self._cacheById.values():
			self._cacheByPkgName.put(dep.name, dep)

		self._allCached = True

	def fetchKnownPackages(self):
		self._knownPackages = set(self.fetchColumn('pkgId'))
		# infomsg(f"We have dependencies for {len(self._knownPackages)} packages")

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
		dep = self._cacheById.get(id)
		if dep or self._allCached:
			return dep

		assert(id)
		d = self.fetchOne(id = id)
		if d is None:
			return None
		return self.constructDependency(d)

	def retrieveDependenciesByPkgId(self, pkgId):
		if self._cacheByPkgId is not None:
			return self._cacheByPkgId.get(pkgId)

		result = []

		for d in self.fetchAll(pkgId = pkgId):
			result.append(self.constructDependency(d))
		return result

	def retrieveDependenciesByPkgName(self, name):
		if self._cacheByPkgName is not None:
			return self._cacheByPkgName.get(name)

		result = []

		for d in self.fetchAll(name = name):
			result.append((self.constructDependency(d), d['pkgId']))
		return result

	def constructDependency(self, d):
		id = d['id']

		dep = self._cacheById.get(id)
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

		# infomsg(args)
		dep = Package.createDependency(**args)
		self._cacheById.put(id, dep)
		return dep

class DirectedGraphTable(NamedTable):
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			requiringPkgId integer NOT NULL,
			requiredPkgId integer NOT NULL,
			dependencyId integer
		"""

	def __init__(self, *args):
		super().__init__(*args)
		self._knownPackages = []
		self._cacheByRequringPkg = None

	def setReadonly(self):
		super().setReadonly()

		self._cacheByRequringPkg = GenericSetCache()
		for d in self.fetchAll():
			# self._cacheByRequringPkg.put(d['requiringPkgId'], (d['dependencyId'], d['requiredPkgId']))
			self._cacheByRequringPkg.put(d['requiringPkgId'], d['requiredPkgId'])

	def fetchKnownPackages(self):
		self._knownPackages = set(self.fetchColumn('requiringPkgId'))
		# infomsg(f"{self.name}: we have dependencies for {len(self._knownPackages)} packages")

	def removeDependenciesForPkgId(self, pkgId):
		self.delete(requiringPkgId = pkgId)
		self._knownPackages.discard(pkgId)

	def removeReverseDependenciesForPkgId(self, pkgId):
		self.delete(requiredPkgId = pkgId)
		self._knownPackages.discard(pkgId)

	def removeDependenciesForList(self, idList):
		self.deleteMultiple('requiringPkgId', idList)
		self._knownPackages.difference_update(set(idList))

	def haveDependencies(self, pkgId):
		assert(pkgId is not None)
		return pkgId in self._knownPackages

	def addEdge(self, **kwargs):
		self.insert(**kwargs)

	# This returns a list of indices for table "latest"
	def retrieveRequired(self, pkgId):
		if self._cacheByRequringPkg is not None:
			return self._cacheByRequringPkg.get(pkgId)

		c = self.selectFrom(fields = ('requiredPkgId', ), selector = {'requiringPkgId':  pkgId})
		if c is None:
			return None

		result = []
		for row in c.fetchall():
			result.append(row[0])

		return result

class BuildPackageRelationTable(NamedTable):
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			buildId integer NOT NULL,
			pkgId integer NOT NULL
		"""

	def __init__(self, *args):
		super().__init__(*args)
		self._knownBuilds = []
		self._cacheByBuildId = None
		self._cacheByPkgId = None

	def setReadonly(self):
		super().setReadonly()

		self._cacheByBuildId = GenericSetCache()
		self._cacheByPkgId = GenericCache()
		for d in self.fetchAll():
			self._cacheByBuildId.put(d['buildId'], d['pkgId'])
			self._cacheByPkgId.put(d['pkgId'], d['buildId'])

	def fetchKnownBuilds(self):
		self._knownBuilds = set(self.fetchColumn('buildId'))

	def removePackagesForList(self, idList):
		self.deleteMultiple('buildId', idList)
		self._knownBuilds.difference_update(set(idList))

	def havePackages(self, pkgId):
		assert(pkgId is not None)
		return pkgId in self._knownBuilds

	def addPackage(self, **kwargs):
		self.insert(**kwargs)

	def removeRelation(self, buildId, pkgId):
		self.delete(buildId = buildId, pkgId = pkgId)

	def retrievePackagesForBuild(self, buildId):
		if self._cacheByBuildId is not None:
			return self._cacheByBuildId.get(buildId)

		c = self.selectFrom(fields = ('pkgId', ), selector = {'buildId':  buildId})
		if c is None:
			raise Exception(f"SQL query failed")

		return [row[0] for row in c.fetchall()]

	def retrieveBuildForPackage(self, pkgId):
		if self._cacheByPkgId is not None:
			return self._cacheByPkgId.get(pkgId)

		c = self.selectFrom(fields = ('buildId', ), selector = {'pkgId':  pkgId})
		if c is None:
			raise Exception(f"SQL query failed")

		found = list(row[0] for row in c.fetchall())
		if len(found) == 0:
			return None
		if len(found) > 1:
			warnmsg(f"DB inconsistency: multiple {self.name} entries for package {pkgId}")
			return None
		return found[0]

	def enumerate(self):
		c = self.selectFrom(fields = ('buildId', 'pkgId' ), selector = None)
		if c is None:
			raise Exception(f"unable to iterate over table {self.name}")

		for row in c.fetchall():
			yield row

class KeyValueTable(NamedTable):
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

class DependencyStringTable(NamedTable):
	TABLE_FIELDS = """
			id integer PRIMARY KEY,
			expression string
		"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._idToString = {}
		self._stringToId = {}

	def onLoad(self):
		for d in self.fetchAll():
			(id, expression) = (d['id'], d['expression'])
			self._idToString[id] = expression
			self._stringToId[expression] = id

		for id, expression in self._idToString.items():
			assert(self._stringToId[expression] == id)

	def add(self, expression):
		id = self._stringToId.get(expression)
		if id is None:
			h = self.insert(expression = expression)
			self._stringToId[expression] = h.id
			self._idToString[id] = expression
		return id

	def idToString(self, id):
		return self._idToString.get(id)

	def stringToId(self, expression):
		return self._stringToId.get(expression)

class PackageCache(dict):
	def put(self, pkg):
		assert(pkg.backingStoreId is not None)
		self[pkg.backingStoreId] = pkg

	def drop(self, pkg):
		try:
			del self[pkg.backingStoreId]
		except:
			pass

class ProvidesCache(dict):
	def put(self, name, packages):
		try:
			self[name] += packages
		except:
			self[name] = [] + packages

class BackingStoreDB(DB):
	def __init__(self, path):
		super().__init__()

		self.readonly = False

		if not self.connect(path):
			raise Exception(f"Unable to open database at {path}")

		self.productCache = ProductCache()
		self.packageProductLink = {}

		self.products = ProductTable(self, 'products')
		self.products.createIndex("idx_prod_key", ["key"])
		self.products.populateCache(self.productCache)

		self.packages = PackageTable(self, 'packages')
		self.packages.createIndex("idx_pkg_name", ["name"])
		self.packages.createIndex("idx_pkg_product", ["productId"])
		# FIXME: rename to idx_pkg_hash
		self.packages.createUniqueIndex("id_pkg_hash", ["repoPackageID"])
		self.packages.updateKnownIDs()

		self.latest = LatestPackageTable(self, 'latest')
		self.latest.createIndex("idx_latest_name", ["name"])
		self.latest.fetchKnownPackages(self)

		self.builds = BuildTable(self, 'builds')
		self.builds.createUniqueIndex("idx_build_name", ["name"])
		self.builds.fetchKnownPackages(self)

		# FIXME:
		# Instead of mapping strings to package ids, it might be
		# more compact if we create tables to map file names
		# and version/release strings to an ID, and have
		# files and dependency tables just refer to these strings.

		self.files = FilesTable(self, 'files')
		self.files.createIndex("idx_file_package", ["pkgId"])

		self.requires = DependencyTable(self, 'requires')
		self.requires.createIndex("idx_req_package", ['pkgId'])
		self.requires.fetchKnownPackages()

		self.provides = DependencyTable(self, 'provides')
		self.provides.createIndex("idx_prov_package", ['pkgId'])
		self.provides.fetchKnownPackages()

		self.dependencies = DependencyStringTable(self, 'depstrings')
		self.dependencies.onLoad()

		self.tree = DirectedGraphTable(self, 'tree')
		self.tree.createIndex("idx_tree_down", ['requiringPkgId'])
		self.tree.createIndex("idx_tree_up", ['requiredPkgId'])
		self.tree.fetchKnownPackages()

		self.fulltree = DirectedGraphTable(self, 'fulltree')
		self.fulltree.createIndex("idx_fulltree_down", ['requiringPkgId'])
		self.fulltree.createIndex("idx_fulltree_up", ['requiredPkgId'])

		self.buildDep = DirectedGraphTable(self, 'builddep')
		self.buildDep.createIndex("idx_bdep_down", ['requiringPkgId'])

		self.buildPkgRelation = BuildPackageRelationTable(self, 'buildpkgs')
		self.buildPkgRelation.createIndex("idx_build_pkg", ['buildId'])
		self.buildPkgRelation.fetchKnownBuilds()

		self.keyValueStore = KeyValueTable(self, 'keyvalue')
		self.keyValueStore.onLoad()

		self.packageCache = PackageCache()
		self.obsPackageCache = PackageCache()
		self.providesCache = ProvidesCache()

		self._allowDepTreeLookups = False
		self._requireSourceLookups = False

	def setReadonly(self):
		if self.readonly:
			return
		self.readonly = True

		self.packages.setReadonly()
		self.latest.setReadonly()
		self.builds.setReadonly()
		self.requires.setReadonly()
		self.provides.setReadonly()
		self.files.setReadonly()
		self.buildPkgRelation.setReadonly()
		self.buildDep.setReadonly()
		self.tree.setReadonly()

		packages = []
		for d in self.packages.fetchAll():
			pkg = self.constructPackage(d['id'], d, deferCompleteInitialization = True)
			packages.append(pkg)

		for pkg in packages:
			self.constructPackageInternal(pkg.backingStoreId, pkg)

		self.enumerateOBSPackages()

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
			infomsg(f"Found new product {entry.key}, mapped to ID {id}")

		assert(self.productCache.entryById(entry.id) == entry)

		entry.object = release

		self.resolvePackageInfoProduct(entry.id, release)

		return entry.id

	def addPackage(self, **kwargs):
		assert('id' not in kwargs)
		return self.packages.insert(**kwargs)

	def isKnownPackageObject(self, obj):
		assert(obj is not None)

		if obj.backingStoreId is not None:
			return True

		id = self.packages.isKnownPackageObject(obj)
		if id is not None:
			obj.backingStoreId = id
			return True
		return False

	def havePackageDependencies(self, obj):
		return self.provides.haveDependencies(obj.backingStoreId) or \
			self.tree.haveDependencies(obj.backingStoreId)

	def addPackageObject(self, obj, updateLatest = False):
		if obj.backingStoreId is None:
			obj.backingStoreId = self.packages.addPackageObject(obj)
			if obj.backingStoreId is None:
				raise Exception(f"{obj.fullname()} has no database id")

		if updateLatest:
			debugDB(f"Updating {self.latest.name} table for {obj} -> {obj.fullname()}")
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
					infomsg(f"Cannot add dependency to DB: {obj.fullname()} requires {required.fullname()}, but the latter has no ID set")
					continue

				# infomsg(f"{obj.fullname()} -> {required.fullname()}")
				edges.add((obj.backingStoreId, required.backingStoreId, None))

			for source, target, depId in edges:
				self.tree.addEdge(requiringPkgId = source, requiredPkgId = target, dependencyId = depId)

	def updateSimplifiedDependencies(self, rpmObj, dependencyList):
		rpmLatestId = self.latest.getIdForRpm(rpmObj)

		# XXX FIXME HACK: temporarily tolerate inconsistent 'latest' information for src rpms
		if rpmLatestId is None:
			if rpmObj.isSourcePackage:
				return

			raise Exception(f"{rpmObj.fullname()}: no entry in table {self.latest.name}")

		self.tree.delete(requiringPkgId = rpmLatestId)
		for dep in dependencyList:
			if dep.backingStoreId is None and dep.expression:
				dep.backingStoreId = self.dependencies.add(dep.expression)
			for requiredId in map(self.latest.getIdForRpm, dep.packages):
				self.tree.insert(dependencyId = dep.backingStoreId, requiringPkgId = rpmLatestId, requiredPkgId = requiredId)

	@classmethod
	def updateForwardDependenciesWork(klass, tree, requiringRpm, requiredObjs):
		assert(requiringRpm.backingStoreId)
		sourceId = requiringRpm.backingStoreId

		requiredIds = set(req.backingStoreId for req in requiredObjs)
		if not all(requiredIds):
			for required in requiredObjs:
				if required.backingStoreId is None:
					raise Exception(f"Cannot add dependency to DB: {requiringRpm.fullname()} requires {required.fullname()}, but the latter has no ID set")

		tree.removeDependenciesForPkgId(sourceId)
		for targetId in requiredIds:
			tree.addEdge(requiringPkgId = sourceId, requiredPkgId = targetId)

	@classmethod
	def updateReverseDependencyWork(klass, tree, requiredRpm, requiringObjs):
		assert(requiredRpm.backingStoreId)
		targetId = requiredRpm.backingStoreId

		requiringIds = set(req.backingStoreId for req in requiringObjs)
		if not all(requiringIds):
			for requiring in requiringObjs:
				if requiring.backingStoreId is None:
					raise Exception(f"Cannot add dependency to DB: {requiredRpm.fullname()} is required by {requiring.fullname()}, but the latter has no ID set")

		tree.removeReverseDependenciesForPkgId(targetId)
		for sourceId in requiringIds:
			tree.addEdge(requiringPkgId = sourceId, requiredPkgId = targetId)

	def updateForwardDependencyFullTree(self, rpmObj, requiredObjs):
		self.updateForwardDependenciesWork(self.fulltree, rpmObj, requiredObjs)

	def updateReverseDependencyFullTree(self, rpmObj, requiredObjs):
		self.updateReverseDependenciesWork(self.fulltree, rpmObj, requiredObjs)

	def updateForwardDependency(self, rpmObj, requiredObjs):
		self.updateForwardDependenciesWork(self.tree, rpmObj, requiredObjs)

	def updateReverseDependency(self, rpmObj, requiredObjs):
		self.updateReverseDependenciesWork(self.tree, rpmObj, requiredObjs)

	def updateDependencyTree(self, rpmObj, dependencyList):
		assert(rpmObj.backingStoreId)
		rpmId = rpmObj.backingStoreId

		debugDB(f"{rpmObj.fullname()} [id={rpmId}]: updating {len(dependencyList)} {self.fulltree.name} dependencies")
		for dep in dependencyList:
			if dep.expression and not dep.backingStoreId:
				dep.backingStoreId = self.dependencies.add(dep.expression)
			for rpm in dep.packages:
				assert(rpm.backingStoreId is not None)

		self.fulltree.delete(requiringPkgId = rpmId)
		for dep in dependencyList:
			for rpm in dep.packages:
				self.fulltree.insert(dependencyId = dep.backingStoreId,
						requiringPkgId = rpmId,
						requiredPkgId = rpm.backingStoreId)

	def retrieveForwardDependenciesFullTree(self, rpmObj):
		assert(rpmObj.backingStoreId)

		resultDict = {}
		result = []
		for d in self.fulltree.fetchAll(('dependencyId', 'requiredPkgId'), requiringPkgId = rpmObj.backingStoreId):
			depId = d['dependencyId']
			if depId is None:
				# already resolved any ambiguities and/or didn't care to record the actual dep expression
				dep = OBSDependency(expression = None)
				result.append(dep)
			else:
				dep = resultDict.get(depId)
				if dep is None:
					expression = self.dependencies.idToString(depId)
					dep = OBSDependency(expression = expression, backingStoreId = depId)
					resultDict[depId] = dep

			requiredRpm = self.retrievePackageById(d['requiredPkgId'], product = rpmObj.product)
			assert(requiredRpm)

			dep.packages.add(requiredRpm)

		return result + list(resultDict.values())

	def retrieveReverseDependenciesFullTree(self, rpmObj):
		assert(rpmObj.backingStoreId)

		resultDict = {}
		result = []
		for d in self.fulltree.fetchAll(('dependencyId', 'requiringPkgId'), requiredPkgId = rpmObj.backingStoreId):
			depId = d['dependencyId']
			if depId is None:
				# already resolved any ambiguities and/or didn't care to record the actual dep expression
				dep = OBSDependency(expression = None)
				result.append(dep)
			else:
				dep = resultDict.get(depId)
				if dep is None:
					expression = self.dependencies.idToString(depId)
					dep = OBSDependency(expression = expression, backingStoreId = depId)
					resultDict[depId] = dep

			requiringRpm = self.retrievePackageById(d['requiringPkgId'], product = rpmObj.product)
			assert(requiringRpm)

			dep.packages.add(requiringRpm)

		return result + list(resultDict.values())

	def retrieveForwardDependenciesTree(self, rpmObj):
		rpmLatestId = self.latest.getIdForRpm(rpmObj)

		resultDict = {}
		result = []
		for d in self.tree.fetchAll(('dependencyId', 'requiredPkgId'), requiringPkgId = rpmLatestId):
			depId = d['dependencyId']
			if depId is None:
				# already resolved any ambiguities and/or didn't care to record the actual dep expression
				dep = OBSDependency(expression = None)
				result.append(dep)
			else:
				dep = resultDict.get(depId)
				if dep is None:
					expression = self.dependencies.idToString(depId)
					dep = OBSDependency(expression = expression, backingStoreId = depId)
					resultDict[depId] = dep

			pinfo = self.latest.getPackageInfoForId(d['requiredPkgId'])
			assert(pinfo)

			requiredRpm = self.retrievePackage(pinfo)
			assert(requiredRpm)

			dep.packages.add(requiredRpm)

		return result + list(resultDict.values())

	def retrieveReverseDependenciesTree(self, rpmObj):
		rpmLatestId = self.latest.getIdForRpm(rpmObj)

		resultDict = {}
		result = []
		for d in self.tree.fetchAll(('dependencyId', 'requiringPkgId'), requiredPkgId = rpmLatestId):
			depId = d['dependencyId']
			if depId is None:
				# already resolved any ambiguities and/or didn't care to record the actual dep expression
				dep = OBSDependency(expression = None)
				result.append(dep)
			else:
				dep = resultDict.get(depId)
				if dep is None:
					expression = self.dependencies.idToString(depId)
					dep = OBSDependency(expression = expression, backingStoreId = depId)
					resultDict[depId] = dep

			pinfo = self.latest.getPackageInfoForId(d['requiringPkgId'])
			assert(pinfo)

			requiredRpm = self.retrievePackage(pinfo)
			assert(requiredRpm)

			dep.packages.add(requiredRpm)

		return result + list(resultDict.values())

	def updatePackageSource(self, obj):
		sourceId = obj.sourceBackingStoreId
		if sourceId is None:
			fail

		if obj.backingStoreId is None:
			raise Exception(f"Cannot update sourceId for {obj.fullname()} - backingStoreId is not set")

		keys = ['sourceId']
		values = [sourceId]
		self.packages.updateKeysAndValues(keys, values, id = obj.backingStoreId)

	# still needed?
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
			infomsg(f"Updating source for {obj.shortname}")
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
				errormsg(f"Unable to add OBS Build {obsPackage.name} to DB")
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
			if not all(rpm.backingStoreId for rpm in obsPackage.binaries):
				errormsg(f"Refusing to update table {self.builds.name} for OBS package {obsPackage.name}: not all rpms are in the database yet")
				continue

			id = self.builds.update(obsPackage.name, obsPackage.buildTime)
			if id is None:
				errormsg(f"Unable to update table {self.builds.name} for OBS package {obsPackage.name}")
				continue

			obsPackage.backingStoreId = id
			updated.append(obsPackage)

		# remove all entries from the build->package relation table for the builds we're updating
		updatedBuildIds = list(map(lambda obp: obp.backingStoreId, updated))
		self.buildPkgRelation.removePackagesForList(updatedBuildIds)

		for obsPackage in updated:
			for rpm in obsPackage.binaries:
				self.buildPkgRelation.addPackage(buildId = obsPackage.backingStoreId, pkgId = rpm.backingStoreId)

		defer.commit()
		return updated

	def xxx_updateBuildDependencies(self, obsPackageList):
		defer = self.deferCommit()

		for obsPackage in obsPackageList:
			requiringPackage = obsPackage.sourcePackage
			if requiringPackage.backingStoreId is None:
				warnmsg(f"obs package {obsPackage.name} refers to {requiringPackage.fullname()} which has no DB ID")

			for used in obsPackage._usedForBuild:
				requiredPackage = used.sourcePackage
				self.addBuildDependency(requiringPackage, requiredPackage)

			# then update build times
			self.builds.update(obsPackage.name, obsPackage.buildTime)

		defer.commit()

	def addBuildDependency(self, requiringPkg, requiredPkg):
		# infomsg(f"build of {requiringPkg.name} uses {requiredPkg.name}")
		self.buildDep.add(requiringPkgId = requiringPkg.backingStoreId,
				requiredPkgId = requiredPkg.backingStoreId)

	def enumerateLatestPackages(self):
		for build in self.enumerateOBSPackages():
			src = build.sourcePackage
			for rpm in build.binaries:
				if rpm.isSourcePackage:
					continue
				if rpm.sourcePackage is None:
					rpm.sourcePackage = src
				yield rpm

	def recoverLatestPackageByName(self, name):
		pinfo = self.latest.getBinaryPackageByName(name)
		if pinfo is None:
			return None
		return self.retrievePackage(pinfo)

	def recoverLatestPackagesByMatch(self, match):
		for pinfo in self.latest.getBinaryPackagesByMatch(match):
			yield self.retrievePackage(pinfo)

	def retrievePackage(self, pinfo):
		if pinfo.backingStoreId is None:
			raise Exception(f"DB: cannot retrieve package {pinfo.fullname()}: no backing store Id")

		if isinstance(pinfo, Package):
			return pinfo

		pkg = self.retrievePackageById(pinfo.backingStoreId, pinfo.product)

		if pkg and pkg.fullname() != pinfo.fullname():
			raise Exception(f"DB inconsistency. Looking up {pinfo.fullname()} returned {pkg.fullname()}")

		return pkg

	def retrievePackageById(self, pkgId, product = None):
		pkg = self.packageCache.get(pkgId)
		if pkg is not None:
			return pkg

		d = self.packages.fetchOne(id = pkgId)
		if d is None:
			return None

		return self.constructPackage(pkgId, d, product)

	def constructPackage(self, pkgId, d, product = None, deferCompleteInitialization = False):
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
		pkg.product = product

		self.packageCache.put(pkg)

		if not deferCompleteInitialization:
			self.constructPackageInternal(pkgId, pkg)

		return pkg

	def constructPackageInternal(self, pkgId, pkg):
		if not pkg.isSourcePackage:
			pkg.sourcePackage = self.retrieveSourcePackage(pkg.sourceBackingStoreId, pkg.product)

		# now do the files
		pkg.files = self.retrievePackageFiles(pkgId)

		# and dependencies
		pkg.requires = self.requires.retrieveDependenciesByPkgId(pkgId)
		pkg.provides = self.provides.retrieveDependenciesByPkgId(pkgId)

		if self._allowDepTreeLookups:
			self.chasePackageDependencies(pkg)

		if self._requireSourceLookups and pkg.sourceBackingStoreId is not None:
			src = self.retrievePackageById(pkg.sourceBackingStoreId, pkg.product)
			if src:
				pkg.setSourcePackage(src)

		# FIXME: cleanup
		# locate the OBS package from which this was built
		if False:
			buildId = self.buildPkgRelation.retrieveBuildForPackage(pkgId)
			if buildId is None:
				warnmsg(f"{pkg.fullname()} (id {pkgId}) not tracked by any build")
			elif pkg.obsBuildId == buildId:
				pass
			elif buildId is not None:
				pkg.obsBuildId = buildId
			else:
				warnmsg(f"{pkg.fullname()} (id {pkgId}) does not seem to belong to any build (previously {pkg.obsBuildId})")

	# Construct the package's resolvedRequires from the DB
	# FIXME: maybe resolvedRequires should be a set of OBSDependency objects rather than this
	# odd list of tuples that we have right now.
	def chasePackageDependencies(self, pkg):
		latestId = self.latest.getIdForRpm(pkg)

		# XXX FIXME HACK: temporarily tolerate inconsistent 'latest' information for src rpms
		if latestId is None:
			pkg.resolvedRequires = []
			return

		debugDB(f"chasing package dependencies for {pkg} latestID={latestId}")

		resolved = set()

		# This returns a list of indices into "latest"
		for id in self.tree.retrieveRequired(latestId):
			# map "latest" id to package info
			pinfo = self.latest.getPackageInfoForId(id)
			if pinfo is None:
				raise Exception(f"{pkg} requires latest pkg id {id}, but I could not find it")

			target = self.retrievePackage(pinfo)
			resolved.add((None, target))

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

	def retrieveSourcePackage(self, sourceId, product = None):
		if sourceId is None:
			return None

		src = self.packageCache.get(sourceId)
		if src is None:
			sd = self.packages.fetchOne(id = sourceId)
			if sd is not None:
				src = self.constructPackage(sourceId, sd, product)

		return src

	def enumerateOBSPackages(self):
		# FIXME: in readonly mode, once we've completed all loads, this routine should just
		# iterate over obsPackageCache
		result = []
		for d in self.builds.fetchAll():
			buildId = d['id']

			obsPackage = self.obsPackageCache.get(buildId)
			if obsPackage is None:
				obsPackage = self.constructOBSPackage(d)

			result.append(obsPackage)

		return result

	def retrieveOBSPackageByBuildId(self, buildId):
		obsPackage = self.obsPackageCache.get(buildId)
		if obsPackage is not None:
			return obsPackage

		d = self.builds.fetchOne(id = buildId)
		if d is None:
			return None

		return self.constructOBSPackage(d)

	def retrieveOBSPackageByPackageId(self, pkgId):
		buildId = self.buildPkgRelation.retrieveBuildForPackage(pkgId)
		if buildId is None:
			return None

		return self.retrieveOBSPackageByBuildId(buildId)

	def insertOBSPackage(self, obsBuild):
		# We use .canonicalName rather than .name here. The build's original name may be
		# foo.12345 or foo.12345:flavor. We want the DB to reflect the "true" name of
		# the build, such as "foo" or "foo:flavoe"
		canonicalName = obsBuild.canonicalName

		id = self.builds.update(canonicalName, None)
		if id is None:
			raise Exception(f"Unable to update table {self.builds.name} for OBS package {canonicalName}")

		obsBuild.backingStoreId = id

	def updateOBSPackage(self, build, rpms, updateTimestamp = False):
		self.obsPackageCache.drop(build)

		rpmIds = [rpm.backingStoreId for rpm in rpms]
		if not all(rpmIds):
			for rpm in rpms:
				if rpm.backingStoreId is None:
					errormsg(f"{build}: {rpm.fullname()} is not in the database")
			raise Exception(f"Cannot update {build}: at least one RPM lacks a DB Id")

		if build.backingStoreId is None:
			self.insertOBSPackage(build)

		self.packages.updateBuildMembership(build.backingStoreId, rpmIds)

		# update the build->package relation table as well
		self.buildPkgRelation.removePackagesForList([build.backingStoreId])

		for rpm in rpms:
			self.buildPkgRelation.addPackage(buildId = build.backingStoreId, pkgId = rpm.backingStoreId)

		if updateTimestamp:
			self.builds.updateDict({'buildTime': build.buildTime, 'sourcePackageId' : 0}, id = build.backingStoreId)

	def constructOBSPackage(self, d):
		obsPackage = self.builds.constructObject(OBSPackage, d)

		assert(d['buildTime'] == obsPackage.buildTime)

		# For now, only the packages that succeed make it into the DB
		obsPackage.buildStatus = OBSPackage.STATUS_SUCCEEDED

		pkgIdList = self.buildPkgRelation.retrievePackagesForBuild(obsPackage.backingStoreId)
		if pkgIdList is None:
			raise Exception(f"OBS package {obsPackage} (id {obsPackage.backingStoreId}) does not seem to produce any packages")
		else:
			obsPackage._binaries = list(self.loadPackagesIntoCache(pkgIdList))

		names = [_.shortname for _ in obsPackage._binaries]
		# infomsg(f"{obsPackage.name} -> {', '.join(names)}")

		# discover the build dependencies if desired
		if self._allowDepTreeLookups:
			rawRequired = self.buildDep.retrieveRequired(obsPackage.backingStoreId)

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
		return self.files.pathsForPackage(pkgId)

	def retrieveFileProviders(self, path):
		return self.files.packagesForPath(path)

	def retrievePackageInfo(self, pkgId):
		d = self.packages.fetchOne(id = pkgId)
		if d is None:
			return None

		return self.constructPackageInfo(d, d['id'])

	def retrieveMultiplePackageInfos(self, pkgIdList):
		result = []
		for d in self.packages.selectMultiple([], 'id', pkgIdList):
			pinfo = self.constructPackageInfo(d, d['id'])
			if pinfo is None:
				badPkgId = d.get('id')
				raise Exception(f"cannot build package info for id {badPkgId}")

			result.append(pinfo)
		return result

	# we pass the package id as a separate argument, because we get called with data from
	# different tables. The basic columns are named the same way, except for the package
	# id. In the packages table, it's in the 'id' column, whereas in table latest it's in pkgId
	def constructPackageInfo(self, d, pkgId):
		productId = d.get('productId')

		pinfo = PackageInfo(name = d['name'],
			version = d['version'],
			release = d['release'],
			epoch = d['epoch'],
			arch = d['arch'],
			backingStoreId = pkgId,
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
				errmormsg(f"{name} {dep} references unknown package {pkgId}")
				continue

			result.append((dep, pinfo))

		if name.startswith('/'):
			dep = Package.createDependency(name = name)
			for pkgId in self.retrieveFileProviders(name):
				pinfo = self.retrievePackageInfo(pkgId)
				if pinfo is None:
					raise Exception(f"cannot get package with id {pkgId}")
				result.append((dep, pinfo))

		# infomsg(f"Resolving {name}: found {len(result)} candidates")
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

	def fixupGuessBuildForRpm(self, rpm):
		buildId = None
		for id in self.packages.fetchColumn('buildId', name = rpm.name):
			if id is None:
				continue
			if buildId == id or buildId is None:
				buildId = id
			else:
				return None

		return self.retrieveOBSPackageByBuildId(buildId)

	# when the database is somewhat hosed it's faster to fix it up than rebuilding it from scratch
	def fixupLatest(self):
		byName = {}
		staleIds = set()
		dropIds = set()

		infomsg(f"Filtering table {self.packages.name} for stale entries")
		for pinfo, buildTime in self.packages.enumeratePackagesAndBuildTimes():
			assert(buildTime is not None)
			pinfo.buildTime = buildTime

			key = pinfo.shortname
			other = byName.get(key)
			if other is None:
				byName[key] = pinfo
			elif other.buildTime < pinfo.buildTime:
				debugmsg(f"  {other.fullname()} is older than {pinfo.fullname()}")
				staleIds.add(other.backingStoreId)
				byName[key] = pinfo

		infomsg(f"Found {len(staleIds)} outdated packages")

		for pinfo in self.latest._duplicates:
			infomsg(f"  table latest contains stale package {pinfo}")
		dropIds.update(map(lambda p: p.backingStoreId, self.latest._duplicates))

		infomsg(f"Filtering table {self.latest.name} for entries that refer to outdated packages")
		updates = []
		for pinfo in self.enumerateLatestPackages():
			key = pinfo.shortname

			if pinfo.backingStoreId in staleIds:
				infomsg(f"  {self.latest.name} refers to {pinfo} which is older than {byName[key]}")
				dropIds.add(pinfo.backingStoreId)
				continue

			other = byName.get(key)
			if other is None:
				infomsg(f"  {self.latest.name} refers to {pinfo} but this package no longer seems to exist")
				dropIds.add(pinfo.backingStoreId)
			elif other.backingStoreId == pinfo.backingStoreId:
				# okay, no change
				pass
			else:
				infomsg(f"  update latest entry {key}: {pinfo.fullname()} -> {other.fullname()}")
				updates.append(other)

		# first remove stale entries from the relational table
		if dropIds:
			infomsg(f"Dropping {len(dropIds)} entries from table {self.latest.name}")
			self.latest.deleteMultiple('pkgId', list(dropIds))

		if not updates:
			infomsg(f"Table \"{self.latest.name}\" seems to be clean")
			return

		defer = self.deferCommit()
		for pinfo in updates:
			infomsg(f"Updating {pinfo.shortname} to {pinfo.version}-{pinfo.release} pkgid {pinfo.backingStoreId}")
			self.latest.updateKeysAndValues(['version', 'release', 'pkgId'],
							[pinfo.version, pinfo.release, pinfo.backingStoreId],
							name = pinfo.name, arch = pinfo.arch)
		defer.commit()

	class RelationTracking(dict):
		def add(self, keyId, valueId):
			try:
				self[keyId].add(valueId)
			except:
				self[keyId] = set((valueId, ))

		def isKnown(self, keyId, valueId):
			trackedIds = self.get(keyId)
			return trackedIds and (valueId in trackedIds)

	def findBuildPackageDuplicates(self):
		found = set()
		duplicates = set()

		for pair in self.buildPkgRelation.enumerate():
			if pair in found:
				duplicates.add(pair)
			found.add(pair)

		return duplicates

	def fixupBuildPackageConflicts(self):
		packageTracking = self.RelationTracking()
		for buildId, pkgId in self.buildPkgRelation.enumerate():
			packageTracking.add(pkgId, buildId)

		unresolved = set()
		for pkgId, buildIds in packageTracking.items():
			if len(buildIds) == 1:
				continue

			pinfo = self.retrievePackageInfo(pkgId)

			builds = []
			for buildId in buildIds:
				d = self.builds.fetchOne(id = buildId)
				assert(d)

				build = self.builds.constructObject(OBSPackage, d)
				builds.append(build)

			names = map(str, builds)
			warnmsg(f"  conflicting entries for {pinfo.fullname()}: {' '.join(names)}")

			uniBuilds = list(filter(lambda b: ':' not in b.name, builds))
			if len(uniBuilds) == 0:
				infomsg(f"    all builds are multibuilds; picking one at random")
				choice = builds[0]
			elif len(uniBuilds) > 1:
				warnmsg("    unable to resolve this conflict")
				unresolved.add(pinfo)
			else:
				infomsg(f"    all builds bar one are multibuilds")
				choice = uniBuilds[0]

			infomsg(f"    place {pinfo} into {build}")

			badBuilds = buildIds.difference(set((build.backingStoreId, )))
			infomsg(f"  will remove pkg from build(s) {' '.join(map(str, badBuilds))}")
			for id in badBuilds:
				self.buildPkgRelation.delete(buildId = id, pkgId = pkgId)

		if unresolved:
			raise Exception(f"Unable to resolve all conflicts")

	def fixupBuildRelation(self, badBuilds, buildTracking):
		defer = self.deferCommit()

		# first remove stale entries from the relational table
		self.buildPkgRelation.deleteMultiple('buildId', list(badBuilds))

		# then recreate these entries, with correct data
		for buildId in sorted(badBuilds):
			for pkgId in sorted(buildTracking[buildId]):
				self.buildPkgRelation.addPackage(buildId = buildId, pkgId = pkgId)

		defer.commit()

	def fixupBuildPackageDuplicates(self, buildTracking):
		packagesFound = {}
		buildsFound = {}
		badBuilds = set()
		for buildId, pkgId in self.buildPkgRelation.enumerate():
			conflict = packagesFound.get(pkgId)
			if conflict is not None:
				if conflict != buildId:
					# this should not happen any longer, we just got rid of those in fixupBuildPackageConflicts() above
					raise Exception(f"  conflicting entries for pkg={pkgId}: build {conflict} vs {buildId}")

				warnmsg(f"  duplicate entry build={buildId} pkg={pkgId}")
				badBuilds.add(buildId)
			else:
				packagesFound[pkgId] = buildId

			buildTracking.add(buildId, pkgId)

		if badBuilds:
			infomsg(f"Found {len(badBuilds)} builds with dupliate entries")
			self.fixupBuildRelation(badBuilds, buildTracking)

	def fixupBuildPackageMissing(self, buildTracking):
		latestPkgIds = set(map(lambda pinfo: pinfo.backingStoreId, self.latest.knownPackages))
		badBuilds = set()

		for pinfo, buildId in self.packages.enumeratePackagesAndBuildIds():
			if pinfo.arch in ('src', 'nosrc'):
				continue

			pkgId = pinfo.backingStoreId
			if pkgId not in latestPkgIds:
				# this package is no longer listed in the latest table, we're not interested
				continue


			if buildTracking.isKnown(buildId, pkgId):
				continue

			warnmsg(f"  {pinfo.fullname()} belongs to build {buildId} but it's not in table {self.buildPkgRelation.name}")
			badBuilds.add(buildId)

			buildTracking.add(buildId, pkgId)

		if badBuilds:
			infomsg(f"Found {len(badBuilds)} incomplete builds")
			self.fixupBuildRelation(badBuilds, buildTracking)

	def fixupBuilds(self):
		buildTracking = self.RelationTracking()
		# First, check for packages that show up in more than one builds.
		# This typically happens for source rpms and multibuilds.
		self.fixupBuildPackageConflicts()

		# Then check for builds (aka OBS packages) that have duplicate entries
		self.fixupBuildPackageDuplicates(buildTracking)

		# Finally, loop over packages to see if there are any that are
		# current (ie referenced from latest) and have a build ID set
		# that we're not tracking
		self.fixupBuildPackageMissing(buildTracking)

	def fixupRequirements(self):
		currentIds = self.latest.currentPackageIds
		staleIds = self.packages.allValidIds.difference(currentIds)

		for row in self.tree.selectFrom(['id', 'requiringPkgId', 'requiredPkgId'], None):
			if row[1] in currentIds and row[2] in staleIds:
				warnmsg(f"  bad dependency in {self.tree.name}: pkg {row[1]} depends on outdated pkg {row[2]}")
