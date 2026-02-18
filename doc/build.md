## Building and getting started

The code for package monkey is available from ```gitea@src.suse.de:okir/package_monkey.git```.

Before you can start, you need to have the fastsets python extension. It is available from
```https://github.com/okirch/python-fastset``` or from IBS in ```home:okir:SLFO-tools```

Install package monkey using:

```
python3 setup.py install --prefix /usr
```

The current classification and composition files for SLFO are available from
```gitea@src.suse.de:okir/SLFO.git```. Unless you wish to start from scratch and design your
own model, you probably want this checked out next to package monkey.
