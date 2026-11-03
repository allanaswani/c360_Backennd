"""The morning alert email - each RM gets the alerts in their own book (c360/alerts.py),
addressed to the RMs on the recipient list (rm_alert_recipients.csv, sales_code,rm_name,email)
and to the active C360 users whose profile carries that sales code. An RM with nothing new
gets no email: a daily "nothing to report" teaches people to ignore it.
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User

from .. import alerts as rules
from . import render

logger = logging.getLogger(__name__)
DEFAULT_LIST = Path(__file__).with_name('rm_alert_recipients.csv')

_COLS = [{'key': 'when', 'label': 'Date', 'type': 'text'},
         {'key': 'customer', 'label': 'Customer', 'type': 'text'},
         {'key': 'title', 'label': 'What happened', 'type': 'text'},
         {'key': 'amount', 'label': 'KES', 'type': 'num'}]


def listed_recipients(path: str | Path | None = None) -> dict[str, list[str]]:
    """Sales code -> emails from the recipient list. A missing file means no listed RMs."""
    path = Path(path or getattr(settings, 'C360_RM_ALERT_RECIPIENTS', '') or DEFAULT_LIST)
    out: dict[str, list[str]] = defaultdict(list)
    try:
        with open(path, newline='', encoding='utf-8-sig') as fh:
            for row in csv.DictReader(fh):
                code = (row.get('sales_code') or '').strip()
                email = (row.get('email') or '').strip()
                if code and '@' in email:
                    out[code].append(email)
    except FileNotFoundError:
        logger.warning('rm alert recipient list not found at %s', path)
    return out


def recipients_by_code(path: str | Path | None = None) -> dict[str, list[str]]:
    """Sales code -> email addresses: the recipient list plus the active C360 users who
    hold that book, each address once (case-insensitive)."""
    out: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    def add(code: str, email: str) -> None:
        if email.lower() not in seen[code]:
            seen[code].add(email.lower())
            out[code].append(email)

    for code, emails in listed_recipients(path).items():
        for e in emails:
            add(code, e)
    users = (User.objects.filter(is_active=True).exclude(email='')
             .exclude(profile__sales_code__isnull=True).select_related('profile'))
    for u in users:
        for code in str(u.profile.sales_code or '').split(','):
            if code.strip():
                add(code.strip(), u.email)
    return out


def build(code: str, alerts: list[dict[str, Any]], *, as_of: str) -> dict[str, Any]:
    """Subject, HTML and text for one RM's alerts (``alerts`` already in their book)."""
    counts = {s['kind']: s['count'] for s in rules.summary(alerts)}
    title = f'{len(alerts)} alert{"s" if len(alerts) != 1 else ""} in your book'
    pre = ', '.join(f'{counts[k]} {label.lower()}' for k, label in rules.KINDS.items() if counts[k])
    blocks = [render.tiles([{'label': label, 'value': f'{counts[k]:,}',
                             'tone': 'bad' if k == 'arrears' and counts[k] else 'neutral'}
                            for k, label in rules.KINDS.items()])]
    lines = [f'Sales code {code}. Loans as of {as_of}.', '']
    for k, label in rules.KINDS.items():
        rows = [a for a in alerts if a['kind'] == k]
        if not rows:
            continue
        blocks.append(render.heading(label, f'{len(rows)}'))
        blocks.append(render.table(_COLS, [{'when': a['date'], 'customer': f"{a['name'] or ''} ({a['cust_id']})",
                                            'title': a['title'], 'amount': a['amount']} for a in rows], limit=25))
        lines.append(f'{label} ({len(rows)})')
        lines += [f"  {a['date']}  {a['name'] or ''} ({a['cust_id']})  {a['title']}. {a['detail']}" for a in rows]
        lines.append('')
    html = render.shell(title, pre, blocks, getattr(settings, 'C360_APP_URL', ''),
                        footer_note=f'Sales code {code}. Arrears are loans 1 to 7 days overdue on the {as_of} loan book; '
                                    'mobile loans are left out. Open My book in Customer 360 for every alert.',
                        cta_path='/book', cta_label='Open My book')
    return {'subject': f'Customer 360: {title}', 'html': html, 'text': render.to_text(title, lines)}
