"""Operational reporting — the parts of Customer 360 that leave the app on their own.

Everything an administrator can see on the Monitoring, Audit trail and Data health
screens is also (a) downloadable as CSV / Excel / PDF and (b) deliverable by email,
so an outage at 02:00 does not depend on somebody happening to be logged in.

The layer is deliberately split so a report is defined exactly once:

* :mod:`c360.reports.datasets` — named, tabular datasets (columns + rows + title).
  Both the export endpoints and the email attachments read from here, so a column
  added for the screen appears in the mail without a second edit.
* :mod:`c360.reports.tabular`  — turns a dataset into CSV or XLSX bytes.
* :mod:`c360.reports.digest`   — the daily / weekly narrative, as HTML for the body.
* :mod:`c360.reports.alerts`   — incident detection, with state so a sustained
  outage is one email rather than one per check.
* :mod:`c360.reports.mailer`   — delivery, from the reports mailbox to the
  operations recipients.
"""
