import atexit
import json
import os
import threading
import itertools
from time import time, perf_counter

_lock = threading.Lock()
_totals = {
    'which': {'hits': 0, 'misses': 0, 'evictions': 0},
    'resolve_file': {'hits': 0, 'misses': 0, 'evictions': 0},
    'inherit': {'hits': 0, 'misses': 0, 'evictions': 0},
    'include': {'hits': 0, 'misses': 0, 'evictions': 0},
    'conf_ast': {'hits': 0, 'misses': 0, 'evictions': 0},
    'supports': {'hits': 0, 'misses': 0, 'evictions': 0},
    # Index attribution counters
    'include_index': {'hits': 0, 'misses': 0, 'evictions': 0},
    'class_index': {'hits': 0, 'misses': 0, 'evictions': 0},
    'which_dir_index': {'hits': 0, 'misses': 0, 'evictions': 0},
}

# Cumulative time (seconds) and counts per section
_times = {
    'which': {'seconds': 0.0, 'count': 0},
    'resolve_file': {'seconds': 0.0, 'count': 0},
    'inherit': {'seconds': 0.0, 'count': 0},
    'include': {'seconds': 0.0, 'count': 0},
    'conf_ast_parse': {'seconds': 0.0, 'count': 0},
    'conf_eval': {'seconds': 0.0, 'count': 0},
    'supports': {'seconds': 0.0, 'count': 0},
}

_metrics_path = None
_seq = itertools.count(1)


def set_tmpdir(tmpdir):
    global _metrics_path
    try:
        if tmpdir:
            _metrics_path = os.path.join(tmpdir, 'bb-cache-metrics.jsonl')
    except Exception:
        pass


def _bump(section, field, n=1):
    with _lock:
        try:
            _totals[section][field] += n
        except Exception:
            pass


def hit(section):
    _bump(section, 'hits', 1)


def miss(section):
    _bump(section, 'misses', 1)


def evict(section):
    _bump(section, 'evictions', 1)

def time_start(section):
    try:
        return (section, perf_counter())
    except Exception:
        return None

def time_end(section, token):
    try:
        if not token:
            return
        sec, t0 = token
        if sec != section:
            section = sec
        dt = perf_counter() - t0
        with _lock:
            ent = _times.get(section)
            if ent is None:
                ent = {'seconds': 0.0, 'count': 0}
                _times[section] = ent
            ent['seconds'] += dt
            ent['count'] += 1
    except Exception:
        pass


def flush(note=None):
    # Append cumulative totals to file; do not reset
    p = _metrics_path or os.path.join(os.environ.get('TMPDIR') or '/tmp', 'bb-cache-metrics.jsonl')
    payload = {
        'ts': time(),
        'pid': os.getpid(),
        'seq': next(_seq),
        'note': note,
    }
    with _lock:
        payload.update(_totals)
        payload['time'] = _times
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'a') as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


@atexit.register
def _on_exit():
    try:
        flush('exit')
    except Exception:
        pass
