"""The relationship view on the Overview tab: the timeline and where the customer
stands among their segment. Its own endpoint so the core-banking tab never waits on
it; each half fails on its own, like the insights blocks."""
from __future__ import annotations

from typing import Any

from ..warehouse.gateway import WarehouseGateway
from .insights import _block


def build_relationship(gateway: WarehouseGateway, cust_id: str, customer: dict) -> dict[str, Any]:
    return {
        'cust_id': str(cust_id),
        'as_of': gateway.as_of_date().isoformat(),
        'timeline': _block(gateway.get_timeline, cust_id),
        'peers': _block(gateway.get_peer_position, cust_id, customer.get('segment')),
    }
