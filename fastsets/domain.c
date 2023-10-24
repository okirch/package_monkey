/*
fastsets - domain objects

Copyright (C) 2023 SUSE

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, version 2.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program; if not, write to the Free Software Foundation, Inc.,
51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
*/

#include <stdio.h>
#include <stdbool.h>
#include <stddef.h>
#include "fastsets.h"
#include <structmember.h>

static PyObject *	Fastset_newDomain(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int		Fastset_initDomain(fastset_Domain *self, PyObject *args, PyObject *kwds);
static void		Fastset_deallocDomain(fastset_Domain *self);

static PyMemberDef	domain_TypeMembers[] = {
	{ "set", T_OBJECT_EX, offsetof(fastset_Domain, set_class), READONLY, },
	{ "member", T_OBJECT_EX, offsetof(fastset_Domain, member_class), READONLY, },
	{ NULL, }
};

static PyTypeObject *
fastset_DSTAlloc(fastset_Domain *domain, const PyTypeObject *typeTemplate, const char *typeName)
{
	fastset_DomainSpecificType *dst;
	const char *domainName = domain->name;
	PyTypeObject *newType;

	dst = calloc(1, sizeof(*dst));
	dst->magic = FASTSET_DST_MAGIC;
	dst->domain = domain;
	/* Note we do not INCREF domain here, else we would create a circular ref */

	newType = &dst->base;
	*newType = *typeTemplate;

	asprintf((char **) &newType->tp_name, "%s.%s", domainName, typeName);
	asprintf((char **) &newType->tp_doc, "%s class for fastset domain %s", typeName, domainName);

	if (PyType_Ready(newType) < 0) {
		fprintf(stderr, "Unable to initialize new type %s", newType->tp_name);
		abort();
	}

	return newType;
}

PyTypeObject	fastset_DomainType = {
	PyVarObject_HEAD_INIT(NULL, 0)

	.tp_name	= "domain",
	.tp_basicsize	= sizeof(fastset_Domain),
	.tp_flags	= Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
	.tp_doc		= NULL,

//	.tp_methods	= fastset_memberMethods,
	.tp_init	= (initproc) Fastset_initDomain,
	.tp_new		= Fastset_newDomain,
	.tp_dealloc	= (destructor) Fastset_deallocDomain,
	.tp_members	= domain_TypeMembers,
};


PyTypeObject *
fastset_CreateMemberClass(fastset_Domain *domain)
{
	return fastset_DSTAlloc(domain, &fastset_MemberTypeTemplate, "member");
}

static const fastset_DomainSpecificType *
Fastset_DSTGetType(PyObject *obj)
{
	PyTypeObject *type;

	for (type = obj->ob_type; type; type = type->tp_base) {
		fastset_DomainSpecificType *dst = (fastset_DomainSpecificType *) type;

		if (dst->magic == FASTSET_DST_MAGIC)
			return dst;
	}

	return NULL;
}

fastset_Domain *
Fastset_DSTGetDomain(PyObject *obj)
{
	const fastset_DomainSpecificType *dst;

	if ((dst = Fastset_DSTGetType(obj)) != NULL) {
		// printf("%s: object belongs to domain \"%s\"\n", __func__, dst->domain->name);
		return dst->domain;
	}

	// printf("%s: object type is %s\n", __func__, obj->ob_type->tp_name);
	PyErr_SetString(PyExc_RuntimeError, "unable to locate fastset domain for this object");
	return NULL;
}

PyTypeObject *
fastset_CreateSetClass(fastset_Domain *domain)
{
	return fastset_DSTAlloc(domain, &fastset_SetTypeTemplate, "set");
}

static PyObject *
Fastset_newDomain(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
	fastset_Domain *self;

	self = (fastset_Domain *) type->tp_alloc(type, 0);
	if (self == NULL)
		return NULL;

	/* init members */
	self->member_class = NULL;
	self->set_class = NULL;

	self->size = 0;
	self->domain_objects = NULL;

	return (PyObject *) self;
}

static int
Fastset_initDomain(fastset_Domain *self, PyObject *args, PyObject *kwds)
{
	static char *kwlist[] = {
		"name",
		NULL
	};
	char *domain_name = NULL;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "s", kwlist, &domain_name))
		return -1;

	self->name = strdup(domain_name);
	self->member_class = fastset_DSTAlloc(self, &fastset_MemberTypeTemplate, "member");
	self->set_class = fastset_DSTAlloc(self, &fastset_SetTypeTemplate, "set");

	return 0;
}

static void
Fastset_deallocDomain(fastset_Domain *self)
{
	if (self->name) {
		free(self->name);
		self->name = NULL;
	}

	Py_CLEAR(self->member_class);
	Py_CLEAR(self->set_class);

	/* Can this really happen? */
	if (self->domain_objects) {
		unsigned int i;

		for (i = 0; i < self->size; ++i) {
			Py_CLEAR(self->domain_objects[i]);
		}
		free(self->domain_objects);
		self->domain_objects = NULL;
		self->count = 0;
		self->size = 0;
	}
}

