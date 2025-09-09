"""
BitBake Parsers

File parsers for the BitBake build tools.

"""


# Copyright (C) 2003, 2004  Chris Larson
# Copyright (C) 2003, 2004  Phil Blundell
#
# SPDX-License-Identifier: GPL-2.0-only
#
# Based on functions from the base bb module, Copyright 2003 Holger Schurig
#

handlers = []
# Cache mapping file extensions to the handler dict for faster dispatch
_supports_cache = {}
try:
    from bb.parse import metrics as _bb_metrics
except Exception:
    _bb_metrics = None

import errno
from collections import deque
import logging
import os
import stat
import bb
import bb.utils
import bb.siggen

logger = logging.getLogger("BitBake.Parsing")

class ParseError(Exception):
    """Exception raised when parsing fails"""
    def __init__(self, msg, filename, lineno=0):
        self.msg = msg
        self.filename = filename
        self.lineno = lineno
        Exception.__init__(self, msg, filename, lineno)

    def __str__(self):
        if self.lineno:
            return "ParseError at %s:%d: %s" % (self.filename, self.lineno, self.msg)
        else:
            return "ParseError in %s: %s" % (self.filename, self.msg)

class SkipRecipe(Exception):
    """Exception raised to skip this recipe"""

class SkipPackage(SkipRecipe):
    """Exception raised to skip this recipe (use SkipRecipe in new code)"""

__mtime_cache = {}
# Maximum entries for resolve_file() memoization
_RESOLVE_CACHE_MAX = 8192
def cached_mtime(f):
    if f not in __mtime_cache:
        res = os.stat(f)
        __mtime_cache[f] = (res.st_mtime_ns, res.st_size, res.st_ino)
    return __mtime_cache[f]

def cached_mtime_noerror(f):
    if f not in __mtime_cache:
        try:
            res = os.stat(f)
            __mtime_cache[f] = (res.st_mtime_ns, res.st_size, res.st_ino)
        except OSError:
            return 0
    return __mtime_cache[f]

def check_mtime(f, mtime):
    try:
        res = os.stat(f)
        current_mtime = (res.st_mtime_ns, res.st_size, res.st_ino)
        __mtime_cache[f] = current_mtime
    except OSError:
        current_mtime = 0
    return current_mtime == mtime

def update_mtime(f):
    try:
        res = os.stat(f)
        __mtime_cache[f] = (res.st_mtime_ns, res.st_size, res.st_ino)
    except OSError:
        if f in __mtime_cache:
            del __mtime_cache[f]
        return 0
    return __mtime_cache[f]

def update_cache(f):
    if f in __mtime_cache:
        logger.debug("Updating mtime cache for %s" % f)
        update_mtime(f)

def clear_cache():
    global __mtime_cache
    __mtime_cache = {}

def mark_dependency(d, f):
    if f.startswith('./'):
        f = "%s/%s" % (os.getcwd(), f[2:])
    deps = (d.getVar('__depends', False) or [])
    s = (f, cached_mtime_noerror(f))
    if s not in deps:
        deps.append(s)
        d.setVar('__depends', deps)

def check_dependency(d, f):
    s = (f, cached_mtime_noerror(f))
    deps = (d.getVar('__depends', False) or [])
    return s in deps
   
def _get_handler(fn, data):
    """Return the handler dict for this filename or None"""
    ext = os.path.splitext(fn)[1]
    if not os.environ.get('BB_OPT_DISABLE_SUPPORTS_CACHE') and ext in _supports_cache:
        if _bb_metrics:
            _bb_metrics.hit('supports')
        return _supports_cache[ext]
    for h in handlers:
        if h['supports'](fn, data):
            if not os.environ.get('BB_OPT_DISABLE_SUPPORTS_CACHE'):
                _supports_cache[ext] = h
            if _bb_metrics:
                _bb_metrics.miss('supports')
            return h
    if not os.environ.get('BB_OPT_DISABLE_SUPPORTS_CACHE'):
        _supports_cache[ext] = None
    if _bb_metrics:
        _bb_metrics.miss('supports')
    return None

def supports(fn, data):
    """Returns true if we have a handler for this file, false otherwise"""
    return 1 if _get_handler(fn, data) else 0

def handle(fn, data, include=0, baseconfig=False):
    """Call the handler that is appropriate for this file"""
    h = _get_handler(fn, data)
    if h:
        with data.inchistory.include(fn):
            return h['handle'](fn, data, include, baseconfig)
    raise ParseError("not a BitBake file", fn)

