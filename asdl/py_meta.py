#!/usr/bin/env python
"""
py_meta.py

Parse an ASDL file, and generate Python classes using metaprogramming.
All objects descends from Obj, which allows them to be dynamically type-checked
and serialized.  Objects hold type descriptors, which are defined in asdl.py.

Usage:
  from osh import ast_ as ast

  n1 = ast.ArithVar()
  n2 = ast.ArrayLiteralPart()

API Notes:

The Python AST module doesn't make any distinction between simple and compound
sum types.  (Simple types have no constructors with fields.)

C++ has to make this distinction for reasons of representation.  It's more
efficient to hold an enum value than a pointer to a class with an enum value.
In Python I guess that's not quite true.

So in order to serialize the correct bytes for C++, our Python metaclass
implementation has to differ from what's generated by asdl_c.py.  More simply
put: an op is Add() and not Add, an instance of a class, not an integer value.
"""

import io
import sys

from asdl import asdl_ as asdl
from asdl import const
from asdl import format as fmt
from core import util


def _CheckType(value, expected_desc):
  """Is value of type expected_desc?

  Args:
    value: Obj or primitive type
    expected_desc: instance of asdl.Product, asl.Sum, asdl.StrType,
      asdl.IntType, ArrayType, MaybeType, etc.
  """
  if isinstance(expected_desc, asdl.Constructor):
    # This doesn't make sense because the descriptors are derived from the
    # declared types.  You can declare a field as arith_expr_e but not
    # ArithBinary.
    raise AssertionError("Invalid Constructor descriptor")

  if isinstance(expected_desc, asdl.MaybeType):
    if value is None:
      return True
    return _CheckType(value, expected_desc.desc)

  if isinstance(expected_desc, asdl.ArrayType):
    if not isinstance(value, list):
      return False
    # Now check all entries
    for item in value:
      if not _CheckType(item, expected_desc.desc):
        return False
    return True

  if isinstance(expected_desc, asdl.StrType):
    return isinstance(value, str)

  if isinstance(expected_desc, asdl.IntType):
    return isinstance(value, int)

  if isinstance(expected_desc, asdl.BoolType):
    return isinstance(value, bool)

  if isinstance(expected_desc, asdl.UserType):
    return isinstance(value, expected_desc.typ)

  try:
    actual_desc = value.__class__.ASDL_TYPE
  except AttributeError:
    return False  # it's not of the right type

  if isinstance(expected_desc, asdl.Product):
    return actual_desc is expected_desc

  if isinstance(expected_desc, asdl.Sum):
    if asdl.is_simple(expected_desc):
      return actual_desc is expected_desc
    else:
      for cons in expected_desc.types:
        #print("CHECKING desc %s against %s" % (desc, cons))

        # It has to be one of the alternatives
        from core.util import log
        #log('Checking %s against %s', actual_desc, cons)
        if actual_desc is cons:
          return True
      return False

  raise AssertionError(
      'Invalid descriptor %r: %r' % (expected_desc.__class__, expected_desc))


class Obj(object):
  # NOTE: We're using CAPS for these static fields, since they are constant at
  # runtime after metaprogramming.
  ASDL_TYPE = None  # Used for type checking


class SimpleObj(Obj):
  """An enum value.

  Other simple objects: int, str, maybe later a float.
  """
  def __init__(self, enum_id, name):
    self.enum_id = enum_id
    self.name = name

  def __repr__(self):
    return '<%s %s %s>' % (self.__class__.__name__, self.name, self.enum_id)


class CompoundObj(Obj):
  # TODO: Remove this?

  # Always set for constructor types, which are subclasses of sum types.  Never
  # set for product types.
  tag = None


