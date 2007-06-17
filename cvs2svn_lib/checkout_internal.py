# (Be in -*- python -*- mode.)
#
# ====================================================================
# Copyright (c) 2007 CollabNet.  All rights reserved.
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

"""This module contains classes that implement the --use-internal-co option.

The idea is to patch up the revisions' contents incrementally, thus
avoiding the huge number of process spawns and the O(n^2) overhead of
using 'co' and 'cvs'.

InternalRevisionRecorder saves the RCS deltas and RCS revision trees
to databases.  Notably, deltas from the trunk need to be reversed, as
CVS stores them so they apply from HEAD backwards.

InternalRevisionExcluder copies the revision trees to a new database,
omitting excluded branches.

InternalRevisionReader produces the revisions' contents on demand.  To
generate the text for a typical revision, we need the revision's delta
text plus the fulltext of the previous revision.  Therefore, we
maintain a checkout database containing a copy of the fulltext of any
revision for which subsequent revisions still need to be retrieved.
It is crucial to remove text from this database as soon as it is no
longer needed, to prevent it from growing enormous.

There are two reasons that the text from a revision can be needed: (1)
because the revision itself still needs to be output to a dumpfile;
(2) because another revision needs it as the base of its delta.  We
maintain a reference count for each revision, which includes *both*
possibilities.  The first time a revision's text is needed, it is
generated by applying the revision's deltatext to the previous
revision's fulltext, and the resulting fulltext is stored in the
checkout database.  Each time a revision's fulltext is retrieved, its
reference count is decremented.  When the reference count goes to
zero, then the fulltext is deleted from the checkout database.

The administrative data for managing this consists of one TextRecord
entry for each revision.  Each TextRecord has an id, which is the same
number as used for the corresponding CVSRevision instance.  It also
maintains a reference count of the times it is expected to be
retrieved.  TextRecords come in several varieties:

FullTextRecord -- Used for revisions whose fulltext is generated
    during CollectRevsPass (i.e., typically revision 1.1 of each
    file).

DeltaTextRecord -- Used for revisions that are defined via a delta
    relative to some other TextRecord.  These records record the id of
    the TextRecord that holds the base text against which the delta is
    defined.  When the text for a DeltaTextRecord is retrieved, the
    DeltaTextRecord instance is deleted and a CheckedOutTextRecord
    instance is created to take its place.

CheckedOutTextRecord -- Used during OutputPass for a revision that
    started out as a DeltaTextRecord, but has already been retrieved
    (and therefore its fulltext is stored in the checkout database).

While a file is being processed during CollectRevsPass, the fulltext
and deltas are stored to the delta database, and TextRecord instances
are created to describe keep track of things.  The reference counts
are all initialized to zero.

After CollectRevsPass has done any preliminary tree mangling, its
_FileDataCollector.parse_completed(), method calls
RevisionRecorder.finish_file(), passing it the CVSFileItems instance
that describes the revisions in the file.  At this point the reference
counts for the file's TextRecords are updated: each record referred to
by a delta has its refcount incremented, and each record that
corresponds to a non-delete CVSRevision is incremented.  After that,
any records with refcount==0 are removed.  When one record is removed,
that can cause another record's reference count to go to zero and be
removed too, recursively.  When a TextRecord is deleted at this stage,
its deltatext is also deleted from the delta database.

In FilterSymbolsPass, the exact same procedure (described in the
previous paragraph) is repeated, but this time using the CVSFileItems
after it has been updated for excluded symbols, symbol
preferred-parent grafting, etc."""


from __future__ import generators

import cStringIO
import re
import types

from cvs2svn_lib.set_support import *
from cvs2svn_lib import config
from cvs2svn_lib.common import DB_OPEN_NEW
from cvs2svn_lib.common import DB_OPEN_READ
from cvs2svn_lib.common import warning_prefix
from cvs2svn_lib.common import InternalError
from cvs2svn_lib.log import Log
from cvs2svn_lib.artifact_manager import artifact_manager
from cvs2svn_lib.symbol import Symbol
from cvs2svn_lib.cvs_item import CVSRevisionModification
from cvs2svn_lib.cvs_item import CVSRevisionDelete
from cvs2svn_lib.collect_data import is_trunk_revision
from cvs2svn_lib.database import Database
from cvs2svn_lib.database import IndexedDatabase
from cvs2svn_lib.rcs_stream import RCSStream
from cvs2svn_lib.revision_recorder import RevisionRecorder
from cvs2svn_lib.revision_excluder import RevisionExcluder
from cvs2svn_lib.revision_reader import RevisionReader
from cvs2svn_lib.serializer import StringSerializer
from cvs2svn_lib.serializer import CompressingSerializer
from cvs2svn_lib.serializer import PrimedPickleSerializer


