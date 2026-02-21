from .options import ApplicationBase
from .filter import Classification
from .util import loggingFacade
from .cmd_label import ClassificationGadget

LEFT_BRACE = '{'

class DotfileWriter(object):
	class Graph(object):
		def __init__(self, writer, name, indent = ""):
			self.writer = writer
			self.name = name
			self.origIndent = indent
			self.indent = indent + "    "

		def begin(self):
			self.writer.write(f"{self.origIndent}digraph {self.name} {LEFT_BRACE}")
			self.writer.write(f"{self.indent}splines=true;")

		def addNode(self, label):
			labelID = self.writer.makeNodeID(label)
			self.writer.write(f"{self.indent}{labelID} [label=\"{label}\"];")
			return labelID

		def getNode(self, label):
			id = self.writer.getNode(label)
			if id is None:
				id = self.addNode(label)
			return id

		def addEdge(self, label0, label1, **kwargs):
			id0 = self.getNode(label0)
			id1 = self.getNode(label1)

			if kwargs:
				attrs = list(f"{key}={value}" for key, value in kwargs.items())
				attrs = f" [{','.join(attrs)}]"
			else:
				attrs = ""

			self.writer.write(f"{self.indent}{id0} -> {id1}{attrs};")

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
		print(self.indent, file = self.fp, end = '')
		print(*args, file = self.fp, **kwargs)

	def graph(self, name = None):
		if name is None:
			name = self.name
		return self.Graph(self, name)

	def makeNodeID(self, label):
		labelID = f"node{self.nextFreeID}"
		self.nodeIds[label] = labelID
		self.nextFreeID += 1
		return labelID

	def getNode(self, label):
		return self.nodeIds.get(label)

def loadClassificationScheme(application):
	db = application.loadDBForSnapshot()

	gadget = ClassificationGadget(db, application.modelDescription)
	gadget.solve(application.productCodebase)

	return gadget.classificationScheme

# Not yet ready for production...
def writeLabelGraph(output, labels, order):
	graph = output.graph()
	graph.begin()

	for label in labels:
		edges = set()
		secondLevelClosure = set()
		for req in order.maxima(label.runtimeRequires):
			edges.add(req)

			closure = order.downwardClosureFor(req)
			closure.discard(req)

			secondLevelClosure.update(closure)

		# do not draw arrows A -> C when there's an indirect connection A -> B -> C
		edges.difference_update(secondLevelClosure)

		for req in edges:
			graph.addEdge(label, req)

	graph.end()

def writeComponentGraph(application):
	classificationScheme = loadClassificationScheme(application)

	order = classificationScheme.epicOrder()
	labels = list(order.bottomUpTraversal())

	output = DotfileWriter("components")
	writeLabelGraph(output, labels, order)

	print(f"Please process output file using something like this: dot -Tpdf {output.filename}")

def writeLayerGraph(application):
	classificationScheme = loadClassificationScheme(application)

	order = classificationScheme.layerOrder()
	labels = list(order.bottomUpTraversal())

	output = DotfileWriter("layers")
	writeLabelGraph(output, labels, order)

	print(f"Please process output file using something like this: dot -Tpdf {output.filename}")


class ChartApplication(ApplicationBase):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		loggingFacade.disableTimestamps()

	def run(self):
		graphType = self.opts.graph_type
		print(f"Creating {graphType} graph")
		if graphType in ("epics", "components"):
			writeComponentGraph(self)
		elif graphType in ("layers"):
			writeLayerGraph(self)
		else:
			raise Exception(f"graph type \"{graphType}\" not supported")
