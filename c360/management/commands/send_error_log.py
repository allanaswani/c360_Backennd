"""Email the error log for a window, as an attached file.

Silent when the window held no errors, so it can run hourly without becoming
something people filter.

    manage.py send_error_log                 # last 60 minutes
    manage.py send_error_log --minutes 1440  # last day
    manage.py send_error_log --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from c360.reports import errorlog, mailer


class Command(BaseCommand):
    help = 'Email the Customer 360 error log for a time window (silent when clean).'

    def add_arguments(self, parser):
        parser.add_argument('--minutes', type=int, default=60,
                            help='Window to report on, in minutes (default 60).')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--to', metavar='ADDR', action='append',
                            help='Override the recipients (repeatable).')

    def handle(self, *args, **options):
        minutes = max(1, options['minutes'])
        report = errorlog.build(minutes)
        if report is None:
            self.stdout.write(self.style.SUCCESS(
                f'no errors in the last {minutes} minute(s) — nothing sent'))
            return

        self.stdout.write(self.style.HTTP_INFO(report['subject']))
        self.stdout.write(f"  {report['dataset']['row_count']:,} error row(s)")
        for name, content, _ in report['attachments']:
            self.stdout.write(f'  attachment: {name} ({len(content):,} bytes)')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('dry run — nothing sent'))
            return

        targets = options.get('to') or mailer.recipients()
        sent = mailer.send(report['subject'], report['html'], report['text'],
                           report['attachments'], to=targets)
        if sent:
            self.stdout.write(self.style.SUCCESS(f"sent to {', '.join(targets)}"))
        else:
            self.stdout.write(self.style.ERROR('not sent — see the log for the reason'))
