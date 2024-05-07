##################################################################
#
# Helper classes for writing (nicely formatted) XML documents
#
# Copyright (C) 2015-2020 Olaf Kirch <okir@suse.com>
#
##################################################################

import xml.etree.ElementTree as ET
import xml.etree.ElementInclude as ElementInclude

class XMLNode:

	def __init__(self, realnode, depth = 0):
		self.realnode = realnode
		self.depth = depth

	def tag(self):
		return self.realnode.tag

	@property
	def children(self):
		return iter(self.realnode)

	def addChild(self, name):
		depth = self.depth + 1

		parent = self.realnode
		if len(parent) == 0:
			parent.text = "\n" + depth * " "
		else:
			parent[-1].tail = "\n" + depth * " "

		child = ET.SubElement(parent, name)
		child.tail = "\n" + self.depth * " "

		return XMLNode(child, depth)

	def append(self, child):
		depth = self.depth + 1

		tail = "\n" + depth * " "

		parent = self.realnode
		if len(parent) == 0:
			parent.text = tail
		else:
			parent[-1].tail = tail

		parent.append(child)
		child.tail = "\n"

		return XMLNode(child, depth)

	def addField(self, name, value):
		# Skip empty elements
		if not value:
			return None

		if type(value) != str:
			value = str(value)

		child = self.addChild(name)
		child.realnode.text = value

		return child

	def addDict(self, dict):
		self.addDictSlice(dict, dict.keys())

	def addDictSlice(self, dict, fields):
		for f in fields:
			if f in dict:
				n = self.addField(f, dict[f])

	def addList(self, name, values):
		for v in values:
			self.addField(name, v)

	def getAttribute(self, name):
		return self.realnode.attrib.get(name)

	def setAttribute(self, name, value):
		if not value:
			return

		self.realnode.set(name, str(value))

	def setText(self, value):
		self.realnode.text = value

	def encode(self):
		return ET.tostring(self.realnode, encoding = 'unicode')

class XMLTree:
	def __init__(self, name):
		self.root = XMLNode(ET.Element(name))

	def write(self, filename):
		import os

		tree = ET.ElementTree(self.root.realnode)
		tree.write(filename + ".new", "UTF-8")
		os.rename(filename + ".new", filename)

	def writeIO(self, io):
		import os

		tree = ET.ElementTree(self.root.realnode)
		tree.write(io, "UTF-8")

class TreeBuilder:
	def __init__(self, listname, itemname, data):
		self.tree = XMLTree(listname)
		self.addList(self.tree.root, itemname, data)

	def addDatum(self, node, key, value):
		if value is None:
			value = ""

		vt = type(value)
		if vt in (bool, int, float, str):
			child = node.addChild(key)
			child.realnode.text = str(value)
			child.setAttribute('type', vt.__name__)
		elif vt in (list, tuple):
			if len(value) == 0:
				return

			self.addList(node.addChild(key), 'i', value)
		elif vt == dict:
			self.addDict(node.addChild(key), value)
		else:
			raise ValueError("TreeBuilder: don't know how to represent %s typed data" % vt)

	def addList(self, node, itemname, listData):
		node.setAttribute('type', 'list')
		for item in listData:
			self.addDatum(node, itemname, item)

	def addDict(self, node, data):
		node.setAttribute('type', 'dict')
		for key in data.keys():
			v = data.get(key)
			if v is not None:
				self.addDatum(node, key, v)

