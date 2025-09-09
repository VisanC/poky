"""
   class for handling .bb files

   Reads a .bb file and obtains its metadata

"""


#  Copyright (C) 2003, 2004  Chris Larson
#  Copyright (C) 2003, 2004  Phil Blundell
#
# SPDX-License-Identifier: GPL-2.0-only
#

import re, bb, os
import bb.build, bb.utils, bb.data_smart

from . import ConfHandler
from .. import resolve_file, ast, logger, ParseError
from .ConfHandler import include, init

__func_start_regexp__    = re.compile(r"(((?P<py>python(?=(\s|\()))|(?P<fr>fakeroot(?=\s)))\s*)*(?P<func>[\w\.\-\+\{\}\$:]+)?\s*\(\s*\)\s*{$" )
__inherit_regexp__       = re.compile(r"inherit\s+(.+)" )
__inherit_def_regexp__   = re.compile(r"inherit_defer\s+(.+)" )
__export_func_regexp__   = re.compile(r"EXPORT_FUNCTIONS\s+(.+)" )
__addtask_regexp__       = re.compile(r"addtask\s+([^#\n]+)(?P<comment>#.*|.*?)")
__deltask_regexp__       = re.compile(r"deltask\s+([^#\n]+)(?P<comment>#.*|.*?)")
__addhandler_regexp__    = re.compile(r"addhandler\s+(.+)" )
__def_regexp__           = re.compile(r"def\s+(\w+).*:" )
__python_func_regexp__   = re.compile(r"(\s+.*)|(^$)|(^#)" )
__python_tab_regexp__    = re.compile(r" *\t")

__infunc__ = []
__inpython__ = False
__body__   = []
__classname__ = ""
__residue__ = []

cached_statements = {}
try:
    from bb.parse import metrics as _bb_metrics
except Exception:
    _bb_metrics = None
_inherit_resolved_cache = {}
_inherit_resolved_order = []
_inherit_resolved_max = 8192

# Class name → absolute path index per (BBPATH, classtype) to avoid repeated which() scans
_class_index_cache = {}
_class_index_order = []
_class_index_max = 128

def _bbpath_dirs_for_classes(bbpath, classtype):
    dirs = []
    for p in (bbpath or '').split(':'):
        if not p:
            continue
        for t in ["classes-" + str(classtype), "classes"]:
            d = os.path.join(p, t)
            if os.path.isdir(d):
                dirs.append(d)
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

def _build_class_index(bbpath, classtype):
    dirs = _bbpath_dirs_for_classes(bbpath, classtype)
    mapping = {}
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for de in it:
                    name = de.name
                    if not name.endswith('.bbclass'):
                        continue
                    cls = name[:-8]
                    # Preserve first match according to directory order
                    if cls not in mapping:
                        mapping[cls] = os.path.join(d, name)
        except OSError:
            continue
    return _dirs_fingerprint(dirs), mapping

def _get_class_index(bbpath, classtype):
    key = (str(classtype), bbpath or '')
    cached = _class_index_cache.get(key)
    dirs = _bbpath_dirs_for_classes(bbpath, classtype)
    fp = _dirs_fingerprint(dirs)
    if cached is not None:
        cfp, cmap = cached
        if cfp == fp:
            # Refresh LRU
            try:
                _class_index_order.remove(key)
            except ValueError:
                pass
            _class_index_order.append(key)
            return cmap
    # (Re)build
    fp, cmap = _build_class_index(bbpath, classtype)
    _class_index_cache[key] = (fp, cmap)
    _class_index_order.append(key)
    if len(_class_index_order) > _class_index_max:
        old = _class_index_order.pop(0)
        _class_index_cache.pop(old, None)
    return cmap

def _inherit_cache_get(key):
    try:
        _inherit_resolved_order.remove(key)
        _inherit_resolved_order.append(key)
        val = _inherit_resolved_cache[key]
        if _bb_metrics:
            _bb_metrics.hit('inherit')
        return val
    except (ValueError, KeyError):
        if _bb_metrics:
            _bb_metrics.miss('inherit')
        return None

