import os
from util import infomsg, warnmsg, errormsg, loggingFacade

class Git(object):
	def __init__(self, workPath):
		self.client = GitClient()
		self.workPath = workPath

		self._clones = {}

	def makeUrlAnonymous(self, url):
		if url.startswith("git@"):
			stumpUrl = url[3:]
		elif url.startswith("gitea@"):
			stumpUrl = url[6:]
		else:
			return url

		# we've stripped off git@ or gitea@
		# Now change host:path to host/path
		assert(':') in stumpUrl
		host, rest = stumpUrl.split(':', maxsplit = 1)
		return f"https://{host}/{rest}"

	def clone(self, name, gitUrl):
		work = self._clones.get(name)
		if work is not None:
			assert(work.gitUrl == gitUrl)
			return work

		work = GitWorkingCopy(self.client, f"{self.workPath}/{name}")
		work.clone(gitUrl)
		return work

class GitConfig(object):
	class Section(object):
		def __init__(self, type, name = None):
			self.type = type
			self.name = name
			self._dict = {}

		def __str__(self):
			if self.name is None:
				return self.type
			return f"{self.type} {self.name}"

		def set(self, key, value):
			self._dict[key] = value

		def get(self, key):
			return self._dict.get(key)

	def __init__(self):
		self._sections = {}

	def getSection(self, type, name = None):
		key = self.makeKey(type, name)
		return self._sections.get(key)

	def makeKey(self, type, name = None):
		if name is None:
			return type
		return f"{type}:{name}"

	def parse(self, path):
		with open(path, "r") as f:
			lineno = 0
			for line in f.readlines():
				lineno += 1

				line = line.rstrip()

				# infomsg(f"{path}:{lineno}: >> {line}")

				if not line:
					continue

				if line[0] == '[':
					line = line.strip("[]")
					section = self.parseSectionStart(line)

					key = self.makeKey(section.type, section.name)
					if key in self._sections:
						raise Exception(f"{path}:{lineno}: duplication definition of section {section}")
					self._sections[key] = section
					continue

				w = line.split()
				if w[1] != '=':
					raise Exception(f"{path}: invalid line {lineno}: {line}")

				section.set(w[0], w[2])


	def parseSectionStart(self, line):
		if ' ' in line:
			type, quotedName = line.split(maxsplit = 1)
			name = quotedName.strip('"')

			section = self.Section(type, name)
		else:
			section = self.Section(line)

		return section

class GitClient(object):
	def __init__(self):
		self.dryRun = False

	def run(self, cmd, okayToFail = False, path = None):
		if path:
			cmd = f"cd {path} && {cmd}"

		if self.dryRun:
			infomsg(f"Would run {cmd}")
			return

		infomsg(f"About to run {cmd}")
		rv = os.system(cmd)
		if rv != 0 and not okayToFail:
			raise Exception(f"git command \"{cmd}\" failed: exit value {rv}")
		return rv

	def popen(self, cmd, okayToFail = False, path = None):
		if path:
			cmd = f"cd {path} && {cmd}"

		if self.dryRun:
			infomsg(f"Would run {cmd}")
			return open("/dev/null")

		infomsg(f"About to run {cmd}")
		return os.popen(cmd)

class GitObject(object):
	def __init__(self, client):
		self.client = client

	def run(self, *args, **kwargs):
		if 'path' not in kwargs:
			kwargs['path'] = self.path
		return self.client.run(*args, **kwargs)

	def popen(self, *args, **kwargs):
		if 'path' not in kwargs:
			kwargs['path'] = self.path
		return self.client.popen(*args, **kwargs)

	def runAndRead(self, *args, **kwargs):
		with self.popen(*args, **kwargs) as f:
			res = f.read().strip()
		return res

