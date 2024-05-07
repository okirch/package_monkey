
# Playing with package monkey

This is very much work in progress, so it's really rough around the edges.
In particular, this piece of code does not install stuff anywhere, you're
basically using it inside your git checkout.

## Building and getting started

Before you can start, you need to build the fastsets python extension:

 make -C fastsets

Then, you need to run the labelling engine at least once on the cached
product DB:

 ./label-groups

This will create several files in the alp/ subdirectory:

 alp/packages.csv
 alp/packages.xml
 alp/hierarchy.xml

## Queries you can run

### Inspect individual packages

pinfo

...

### Inspect components

query-components

...

