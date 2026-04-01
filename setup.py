from setuptools import setup, find_packages

print(find_packages())

kwargs = {
      'name' : 'package_monkey',
      'version' : '0.9',
      'license' : "GPL-2.0-or-later",
      'packages': find_packages(),
      'scripts' : (
      	'monkey',
      ),
}

setup(**kwargs)
