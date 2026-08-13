"""Data-provenance primitives.

The single most important product rule in Customer 360 is: *never show a silent
``--``*. Every number the frontend renders must declare where it came from, so the
UI can badge preview/unsourced data honestly instead of pretending it is real.

Three states, matching the colour convention used across the architecture doc and
the admin/status UI:

* ``LIVE`` (teal)   — real data, from a query we can run today against
  ``hf_customer`` / ``retail_allocated_portfolio`` / ``delta.gold_db``. In mock
  mode these values are seeded, but they represent the live query path and carry
  no caveat once credentials are wired.
* ``PREVIEW`` (amber) — a simulated stand-in shown so the view is complete and
  designed, but which is *not* real yet: new logic or a "must build" derivation
  (e.g. per-customer time series unpivoted from movement tables). Always badged.
* ``DERIVED`` (violet) — a value we *compute* from live warehouse data rather than
  read from a dedicated source column/feed: KYC completeness from the held identity
  attributes, operational risk from loan performance + leverage. It is real (not
  simulated) but is an internally-derived judgement, not an authoritative external
  rating — so it is badged distinctly and the derivation is stated in the note.
* ``TO_SOURCE`` (coral) — data that does not exist in any warehouse table yet and
  cannot be derived from what we hold (e.g. an external CRB bureau score).
  Never faked. Either the field renders an explicit "not yet sourced" state, or —
  where it gates a decision — the dependent output is suppressed entirely.

A metric is always an object, never a bare scalar, so the contract can't
accidentally leak an un-provenanced value to the client.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Provenance(str, Enum):
    LIVE = 'live'
    PREVIEW = 'preview'
    DERIVED = 'derived'
    TO_SOURCE = 'to_source'


# Default human-readable notes per non-live state. Callers can override.
_DEFAULT_NOTES = {
    Provenance.PREVIEW: 'Preview data — simulated until the source series is built.',
    Provenance.DERIVED: 'Derived — computed from live warehouse data, not read from a dedicated source feed.',
    Provenance.TO_SOURCE: 'Not yet sourced — pending a warehouse feed.',
}


@dataclass(frozen=True)
class Metric:
    """A single provenanced value destined for a card or stat tile."""

    value: Any
    status: Provenance = Provenance.LIVE
    unit: str | None = None          # 'KES', 'count', 'pct', 'date', …
    note: str | None = None          # caveat shown in a tooltip / badge
    label: str | None = None         # optional display label

    def to_dict(self) -> dict[str, Any]:
        note = self.note or (_DEFAULT_NOTES.get(self.status) if self.status != Provenance.LIVE else None)
        out: dict[str, Any] = {'value': self.value, 'status': self.status.value}
        if self.unit is not None:
            out['unit'] = self.unit
        if self.label is not None:
            out['label'] = self.label
        if note is not None:
            out['note'] = note
        return out


@dataclass(frozen=True)
class Series:
    """A provenanced time/category series for a chart.

    ``points`` is always long-format — a list of small dicts — so the frontend
    never sees the pivoted, year-column-named shape that ``daily_balance_movement``
    stores natively (see architecture §7A.7).
    """

    key: str
    name: str
    points: list[dict[str, Any]] = field(default_factory=list)
    status: Provenance = Provenance.LIVE
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        note = self.note or (_DEFAULT_NOTES.get(self.status) if self.status != Provenance.LIVE else None)
        out: dict[str, Any] = {
            'key': self.key,
            'name': self.name,
            'status': self.status.value,
            'points': self.points,
        }
        if note is not None:
            out['note'] = note
        return out


# Convenience constructors — keep call sites terse and readable.
def live(value: Any, **kw: Any) -> Metric:
    return Metric(value=value, status=Provenance.LIVE, **kw)


def preview(value: Any, **kw: Any) -> Metric:
    return Metric(value=value, status=Provenance.PREVIEW, **kw)


def derived(value: Any, **kw: Any) -> Metric:
    return Metric(value=value, status=Provenance.DERIVED, **kw)


def to_source(value: Any = None, **kw: Any) -> Metric:
    return Metric(value=value, status=Provenance.TO_SOURCE, **kw)
