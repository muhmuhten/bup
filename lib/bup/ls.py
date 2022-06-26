"""Common code for listing files from a bup repository."""

from __future__ import absolute_import
from binascii import hexlify
from itertools import chain
from stat import S_ISDIR
import os.path
import posixpath

from bup import metadata, vfs, xstat
from bup.compat import argv_bytes
from bup.io import path_msg
from bup.options import Options
from bup.repo import LocalRepo, RemoteRepo
from bup.helpers import columnate, istty1, log

def item_hash(item, tree_for_commit):
    """If the item is a Commit, return its commit oid, otherwise return
    the item's oid, if it has one.

    """
    if tree_for_commit and isinstance(item, vfs.Commit):
        return item.coid
    return getattr(item, 'oid', None)

def item_info(item, name,
              show_hash = False,
              commit_hash=False,
              long_fmt = False,
              classification = None,
              numeric_ids = False,
              human_readable = False):
    """Return bytes containing the information to display for the VFS
    item.  Classification may be "all", "type", or None.

    """
    result = b''
    if show_hash:
        oid = item_hash(item, commit_hash)
        result += b'%s ' % (hexlify(oid) if oid
                            else b'0000000000000000000000000000000000000000')
    if long_fmt:
        meta = item.meta.copy()
        meta.path = name
        # FIXME: need some way to track fake vs real meta items?
        result += metadata.summary_bytes(meta,
                                         numeric_ids=numeric_ids,
                                         classification=classification,
                                         human_readable=human_readable)
    else:
        result += name
        if classification:
            cls = xstat.classification_str(vfs.item_mode(item),
                                           classification == 'all')
            result += cls.encode('ascii')
    return result


optspec = """
bup ls [-r host:path] [-l] [-d] [-F] [-a] [-A] [-s] [-n] [path...]
--
r,remote=   remote repository path
s,hash   show hash for each file
commit-hash show commit hash instead of tree for commits (implies -s)
a,all    show hidden files
A,almost-all    show hidden files except . and ..
l        use a detailed, long listing format
d,directory show directories, not contents; don't follow symlinks
R,recursive recurse into subdirectories
F,classify append type indicator: dir/ sym@ fifo| sock= exec*
file-type append type indicator: dir/ sym@ fifo| sock=
human-readable    print human readable file sizes (i.e. 3.9K, 4.7M)
n,numeric-ids list numeric IDs (user, group, etc.) rather than names
"""

def opts_from_cmdline(args, onabort=None, pwd=b'/'):
    """Parse ls command line arguments and return a dictionary of ls
    options, agumented with "classification", "long_listing",
    "paths", and "show_hidden".

    """
    if onabort:
        opt, flags, extra = Options(optspec, onabort=onabort).parse_bytes(args)
    else:
        opt, flags, extra = Options(optspec).parse_bytes(args)

    opt.paths = [argv_bytes(x) for x in extra] or (pwd,)
    opt.long_listing = opt.l
    opt.classification = None
    opt.show_hidden = None
    for flag in flags:
        option, parameter = flag
        if option in ('-F', '--classify'):
            opt.classification = 'all'
        elif option == '--file-type':
            opt.classification = 'type'
        elif option in ('-a', '--all'):
            opt.show_hidden = 'all'
        elif option in ('-A', '--almost-all'):
            opt.show_hidden = 'almost'
    return opt

def show_paths(repo, opt, paths, out, pwd, should_columnate, prefix=b''):
    def item_line(item, name):
        return item_info(item, prefix + name,
                         show_hash=opt.hash,
                         commit_hash=opt.commit_hash,
                         long_fmt=opt.long_listing,
                         classification=opt.classification,
                         numeric_ids=opt.numeric_ids,
                         human_readable=opt.human_readable)

    ret = 0
    want_meta = bool(opt.long_listing or opt.classification)

    pending = []

    last_n = len(paths) - 1
    for n, printpath in enumerate(paths):
        path = posixpath.join(pwd, printpath)
        try:
            if last_n > 0:
                out.write(b'%s:\n' % printpath)

            if opt.directory:
                resolved = vfs.resolve(repo, path, follow=False)
            else:
                resolved = vfs.try_resolve(repo, path, want_meta=want_meta)

            leaf_name, leaf_item = resolved[-1]
            if not leaf_item:
                log('error: cannot access %r in %r\n'
                    % ('/'.join(path_msg(name) for name, item in resolved),
                       path_msg(path)))
                ret = 1
                continue
            if not opt.directory and S_ISDIR(vfs.item_mode(leaf_item)):
                items = vfs.contents(repo, leaf_item, want_meta=want_meta)
                if opt.show_hidden == 'all':
                    # Match non-bup "ls -a ... /".
                    parent = resolved[-2] if len(resolved) > 1 else resolved[0]
                    items = chain(items, ((b'..', parent[1]),))
                for sub_name, sub_item in sorted(items, key=lambda x: x[0]):
                    if opt.show_hidden != 'all' and sub_name == b'.':
                        continue
                    if sub_name.startswith(b'.') and \
                       opt.show_hidden not in ('almost', 'all'):
                        continue
                    # always skip . and .. in the subfolders - already printed it anyway
                    if prefix and sub_name in (b'.', b'..'):
                        continue
                    if opt.l:
                        sub_item = vfs.ensure_item_has_metadata(repo, sub_item,
                                                                include_size=True)
                    elif want_meta:
                        sub_item = vfs.augment_item_meta(repo, sub_item,
                                                         include_size=True)
                    line = item_line(sub_item, sub_name)
                    if should_columnate:
                        pending.append(line)
                    else:
                        out.write(line)
                        out.write(b'\n')
                    # recurse into subdirectories (apart from . and .., of course)
                    if opt.recursive and S_ISDIR(vfs.item_mode(sub_item)) and sub_name not in (b'.', b'..'):
                        show_paths(repo, opt, [path + b'/' + sub_name], out, pwd,
                                   should_columnate, prefix=prefix + sub_name + b'/')
            else:
                if opt.long_listing:
                    leaf_item = vfs.augment_item_meta(repo, leaf_item,
                                                      include_size=True)
                line = item_line(leaf_item, os.path.normpath(path))
                if should_columnate:
                    pending.append(line)
                else:
                    out.write(line)
                    out.write(b'\n')
        except vfs.IOError as ex:
            log('bup: %s\n' % ex)
            ret = 1

        if pending:
            out.write(columnate(pending, b''))
            pending = []

        if n < last_n:
            out.write(b'\n')

    return ret

def within_repo(repo, opt, out, pwd=b''):
    if opt.commit_hash:
        opt.hash = True

    should_columnate = not opt.recursive and not opt.long_listing and istty1
    return show_paths(repo, opt, opt.paths, out, pwd, should_columnate)

def via_cmdline(args, out=None, onabort=None):
    """Write a listing of a file or directory in the bup repository to out.

    When a long listing is not requested and stdout is attached to a
    tty, the output is formatted in columns. When not attached to tty
    (for example when the output is piped to another command), one
    file is listed per line.

    """
    assert out
    opt = opts_from_cmdline(args, onabort=onabort)
    with RemoteRepo(argv_bytes(opt.remote)) if opt.remote \
         else LocalRepo() as repo:
        return within_repo(repo, opt, out)
