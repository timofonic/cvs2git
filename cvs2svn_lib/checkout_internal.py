# (Be in -*- python -*- mode.)
#
# ====================================================================
# Copyright (c) 2007-2009 CollabNet.  All rights reserved.
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
id as used for the corresponding CVSRevision instance.  It also
maintains a count of the times it is expected to be retrieved.
TextRecords come in several varieties:

FullTextRecord -- Used for revisions whose fulltext is contained
    directly in the RCS file, and therefore available during
    CollectRevsPass (i.e., typically revision 1.1 of each file).

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
are created to keep track of things.  The reference counts are all
initialized to zero.

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


from cStringIO import StringIO
import re
import time

from cvs2svn_lib import config
from cvs2svn_lib.common import DB_OPEN_NEW
from cvs2svn_lib.common import DB_OPEN_READ
from cvs2svn_lib.common import warning_prefix
from cvs2svn_lib.common import FatalError
from cvs2svn_lib.common import InternalError
from cvs2svn_lib.common import is_trunk_revision
from cvs2svn_lib.context import Ctx
from cvs2svn_lib.log import Log
from cvs2svn_lib.artifact_manager import artifact_manager
from cvs2svn_lib.symbol import Trunk
from cvs2svn_lib.cvs_item import CVSRevisionModification
from cvs2svn_lib.database import Database
from cvs2svn_lib.database import IndexedDatabase
from cvs2svn_lib.rcs_stream import RCSStream
from cvs2svn_lib.rcs_stream import MalformedDeltaException
from cvs2svn_lib.revision_manager import RevisionRecorder
from cvs2svn_lib.revision_manager import RevisionExcluder
from cvs2svn_lib.revision_manager import RevisionReader
from cvs2svn_lib.serializer import MarshalSerializer
from cvs2svn_lib.serializer import CompressingSerializer
from cvs2svn_lib.serializer import PrimedPickleSerializer

import cvs2svn_rcsparse


class TextRecord(object):
  """Bookkeeping data for the text of a single CVSRevision."""

  __slots__ = ['id', 'refcount']

  def __init__(self, id):
    # The cvs_rev_id of the revision whose text this is.
    self.id = id

    # The number of times that the text of this revision will be
    # retrieved.
    self.refcount = 0

  def __getstate__(self):
    return (self.id, self.refcount,)

  def __setstate__(self, state):
    (self.id, self.refcount,) = state

  def increment_dependency_refcounts(self, text_record_db):
    """Increment the refcounts of any records that this one depends on."""

    pass

  def decrement_refcount(self, text_record_db):
    """Decrement the number of times our text still has to be checked out.

    If the reference count goes to zero, call discard()."""

    self.refcount -= 1
    if self.refcount == 0:
      text_record_db.discard(self.id)

  def checkout(self, text_record_db):
    """Workhorse of the checkout process.

    Return the text for this revision, decrement our reference count,
    and update the databases depending on whether there will be future
    checkouts."""

    raise NotImplementedError()

  def free(self, text_record_db):
    """This instance will never again be checked out; free it.

    Also free any associated resources and decrement the refcounts of
    any other TextRecords that this one depends on."""

    raise NotImplementedError()


class FullTextRecord(TextRecord):
  __slots__ = []

  def __getstate__(self):
    return (self.id, self.refcount,)

  def __setstate__(self, state):
    (self.id, self.refcount,) = state

  def checkout(self, text_record_db):
    text = text_record_db.delta_db[self.id]
    self.decrement_refcount(text_record_db)
    return text

  def free(self, text_record_db):
    del text_record_db.delta_db[self.id]

  def __str__(self):
    return 'FullTextRecord(%x, %d)' % (self.id, self.refcount,)


class DeltaTextRecord(TextRecord):
  __slots__ = ['pred_id']

  def __init__(self, id, pred_id):
    TextRecord.__init__(self, id)

    # The cvs_rev_id of the revision relative to which this delta is
    # defined.
    self.pred_id = pred_id

  def __getstate__(self):
    return (self.id, self.refcount, self.pred_id,)

  def __setstate__(self, state):
    (self.id, self.refcount, self.pred_id,) = state

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

  def free(self, text_record_db):
    del text_record_db.delta_db[self.id]
    text_record_db[self.pred_id].decrement_refcount(text_record_db)

  def __str__(self):
    return 'DeltaTextRecord(%x -> %x, %d)' \
           % (self.pred_id, self.id, self.refcount,)


