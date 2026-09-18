"""An RM's deposit and loan position, defined the way the business defines it.

Customer 360 and the RM Portfolio tool showed different numbers for the same
relationship manager, and the RM was right to report it. For Robin Magerer (MR4087):

                       C360          RM Portfolio
    Deposits           47.2M         55.77M
    Loans             138.6M        250.82M
    Customers            191           220

None of those were bugs in the arithmetic. They were three different definitions,
and the portfolio tool's are the ones the business quotes. Its own source says so
(``hf_group_backend/services/portfolio_service.py``), having already hit and fixed
this exact problem:

    The "Total Deposits" tile used to be SUM(hf_customer.total_depost_balance) over
    the RM's allocated customers. The trend chart underneath it plots
    daily_balance_movement, filtered on the account's rm_code. For EM3579 on 16 Sep
    2026 those two read 314,673,297 and 296,503,082 - and the RM knew the second one
    was theirs, because it is the figure the business quotes.

So this module ports their rules rather than inventing a fourth definition. Three
things matter and all three are load-bearing:

* **The population is accounts, not customers.** Balances key on ``rm_code`` on the
  account row, not on who the customer is allocated to. An account this RM manages
  counts even if the customer sits in someone else's allocation, which is most of
  why the loan figures diverged so far.
* **Freshness is a ladder, not an assumption.** Yesterday, then the day before, then
  the last CLOSED month end. The overnight load does not always land, and on the day
  they investigated, the loan file had not posted at all. Whichever rung answers is
  returned with the date it is as at, so the number never claims to be fresher than
  it is.
* **The month columns are not a uniform series.** 2026 is monthly; older periods are
  quarterly, so ``jul_25_bal`` does not exist while ``jun_25_bal`` does. Naming a
  column that was never created fails the whole statement, which is how their first
  version returned 0.00 for an RM sitting on 296 million. The columns are asked of
  the catalogue, never assumed.

INTERNAL ACCOUNTS and VIRTUAL are excluded because they are not customer money.
"""
from __future__ import annotations

from datetime import date

#: Segments that are not customer money. Excluded here for the same reason the
#: portfolio tool's trend chart excludes them.
EXCLUDED_SEGMENTS = ('INTERNAL ACCOUNTS', 'VIRTUAL')

_MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun',
           'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
_MONTH_FULL = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
               'August', 'September', 'October', 'November', 'December')
_MONTH_END = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

#: The two same-week rungs, tried before any month end.
_RECENT = (('yester_1_bal', 'yesterday'), ('yester_2_bal', '2 days ago'))


def closed_months(today: date | None = None, depth: int = 14) -> list[tuple[str, str]]:
    """``(column, human label)`` for every CLOSED month, most recent first.

    The warehouse pre-creates the whole year's columns, so ``sep_26_bal`` exists and
    holds a part-month total all through September. A month-end column for a month
    that has not ended is not a month-end balance, so the running month is never a
    rung on the ladder.
    """
    today = today or date.today()
    out: list[tuple[str, str]] = []
    year, month = today.year, today.month        # the RUNNING month; we start below it
    for _ in range(depth):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        day = _MONTH_END[month - 1]
        if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
            day = 29
        out.append((f'{_MONTHS[month - 1]}_{str(year)[-2:]}_bal',
                    f'{day} {_MONTH_FULL[month - 1]} {year}'))
    return out


def ladder(available_columns, today: date | None = None) -> list[tuple[str, str]]:
    """The freshness ladder, filtered to columns this table actually has.

    Order is deliberate: the most recent rung that holds a positive total wins, so a
    night when the load did not land falls through to the previous day rather than
    reporting zero.

    ``today`` is injectable so the rungs can be reasoned about at a fixed date. The
    ladder walks back a bounded number of months from the running one, which means a
    test that names a real column is otherwise only true for part of a year.
    """
    cols = {str(c).lower() for c in available_columns}
    rungs = [(c, label) for c, label in _RECENT if c in cols]
    rungs += [(c, f'as at {label}') for c, label in closed_months(today) if c in cols]
    return rungs


def pick(row: dict, rungs: list[tuple[str, str]]) -> tuple[float, str | None]:
    """First rung carrying a positive total, as ``(value, as_at label)``.

    ``(0.0, None)`` when no rung answers, which the caller must render as "not
    available" rather than as a zero balance: an RM with no rows in the movement
    table has an unknown position, not an empty one.
    """
    for key, label in rungs:
        value = row.get(key)
        if value in (None, ''):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number, label
    return 0.0, None


def build_sql(table: str, columns, *, has_segment: bool,
              today: date | None = None) -> tuple[str, list[str]]:
    """The balance query for one movement table, plus the rungs it can answer.

    Every rung is summed in a single pass (``SUM(col) FILTER (WHERE col > 0)``) so
    the ladder costs one query rather than one per rung. Returns ``('', [])`` when
    the table carries none of the expected columns, and the caller then declines to
    report a figure instead of reporting nothing as zero.
    """
    rungs = ladder(columns, today)
    if not rungs:
        return '', []
    parts = ', '.join(f'SUM({c}) FILTER (WHERE {c} > 0) AS {c}' for c, _ in rungs)
    # customer_segment is excluded the way the portfolio tool's chart excludes it,
    # but only when this table carries the column at all.
    seg = ('AND COALESCE(customer_segment, \'\') <> ALL(%s)' if has_segment else '')
    sql = (f'SELECT {parts} FROM {table} '
           f'WHERE TRIM(rm_code) = TRIM(%s) {seg}')
    return sql, [c for c, _ in rungs]