void
FastsetDomain_register(fastset_Domain *self, fastset_Member *member)
{
	int slot = -1;

	if (self->count < self->size) {
		unsigned int i;

		for (i = 0; i < self->size; ++i) {
			if (self->domain_objects[i] == NULL) {
				slot = i;
				break;
			}
		}
	}

	if (slot < 0) {
		static const unsigned int chunk_size = 16;

		/* allocate array space in chunks */
		if ((self->size % chunk_size) == 0) {
			unsigned int nalloc = self->size + chunk_size;
			PyObject **new_array;

			new_array = realloc(self->domain_objects, nalloc * sizeof(new_array[0]));
			if (new_array == NULL)
				abort();
			self->domain_objects = new_array;
		}

		slot = self->size++;
	}

	self->domain_objects[slot] = (PyObject *) member;
	Py_INCREF(member);

	self->count += 1;

	member->index = slot;
}

void
FastsetDomain_unregister(fastset_Domain *self, fastset_Member *member)
{
	if (member->index < 0)
		return;

	assert(self->count);
	assert(member->index < self->size);
	assert(self->domain_objects[member->index] == (PyObject *) member);

	self->domain_objects[member->index] = NULL;
	member->index = -1;

	self->count -= 1;
}

bool
FastsetDomain_IsMember(fastset_Domain *self, PyObject *object)
{
	const fastset_DomainSpecificType *dst;

	return ((dst = Fastset_DSTGetType(object)) != NULL && &dst->base == self->member_class);
}

PyObject *
FastsetDomain_GetMember(fastset_Domain *self, unsigned int index)
{
	/* printf("%s %s count=%u\n", __func__, self->name, self->count); */
	if (index >= self->size)
		return NULL;

	return self->domain_objects[index];
}

#if 0
PyObject *
Fastset_newSet(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
	fastset_Set *self;

	self = (fastset_Set *) type->tp_alloc(type, 0);
	if (self == NULL)
		return NULL;

	/* init members */
	self->domain = NULL;
	self->bitvec = NULL;

	return (PyObject *) self;
}

int
Fastset_initSet(fastset_Set *self, PyObject *args, PyObject *kwds)
{
	static char *kwlist[] = { NULL };
	fastset_Domain *domain;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "", kwlist))
		return -1;

	if (!(Fastset_DSTGetDomain((PyObject *) self)))
		return -1;

	Py_INCREF(domain);
	self->domain = domain;
	self->bitvec = fastset_bitvec_new(domain->size);
	return 0;
}

void
Fastset_deallocSet(fastset_Set *self)
{
	if (self->domain) {
		Py_DECREF(self->domain);
		self->domain = NULL;
	}

	if (self->bitvec) {
		fastset_bitvec_release(self->bitvec);
		self->bitvec = NULL;
	}
}

static fastset_Domain *
Fastset_argsToMember(fastset_Set *self, PyObject *args, PyObject *kwds)
{
	static char *kwlist[] = {
		"member",
		NULL
	};
	PyObject *member_object = NULL;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "O", kwlist, &member_object))
		return NULL;

	if (!FastsetElement_Check(member_object, self->domain)) {
		PyErr_SetString(PyExc_RuntimeError, "argument is not compatible with domain");
		return NULL;
	}

	return (fastset_Member *) member_object;
}

PyObject *
Fastset_add(fastset_Set *self, PyObject *args, PyObject *kwds)
{
	fastset_Member *member;
	PyObject *ret;

	if (!(member = Fastset_argsToMember(self, args, kwds)))
		return NULL;

	if (member->index < 0) {
		PyErr_SetString(PyExc_RuntimeError, "fastset member has invalid index");
		return NULL;
	}

	if (fastset_bitvec_set(self->bitvec, member->index))
		ret = Py_True;
	else
		ret = Py_False;

	Py_INCREF(ret);
	return ret;
}

static PyObject *
__fastset_clear(fastset_Set *self, PyObject *args, PyObject *kwds)
{
	fastset_Member *member;
	PyObject *ret;

	if (!(member = Fastset_argsToMember(self, args, kwds)))
		return NULL;

	if (member->index < 0)
		return Py_False;

	if (fastset_bitvec_clear(self->bitvec, member->index))
		return Py_True;
	return Py_False;
}

PyObject *
Fastset_clear(fastset_Set *self, PyObject *args, PyObject *kwds)
{
	fastset_Member *member;

	if (!(member = Fastset_argsToMember(self, args, kwds)))
		return NULL;

	if (member->index < 0 || !fastset_bitvec_clear(self->bitvec, member->index)) {
		/* raise KeyError exception */
		_PyErr_SetKeyError((PyObject *) member);
		return NULL;
	}

	Py_INCREF(Py_None);
	return Py_None;
}

PyObject *
Fastset_discard(fastset_Set *self, PyObject *args, PyObject *kwds)
{
	fastset_Member *member;
	PyObject *ret;

	if (!(member = Fastset_argsToMember(self, args, kwds)))
		return NULL;

	ret = Py_True;
	if (member->index < 0 || !fastset_bitvec_clear(self->bitvec, member->index))
		ret = Py_False;

	Py_INCREF(ret);
	return ret;
}
#endif

#if 0


