
# the profile module needs to be implemented before anything else
from .profile import profiling
from .subcommands import *

__names__ = ['PackageMonkey']

class PackageInfoCommand(GenericSubcommand):
	NAME = 'rpminfo'
	ALIASES = ['ri', 'pi', 'pinfo', 'packageinfo']
	HELP = 'Display information on rpm(s)'

	def registerArguments(self, args):
		args.add_argument('--siblings', action = 'store_true', default = False,
						help = 'display other rpms the originate from the same OBS build')
		args.add_argument('--requires-only', action = 'store_true', default = False,
						help = 'only show required packages, hide list of required-by')
		args.add_argument('--provides-only', action = 'store_true', default = False,
						help = 'only show required-by packages, hide list of required')
		args.add_argument('--names-only', action = 'store_true', default = False,
						help = 'only display the package name(s). Useful in displaying the list of rpms displayed by a specific build')
		args.add_argument('--no-labels', action = 'store_true', default = False,
						help = 'do not display any labels')
		args.add_argument(dest = 'packages', metavar = 'PACKAGES', nargs = '+',
						help = 'list of rpms to query')


	def createApplication(self, opts):
		from package_monkey.cmd_pinfo import PackageInfoApplication

		return PackageInfoApplication(self.NAME, opts)

class BuildInfoCommand(GenericSubcommand):
	NAME = 'buildinfo'
	ALIASES = ['bi', 'binfo']
	HELP = 'Display information on build(s)'

	def registerArguments(self, args):
		args.add_argument('--requires-only', action = 'store_true', default = False,
						help = 'only show required packages, hide list of required-by')
		args.add_argument('--provides-only', action = 'store_true', default = False,
						help = 'only show required-by packages, hide list of required')
		args.add_argument('--names-only', action = 'store_true', default = False,
						help = 'only display the package name(s). Useful in displaying the list of rpms displayed by a specific build')
		args.add_argument('--no-labels', action = 'store_true', default = False,
						help = 'do not display any labels')
		args.add_argument(dest = 'packages', metavar = 'PACKAGES', nargs = '+',
						help = 'list of builds to query')


	def createApplication(self, opts):
		from package_monkey.cmd_pinfo import BuildInfoApplication

		# We do not support the --siblings option, as we display all siblings in a build, anyway
		opts.siblings = False

		return BuildInfoApplication(self.NAME, opts)

class PackageDiffCommand(GenericSubcommand):
	NAME = 'packagediff'
	ALIASES = ['pd', 'pdiff']
	HELP = 'display what has changed between two labelling runs'
	LOG_USE_TIMESTAMPS = False
	LOG_USE_LATE_STDOUT = True

	def registerArguments(self, args):
		args.add_argument('--added', dest = 'restrict', action = 'store_const', const = 'added',
				help = 'display only changes relating to added packages')
		args.add_argument('--removed', dest = 'restrict', action = 'store_const', const = 'removed',
				help = 'display only changes relating to removed packages')
		args.add_argument('--changed', dest = 'restrict', action = 'store_const', const = 'changed',
				help = 'display only changes relating to changed packages')
		args.add_argument(dest = 'oldPath', metavar = 'OLD-PATH', nargs = '?',
				help = 'name of the snapshot to compare against, or path of packages.csv file. Defaults to snapshot @latest')
		args.add_argument(dest = 'newPath', metavar = 'NEW-PATH', nargs = '?',
				help = 'new packages.csv file (defaults to $state/packages.csv)')

	def createApplication(self, opts):
		from package_monkey.cmd_pdiff import PackageDiffApplication

		return PackageDiffApplication(self.NAME, opts)

class DownloadRpmsCommand(OBSSubcommand):
	NAME = 'download'
	HELP = 'Download package information from OBS'

	def registerArguments(self, args):
		super().registerArguments(args)

		args.add_argument('--staging',
				help = 'Download packages from staging projects (either "all" or a comma separated list, such as A,B,C)')

	def createApplication(self, opts):
		from package_monkey.cmd_download import SolverDownloadApplication

		return SolverDownloadApplication(self.NAME, opts)

class ExtractRpmInfoCommand(OBSSubcommand):
	NAME = 'extractinfo'
	HELP = 'Obsolete command to extract auxiliary information from rpm headers.'

	def registerArguments(self, args):
		super().registerArguments(args)

	def createApplication(self, opts):
		from package_monkey.cmd_download import RpmHeaderExtractorApplication

		return RpmHeaderExtractorApplication(self.NAME, opts)