class TreeParser:
	def __init__(self, description, listname):
		self.description = description
		self.listname = listname

		self.processors = {
			'bool':		self.processBool,
			'int':		self.processInt,
			'float':	self.processFloat,
			'str':		self.processStr,
			'list':		self.processList,
			'dict':		self.processDict,
		}

	def load(self, filename):
		import os.path

		if not os.path.exists(filename):
			suse.debug("Not loading %s - %s does not exist yet" % (self.description, filename))
			return None

		suse.debug("Loading %s from %s" % (self.description, filename))
		tree = ET.parse(filename)

		root = tree.getroot()
		assert(root.tag == self.listname)
		return self.process(root)

	def processBool(self, node):
		return bool(node.text.strip())

	def processInt(self, node):
		return int(node.text.strip())

	def processFloat(self, node):
		return float(node.text.strip())

	def processStr(self, node):
		if node.text is None:
			return ""
		return node.text.strip()

	def processList(self, node):
		result = []
		for child in node:
			result.append(self.process(child))
		return result

	def processDict(self, node):
		result = {}
		for child in node:
			result[child.tag] = self.process(child)
		return result

	def process(self, node):
		type = node.attrib.get('type')
		if type is None:
			# assume it's a string
			return self.processStr(node)

		fn = self.processors.get(type)
		if fn is None:
			raise ValueError("TreeParser: unable to process node type=%s" % type)

		return fn(node)

def parse(filename):
	return ET.parse(filename)

# xml.etree.ElementInclude does not handle include files that contain
# 0 or N > 1 elements...
XINCLUDE = "{http://www.w3.org/2001/XInclude}"
XINCLUDE_INCLUDE = XINCLUDE + "include"
XINCLUDE_FALLBACK = XINCLUDE + "fallback"

xinclude_debugging = False

def xinclude_expand(xmlnode, cache_handle):
	i = 0
	while i < len(xmlnode):
		e = xmlnode[i]
		if e.tag == XINCLUDE_INCLUDE:
			# process xinclude directive
			href = e.get("href")
			parse = e.get("parse", "xml")
			assert(parse == "xml")

			new_elems = xinclude_expand_one(cache_handle, href)
			# The original ElementInclude code tries to copy over .tail
			# but I'm not going to bother with that for now

			xmlnode[i:i+1] = new_elems

			i += len(new_elems)
		elif e.tag == XINCLUDE_FALLBACK:
			raise ValueError()
		else:
			xinclude_expand(e, cache_handle)
			i += 1

def xinclude_expand_one(cache_handle, href):
	global xinclude_debugging

	if xinclude_debugging:
		suse.debug("Processing XInclude href=%s" % href)

	text = cache_handle.getText(href)
	if not text:
		raise ValueError("Failed to expand XInclude href=\"%s\"" % href)

	# Sometimes, IBS files start with <?xml ...> but are still invalid
	# (because they contain several top-level elements)
	text = text.strip()
	if text.startswith('<?xml'):
		k = text.find('>')
		if xinclude_debugging:
			suse.debug("%s has an <?xml> element, removing: %s"  % text[:k+1])
		text = text[k+1:].strip()

	text = "<fakeroot>" + text + "</fakeroot>"

	root = ET.fromstring(text)

	xinclude_expand(root, cache_handle)

	return [child for child in root]

def fromString(xml):
	root = ET.fromstring(xml)
	if root is None:
		return None
	return ET.ElementTree(root)

def toString(xmlnode):
	if isinstance(xmlnode, XMLNode):
		xmlnode = xmlnode.realnode

	return ET.tostring(xmlnode, encoding = 'unicode')

# Protect &nbsp; in product names. This happens in bugzilla quite a lot
# Unfortunately, this is not really perfect. Thanks to duplicate
# escaping, what ends up in the xml file will be "&amp;nbsp;", but I don't
# have the time to fix that right now.
def escape(s):
	if u'\xa0' in s:
		s = s.replace(u'\xa0', '&nbsp;')
	return s

def unescape(s):
	if '&nbsp;' in s:
		t = s
		if type(t) != str:
			print("unescape(%s <%s>)\n" % (t, type(t)))
		s = s.replace('&nbsp;', u'\xa0')
		#suse.debug("unescape \"%s\" -> \"%s\"" % (t, s))

	return s

def childElementAsString(node, name):
	child = node.find(name)
	if child is None:
		return None

	return child.text.strip()
