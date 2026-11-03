"""Email each RM the alerts in their own book. Driven by cron, once each morning.

    manage.py send_rm_alerts --dry-run                 # who would get what, sends nothing
    manage.py send_rm_alerts --code CM3010 --to me@hfgroup.co.ke   # one book, to a test inbox
    manage.py send_rm_alerts                           # the real send

Recipients are the RMs on c360/reports/rm_alert_recipients.csv plus the active C360 users
whose profile sales code holds the book; a book with no recipient, or no alerts, gets no email.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from c360.reports import mailer, rm_alerts
from c360.services import alerts as alerts_service
from c360.warehouse.factory import get_gateway


class Command(BaseCommand):
    help = 'Email each RM the new arrears, deposit falls and maturities in their book.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Build every email and send nothing.')
        parser.add_argument('--code', action='append', help='Only these sales codes (repeatable).')
        parser.add_argument('--to', action='append', metavar='ADDR',
                            help='Send to these addresses instead of the RMs (repeatable).')
        parser.add_argument('--write-html', metavar='PATH', help='Write the first email body to this file.')

    def handle(self, *args, **options):
        payload, _ = alerts_service.book_alerts(get_gateway())
        if not payload.get('available'):
            raise CommandError(payload.get('detail') or 'alerts are not available')
        by_code = defaultdict(list)
        for a in payload['alerts']:
            if a.get('rm_code'):
                by_code[str(a['rm_code'])].append(a)
        only = {c.strip() for c in options.get('code') or [] if c.strip()}
        people = rm_alerts.recipients_by_code()
        ready = sent = skipped = 0
        wrote = False
        for code in sorted(by_code):
            if only and code not in only:
                continue
            targets = options.get('to') or people.get(code) or []
            msg = rm_alerts.build(code, by_code[code], as_of=payload['loan_as_of'])
            if options.get('write_html') and not wrote:
                with open(options['write_html'], 'w', encoding='utf-8') as fh:
                    fh.write(msg['html'])
                wrote = True
            if not targets:
                skipped += 1
                continue
            ready += 1
            self.stdout.write(f"{code}: {len(by_code[code])} alert(s) -> {', '.join(targets)}")
            if not options['dry_run'] and mailer.send(msg['subject'], msg['html'], msg['text'], to=targets):
                sent += 1
        verb = 'would send' if options['dry_run'] else 'sent'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {ready if options["dry_run"] else sent} email(s); '
            f'{skipped} book(s) with alerts have no recipient on that sales code.'))
