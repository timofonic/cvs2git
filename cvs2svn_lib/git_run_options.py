# (Be in -*- python -*- mode.)
#
# ====================================================================
# Copyright (c) 2000-2009 CollabNet.  All rights reserved.
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

"""This module manages cvs2git run options."""


import sys
import datetime
import codecs

from cvs2svn_lib.version import VERSION
from cvs2svn_lib.common import error_prefix
from cvs2svn_lib.common import FatalError
from cvs2svn_lib.context import Ctx
from cvs2svn_lib.dvcs_common import DVCSRunOptions
from cvs2svn_lib.run_options import RunOptions
from cvs2svn_lib.run_options import ContextOption
from cvs2svn_lib.run_options import IncompatibleOption
from cvs2svn_lib.run_options import authors
from cvs2svn_lib.run_options import not_both
from cvs2svn_lib.man_writer import ManWriter
from cvs2svn_lib.revision_manager import NullRevisionRecorder
from cvs2svn_lib.revision_manager import NullRevisionExcluder
from cvs2svn_lib.rcs_revision_manager import RCSRevisionReader
from cvs2svn_lib.cvs_revision_manager import CVSRevisionReader
from cvs2svn_lib.git_revision_recorder import GitRevisionRecorder
from cvs2svn_lib.git_output_option import GitRevisionMarkWriter
from cvs2svn_lib.git_output_option import GitOutputOption
from cvs2svn_lib.fulltext_revision_recorder \
     import SimpleFulltextRevisionRecorderAdapter


short_desc = 'convert a cvs repository into a git repository'

synopsis = """\
.B cvs2git
[\\fIOPTION\\fR]... \\fIOUTPUT-OPTIONS CVS-REPOS-PATH\\fR
.br
.B cvs2git
[\\fIOPTION\\fR]... \\fI--options=PATH\\fR
"""

long_desc = """\
Create a new git repository based on the version history stored in a
CVS repository. Each CVS commit will be mirrored in the git
repository, including such information as date of commit and id of the
committer.
.P
The output of this program are a "blobfile" and a "dumpfile", which
together can be loaded into a git repository using "git fast-import".
.P
\\fICVS-REPOS-PATH\\fR is the filesystem path of the part of the CVS
repository that you want to convert.  This path doesn't have to be the
top level directory of a CVS repository; it can point at a project
within a repository, in which case only that project will be
converted.  This path or one of its parent directories has to contain
a subdirectory called CVSROOT (though the CVSROOT directory can be
empty).
.P
It is not possible directly to convert a CVS repository to which you
only have remote access, but the FAQ describes tools that may be used
to create a local copy of a remote CVS repository.
"""

files = """\
A directory called \\fIcvs2svn-tmp\\fR (or the directory specified by
\\fB--tmpdir\\fR) is used as scratch space for temporary data files.
"""

see_also = [
  ('cvs', '1'),
  ('git', '1'),
  ('git-fast-import', '1'),
  ]


class GitRunOptions(DVCSRunOptions):

  def _get_output_options_group(self):
    group = DVCSRunOptions._get_output_options_group(self)

    group.add_option(IncompatibleOption(
        '--blobfile', type='string',
        action='store',
        help='path to which the "blob" data should be written',
        man_help=(
            'Write the "blob" data (containing revision contents) to '
            '\\fIpath\\fR.'
            ),
        metavar='PATH',
        ))
    group.add_option(IncompatibleOption(
        '--dumpfile', type='string',
        action='store',
        help='path to which the revision data should be written',
        man_help=(
            'Write the revision data (branches and commits) to \\fIpath\\fR.'
            ),
        metavar='PATH',
        ))
    group.add_option(ContextOption(
        '--dry-run',
        action='store_true',
        help=(
            'do not create any output; just print what would happen.'
            ),
        man_help=(
            'Do not create any output; just print what would happen.'
            ),
        ))

    return group

  # XXX shouldn't this be the same as -- or, better, shared with --
  # SVNRunOptions?
  def _get_extraction_options_group(self):
    group = RunOptions._get_extraction_options_group(self)

    self.parser.set_default('use_cvs', False)
    group.add_option(IncompatibleOption(
        '--use-cvs',
        action='store_true',
        help=(
            'use CVS to extract revision contents (slower than '
            '--use-rcs but more reliable) (default)'
            ),
        man_help=(
            'Use CVS to extract revision contents.  This option is slower '
            'than \\fB--use-rcs\\fR but more reliable.'
            ),
        ))
    self.parser.set_default('use_rcs', False)
    group.add_option(IncompatibleOption(
        '--use-rcs',
        action='store_true',
        help=(
            'use RCS to extract revision contents (faster than '
            '--use-cvs but fails in some cases)'
            ),
        man_help=(
            'Use RCS \'co\' to extract revision contents.  This option is '
            'faster than \\fB--use-cvs\\fR but fails in some cases.'
            ),
        ))

    return group

  def callback_manpage(self, option, opt_str, value, parser):
    f = codecs.getwriter('utf_8')(sys.stdout)
    ManWriter(
        parser,
        section='1',
        date=datetime.date.today(),
        source='Version %s' % (VERSION,),
        manual='User Commands',
        short_desc=short_desc,
        synopsis=synopsis,
        long_desc=long_desc,
        files=files,
        authors=authors,
        see_also=see_also,
        ).write_manpage(f)
    sys.exit(0)

  # XXX not quite the same as same method in SVNRunOptions, but it
  # probably should be
  def process_extraction_options(self):
    """Process options related to extracting data from the CVS
    repository."""
    ctx = Ctx()
    options = self.options

    not_both(options.use_rcs, '--use-rcs',
             options.use_cvs, '--use-cvs')

    if options.use_rcs:
      revision_reader = RCSRevisionReader(
          co_executable=options.co_executable
          )
    else:
      # --use-cvs is the default:
      revision_reader = CVSRevisionReader(
          cvs_executable=options.cvs_executable
          )

    if ctx.dry_run:
      ctx.revision_recorder = NullRevisionRecorder()
    else:
      if not (options.blobfile and options.dumpfile):
        raise FatalError("must pass '--blobfile' and '--dumpfile' options.")
      ctx.revision_recorder = SimpleFulltextRevisionRecorderAdapter(
          revision_reader,
          GitRevisionRecorder(options.blobfile),
          )

    ctx.revision_excluder = NullRevisionExcluder()
    ctx.revision_reader = None

  def process_output_options(self):
    """Process options related to fastimport output."""
    ctx = Ctx()
    ctx.output_option = GitOutputOption(
        self.options.dumpfile,
        GitRevisionMarkWriter(),
        max_merges=None,
        # Optional map from CVS author names to git author names:
        author_transforms={}, # FIXME
        )