class TextRecord(object):
  """Bookkeeping data for the text of a single CVSRevision."""

  def __init__(self, id):
    # The cvs_rev_id of the revision whose text this is.
    self.id = id

    # The number of times that the text of this revision will be
    # retrieved.
    self.refcount = 0

  def increment_dependency_refcounts(self, text_record_db):
    """Increment the refcounts of any records that this one depends on."""

    pass

  def decrement_refcount(self, text_record_db):
    """Decrement the number of times our text still has to be checked out.

    If the reference count goes to zero, call discard()."""

    self.refcount -= 1
    if self.refcount == 0:
      self.discard(text_record_db)

  def checkout(self, text_record_db):
    """Workhorse of the checkout process.

    Return the text for this revision, decrement our reference count,
    and update the databases depending on whether there will be future
    checkouts."""

    raise NotImplementedError()

  def discard(self, text_record_db):
    """This instance will never again be checked out; discard it."""

    if self.refcount != 0:
      raise InternalError(
          '%s.discard() called with refcount = %d'
          % (self.__class__, self.refcount,)
          )

    del text_record_db[self.id]


class FullTextRecord(TextRecord):
  def checkout(self, text_record_db):
    text = text_record_db.delta_db[self.id]
    self.decrement_refcount(text_record_db)
    return text

  def discard(self, text_record_db):
    TextRecord.discard(self, text_record_db)
    del text_record_db.delta_db[self.id]

  def __str__(self):
    return 'FullTextRecord(%x, %d)' % (self.id, self.refcount,)


class DeltaTextRecord(TextRecord):
  def __init__(self, id, pred_id):
    TextRecord.__init__(self, id)

    # The cvs_rev_id of the revision relative to which this delta is
    # defined.
    self.pred_id = pred_id

  def increment_dependency_refcounts(self, text_record_db):
    text_record_db[self.pred_id].refcount += 1

  def checkout(self, text_record_db):
    base_text = text_record_db[self.pred_id].checkout(text_record_db)
    co = RCSStream(base_text)
    delta_text = text_record_db.delta_db[self.id]
    co.apply_diff(delta_text)
    text = co.get_text()
    del co
    self.refcount -= 1
    if self.refcount == 0:
      # This text will never be needed again; just delete ourselves
      # without ever having stored the fulltext to the checkout
      # database:
      del text_record_db[self.id]
    else:
      # Store a new CheckedOutTextRecord in place of ourselves:
      text_record_db.checkout_db['%x' % self.id] = text
      new_text_record = CheckedOutTextRecord(self.id)
      new_text_record.refcount = self.refcount
      text_record_db.replace(new_text_record)
    return text

  def discard(self, text_record_db):
    TextRecord.discard(self, text_record_db)
    del text_record_db.delta_db[self.id]
    text_record_db[self.pred_id].decrement_refcount(text_record_db)

  def __str__(self):
    return 'DeltaTextRecord(%x -> %x, %d)' \
           % (self.pred_id, self.id, self.refcount,)


class CheckedOutTextRecord(TextRecord):
  def __init__(self, id):
    TextRecord.__init__(self, id)

  def checkout(self, text_record_db):
    text = text_record_db.checkout_db['%x' % self.id]
    self.decrement_refcount(text_record_db)
    return text

  def discard(self, text_record_db):
    TextRecord.discard(self, text_record_db)
    del text_record_db.checkout_db['%x' % self.id]

  def __str__(self):
    return 'CheckedOutTextRecord(%x, %d)' % (self.id, self.refcount,)


class NullDatabase(object):
  """A do-nothing database that can be used with TextRecordDatabase.

  Use this when you don't actually want to allow anything to be
  deleted."""

  def __delitem__(self, id):
    pass


