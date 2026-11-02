"""RM alerts for the screen and the morning email (rules in c360/alerts.py).

The book-wide feed is read once an hour and cached; each caller then sees the part of
it in their own book, so a hundred RMs opening the page cost one warehouse read.
"""
from __future__ import annotations

from typing import Any

from .. import alerts as rules
from ..warehouse.factory import data_mode
from ..warehouse.gateway import WarehouseGateway
from . import portfolio_cache

TTL_SECONDS = 3600


def _build(gateway: WarehouseGateway) -> dict[str, Any]:
    feed = gateway.get_alert_feed()
    if not feed:
        return {'available': False,
                'detail': 'Alerts need the live warehouse, which is not available in preview mode.'}
    items = rules.build(arrears=feed['arrears'], drops=feed['drops'], maturities=feed['maturities'],
                        loan_as_of=feed['loan_as_of'], deposit_as_of=feed['deposit_as_of'])
    rm = feed.get('rm') or {}
    for a in items:
        cur = rm.get(a['cust_id']) or {}
        a['rm_code'], a['rm_name'] = cur.get('code'), cur.get('name')
    return {'available': True, 'loan_as_of': feed['loan_as_of'],
            'deposit_as_of': feed['deposit_as_of'], 'alerts': items}


def book_alerts(gateway: WarehouseGateway) -> tuple[dict[str, Any], dict]:
    """The whole book's alerts (cached) and the cache freshness."""
    key = f'alerts:{data_mode()}:{gateway.as_of_date().isoformat()}'
    return portfolio_cache.get_or_build(key, lambda: _build(gateway), ttl=TTL_SECONDS)


def in_book(payload: dict[str, Any], sales_codes: list[str] | None) -> dict[str, Any]:
    if not payload.get('available'):
        return payload
    items = rules.for_codes(payload['alerts'], sales_codes)
    unassigned = (sum(1 for a in payload['alerts'] if not a.get('rm_code'))
                  if sales_codes is None else None)
    return {**payload, 'alerts': items, 'summary': rules.summary(items),
            'whole_book': sales_codes is None, 'unassigned': unassigned}
