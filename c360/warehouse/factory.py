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
        # Curated reporting Postgres — holds the CURRENT customer→RM allocation
        # (retail_allocated_portfolio) that core banking only carries as the frozen
        # account-opening officer. Wired only when PG_HOST is configured; the gateway
        # falls back to the onboarding officer when it is absent or unreachable, so an
        # unset / down Postgres never breaks live mode.
        pg_cfg = settings.C360.get('postgres_config') or {}
        pg_conn = None
        if pg_cfg.get('host'):
            from .connector import PostgresDBAPIConnector
            pg_conn = PostgresDBAPIConnector(pg_cfg)
        return TrinoWarehouse(trino_conn, postgres=pg_conn)

    from .mock.mock_gateway import MockWarehouse
    return MockWarehouse()


def data_mode() -> str:
    return settings.C360.get('DATA_MODE', 'mock')
