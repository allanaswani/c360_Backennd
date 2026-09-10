"""Send the daily or weekly operations digest. Driven by cron / a scheduled task.

    manage.py send_ops_report --period daily
    manage.py send_ops_report --period weekly
    manage.py send_ops_report --period daily --dry-run --write-html out.html

``--dry-run`` builds the whole report and sends nothing, so the content can be
checked (and the HTML opened in a browser) before it goes to real inboxes.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from c360.reports import digest, mailer


class Command(BaseCommand):
    help = 'Email the daily or weekly Customer 360 operations report.'

    def add_arguments(self, parser):
        parser.add_argument('--period', choices=['daily', 'weekly'], default='daily')
        parser.add_argument('--dry-run', action='store_true',
                            help='Build and summarise the report without sending it.')
        parser.add_argument('--write-html', metavar='PATH',
                            help='Also write the rendered HTML body to this file.')
        parser.add_argument('--to', metavar='ADDR', action='append',
                            help='Override the recipients (repeatable).')

    def handle(self, *args, **options):
        period = options['period']
        report = (digest.build(24 * 60, 'Daily operations report') if period == 'daily'
                  else digest.build(7 * 24 * 60, 'Weekly operations rollup'))

        if options.get('write_html'):
            try:
                with open(options['write_html'], 'w', encoding='utf-8') as fh:
                    fh.write(report['html'])
            except OSError as exc:
                raise CommandError(f'could not write HTML: {exc}') from exc
            self.stdout.write(f"HTML written to {options['write_html']}")

        summary = report['summary']
        self.stdout.write(self.style.HTTP_INFO(report['subject']))
        self.stdout.write(
            f"  requests={summary['requests']:,} errors={summary['errors']:,} "
            f"error_rate={summary['error_rate_pct']}% p95={summary['p95_ms']}ms "
            f"active_users={summary['active_users']}")
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
            # Not an exception: a cron job that exits non-zero on a transient SMTP
            # failure produces a second alarm about the alarm. The mailer logs it.
            self.stdout.write(self.style.ERROR('not sent — see the log for the reason'))