class DebugCompoundObj(CompoundObj):
  """A CompoundObj that does dynamic type checks.

  Used by MakeTypes().
  """
  # Always set for constructor types, which are subclasses of sum types.  Never
  # set for product types.
  tag = None

  def __init__(self, *args, **kwargs):
    # The user must specify ALL required fields or NONE.
    self._assigned = {f: False for f in self.ASDL_TYPE.GetFieldNames()}
    self._SetDefaults()
    if args or kwargs:
      self._Init(args, kwargs)

  def _SetDefaults(self):
    for name, desc in self.ASDL_TYPE.GetFields():

      if isinstance(desc, asdl.MaybeType):
        child = desc.desc
        if isinstance(child, asdl.IntType):
          value = const.NO_INTEGER
        elif isinstance(child, asdl.StrType):
          value = ''
        else:
          value = None
        self.__setattr__(name, value)  # Maybe values can be None

      elif isinstance(desc, asdl.ArrayType):
        self.__setattr__(name, [])

  def _Init(self, args, kwargs):
    field_names = list(self.ASDL_TYPE.GetFieldNames())
    for i, val in enumerate(args):
      name = field_names[i]
      self.__setattr__(name, val)

    for name, val in kwargs.items():
      if self._assigned[name]:
        raise TypeError('Duplicate assignment of field %r' % name)
      self.__setattr__(name, val)

    # Disable type checking here
    #return
    for name in field_names:
      if not self._assigned[name]:
        # If anything was set, then required fields raise an error.
        raise ValueError("Field %r is required and wasn't initialized" % name)

  def CheckUnassigned(self):
    """See if there are unassigned fields, for later encoding.

    This is currently only used in unit tests.
    """
    unassigned = []
    for name in self.ASDL_TYPE.GetFieldNames():
      if not self._assigned[name]:
        desc = self.ASDL_TYPE.LookupFieldType(name)
        if not isinstance(desc, asdl.MaybeType):
          unassigned.append(name)
    if unassigned:
      raise ValueError("Fields %r were't be assigned" % unassigned)

  if 1:  # Disable type checking here
    def __setattr__(self, name, value):
      if name == '_assigned':
        self.__dict__[name] = value
        return
      try:
        desc = self.ASDL_TYPE.LookupFieldType(name)
      except KeyError:
        raise AttributeError('Object of type %r has no attribute %r' %
                             (self.__class__.__name__, name))

      if not _CheckType(value, desc):
        raise AssertionError("Field %r should be of type %s, got %r (%s)" %
                             (name, desc, value, value.__class__))

      self._assigned[name] = True  # check this later when encoding
      self.__dict__[name] = value

  def __repr__(self):
    ast_f = fmt.TextOutput(util.Buffer())  # No color by default.
    #ast_f = fmt.AnsiOutput(io.StringIO())
    tree = fmt.MakeTree(self)
    fmt.PrintTree(tree, ast_f)
    s, _ = ast_f.GetRaw()
    return s


def MakeTypes(module, root, type_lookup):
  """
  Args:
    module: asdl.Module
    root: an object/package to add types to
  """
  for defn in module.dfns:
    typ = defn.value

    #print('TYPE', defn.name, typ)
    if isinstance(typ, asdl.Sum):
      sum_type = typ
      if asdl.is_simple(sum_type):
        # An object without fields, which can be stored inline.
        
        # Create a class called foo_e.  Unlike the CompoundObj case, it doesn't
        # have subtypes.  Instead if has attributes foo_e.Bar, which Bar is an
        # instance of foo_e.
        #
        # Problem: This means you have a dichotomy between:
        # cflow_e.Break vs. cflow_e.Break()
        # If you add a non-simple type like cflow_e.Return(5), the usage will
        # change.  I haven't run into this problem in practice yet.

        class_name = defn.name + '_e'
        class_attr = {'ASDL_TYPE': sum_type}  # asdl.Sum
        cls = type(class_name, (SimpleObj, ), class_attr)
        setattr(root, class_name, cls)

        # TODO: cons needs ASDL_TYPE?
        for i, cons in enumerate(sum_type.types):
          enum_id = i + 1
          name = cons.name
          val = cls(enum_id, cons.name)  # Instantiate SimpleObj subtype

          # Set a static attribute like op_id.Plus, op_id.Minus.
          setattr(cls, name, val)
      else:
        tag_num = {}

        # e.g. for arith_expr
        # Should this be arith_expr_t?  It is in C++.
        base_class = type(defn.name, (DebugCompoundObj, ), {})
        setattr(root, defn.name, base_class)

        # Make a type and a enum tag for each alternative.
        for i, cons in enumerate(sum_type.types):
          tag = i + 1  # zero reserved?
          tag_num[cons.name] = tag  # for enum

          # Add 'int* spids' to every constructor.
          class_attr = {
              'ASDL_TYPE': cons,  # asdl.Constructor
              'tag': tag,  # Does this API change?
          }

          cls = type(cons.name, (base_class, ), class_attr)
          setattr(root, cons.name, cls)

        # e.g. arith_expr_e.Const == 1
        enum_name = defn.name + '_e'
        tag_enum = type(enum_name, (), tag_num)
        setattr(root, enum_name, tag_enum)

    elif isinstance(typ, asdl.Product):
      class_attr = {'ASDL_TYPE': typ}
      cls = type(defn.name, (DebugCompoundObj, ), class_attr)
      setattr(root, defn.name, cls)

    else:
      raise AssertionError(typ)


def AssignTypes(src_module, dest_module):
  """For generated code."""
  for name in dir(src_module):
    if not name.startswith('__'):
      v = getattr(src_module, name)
      setattr(dest_module, name, v)