static void		Domain_dealloc(curlies_Domain *self);
static PyObject *	Domain_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int		Domain_init(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_name(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_workspace(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_report(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_nodes(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_networks(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_tree(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_save(curlies_Domain *self, PyObject *args, PyObject *kwds);

static PyObject *	Domain_node_target(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_node_internal_ip(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_node_external_ip(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_node_internal_ip6(curlies_Domain *self, PyObject *args, PyObject *kwds);

static PyObject *	Domain_network_subnet(curlies_Domain *self, PyObject *args, PyObject *kwds);
static PyObject *	Domain_network_gateway(curlies_Domain *self, PyObject *args, PyObject *kwds);


/*
 * Define the python bindings of class "Domain"
 */
static PyMethodDef curlies_DomainMethods[] = {
      /* Top-level attributes */
      { "name", (PyCFunction) Domain_name, METH_VARARGS | METH_KEYWORDS,
	"Get the name of the fastset domain",
      },
      { "workspace", (PyCFunction) Domain_workspace, METH_VARARGS | METH_KEYWORDS,
	"Get the workspace of the test project",
      },
      { "report", (PyCFunction) Domain_report, METH_VARARGS | METH_KEYWORDS,
	"Get the report of the test project",
      },

      /* Top-level children */
      { "nodes", (PyCFunction) Domain_nodes, METH_VARARGS | METH_KEYWORDS,
	"Get the nodes of the test project",
      },
      { "networks", (PyCFunction) Domain_networks, METH_VARARGS | METH_KEYWORDS,
	"Get the networks of the test project",
      },

      /* Node attributes */
      {	"node_target", (PyCFunction) Domain_node_target, METH_VARARGS | METH_KEYWORDS,
	"Get the node's target description"
      },
      {	"node_internal_ip", (PyCFunction) Domain_node_internal_ip, METH_VARARGS | METH_KEYWORDS,
	"Get the node's internal IPv4 address"
      },
      {	"node_external_ip", (PyCFunction) Domain_node_external_ip, METH_VARARGS | METH_KEYWORDS,
	"Get the node's external IPv4 address"
      },
      {	"node_internal_ip6", (PyCFunction) Domain_node_internal_ip6, METH_VARARGS | METH_KEYWORDS,
	"Get the node's internal IPv6 address"
      },

      /* Network attributes */
      {	"network_subnet", (PyCFunction) Domain_network_subnet, METH_VARARGS | METH_KEYWORDS,
	"Get the networks's IPv4 subnet"
      },
      {	"network_gateway", (PyCFunction) Domain_network_gateway, METH_VARARGS | METH_KEYWORDS,
	"Get the networks's IPv4 gateway"
      },

      /* Access to low-level config functions */
      {	"tree", (PyCFunction) Domain_tree, METH_VARARGS | METH_KEYWORDS,
	"Get the config tree"
      },
      {	"save", (PyCFunction) Domain_save, METH_VARARGS | METH_KEYWORDS,
	"Save configuration to file"
      },

      /* Interface stuff */
      {	NULL }
};

PyTypeObject curlies_DomainType = {
	PyVarObject_HEAD_INIT(NULL, 0)

	.tp_name	= "curly.Domain",
	.tp_basicsize	= sizeof(curlies_Domain),
	.tp_flags	= Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
	.tp_doc		= "Domain object for twopence based tests",

	.tp_methods	= curlies_DomainMethods,
	.tp_init	= (initproc) Domain_init,
	.tp_new		= Domain_new,
	.tp_dealloc	= (destructor) Domain_dealloc,
};

/*
 * Constructor: allocate empty Domain object, and set its members.
 */
static PyObject *
Domain_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
	curlies_Domain *self;

	self = (curlies_Domain *) type->tp_alloc(type, 0);
	if (self == NULL)
		return NULL;

	/* init members */
	self->config = NULL;
	self->name = NULL;

	return (PyObject *)self;
}

/*
 * Initialize the status object
 */
static int
Domain_init(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	static char *kwlist[] = {
		"file",
		NULL
	};
	PyObject *arg_object = NULL;
	const char *filename = NULL;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &arg_object))
		return -1;

	if (arg_object == Py_None || arg_object == NULL) {
		/* create an empty Domain object */
		self->config_root = curly_node_new();
		self->config = self->config_root;
	} else {
		filename = PyUnicode_AsUTF8(arg_object);
		if (filename == NULL)
			return -1;

		self->config_root = curly_node_read(filename);
		if (self->config_root == NULL) {
			PyErr_Format(PyExc_SystemError, "Unable to read curlies config from file \"%s\"", filename);
			return -1;
		}

		/* While we're transitioning from the old-style curly stuff to Eric's
		 * XML stuff, there may or may not be a testenv group between the root and
		 * the stuff we're interested in.
		 */
		self->config = curly_node_get_child(self->config_root, "testenv", NULL);
		if (self->config != NULL) {
			self->name = (char *) curly_node_name(self->config);
		} else {
			self->config = self->config_root;
		}

		/* printf("Using curly config file %s\n", filename); */
	}

	return 0;
}

/*
 * Destructor: clean any state inside the Domain object
 */
static void
Domain_dealloc(curlies_Domain *self)
{
	// printf("Destroying %p\n", self);
	/* drop_string(&self->name); */
	if (self->config_root)
		curly_node_free(self->config_root);
	self->config_root = NULL;
	self->config = NULL;
	self->name = NULL;
}

int
Domain_Check(PyObject *self)
{
	return PyType_IsSubtype(Py_TYPE(self), &curlies_DomainType);
}

static bool
__check_void_args(PyObject *args, PyObject *kwds)
{
	static char *kwlist[] = {
		NULL
	};

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "", kwlist, NULL))
		return false;

	return true;
}

static bool
__get_single_string_arg(PyObject *args, PyObject *kwds, const char *arg_name, const char **string_arg_p)
{
	char *kwlist[] = {
		(char *) arg_name,
		NULL
	};

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "s", kwlist, string_arg_p))
		return false;

	return true;
}

static PyObject *
__to_string(const char *value)
{
	if (value != NULL)
		return PyUnicode_FromString(value);

	Py_INCREF(Py_None);
	return Py_None;
}

static PyObject *
__to_string_list(const char * const*values)
{
	PyObject *result;

	result = PyList_New(0);
	while (values && *values)
		PyList_Append(result, PyUnicode_FromString(*values++));

	return result;
}

static PyObject *
__toplevel_string_attr(curlies_Domain *self, PyObject *args, PyObject *kwds, const char *attrname)
{
	if (!__check_void_args(args, kwds))
		return NULL;

	return __to_string(curly_node_get_attr(self->config, attrname));
}

static PyObject *
__get_children(curly_node_t *config, const char *type)
{
	const char **values;
	PyObject *result;

	values = curly_node_get_children(config, type);
	if (values == NULL) {
		PyErr_SetString(PyExc_RuntimeError, "failed to get child names for configuration object");
		return NULL;
	}

	result = __to_string_list(values);
	free(values);

	return result;
}

static PyObject *
__get_attr_names(curly_node_t *config)
{
	const char **values;
	PyObject *result;

	values = curly_node_get_attr_names(config);
	if (values == NULL) {
		PyErr_SetString(PyExc_RuntimeError, "failed to get attribute names for configuration object");
		return NULL;
	}

	result = __to_string_list(values);
	free(values);

	return result;
}

static PyObject *
__toplevel_name_list(curlies_Domain *self, PyObject *args, PyObject *kwds, const char *type)
{
	if (!__check_void_args(args, kwds))
		return NULL;

	return __get_children(self->config, type);
}

static PyObject *
__firstlevel_string_attr(curlies_Domain *self, PyObject *args, PyObject *kwds, const char *type, const char *attrname, const char *compat_attrname)
{
	const char *name, *value;
	curly_node_t *child;

	if (!__get_single_string_arg(args, kwds, "name", &name))
		return NULL;

	child = curly_node_get_child(self->config, type, name);
	if (child == NULL) {
		PyErr_Format(PyExc_AttributeError, "Unknown %s \"%s\"", type, name);
		return NULL;
	}

	value = curly_node_get_attr(child, attrname);
	if (value == NULL && compat_attrname)
		value = curly_node_get_attr(child, compat_attrname);

	return __to_string(value);
}

static PyObject *
Domain_name(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __to_string(self->name);
}

static PyObject *
Domain_workspace(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __toplevel_string_attr(self, args, kwds, "workspace");
}

static PyObject *
Domain_report(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __toplevel_string_attr(self, args, kwds, "report");
}

static PyObject *
Domain_nodes(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __toplevel_name_list(self, args, kwds, "node");
}

static PyObject *
Domain_node_target(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __firstlevel_string_attr(self, args, kwds, "node", "target", NULL);
}

static PyObject *
Domain_node_internal_ip(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __firstlevel_string_attr(self, args, kwds, "node", "ipv4_address", "ipv4_addr");
}

static PyObject *
Domain_node_external_ip(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	Py_INCREF(Py_None);
	return Py_None;
}

static PyObject *
Domain_node_internal_ip6(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __firstlevel_string_attr(self, args, kwds, "node", "ipv6_address", "ipv6_addr");
}

static PyObject *
Domain_networks(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __toplevel_name_list(self, args, kwds, "network");
}

static PyObject *
Domain_network_subnet(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __firstlevel_string_attr(self, args, kwds, "network", "subnet", NULL);
}

static PyObject *
Domain_network_gateway(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	return __firstlevel_string_attr(self, args, kwds, "network", "gateway", NULL);
}


static PyObject *
Domain_tree(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	PyObject *tuple;
	PyObject *nodeObj;

	if (!__check_void_args(args, kwds))
		return NULL;

	tuple = PyTuple_New(1);

	PyTuple_SetItem(tuple, 0, (PyObject *) self);
	Py_INCREF(self);

	nodeObj = curlies_callType(&curlies_DomainNodeType, tuple, NULL);

	Py_DECREF(tuple);
	return nodeObj;
}

static PyObject *
Domain_save(curlies_Domain *self, PyObject *args, PyObject *kwds)
{
	const char *filename;

	if (!__get_single_string_arg(args, kwds, "filename", &filename))
		return NULL;

	if (curly_node_write(self->config_root, filename) < 0) {
		PyErr_Format(PyExc_OSError, "unable to write config file %s", filename);
		return NULL;
	}

	Py_INCREF(Py_None);
	return Py_None;
}

static PyObject *	DomainNode_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int		DomainNode_init(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static void		DomainNode_dealloc(curlies_DomainNode *self);
static PyObject *	DomainNode_iter(curlies_DomainNode *self);
static PyObject *	DomainNode_attribute_iter(curlies_DomainNode *self);
static PyObject *	DomainNode_getattro(curlies_DomainNode *self, PyObject *name);
static PyObject *	DomainNode_str(curlies_DomainNode *self);
static PyObject *	DomainNode_get_child(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_add_child(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_drop_child(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_get_children(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_get_attributes(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_get_value(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_set_value(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_unset_value(curlies_DomainNode *self, PyObject *args, PyObject *kwds);
static PyObject *	DomainNode_get_values(curlies_DomainNode *self, PyObject *args, PyObject *kwds);

static PyObject *	DomainAttr_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int		DomainAttr_init(curlies_Attr *self, PyObject *args, PyObject *kwds);
static void		DomainAttr_dealloc(curlies_Attr *self);
static PyObject *	DomainAttr_getattro(curlies_Attr *self, PyObject *name);

static PyObject *	DomainIter_new(PyTypeObject *type, PyObject *args, PyObject *kwds);
static int		DomainIter_init(curlies_Iterator *self, PyObject *args, PyObject *kwds);
static void		DomainIter_dealloc(curlies_Iterator *self);
static PyObject *	DomainIter_iter(curlies_Iterator *self);
static PyObject *	DomainNodeIter_iternext(curlies_Iterator *self);
static PyObject *	DomainAttrIter_iternext(curlies_Iterator *self);

/* should prolly be in a public header (if we had one) */
extern int		DomainIter_Check(PyObject *self);

/*
 * Define the python bindings of class "Domain"
 * Much of this cruft is no longer needed and can go away.
 */
static PyMethodDef curly_DomainNodeMethods[] = {
      /* Top-level attributes */
      { "get_child", (PyCFunction) DomainNode_get_child, METH_VARARGS | METH_KEYWORDS,
	"Find the child node with given type and name",
      },
      { "add_child", (PyCFunction) DomainNode_add_child, METH_VARARGS | METH_KEYWORDS,
	"Add a child node with given type and name",
      },
      { "drop_child", (PyCFunction) DomainNode_drop_child, METH_VARARGS | METH_KEYWORDS,
	"Drop the given child",
      },
      { "get_children", (PyCFunction) DomainNode_get_children, METH_VARARGS | METH_KEYWORDS,
	"Get all child nodes with given type",
      },
      { "get_attributes", (PyCFunction) DomainNode_get_attributes, METH_VARARGS | METH_KEYWORDS,
	"Get the names of all attributes of this node",
      },
      { "get_value", (PyCFunction) DomainNode_get_value, METH_VARARGS | METH_KEYWORDS,
	"Get the value of the named attribute as a single string"
      },
      { "set_value", (PyCFunction) DomainNode_set_value, METH_VARARGS | METH_KEYWORDS,
	"Set the value of the named attribute"
      },
      { "drop", (PyCFunction) DomainNode_unset_value, METH_VARARGS | METH_KEYWORDS,
	"Drop the named attribute"
      },
      { "get_values", (PyCFunction) DomainNode_get_values, METH_VARARGS | METH_KEYWORDS,
	"Get the value of the named attribute as list of strings"
      },

      {	NULL }
};

PyTypeObject curlies_DomainNodeType = {
	PyVarObject_HEAD_INIT(NULL, 0)

	.tp_name	= "curly.DomainNode",
	.tp_basicsize	= sizeof(curlies_DomainNode),
	.tp_flags	= Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
	.tp_doc		= "Domain object representing a curly config file",

	.tp_methods	= curly_DomainNodeMethods,
	.tp_init	= (initproc) DomainNode_init,
	.tp_new		= DomainNode_new,
	.tp_dealloc	= (destructor) DomainNode_dealloc,
	.tp_getattro	= (getattrofunc) DomainNode_getattro,
	.tp_iter	= (getiterfunc) DomainNode_iter,
	.tp_str		= (reprfunc) DomainNode_str,
};

PyTypeObject curlies_DomainAttrType = {
	PyVarObject_HEAD_INIT(NULL, 0)

	.tp_name	= "curly.Attr",
	.tp_basicsize	= sizeof(curlies_Attr),
	.tp_flags	= Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
	.tp_doc		= "Domain object representing a curly node attribute",

	.tp_methods	= NULL,
	.tp_init	= (initproc) DomainAttr_init,
	.tp_new		= DomainAttr_new,
	.tp_dealloc	= (destructor) DomainAttr_dealloc,
	.tp_getattro	= (getattrofunc) DomainAttr_getattro,
};


static PyMethodDef curly_DomainIterMethods[] = {
      {	NULL }
};

PyTypeObject curlies_NodeIteratorType = {
	PyVarObject_HEAD_INIT(NULL, 0)

	.tp_name	= "curly.NodeIterator",
	.tp_basicsize	= sizeof(curlies_Iterator),
	.tp_flags	= Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
	.tp_doc		= "Object representing an iterator over the children of a Curly config node",

	.tp_methods	= curly_DomainIterMethods,
	.tp_init	= (initproc) DomainIter_init,
	.tp_new		= DomainIter_new,
	.tp_dealloc	= (destructor) DomainIter_dealloc,
	.tp_iter	= (getiterfunc) DomainIter_iter,
	.tp_iternext	= (iternextfunc) DomainNodeIter_iternext,
};

PyTypeObject curlies_AttrIteratorType = {
	PyVarObject_HEAD_INIT(NULL, 0)

	.tp_name	= "curly.AttrIterator",
	.tp_basicsize	= sizeof(curlies_Iterator),
	.tp_flags	= Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
	.tp_doc		= "Object representing an iterator over the attributes of a Curly config node",

	.tp_methods	= curly_DomainIterMethods,
	.tp_init	= (initproc) DomainIter_init,
	.tp_new		= DomainIter_new,
	.tp_dealloc	= (destructor) DomainIter_dealloc,
	.tp_iter	= (getiterfunc) DomainIter_iter,
	.tp_iternext	= (iternextfunc) DomainAttrIter_iternext,
};

/*
 * Constructor: allocate empty Domain object, and set its members.
 */
static PyObject *
DomainNode_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
	curlies_DomainNode *self;

	self = (curlies_DomainNode *) type->tp_alloc(type, 0);
	if (self == NULL)
		return NULL;

	/* init members */
	self->config_object = NULL;
	self->node = NULL;

	return (PyObject *)self;
}

static inline void
__DomainNode_attach(curlies_DomainNode *self, PyObject *config_object, curly_node_t *node)
{
	assert(self->config_object == NULL);

	self->node = node;
	self->config_object = config_object;
	Py_INCREF(config_object);

	// printf("DomainNode %p references %p count=%ld\n", self, config_object, config_object->ob_refcnt);
}

static inline void
__DomainNode_detach(curlies_DomainNode *self)
{
	if (self->config_object) {
		// printf("DomainNode %p releases %p count=%ld\n", self, self->config_object, self->config_object->ob_refcnt);
		Py_DECREF(self->config_object);
	}
	self->config_object = NULL;
}

/*
 * Initialize the node object
 */
static int
DomainNode_init(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	static char *kwlist[] = {
		"config",
		NULL
	};
	PyObject *config_object = NULL;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &config_object))
		return -1;

	if (config_object && !Domain_Check(config_object)) {
		PyErr_SetString(PyExc_RuntimeError, "config argument must be an instance of curly.Domain");
		return -1;
	}

	if (config_object)
		__DomainNode_attach(self, config_object, ((curlies_Domain *) config_object)->config_root);

	return 0;
}

/*
 * Destructor: clean any state inside the Domain object
 */
static void
DomainNode_dealloc(curlies_DomainNode *self)
{
	__DomainNode_detach(self);
}

int
DomainNode_Check(PyObject *self)
{
	return PyType_IsSubtype(Py_TYPE(self), &curlies_DomainNodeType);
}

static curly_node_t *
DomainNode_GetPointer(PyObject *self)
{
	curly_node_t *node;

	if (!DomainNode_Check(self)) {
		PyErr_SetString(PyExc_RuntimeError, "node argument must be an instance of curly.DomainNode");
		return NULL;
	}

	node = ((curlies_DomainNode *) self)->node;
	if (node == NULL) {
		PyErr_SetString(PyExc_RuntimeError, "DomainNode object does not refer to anything");
		return NULL;
	}

	return node;
}

static PyObject *
DomainNode_getattro(curlies_DomainNode *self, PyObject *nameo)
{
	if (self->node) {
		const char *name = PyUnicode_AsUTF8(nameo);
		const char *const *values;

		if (name == NULL)
			return NULL;

		if (!strcmp(name, "attributes"))
			return DomainNode_attribute_iter(self);
		if (!strcmp(name, "type"))
			return __to_string(curly_node_type(self->node));
		if (!strcmp(name, "name"))
			return __to_string(curly_node_name(self->node));
		if (!strcmp(name, "origin")) {
			char buffer[4096];
			const char *path;

			if (!(path = curly_node_get_source_file(self->node))) {
				Py_INCREF(Py_None);
				return Py_None;
			}

			snprintf(buffer, sizeof(buffer), "%s, line %u", path,
					curly_node_get_source_line(self->node));
			return __to_string(buffer);
		}

		values = curly_node_get_attr_list(self->node, name);
		if (values) {
			if (values[0] == NULL || values[1] == NULL)
				return __to_string(values[0]);

			return __to_string_list(values);
		}
	}

	return PyObject_GenericGetAttr((PyObject *) self, nameo);
}

static bool
__check_node(curlies_DomainNode *self)
{
	if (self->node == NULL) {
		PyErr_SetString(PyExc_RuntimeError, "DomainNode object does not refer to any config data");
		return false;
	}

	return true;
}

static bool
__check_call(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	return __check_void_args(args, kwds) && __check_node(self);
}

static PyObject *
__wrap_node(curly_node_t *node, curlies_DomainNode *parent)
{
	PyObject *result;

	result = curlies_callType(&curlies_DomainNodeType, NULL, NULL);
	if (result == NULL)
		return NULL;

	if (!DomainNode_Check(result)) {
		PyErr_SetString(PyExc_RuntimeError, "cannot create DomainNode object");
		result = NULL;
	} else {
		__DomainNode_attach((curlies_DomainNode *) result, parent->config_object, node);
	}

	return (PyObject *) result;
}

/*
 * __str__()
 */
PyObject *
DomainNode_str(curlies_DomainNode *self)
{
	curly_node_t *node;
	char buf1[256], buf2[4096];
	const char *type, *name, *path;
	unsigned int line;

	if (!__check_node(self))
		return NULL;

	node = self->node;
	type = curly_node_type(node);
	name = curly_node_name(node);

	if (name)
		snprintf(buf1, sizeof(buf1), "%s \"%s\" { ... }", type, name);
	else
		snprintf(buf1, sizeof(buf1), "%s { ... }", type);

	path = curly_node_get_source_file(node);
	line = curly_node_get_source_line(node);

	if (path != NULL) {
		snprintf(buf2, sizeof(buf2), "%s (defined in %s, line %u)", buf1, path, line);
		return PyUnicode_FromString(buf2);
	}
	return PyUnicode_FromString(buf1);
}


/*
 * def __iter__():
 *	return DomainIter(self)
 */
static PyObject *
DomainNode_iter(curlies_DomainNode *self)
{
	PyObject *tuple, *result;

	if (!__check_node(self))
		return NULL;

	tuple = PyTuple_New(1);

	PyTuple_SetItem(tuple, 0, (PyObject *) self);
	Py_INCREF(self);

	result = curlies_callType(&curlies_NodeIteratorType, tuple, NULL);

	Py_DECREF(tuple);
	return result;
}

static PyObject *
DomainNode_attribute_iter(curlies_DomainNode *self)
{
	PyObject *tuple, *result;

	if (!__check_node(self))
		return NULL;

	tuple = PyTuple_New(1);

	PyTuple_SetItem(tuple, 0, (PyObject *) self);
	Py_INCREF(self);

	result = curlies_callType(&curlies_AttrIteratorType, tuple, NULL);

	Py_DECREF(tuple);
	return result;
}

static PyObject *
DomainNode_get_children(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	const char *type;

	if (!__get_single_string_arg(args, kwds, "type", &type))
		return NULL;

	if (!__check_node(self))
		return NULL;

	return __get_children(self->node, type);
}

static PyObject *
DomainNode_get_child(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	char *kwlist[] = {
		"type",
		"name",
		NULL
	};
	const char *type, *name = NULL;
	curly_node_t *child;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "s|s", kwlist, &type, &name))
		return NULL;

	if (!__check_node(self))
		return NULL;

	child = curly_node_get_child(self->node, type, name);
	if (child == NULL) {
		Py_INCREF(Py_None);
		return Py_None;
	}

	return __wrap_node(child, self);
}

static PyObject *
DomainNode_add_child(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	char *kwlist[] = {
		"type",
		"name",
		NULL
	};
	const char *type, *name = NULL;
	curly_node_t *child;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "s|s", kwlist, &type, &name))
		return NULL;

	if (!__check_node(self))
		return NULL;

	child = curly_node_add_child(self->node, type, name);
	if (child == NULL) {
		PyErr_Format(PyExc_SystemError, "Unable to create a %s node name \"%s\"", type, name);
		return NULL;
	}

	return __wrap_node(child, self);
}

static PyObject *
DomainNode_drop_child(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	char *kwlist[] = {
		"child",
		NULL
	};
	PyObject *childObject;
	unsigned int count;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "O", kwlist, &childObject))
		return NULL;

	if (!__check_node(self))
		return NULL;

	if (!DomainNode_Check(childObject)) {
		PyErr_SetString(PyExc_ValueError, "Argument is not a DomainNode instance");
		return NULL;
	}

	count = curly_node_drop_child(self->node, ((curlies_DomainNode *) childObject)->node);
	return PyLong_FromUnsignedLong(count);
}

static PyObject *
DomainNode_get_attributes(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	if (!__check_call(self, args, kwds))
		return NULL;

	return __get_attr_names(self->node);
}

static PyObject *
DomainNode_get_value(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	const char *name;

	if (!__get_single_string_arg(args, kwds, "name", &name))
		return NULL;

	if (!__check_node(self))
		return NULL;

	return __to_string(curly_node_get_attr(self->node, name));
}

static PyObject *
DomainNode_set_value(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	char *kwlist[] = {
		"name",
		"value",
		NULL
	};
	const char *name;
	PyObject *valueObj = NULL;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "sO", kwlist, &name, &valueObj))
		return NULL;

	if (!__check_node(self))
		return NULL;

	if (PyUnicode_Check(valueObj)) {
		curly_node_set_attr(self->node, name, PyUnicode_AsUTF8(valueObj));
	} else if (PySequence_Check(valueObj)) {
		Py_ssize_t i, len = PySequence_Size(valueObj);

		if (len < 0) {
bad_seq:
			PyErr_SetString(PyExc_ValueError, "bad sequence arg");
			return NULL;
		}
		curly_node_set_attr(self->node, name, NULL);

		for (i = 0; i < len; ++i) {
			PyObject *itemObj = PySequence_GetItem(valueObj, i);

			if (itemObj == NULL)
				goto bad_seq;

			if (!PyUnicode_Check(itemObj)) {
				PyErr_Format(PyExc_ValueError, "bad value at sequence position %d - expected a str", (int) i);
				Py_DECREF(itemObj);
				return NULL;
			}
			curly_node_add_attr_list(self->node, name, PyUnicode_AsUTF8(itemObj));
			Py_DECREF(itemObj);
		}
	} else if (valueObj == Py_None) {
		curly_node_set_attr(self->node, name, NULL);
	} else {
		PyErr_SetString(PyExc_ValueError, "cannot handle values of this type");
		return NULL;
	}

	Py_INCREF(Py_None);
	return Py_None;
}

