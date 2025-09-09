"""
   class for handling configuration data files

   Reads a .conf file and obtains its metadata

"""

# Copyright (C) 2003, 2004  Chris Larson
# Copyright (C) 2003, 2004  Phil Blundell
#
# SPDX-License-Identifier: GPL-2.0-only
#

import errno
import re
import os
import bb.utils
from bb.parse import ParseError, resolve_file, ast, logger, handle
try:
    from bb.parse import metrics as _bb_metrics
except Exception:
    _bb_metrics = None

__config_regexp__  = re.compile( r"""
    ^
    (?P<exp>export\s+)?
    (?P<var>[a-zA-Z0-9\-_+.${}/~:]*?)
    (\[(?P<flag>[a-zA-Z0-9\-_+.][a-zA-Z0-9\-_+.@/]*)\])?

    (?P<whitespace>\s*) (
        (?P<colon>:=) |
        (?P<lazyques>\?\?=) |
        (?P<ques>\?=) |
        (?P<append>\+=) |
        (?P<prepend>=\+) |
        (?P<predot>=\.) |
        (?P<postdot>\.=) |
        =
    ) (?P<whitespace2>\s*)

    (?!'[^']*'[^']*'$)
    (?!\"[^\"]*\"[^\"]*\"$)
    (?P<apo>['\"])
    (?P<value>.*)
    (?P=apo)
    $
    """, re.X)
__include_regexp__ = re.compile( r"include\s+(.+)" )
__require_regexp__ = re.compile( r"require\s+(.+)" )
__includeall_regexp__ = re.compile( r"include_all\s+(.+)" )
__export_regexp__ = re.compile( r"export\s+([a-zA-Z0-9\-_+.${}/~]+)$" )
__unset_regexp__ = re.compile( r"unset\s+([a-zA-Z0-9\-_+.${}/~]+)$" )
__unset_flag_regexp__ = re.compile( r"unset\s+([a-zA-Z0-9\-_+.${}/~]+)\[([a-zA-Z0-9\-_+.][a-zA-Z0-9\-_+.@]+)\]$" )
__addpylib_regexp__      = re.compile(r"addpylib\s+(.+)\s+(.+)" )
__addfragments_regexp__  = re.compile(r"addfragments\s+(.+)\s+(.+)\s+(.+)\s+(.+)" )

def init(data):
    return

def supports(fn, d):
    return fn[-5:] == ".conf"

def include(parentfn, fns, lineno, data, error_out):
    """
    error_out: A string indicating the verb (e.g. "include", "inherit") to be
    used in a ParseError that will be raised if the file to be included could
    not be included. Specify False to avoid raising an error in this case.
    """
    fns = data.expand(fns)
    parentfn = data.expand(parentfn)

    # "include" or "require" accept zero to n space-separated file names to include.
    for fn in fns.split():
        include_single_file(parentfn, fn, lineno, data, error_out)

