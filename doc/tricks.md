# Useful Tips and Tricks

## List packages that have unresolved dependencies

Package monkey uses a special rpm name to represent an unresolvable dependency, called ``__unresolved__``.

In order to list all rpms that have an unresolved dependency, use the following command:

```
$ monkey rpminfo __unresolved__
__unresolved__ (Unresolved-unresolved)
  architectures: none
  required by:
    - aws-cli
    - build-mkdrpms (CoreWorkbench+obs-default)
    - caca-utils (AsciiArt-default)
    - ceph-mgr-cephadm (Rados-default)
    - ceph-mgr-dashboard (Rados-default)
    - ceph-mgr-diskprediction-local (Rados-default)
    - ceph-mgr-k8sevents (Rados-default)
    - ceph-mgr-modules-core (Rados-default)
    - ceph-mgr-rook (Rados-default)
...
```

If you want to find out which dependency is missing for a given package, just display the
information on that package:

```
$ monkey rpminfo ceph-mgr-cephadm
ceph-mgr-cephadm (Rados-default)
  version: 18.2.7
  architectures: x86_64 s390x ppc64le aarch64
  summary: Ceph Manager module for cephadm-based orchestration
  lifecycle: stable
  OBS build: ceph
  requires:
    - __unresolved__ (Unresolved-unresolved)
    - ceph-mgr (Rados-default)
    - cephadm (Rados-default)
    - openssh (OpenSSH-default)
    - python313-CherryPy (PythonCommons-default)
    - python313-Jinja2 (PythonCommons-default)
    - python313-natsort (Python-default)
  unresolvable requires:
    - python3-asyncssh
  not required by anything
```

## Editing tips for yaml files

With YAML relying on indentation to express nesting of data structures, it becomes sort of
important how your editor translates TABs to space. To be on the safe side, I have opted to expand
all tabs in the yaml files involved here. All files have a vi modeline that automatically enables TAB
expansion when you open the file; you may need to use ``set modelines`` in your global ``.virmc`` file
to make vim parse them.

If you're using a different editor, which requires a different way of ensuring the equivalent effect,
feel free to send me patches.
