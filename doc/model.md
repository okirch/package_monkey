# Classification language for package monkey

Package Monkey tries to simplify the task of defining a product composition
by letting you group packages (or rather, builds) into what it calls epics
and layers, and letting you compose a product by selecting and deselecting specifc
layers or epics.

## Terminology

### Codebase vs Product

A product is, well, what we deliver to customers as a collection of packages, with a support
contract, possibly with media, and a steady stream of maintenance updates. Think of SLES,
SLES for SAP or the HA Extension.

The codebase is the collection of packages from which a family of products is built, or
_composed_. In the SLE16 family of products, the main body of packages lives in OBS projects like
``SUSE:SLFO:Main`` or ``SUSE:SLFO:1.2``.

### Builds vs RPMs vs Packages

A _build_ is essentially an OBS package built in a specific configuration, such as ``glibc`` or
``bash``. Depending on the build service setup, a build generates a certain number of rpms, usually
on the order of a few. However, some builds can be empty and not generate any (binary) rpms.

In the presence of multi-builds, there may be several builds of a single OBS package;
these are usually tagged with a suffix, separate from the package name by a colon, as in
``gdb:testsuite``. For example, as of this writing, there are the following builds for the
glibc package:

- ``glibc``
- ``glibc:cross-aarch64``
- ``glibc:cross-ppc64le``
- ``glibc:cross-riscv64``
- ``glibc:cross-s390x``
- ``glibc:i686``
- ``glibc:utils``

These are considered separate builds by package monkey, and can be labelled independently.


### Labelling the codebase

When composing one or more products, you do not feed package monkey with a list of individual
rpm names like we used to do with pkglistgen. Instead, composition selects and deselects labels
that we want or do not want the product to include; and certain rules that refine these
selections.

One pretty central paradigm of package monkey is to distinguish between the labelling, as a property
of the code base, and the composition, as a property of the product. Phrased differently, we
should try to keep product decisions out of the codebase and its labelling as much as we can,
so that the product composition can be as flexible as it wants and needs to be.

There are two primary types of labels that can be used to structure the codebase.


#### Epics

The primary type of label is the _epic_. An epic contains a certain number of builds that
belong together, usually conceptually. For example, we can have an epic called ``CoreCompression``,
intended to contain a set of compression libraries and tools that are somehow essential to
what we ship. Its (simplified) definition may look like this:

```
 CoreCompression:
   description: |
     Provides a set of compression libraries and utilities, including bzip2 and xz.

   builds:
    - gzip
    - bzip2
    - zlib
    - zlib-ng
    - xz
    - zstd
    - brotli
    - lz4
    - lzo
    - libzio
```

Note that any build can be assigned to one epic at most.

#### Layers

In addition, there are _layer_ labels, which provide a grouping for epics. For example, we
can have epics for various aspects of the virtualization stack (like ``Qemu``, ``Libvirt``,
``VirtManager``, etc), which are aggregated in a layer called ``LayerVirtualization``.
While composing the product, you can, for example, include the entire virtualization stack but
exclude one specific epic like ``Kubevirt``. Or you could limit the layer to a subset of the
available architectures by specifying architecture constraints for it.

### Handling RPMs

In the world of package monkey, any (binary) RPM must originate from exactly one build. If there are
several builds that produce the same RPM, this is usually considered an error. The code has some heuristics
to deal with such situations, so that it continues to do _something_ in these circumstances, but at
the end of the day, these ambiguities need to be resolved by the release managers.

So, with every RPM being covered by one build, the labelling information can be used to control what
goes into a product and what doesn't. However, including the rpms from a given build is rarely an
all or nothing decision.

For instance, we may want to ship our product with all sorts of audio and video codecs, but without the
corresponding development packages because we consider these codecs an implementation detail. So it
would be useful if we could tell the composer, "include these epics, but leave out any devel packages
that belong to them".

In a similar vein, there are packages that, in addition to providing certain functionality (let's say
advanced forbnication), also come with a variety of langugage bindings. We would like to tell the
composer, "include ``AdvancedFrobnication``, and while we want the python bindings, leave out the
ocaml and ruby bindings".