class ProcessSolverCommand(GenericSubcommand):
	NAME = 'prepare'
	ALIASES = ['prep']
	HELP = 'Process OBS package information into a database for further processing'

	def registerArguments(self, args):
		args.add_argument('--reslog', default = None)
		args.add_argument('--ignore-errors', action = 'store_true')
		args.add_argument('--pedantic', action = 'store_true', default = False,
					help = 'Actually try to resolve each rpm to validate the result of the what-require processing')
		args.add_argument('--trace-scenarios', action = 'store_true', default = False,
					help = 'Display information how we disambiguate using scenarios')
		args.add_argument('--only-arch', action = 'append')
		args.add_argument('--staging',
					help = 'Use packages from the indicated staging project (eg "A") in addition to the regular build project')
		args.add_argument('--trace', action = 'append', default = [],
					help = 'Enable tracing for packages and/or labels. Specify multiple times or use comma to separate strings to trace for')

	def createApplication(self, opts):
		from package_monkey.cmd_preproc import SolverApplication

		return SolverApplication(self.NAME, opts)

class CodebasePatchCommand(GenericSubcommand):
	NAME = 'patch'
	HELP = 'Add ghost rpms to codebase and fudge unresolved dependencies'

	def registerArguments(self, args):
		args.add_argument('--trace-scenarios', action = 'store_true', default = False,
					help = 'Display information how we disambiguate using scenarios')
		args.add_argument('--only-arch', action = 'append')
		args.add_argument('--staging',
					help = 'Use packages from the indicated staging project (eg "A") in addition to the regular build project')
		args.add_argument('--trace', action = 'append', default = [],
					help = 'Enable tracing for packages and/or labels. Specify multiple times or use comma to separate strings to trace for')

	def createApplication(self, opts):
		from package_monkey.cmd_preproc import PatchApplication

		return PatchApplication(self.NAME, opts)

class LabellingCommand(GenericSubcommand):
	NAME = 'classify'
	ALIASES = ['label']
	HELP = 'Use model description for the indicated codebase to label packages'

	def registerArguments(self, args):
		args.add_argument('--trace', action = 'append', default = [],
				help = 'Enable tracing for packages and/or labels. Specify multiple times or use comma to separate strings to trace for')


	def createApplication(self, opts):
		from package_monkey.cmd_label import LabellingApplication

		return LabellingApplication(self.NAME, opts)

class ComposeCommand(GenericSubcommand):
	NAME = 'compose'
	HELP = 'Compose product(s) for the indicated release'

	def registerArguments(self, args):
		args.add_argument('--release', default = None,
				help = 'name of product(s) to compose (defaults to release specified by codebase)')
		args.add_argument('--build-path', default = '../SLES',
				help = 'path to where the product composer files live')
		args.add_argument('--ignore-errors', action = 'store_true')
		args.add_argument('--trace', action = 'append', default = [],
				help = 'Enable tracing for packages and/or labels. Specify multiple times or use comma to separate strings to trace for')


	def createApplication(self, opts):
		from package_monkey.cmd_compose import ComposerApplication

		return ComposerApplication(self.NAME, opts)

class ExplainCommand(GenericSubcommand):
	NAME = 'explain'
	ALIASES = ['ex']
	HELP = 'Explain composer decisions on package(s)'

	def registerArguments(self, args):
		args.add_argument('--trace', action = 'append', default = [],
				help = 'Enable tracing for packages and/or labels. Specify multiple times or use comma to separate strings to trace for')
		args.add_argument(dest = 'packages', metavar = 'PACKAGES', nargs = '+',
						help = 'list of packages to query')


	def createApplication(self, opts):
		from package_monkey.cmd_compose import ErklaerBaerApplication

		return ErklaerBaerApplication(self.NAME, opts)

class ProductDiffCommand(GenericSubcommand):
	NAME = 'productdiff'
	ALIASES = ['cdiff']
	HELP = 'Display changes in the set of available packages, and their labels, between two runs'
	LOG_USE_TIMESTAMPS = False
	LOG_USE_LATE_STDOUT = True

	def registerArguments(self, args):
		args.add_argument('--release', default = None,
				help = 'name of product(s) to compose')
		args.add_argument('--ignore-product', action = 'append', default = [],
				help = 'ignore the given product or extension')
		args.add_argument(dest = 'srcfile', metavar = 'SRC-FILE', nargs = '?',
				help = 'name of the original productcomposer file, or @SNAPSHOT',
				default = '@latest')
		args.add_argument(dest = 'dstfile', metavar = 'DST-FILE', nargs = '?',
				help = 'name of the new productcomposer file, or @SNAPSHOT. Use latest production if omitted',
				default = '@@')

	def createApplication(self, opts):
		from package_monkey.cmd_prodcmp import ProductDiffApplication

		return ProductDiffApplication(self.NAME, opts)

class EpicListCommand(GenericSubcommand):
	NAME = 'list'
	HELP = 'list epics'
	LOG_USE_TIMESTAMPS = False
	LOG_USE_LATE_STDOUT = True

	def registerArguments(self, args):
		args.add_argument(dest = 'epics', metavar = 'EPICS', nargs = '*',
				help = 'list of epics or layers to query')

	def createApplication(self, opts):
		from package_monkey.cmd_epicinfo import EpicListApplication

		return EpicListApplication(self.NAME, opts)