class CheckedOutTextRecord(TextRecord):
  __slots__ = []

  def __getstate__(self):
    return (self.id, self.refcount,)

  def __setstate__(self, state):
    (self.id, self.refcount,) = state

  def checkout(self, text_record_db):
    text = text_record_db.checkout_db['%x' % self.id]
    self.decrement_refcount(text_record_db)
    return text

  def free(self, text_record_db):
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
    #     need to retrieve deltas, but we are not allowed to modify
    #     the delta database.  So we use an IndexedDatabase whose
    #     __del__() method has been disabled to do nothing.
    self.delta_db = delta_db

    # A database-like object using cvs_rev_ids as keys and containing
    # fulltext strings as values.  This database is only set during
    # OutputPass.
    self.checkout_db = checkout_db

    # If this is set to a list, then the list holds the ids of
    # text_records that have to be deleted; when discard() is called,
    # it adds the requested id to the list but does not delete it.  If
    # this member is set to None, then text_records are deleted
    # immediately when discard() is called.
    self.deferred_deletes = None

  def __getstate__(self):
    return (self.text_records.values(),)

  def __setstate__(self, state):
    (text_records,) = state
    self.text_records = {}
    for text_record in text_records:
      self.add(text_record)
    self.delta_db = NullDatabase()
    self.checkout_db = NullDatabase()
    self.deferred_deletes = None

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

  def discard(self, *ids):
    """The text records with IDS are no longer needed; discard them.

    This involves calling their free() methods and also removing them
    from SELF.

    If SELF.deferred_deletes is not None, then the ids to be deleted
    are added to the list instead of deleted immediately.  This
    mechanism is to prevent a stack overflow from the avalanche of
    deletes that can result from deleting a long chain of revisions."""

    if self.deferred_deletes is None:
      # This is an outer-level delete.
      self.deferred_deletes = list(ids)
      while self.deferred_deletes:
        id = self.deferred_deletes.pop()
        text_record = self[id]
        if text_record.refcount != 0:
          raise InternalError(
              'TextRecordDatabase.discard(%s) called with refcount = %d'
              % (text_record, text_record.refcount,)
              )
        # This call might cause other text_record ids to be added to
        # self.deferred_deletes:
        text_record.free(self)
        del self[id]
      self.deferred_deletes = None
    else:
      self.deferred_deletes.extend(ids)

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
        text_record.id
        for text_record in self.itervalues()
        if text_record.refcount == 0
        ]

    self.discard(*unused)

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


class _Sink(cvs2svn_rcsparse.Sink):
  def __init__(self, revision_recorder, cvs_file_items):
    self.revision_recorder = revision_recorder
    self.cvs_file_items = cvs_file_items

    # A map {rev : base_rev} indicating that the text for rev is
    # stored in CVS as a delta relative to base_rev.
    self.base_revisions = {}

    # The revision that is stored with its fulltext in CVS (usually
    # the oldest revision on trunk):
    self.head_revision = None

    # The first logical revision on trunk (usually '1.1'):
    self.revision_1_1 = None

    # Keep track of the revisions whose revision info has been seen so
    # far (to avoid repeated revision info blocks):
    self.revisions_seen = set()

  def set_head_revision(self, revision):
    self.head_revision = revision

  def define_revision(
        self, revision, timestamp, author, state, branches, next
        ):
    if next:
      self.base_revisions[next] = revision
    else:
      if is_trunk_revision(revision):
        self.revision_1_1 = revision

    for branch in branches:
      self.base_revisions[branch] = revision

  def set_revision_info(self, revision, log, text):
    if revision in self.revisions_seen:
      # One common form of CVS repository corruption is that the
      # Deltatext block for revision 1.1 appears twice.  CollectData
      # has already warned about this problem; here we can just ignore
      # it.
      return
    else:
      self.revisions_seen.add(revision)

    cvs_rev_id = self.cvs_file_items.original_ids[revision]
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

      if revision == self.head_revision:
        # This is HEAD, as fulltext.  Initialize the RCSStream so
        # that we can compute deltas backwards in time.
        self._stream = RCSStream(text)
        self._stream_revision = revision
      else:
        # Any other trunk revision is a backward delta.  Apply the
        # delta to the RCSStream to mutate it to the contents of this
        # revision, and also to get the reverse delta, which we store
        # as the forward delta of our child revision.
        try:
          text = self._stream.invert_diff(text)
        except MalformedDeltaException, e:
          Log().error(
              'Malformed RCS delta in %s, revision %s: %s'
              % (self.cvs_file_items.cvs_file.filename, revision, e)
              )
          raise RuntimeError()
        text_record = DeltaTextRecord(
            self.cvs_file_items.original_ids[self._stream_revision],
            cvs_rev_id
            )
        self.revision_recorder._writeout(text_record, text)
        self._stream_revision = revision

      if revision == self.revision_1_1:
        # This is revision 1.1.  Write its fulltext:
        text_record = FullTextRecord(cvs_rev_id)
        self.revision_recorder._writeout(text_record, self._stream.get_text())

        # There will be no more trunk revisions delivered, so free the
        # RCSStream.
        del self._stream
        del self._stream_revision

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
          cvs_rev_id,
          self.cvs_file_items.original_ids[self.base_revisions[revision]]
          )
      self.revision_recorder._writeout(text_record, text)

    return None