Both are possible to express in package monkey. The former case is addressed by what we call *rpm classes*,
and the latter is handled via so-called *build options*.

#### RPM Classes

The SUSE build conventions require that different aretefacts generated by the build of a package
go into individual rpms. For example, consider the ``audit`` package. It has a base build called
just ``audit`` that provides its libraries, and ``audit:secondary`` that generates RPMs for the
tools, the audit system group, and default rules:

```
Build audit:
  libaudit1
  libauparse0
  audit-devel
  audit-devel-32bit
  libaudit1-32bit
  libauparse0-32bit

Build audit:audit-secondary:
  system-group-audit
  audit
  audit-rules
  audit-audispd-plugins
  python3-audit
```

Obviously, there are some patterns in there. For example, there are _library_ packages, _development_ packages,
some _32bit_ packages, etc. This is why package monkey has the concept of **rpm classes**. The following
table provides a overview of the most important classes currently defined by the model:

|Class          |Description                                |
|---------------|-------------------------------------------|
|user           |``system-user-*`` and ``system-group-*`` packages |
|libraries      |packages containing shared libraries |
|default        |packages for utilities, CLIs etc |
|api            |``*-devel`` packages and the like |
|doc            |documentation packages |
|apidoc         |documentation packages for developers |
|32bit          |packages containing 32bit libraries and tools |
|32bit_api      |32bit ``*-devel`` packages |
|i18n           |``*-lang`` packages |
|man            |packages containing manpages |

These classes can be used to fine-tune which parts of a build make it into a product once the containing
epic is selected. For example, in SLES 16, we exclude any 32bit packages globally. Similarly, we may decide
that we consider certain libraries and their interfaces an implementation detail, and not ship any devel
packages for those. This is what we do for multimedia packages in SLES 16.

Thanks to the packaging conventions, there is a relatively uniform approach to naming rpm subpackages that
help to map rpm names to classes pretty much automatically. However, it is also possible to override the
class assignment per RPM.

#### Build Options

As mentioned above, builds often generate subpackages that are ephemeral in some sense; these can be
language bindings for a library, or backends for a range of different implementations. These are singled
out into subpackages precisely because we want to allow users to install the main package without pulling
in all of ocaml, ruby, or all sorts of database drivers. It is ultimately the user's choice which database
backend they want (if any), or which programming language they want to use a given library with.

However, in composing a product, we make some of these choices for them, by excluding or including various
options in the product.

To make this less abstract, consider Gtk, the Gnome Toolkit. The codebase currently contains 3 versions
of it, and consequently, we defined 3 epics, one for each version.

Now, there are packages outside of Gtk that produce RPMs with bindings for some or all of these versions.
One of them is IBus, a framework of input methods for Gtk-based applications. It provides a subpackage
with a driver for each specific gtk version, ``ibus-gtk``, ``ibus-gtk3`` and ``ibus-gtk4``,
respectively. Now, how would we compose a product that contains all of ibus but not Gtk2?

We can either deal with these by excluding individual rpms from being shipped, i.e. we edit the ``IBus``
epic and exclude ``ibus-gtk``. This is possible, but tedious and error prone, because we have to do it
for *every* package that depends on Gtk2, and if we at some point revert that decision, we would have
to go back and find all those individual packages we excluded. In addition, this means that decisions
about the eventual product composition get encoded in the labelling.

An alternative approach is to use so-called _build options_. A build option can be considered like a
switch that can enable or disable certain functionality across all epics in the labelled codebase.
It is implemented as just another label type in package monkey's universe of labels.

In the example of Gtk, we define three build options ``gtk2``, ``gtk3`` and ``gtk4``, respectively.
You can think of a build option as subsets of their corresponding epics. Inside the ``gtk2`` option,
we place ``libgtk-2`` and a couple of other Gtk2 packages - and now the magic happens.  The composer
detects that ``ibus-gtk`` depends on the ``gtk2`` build option, so when we disable this build option,
we also exclude anything that depends on an RPM that belongs to this option - such as ``ibus-gtk``.

Describing this in terms of epics rather than rpms; a build option refines which bits inside an epic
end up in the product if we include the epic. If rpms in an epic depend on a build option that is *enabled*,
these are *included* along with everything else in the epic. If rpms in an epic depend on a build option
that is *disabled*, these are *excluded* even if the rest of the epic is included.