class EpicShowCommand(GenericSubcommand):
	NAME = 'show'
	HELP = 'show contents of epic'
	LOG_USE_TIMESTAMPS = False
	LOG_USE_LATE_STDOUT = True

	def registerArguments(self, args):
		args.add_argument('--display-empty-builds', action = 'store_true',
				help = 'Builds that do not generate rpms are usually not display. This option forces them to be shown.')
		args.add_argument(dest = 'epics', metavar = 'EPICS', nargs = '*',
				help = 'list of epics or layers to query')

	def createApplication(self, opts):
		from package_monkey.cmd_epicinfo import EpicShowApplication

		return EpicShowApplication(self.NAME, opts)

class EpicInfoCommand(GenericSubcommand):
	NAME = 'epicinfo'
	ALIASES = ['epics', 'einfo', 'ei']
	SUBCOMMANDS = [EpicListCommand(), EpicShowCommand()]
	HELP = 'Display information on layers and epics'

	def registerArguments(self, args):
		args.add_argument('--terse', action = 'store_true', default = False)

	def createSubParser(self, subparsers):
		return self.args.add_subparsers(dest = 'query', help = 'query command', metavar = 'QUERY')

class ChartCommand(GenericSubcommand):
	NAME = 'make-chart'
	ALIASES = ['chart']
	HELP = 'Generate a dot(1) displaying the label graph'

	def registerArguments(self, args):
		args.add_argument(dest = 'graph_type', metavar = 'GRAPH_TYPE', nargs = "?",
					help = 'Type of graph to create (epics, layers)',
					default = 'epics')

	def createApplication(self, opts):
		from package_monkey.cmd_chart import ChartApplication

		return ChartApplication(self.NAME, opts)

class SnapshotCommand(GenericSubcommand):
	NAME = 'snapshot'
	HELP = 'Snapshot generated files'
	DESCRIPTION = '''
		Take a snapshot of output files, for later comparison. 
		Can be used with packagediff and productdiff to compare
		against well-known results.
		'''

	def registerArguments(self, args):
		args.add_argument('--with-rpms', action = 'store_true',
					help = 'Create a snapshot of the downloaded rpmhdrs as well (adds 5-10 MB to a snapshot)')
		args.add_argument(dest = 'slug', metavar = 'SLUG', nargs = '?',
					help = 'The name by which this snapshot will be accessible',
					default = 'latest')

	def createApplication(self, opts):
		from package_monkey.cmd_snapshot import SnapshotApplication

		return SnapshotApplication(self.NAME, opts)

class PublishCommand(GenericSubcommand):
	NAME = 'publish'
	HELP = 'Publish generated files'
	DESCRIPTION = '''
		Push the generated files to some directory for publication.
		You will still have to perform any git commit operations etc
		manually.
		'''

	def registerArguments(self, args):
		args.add_argument(dest = 'path', metavar = 'PATH',
					help = 'Directory to which to publish generated files to')
		args.add_argument(dest = 'slug', metavar = 'SLUG', nargs = '?',
					help = 'The name of the snapshot to publish (current results by default)',
					default = None)

	def createApplication(self, opts):
		from package_monkey.cmd_snapshot import PublishApplication

		return PublishApplication(self.NAME, opts)

class OwnerSignoffCommand(GenericSubcommand):
	NAME = 'owner-signoff'
	HELP = 'Component owner signoff'
	DESCRIPTION = '''
		Component owner acknowledges the validity of a component
		'''

	def registerArguments(self, args):
		args.add_argument('--release',
					help = 'name of product release the sign-off applies to')
		args.add_argument('--hash',
					help = 'git hash to sign off (defaults to HEAD)')
		args.add_argument(dest = 'owner', metavar = 'OWNER',
					help = 'owner who provided sign-off (email or maintainer ID)')

	def createApplication(self, opts):
		from package_monkey.cmd_owner import OwnerSignoffApplication

		return OwnerSignoffApplication(self.NAME, opts)

subcommandRegistry.registerCommand(DownloadRpmsCommand())
subcommandRegistry.registerCommand(ExtractRpmInfoCommand())
subcommandRegistry.registerCommand(ProcessSolverCommand())
subcommandRegistry.registerCommand(CodebasePatchCommand())
subcommandRegistry.registerCommand(PackageInfoCommand())
subcommandRegistry.registerCommand(BuildInfoCommand())
subcommandRegistry.registerCommand(LabellingCommand())
subcommandRegistry.registerCommand(ComposeCommand())
subcommandRegistry.registerCommand(ExplainCommand())
subcommandRegistry.registerCommand(EpicInfoCommand())
subcommandRegistry.registerCommand(PackageDiffCommand())
subcommandRegistry.registerCommand(ProductDiffCommand())
subcommandRegistry.registerCommand(ChartCommand())
subcommandRegistry.registerCommand(SnapshotCommand())
subcommandRegistry.registerCommand(PublishCommand())
subcommandRegistry.registerCommand(OwnerSignoffCommand())
