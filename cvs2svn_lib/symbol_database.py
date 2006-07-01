# (Be in -*- python -*- mode.)
#
# ====================================================================
# Copyright (c) 2000-2006 CollabNet.  All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.  The terms
# are also available at http://subversion.tigris.org/license-1.html.
# If newer versions of this license are posted there, you may use a
# newer version instead, at your option.
#
# This software consists of voluntary contributions made by many
# individuals.  For exact contribution history, see the revision
# history and logs, available at http://cvs2svn.tigris.org/.
# ====================================================================

"""This module contains the SymbolDatabase class."""


from cvs2svn_lib.boolean import *
from cvs2svn_lib import config
from cvs2svn_lib.artifact_manager import artifact_manager
from cvs2svn_lib.database import DB_OPEN_READ
from cvs2svn_lib.database import DB_OPEN_NEW
from cvs2svn_lib.database import PDatabase


class Symbol:
  def __init__(self, id, name):
    self.id = id
    self.name = name


class BranchSymbol(Symbol):
  def __str__(self):
    """For convenience only.  The format is subject to change at any time."""

    return 'Branch %r <%x>' % (self.name, self.id,)


class TagSymbol(Symbol):
  def __str__(self):
    """For convenience only.  The format is subject to change at any time."""

    return 'Tag %r <%x>' % (self.name, self.id,)


class SymbolDatabase:
  """Read-only access to symbol database.

  The primary lookup mechanism is name -> symbol, where symbol is a
  Symbol instance.  The whole database is read into memory upon
  construction."""

  def __init__(self):
    # A map { id : Symbol }
    self._symbols = {}

    # A map { name : Symbol }
    self._symbols_by_name = {}

    db = PDatabase(
        artifact_manager.get_temp_file(config.SYMBOL_DB), DB_OPEN_READ)
    for name in db.keys():
      symbol = db[name]
      self._symbols[symbol.id] = symbol
      self._symbols_by_name[name] = symbol

  def get_symbol(self, name):
    """Return the symbol instance with name NAME.

    Return None if there is no such instance (for example, if NAME is
    being excluded from the conversion)."""

    return self._symbols_by_name.get(name)

  def get_id(self, name):
    """Return the id of the symbol with the specified NAME.

    Raise a KeyError if there is no such symbol."""

    return self._symbols_by_name[name].id

  def get_name(self, id):
    """Return the name of the symbol with the specified ID.

    Raise a KeyError if there is no such symbol."""

    return self._symbols[id].name

  def __contains__(self, name):
    """Return True iff NAME is a symbol being included in the conversion."""

    return name in self._symbols_by_name

  def collate_symbols(self, names):
    """Given an iterable of symbol, divide them into branches and tags.

    Return a tuple of two lists (branches, tags), containing the
    symbols that should be converted as branches and tags
    respectively.  Symbols that we do not know about are not included
    in either output list."""

    branches = []
    tags = []
    for name in names:
      symbol = self.get_symbol(name)
      if isinstance(symbol, BranchSymbol):
        branches.append(name)
      elif isinstance(symbol, TagSymbol):
        tags.append(name)

    return (branches, tags,)


def create_symbol_database(symbols):
  """Create and fill a symbol database.

  Record each symbol that is listed in SYMBOLS, which is an iterable
  containing Symbol objects."""

  db = PDatabase(artifact_manager.get_temp_file(config.SYMBOL_DB),
                 DB_OPEN_NEW)
  for symbol in symbols:
    db[symbol.name] = symbol


