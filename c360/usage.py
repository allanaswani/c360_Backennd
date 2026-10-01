"""Who is actually using Customer 360 - daily per-user usage, for the portfolio tool.

Customer 360 already records every API call and client navigation in ``AuditEvent``
(see middleware.py), with the username resolved from the JWT - portfolio single
sign-on users included. What nobody could see was the answer to "we trained these
people; are they using it?", because that trail lives in this app's own database and
the portfolio tool's Usage Analytics only covers the portfolio tool.

This module rolls the trail up into one row per (day, username) and the management
command ``push_usage`` posts those rows to the portfolio backend's
``observability/ingest/`` endpoint, where they are matched against the portfolio's own
user directory (branch, role) - so it can also show who has NEVER opened it.

Days are cut in Nairobi time, not the server's UTC, so "yesterday" means the bank's
yesterday. Rows are idempotent on (day, username): re-pushing a day replaces it.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

USAGE_TZ = ZoneInfo(os.environ.get('C360_USAGE_TZ', 'Africa/Nairobi'))

CUSTOMER_ROUTE = '/api/customers/<str:cust_id>/'
SEARCH_ROUTE = '/api/customers/'

# Normalised route -> feature. Checked in order; first match wins. Anything
# unmatched (auth, meta, telemetry) is plumbing, not a feature, and is not counted.
FEATURES: tuple[tuple[str, str], ...] = (
    ('/api/customers/<str:cust_id>/insights/', 'insights'),
    ('/api/customers/<str:cust_id>/relationship/', 'relationship'),
    ('/api/customers/<str:cust_id>/statement/', 'statement'),
    ('/api/customers/<str:cust_id>/recommendations/', 'recommendations'),
    ('/api/customers/<str:cust_id>/domains/hfcb/', 'core_banking'),
    ('/api/customers/<str:cust_id>/domains/', 'domains'),
    ('/api/customers/<str:cust_id>/overview/', 'overview'),
    ('/api/customers/<str:cust_id>/linked/', 'linked_parties'),
    (CUSTOMER_ROUTE, 'customer_profile'),
    (SEARCH_ROUTE, 'search'),
    ('/api/portfolio/activity-prospects/', 'activity_call_list'),
    ('/api/portfolio/maturities/', 'maturities'),
    ('/api/portfolio/worklist/', 'worklist'),
    ('/api/portfolio/overview/', 'portfolio'),
    ('/api/book/', 'my_book'),
    ('/api/property-clients/', 'property_clients'),
    ('/api/insurance-clients/', 'insurance_clients'),
    ('/api/recommendations/feedback/', 'feedback'),
    ('/api/observability/export/', 'export'),
)


def feature_for(route: str) -> str | None:
    r = route or ''
    for prefix, name in FEATURES:
        # Customer profile and search are exact routes; the rest are prefixes.
        if name in ('customer_profile', 'search'):
            if r == prefix:
                return name
        elif r.startswith(prefix):
            return name
    return None


def local_day(ts: datetime) -> date:
    return ts.astimezone(USAGE_TZ).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a Nairobi calendar day, as aware datetimes."""
    start = datetime.combine(day, time.min, tzinfo=USAGE_TZ)
    return start, start + timedelta(days=1)


def rollup(events) -> list[dict[str, Any]]:
    """Fold audit events into per-(day, username) usage rows.

    ``events`` is an iterable of objects or dicts with ts, username, kind, route,
    method, status, target, session. Events without a username are anonymous
    (sign-in screen, probes) and are skipped.
    """
    rows: dict[tuple[date, str], dict[str, Any]] = {}
    customers: dict[tuple[date, str], set] = defaultdict(set)
    sessions: dict[tuple[date, str], set] = defaultdict(set)
    minutes: dict[tuple[date, str], set] = defaultdict(set)

    def g(e, k):
        return e.get(k) if isinstance(e, dict) else getattr(e, k, None)

    for e in events:
        user = (g(e, 'username') or '').strip()
        ts = g(e, 'ts')
        if not user or ts is None:
            continue
        day = local_day(ts)
        key = (day, user.lower())
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                'day': day.isoformat(), 'username': user.lower(),
                'requests': 0, 'page_views': 0, 'customer_views': 0, 'searches': 0,
                'exports': 0, 'errors': 0, 'first_at': ts, 'last_at': ts,
                'features': defaultdict(int),
            }
        row['first_at'] = min(row['first_at'], ts)
        row['last_at'] = max(row['last_at'], ts)
        minutes[key].add(ts.replace(second=0, microsecond=0))
        sess = g(e, 'session')
        if sess:
            sessions[key].add(sess)
        kind = g(e, 'kind')
        if kind in ('page_view', 'nav'):
            row['page_views'] += 1
            continue
        if kind != 'api':
            continue
        row['requests'] += 1
        status = g(e, 'status')
        if status is not None and int(status) >= 500:
            row['errors'] += 1
        route = g(e, 'route') or ''
        ok = status is None or 200 <= int(status) < 300
        feature = feature_for(route)
        if feature and ok:
            row['features'][feature] += 1
        if route == CUSTOMER_ROUTE and ok:
            row['customer_views'] += 1
            tgt = g(e, 'target')
            if tgt:
                customers[key].add(str(tgt))
        elif route == SEARCH_ROUTE and ok:
            row['searches'] += 1
        elif feature == 'export' and ok:
            row['exports'] += 1

    out = []
    for key, row in rows.items():
        row['distinct_customers'] = len(customers[key])
        row['sessions'] = len(sessions[key])
        row['active_minutes'] = len(minutes[key])
        row['first_at'] = row['first_at'].isoformat()
        row['last_at'] = row['last_at'].isoformat()
        row['features'] = dict(row['features'])
        out.append(row)
    out.sort(key=lambda r: (r['day'], r['username']))
    return out


def rollup_days(first: date, last: date) -> list[dict[str, Any]]:
    """Usage rows for Nairobi days ``first``..``last`` inclusive, from the audit trail."""
    from .models import AuditEvent
    start, _ = day_bounds(first)
    _, end = day_bounds(last)
    qs = (AuditEvent.objects.filter(ts__gte=start, ts__lt=end)
          .exclude(username='')
          .only('ts', 'username', 'kind', 'route', 'method', 'status', 'target', 'session')
          .order_by())
    return rollup(qs.iterator(chunk_size=5000))