class TextRecordDatabase:
  """Holds the TextRecord instances that are currently live.

  During CollectRevsPass and FilterSymbolsPass, files are processed
  one by one and a new TextRecordDatabase instance is used for each
  file.  During OutputPass, a single TextRecordDatabase instance is
  used for the duration of OutputPass; individual records are added
  and removed when they are active."""

  def __init__(self, delta_db, checkout_db):
    # A map { cvs_rev_id -> TextRecord }.
    self.text_records = {}

    # A database-like object using cvs_rev_ids as keys and containing
    # fulltext/deltatext strings as values.  Its __getitem__() method
    # is used to retrieve deltas when they are needed, and its
    # __delitem__() method is used to delete deltas when they can be
    # freed.  The modifiability of the delta database varies from pass
    # to pass, so the object stored here varies as well:
    #
    # CollectRevsPass: a fully-functional IndexedDatabase.  This
    #     allows deltas that will not be needed to be deleted.
    #
    # FilterSymbolsPass: a NullDatabase.  The delta database cannot be
    #     modified during this pass, and we have no need to retrieve
    #     deltas, so we just use a dummy object here.
    #
    # OutputPass: a disabled IndexedDatabase.  During this pass we
    # need to retrieve deltas, but we are not allowed to modify the
    # delta database.  So we use an IndexedDatabase whose __del__()
    # method has been disabled to do nothing.
    self.delta_db = delta_db

    # A database-like object using cvs_rev_ids as keys and containing
    # fulltext strings as values.  This database is only set during
    # OutputPass.
    self.checkout_db = checkout_db

  def __getstate__(self):
    return self.text_records.values()

  def __setstate__(self, state):
    self.text_records = {}
    for text_record in state:
      self.add(text_record)
    self.delta_db = NullDatabase()
    self.checkout_db = NullDatabase()

  def add(self, text_record):
    """Add TEXT_RECORD to our database.

    There must not already be a record with the same id."""

    assert not self.text_records.has_key(text_record.id)

    self.text_records[text_record.id] = text_record

  def __getitem__(self, id):
    return self.text_records[id]

  def __delitem__(self, id):
    """Free the record with the specified ID."""

    del self.text_records[id]

  def replace(self, text_record):
    """Store TEXT_RECORD in place of the existing record with the same id.

    Do not do anything with the old record."""

    assert self.text_records.has_key(text_record.id)
    self.text_records[text_record.id] = text_record

  def itervalues(self):
    return self.text_records.itervalues()

  def recompute_refcounts(self, cvs_file_items):
    """Recompute the refcounts of the contained TextRecords.

    Use CVS_FILE_ITEMS to determine which records will be needed by
    cvs2svn."""

    # First clear all of the refcounts:
    for text_record in self.itervalues():
      text_record.refcount = 0

    # Now increment the reference count of records that are needed as
    # the source of another record's deltas:
    for text_record in self.itervalues():
      text_record.increment_dependency_refcounts(self.text_records)

    # Now increment the reference count of records that will be needed
    # by cvs2svn:
    for lod_items in cvs_file_items.iter_lods():
      for cvs_rev in lod_items.cvs_revisions:
        if isinstance(cvs_rev, CVSRevisionModification):
          self[cvs_rev.id].refcount += 1

  def free_unused(self):
    """Free any TextRecords whose reference counts are zero."""

    # The deletion of some of these text records might cause others to
    # be unused, in which case they will be deleted automatically.
    # But since the initially-unused records are not referred to by
    # any others, we don't have to be afraid that they will be deleted
    # before we get to them.  But it *is* crucial that we create the
    # whole unused list before starting the loop.

    unused = [
        text_record
        for text_record in self.itervalues()
        if text_record.refcount == 0
        ]

    for text_record in unused:
      text_record.discard(self)

  def log_leftovers(self):
    """If any TextRecords still exist, log them."""

    if self.text_records:
      Log().warn(
          "%s: internal problem: leftover revisions in the checkout cache:"
          % warning_prefix)
      for text_record in self.itervalues():
        Log().warn('    %s' % (text_record,))

  def __repr__(self):
    """Debugging output of the current contents of the TextRecordDatabase."""

    retval = ['TextRecordDatabase:']
    for text_record in self.itervalues():
      retval.append('    %s' % (text_record,))
    return '\n'.join(retval)


