##################################################################
#
# Handle various queries on epics
#
##################################################################

from .options import ApplicationBase
from .util import infomsg, errormsg, warnmsg
from .queries import QueryContext
from .query.runtime_requires import WhatRequiresQuery
from .query.list import ListQuery, ShowQuery

class EpicQueryApplication(ApplicationBase):
	def __init__(self, name, *args, **kwargs):
		super().__init__(name, *args, **kwargs)

	def run(self):
		self.verbosityLevel = 1
		if self.opts.terse:
			self.verbosityLevel = 0
		if self.opts.verbose:
			self.verbosityLevel = 2

		query = self.createQuery(QueryContext(self))
		if query.renderer is not None:
			query.renderer.renderPreamble(query)

		epics = getattr(self.opts, 'epics', [])
		query.perform(epics)

class EpicWhatRequiresApplication(EpicQueryApplication):
	def createQuery(self, context):
		return WhatRequiresQuery(context, self.opts.required.split(','), onlyRpms = self.opts.only_rpms)

class EpicListApplication(EpicQueryApplication):
	def createQuery(self, context):
		return ListQuery(context)

class EpicShowApplication(EpicQueryApplication):
	def createQuery(self, context):
		return ShowQuery(context)
