
class DotfileWriter:
	class Graph:
		def __init__(self, writer, name, indent = ""):
			self.writer = writer
			self.name = name
			self.origIndent = indent
			self.indent = indent + "    "

		def begin(self):
			self.writer.write(f"{self.origIndent}digraph {self.name} \{")

		def addNode(self, label):
			labelID = self.writer.makeNodeID(label)
			self.writer.write(f"{self.indent}{labelID} [label=\"{label}\"];")
			return labelID

		def addEdge(self, label0, label1):
			id0 = self.writer.getNode(label0)
			id1 = self.writer.getNode(label1)
			self.writer.write(f"{self.indent}{id0} -> {id1};")

		def end(self):
			self.writer.write(self.origIndent + "}")

	def __init__(self, name, filename = None):
		self.name = name
		self.filename = filename or f"{name}.gv"
		self.fp = open(self.filename, "w")

		self.live = set()
		self.nodeIds = {}
		self.nextFreeID = 0
		self.indent = ""

	def write(self, *args, **kwargs):
		print(self.indent, file = self.fp, endl = '')
		print(*args, file = self.fp, **kwargs)

	def graph(self, name):
		return self.Graph(self, name)

	def makeNodeID(self, label):
		labelID = f"topic{self.nextFreeID}"
		self.nodeIds[label] = labelID
		self.nextFreeID += 1
		return labelID

	def getNode(self, label):
		return self.nodeIds[label]

# Not yet ready for production...
def writeLabelGraph(output, labels):
	live = set()
	for label in labels:
#		if not closure[label.name]:
#			continue

#		if label.name.endswith("+unused"):
#			continue

		edges = set()
		secondLevelClosure = set()
		for req in label.runtimeRequires:
			if req in live:
				edges.add(req)
				secondLevelClosure.update(req.closure.difference(set([req])))

		# do not draw arrows A -> C when there's an indirent connection A -> B -> C
		edges.difference_update(secondLevelClosure)

		if label in live or edges:
			live.add(label)

			output.addNode(label)
			for req in edges:
				output.addEdge(label, req)
