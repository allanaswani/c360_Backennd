"""Period-filter resolution.

One global period control per page drives every chart on that page (architecture
§4). The frontend sends a single ``period`` token; the backend resolves it to a
concrete ``(start, end)`` range plus a list of month anchors. Resolving this
*server-side* is deliberate: it's the seam where the pivoted movement tables
(``daily_balance_movement`` with per-month, year-stamped columns) get turned into
a clean long-format series, and where we pin the last closed business date the way
``bank_parameters.PREV_TRX_DATE`` is used across the reporting estate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

PRESETS = ('7D', '30D', 'QTD', 'YTD', 'CUSTOM')


@dataclass(frozen=True)
class ResolvedPeriod:
    token: str          # normalised token echoed back to the client
    start: date
    end: date           # the "as-of" / last closed business date anchor
    label: str          # human label, e.g. "Quarter to date"

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def month_anchors(self) -> list[date]:
        """First-of-month dates spanned by the range (inclusive).

        Used to build the dynamic month-column names for the pivoted movement
        tables (``jan_26_bal`` …). Generated from the range, never hardcoded, so
        the schema rolling over every January doesn't silently break queries.
        """
        anchors: list[date] = []
        cur = date(self.start.year, self.start.month, 1)
        last = date(self.end.year, self.end.month, 1)
        while cur <= last:
            anchors.append(cur)
            cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        return anchors

    def to_dict(self) -> dict:
        return {
            'token': self.token,
            'label': self.label,
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'days': self.days,
        }


_LABELS = {
    '7D': 'Last 7 days',
    '30D': 'Last 30 days',
    'QTD': 'Quarter to date',
    'YTD': 'Year to date',
}


def resolve_period(
    token: str | None,
    *,
    as_of: date,
    start: str | None = None,
    end: str | None = None,
) -> ResolvedPeriod:
    """Resolve a period token against a fixed ``as_of`` (last closed business date).

    ``as_of`` should come from ``bank_parameters.PREV_TRX_DATE`` in live mode so
    Customer 360's numbers reconcile with every other report. In mock mode the
    gateway supplies a stable synthetic close date.
    """
    tok = (token or '30D').strip().upper()
    if tok not in PRESETS:
        tok = '30D'

    if tok == 'CUSTOM':
        s = _parse_iso(start) or (as_of - timedelta(days=30))
        e = _parse_iso(end) or as_of
        if s > e:
            s, e = e, s
        label = f'{s.isoformat()} → {e.isoformat()}'
        return ResolvedPeriod('CUSTOM', s, e, label)

    if tok == '7D':
        return ResolvedPeriod(tok, as_of - timedelta(days=7), as_of, _LABELS[tok])
    if tok == '30D':
        return ResolvedPeriod(tok, as_of - timedelta(days=30), as_of, _LABELS[tok])
    if tok == 'QTD':
        q_first_month = 3 * ((as_of.month - 1) // 3) + 1
        return ResolvedPeriod(tok, date(as_of.year, q_first_month, 1), as_of, _LABELS[tok])
    # YTD
    return ResolvedPeriod(tok, date(as_of.year, 1, 1), as_of, _LABELS[tok])


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def month_column_name(anchor: date, suffix: str = 'bal') -> str:
    """Build a pivoted movement-table column name, e.g. date(2026,1,1) -> 'jan_26_bal'.

    Generated dynamically so the year-stamped schema never has to be hardcoded.
    """
    return f'{anchor.strftime("%b").lower()}_{anchor.strftime("%y")}_{suffix}'