Similar approaches can be used to handle shell completion packages (for ``zsh`` or ``fish``, for instance),
or the various GCC frontends for different programming languages, or for separating the LLVM runtime
from the full LLVM stack.

However, it is worth noting that build options only really make sense if they represent a somewhat global
aspect of the codebase, like programming languages, or alternative implementations of say databases,
widget toolkits or spelling libraries, which impact the codebase more or less all over the place.

#### Extras

In addition to what we said about build options in the preceding section, there may be rpms in the
codebase that should be considered optional in a similar sense, but which are, conceptually more of
a one-off thing than a globally applicable build option.

As an example, consider the ``e2fsprogs`` package, which, in addition to lots of valuable low-level
rpms, provides something called ``e2fsprogs-scrub``. Nevermind what it does, this package depends on
``postfix``. Now ``e2fsprogs`` belongs to epic ``Filesystems.``

Unless we somehow make ``e2fsprogs-scrub`` optional, this means that whenever a composition includes
the ``Filesystems`` epic, we would automatically pull in ``postfix``, and with this, ``systemd`` and
everything *that* depends on, and we end up with a huge sticky lump of packages.

We could solve this by introducing a build option called ``postfix``, and stick most of the postfix
packages into it. However, this goes a bit against the grain of what build options were designed for.

As an alternative, we could use what is called _extras_:

```
  Filesystems:
    builds:
     - e2fsprogs
     ..
    
    extras:
       rpms:
        - e2fsprogs-scrub
```

Just like a build option, an extra is a subset of rpms that should be considered optional. Unlike build
options, however, extras are just _local_, not global. And while build options are subsets containing rpms
that optional packages may depend upon (eg ``libgtk-2``), an extra is a subset that contain the optional
packages themselves - in this case, ``e2fsprogs-scrub``. And, last but not least, build options impact
the product globally; if you include one, it automatically includes all optional packages (of included epics)
that depend on that option. On the other hand, enabling an epic does not automatically enable all extras
that belong to it. In other words, your product can include ``Filesystems``, but as long as you do not
explicitly enable ``Filesystems+postfix`` as well, ``e2fsprogs-scrub`` will not be part of it.

#### A note on promises

Currently, the model and the package monkey code make pretty heavy use of something called *promises*, which
is essentially a way of transforming certain rpm dependencies. They take the form ``promise:libfoo0`` where
``libfoo0`` is a library package that the promise represents. For the time being, you can safely ignore
anything related promises, as this concept will change significantly, and maybe even disappear entirely.

## File Syntax and Structure

### Main file

The main file that is loaded by package monkey to process the model definition is called ``filter.yaml``
and resides in the model directory. It includes all other definitions of releases, life cycles, and, most
importantly, the epics.

The main file will usually define things like the rpm class hierarchy, and the mapping of rpm name
patterns to classes (such as mapping ``*-32bit-*`` to class ``32bit``). We will not cover all elements
of the main file here, but postpone that to a separate section. Instead, we'll focus on the syntax
constructs you will probably deal with most frequently.

### Defining Epics

Let's consider the epic ``Glibc``, defined in ``core.yaml``:

```
epics:
 Glibc:
   description: The C library and other absolutely essential libraries.
   layer: LayerCore
   lifecycle: stable
   reviewer: team_slfo

   decisionlog: |
    - nscd is obsolete, do not ship it any longer.

   builds:
    - glibc
    - filesystem
    - libxcrypt
    - linux-glibc-devel
    - pkgconf
    - system-user-root

   rpms:
    - libnsl1             noship
    - nscd                noship
    - cross-*-glibc-devel api option=obs
```

The ``epics`` keyword starts the definition of epics, the first of which shown here is ``Glibc``.

The ``build`` attribute is a list of builds that belong to this epic. Two things are worth pointing out
here. One, you do not have to specify multibuilds explicitly, but you can. For example, you may want to
place ``openmpi4:standard`` in one epic, while placing its HPC optimized ``openmpi4:gnu-hpc`` build
in another one. However, unless multibuilds are placed explicitly, they will be put into the same epic
as the base build. In the example above, builds like ``glibc:cross-s390x`` would end up in Glibc, too.