class InternalRevisionRecorder(RevisionRecorder):
  """A RevisionRecorder that reconstructs the fulltext internally."""

  def __init__(self, compress):
    self._compress = compress

  def register_artifacts(self, which_pass):
    which_pass._register_temp_file(config.RCS_DELTAS_INDEX_TABLE)
    which_pass._register_temp_file(config.RCS_DELTAS_STORE)
    which_pass._register_temp_file(config.RCS_TREES_INDEX_TABLE)
    which_pass._register_temp_file(config.RCS_TREES_STORE)

  def start(self):
    ser = StringSerializer()
    if self._compress:
      ser = CompressingSerializer(ser)
    self._rcs_deltas = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_DELTAS_STORE),
        artifact_manager.get_temp_file(config.RCS_DELTAS_INDEX_TABLE),
        DB_OPEN_NEW, ser)
    primer = (FullTextRecord, DeltaTextRecord)
    self._rcs_trees = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_TREES_STORE),
        artifact_manager.get_temp_file(config.RCS_TREES_INDEX_TABLE),
        DB_OPEN_NEW, PrimedPickleSerializer(primer))

  def start_file(self, cvs_file):
    self._cvs_file = cvs_file

    # A map from cvs_rev_id to TextRecord instance:
    self.text_record_db = TextRecordDatabase(self._rcs_deltas, NullDatabase())

  def record_text(self, revisions_data, revision, log, text):
    revision_data = revisions_data[revision]
    if is_trunk_revision(revision):
      # On trunk, revisions are encountered in reverse order (1.<N>
      # ... 1.1) and deltas are inverted.  The first text that we see
      # is the fulltext for the HEAD revision.  After that, the text
      # corresponding to revision 1.N is the delta (1.<N+1> ->
      # 1.<N>)).  We have to invert the deltas here so that we can
      # read the revisions out in dependency order; that is, for
      # revision 1.1 we want the fulltext, and for revision 1.<N> we
      # want the delta (1.<N-1> -> 1.<N>).  This means that we can't
      # compute the delta for a revision until we see its logical
      # parent.  When we finally see revision 1.1 (which is recognized
      # because it doesn't have a parent), we can record the diff (1.1
      # -> 1.2) for revision 1.2, and also the fulltext for 1.1.

      if revision_data.child is None:
        # This is HEAD, as fulltext.  Initialize the RCSStream so
        # that we can compute deltas backwards in time.
        self._stream = RCSStream(text)
      else:
        # Any other trunk revision is a backward delta.  Apply the
        # delta to the RCSStream to mutate it to the contents of this
        # revision, and also to get the reverse delta, which we store
        # as the forward delta of our child revision.
        text = self._stream.invert_diff(text)
        text_record = DeltaTextRecord(
            revisions_data[revision_data.child].cvs_rev_id,
            revision_data.cvs_rev_id
            )
        self._writeout(text_record, text)

      if revision_data.parent is None:
        # This is revision 1.1.  Write its fulltext:
        text_record = FullTextRecord(revision_data.cvs_rev_id)
        self._writeout(text_record, self._stream.get_text())

        # There will be no more trunk revisions delivered, so free the
        # RCSStream.
        del self._stream

    else:
      # On branches, revisions are encountered in logical order
      # (<BRANCH>.1 ... <BRANCH>.<N>) and the text corresponding to
      # revision <BRANCH>.<N> is the forward delta (<BRANCH>.<N-1> ->
      # <BRANCH>.<N>).  That's what we need, so just store it.

      # FIXME: It would be nice to avoid writing out branch deltas
      # when --trunk-only.  (They will be deleted when finish_file()
      # is called, but if the delta db is in an IndexedDatabase the
      # deletions won't actually recover any disk space.)
      text_record = DeltaTextRecord(
          revision_data.cvs_rev_id,
          revisions_data[revision_data.parent].cvs_rev_id
          )
      self._writeout(text_record, text)

    return None

  def _writeout(self, text_record, text):
    self.text_record_db.add(text_record)
    self._rcs_deltas[text_record.id] = text

  def finish_file(self, cvs_file_items):
    """Finish processing of the current file.

    Compute the initial text record refcounts, discard any records
    that are unneeded, and store the text records for the file to the
    _rcs_trees database."""

    self.text_record_db.recompute_refcounts(cvs_file_items)
    self.text_record_db.free_unused()
    self._rcs_trees[self._cvs_file.id] = self.text_record_db
    del self._cvs_file
    del self.text_record_db

  def finish(self):
    self._rcs_deltas.close()
    self._rcs_trees.close()


