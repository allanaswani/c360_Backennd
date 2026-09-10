"""Renders a dataset (see :mod:`c360.reports.datasets`) to CSV or XLSX bytes.

Used by the export endpoints and by the digest emails' attachments, so a file
downloaded from the screen and the same file arriving by mail are byte-for-byte
the same report.

Two details that matter more than they look:

* **CSV is written with a UTF-8 BOM.** Excel on Windows reads a BOM-less UTF-8 CSV
  as the system codepage and mangles every non-ASCII character. Customer and
  branch names here are not guaranteed ASCII.
* **Numbers stay numbers.** A value typed ``num`` / ``ms`` / ``pct`` is written as
  a number, never a pre-formatted string, so the recipient can sort and sum
  without re-typing the column.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from django.utils import timezone

_NUMERIC_TYPES = {'num', 'ms', 'pct'}


def _coerce(value, type_: str):
    """A cell as it should land in the file: numbers numeric, everything else text."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if type_ in _NUMERIC_TYPES:
        if isinstance(value, (int, float)):
            return value
        try:
            text = str(value).strip()
            return float(text) if '.' in text else int(text)
        except (TypeError, ValueError):
            return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return str(value)
    return value


def to_csv(dataset: dict) -> bytes:
    """UTF-8 CSV with a BOM, one header row, no leading title rows.

    The title deliberately does NOT go in the file: a CSV with prose above the
    header stops being loadable by anything that reads CSVs. The context lives in
    the filename and in the covering email.
    """
    columns = dataset['columns']
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow([c['label'] for c in columns])
    for row in dataset['rows']:
        writer.writerow([_coerce(row.get(c['key']), c['type']) for c in columns])
    return b'\xef\xbb\xbf' + buffer.getvalue().encode('utf-8')


def to_xlsx(dataset: dict) -> bytes:
    """A single-sheet workbook: title block, frozen header, auto-width, filters."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    columns = dataset['columns']
    wb = Workbook()
    ws = wb.active
    ws.title = _sheet_name(dataset.get('title') or 'Report')

    ws.append([dataset.get('title') or 'Report'])
    ws['A1'].font = Font(bold=True, size=14, color='084B65')
    stamp = f"{dataset.get('subtitle') or ''}  ·  generated {timezone.localtime():%d %b %Y %H:%M}".strip(' ·')
    ws.append([stamp])
    ws['A2'].font = Font(size=9, color='6B7A82')
    ws.append([])

    header_row = 4
    ws.append([c['label'] for c in columns])
    header_fill = PatternFill('solid', fgColor='084B65')
    for idx in range(1, len(columns) + 1):
        cell = ws.cell(row=header_row, column=idx)
        cell.font = Font(bold=True, color='FFFFFF', size=10)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical='center')

    for row in dataset['rows']:
        ws.append([_coerce(row.get(c['key']), c['type']) for c in columns])

    # Percentages and millisecond columns read better with a fixed format; leave
    # plain counts alone so they stay integers.
    for idx, column in enumerate(columns, start=1):
        if column['type'] == 'pct':
            for cell in ws.iter_rows(min_row=header_row + 1, min_col=idx, max_col=idx):
                cell[0].number_format = '0.00'
        elif column['type'] in {'num', 'ms'}:
            for cell in ws.iter_rows(min_row=header_row + 1, min_col=idx, max_col=idx):
                cell[0].number_format = '#,##0'

    last_row = header_row + len(dataset['rows'])
    if columns:
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
        ws.auto_filter.ref = f'A{header_row}:{get_column_letter(len(columns))}{max(last_row, header_row)}'

    for idx, column in enumerate(columns, start=1):
        widest = len(column['label'])
        for row in dataset['rows'][:500]:          # sample, so a huge export stays quick
            widest = max(widest, len(str(row.get(column['key']) or '')))
        ws.column_dimensions[get_column_letter(idx)].width = min(52, max(10, widest + 2))

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _sheet_name(title: str) -> str:
    """Excel rejects []:*?/\\ and anything over 31 characters in a sheet name."""
    return re.sub(r'[\[\]:*?/\\]', '-', title)[:31] or 'Report'


def filename(dataset: dict, extension: str) -> str:
    """``c360-activity-trail-20260910-1432.csv`` — sortable, and says what it is."""
    slug = re.sub(r'[^a-z0-9]+', '-', (dataset.get('title') or dataset.get('key') or 'report').lower()).strip('-')
    return f"c360-{slug}-{timezone.localtime():%Y%m%d-%H%M}.{extension}"
