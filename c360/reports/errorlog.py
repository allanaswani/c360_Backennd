"""The batched error-log email.

Separate from the incident alerts on purpose. An alert says "the error rate is
above threshold, go look"; this is the log itself — every 4xx/5xx served in the
period, attached as a file you can grep outside the app.

It is batched (hourly by default, by the cron entry) and it is **silent when
there is nothing to report**: a log mail that arrives every hour saying "no
errors" is a mail rule waiting to happen, and then the one that matters is
filtered too.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from . import datasets, mailer, render


def build(window_minutes: int = 60) -> dict | None:
    """Return the mail parts, or ``None`` when the window held no errors."""
    dataset = datasets.error_log(window_minutes)
    if dataset['row_count'] == 0:
        return None

    rows = dataset['rows']
    server = [r for r in rows if r['severity'] == 'server']
    client = [r for r in rows if r['severity'] == 'client']

    # Group by (status, route) so a single failing endpoint reads as one line with
    # a count, not as 400 identical rows.
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (row['status'], row['method'], row['route'])
        bucket = grouped.setdefault(key, {
            'status': row['status'], 'method': row['method'], 'route': row['route'],
            'count': 0, 'last_seen': row['ts'], 'users': set(),
            'tone': 'bad' if row['severity'] == 'server' else 'warn',
        })
        bucket['count'] += 1
        if row['username']:
            bucket['users'].add(row['username'])
        if row['ts'] > bucket['last_seen']:
            bucket['last_seen'] = row['ts']
    top = sorted(grouped.values(), key=lambda b: -b['count'])
    for bucket in top:
        bucket['users'] = len(bucket['users'])

    window_text = dataset['subtitle']
    blocks = [
        render.tiles([
            {'label': 'Server errors', 'value': f'{len(server):,}',
             'tone': 'bad' if server else 'good', 'sub': '5xx'},
            {'label': 'Client errors', 'value': f'{len(client):,}',
             'tone': 'warn' if client else 'good', 'sub': '4xx'},
            {'label': 'Distinct failures', 'value': f'{len(grouped):,}',
             'sub': 'status × endpoint'},
        ]),
        render.heading('Grouped by endpoint', window_text),
        render.table(
            [
                {'key': 'status', 'label': 'Status', 'type': 'num'},
                {'key': 'route', 'label': 'Route', 'type': 'text'},
                {'key': 'count', 'label': 'Count', 'type': 'num'},
                {'key': 'users', 'label': 'Users', 'type': 'num'},
                {'key': 'last_seen', 'label': 'Last seen', 'type': 'datetime'},
            ],
            top, limit=15, tone_key='tone'),
        render.heading('Most recent'),
        render.table(
            [c for c in dataset['columns'] if c['key'] in {'ts', 'status', 'route', 'username', 'detail'}],
            [{**r, 'tone': 'bad' if r['severity'] == 'server' else 'warn'} for r in rows],
            limit=10, tone_key='tone'),
    ]

    subject = (f"[C360] Error log — {len(server):,} server / {len(client):,} client "
               f"({window_text})")
    html = render.shell(
        title='Error log',
        preheader=f"{dataset['row_count']:,} error responses · {window_text}",
        blocks=blocks,
        app_url=getattr(settings, 'C360_APP_URL', ''),
        footer_note='Sent only when there are errors to report. The attachment holds every line.',
    )
    text = render.to_text(subject, [
        f'{len(server):,} server errors (5xx), {len(client):,} client errors (4xx) in the {window_text}.',
        '',
        *[f"{b['status']} {b['method']} {b['route']} — {b['count']}x" for b in top[:20]],
    ])

    return {
        'subject': subject, 'html': html, 'text': text,
        'attachments': [mailer.csv_attachment(dataset), mailer.xlsx_attachment(dataset)],
        'dataset': dataset,
        'generated_at': timezone.now().isoformat(),
    }


def send(window_minutes: int = 60) -> dict:
    report = build(window_minutes)
    if report is None:
        return {'sent': 0, 'skipped': 'no errors in window'}
    report['sent'] = mailer.send(report['subject'], report['html'], report['text'],
                                 report['attachments'])
    return report