def _inherit_cache_put(key, value):
    _inherit_resolved_cache[key] = value
    _inherit_resolved_order.append(key)
    if len(_inherit_resolved_order) > _inherit_resolved_max:
        old = _inherit_resolved_order.pop(0)
        _inherit_resolved_cache.pop(old, None)
        if _bb_metrics:
            _bb_metrics.evict('inherit')

def _resolve_inherit_file(d, origfile):
    """Resolve an inherit target to an absolute file path and attempts list.
    Returns (resolved_path, attempts). resolved_path may be None if not found.
    Attempts is a tuple of candidate paths checked (for dependency marking).
    """
    m = _bb_metrics
    _tok = None
    if m:
        try:
            _tok = m.time_start('inherit')
        except Exception:
            _tok = None
    try:
        classtype = d.getVar("__bbclasstype", False)
        bbpath = d.getVar("BBPATH")
        key = (origfile, classtype, bbpath)

        if not os.path.isabs(origfile) and not origfile.endswith(".bbclass"):
            cached = _inherit_cache_get(key)
            if cached is not None:
                return cached

            attempts_accum = []
            resolved = None
            # If the class reference contains subdirectories, fall back to path-based resolution
            if '/' in origfile:
                for t in ["classes-" + str(classtype), "classes"]:
                    cand = os.path.join(t, '%s.bbclass' % origfile)
                    abs_fn, attempts = bb.utils.which(bbpath, cand, history=True)
                    attempts_accum.extend(attempts)
                    if abs_fn:
                        resolved = abs_fn
                        break
            else:
                # Use class index to map class name to path in O(1)
                if os.environ.get('BB_OPT_DISABLE_CLASS_INDEX'):
                    # Simulate miss by falling back to which()
                    for t in ["classes-" + str(classtype), "classes"]:
                        cand = os.path.join(t, '%s.bbclass' % origfile)
                        abs_fn, attempts = bb.utils.which(bbpath, cand, history=True)
                        attempts_accum.extend(attempts)
                        if abs_fn:
                            resolved = abs_fn
                            break
                    if _bb_metrics:
                        _bb_metrics.miss('class_index')
                else:
                    cmap = _get_class_index(bbpath, classtype)
                    resolved = cmap.get(origfile)
                    if _bb_metrics:
                        # Count any lookup through index as a hit regardless of found status
                        if resolved:
                            _bb_metrics.hit('class_index')
                        else:
                            _bb_metrics.miss('class_index')
                # Generate attempts list in search order for dependency marking
                for p in (bbpath or '').split(':'):
                    if not p:
                        continue
                    for t in ["classes-" + str(classtype), "classes"]:
                        cand = os.path.abspath(os.path.join(p, t, '%s.bbclass' % origfile))
                        attempts_accum.append(cand)

            val = (resolved, tuple(attempts_accum))
            _inherit_cache_put(key, val)
            return val

        # Absolute or already a .bbclass - check existence only, no BBPATH search
        return (origfile if os.path.exists(origfile) else None, tuple())
    finally:
        if m and _tok:
            try:
                m.time_end('inherit', _tok)
            except Exception:
                pass

def supports(fn, d):
    """Return True if fn has a supported extension"""
    return os.path.splitext(fn)[-1] in [".bb", ".bbclass", ".inc"]

def inherit_defer(expression, fn, lineno, d):
    inherit = (expression, fn, lineno)
    inherits = d.getVar('__BBDEFINHERITS', False) or []
    inherits.append(inherit)
    d.setVar('__BBDEFINHERITS', inherits)

