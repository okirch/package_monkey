##################################################################
#
# Simple profiling decorator.
# This is not for application-level profiling a la cProfile,
# but can be used to select specific functions you want to
# measure.
#
# Simply decorate individual functions with @profiling, and
# run your application with --profile
#
##################################################################
import yaml
import time
import sys
import atexit

class Profiling:
	_instance = None

	def __init__(self):
		self.enabled = False
		self.verbose = False
		self.functions = {}

	def __del__(self):
		self.disable()

	@classmethod
	def instance(klass):
		if klass._instance is None:
			klass._instance = Profiling()
		return klass._instance

	def enable(self):
		if not self.enabled:
			print("Enable profiling")
			self.enabled = True

			atexit.register(self.exitHandler)

	def disable(self):
		self.enabled = False
		atexit.unregister(self.exitHandler)

	def registerFunction(self, function):
		self.functions[function.name] = function

	def exitHandler(self):
		self.report()

	def report(self):
		if not self.enabled or not self.functions:
			return False

		print()
		print("*** Profiling information for selected functions ***")
		for key, function in sorted(self.functions.items()):
			if function.invocations == 0:
				continue
			print(f"{key:40} {function.invocations:4} calls; {function.accumTime/function.invocations:.3} sec avg; {function.accumTime:.3} sec total")

class FunctionTrampoline(object):
	def __init__(self, f):
		self.name = f"{f.__module__}.{f.__qualname__}"
		self.function = f

		self.invocations = 0
		self.accumTime = 0.0

	def __call__(self, *args, **kwargs):
		t0 = time.time()
		result = self.function(*args, **kwargs)
		self.accumTime += time.time() - t0
		self.invocations += 1
		return result

	# This is the only way I found that allows me to wrap a class method and
	# access the profiling trampoline without using an external lookup table
	# of some sorts
	def invokeClassMethod(objectSelf, *args, xxx_profileHandle = None, **kwargs):
		return xxx_profileHandle(objectSelf, *args, **kwargs)

def isClassMethod(f):
	if '.' not in f.__qualname__:
		return False
	
	vars = f.__code__.co_varnames
	if not vars or vars[0] != 'self':
		return False
	
	return True

def profiling(f):
	prof = Profiling.instance()
	if not prof.enabled:
		return f

	if prof.verbose:
		print(f"Creating trampoline for {f} {f.__qualname__}")

	handle = FunctionTrampoline(f)
	prof.registerFunction(handle)

	if isClassMethod(f):
		return lambda *args, **kwargs: FunctionTrampoline.invokeClassMethod(*args, xxx_profileHandle = handle, **kwargs)

	return handle

try:
	i = sys.argv.index('--profile')
except:
	i = -1
if i >= 0:
	prof = Profiling.instance()
	prof.enable()
	del sys.argv[i]

if __name__ == '__main__':
	prof = Profiling.instance()
	prof.verbose = True
	prof.enable()

	@profiling
	def myfunc(value):
		print(f"myfunc({value}) called")

	@profiling
	def slowfunc(value):
		return sum(range(value))

	class SomeClass:
		def __init__(self):
			self.counter = 0

		@profiling
		def method(self, value):
			self.counter += value

		def othermethod(self):
			pass

	myfunc(12)

	for k in range(1000):
		slowfunc(10000)

	x = SomeClass()
	x.method(42)
	assert(x.counter == 42)

	# this should not be profiled
	x.othermethod()

	print("Done.")
	exit(0)

