"""In-process observability core — the performance-critical machinery behind the ops
dashboard, the audit trail and the Prometheus ``/metrics`` endpoint.

Design (the whole point is that monitoring must not become the bottleneck):

* **Metrics** aggregate in memory into per-minute buckets — O(1) per request, *zero*
  DB writes on the request hot path. Latencies are kept as a bounded reservoir sample
  (so a hot minute uses fixed memory), from which p50/p95/p99 are computed at flush.
* A **background flusher** (one daemon thread per worker) persists finished minutes to
  ``MetricMinute`` and bulk-inserts buffered ``AuditEvent`` rows every few seconds, and
  prunes both tables to a bounded retention window. It also writes zero-count heartbeat
  minutes so a *gap* in the series is real downtime, not just an idle process.
* **Audit** events are appended to an in-memory ring and drained by the same flusher, so
  recording an action never blocks the user. A hard cap drops (and counts) overflow
  rather than growing without bound.

Everything here is per-process; the read API aggregates across workers via the DB.
"""
from __future__ import annotations

import os
import random
import socket
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone as _tz
from typing import Any

# This worker's identity, so multi-worker rollups don't collide (host:pid).
INSTANCE = f'{socket.gethostname()[:24]}:{os.getpid()}'

_RESERVOIR = 4096          # max latency samples kept per minute (reservoir-sampled)
_RECENT_MINUTES = 360      # finalised minutes kept in memory for the live dashboard (~6h)
_AUDIT_CAP = 10000         # hard cap on the in-memory audit buffer (overflow is dropped+counted)


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def percentile(sorted_vals: list[int], q: float) -> int:
    """Nearest-rank percentile of an already-sorted list. Empty -> 0."""
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return int(sorted_vals[0])
    k = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return int(sorted_vals[k])


class _Bucket:
    __slots__ = ('count', 'errors', 'client_errors', 'sum_ms', 'max_ms', 'by_status', 'lat', '_seen')

    def __init__(self) -> None:
        self.count = 0
        self.errors = 0
        self.client_errors = 0
        self.sum_ms = 0
        self.max_ms = 0
        self.by_status: dict[str, int] = defaultdict(int)
        self.lat: list[int] = []
        self._seen = 0

    def add(self, status: int, dur_ms: int) -> None:
        self.count += 1
        self.sum_ms += dur_ms
        if dur_ms > self.max_ms:
            self.max_ms = dur_ms
        self.by_status[f'{status // 100}xx'] += 1
        if status >= 500:
            self.errors += 1
        elif status >= 400:
            self.client_errors += 1
        # Reservoir sample so a very hot minute keeps fixed memory but a fair latency spread.
        self._seen += 1
        if len(self.lat) < _RESERVOIR:
            self.lat.append(dur_ms)
        else:
            j = random.randint(0, self._seen - 1)
            if j < _RESERVOIR:
                self.lat[j] = dur_ms

    def rollup(self, minute: datetime) -> dict[str, Any]:
        s = sorted(self.lat)
        return {
            'minute': minute,
            'instance': INSTANCE,
            'count': self.count,
            'errors': self.errors,
            'client_errors': self.client_errors,
            'sum_ms': self.sum_ms,
            'p50_ms': percentile(s, 0.50),
            'p95_ms': percentile(s, 0.95),
            'p99_ms': percentile(s, 0.99),
            'max_ms': self.max_ms,
            'by_status': dict(self.by_status),
        }


class _Collector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[datetime, _Bucket] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_MINUTES)
        self._started = time.time()
        self._total = 0

    def record(self, dur_ms: float, status: int) -> None:
        now = _minute(datetime.now(_tz.utc))
        d = int(dur_ms)
        with self._lock:
            b = self._buckets.get(now)
            if b is None:
                b = self._buckets[now] = _Bucket()
            b.add(status, d)
            self._total += 1

    def roll(self) -> list[dict[str, Any]]:
        """Finalise every bucket strictly older than the current minute; return their
        rollups (also cached in the recent ring for the live view)."""
        cur = _minute(datetime.now(_tz.utc))
        out: list[dict[str, Any]] = []
        with self._lock:
            for mk in sorted(k for k in self._buckets if k < cur):
                r = self._buckets.pop(mk).rollup(mk)
                out.append(r)
                self._recent.append(r)
        return out

    def live(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None, float, int]:
        cur = _minute(datetime.now(_tz.utc))
        with self._lock:
            partial = self._buckets.get(cur)
            return list(self._recent), (partial.rollup(cur) if partial else None), self._started, self._total


class _AuditBuffer:
    def __init__(self, cap: int = _AUDIT_CAP) -> None:
        self._lock = threading.Lock()
        self._q: list[dict[str, Any]] = []
        self._cap = cap
        self._dropped = 0

    def record(self, **event: Any) -> None:
        with self._lock:
            if len(self._q) >= self._cap:
                self._dropped += 1
                return
            self._q.append(event)

    def drain(self) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            items, self._q = self._q, []
            dropped, self._dropped = self._dropped, 0
        return items, dropped


