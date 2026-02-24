# Life Cycles

One of the key changes in SLES 16 is the introduction of life cycles that describe how a given product
component can be expected to evolve over the course of time.  They provide an indication of how frequently
we will introduce new upstream versions of packages, what to expect in terms of backward compatibility,
and at which points in time customers can expect these updates to happen.

Package monkey supports this by letting you define life cycles, and attach these to one or more epics. Many
lifecycles currently used in the SLFO model are defined in the file `00lifecycles.yaml`, but some are
defined directly where they are being used (such as for the rust and go components).

## Using Life Cycles

Before we dive into the details of how lifecycles are defined, let's take a quick look at how you apply
them - because that is the simpler part of it.

You can assign a life cycle to an epic by using the `lifecycle` attribute, as in the following example:

```
 OpenSSH:
   layer: LayerSecurity
   lifecycle: balanced

   builds:
    - openssh
```

If you do not set a life cycle explicitly, the default one will be used (which is `stable` in the
SLFO model).

## Standard SLES 16 life cycles

SLES 16 defines three standard life cycles called stable, agile and balanced, as follows:

```
lifecycles:
    stable:
        mode:           sequential
        stability:      compatible

    agile:
        description: |
          This component is being updated according to an agile model, which
          means that new upstream versions are provided on a regular basis
          as maintenance updates throughout the life time of a minor release.

        mode:           sequential
        cadence:        upstream
        stability:      upstream

    balanced:
        description: |
          This component is being updated regularly with every minor release of SLES.

        mode:           sequential
        cadence:        minor_release
        stability:      compatible

```

Let's look at the various attributes visible here.

Obviously, the most important part if the `description` field. This description will be displayed
to customers who want to understand how the life cycle actually works.

This life cycle `mode` describes how new versions of packages are introduced. In all three life cycles
shown above, the mode is `sequential`, meaning whenever we introduce a new upstream version, it replaces
the previous version.

There is also a mode called `versioned`, which indicates that we will ship several versions of the
same upstream package simultaneously. The mechanics around versioned life cycles will be explained in
a separate section below.

The `cadence` indicates how frequently new versions will be introduced. This can be one of the following:

- `minor_release`: expect updates to happen when we release a new minor version of the product.
- `upstream`: we plan to release new versions as maintenance updates whenever a new one becomes available
  upstream; plus a certain lag owing to the work related to packaging, testing and releasing the update.
- relative times, such as `3 months` or `1 year`

The `stability` keyword gives a rough indication of how disruptive changes will be.  In general,
the values from this field are copied to output files without further checks, but right now you should
adhere to the following conventions:

- `none` indicates we do not make any promises regarding ABI or API stability.
- `upstream` indicates that the primary focus is on being compatible with the upstream community,
  project, or standard.
- `compatible` indicates that we strive to make new versions backward compatible. Exceptions should be
  rare but are possible. If they occur, they will be documented.
- We *could* introduce more specifiy stability qualifiers, for instance expressing stability in relation
  to the package class. `libraries=compatible` would indicate ABI compatibility, `api=compatible` would
  indicate API compatibility, and `default=compatible` would indicate compatiblity at the CLI level.

## General Support vs LTS

For some components, we have defined life cycles that limit availability or support when the release
enters LTS. We're trying to support this by defining different *support contracts*, and referencing
them in the life cycles.

Right now, the SLFO model defines two support contracts, `general` for General Support, and `lts` for LTS.
The SLFO model also provides a tentative roadmap for upcoming releases, and the EOL dates for the
respective support contracts, to be found in `filter/00lifecycles.yaml`.

Here's an example of a lifecycle definition that sets an EOL date for both contracts:

```
lifecycle:docker-stable:
    description: |
      As part of SLES, we support and maintain a stable version of docker that is pinned at
      a specific major/minor release. From the date of release, this package is supported
      and maintained for 3 years. The version of docker-stable is the same across all code
      streams. At the end of these 3 years, a new version of docker-stable will be released.

    inherit:        stable
    mode:           sequential
    stability:      compatible
    releasedate:    2025-03-01
    general:
       eol:         2028-03-31
    lts:
       eol:         2028-03-31
```

In a similar vein, you can mark a component as unavailable under LTS, which is what we do for
the GCC _application compilers_:

```
gcc16:
    description: |
       This is the first GCC application compiler, to be released with SLES 16.1.

    ...
    general:
       duration:       2 years
    lts:
       enabled:        false
```

## Versioned Lifecycles

For some components, such as python, rust, go and openjdk, we plan to release new upstream versions
on a regular basis, maintaining several versions concurrently. Let's look at rust as an example,
which the SLFO model describes in `rust.yaml` (for clarity's sake, this is a slightly abridged version):

```
lifecycles:
   rust:
      description: |
        SUSE will publish updated rust packages roughly in sync with the upstream
        community, which is currently on a 6 week schedule.

        SUSE will maintain two rust releases concurrently; the most recent one
        as well as the previous release.

      mode: versioned
      stability: upstream
      cadence: 6 weeks
      general:
         concurrent_versions: 2
         duration: 6 months
      lts:
         enabled: no

   rust1.93:
      implement: rust
      releasedate: 2026-01-22

   rust1.92:
      implement: rust
      releasedate: 2025-12-11

   rust1.91:
      implement: rust
      releasedate: 2025-10-30
```

The crucial aspect illustrated here is that there is a general lifecycle called `rust`, which has
`mode=versioned`, and several version-specific lifecycles that *implement* `rust`.

Each of these version-specific lifecycles require EOL dates for the two support contracts. You could either
provide these manually, or you can have them populated by package monkey, using their release data plus the
`duration` value from the general `rust` lifecycle.

As a side node, there is a bit of hard-coded policy in package monkey, making it round out EOL dates to the
end of the month.

Now, the interesting part is in the definition of the epics:

```
 Rust:
   lifecycle: rust

   builds:
    - rust
    - cargo
    ...

   rpms:
    - rust-bindgen              private
    - corrosion                 private

 Rust1.93:
   lifecycle: rust1.93
   implement_scenario: rust=1.93

 Rust1.92:
   lifecycle: rust1.92
   implement_scenario: rust=1.92
```

The `Rust` epic itself provides a bunch of unversioned packages, including `rust` and `cargo`
which should just be front-ends to the most recent version available. In addition, there are
version-specific epics such as `Rust1.93`, which connects the lifecycle we defined for 1.93 and the
packages that make up rust 1.93. The latter happens by referencing the scenario `rust=1.93` which
we defined in `hints.conf`.

Right now, you still have to explicitly reference the rust epics you want your product to include
in `compose.yaml`.

Whenever your product composition includes an epic with a versioned lifecycle, like the `Rust` epic
shown above, it also includes epics that have an "appropriate" matching lifecycle that implements
the versioned lifecycle. Appropriate, in this context, means that the support window contains the
current date, plus a few other checks. If that is not the case, a warning is issued.

There is still room for improvement in this area. For example, we could tie scenarios and lifecycles
together even more closely; or we could make the composer aware of additional constraints like "the
product should always ship the two most recent versions of scenario `rust`".

## Composer Output

When running `monkey compose`, the tool outputs several files containing lifecycle data. One set
is `lifecycle-data-*.txt`, which contain information for consumption by the old zypper lifecycle plugin.

The other set, with files called `lifecycle-*.yaml`, contains lifecycle data for consumption via the
`suse-lifecycle` tool.