def include_single_file(parentfn, fn, lineno, data, error_out):
    """
    Helper function for include() which does not expand or split its parameters.
    """
    if parentfn == fn: # prevent infinite recursion
        return None

    if not os.path.isabs(fn):
        dname = os.path.dirname(parentfn)
        bbpath = "%s:%s" % (dname, data.getVar("BBPATH"))
        # Resolve includes with a small LRU cache to avoid repeated scans
        global _include_resolve_cache, _include_resolve_order, _include_resolve_max
        try:
            _include_resolve_cache
        except NameError:
            _include_resolve_cache = {}
            _include_resolve_order = []
            _include_resolve_max = 8192
        key = (fn, dname, data.getVar("BBPATH"))
        cached = None
        if not os.environ.get('BB_OPT_DISABLE_INCLUDE_LRU'):
            try:
                _include_resolve_order.remove(key)
                _include_resolve_order.append(key)
                cached = _include_resolve_cache[key]
            except (ValueError, KeyError):
                pass
        if cached is not None:
            abs_fn, attempts = cached
            if _bb_metrics:
                _bb_metrics.hit('include')
        else:
            # Use an index for basename-only includes; fall back to which() for hierarchical paths
            _tok = None
            if _bb_metrics:
                try:
                    _tok = _bb_metrics.time_start('include')
                except Exception:
                    _tok = None
            try:
                use_index = ('/' not in fn) and (not os.environ.get('BB_OPT_DISABLE_INCLUDE_INDEX'))
                if use_index:
                    abs_fn, attempts = _include_index_resolve(dname, data.getVar("BBPATH"), fn)
                    if _bb_metrics:
                        # Count index usage
                        if abs_fn:
                            _bb_metrics.hit('include_index')
                        else:
                            _bb_metrics.miss('include_index')
                else:
                    abs_fn, attempts = bb.utils.which(bbpath, fn, history=True)
                    if _bb_metrics and ('/' not in fn):
                        _bb_metrics.miss('include_index')
            finally:
                if _bb_metrics and _tok:
                    try:
                        _bb_metrics.time_end('include', _tok)
                    except Exception:
                        pass
            if not os.environ.get('BB_OPT_DISABLE_INCLUDE_LRU'):
                _include_resolve_cache[key] = (abs_fn, tuple(attempts))
                _include_resolve_order.append(key)
                if len(_include_resolve_order) > _include_resolve_max:
                    old = _include_resolve_order.pop(0)
                    _include_resolve_cache.pop(old, None)
                    if _bb_metrics:
                        _bb_metrics.evict('include')
            if _bb_metrics:
                _bb_metrics.miss('include')
        if abs_fn and bb.parse.check_dependency(data, abs_fn):
            logger.warning("Duplicate inclusion for %s in %s" % (abs_fn, data.getVar('FILE')))
        for af in attempts:
            bb.parse.mark_dependency(data, af)
        if abs_fn:
            fn = abs_fn
    elif bb.parse.check_dependency(data, fn):
        logger.warning("Duplicate inclusion for %s in %s" % (fn, data.getVar('FILE')))

    try:
        bb.parse.handle(fn, data, True)
    except (IOError, OSError) as exc:
        if exc.errno == errno.ENOENT:
            if error_out:
                raise ParseError("Could not %s file %s" % (error_out, fn), parentfn, lineno)
            logger.debug2("CONF file '%s' not found", fn)
        else:
            if error_out:
                raise ParseError("Could not %s file %s: %s" % (error_out, fn, exc.strerror), parentfn, lineno)
            else:
                raise ParseError("Error parsing %s: %s" % (fn, exc.strerror), parentfn, lineno)

# We have an issue where a UI might want to enforce particular settings such as
# an empty DISTRO variable. If configuration files do something like assigning
# a weak default, it turns out to be very difficult to filter out these changes,
# particularly when the weak default might appear half way though parsing a chain
# of configuration files. We therefore let the UIs hook into configuration file
# parsing. This turns out to be a hard problem to solve any other way.
confFilters = []

def handle(fn, data, include, baseconfig=False):
    init(data)

    if include == 0:
        oldfile = None
    else:
        oldfile = data.getVar('FILE', False)

    abs_fn = resolve_file(fn, data)
    with open(abs_fn, 'r') as f:

        statements = ast.StatementGroup()
        lineno = 0
        for lineno, origline in enumerate(f, start=1):
            w = origline.strip()
            if not w:
                continue
            s = origline.rstrip()
            origlineno = lineno
            while s[-1] == '\\':
                line = f.readline()
                if not line:
                    break
                origline += line
                s2 = line.rstrip()
                lineno = lineno + 1
                if (not s2 or (s2 and s2[0] != "#")) and s[0] == "#":
                    bb.fatal("There is a confusing multiline, partially commented expression starting on line %s of file %s:\n%s\nPlease clarify whether this is all a comment or should be parsed." % (origlineno, fn, origline))

                s = s[:-1] + s2
            if s and s[0] == '#':
                continue
            feeder(lineno, s, abs_fn, statements, baseconfig=baseconfig)

    # DONE WITH PARSING... time to evaluate
    data.setVar('FILE', abs_fn)
    statements.eval(data)
    if oldfile:
        data.setVar('FILE', oldfile)

    for f in confFilters:
        f(fn, data)

    return data