Two, it is possible to use shell wildcards here as well as in the ``rpms`` attribute, using the ``*``
and ``?`` operators as well as character classes like ``[0-9]``.

**Implementation note**: Internally, package monkey uses a hand-crafted parallel string matcher capable
of matching a string against hundreds of patterns efficiently. However, if you make your patterns too
complex, it will cop out and you will end up with lots of successive calls to ``fnmatch``, which tends
to make things rather slow.

The ``rpms`` keyword helps you finetune how individual rpms are classified. This where you will often
make use of wildcards, especially when handling shared library packages that encode the soversion in the
package name.

In the example above, we see two common ways of fine-tuning. One is to mark a package as ``noship``,
which effectively blacklists the rpm so that it cannot be included in any product. This should mainly
be used with functionality we consider obsolete, or unsupportable, or otherwise unfit for shipping
as part of a SUSE product. (Another, similar mechanism, is to mark an rpm as ``private``, which we will
cover in section XXX).

The last element in the ``rpms`` list affects the glibc development packages for cross-compiling.
The keyword ``api`` is a class name, and tells the classifier to mark the rpms as belong to the ``api``
class. (This is rather redundant because this is happening anyway, thanks to the ``-devel`` suffix).
In addition, ``option=obs`` marks these rpms as depending on the build option ``obs``. In the
``SLFO`` model, we are using this option to mark packages that we generally need only for building
other packages, but do not intend to ship with any SLE product.

Having covered the two main attributes of an epic, let's turn to the full list of attributes
supported inside an epic (some of them shown above). Please be aware of how multi-line strings have
to be formatted in YAML, which is a bit peculiar. If a value starts with ``|`` immediately followed
by a newline, this indicates a multi-line string. The first line that follows sets the minimum
indentation that is stripped off by the YAML parser; any line that has a shorter indentation ends
the multi-line string and starts a new YAML node.

- The ``description`` (single-line or multi-line) can be used to specify a description of what the
  epic represents.

- The ``layer`` attribute specifies the layer this epic belongs to. The full hierarchy of layers
  and their dependencies are defined in ``filter.yaml``. You can omit this attribute and
  specify a ``default_layer`` at the top of the file instead. Epics without layer are not allowed.

- The ``lifecycle`` attribute specifies the lifecycle for this epic; for more details on lifecycles
  refer to section [Life Cycles](lifecycles.md). If you omit this, a default life is assigned,
  which is usually ``stable``.

- The ``reviewer`` attribute specifies a team or individual considered as gatekeeper for this epic.
  This mechanism is not used very much yet, but could help us going forward to avoid *happy little
  accidents* that cause frictions between release managers and maintainers.

- The ``decisionlog`` should be used to explain some of the decisions taken with respect to
  what we include or exclude, preferably with a jira or bugzilla reference.
  In the form shown above, the yaml parser will treat this field as a multi-line string,
  allowing you to format the log any way you like (as long as you take care of the indentation).

- The attributes ``architectures`` and ``exclude_architectures`` can be used to restrict
  packages belonging to the epic to a certain set of architectures. These fields are YAML
  lists, with elements from the set of known architectures.

- The ``requires_options`` list attribute can be used to specify one or more build options that
  the epic depends on. At composition time, if any of these options is not included in a product, the
  entire epic depending on them will be excluded as well.

- There is also a ``requires`` list attribute that can refer to other epics; this has been in
  heavy use in the past but is up for a rework some time in the future. It should be safe to ignore it
  for the time being.

### Annotating RPMs

As shown above, we can fine-tune how rpms are treated in the model by annotating them in the
``rpms`` list of an epic. As this is a pretty central aspect of package monkey, it is worth
looking at this in a bit more detail.

To begin with, it is possible to use shell glob patterns in this list. These patterns will
apply only to rpms that come from a build that belongs to the epic in question. In other words,
if you have a pattern ``lib*[0-9]`` in some application epic, this will apply to rpms from this
epic, not to those from other epics. 

