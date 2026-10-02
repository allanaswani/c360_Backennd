"""Run a page's independent warehouse reads side by side.

One long-lived pool per worker process (as in services/insights.py): its threads are
reused, so each keeps its own Trino and Postgres connection across requests (both
connectors are per-thread). Only request threads submit work here - nothing running
on the pool submits more - so a busy pool queues, it never deadlocks.

The customer page and its Overview and HFCB tabs each made ten or more reads one
after another; on a slow day that was long enough for the page to time out
(2026-10-02). Side by side, a page takes about as long as its slowest read.
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='c360-read')


def quiet(fn, *args):
    """Call ``fn``; None on any failure. For reads the page can do without."""
    try:
        return fn(*args)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning('read %s failed: %s', getattr(fn, '__name__', fn), exc)
        return None


def submit(fn, *args, **kwargs) -> Future:
    """A read the page cannot do without: ``.result()`` re-raises its error."""
    return POOL.submit(fn, *args, **kwargs)


def optional(calls: dict[str, tuple]) -> dict[str, Future]:
    """Start ``{key: (fn, *args)}`` reads that may fail; each result is None if so."""
    return {k: POOL.submit(quiet, *call) for k, call in calls.items()}


def gather(futures: dict[str, Future]) -> dict[str, Any]:
    return {k: f.result() for k, f in futures.items()}
