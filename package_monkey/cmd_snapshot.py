##################################################################
#
# Subcommand for snapshotting the results of a labelling run
# for later comparison
#
##################################################################

import sys
import os
import time
import shutil

from .util import infomsg, errormsg, warnmsg
from .options import ApplicationBase
from .snapshots import *

class SnapshotApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.keep = False

	def run(self):
		slug = self.opts.slug
		slug = slug.lstrip('@')
		if not slug:
			errormsg(f"invalid snapshot slug {self.opts.slug}")
			return 1

		infomsg(f"Snapshotting current state as {slug}")

		snapRoot = self.getCachePath('snapshots')
		if not os.path.isdir(snapRoot):
			os.makedirs(snapRoot)

		snapFactory = SnapshotFactory(snapRoot)

		snapshot = snapFactory.createSnapshot(self.expandedStateRoot)

		if self.opts.with_rpms:
			snapshot.addRpms(self.productCodebase, self.getCachePath("rpmhdrs"))

		if not self.keep:
			snapFactory.remove(slug)

		snapFactory.remember(slug, snapshot)

class PublishApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def run(self):
		path = self.opts.path
		if not os.path.isdir(path):
			raise Exception(f"Cannot publish to {path}: not a directory")

		data = self.load(self.opts.slug)

		if self.opts.slug:
			infomsg(f"Publishing snapshot {self.opts.slug}")
		else:
			infomsg(f"Publishing current state")

		return data.publish(path)

	def load(self, slug):
		if slug is None:
                        return self.data

		data = self.getSnapshot(slug)
		if data is None:
			raise Exception(f"Unknown snapshot {slug}")

		return data

