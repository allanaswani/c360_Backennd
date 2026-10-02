"""Is this deployment set up the way the app needs? Checks that need no warehouse.

Every one of these broke Customer 360 on 2026-10-01 and none of them showed on the
health page, because the page only asked the warehouse:

* the server ran a backend built before the code it was meant to run (sections
  showed "could not be loaded" while the warehouse was fine);
* ``DJANGO_ALLOWED_HOSTS`` had lost the two internal names the web container uses
  to reach the backend, so every LAN request got a 400 and LAN users were sent back
  to the login page;
* the portfolio sign-in and monitoring feed depend on settings that are easy to
  drop when an env file is rewritten.

``checks()`` returns rows in the same shape as the warehouse checks, so the health
page, the portfolio push (push_monitoring) and the alerts all read them the same way.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings

GROUP = 'Deployment'
#: The names the web container reaches the backend by (see DEPLOY docs, 24 Aug and
#: 1 Oct 2026). Without them Django answers 400 to every proxied request.
PROXY_HOSTS = ('172.17.0.1', 'host.docker.internal')


def build_info() -> dict[str, Any]:
    """The code this backend was built from, and when the image was built.

    ``C360_BUILD`` is stamped in at build time (Dockerfile ``ARG``); .git is not in
    the image, so without it the commit is unknown. The build time is the time
    ``collectstatic`` wrote STATIC_ROOT, which happens once, during ``docker build``.
    """
    commit = (os.environ.get('C360_BUILD') or '').strip() or None
    built_at = None
    root = getattr(settings, 'STATIC_ROOT', None)
    try:
        if root and Path(root).exists():
            built_at = datetime.fromtimestamp(Path(root).stat().st_mtime, tz=dt_timezone.utc).isoformat()
    except OSError:
        built_at = None
    return {'commit': commit if commit and commit != 'unknown' else None, 'built_at': built_at}


def _row(key: str, label: str, table: str, status: str, detail: str, value: Any = None) -> dict:
    return {'key': key, 'label': label, 'group': GROUP, 'table': table,
            'status': status, 'value': value, 'detail': detail}


def checks() -> list[dict[str, Any]]:
    out = []

    b = build_info()
    when = (b['built_at'] or '')[:16].replace('T', ' ')
    if b['commit']:
        out.append(_row('build', 'Backend build', 'image', 'ok',
                        f"code {b['commit']}" + (f', image built {when} UTC' if when else '')))
    else:
        out.append(_row('build', 'Backend build', 'image', 'unknown',
                        'code version not stamped (build with --build-arg C360_BUILD=$(git rev-parse --short HEAD))'
                        + (f'; image built {when} UTC' if when else '')))

    hosts = [h.strip() for h in getattr(settings, 'ALLOWED_HOSTS', [])]
    missing = [h for h in PROXY_HOSTS if h not in hosts and '*' not in hosts]
    if missing:
        out.append(_row('lan_proxy_hosts', 'LAN access (proxy hosts)', 'DJANGO_ALLOWED_HOSTS', 'error',
                        f"missing {', '.join(missing)}: every request through the web container is refused (400) "
                        f"and LAN users are sent back to the login page. Add them to /etc/hf/c360.env and "
                        f"recreate the backend container.", 0))
    else:
        out.append(_row('lan_proxy_hosts', 'LAN access (proxy hosts)', 'DJANGO_ALLOWED_HOSTS', 'ok',
                        'the web container can reach the backend', 1))

    if (os.environ.get('C360_JWT_SIGNING_KEY') or '').strip():
        out.append(_row('portfolio_key', 'Portfolio sign-in key', 'C360_JWT_SIGNING_KEY', 'ok',
                        'set (it must equal the portfolio backend SECRET_KEY)', 1))
    else:
        out.append(_row('portfolio_key', 'Portfolio sign-in key', 'C360_JWT_SIGNING_KEY', 'error',
                        'not set: sign-ins handed over from the portfolio cannot be verified', 0))

    url = (os.environ.get('C360_PORTFOLIO_INGEST_URL') or '').strip()
    token = (os.environ.get('C360_PORTFOLIO_INGEST_TOKEN') or '').strip()
    if url and token:
        out.append(_row('portfolio_feed', 'Portfolio monitoring feed', 'C360_PORTFOLIO_INGEST_*', 'ok',
                        'usage, health and audit are pushed to the portfolio', 1))
    else:
        out.append(_row('portfolio_feed', 'Portfolio monitoring feed', 'C360_PORTFOLIO_INGEST_*', 'empty',
                        'not configured: the portfolio cannot see Customer 360 usage, health or audit', 0))
    return out + service_checks()


#: Server errors in the last hour before the row is an error (and alerts). One or two
#: is a bad request somewhere; five in an hour is something broken for users.
ERRORS_ALERT_AT = 5


def service_checks() -> list[dict[str, Any]]:
    """Is the app answering users without failing? From the request log it already
    keeps (AuditEvent), so this needs no warehouse either."""
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from .models import AuditEvent

    base = {'key': 'server_errors', 'label': 'Server errors (last hour)', 'group': 'Service',
            'table': 'c360_audit_event'}
    try:
        since = timezone.now() - timedelta(hours=1)
        qs = AuditEvent.objects.filter(kind=AuditEvent.KIND_API, ts__gte=since, status__gte=500)
        n = qs.count()
        top = list(qs.values('route').annotate(c=Count('id')).order_by('-c')[:3])
    except Exception as exc:                                       # noqa: BLE001
        return [{**base, 'status': 'unknown', 'value': None,
                 'detail': f'request log could not be read: {type(exc).__name__}'}]
    if n == 0:
        return [{**base, 'status': 'ok', 'value': 0, 'detail': 'no failed requests in the last hour'}]
    where = ', '.join(f"{t['route']} ({t['c']})" for t in top)
    status = 'error' if n >= ERRORS_ALERT_AT else 'warn'
    return [{**base, 'status': status, 'value': n,
             'detail': f'{n} request{"s" if n != 1 else ""} failed with a server error: {where}'}]
