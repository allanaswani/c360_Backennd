"""Customer insights - the blocks under "What they hold, owe, earn us and do".

Five independent blocks, each read from its own source and each allowed to fail on
its own: a slow or empty table must never blank the others, and "could not load"
must never read as "has none". Every block therefore carries a status:

* ``live``        - read from the warehouse today.
* ``none``        - the source answered, and this customer has nothing in it.
* ``unavailable`` - the source could not be read (error / not wired in this mode).

Blocks: products (current / savings / fixed deposit … and loan types), facilities
(sanctioned limit vs outstanding, plus the mobile-loan history), activity (90 days of
transactions by purpose and the opportunities they point to), profile (AML risk,
income band, … and cards) and revenue (what the bank earned this year).

Results are cached per customer for ten minutes in a bounded in-process cache, so
the recommendation panel can reuse the activity read instead of repeating it.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from threading import Lock
from typing import Any

from .. import activity as activity_rules
from ..warehouse.gateway import WarehouseGateway

logger = logging.getLogger('c360')

_TTL_SECONDS = 600
_MAX_ENTRIES = 500
_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_lock = Lock()


def _cached(key: str) -> dict | None:
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL_SECONDS:
            _cache.move_to_end(key)
            return hit[1]
        if hit:
            _cache.pop(key, None)
    return None


def _store(key: str, value: dict) -> None:
    with _lock:
        _cache[key] = (time.time(), value)
        _cache.move_to_end(key)
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _block(fn, *args) -> dict[str, Any]:
    try:
        data = fn(*args)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning('insights block %s failed: %s', getattr(fn, '__name__', fn), exc)
        return {'status': 'unavailable', 'data': None}
    if data is None:
        return {'status': 'none', 'data': None}
    return {'status': 'live', 'data': data}


def _is_individual(customer: dict) -> bool | None:
    ctype = ((customer.get('bio') or {}).get('customer_type') or '').strip().lower()
    if ctype == 'individual':
        return True
    if ctype in ('organisation', 'correspondent / internal'):
        return False
    return None


def _activity_block(gateway: WarehouseGateway, customer: dict, cust_id: str,
                    products: dict, facilities: dict) -> dict[str, Any]:
    act = _block(gateway.get_activity, cust_id)
    if act['status'] != 'live':
        return act
    mix = products.get('data') if products.get('status') == 'live' else None
    try:
        card = gateway.has_active_card(cust_id)
    except Exception:                                          # noqa: BLE001
        card = None
    prof = act['data']['profile']
    categories = [{'key': k, 'label': activity_rules.CATEGORY_LABELS[k],
                   'count': prof[k]['count'], 'value': prof[k]['value']}
                  for k in activity_rules.CATEGORIES if prof[k]['count'] > 0]
    # Opportunities need to know what is already held; without the product mix they
    # would pitch products the customer has, so they abstain instead.
    held = set(mix.get('held_keys') or []) if mix is not None else set()
    # A mobile loan repaid last week still makes them a mobile borrower. If the loan
    # history could not be read, the Whizz-loan rule cannot know, so it is held back.
    fac_ok = facilities.get('status') in ('live', 'none')
    mob = (facilities.get('data') or {}).get('mobile') if fac_ok else None
    if not fac_ok or activity_rules.recent_mobile_borrower(mob, gateway.as_of_date()):
        held.add('mobile_loan')
    opps = (activity_rules.opportunities(
                prof, held=held,
                liquid_balance=float(mix.get('liquid_balance') or 0),
                has_active_card=card, individual=_is_individual(customer),
                segment=customer.get('segment'))
            if mix is not None else [])
    act['data'] = {
        'from': act['data']['from'], 'to': act['data']['to'],
        'window_days': activity_rules.WINDOW_DAYS,
        'categories': categories,
        'opportunities': opps,
        'opportunities_note': (None if mix is not None else
                               'Opportunities are withheld because the accounts held could '
                               'not be read, so a suggestion might repeat a product they have.'),
        'watch': activity_rules.watch_signals(prof),
    }
    if not categories:
        act['status'] = 'none'
    return act


def build_customer_insights(gateway: WarehouseGateway, cust_id: str,
                            customer: dict | None = None) -> dict[str, Any]:
    customer = customer if customer is not None else (gateway.get_customer(cust_id) or {})
    as_of = gateway.as_of_date().isoformat()
    key = f'{cust_id}:{as_of}'
    hit = _cached(key)
    if hit is not None:
        return hit

    products = _block(gateway.get_product_mix, cust_id)
    facilities = _block(gateway.get_facilities, cust_id)
    out = {
        'cust_id': str(cust_id),
        'as_of': as_of,
        'products': products,
        'facilities': facilities,
        'activity': _activity_block(gateway, customer, cust_id, products, facilities),
        'profile': _block(gateway.get_customer_profile, cust_id),
        'revenue': _block(gateway.get_revenue, cust_id),
    }
    # A product mix with nothing in it is "none", not an empty "live" block.
    if products['status'] == 'live' and not (products['data']['deposits'] or products['data']['loans']):
        products['status'] = 'none'
    # Only cache an answer that was actually read; a failed block should be retried.
    if all(out[b]['status'] != 'unavailable' for b in ('products', 'facilities', 'activity', 'profile', 'revenue')):
        _store(key, out)
    return out


def activity_candidates(gateway: WarehouseGateway, cust_id: str,
                        customer: dict | None = None) -> list[dict[str, Any]]:
    """The activity opportunities for the recommendation engine ([] when unavailable)."""
    try:
        ins = build_customer_insights(gateway, cust_id, customer)
    except Exception:                                          # noqa: BLE001
        return []
    act = ins.get('activity') or {}
    if act.get('status') != 'live':
        return []
    return list((act.get('data') or {}).get('opportunities') or [])