# Module-level singletons — one collector + buffer per worker process.
collector = _Collector()
audit_buffer = _AuditBuffer()


# --------------------------------------------------------------------------- #
# Prometheus text exposition (so real Prometheus/Grafana can scrape this later).
# --------------------------------------------------------------------------- #
def prometheus_text() -> str:
    recent, partial, started, total = collector.live()
    mins = recent + ([partial] if partial else [])
    reqs = sum(m['count'] for m in mins)
    errs = sum(m['errors'] for m in mins)
    p95 = max((m['p95_ms'] for m in mins), default=0)
    up = int(time.time() - started)
    lines = [
        '# HELP c360_requests_total Requests observed since process start.',
        '# TYPE c360_requests_total counter',
        f'c360_requests_total{{instance="{INSTANCE}"}} {total}',
        '# HELP c360_requests_recent Requests in the in-memory window.',
        '# TYPE c360_requests_recent gauge',
        f'c360_requests_recent{{instance="{INSTANCE}"}} {reqs}',
        '# HELP c360_errors_recent 5xx responses in the in-memory window.',
        '# TYPE c360_errors_recent gauge',
        f'c360_errors_recent{{instance="{INSTANCE}"}} {errs}',
        '# HELP c360_latency_p95_ms Worst per-minute p95 latency in the window.',
        '# TYPE c360_latency_p95_ms gauge',
        f'c360_latency_p95_ms{{instance="{INSTANCE}"}} {p95}',
        '# HELP c360_process_uptime_seconds Seconds since this worker started.',
        '# TYPE c360_process_uptime_seconds gauge',
        f'c360_process_uptime_seconds{{instance="{INSTANCE}"}} {up}',
        '# HELP c360_up Always 1 when scraped (liveness).',
        '# TYPE c360_up gauge',
        f'c360_up{{instance="{INSTANCE}"}} 1',
    ]
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- #
# Background flusher — persists metrics + audit, writes heartbeats, prunes.
# --------------------------------------------------------------------------- #
_flusher_started = False
_flusher_lock = threading.Lock()


def _flush_once(retain_metric_days: int, retain_audit_days: int, prune: bool) -> None:
    from django.db import connections
    from .models import AppHeartbeat, AuditEvent, MetricMinute

    # 0) heartbeat: record that the app was alive, independently of whether anybody
    #    happened to call it. Uptime is measured from these; deriving it from request
    #    traffic reported a quiet instance as being down (see AppHeartbeat).
    #    Both the current and previous minute are stamped so a flush interval longer
    #    than a minute, or a tick that lands late, cannot punch a false gap.
    now = datetime.now(_tz.utc)
    beats = [AppHeartbeat(minute=_minute(now)),
             AppHeartbeat(minute=_minute(now) - timedelta(minutes=1))]
    AppHeartbeat.objects.bulk_create(beats, ignore_conflicts=True)

    # 1) metrics: persist finished minutes for this instance.
    rolled = collector.roll()
    rows = [MetricMinute(**r) for r in rolled]
    if rows:
        MetricMinute.objects.bulk_create(rows, ignore_conflicts=True)

    # 2) audit: bulk-insert whatever buffered up.
    events, dropped = audit_buffer.drain()
    if events:
        AuditEvent.objects.bulk_create([AuditEvent(**e) for e in events], batch_size=500)
    if dropped:
        # Record that we shed load, so the gap is visible rather than silent.
        AuditEvent.objects.create(kind='audit', route='(overflow)',
                                  meta={'dropped': dropped, 'instance': INSTANCE})

    # 3) retention prune (cheap; only when asked, e.g. every Nth tick).
    if prune:
        MetricMinute.objects.filter(minute__lt=now - timedelta(days=retain_metric_days)).delete()
        AuditEvent.objects.filter(ts__lt=now - timedelta(days=retain_audit_days)).delete()
        AppHeartbeat.objects.filter(minute__lt=now - timedelta(days=retain_metric_days)).delete()

    for c in connections.all():
        c.close_if_unusable_or_obsolete()


def start_flusher() -> None:
    """Start the single background flush thread for this process. Idempotent."""
    global _flusher_started
    with _flusher_lock:
        if _flusher_started:
            return
        _flusher_started = True

    from django.conf import settings
    interval = int(getattr(settings, 'OBSERVABILITY_FLUSH_SECONDS', 20))
    retain_metric_days = int(getattr(settings, 'OBSERVABILITY_METRIC_RETENTION_DAYS', 30))
    retain_audit_days = int(getattr(settings, 'OBSERVABILITY_AUDIT_RETENTION_DAYS', 90))

    def _loop() -> None:
        tick = 0
        while True:
            time.sleep(interval)
            tick += 1
            try:
                _flush_once(retain_metric_days, retain_audit_days, prune=(tick % 60 == 0))
            except Exception:
                pass  # never let a telemetry hiccup kill the thread

    threading.Thread(target=_loop, name='c360-observability-flusher', daemon=True).start()