class InternalRevisionExcluder(RevisionExcluder):
  """The RevisionExcluder used by InternalRevisionReader."""

  def register_artifacts(self, which_pass):
    which_pass._register_temp_file_needed(config.RCS_TREES_STORE)
    which_pass._register_temp_file_needed(config.RCS_TREES_INDEX_TABLE)
    which_pass._register_temp_file(config.RCS_TREES_FILTERED_STORE)
    which_pass._register_temp_file(config.RCS_TREES_FILTERED_INDEX_TABLE)

  def start(self):
    self._tree_db = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_TREES_STORE),
        artifact_manager.get_temp_file(config.RCS_TREES_INDEX_TABLE),
        DB_OPEN_READ)
    primer = (FullTextRecord, DeltaTextRecord)
    self._new_tree_db = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_TREES_FILTERED_STORE),
        artifact_manager.get_temp_file(config.RCS_TREES_FILTERED_INDEX_TABLE),
        DB_OPEN_NEW, PrimedPickleSerializer(primer))

  def finish_file(self, cvs_file_items):
    text_record_db = self._tree_db[cvs_file_items.cvs_file.id]
    text_record_db.recompute_refcounts(cvs_file_items)
    text_record_db.free_unused()
    self._new_tree_db[cvs_file_items.cvs_file.id] = text_record_db

  def skip_file(self, cvs_file):
    text_record_db = self._tree_db[cvs_file.id]
    self._new_tree_db[cvs_file.id] = text_record_db

  def finish(self):
    self._tree_db.close()
    self._new_tree_db.close()


class InternalRevisionReader(RevisionReader):
  """A RevisionReader that reads the contents from an own delta store."""

  _kw_re = re.compile(
      r'\$(' +
      r'Author|Date|Header|Id|Name|Locker|Log|RCSfile|Revision|Source|State' +
      r'):[^$\n]*\$')

  def __init__(self, compress):
    self._compress = compress

  def register_artifacts(self, which_pass):
    which_pass._register_temp_file(config.CVS_CHECKOUT_DB)
    which_pass._register_temp_file_needed(config.RCS_DELTAS_STORE)
    which_pass._register_temp_file_needed(config.RCS_DELTAS_INDEX_TABLE)
    which_pass._register_temp_file_needed(config.RCS_TREES_FILTERED_STORE)
    which_pass._register_temp_file_needed(
        config.RCS_TREES_FILTERED_INDEX_TABLE)

  def get_revision_recorder(self):
    return InternalRevisionRecorder(self._compress)

  def get_revision_excluder(self):
    return InternalRevisionExcluder()

  def start(self):
    self._delta_db = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_DELTAS_STORE),
        artifact_manager.get_temp_file(config.RCS_DELTAS_INDEX_TABLE),
        DB_OPEN_READ)
    self._delta_db.__delitem__ = lambda id: None
    self._tree_db = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_TREES_FILTERED_STORE),
        artifact_manager.get_temp_file(config.RCS_TREES_FILTERED_INDEX_TABLE),
        DB_OPEN_READ)
    ser = StringSerializer()
    if self._compress:
      ser = CompressingSerializer(ser)
    self._co_db = Database(
        artifact_manager.get_temp_file(config.CVS_CHECKOUT_DB), DB_OPEN_NEW,
        ser)

    # The set of CVSFile instances whose TextRecords have already been
    # read:
    self._loaded_files = set()

    # A map { CVSFILE : _FileTree } for files that currently have live
    # revisions:
    self._text_record_db = TextRecordDatabase(self._delta_db, self._co_db)

  def _get_text_record(self, cvs_rev):
    """Return the TextRecord instance for CVS_REV.

    If the TextRecords for CVS_REV.cvs_file haven't been loaded yet,
    do so now."""

    if cvs_rev.cvs_file not in self._loaded_files:
      for text_record in self._tree_db[cvs_rev.cvs_file.id].itervalues():
        self._text_record_db.add(text_record)
      self._loaded_files.add(cvs_rev.cvs_file)

    return self._text_record_db[cvs_rev.id]

  def get_content_stream(self, cvs_rev, suppress_keyword_substitution=False):
    """Check out the text for revision C_REV from the repository.

    Return the text wrapped in a readable file object.  If
    SUPPRESS_KEYWORD_SUBSTITUTION is True, any RCS keywords will be
    _un_expanded prior to returning the file content.  Note that $Log$
    never actually generates a log (makes test 68 fail).

    Revisions may be requested in any order, but if they are not
    requested in dependency order the checkout database will become
    very large.  Revisions may be skipped.  Each revision may be
    requested only once."""

    text = self._get_text_record(cvs_rev).checkout(self._text_record_db)
    if suppress_keyword_substitution:
      text = re.sub(self._kw_re, r'$\1$', text)

    return cStringIO.StringIO(text)

  def skip_content(self, cvs_rev):
    self._get_text_record(cvs_rev).decrement_refcount(self._text_record_db)

  def finish(self):
    self._text_record_db.log_leftovers()

    del self._text_record_db
    self._delta_db.close()
    self._tree_db.close()
    self._co_db.close()

