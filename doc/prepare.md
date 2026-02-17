# The ``prepare`` command and the hints file

This stage processes the dependencies of all rpms and generates an architecture-independent
view of the dependency graph. This internal representation of the codebase serves as the input
to all further steps of the processing pipeline. Its operation is controlled by a file called
``hints.conf`` in the model directory.

The prepare command looks at each architecture and each rpm for that architecture in turn, trying to
resolve their dependencies. The two most important aspects you should be aware of include
ambiguities, and conditionals.

## Conditionals in RPM dependencies

Many of our RPMs have conditional dependencies, and there are a bunch of commonly used
conditionals like ``systemd`` or ``kernel`` that have a major influence on the codebase.
In the hints file, you can define whether the resolver should assume these conditions
to evaluate to true of false. You do not _have to_ pre-define these conditionals, but it is
probably useful to define this for most of the ones you expect to be relevant eventually.

## Ambiguous RPM dependencies

The other aspect is ambiguous dependencies, where a single dependency by a given RPM can be
satisfied by more than one other package in the codebase. However, not all alternatives are
equally desirable, so the tool requires you to look at all ambiguities and decide on them
in one way or the other.

The simplest way to deal with an ambiguity is to just mark it as acceptable. Assume an rpm ``foobar``
requires ``dbus-service``, which is provided by both ``dbus-1-daemon`` and ``dbus-broker``.
You could just tell ``monkey prepare`` that you're fine with this and don't care, so you put the
following into ``hints.conf``:

```
accept-ambiguity
    dbus-broker
    dbus-1-daemon
```

As a consequence, in the codebase model, ``foobar`` will end up requiring both ``dbus-1-daemon``
and ``dbus-broker``, and if your product contains ``foobar``, it will pull in both these DBus packages.
If this is not what you want, and instead you plan to phase out ``dbus-1-daemon`` and use
``dbus-broker`` instead, you would put something like this into hints.conf:

```
prefer dbus-broker over dbus-1-daemon
```

One fairly common ambiguity is with mini packages; for quite a few packages, we have a
stripped-down copy with fewer dependencies, used to bootstrap new builds and/or resolve
build dependency loops. In the context of product composition, these minified rpms are
not desirable, so you want the prepare stage to ignore those packages.

Similar to the previous one are packaging conflicts between the 64bit and the 32bit version
of a package. For instance, there are quite a few packages where the same .so file is present
in both the 64bit and the 32bit flavor of a shared library package. If another package
(such as a -devel package) requires just the .so file, we want to make sure we resolve this
requirement with the correct rpm.

Another type of ambiguity results from true alternative implementations of the same tool,
shared library or java class. In the example above, we described a scenario involving ``dbus-service``
as a requirement. But there are more examples like this, such as about half a dozen rpms that all
provide ``servletapi`` - in this case, we pick one implementation as the main one (``servletapi5``)
and reduce any ambiguous alternatives to it:

```
prefer servletapi5 over servletapi4
prefer servletapi5 over geronimo-servlet-2_4-api
prefer servletapi5 over geronimo-servlet-2_5-api
prefer servletapi5 over tomcat10-servlet-6_0-api
prefer servletapi5 over tomcat11-servlet-6_1-api
```

Note that these rules just impact how we construct our internal representation of the codebase.
Real issues may impact in the final product, which is why it is important to understand any
ambiguities flagged by package monkey, and make a sound decision. As a noteworthy example,
there are busybox subpackages like ``busybox-find`` that provide an alternative ``/usr/bin/find``
implementation. When package monkey flags such things as problematic, it can save you some painful
bug reports if you take note and address them properly right away (which, in this case, means never
ever shipping a package like ``busybox-find``).


## Scenarios

Scenarios are another powerful way of dealing with certain ambiguous dependencies.

In essence, a scenario is a description of several alternative implementations, which you want to
handle as true alternatives in your model, and during composition. Typical examples include multiple
versions of the same project, like difference ``openjdk`` versions. If your OBS project contains ``openjdk21``
all the way through ``openjdk25``, this will generate quite a few ambiguous dependencies. In fact, more or
less every package in the Java cosmos ends up requring either ``java`` or ``java-headless``; and guess what,
each version of openjdk contains two RPMs that will satisfy these dependencies.

Conceptually, what scenarios do is to define a scenario variable (in this example, called ``jdk``), which
can take as its values the version numbers (21, 22, ... 25 - and even 1.8). In addition, we define sort of
fake rpm names like ``jdk/java-headless`` that expands to a different real rpm for each value of the
scenario variable: ``java-21-openjdk-headless``, ``java-22-openjdk-headless``, etc. When we encounter a
dependency that resolves to several such ``java-*-openjdk-headless`` packages, we replace it with this fake
rpm name ``jdk/java-headless`` and mark the requiring package as valid in scenarios ``jdk=21``, ``jdk=22``, etc.

Later, during composition, we then choose which version of OpenJDK to ship. Once we do this, we eventually
validate all scenario dependencies to make sure that the resulting product supports at least _one_ scenario
in which these doctored dependencies can be satisfied.

Just like in this Java example, we define similar version-based scenarios for Rust, Go or LLVM. However, in addition
to these types of scenarios, there are others, like the product scenarios. In the codebase, we have a certain
number of product-specific packages for branding, identifying the release, providing presets for things like
systemd, etc. In order to deal with these properly, we can define a scenario called ``product``, taking values
like ``sles``, ``sles-sap`` and ``sles-tukit``. Then, we can define scenario rpms like this:

```
scenario product/release
        sles:           SLES-release
        sles-tukit:     SLES_immutable-release
        sles-sap:       SLES_SAP-release
        sles-sap-tukit: SLES_SAP_immutable-release
        micro:          SL-Micro-release
        other:          ALP-dummy-release
```

Just like with the OpenJDK scenario, packages requiring a release package will have that dependency replaced with
``product/release``; and during the final stage of product composition, we will make sure that these requirements
are satisfiable for at least one of these product scenarios.
