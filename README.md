
# Playing with package monkey

This is very much work in progress, so it's really rough around the edges.
In particular, this piece of code does not install stuff anywhere, you're
basically using it inside your git checkout.

## Building and getting started

Before you can start, you need to have the fastsets python extension. It is available
from https://github.com/okirch/python-fastset or from IBS in home:okir:SLFO-tools

Install package monkey using:

 python3.11 setup.py install --prefix /usr

## Theory of operation

Downloads the latest rpmhdrs from OBS for each repository/arch combination, and builds a solver file from it

monkey --log download.log download

Extract header info like summary, description etc. This is not strictly necessary, but may be helpful if inspecting the codebase:

monkey extractinfo

The next steps will need a model that describes how to handle the codebase. For SLFO, you will find the product and model descriptions
in https://src.suse.de/okir/SLFO. All subsequent steps assume that you have checked out this git repo and made it accessible as
../SLFO.

The next step is to preprocess the raw set of rpms and extract their dependency information:

```monkey --log prepare.log prep

This produces a "database" that describes the rpms and their dependencies in a way that abstracts from the
different architectures. Calling it a database is a bit of an overstatement; it's really just a text file...

Note that this stage will often cause trouble when the codebase is still heavily in flux. The prepare stage
will detect ambiguities while resolving dependencies and assess them based on the contents of the hints.conf
file. The ambiguity may be acceptable; or it may be due to the codebase providing different versions of the
same package, etc. Unknown ambiguities are usually considered an error, however.

This information can be used as input into the labelling stage:

monkey --log label.log label

This generates a model of the codebase that assigns packages to components (aka epics).

The final step is to compose products from this model by describing which components should be included in a product, and which
should not.

monkey compose

The output of this stage is a couple of files. Some of them can serve as input to the product composer, or try to
mimic the actual output of the product composer, as well as a yaml file describing the components.
The components.yaml file is designed primarily with the goal of facilitating a review by component owners. 

## Queries you can run

### Inspect individual packages

monkey pinfo

This command lets you display information about individual packages, and their dependencies. Please use the --help
command to display what you can do with it.

### Inspect components

monkey epics

This command lets you display information about epics, and their dependencies.
Try using

```monkey epics what-requires [--only-rpms] Systemd

or

```monkey epics show KernelPlus

Please use the --help command for further details.

## Signoffs

When a component owner has reviewed all epics they are responsible for, and agrees with what ends up in the product, 
we can document their approval by using sign-offs:

  monkey owner-signoff okir@suse.com

Of course, instead of the owner email, you can also use the ID (such as team_kernel).

This will look at all epics owned by this maintainer, and generate a record for each of them in $MODEL/$RELEASE/signoffs.txt
This information is picked up by the composer, and added to composer.yaml.