def inherit(files, fn, lineno, d, deferred=False):
    __inherit_cache = d.getVar('__inherit_cache', False) or []
    #if "${" in files and not deferred:
    #    bb.warn("%s:%s has non deferred conditional inherit" % (fn, lineno))
    files = d.expand(files).split()
    for file in files:
        defer = (d.getVar("BB_DEFER_BBCLASSES") or "").split()
        if not deferred and file in defer:
            inherit_defer(file, fn, lineno, d)
            continue
        origfile = file
        resolved, attempts = _resolve_inherit_file(d, origfile)
        for af in attempts:
            if af != resolved:
                bb.parse.mark_dependency(d, af)

        file = resolved

        if not file or not os.path.exists(file):
            raise ParseError("Could not inherit file %s" % (file), fn, lineno)

        if not file in __inherit_cache:
            logger.debug("Inheriting %s (from %s:%d)" % (file, fn, lineno))
            __inherit_cache.append( file )
            d.setVar('__inherit_cache', __inherit_cache)
            try:
                bb.parse.handle(file, d, True)
            except (IOError, OSError) as exc:
                raise ParseError("Could not inherit file %s: %s" % (fn, exc.strerror), fn, lineno)
            __inherit_cache = d.getVar('__inherit_cache', False) or []

def get_statements(filename, absolute_filename, base_name):
    global cached_statements, __residue__, __body__

    try:
        return cached_statements[absolute_filename]
    except KeyError:
        with open(absolute_filename, 'r') as f:
            statements = ast.StatementGroup()

            lineno = 0
            for lineno, line in enumerate(f, start=1):
                s = line.rstrip()
                feeder(lineno, s, filename, base_name, statements)

        if __inpython__:
            # add a blank line to close out any python definition
            feeder(lineno, "", filename, base_name, statements, eof=True)

        if __residue__:
            raise ParseError("Unparsed lines %s: %s" % (filename, str(__residue__)), filename, lineno)
        if __body__:
            raise ParseError("Unparsed lines from unclosed function %s: %s" % (filename, str(__body__)), filename, lineno)

        if filename.endswith(".bbclass") or filename.endswith(".inc"):
            cached_statements[absolute_filename] = statements
        return statements

def handle(fn, d, include, baseconfig=False):
    global __infunc__, __body__, __residue__, __classname__
    __body__ = []
    __infunc__ = []
    __classname__ = ""
    __residue__ = []

    base_name = os.path.basename(fn)
    (root, ext) = os.path.splitext(base_name)
    init(d)
    # Tell metrics where TMPDIR is so it can write its file
    try:
        from bb.parse import metrics as _m
        _m.set_tmpdir(d.getVar('TMPDIR'))
    except Exception:
        pass

    if ext == ".bbclass":
        __classname__ = root
        __inherit_cache = d.getVar('__inherit_cache', False) or []
        if not fn in __inherit_cache:
            __inherit_cache.append(fn)
            d.setVar('__inherit_cache', __inherit_cache)

    if include != 0:
        oldfile = d.getVar('FILE', False)
    else:
        oldfile = None

    abs_fn = resolve_file(fn, d)

    # actual loading
    statements = get_statements(fn, abs_fn, base_name)

    # DONE WITH PARSING... time to evaluate
    if ext != ".bbclass" and abs_fn != oldfile:
        d.setVar('FILE', abs_fn)

    try:
        statements.eval(d)
    except bb.parse.SkipRecipe:
        d.setVar("__SKIPPED", True)
        if include == 0:
            try:
                from bb.parse import metrics as _m
                _m.flush('bbhandler')
            except Exception:
                pass
            return { "" : d }

    if __infunc__:
        raise ParseError("Shell function %s is never closed" % __infunc__[0], __infunc__[1], __infunc__[2])
    if __residue__:
        raise ParseError("Leftover unparsed (incomplete?) data %s from %s" % __residue__, fn)

    if ext != ".bbclass" and include == 0:
        try:
            from bb.parse import metrics as _m
            _m.flush('bbhandler')
        except Exception:
            pass
        return ast.multi_finalize(fn, d)

    if ext != ".bbclass" and oldfile and abs_fn != oldfile:
        d.setVar("FILE", oldfile)

    return d

