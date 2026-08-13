"""Gateway selection.

``get_gateway()`` returns the warehouse implementation for the configured
``DATA_MODE``. Cached per-process. This is the single switch between preview and
production data — nothing downstream (services, API, frontend) knows or cares which
one it holds.
"""
from __future__ import annotations

from functools import lru_cache

from django.conf import settings

from .gateway import WarehouseGateway


@lru_cache(maxsize=1)
def get_gateway() -> WarehouseGateway:
    mode = settings.C360.get('DATA_MODE', 'mock')
    if mode == 'live':
        from .connector import TrinoDBAPIConnector
        from .trino.trino_gateway import TrinoWarehouse

        trino_conn = TrinoDBAPIConnector(settings.C360['trino_config'])
        # A dedicated Postgres connector would slot in here; for now the same
        # execute() protocol is reused. Wiring the curated-Postgres reader is a
        # follow-up once its credentials are issued.
        return TrinoWarehouse(trino_conn)

    from .mock.mock_gateway import MockWarehouse
    return MockWarehouse()


def data_mode() -> str:
    return settings.C360.get('DATA_MODE', 'mock')
