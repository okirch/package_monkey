##################################################################
# Kludgy parser for boolean rpm expressions
# It doesn't help much (right now) but it doesn't make things
# worse either. Great Success!
##################################################################

from .util import infomsg

__names__ = ['BooleanDependency']

class BooleanDependency(object):
	@classmethod
	def parse(klass, string):
		parser = DependencyParser(string)
		tree = parser.process()

		if False:
			infomsg(f"Parsed expression:")
			dumper = ExpressionNodeDumper(indent = "   ")
			tree.dump(dumper)

		return tree.build()

	@staticmethod
	def test():
		infomsg(f"Testing")
		p = DependencyParser("(foobar == 1.0 if kernel)")
		while True:
			type, value = p.nextToken()
			if type is DependencyParser.Lexer.EOL:
				break

		inputs = (
			"(foobar == 1.0 if kernel)",
			"(foo or alternative(foo))",
			"((foo or bar))",
			"salt-transactional-update = 3006.0-150500.4.12.2 if read-only-root-fs",
			"(systemd >= 238 if systemd)",
		)

		dumper = ExpressionNodeDumper(indent = "   ")
		for s in inputs:
			infomsg(f"Processing {s}")
			p = DependencyParser(s)
			tree = p.process()
			tree.dump(dumper)

			dep = tree.build()
			infomsg(f" => {dep}")
			infomsg("")


class SingleStringDependency(object):
	def __init__(self, name):
		self.name = name

	def __str__(self):
		return self.name

class FileDependency(SingleStringDependency):
	def eval(self, oracle):
		return oracle.evalFileDependency(self.name)

class UnversionedPackageDependency(SingleStringDependency):
	def eval(self, oracle):
		return oracle.evalUnversionedDependency(self.name)

class FailingDependency(SingleStringDependency):
	def eval(self, oracle):
		return False

class VersionedPackageDependency(object):
	compare = {
		"EQ" : lambda a, b: (int(a) == int(b)),
		"NE" : lambda a, b: (int(a) != int(b)),
		"LE" : lambda a, b: (a <= b),
		"GE" : lambda a, b: (a >= b),
		"LT" : lambda a, b: (a < b),
		"GT" : lambda a, b: (a > b),
	}

	def __init__(self, name,  flags = None, ver = None):
		self.name = name
		self.flags = flags
		self.op = self.compare[flags]
		self.ver = ver

	def __str__(self):
		return f"{self.name} {self.flags} {self.ver}"

	def eval(self, oracle):
		return oracle.evalVersionedDependency(self.name, self.flags, self.ver)

class ConditionalDependency(object):
	def __init__(self, condition, inner):
		self.condition = condition
		self.inner = inner

	def __str__(self):
		return f"({self.inner} if {self.condition})";

	@property
	def name(self):
		return str(self)

class OrDependency(object):
	def __init__(self, children):
		self.children = children

	def __str__(self):
		return "(" + " or ".join(str(_) for _ in self.children) + ")"

	@property
	def name(self):
		return str(self)

	def eval(self, oracle):
		for child in self.children:
			if child.eval(oracle) == True:
				return True
		return False

class AndDependency(object):
	def __init__(self, children):
		self.children = children

	def __str__(self):
		return "(" + " with ".join(str(_) for _ in self.children) + ")"

	@property
	def name(self):
		return str(self)

	def eval(self, oracle):
		for child in self.children:
			if child.eval(oracle) is not True:
				return False
		return True

