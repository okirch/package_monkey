# What is it?

Package Monkey is a Python command-line toolchain for analyzing an RPM/OBS
codebase, structuring packages into logical components and use cases, and
composing products from selected components. It is primarily aimed at
SUSE/openSUSE product and package composition workflows rather than being a
general-purpose product assembler.

The main entry point is the `monkey` command.

Package Monkey is organized as a pipeline. The individual stages can be run
separately, which makes it possible to iterate on package classification or
product composition without repeating the slower download and dependency
processing steps.

## Model directory

Package Monkey needs an external model directory describing the codebase,
classification rules, and product composition policy.

The model directory contains data such as:

- codebase and OBS project definitions,
- dependency resolution hints,
- package classification rules,
- lifecycle information,
- product composition definitions.

This repository contains the tooling. The product-specific model data is expected
to live separately.

## Pipeline overview

### `monkey download`

Downloads package, build, and RPM metadata from the configured OBS projects. This
includes RPM headers and build information used by later stages.

### `monkey prepare`

Processes RPM dependencies and produces an internal, architecture-independent
view of the codebase and dependency graph. This stage uses dependency hints from
the model directory to resolve ambiguous or difficult cases.

### `monkey classify`

Applies labels, classes, lifecycle data, and other model-defined metadata to the
prepared package graph. The classification output is used by inspection tools and
by the product composition stage.

### `monkey compose`

Generates product composition output from the labelled codebase and the
composition policy in the model directory. Outputs include files such as
`default.productcompose` and lifecycle YAML files.

## Inspection and debugging commands

Package Monkey includes tools for understanding the generated package model:

- `monkey rpminfo <rpm>`: show information about an RPM, including dependencies,
  reverse dependencies, architecture-specific requirements, classification, and
  lifecycle data.
- `monkey buildinfo <build>`: show information about all RPMs produced by an OBS
  build.
- `monkey epicinfo ...`: inspect epics, layers, and associated builds.
- `monkey explain <rpm>`: explain why a package is included in a product
  composition.

These commands are useful for answering questions such as "why is this package
included?", "what depends on this?", and "which component owns this RPM?".

## Change tracking and publishing commands

The toolchain also supports comparing and publishing generated state:

- `monkey snapshot`: save the current generated state for later comparison.
- `monkey packagediff`: compare package/classification changes between states.
- `monkey productdiff` / `monkey cdiff`: compare product composition output.
- `monkey publish`: copy generated outputs to a target directory for publication
  or further review.

## Documentation

More detailed documentation is available in the guide:

- [Package Monkey Guide](doc/index.md)
- [Overview](doc/overview.md)
- [Codebase Analysis](doc/prepare.md)
- [Classification](doc/classify.md)
- [Model](doc/model.md)
- [Composition](doc/compose.md)
- [Tools](doc/tools.md)
- [Build and Install](doc/build.md)
- [Tips and Tricks](doc/tricks.md)