class GitWorkingCopy(GitObject):
	class Change(object):
		def __init__(self, verb, type, name):
			self.verb = verb
			self.type = type
			self.name = name

		def __str__(self):
			return f"{self.verb} {self.type} {self.name}"

	def __init__(self, client, path):
		super().__init__(client)

		self.path = path
		self.gitUrl = None

		self._dotgit = None
		self._submodules = None

		self._changes = []
		self._clean = True

	def __str__(self):
		return self.path

	@property
	def originURL(self, remoteName = "origin"):
		try:
			config = self.config
		except Exception as e:
			config = None

		if config is None:
			errormsg(f"{self}: unable to load .git/config: {e}")
			return self.runAndRead(f"git remote get-url {remoteName}")

		origin = config.getSection("remote", remoteName)
		return origin.get("url")

	@property
	def gitdir(self):
		if self._dotgit is not None:
			return self._dotgit

		gitdir = f"{self.path}/.git"
		if os.path.isdir(gitdir):
			self._dotgit = gitdir
			return self._dotgit
		elif os.path.isfile(gitdir):
			with open(gitdir) as f:
				for line in f.readlines():
					key, value = line.split()
					key = key.rstrip(':')
					if key == "gitdir":
						self._dotgit = f"{self.path}/{value}"
						break

		if self._dotgit is None:
			raise Exception(f"{self}: cannot find .git directory")

		return self._dotgit

	def locateGitFile(self, relativeName):
		gitdir = self.gitdir
		path = f"{gitdir}/{relativeName}"
		if os.path.exists(path):
			return path

		raise Exception(f"{self}: cannot locate git file {relativeName}: {path} does not exist")

	@property
	def config(self):
		config = GitConfig()
		config.parse(self.locateGitFile("config"))
		return config

	def raw_status(self):
		changes = []
		with self.popen("git status --porcelain --untracked-files=no") as f:
			changes = list(f.readlines())
		return changes

	@property
	def modified(self):
		return bool(self.raw_status())

	def clone(self, gitUrl):
		if self.gitUrl is not None:
			fail

		if not os.path.isdir(self.path):
			os.makedirs(self.path)

		dotGit = f"{self.path}/.git"
		if os.path.isdir(dotGit):
			actualUrl = self.originURL
			if actualUrl != gitUrl:
				raise Exception(f"Working copy {self.path} is for {actualUrl} not {gitUrl}")
			infomsg(f"{dotGit} already exists and contains a checkout of {gitUrl}")
			self.gitUrl = gitUrl

			changes = self.raw_status()
			if changes:
				warnmsg(f"Detected modifications in the working copy of {gitUrl} at {self.path}")
				for line in changes:
					warnmsg(f"  {line.rstrip()}")
				warnmsg(f"Proceeding with caution")
				self._clean = False
			return

		self.run(f"git clone {gitUrl} {self.path}", path = None)

	def hasPendingChanges(self):
		return bool(self._changes or not self._clean)

	def commit(self, msg = None):
		import tempfile

		if not self._clean:
			raise Exception(f"Refusing to commit unknown changes in working copy of {self.gitUrl} at {self.path} - not clean")

		if not self._changes:
			errormsg(f"Refusing to commit changes in working copy of {self.gitUrl} at {self.path} - no changes recorded")
			return

		with tempfile.NamedTemporaryFile(mode = "w", prefix = "obsgit") as f:
			changelog = self.generateChangelog(msg)
			f.write(changelog)
			f.flush()

			self.run(f"git commit --file {f.name}")
			self._changes = []

	def generateChangelog(self, msg = None):
		if not msg:
			type = self._changes[0].type
			verb = self._changes[0].verb
			names = []
			tail = ""
			for c in self._changes:
				if c.type == type and c.verb == verb:
					names.append(c.name)
				else:
					tail = ", and more"

			msg = f"{verb} {type}(s) {', '.join(names)}{tail}"

		changelog = msg + "\n\n"
		for c in self._changes:
			changelog += f" - {c}\n"

		return changelog

	def add(self, name):
		self.run(f"git add '{name}'")

	def rm(self, name, recursive = False):
		if recursive:
			self.run(f"git rm -rf '{name}'")
		else:
			self.run(f"git rm -f '{name}'")

	def push(self):
		self.run("git push")

	def checkout(self):
		pass

	# regular packages as subdirs
	def addPackage(self, name, files):
		packageExisted = True
		somethingChanged = False

		sm = self.getSubmodule(name)
		if sm is not None:
			raise Exception(f"Cannot add regular package {name} to git: there is already a submodule of the same name")

		path = f"{self.path}/{name}"
		if not os.path.isdir(path):
			os.makedirs(path, 0o755)
			somethingChanged = True
			packageExisted = False

		infomsg(f"Updating package {name} in git working copy")
		for fname, content in files.items():
			fpath = f"{path}/{fname}"
			if os.path.isfile(fpath):
				current = open(fpath, "r").read()
				if current == content:
					infomsg(f"   {fname} is unchanged")
					continue

			open(fpath, "w").write(content)
			self.add(f"{name}/{fname}")
			somethingChanged = True

		# find old files
		allFiles = set()
		with os.scandir(path) as it:
			for entry in it:
				if entry.is_file():
					allFiles.add(entry.name)

		oldFiles = allFiles.difference(set(files.keys()))
		if oldFiles:
			for fname in oldFiles:
				self.rm(f"{name}/{fname}")
			somethingChanged = True

		if somethingChanged:
			verb = 'add'
			if packageExisted:
				verb = 'update'
			self._changes.append(self.Change('add', 'package', name))

		# FIXME: create a "git package" object?

	def removePackage(self, name):
		sm = self.getSubmodule(name)
		if sm is not None:
			raise Exception(f"Unable to remove submodule {name}: not yet supported")

		if os.path.isdir(f"{self.path}/{name}"):
			infomsg(f"Removing package {name} in git working copy")
			self.rm(name, recursive = True)

			self._changes.append(self.Change('remove', 'package', name))

		return True

	@property
	def subdirectories(self):
		result = []
		for de in os.scandir(self.path):
			if not de.name.startswith('.') and de.is_dir():
				result.append(de.name)
		return result

	def refreshSubmodules(self):
		if self._submodules is None:
			self._submodules = {}
			with self.popen(f"git submodule") as f:
				for line in f.readlines():
					w = line.split()
					sm = GitSubmodule(self, w[1], w[0])
					self._submodules[sm.name] = sm

	@property
	def submodules(self):
		self.refreshSubmodules()
		return sorted(self._submodules.values(), key = str)

	def addSubmodule(self, gitUrl, name = None):
		self.refreshSubmodules()

		if name is None:
			# try to guess the name
			name = gitUrl
			if name.endswith('.git'):
				name = name[:-4]
			name = name.rstrip('/')
			name = os.path.basename(name)
			# print(f"{gitUrl} -> {name}")

		dummy = self.submodules
		if name in self._submodules:
			infomsg(f"submodule {name} already exists, no need to create it")
			return self._submodules[name]

		self.run(f"git submodule add {gitUrl}")
		sm = GitSubmodule(self, name, None, url = gitUrl)
		self._submodules[name] = sm

		self._changes.append(self.Change('add', 'submodule', name))
		return sm

	def getSubmodule(self, name):
		self.refreshSubmodules()
		return self._submodules.get(name)

class GitSubmodule(GitWorkingCopy):
	def __init__(self, parentCopy, name, hash, url = None):
		super().__init__(parentCopy.client, f"{parentCopy.path}/{name}")

		self.parentPath = parentCopy.path
		self.name = name

		self.gitUrl = url or self.originURL

	def checkout(self):
		parentPath = os.path.dirname(self.path)
		self.run(f"git submodule update --init ./{self.name}", path = self.parentPath)

if __name__ == '__main__':
	loggingFacade.enableStdout()

	git = Git("work")

	work = git.clone("test", "gitea@src.suse.de:okir/sle16_foundations")

	for sm in work.submodules:
		print(f"Found submodule {sm.name}")

	sm = work.getSubmodule('bash')
	if sm is not None:
		print(f"Checking out bash into {sm}")
		sm.checkout()

	sm = work.addSubmodule('https://src.suse.de/SLFO-pool/glibc')
	sm = work.addSubmodule('https://src.suse.de/SLFO-pool/bcel')
	work.commit()
	work.push()
