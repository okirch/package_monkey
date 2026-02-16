##################################################################
#
# Some helper classes for dealing with downloads of rpm headers
# The processCpio stuff was blatantly stolen from openSUSE-release-tools
#
##################################################################
import os
import struct
import tempfile
import re
import time
from .util import infomsg, warnmsg, errormsg

class DownloadManager(object):
	def __init__(self, destdir):
		self.destdir = destdir

		if not os.path.isdir(destdir):
			os.makedirs(destdir)

	@property
	def localFilenames(self):
		for filename in os.listdir(self.destdir):
			if filename.endswith('.rpm'):
				yield filename

	def fullpath(self, name):
		return os.path.join(self.destdir, name)

	def storeFromStream(self, filename, stream, count = -1):
		destpath = os.path.join(self.destdir, filename)

		with tempfile.NamedTemporaryFile(mode = 'wb', dir = self.destdir) as tmpfile:
			tmpfile.write(stream.read(count))
			if os.path.exists(destpath):
				os.unlink(destpath)
			os.link(tmpfile.name, destpath)

		return destpath

	def processCpio(self, stream):
		from osc.util.cpio import CpioHdr

		cpio_struct = struct.Struct('6s8s8s8s8s8s8s8s8s8s8s8s8s8s')
		cpio_name_re = re.compile('^([^/]+)-([0-9a-f]{32})$')

		while True:
			hdrtuples = cpio_struct.unpack(stream.read(cpio_struct.size))
			# Read and parse the CPIO header
			if hdrtuples[0] != b'070701':
				raise NotImplementedError(f'CPIO format {hdrtuples[0]:x} not implemented')

			# The new-ascii format has padding for 4 byte alignment
			def align():
				stream.read((4 - (stream.tell() % 4)) % 4)

			hdr = CpioHdr(*hdrtuples)
			hdr.filename = stream.read(hdr.namesize - 1).decode('ascii')
			stream.read(1)  # Skip terminator
			align()

			if hdr.filename == '.errors':
				content = stream.read(hdr.filesize)
				raise RuntimeError('Download has errors: ' + content.decode('ascii'))
			elif hdr.filename == 'TRAILER!!!':
				if stream.read(1):
					raise RuntimeError('Expected end of CPIO')
				break
			else:
				yield hdr
				align()

	def storeFromCpio(self, stream):
		for hdr in self.processCpio(stream):
			self.storeFromStream(hdr.filename, stream, hdr.filesize)

class RepositoryRpmDownadloadManager(DownloadManager):
	cpio_name_re = re.compile('^([^/]+)-([0-9a-f]{32})$')

	def storeFromCpio(self, stream):
		for hdr in self.processCpio(stream):
			binarymatch = self.cpio_name_re.match(hdr.filename)
			if not binarymatch:
				raise NotImplementedError(f'Cannot handle file name {hdr.filename} in archive')

			name = binarymatch.group(1)
			md5 = binarymatch.group(2)
			localName = f"{md5}-{name}.rpm"

			self.storeFromStream(localName, stream, hdr.filesize)

class DownloadQueue(object):
	def __init__(self, downloadManager, requestedLocalNames, remoteNameMap = None):
		self.downloadManager = downloadManager
		self.requestedLocalNames = requestedLocalNames
		self.remoteNameMap = remoteNameMap

		alreadyPresent = set(self.downloadManager.localFilenames)
		self.downloadNames = requestedLocalNames.difference(alreadyPresent)

		if remoteNameMap is None:
			self.queue = sorted(self.downloadNames)
		else:
			self.queue = sorted(map(remoteNameMap.get, self.downloadNames))

		# Used by the OBS rpmhdr download code
		self.remoteHash = None

	def __bool__(self):
		return bool(self.queue)

	def __len__(self):
		return len(self.queue)

	@property
	def state(self):
		if self.remoteHash is None:
			return None
		return self.remoteHash[:7]

	@property
	def downloadedFiles(self):
		if True:
			for name in self.requestedLocalNames:
				path = self.downloadManager.fullpath(name)
				if not os.path.isfile(path):
					raise Exception(f"Download of {path} failed")
				yield path
			return
		return set(map(self.downloadManager.fullpath, self.requestedLocalNames))

	def popChunk(self, count):
		result = self.queue[:count]
		del self.queue[:count]
		return result

	def purgeCache(self):
		alreadyPresent = set(self.downloadManager.localFilenames)
		toRemove = alreadyPresent.difference(self.requestedLocalNames)

		if toRemove:
			cacheDir = self.downloadManager.destdir
			infomsg(f"Going to remove {len(toRemove)} stale files from {cacheDir}")

			for filename in toRemove:
				os.unlink(os.path.join(cacheDir, filename))

class DownloadInfo(object):
	def __init__(self):
		self.timestamp = None

	def setTimestampNow(self):
		self.timestamp = time.strftime("%Y-%m-%d %H:%M %Z")

	def save(self, path):
		with open(path, "w") as f:
			if self.timestamp is not None:
				print(f"timestamp {self.timestamp}", file = f)
	def load(self, path):
		with open(path) as f:
			for line in f.readlines():
				w = line.strip().split()

				kwd = w.pop(0)
				if kwd == 'timestamp':
					self.timestamp = ' '.join(w)
				else:
					raise Exception(f"{path}: unknown keyword {kwd}")