static PyObject *
DomainNode_unset_value(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	const char *name;

	if (!__get_single_string_arg(args, kwds, "name", &name))
		return NULL;

	if (!__check_node(self))
		return NULL;

	curly_node_set_attr(self->node, name, NULL);
	Py_INCREF(Py_None);
	return Py_None;
}

static PyObject *
DomainNode_get_values(curlies_DomainNode *self, PyObject *args, PyObject *kwds)
{
	const char *name;

	if (!__get_single_string_arg(args, kwds, "name", &name))
		return NULL;

	if (!__check_node(self))
		return NULL;

	return __to_string_list(curly_node_get_attr_list(self->node, name));
}

/*
 * Wrapper object for curly attributes
 */
static PyObject *
DomainAttr_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
	curlies_Attr *self;

	self = (curlies_Attr *) type->tp_alloc(type, 0);
	if (self == NULL)
		return NULL;

	/* init members */
	self->node_object = NULL;
	self->attr = NULL;

	return (PyObject *)self;
}

static inline void
__DomainAttr_attach(curlies_Attr *self, PyObject *node_object, curly_attr_t *attr)
{
	assert(self->node_object == NULL);

	self->attr = attr;
	self->node_object = node_object;
	Py_INCREF(node_object);
}

static inline void
__DomainAttr_detach(curlies_Attr *self)
{
	self->attr = NULL;

	if (self->node_object)
		Py_DECREF(self->node_object);
	self->node_object = NULL;
}