class InternalRevisionRecorder(RevisionRecorder):
  """A RevisionRecorder that reconstructs the fulltext internally."""

  def record_text(self, cvs_rev, log, text):
    return None


class InternalRevisionExcluder(RevisionExcluder):
  """The RevisionExcluder used by InternalRevisionReader."""

  def __init__(self, compress):
    RevisionExcluder.__init__(self)
    self._compress = compress

  def register_artifacts(self, which_pass):
    artifact_manager.register_temp_file(
        config.RCS_DELTAS_INDEX_TABLE, which_pass
        )
    artifact_manager.register_temp_file(config.RCS_DELTAS_STORE, which_pass)
    artifact_manager.register_temp_file(
        config.RCS_TREES_INDEX_TABLE, which_pass
        )
    artifact_manager.register_temp_file(config.RCS_TREES_STORE, which_pass)

  def start(self):
    ser = MarshalSerializer()
    if self._compress:
      ser = CompressingSerializer(ser)
    self._rcs_deltas = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_DELTAS_STORE),
        artifact_manager.get_temp_file(config.RCS_DELTAS_INDEX_TABLE),
        DB_OPEN_NEW, ser
        )
    primer = (FullTextRecord, DeltaTextRecord)
    self._rcs_trees = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_TREES_STORE),
        artifact_manager.get_temp_file(config.RCS_TREES_INDEX_TABLE),
        DB_OPEN_NEW, PrimedPickleSerializer(primer)
        )

  def _writeout(self, text_record, text):
    self.text_record_db.add(text_record)
    self._rcs_deltas[text_record.id] = text

  def process_file(self, cvs_file_items):
    """Read revision information for the file described by CVS_FILE_ITEMS.

    Compute the text record refcounts, discard any records that are
    unneeded, and store the text records for the file to the
    _rcs_trees database."""

    # A map from cvs_rev_id to TextRecord instance:
    self.text_record_db = TextRecordDatabase(self._rcs_deltas, NullDatabase())

    cvs2svn_rcsparse.parse(
        open(cvs_file_items.cvs_file.filename, 'rb'),
        _Sink(self, cvs_file_items),
        )

    self.text_record_db.recompute_refcounts(cvs_file_items)
    self.text_record_db.free_unused()
    self._rcs_trees[cvs_file_items.cvs_file.id] = self.text_record_db
    del self.text_record_db

  def finish(self):
    self._rcs_deltas.close()
    self._rcs_trees.close()