# Cache parsed statements of .conf files (AST), keyed by (abs_fn, baseconfig)
_conf_statements_cache = {}

# Include index: basename -> absolute path per (dname, BBPATH) with directory fingerprinting
_include_index_cache = {}
_include_index_order = []
_include_index_max = 256

def _include_search_dirs(dname, bbpath):
    dirs = []
    # Prepend the directory of the including file
    if dname:
        dirs.append(dname)
    for p in (bbpath or '').split(':'):
        if p:
            dirs.append(p)
    return dirs

def _dirs_fingerprint(dirs):
    fp = []
    for d in dirs:
        try:
            st = os.stat(d)
            fp.append((d, st.st_mtime_ns, st.st_ino))
        except OSError:
            fp.append((d, 0, 0))
    return tuple(fp)

def _build_include_index(dname, bbpath):
    dirs = _include_search_dirs(dname, bbpath)
    mapping = {}
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for de in it:
                    # Only index regular files and symlinks to files
                    try:
                        isfile = de.is_file(follow_symlinks=True)
                    except OSError:
                        isfile = False
                    if not isfile:
                        continue
                    name = de.name
                    if name not in mapping:
                        mapping[name] = os.path.join(d, name)
        except OSError:
            continue
    return _dirs_fingerprint(dirs), mapping

def _get_include_index(dname, bbpath):
    key = (dname or '', bbpath or '')
    cached = _include_index_cache.get(key)
    dirs = _include_search_dirs(dname, bbpath)
    fp = _dirs_fingerprint(dirs)
    if cached is not None:
        cfp, cmap = cached
        if cfp == fp:
            try:
                _include_index_order.remove(key)
            except ValueError:
                pass
            _include_index_order.append(key)
            return cmap
    # (Re)build
    cfp, cmap = _build_include_index(dname, bbpath)
    _include_index_cache[key] = (cfp, cmap)
    _include_index_order.append(key)
    if len(_include_index_order) > _include_index_max:
        old = _include_index_order.pop(0)
        _include_index_cache.pop(old, None)
    return cmap

def _include_index_resolve(dname, bbpath, filename):
    """Resolve a basename include using the include index.
    Returns (resolved_abs_path, attempts_list)
    """
    cmap = _get_include_index(dname, bbpath)
    resolved = cmap.get(filename)
    attempts = []
    for d in _include_search_dirs(dname, bbpath):
        attempts.append(os.path.abspath(os.path.join(d, filename)))
    return resolved, tuple(attempts)

def _get_conf_statements(abs_fn, baseconfig):
    key = (abs_fn, bool(baseconfig))
    st = None if os.environ.get('BB_OPT_DISABLE_CONF_AST_CACHE') else _conf_statements_cache.get(key)
    if st is not None:
        if _bb_metrics:
            _bb_metrics.hit('conf_ast')
        return st
    _tok = None
    if _bb_metrics:
        try:
            _tok = _bb_metrics.time_start('conf_ast_parse')
        except Exception:
            _tok = None
    with open(abs_fn, 'r') as f:
        statements = ast.StatementGroup()
        lineno = 0
        for lineno, origline in enumerate(f, start=1):
            w = origline.strip()
            if not w:
                continue
            s = origline.rstrip()
            origlineno = lineno
            while s[-1] == '\\':
                line = f.readline()
                if not line:
                    break
                origline += line
                s2 = line.rstrip()
                lineno = lineno + 1
                if (not s2 or (s2 and s2[0] != "#")) and s[0] == "#":
                    bb.fatal("There is a confusing multiline, partially commented expression starting on line %s of file %s:\n%s\nPlease clarify whether this is all a comment or should be parsed." % (origlineno, abs_fn, origline))
                s = s[:-1] + s2
            if s and s[0] == '#':
                continue
            feeder(lineno, s, abs_fn, statements, baseconfig=baseconfig)
    if not os.environ.get('BB_OPT_DISABLE_CONF_AST_CACHE'):
        _conf_statements_cache[key] = statements
    if _bb_metrics:
        _bb_metrics.miss('conf_ast')
    if _bb_metrics and _tok:
        try:
            _bb_metrics.time_end('conf_ast_parse', _tok)
        except Exception:
            pass
    return statements

