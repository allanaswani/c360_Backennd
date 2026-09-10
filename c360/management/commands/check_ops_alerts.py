"""Run the incident checks and email anything that changed state.

Intended to run every few minutes from cron. Safe to run often: the dedupe state
in ``AlertState`` means a sustained incident sends one email, not one per run.

    manage.py check_ops_alerts
    manage.py check_ops_alerts --dry-run      # evaluate + print, send nothing, no state change
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from c360.reports import alerts


class Command(BaseCommand):
    help = 'Evaluate Customer 360 health/performance alerts and email state changes.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Evaluate and print findings; send nothing and do not '
                                 'record alert state.')

    def handle(self, *args, **options):
        outcome = alerts.run(dry_run=options['dry_run'])

        findings = outcome['findings']
        if not findings:
            self.stdout.write(self.style.SUCCESS('all checks clear'))
        for finding in findings:
            style = self.style.ERROR if finding['severity'] == 'critical' else self.style.WARNING
            self.stdout.write(style(f"[{finding['severity']}] {finding['title']} — {finding['detail']}"))

        if options['dry_run']:
            self.stdout.write(self.style.WARNING(
                f"dry run — {len(findings)} finding(s), nothing sent, state unchanged"))
            return

        self.stdout.write(
            f"new={len(outcome['new'])} ongoing={len(outcome['ongoing'])} "
            f"resolved={len(outcome['resolved'])}")
        if outcome.get('subject'):
            if outcome['sent']:
                self.stdout.write(self.style.SUCCESS(f"sent: {outcome['subject']}"))
            else:
                self.stdout.write(self.style.ERROR('not sent — see the log for the reason'))
        else:
            self.stdout.write('no state changes to report')
