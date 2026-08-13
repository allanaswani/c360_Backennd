"""Trino connector protocol + a thin concrete adapter.

The architecture is explicit: *reuse the existing ``TrinoConnector`` interface and
``trino_config`` — do not build a new connector.* We honour that here by defining
the minimal protocol the existing connector already satisfies (an ``execute`` that
returns rows as dicts), and injecting whatever connector the host app provides.

The ``TrinoDBAPIConnector`` below is only a local fallback for standalone dev/live
mode when the platform connector isn't importable — it wraps the official ``trino``
dbapi with the same ``execute`` shape. It is never constructed in mock mode, so no
credentials are required to run the app.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class TrinoConnector(Protocol):
    """Minimal surface Customer 360 depends on.

    The existing platform connector already exposes an execute-and-fetch method;
    this protocol lets us type against it without importing it here. Params use the
    Trino ``?`` positional style (a sequence), not named/dict binding.
    """

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        ...


class TrinoDBAPIConnector:
    """Standalone adapter over the ``trino`` dbapi, used only in live mode.

    Reads ``settings.C360['trino_config']`` (env-driven). Constructed lazily so
    importing this module never requires the driver or a live warehouse.
    """

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._conn = None

    def _connection(self):
        if self._conn is None:
            import trino  # imported lazily; not needed in mock mode
            from trino.auth import BasicAuthentication

            cfg = self._config
            verify = cfg.get('verify', True)
            if not verify:
                # Internal gateway uses a self-signed cert; silence the noise.
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            auth = None
            if cfg.get('password'):
                auth = BasicAuthentication(cfg['user'], cfg['password'])
            self._conn = trino.dbapi.connect(
                host=cfg['host'],
                port=cfg['port'],
                user=cfg['user'],
                http_scheme=cfg.get('http_scheme', 'https'),
                catalog=cfg.get('catalog'),
                schema=cfg.get('schema'),
                auth=auth,
                verify=verify,
            )
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cur = self._connection().cursor()
        if params is not None:
            cur.execute(sql, tuple(params))   # Trino ? positional binding
        else:
            cur.execute(sql)
        cols = [c[0] for c in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
