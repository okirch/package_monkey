import os
from util import infomsg, loggingFacade

class Git(object):
	def __init__(self, workPath):
		self.client = GitClient()
		self.workPath = workPath

		self._clones = {}

	def clone(self, name, gitUrl):
		work = self._clones.get(name)
		if work is not None:
			assert(work.gitUrl == gitUrl)
			return work

		work = GitWorkingCopy(self.client, f"{self.workPath}/{name}")
		work.clone(gitUrl)
		return work

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
	def __init__(self, client, path):
		super().__init__(client)

		self.path = path
		self.gitUrl = None

		self._submodules = None

		self._changelog = ""

	def __str__(self):
		return self.path

	@property
	def originURL(self):
		return self.runAndRead(f"git remote get-url origin")

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
			return

		self.run(f"git clone {gitUrl} {self.path}", path = None)

	def commit(self):
		import tempfile

		with tempfile.NamedTemporaryFile(mode = "w", prefix = "obsgit") as f:
			if self._changelog:
				f.write(self._changelog)
			else:
				f.write("Weird changes without changelog")
			f.flush()

			self.run(f"git commit --file {f.name}")

	def push(self):
		self.run("git push")

	def checkout(self):
		pass

	@property
	def submodules(self):
		if self._submodules is None:
			self._submodules = {}
			with self.popen(f"git submodule") as f:
				for line in f.readlines():
					w = line.split()
					sm = GitSubmodule(self, w[1], w[0])
					self._submodules[sm.name] = sm
		return sorted(self._submodules.values(), key = str)

	def addSubmodule(self, gitUrl, name = None):
		if name is None:
			# try to guess the name
			name = gitUrl
			if name.endswith('.git'):
				name = name[:-4]
			name = name.rstrip('/')
			name = os.path.basename(name)
			# print(f"{gitUrl} -> {name}")

		if name in self._submodules:
			infomsg(f"submodule {name} already exists, no need to create it")
			return self._submodules[name]

		self.run(f"git submodule add {gitUrl}")
		sm = GitSubmodule(self, name, None, url = gitUrl)
		self._submodules[name] = sm

		self._changelog += f" - Added submodule {name}\n"
		return sm

	def getSubmodule(self, name):
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
