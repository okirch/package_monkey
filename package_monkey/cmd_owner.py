##################################################################
#
# Subcommand that implements owner signoff
#
##################################################################

import sys
import os
import time
import datetime

from .util import infomsg, errormsg, warnmsg
from .options import ApplicationBase
from .snapshots import *
from .postprocess import *
from .git import GitClient, GitWorkingCopy


class OwnerSignoffApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def run(self):
		model = self.modelDescription

		hash = self.opts.hash
		if hash is None:
			gh = self.useGitRepo(self.modelDescription.path)
			hash = gh.getRevisionHash()

		release = model.releaseID

		path = self.getCodebasePath("packages.csv")
		labelFacade = TrivialLabelFacade(path)

		policy = self.codebaseData.loadPolicy(labelFacade)
		owner = policy.matchOwner(self.opts.owner)
		if owner is None:
			raise Exception(f"Could not identify maintainer for {self.opts.owner}")

		infomsg(f"Processing sign-off on behalf of {owner.id}: {owner}")
		timestamp = datetime.datetime.now().isoformat('T', 'seconds')

		signoffs = []
		for epic in labelFacade.epics:
			if epic.ownerID == owner.id:
				infomsg(f"Sign-off {release} {hash} {epic}")
				signoffs.append(model.Signoff(release, epic, owner, hash, timestamp))

		model.addSignoffs(signoffs)

	def useGitRepo(self, path):
		client = GitClient()
		git = GitWorkingCopy(client, path)

		infomsg(f"Using local copy of {git.originURL}")
		return git