/*
 * Initialize the iterator object
 */
static int
DomainAttr_init(curlies_Attr *self, PyObject *args, PyObject *kwds)
{
	if (!__check_void_args(args, kwds))
		return -1;

	return 0;
}

/*
 * Destructor: clean any state inside the Domain object
 */
static void
DomainAttr_dealloc(curlies_Attr *self)
{
	__DomainAttr_detach(self);
}

int
DomainAttr_Check(PyObject *self)
{
	return PyType_IsSubtype(Py_TYPE(self), &curlies_DomainAttrType);
}

PyObject *
DomainAttr_getattro(curlies_Attr *self, PyObject *nameo)
{
	if (self->attr) {
		const char *name = PyUnicode_AsUTF8(nameo);

		if (name == NULL)
			return NULL;

		if (!strcmp(name, "name"))
			return __to_string(curly_attr_get_name(self->attr));

		if (!strcmp(name, "value"))
			return __to_string(curly_attr_get_value(self->attr, 0));

		if (!strcmp(name, "values"))
			return __to_string_list(curly_attr_get_values(self->attr));
	}

	return PyObject_GenericGetAttr((PyObject *) self, nameo);
}

static PyObject *
__wrap_attr(curly_attr_t *attr, curlies_DomainNode *parent)
{
	PyObject *result;

	result = curlies_callType(&curlies_DomainAttrType, NULL, NULL);
	if (result == NULL)
		return NULL;

	if (!DomainAttr_Check(result)) {
		PyErr_SetString(PyExc_RuntimeError, "cannot create DomainAttr object");
		result = NULL;
	} else {
		__DomainAttr_attach((curlies_Attr *) result, parent->config_object, attr);
	}

	return (PyObject *) result;
}


