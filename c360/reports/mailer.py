"""Delivery — from the reports mailbox to the operations recipients.

Kept separate from the report builders so the commands can be dry-run (build the
report, print it, send nothing) and so a mail failure is logged as a mail failure
rather than looking like a broken report.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection

log = logging.getLogger('c360')

_CSV = 'text/csv'
_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def recipients() -> list[str]:
    return list(getattr(settings, 'C360_REPORT_RECIPIENTS', []) or [])


def sender() -> str:
    return getattr(settings, 'C360_REPORT_FROM', None) or settings.DEFAULT_FROM_EMAIL


def send(subject: str, html: str, text: str, attachments: list[tuple[str, bytes, str]] | None = None,
         to: list[str] | None = None) -> int:
    """Send one report mail. Returns the number of messages actually accepted.

    Never raises: a monitoring email that crashes the cron job it runs in would
    take the next report down with it. Failures are logged with the subject so the
    absence of a report is traceable.
    """
    targets = to if to is not None else recipients()
    if not targets:
        log.warning('report mail "%s" not sent: no recipients configured '
                    '(set C360_REPORT_RECIPIENTS)', subject)
        return 0
    try:
        connection = get_connection(fail_silently=False)
        message = EmailMultiAlternatives(
            subject=subject, body=text, from_email=sender(), to=targets,
            connection=connection,
        )
        message.attach_alternative(html, 'text/html')
        for name, content, mimetype in attachments or []:
            message.attach(name, content, mimetype)
        sent = message.send()
        log.info('report mail sent: "%s" → %s (%d attachment(s))',
                 subject, ', '.join(targets), len(attachments or []))
        return sent
    except Exception:                                        # noqa: BLE001
        log.exception('report mail FAILED: "%s" → %s', subject, ', '.join(targets))
        return 0


def csv_attachment(dataset: dict) -> tuple[str, bytes, str]:
    from . import tabular
    return tabular.filename(dataset, 'csv'), tabular.to_csv(dataset), _CSV


def xlsx_attachment(dataset: dict) -> tuple[str, bytes, str]:
    from . import tabular
    return tabular.filename(dataset, 'xlsx'), tabular.to_xlsx(dataset), _XLSX