class _KeywordExpander:
  """A class whose instances provide substitutions for CVS keywords.

  This class is used via its __call__() method, which should be called
  with a match object representing a match for a CVS keyword string.
  The method returns the replacement for the matched text.

  The __call__() method works by calling the method with the same name
  as that of the CVS keyword (converted to lower case).

  Instances of this class can be passed as the REPL argument to
  re.sub()."""

  date_fmt_old = "%Y/%m/%d %H:%M:%S"    # CVS 1.11, rcs
  date_fmt_new = "%Y-%m-%d %H:%M:%S"    # CVS 1.12

  date_fmt = date_fmt_new

  @classmethod
  def use_old_date_format(klass):
      """Class method to ensure exact compatibility with CVS 1.11
      output.  Use this if you want to verify your conversion and you're
      using CVS 1.11."""
      klass.date_fmt = klass.date_fmt_old

  def __init__(self, cvs_rev):
    self.cvs_rev = cvs_rev

  def __call__(self, match):
    return '$%s: %s $' % \
           (match.group(1), getattr(self, match.group(1).lower())(),)

  def author(self):
    return Ctx()._metadata_db[self.cvs_rev.metadata_id].original_author

  def date(self):
    return time.strftime(self.date_fmt,
                         time.gmtime(self.cvs_rev.timestamp))

  def header(self):
    return '%s %s %s %s Exp' % \
           (self.source(), self.cvs_rev.rev, self.date(), self.author())

  def id(self):
    return '%s %s %s %s Exp' % \
           (self.rcsfile(), self.cvs_rev.rev, self.date(), self.author())

  def locker(self):
    # Handle kvl like kv, as a converted repo is supposed to have no
    # locks.
    return ''

  def log(self):
    # Would need some special handling.
    return 'not supported by cvs2svn'

  def name(self):
    # Cannot work, as just creating a new symbol does not check out
    # the revision again.
    return 'not supported by cvs2svn'

  def rcsfile(self):
    return self.cvs_rev.cvs_file.basename + ",v"

  def revision(self):
    return self.cvs_rev.rev

  def source(self):
    project = self.cvs_rev.cvs_file.project
    return project.cvs_repository_root + '/' + project.cvs_module + \
        self.cvs_rev.cvs_file.cvs_path + ",v"

  def state(self):
    # We check out only live revisions.
    return 'Exp'


class InternalRevisionReader(RevisionReader):
  """A RevisionReader that reads the contents from an own delta store."""

  _kws = 'Author|Date|Header|Id|Locker|Log|Name|RCSfile|Revision|Source|State'
  _kw_re = re.compile(r'\$(' + _kws + r'):[^$\n]*\$')
  _kwo_re = re.compile(r'\$(' + _kws + r')(:[^$\n]*)?\$')

  def __init__(self, compress):
    self._compress = compress

  def register_artifacts(self, which_pass):
    artifact_manager.register_temp_file(config.CVS_CHECKOUT_DB, which_pass)
    artifact_manager.register_temp_file_needed(
        config.RCS_DELTAS_STORE, which_pass
        )
    artifact_manager.register_temp_file_needed(
        config.RCS_DELTAS_INDEX_TABLE, which_pass
        )
    artifact_manager.register_temp_file_needed(
        config.RCS_TREES_STORE, which_pass
        )
    artifact_manager.register_temp_file_needed(
        config.RCS_TREES_INDEX_TABLE, which_pass
        )

  def start(self):
    self._delta_db = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_DELTAS_STORE),
        artifact_manager.get_temp_file(config.RCS_DELTAS_INDEX_TABLE),
        DB_OPEN_READ)
    self._delta_db.__delitem__ = lambda id: None
    self._tree_db = IndexedDatabase(
        artifact_manager.get_temp_file(config.RCS_TREES_STORE),
        artifact_manager.get_temp_file(config.RCS_TREES_INDEX_TABLE),
        DB_OPEN_READ)
    ser = MarshalSerializer()
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
    never actually generates a log (which makes test 'requires_cvs()'
    fail).

    Revisions may be requested in any order, but if they are not
    requested in dependency order the checkout database will become
    very large.  Revisions may be skipped.  Each revision may be
    requested only once."""

    try:
      text = self._get_text_record(cvs_rev).checkout(self._text_record_db)
    except MalformedDeltaException, (msg):
      raise FatalError('Malformed RCS delta in %s, revision %s: %s'
                       % (cvs_rev.cvs_file.get_filename(), cvs_rev.rev, msg))
    if cvs_rev.cvs_file.mode != 'b' and cvs_rev.cvs_file.mode != 'o':
      if suppress_keyword_substitution or cvs_rev.cvs_file.mode == 'k':
        text = self._kw_re.sub(r'$\1$', text)
      else:
        text = self._kwo_re.sub(_KeywordExpander(cvs_rev), text)

    return StringIO(text)

  def finish(self):
    self._text_record_db.log_leftovers()

    del self._text_record_db
    self._delta_db.close()
    self._tree_db.close()
    self._co_db.close()