When applying annotations, a given rpm name can match more than one pattern. The annotations from
these patterns are applied in a certain order. Longer matches take higher precedence than shorter
matches. An exact match (being the longest possible match) obviously has the highest precedence.
In addition, patterns associated with an epic take precedence over rules that map rpm names to
classes (these are defined in the main ``filter.yaml`` file).

The following annotations are currently supported:

- Class names, such as ``libraries``, ``runtime``, ``api`` etc will set the class of the rpm.
  You can find the full list of classes in ``filter.yaml``.

- ``private`` will mark the rpm as not intended for shipping with a product (it may still be pulled
  in via dependencies)

- ``noship`` will definitely exclude the rpm from being shipped at all, as well as anything that
  depends on it. Compositions that require a ``noship`` package will fry your CPU and erase your
  hard disk.

- ``option=xxx`` will mark the rpm as depending on option ``xxx``. This can be used to mark an
  existing rpm requirement more explicitly, but it can also be used to create such a dependency.
  For example, quite a few packages get marked by the SLFO model using ``option=obs``, indicating
  that they should only be used for building other packages, and are not for shipping with e.g. SLES.

- ``arch`` can be used to modify the set of architectures for which we allow an RPM to be
  shipped. ``arch=aarch64,x86_64`` will set the rpm's architecture set, ``arch-=ppc64le`` will
  remove architecture(s), and ``arch+=aarch64`` will add a (previously removed) architecture.
  Trying to add an architecture not supported by the actual rpm is ignored.

Caveat: YAML is reacting a bit odd to strings that start with an asterisk, as that is a special
syntax element. So if you want to use a pattern like ``*-devel``, you need to put it into quotation
marks, and include the annotation within this quoted string, like this:

```
  rpms:
   - "*-devel api option=obs"
```

### Defining Build Options

Build options can be defined in the context of an epic. As a consequence, a build option can only
include rpms that belong to this epic; it cannot combine rpms from different epics.

Here's how you could define an option called ``llvm21_runtime`` that provides just those libraries
needed to support OpenGL and ``bpftrace``:

```
 LLVM21:

 builds:
  - llvm21
 
 options:
   llvm21_runtime:
      rpms:
       - libLLVM21
       - libclang13
       - libclang-cpp21
```

This places the required libraries in a subset associated with ``llvm21_runtime``. Now, in your
product composition, you can enable this option while excluding the ``LLVM21`` epic in general,
causing just those three libraries to be shipped.

Of course, you can use annotations in rpm list of build options. However, it is not possible to
use ``option=xxx`` inside an option definition.

As a different example, consider how we define an option called ``systemd``. Its purpose is to
allow handling core packages that depend on ``libsystemd0`` and the like.

```
 Systemd:
   builds:
    - dbus-1
    - libgudev
    - libsecret
    - systemd
    ...

   options:
    systemd:
      builds:
       - systemd	class=api
       - libgudev	class=libraries
       - dbus-1		class=default
```

If you want to define more than one option within a single epic, you can do this. In case one
is supposed to be a superset of the other, use ``requires_options`` to express this relationship.

Note how this lists a number of builds rather than rpms. The class names in these annotations serve as
a kind of filter when selecting rpms from these builds. Without those, the option would include all
rpms that are part of e.g. the ``libgudev`` build. However, ``class=libraries`` limits the rpms that
become part of the subset to those that have class ``libraries``, or something lower (which includes ``user``,
``runtime`` and ``i18n``). So the option will contain ``libgudev-1*`` and the ``typelib`` package that goes
with it, but not the ``devel`` package, not the ``32bit`` package.

### Defining Extras

Extras are defined very much like build options:

```
 PacketCapture:
    builds:
     - libpcap
     ...

    extras:
      bluez:
        rpms:
	  - libpcap-devel-static
```

For reasons ineffable, ``libpcap-devel-static`` requires ``bluez-devel``. We could file a bugzilla for this
and start a discussion; or we could define a global build option ``bluez`` that indicates we're building with
Bluetooth support provided by bluez. In this case, defining an extra is definitely a hackish workaround, but
it does the trick. And if you feel it's worth it, you can explicitly include this extra in your product by
referencing it from the product composition file.
