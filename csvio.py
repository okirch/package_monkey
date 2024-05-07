##################################################################
#
# Convenience functions for dealing with CSV files
#
# Copyright (C) 2015-2023 Olaf Kirch <okir@suse.com>
#
##################################################################

import sys
import os
import csv
import copy
from util import debugmsg, infomsg, warnmsg, errormsg

##################################################################
# CSVWriter class
##################################################################
class CSVWriter:
	def __init__(self, filename, fields = []):
		self.__filename = filename
		self.__backend = None
		self.__count = 0
		self.__fields = copy.copy(fields)
		self.__headerWritten = False

		if filename:
			# Create the directory if needed
			dirpath = os.path.dirname(filename)
			if dirpath and not os.path.isdir(dirpath):
				os.makedirs(dirpath, 0o755)

			file = open(filename, "w")
		else:
			self.__filename = "stdout"
			file = sys.stdout
		self.__backend = csv.writer(file)

	@property
	def count(self):
		return self.__count

	@property
	def filename(self):
		return self.__filename

	def close(self):
		written = self.flush()
		if self.__backend:
			del self.__backend
		return written

	def flush(self):
		self.flushHeader()
		if self.__backend is not None:
			infomsg(f"Wrote {self.count} records to {self.filename}")
		return self.__count

	def addField(self, name):
		if self.__headerWritten:
			raise Exception(f"{self.filename}: cannot add header fields after writing out header")

		self.__fields.append(name)

	def addFields(self, list):
		if self.__headerWritten:
			raise Exception(f"{self.filename}: cannot add header fields after writing out header")

		self.__fields += list

	def write(self, row):
		self.flushHeader()

		if isinstance(row, CSVRow):
			self.__backend.writerow(row.values())
		else:
			# else should be a tuple or list
			self.__backend.writerow(row)
		self.__count += 1

	def writerow(self, row):
		return self.write(row)

	def writeObjectSlice(self, object, attrs):
		row = CSVRow()
		row.addObjectSlice(object, attrs)
		self.write(row)

	def newRow(self):
		return CSVRow()

	def flushHeader(self):
		if not self.__headerWritten:
			if self.__fields:
				self.__backend.writerow(self.__fields)
			self.__headerWritten = True

##################################################################
# CSVReader class
##################################################################
class CSVReader:
	def __init__(self, filename):
		self.__filename = filename
		self.__backend = None
		self.__count = 0
		self.__fields = []
		self.__headerSeen = False

		if filename:
			file = open(filename, "r")
		else:
			self.__filename = "stdin"
			file = sys.stdin
		self.__backend = csv.reader(file)

		try:
			header = next(self.__backend)
		except StopIteration:
			header = None

		if not header:
			warnmsg(f"{self.filename}: empty CSV file")
			return

		self.__fields = header
		self.__headerSeen = True

	def fields(self):
		if not self.__headerSeen:
			return []

		# use copy.copy?
		return self.__fields

	@property
	def count(self):
		return self.__count

	@property
	def filename(self):
		return self.__filename

	def close(self):
		if self.__backend is not None:
			infomsg(f"Read {self.count} records from {self.filename}")
			self.__backend = None
		return 0

	def read(self):
		try:
			values = next(self.__backend)
		except StopIteration:
			values = None

		if not values:
			return None
		self.__count += 1


		row = CSVRow()
		for s in values:
			row.addDecode(s)

		return row

	def readObject(self):
		row = self.read();
		if row is None:
			return None

		o = CVSRowObject(self.__fields)

		values = row.values()
		for i in range(len(values)):
			try:
				f = self.__fields[i]
			except:
				break

			setattr(o, f, values[i])

		return o;

class CVSRowObject:
	def __init__(self, fields):
		for f in fields:
			setattr(self, f, None)

class CSVRow:
	def __init__(self):
		self.__values = []

	def values(self):
		return self.__values

	def add(self, v):
		if v is None:
			v = ''
		elif type(v) != str:
			v = str(v)
		self.__values.append(v)

	def addDecode(self, v):
		self.__values.append(v)

	def addDictValue(self, dict, key):
		if dict and key in dict:
			self.add(dict[key])
		else:
			self.add('')

	def addDictSlice(self, dict, keys):
		for k in keys:
			self.addDictValue(dict, k)

	def addObjectSlice(self, object, attrs):
		for a in attrs:
			self.add(getattr(object, a, ""))

##################################################################
# CSV Helper functions
##################################################################
def csv_value(v):
	if type(v) != str:
		v = str(v)
	return v

def csv_dict_value(d, f):
	if not d or f not in d:
		return ''
	return csv_value(d[f])

def csv_dict_slice(dict, fields):
	result = []
	for f in fields:
		result.append(csv_dict_value(dict, f))
	return result