/*
 * Iterator implementation
 */
static PyObject *
DomainIter_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
	curlies_Iterator *self;

	self = (curlies_Iterator *) type->tp_alloc(type, 0);
	if (self == NULL)
		return NULL;

	/* init members */
	self->node_object = NULL;
	self->iter = NULL;

	return (PyObject *)self;
}

static inline void
__DomainIter_attach(curlies_Iterator *self, PyObject *node_object, curly_iter_t *iter)
{
	assert(self->node_object == NULL);

	self->iter = iter;
	self->node_object = node_object;
	Py_INCREF(node_object);
}

static inline void
__DomainIter_detach(curlies_Iterator *self)
{
	if (self->iter)
		curly_iter_free(self->iter);
	self->iter = NULL;

	if (self->node_object)
		Py_DECREF(self->node_object);
	self->node_object = NULL;
}

/*
 * Initialize the iterator object
 */
static int
DomainIter_init(curlies_Iterator *self, PyObject *args, PyObject *kwds)
{
	static char *kwlist[] = {
		"config",
		NULL
	};
	PyObject *node_object = NULL;

	if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &node_object))
		return -1;

	if (node_object) {
		curly_node_t *node;
		curly_iter_t *iter;

		if (!(node = DomainNode_GetPointer(node_object)))
			return -1;

		if (!(iter = curly_node_iterate(node))) {
			PyErr_SetString(PyExc_RuntimeError, "unable to create iterator for DomainNode");
			return -1;
		}

		/* printf("Attach node %s iter %p\n", curly_node_name(node), iter); */
		__DomainIter_attach(self, node_object, iter);
	}

	return 0;
}