def feeder(lineno, s, fn, root, statements, eof=False):
    global __inpython__, __infunc__, __body__, __residue__, __classname__

    # Check tabs in python functions:
    # - def py_funcname(): covered by __inpython__
    # - python(): covered by '__anonymous' == __infunc__[0]
    # - python funcname(): covered by __infunc__[3]
    if __inpython__ or (__infunc__ and ('__anonymous' == __infunc__[0] or __infunc__[3])):
        tab = __python_tab_regexp__.match(s)
        if tab:
            bb.warn('python should use 4 spaces indentation, but found tabs in %s, line %s' % (root, lineno))

    if __infunc__:
        if s == '}':
            __body__.append('')
            ast.handleMethod(statements, fn, lineno, __infunc__[0], __body__, __infunc__[3], __infunc__[4])
            __infunc__ = []
            __body__ = []
        else:
            __body__.append(s)
        return

    if __inpython__:
        m = __python_func_regexp__.match(s)
        if m and not eof:
            __body__.append(s)
            return
        else:
            ast.handlePythonMethod(statements, fn, lineno, __inpython__,
                                   root, __body__)
            __body__ = []
            __inpython__ = False

            if eof:
                return

    if s and s[0] == '#':
        if len(__residue__) != 0 and __residue__[0][0] != "#":
            bb.fatal("There is a comment on line %s of file %s:\n'''\n%s\n'''\nwhich is in the middle of a multiline expression. This syntax is invalid, please correct it." % (lineno, fn, s))

    if len(__residue__) != 0 and __residue__[0][0] == "#" and (not s or s[0] != "#"):
        bb.fatal("There is a confusing multiline partially commented expression on line %s of file %s:\n%s\nPlease clarify whether this is all a comment or should be parsed." % (lineno - len(__residue__), fn, "\n".join(__residue__)))

    if s and s[-1] == '\\':
        __residue__.append(s[:-1])
        return

    s = "".join(__residue__) + s
    __residue__ = []

    # Skip empty lines
    if s == '':
        return   

    # Skip comments
    if s[0] == '#':
        return

    m = __func_start_regexp__.match(s)
    if m:
        __infunc__ = [m.group("func") or "__anonymous", fn, lineno, m.group("py") is not None, m.group("fr") is not None]
        return

    m = __def_regexp__.match(s)
    if m:
        __body__.append(s)
        __inpython__ = m.group(1)

        return

    m = __export_func_regexp__.match(s)
    if m:
        ast.handleExportFuncs(statements, fn, lineno, m, __classname__)
        return

    m = __addtask_regexp__.match(s)
    if m:
        after = ""
        before = ""

        # This code splits on 'before' and 'after' instead of on whitespace so we can defer
        # evaluation to as late as possible.
        tasks = m.group(1).split(" before ")[0].split(" after ")[0]

        for exp in m.group(1).split(" before "):
            exp2 = exp.split(" after ")
            if len(exp2) > 1:
                after = after + " ".join(exp2[1:])

        for exp in m.group(1).split(" after "):
            exp2 = exp.split(" before ")
            if len(exp2) > 1:
                before = before + " ".join(exp2[1:])

        # Check and warn for having task with a keyword as part of task name
        taskexpression = s.split()
        for te in taskexpression:
            if any( ( "%s_" % keyword ) in te for keyword in bb.data_smart.__setvar_keyword__ ):
                raise ParseError("Task name '%s' contains a keyword which is not recommended/supported.\nPlease rename the task not to include the keyword.\n%s" % (te, ("\n".join(map(str, bb.data_smart.__setvar_keyword__)))), fn)

        if tasks is not None:
            ast.handleAddTask(statements, fn, lineno, tasks, before, after)
        return

    m = __deltask_regexp__.match(s)
    if m:
        task = m.group(1)
        if task is not None:
            ast.handleDelTask(statements, fn, lineno, task)
        return

    m = __addhandler_regexp__.match(s)
    if m:
        ast.handleBBHandlers(statements, fn, lineno, m)
        return

    m = __inherit_regexp__.match(s)
    if m:
        ast.handleInherit(statements, fn, lineno, m)
        return

    m = __inherit_def_regexp__.match(s)
    if m:
        ast.handleInheritDeferred(statements, fn, lineno, m)
        return

    return ConfHandler.feeder(lineno, s, fn, statements, conffile=False)

# Add us to the handlers list
from .. import handlers
handlers.append({'supports': supports, 'handle': handle, 'init': init})
del handlers