def init(fn, data):
    for h in handlers:
        if h['supports'](fn):
            return h['init'](data)

def init_parser(d):
    if hasattr(bb.parse, "siggen"):
        bb.parse.siggen.exit()
    bb.parse.siggen = bb.siggen.init(d)

def resolve_file(fn, d):
    # Lightweight memoization of resolution to cut repeated BBPATH scans
    # Key on (requested fn, absolute flag, BBPATH). Attempts are re-marked
    # every call to preserve dependency tracking semantics.
    global _resolve_cache, _resolve_cache_order
    try:
        _resolve_cache
    except NameError:
        _resolve_cache = {}
        _resolve_cache_order = deque()

    m = _bb_metrics
    _tok = None
    if m:
        try:
            _tok = m.time_start('resolve_file')
        except Exception:
            _tok = None
    try:
        if not os.path.isabs(fn):
            bbpath = d.getVar("BBPATH")
            key = (fn, False, bbpath)
            disable_cache = os.environ.get('BB_OPT_DISABLE_RESOLVE_CACHE')
            cached = None if disable_cache else _resolve_cache.get(key)
            if cached is not None:
                newfn, attempts = cached
                # Refresh simple LRU order
                try:
                    _resolve_cache_order.remove(key)
                except ValueError:
                    pass
                _resolve_cache_order.append(key)
                if _bb_metrics:
                    _bb_metrics.hit('resolve_file')
            else:
                newfn, attempts = bb.utils.which(bbpath, fn, history=True)
                # Maintain bounded cache size
                if not disable_cache:
                    _resolve_cache[key] = (newfn, tuple(attempts))
                    _resolve_cache_order.append(key)
                    if len(_resolve_cache_order) > _RESOLVE_CACHE_MAX:
                        old = _resolve_cache_order.popleft()
                        _resolve_cache.pop(old, None)
                        if _bb_metrics:
                            _bb_metrics.evict('resolve_file')
                if _bb_metrics:
                    _bb_metrics.miss('resolve_file')

            for af in attempts:
                mark_dependency(d, af)
            if not newfn:
                raise IOError(errno.ENOENT, "file %s not found in %s" % (fn, bbpath))
            fn = newfn
        else:
            mark_dependency(d, fn)

        if not os.path.isfile(fn):
            raise IOError(errno.ENOENT, "file %s not found" % fn)

        return fn
    finally:
        if m and _tok:
            try:
                m.time_end('resolve_file', _tok)
            except Exception:
                pass

# Used by OpenEmbedded metadata
__pkgsplit_cache__={}
def vars_from_file(mypkg, d):
    if not mypkg or not mypkg.endswith((".bb", ".bbappend")):
        return (None, None, None)
    if mypkg in __pkgsplit_cache__:
        return __pkgsplit_cache__[mypkg]

    myfile = os.path.splitext(os.path.basename(mypkg))
    parts = myfile[0].split('_')
    __pkgsplit_cache__[mypkg] = parts
    if len(parts) > 3:
        raise ParseError("Unable to generate default variables from filename (too many underscores)", mypkg)
    exp = 3 - len(parts)
    tmplist = []
    while exp != 0:
        exp -= 1
        tmplist.append(None)
    parts.extend(tmplist)
    return parts

def get_file_depends(d):
    '''Return the dependent files'''
    dep_files = []
    depends = d.getVar('__base_depends', False) or []
    depends = depends + (d.getVar('__depends', False) or [])
    for (fn, _) in depends:
        dep_files.append(os.path.abspath(fn))
    return " ".join(dep_files)

def vardeps(*varnames):
    """
    Function decorator that can be used to instruct the bitbake dependency
    parsing to add a dependency on the specified variables names

    Example:

        @bb.parse.vardeps("FOO", "BAR")
        def my_function():
            ...

    """
    def inner(f):
        if not hasattr(f, "bb_vardeps"):
            f.bb_vardeps = set()
        f.bb_vardeps |= set(varnames)
        return f
    return inner

def vardepsexclude(*varnames):
    """
    Function decorator that can be used to instruct the bitbake dependency
    parsing to ignore dependencies on the specified variable names in the code

    Example:

        @bb.parse.vardepsexclude("FOO", "BAR")
        def my_function():
            ...
    """
    def inner(f):
        if not hasattr(f, "bb_vardepsexclude"):
            f.bb_vardepsexclude = set()
        f.bb_vardepsexclude |= set(varnames)
        return f
    return inner

from bb.parse.parse_py import __version__, ConfHandler, BBHandler