class DependencyParser(object):
	class Lexer(object):
		EOL = 0
		LEFTB = 1
		RIGHTB = 2
		OPERATOR = 3
		IDENTIFIER = 4

		CHARCLASS_OPERATOR = ('<', '>', '=', '!')
		CHARCLASS_WORDBREAK = CHARCLASS_OPERATOR

		OPERATOR_IDENTIFIERS = ('EQ', 'NE', 'LT', 'GT', 'LE', 'GE')
		OPERATOR_TABLE = {
			'=':  'EQ',
			'==': 'EQ',
			'<=': 'LE',
			'>=': 'GE',
			'<':  'LT',
			'>':  'GT',
			'!=': 'NE',
		}


		def __init__(self, string):
			self.value = list(string)
			self.pos = 0

		def __str__(self):
			return "".join(self.value)

		def getc(self):
			try:
				cc = self.value[self.pos]
			except:
				return None

			self.pos += 1
			return cc

		def ungetc(self, cc):
			assert(self.pos > 0)
			assert(self.value[self.pos - 1] == cc)
			self.pos -= 1

		def next(self):
			result = ""
			while True:
				cc = self.getc()
				if cc is None:
					break

				while cc and cc.isspace():
					cc = self.getc()

				if cc in self.CHARCLASS_OPERATOR:
					while cc in self.CHARCLASS_OPERATOR:
						result += cc
						cc = self.getc()
					# translate operator "<=" to "LE" and so on
					result = self.OPERATOR_TABLE[result]
					return (self.OPERATOR, result)

				if cc == '(':
					return (self.LEFTB, cc)
				if cc == ')':
					return (self.RIGHTB, cc)

				processingBracketedArgument = False
				while cc and not cc.isspace() and not cc in self.CHARCLASS_WORDBREAK:
					if cc == '(':
						if processingBracketedArgument:
							raise Exception("Dependency parser: nested brackets not allowed inside Identifier")
						processingBracketedArgument = True
					elif cc == ')':
						if not processingBracketedArgument:
							break
						processingBracketedArgument = False

					result += cc
					cc = self.getc()

				if cc:
					self.ungetc(cc)

				if not result:
					break

				if result in self.OPERATOR_IDENTIFIERS:
					return (self.OPERATOR, result)

				return (self.IDENTIFIER, result)

			return (self.EOL, result)

		def symbolicToStringOperator(self, op):
			return self.OPERATOR_TABLE[op]

	class ProcessedExpression(object):
		pass

	class DependencySingleton(ProcessedExpression):
		# The flags argument is a symbolic operator like EQ, LE etc
		def __init__(self, name, flags = None, version = None):
			self.name = name
			self.flags = flags
			self.version = version

		def dump(self, printmsg):
			if self.flags:
				printmsg(f"{self.name} {self.flags} {self.version}")
			else:
				printmsg(f"{self.name}")

		def build(self):
			if self.flags is None:
				return UnversionedPackageDependency(self.name)
			else:
				return VersionedPackageDependency(self.name, flags = self.flags, ver = self.version)

	class BracketedTerm(ProcessedExpression):
		def __init__(self, term):
			self.term = term

		def dump(self, printmsg):
			self.term.dump(printmsg)

		def build(self):
			return self.term.build()

	class ConditionalExpression(ProcessedExpression):
		def __init__(self, inner, conditional = None):
			self.conditional = conditional
			self.inner = inner

		def add(self, child):
			assert(self.conditional is None)
			self.conditional = child

		def dump(self, printmsg):
			printmsg(f"IF")
			if self.conditional:
				self.conditional.dump(printmsg.nest())
			else:
				printmsg(f"ALWAYS FALSE")
			if self.inner:
				self.inner.dump(printmsg.nest())
			else:
				printmsg(f"NO INNER TERM")

		def build(self):
			conditionalTerm = self.conditional.build()
			innerTerm = self.inner.build()
			return ConditionalDependency(conditionalTerm, innerTerm)

	class AssociativeExpression(ProcessedExpression):
		def __init__(self, child):
			self.children = [child]

		def add(self, child):
			self.children.append(child)

		def buildTerms(self):
			result = []
			for child in self.children:
				result.append(child.build())
			return result

	class OrExpression(AssociativeExpression):
		def dump(self, printmsg):
			printmsg(f"OR")
			for child in self.children:
				child.dump(printmsg.nest())

		def build(self):
			return OrDependency(self.buildTerms())

	class AndExpression(AssociativeExpression):
		def dump(self, printmsg):
			printmsg(f"AND")
			for child in self.children:
				child.dump(printmsg.nest())

		def build(self):
			return AndDependency(self.buildTerms())

	def __init__(self, string):
		# infomsg(f"## Parsing \"{string}\"")
		self.lex = self.Lexer(string)

		self.lookahead = None

	def __str__(self):
		return str(self.lex)

	def nextToken(self):
		lookahead = self.lookahead
		if lookahead is not None:
			self.lookahead = None
			return lookahead

		type, value = self.lex.next()
		# infomsg(f"## -> type={type} value=\"{value}\"")
		return type, value

	def pushBackToken(self, *args):
		assert(self.lookahead is None)
		self.lookahead = args

	class BadExpressionException(Exception):
		def __init__(self, lexer):
			value = "".join(lexer.value)
			ws = " " * lexer.pos
			msg = f"Bad expression:\n{value}\n{ws}^--- HERE"
			super().__init__(msg)

	def BadExpression(self):
		return self.BadExpressionException(self.lex)

	def process(self, endToken = None):
		if endToken is None:
			endToken = self.Lexer.EOL

		leftTerm = None
		while True:
			type, value = self.nextToken()
			if type == endToken:
				break

			# infomsg("# About to process next term")
			if type == self.Lexer.RIGHTB or type == self.Lexer.EOL:
				infomsg(f"endToken={endToken}")
				raise self.BadExpression()

			groupClass = None

			if type == self.Lexer.IDENTIFIER:
				if value == "or":
					groupClass = self.OrExpression
				elif value == "and" or value == "with":
					groupClass = self.AndExpression
				elif value == "if":
					groupClass = self.ConditionalExpression

			if groupClass:
				if leftTerm is None:
					raise self.BadExpression()

				if not isinstance(leftTerm, self.AssociativeExpression):
					leftTerm = groupClass(leftTerm)
				elif leftTerm.__class__ != groupClass:
					infomsg("Cannot mix terms with different precendence")
					raise self.BadExpression()

				type, value = self.nextToken()

			if type == self.Lexer.LEFTB:
				term = self.process(endToken = self.Lexer.RIGHTB)
				term = self.BracketedTerm(term)
			else:
				if type != self.Lexer.IDENTIFIER:
					raise self.BadExpression()

				args = [value]

				type, value = self.nextToken()
				if type == self.Lexer.OPERATOR:
					args.append(value)

					type, value = self.nextToken()
					if type != self.Lexer.IDENTIFIER:
						raise self.BadExpression()

					args.append(value)
				else:
					self.pushBackToken(type, value)

				term = self.DependencySingleton(*args)

			if leftTerm:
				leftTerm.add(term)
			else:
				leftTerm = term

		return leftTerm

class ExpressionNodeDumper(object):
	def __init__(self, indent = "", func = infomsg):
		self.indent = indent
		self.func = func

	def nest(self):
		return ExpressionNodeDumper(indent = self.indent + "  ", func = self.func)

	def __call__(self, msg):
		self.func(f"{self.indent}{msg}")
