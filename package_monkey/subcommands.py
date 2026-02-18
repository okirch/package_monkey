##################################################################
#
# Describe package_monkey commands and subcommands
#
##################################################################

import argparse
import os

from .util import loggingFacade, debugmsg, infomsg, warnmsg, errormsg

__names__ = ['subcommandRegistry', 'GenericSubcommand', 'OBSSubcommand', 'PackageMonkey']

##################################################################
# Very simple facility for registering the sub-commands
##################################################################
class SubCommandRegistry(object):
	def __init__(self):
		self.commands = []

	def registerCommand(self, command):
		assert(isinstance(command, GenericSubcommand))
		self.commands.append(command)

subcommandRegistry = SubCommandRegistry()

class GenericSubcommand(object):
	HELP = None
	ALIASES = []
	DESCRIPTION = None
	SUBCOMMANDS = []

	LOG_USE_TIMESTAMPS = True
	LOG_USE_LATE_STDOUT = False

	def __init__(self):
		pass

	def registerArguments(self, args):
		pass

	def createApplication(self, opts):
		raise NotImplementedError(f"subcommand {self.NAME}")

##################################################################
# base class for subcommands that want to talk to OBS
##################################################################
class OBSSubcommand(GenericSubcommand):
	OBS_HOST_DEFAULT = "api.suse.de"

	def __init__(self):
		self.args = None

	def registerArguments(self, args):
		args.add_argument('--obs-host', default = self.OBS_HOST_DEFAULT,
					help = f'Specify the OBS service to talk to (default: {self.OBS_HOST_DEFAULT})')
		args.add_argument('--obs-cache-strategy', default = None)

class PackageMonkey(object):
	def __init__(self, name):
		self.name = name
		self.args = argparse.ArgumentParser(name)
		self.opts = None

		self.args.add_argument('--model-path', default = '../SLFO',
					help = 'Specify where to find the model definition (default: $MONKEY_MODEL_PATH or "../SLFO")')
		self.args.add_argument('--codebase', default = None,
					help = 'Name of the codebase to inspect (default: $MONKEY_CODEBASE or "slfo")')
		self.args.add_argument('--statedir', default = '~/.local/package_monkey',
					help = 'Specify where to store generated data')
		self.args.add_argument('--cache', default = '~/.cache/package_monkey',
					help = 'Specify cache location')
		self.args.add_argument('--version', default = 'latest')
		self.args.add_argument('--quiet', action = 'store_true', default = False,
					help = 'Decrease verbosity (effect depends on the subcommand)')
		self.args.add_argument('--verbose', action = 'store_true', default = False,
					help = 'Increase verbosity (effect depends on the subcommand)')
		self.args.add_argument('--debug', action = 'append', default = [])
		self.args.add_argument('--trace', action = 'append', default = [])
		self.args.add_argument('--logfile', action = 'store')

		if subcommandRegistry.commands:
			subparsers = self.args.add_subparsers(help='supported subcommands', dest = 'command', metavar = 'SUBCOMMAND')

			cmdArgs = subparsers.add_parser('help', help = 'display help')
			cmdArgs.add_argument('topic', nargs = '?')

			self.registerSubcommands(subparsers, subcommandRegistry.commands)

	def registerSubcommands(self, subparsers, commandList):
		for command in commandList:
			cmdArgs = subparsers.add_parser(command.NAME, help = command.HELP,
						aliases = command.ALIASES,
						description = command.DESCRIPTION)
			command.registerArguments(cmdArgs)
			command.args = cmdArgs

			if command.SUBCOMMANDS:
				subsub = command.createSubParser(subparsers)
				command.dest = subsub.dest

				self.registerSubcommands(subsub, command.SUBCOMMANDS)

	def findSubcommand(self, name, commandList):
		for cmd in commandList:
			if cmd.NAME == name or name in cmd.ALIASES:
				if not cmd.SUBCOMMANDS:
					return cmd

				subName = getattr(self.opts, cmd.dest)
				return self.findSubcommand(subName, cmd.SUBCOMMANDS)

	def run(self):
		if self.opts is not None:
			raise Exception(f"You cannot step into the same river twice")

		self.opts = self.args.parse_args()

		if self.opts.codebase is None:
			self.opts.codebase = os.getenv("MONKEY_CODEBASE")
		if self.opts.codebase is None:
			self.opts.codebase = 'slfo'

		name = self.opts.command
		if name is None:
			self.args.print_help()
			return

		if name == 'help':
			if self.opts.topic is not None:
				name = self.opts.topic
				for cmd in subcommandRegistry.commands:
					if cmd.NAME == name or name in cmd.ALIASES:
						cmd.args.print_help()
						return

			self.args.print_help()
			return

		cmd = self.findSubcommand(name, subcommandRegistry.commands)
		if cmd is None:
			raise NotImplementedError(f"Command {name} not implemented?")

		self.initializeLogging(cmd)

		application = cmd.createApplication(self.opts)
		return application.run()


	def initializeLogging(self, cmd):
		if cmd.LOG_USE_LATE_STDOUT:
			useStdout = self.opts.verbose
		else:
			useStdout = not self.opts.quiet
		if useStdout:
			loggingFacade.enableStdout()

		if self.opts.logfile:
			loggingFacade.addLogfile(self.opts.logfile)

		if not cmd.LOG_USE_TIMESTAMPS:
			loggingFacade.disableTimestamps()

		infomsg(f"Starting {cmd.NAME} (codebase {self.opts.codebase})")

		# --debug <facility> enables debugging for a specific facility
		# default: log all messages logged through util.debugmsg()
		# all: log all messages logged through any logger's debug() method
		for facility in self.opts.debug:
			if ',' in facility:
				for facility in facility.split(','):
					loggingFacade.setLogLevel(facility, 'debug')
			else:
				loggingFacade.setLogLevel(facility, 'debug')

		if loggingFacade.isDebugEnabled('obs'):
			debugmsg("obs debugging enabled")

