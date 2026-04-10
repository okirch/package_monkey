#!/usr/bin/python3

from package_monkey import *

monkey = PackageMonkey('monkey')
exitval = monkey.run()

if type(exitval) != int:
	exitval = int(bool(exitval))

exit(exitval)
