##################################################################
#
# yaml loader with tracking of where something was defined
#
# This does not track every scalar; just lists, dicts and strings.
# This should do for most purposes.
#
##################################################################
from yaml.composer import Composer
from yaml.constructor import SafeConstructor
from yaml.parser import Parser
from yaml.reader import Reader
from yaml.resolver import Resolver
from yaml.scanner import Scanner

class YamlLocationTracking(dict):
	def add(self, obj, mark):
		self[id(obj)] = mark

	def get(self, obj):
		return super().get(id(obj))

class NodeConstructor(SafeConstructor):
	initialized = False

	def __init__(self, *args, line_tracking = None, **kwargs):
		super().__init__(*args, **kwargs)
		self._line_tracking = line_tracking 
		self.register()

	def track_location(self, obj, node):
		line_tracking = getattr(self, '_line_tracking', None)
		if line_tracking is not None:
			line_tracking.add(obj, node.start_mark)

	def construct_yaml_map(self, node):
		obj, = SafeConstructor.construct_yaml_map(self, node)
		self.track_location(obj, node)
		yield obj

	def construct_yaml_seq(self, node):
		obj, = SafeConstructor.construct_yaml_seq(self, node)
		self.track_location(obj, node)
		yield obj

	def construct_yaml_str(self, node):
		obj = SafeConstructor.construct_yaml_str(self, node)
		self.track_location(obj, node)
		return obj

	@classmethod
	def register(klass):
		if not klass.initialized:
			klass.initialized = True
			klass.add_constructor(
				u'tag:yaml.org,2002:map',
				klass.construct_yaml_map)

			klass.add_constructor(
				u'tag:yaml.org,2002:seq',
				klass.construct_yaml_seq)

			klass.add_constructor(
				u'tag:yaml.org,2002:str',
				klass.construct_yaml_str)

class TrackingLoader(Reader, Scanner, Parser, Composer, NodeConstructor, Resolver):
	def __init__(self, stream, line_tracking = None):
		Reader.__init__(self, stream)
		Scanner.__init__(self)
		Parser.__init__(self)
		Composer.__init__(self)
		NodeConstructor.__init__(self, line_tracking = line_tracking)
		Resolver.__init__(self)

def tracked_load(stream, **kwargs):
	loader = TrackingLoader(stream, **kwargs)
	try:
		return loader.get_single_data()
	finally:
		loader.dispose()