/*
 * Destructor: clean any state inside the Domain object
 */
static void
DomainIter_dealloc(curlies_Iterator *self)
{
	__DomainIter_detach(self);
}

int
DomainIter_Check(PyObject *self)
{
	return PyType_IsSubtype(Py_TYPE(self), &curlies_NodeIteratorType);
}

PyObject *
DomainIter_iter(curlies_Iterator *self)
{
	Py_INCREF(self);
	return (PyObject *) self;
}

PyObject *
DomainNodeIter_iternext(curlies_Iterator *self)
{
	curly_node_t *node = NULL;

	if (self->iter)
		node = curly_iter_next_node(self->iter);

	//printf("Next child for iter %p is %p\n", self->iter, node);
	if (node == NULL) {
		PyErr_SetString(PyExc_StopIteration, "stop");
		return NULL;
	}

	return __wrap_node(node, (curlies_DomainNode *) self->node_object);
}

PyObject *
DomainAttrIter_iternext(curlies_Iterator *self)
{
	curly_attr_t *attr = NULL;

	if (self->iter)
		attr = curly_iter_next_attr(self->iter);

	//printf("Next child for iter %p is %p\n", self->iter, attr);
	if (attr == NULL) {
		PyErr_SetString(PyExc_StopIteration, "stop");
		return NULL;
	}

	return __wrap_attr(attr, (curlies_DomainNode *) self->node_object);
}
#endif
