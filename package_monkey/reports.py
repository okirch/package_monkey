##################################################################
#
# Collection of classes used to report problems
#
##################################################################

from .util import loggingFacade
from .util import infomsg, warnmsg, errormsg

class LocationIndexedReport(object):
	def __init__(self):
		self._messages = []

	def __bool__(self):
		return bool(self._messages)

	def __len__(self):
		return len(self._messages)

	def add(self, location, message):
		if location is None:
			self._messages.append(("unknown", 0, message, ))
		else:
			self._messages.append(location.key + (message, ))

	def render(self):
		lastName = None

		for name, seq, msg in sorted(self._messages):
			if name != lastName:
				infomsg(f"{name}:")
				lastName = name

			infomsg(f"   {msg}")

class GenericStringReport(object):
	def __init__(self, title = None):
		self.title = title
		self.values = []

	def __bool__(self):
		return bool(self.values)

	def add(self, value):
		self.values.append(value)

	def display(self):
		if self.title is not None:
			infomsg(f"{self.title}:")
		for value in self.values:
			infomsg(f"   {value}")