# Override handle() with a version that reuses cached AST for .conf files
def handle(fn, data, include, baseconfig=False):
    init(data)
    # Set metrics output path from TMPDIR so metrics can write without extra config
    try:
        from bb.parse import metrics as _m
        _m.set_tmpdir(data.getVar('TMPDIR'))
    except Exception:
        pass

    if include == 0:
        oldfile = None
    else:
        oldfile = data.getVar('FILE', False)

    abs_fn = resolve_file(fn, data)
    statements = _get_conf_statements(abs_fn, baseconfig)

    # DONE WITH PARSING... time to evaluate
    data.setVar('FILE', abs_fn)
    _tok = None
    try:
        if _bb_metrics:
            _tok = _bb_metrics.time_start('conf_eval')
    except Exception:
        _tok = None
    statements.eval(data)
    if _bb_metrics and _tok:
        try:
            _bb_metrics.time_end('conf_eval', _tok)
        except Exception:
            pass
    if oldfile:
        data.setVar('FILE', oldfile)

    for f in confFilters:
        f(fn, data)

    try:
        return data
    finally:
        try:
            if include == 0:
                from bb.parse import metrics as _m
                _m.flush('confhandler')
        except Exception:
            pass

# baseconfig is set for the bblayers/layer.conf cookerdata config parsing
# The function is also used by BBHandler, conffile would be False
def feeder(lineno, s, fn, statements, baseconfig=False, conffile=True):
    m = __config_regexp__.match(s)
    if m:
        groupd = m.groupdict()
        if groupd['var'] == "":
            raise ParseError("Empty variable name in assignment: '%s'" % s, fn, lineno);
        if not groupd['whitespace'] or not groupd['whitespace2']:
            logger.warning("%s:%s has a lack of whitespace around the assignment: '%s'" % (fn, lineno, s))
        ast.handleData(statements, fn, lineno, groupd)
        return

    m = __include_regexp__.match(s)
    if m:
        ast.handleInclude(statements, fn, lineno, m, False)
        return

    m = __require_regexp__.match(s)
    if m:
        ast.handleInclude(statements, fn, lineno, m, True)
        return

    m = __includeall_regexp__.match(s)
    if m:
        ast.handleIncludeAll(statements, fn, lineno, m)
        return

    m = __export_regexp__.match(s)
    if m:
        ast.handleExport(statements, fn, lineno, m)
        return

    m = __unset_regexp__.match(s)
    if m:
        ast.handleUnset(statements, fn, lineno, m)
        return

    m = __unset_flag_regexp__.match(s)
    if m:
        ast.handleUnsetFlag(statements, fn, lineno, m)
        return

    m = __addpylib_regexp__.match(s)
    if baseconfig and conffile and m:
        ast.handlePyLib(statements, fn, lineno, m)
        return

    m = __addfragments_regexp__.match(s)
    if m:
        ast.handleAddFragments(statements, fn, lineno, m)
        return

    raise ParseError("unparsed line: '%s'" % s, fn, lineno);

# Add us to the handlers list
from bb.parse import handlers
handlers.append({'supports': supports, 'handle': handle, 'init': init})
del handlers
