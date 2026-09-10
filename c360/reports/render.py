"""HTML for the report emails.

Written as inline-styled tables on purpose. Outlook renders a subset of CSS,
ignores ``<style>`` blocks in some configurations and has no flexbox or grid at
all, so anything clever here arrives as a stack of unstyled text. Tables with
inline styles are the format that survives.

The palette is the HFCB brand set used by the app (navy #084B65, teal #1996A9,
coral #F04E45, gold #F4BE18) so a report looks like it came from Customer 360 and
not from a monitoring tool nobody recognises.
"""
from __future__ import annotations

import html as html_escape
from typing import Iterable

NAVY = '#084B65'
TEAL = '#1996A9'
CORAL = '#F04E45'
GOLD = '#F4BE18'
INK = '#12333F'
MUTED = '#6B7A82'
HAIRLINE = '#E3EAED'
SURFACE = '#FFFFFF'
CANVAS = '#F4F7F8'

TONE_COLOR = {'good': TEAL, 'warn': GOLD, 'bad': CORAL, 'neutral': INK}


def _e(value) -> str:
    return html_escape.escape('' if value is None else str(value))


def shell(title: str, preheader: str, blocks: Iterable[str], app_url: str,
          footer_note: str = '') -> str:
    """Wrap rendered blocks in the branded email frame."""
    body = '\n'.join(blocks)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title></head>
<body style="margin:0;padding:0;background:{CANVAS};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_e(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CANVAS};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
       style="width:640px;max-width:100%;background:{SURFACE};border:1px solid {HAIRLINE};border-radius:12px;overflow:hidden;
              font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <tr><td style="background:{NAVY};padding:20px 24px;">
    <div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#9FC4D2;">HFCB Customer 360</div>
    <div style="font-size:20px;font-weight:600;color:#ffffff;margin-top:4px;">{_e(title)}</div>
    <div style="font-size:12.5px;color:#9FC4D2;margin-top:3px;">{_e(preheader)}</div>
  </td></tr>
  <tr><td style="padding:20px 24px 8px;">{body}</td></tr>
  <tr><td style="padding:8px 24px 22px;">
    <a href="{_e(app_url)}/admin/observability"
       style="display:inline-block;background:{TEAL};color:#ffffff;text-decoration:none;
              font-size:13px;font-weight:600;padding:10px 18px;border-radius:8px;">Open the dashboard</a>
  </td></tr>
  <tr><td style="border-top:1px solid {HAIRLINE};padding:14px 24px;background:{CANVAS};">
    <div style="font-size:11px;color:{MUTED};line-height:1.55;">
      Sent automatically by Customer 360. {_e(footer_note)}
    </div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def tiles(items: list[dict]) -> str:
    """A row of headline numbers. ``[{label, value, sub, tone}]``, three per row."""
    if not items:
        return ''
    cells = []
    for item in items:
        color = TONE_COLOR.get(item.get('tone') or 'neutral', INK)
        sub = (f'<div style="font-size:11px;color:{MUTED};margin-top:2px;">{_e(item["sub"])}</div>'
               if item.get('sub') else '')
        cells.append(f"""<td width="33%" valign="top"
          style="padding:10px 12px;border:1px solid {HAIRLINE};border-radius:10px;background:{SURFACE};">
          <div style="font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:{MUTED};">{_e(item['label'])}</div>
          <div style="font-size:22px;font-weight:700;color:{color};margin-top:3px;">{_e(item['value'])}</div>
          {sub}</td>""")

    rows = []
    for i in range(0, len(cells), 3):
        chunk = cells[i:i + 3]
        while len(chunk) < 3:
            chunk.append('<td width="33%"></td>')
        rows.append('<tr>' + '<td width="8"></td>'.join(chunk) + '</tr>')
    spacer = '<tr><td colspan="5" height="8"></td></tr>'
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin-bottom:14px;">{spacer.join(rows)}</table>')


def heading(text: str, note: str = '') -> str:
    note_html = f'<span style="font-size:11.5px;color:{MUTED};font-weight:400;"> · {_e(note)}</span>' if note else ''
    return (f'<div style="font-size:14px;font-weight:600;color:{INK};margin:18px 0 8px;">'
            f'{_e(text)}{note_html}</div>')


def paragraph(text: str) -> str:
    return f'<p style="font-size:13px;line-height:1.6;color:{INK};margin:0 0 12px;">{_e(text)}</p>'


def empty(text: str) -> str:
    return (f'<div style="font-size:12.5px;color:{MUTED};padding:12px;border:1px dashed {HAIRLINE};'
            f'border-radius:8px;margin-bottom:12px;">{_e(text)}</div>')


def table(columns: list[dict], rows: list[dict], limit: int = 12,
          tone_key: str | None = None) -> str:
    """A compact table. ``tone_key`` names a row key holding good/warn/bad, which
    tints that row's first cell — used by the data-health and error blocks."""
    if not rows:
        return empty('Nothing to report here.')
    head = ''.join(
        f'<th align="{"right" if c["type"] in {"num", "ms", "pct"} else "left"}" '
        f'style="font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:{MUTED};'
        f'padding:7px 8px;border-bottom:1px solid {HAIRLINE};font-weight:600;">{_e(c["label"])}</th>'
        for c in columns)

    body = []
    for row in rows[:limit]:
        tone = TONE_COLOR.get(row.get(tone_key) or '', None) if tone_key else None
        cells = []
        for i, c in enumerate(columns):
            align = 'right' if c['type'] in {'num', 'ms', 'pct'} else 'left'
            value = row.get(c['key'])
            text = '—' if value is None or value == '' else str(value)
            if c['type'] == 'pct' and isinstance(value, (int, float)):
                text = f'{value}%'
            if c['type'] == 'ms' and isinstance(value, (int, float)):
                text = f'{int(value):,} ms'
            elif c['type'] == 'num' and isinstance(value, (int, float)):
                text = f'{value:,}'
            style = (f'font-size:12.5px;color:{INK};padding:7px 8px;'
                     f'border-bottom:1px solid {HAIRLINE};')
            if i == 0 and tone:
                style += f'border-left:3px solid {tone};font-weight:600;'
            cells.append(f'<td align="{align}" style="{style}">{_e(text)}</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')

    more = ''
    if len(rows) > limit:
        more = (f'<div style="font-size:11.5px;color:{MUTED};padding:8px 2px;">'
                f'+ {len(rows) - limit:,} more — see the attached file.</div>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid {HAIRLINE};border-radius:8px;border-collapse:separate;'
            f'border-spacing:0;overflow:hidden;margin-bottom:6px;">'
            f'<tr style="background:{CANVAS};">{head}</tr>{"".join(body)}</table>{more}')


def callout(text: str, tone: str = 'bad') -> str:
    color = TONE_COLOR.get(tone, CORAL)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 14px;"><tr><td style="border-left:4px solid {color};'
            f'background:{CANVAS};padding:12px 14px;border-radius:0 8px 8px 0;'
            f'font-size:13px;line-height:1.55;color:{INK};">{_e(text)}</td></tr></table>')


def to_text(title: str, lines: Iterable[str]) -> str:
    """The plain-text alternative. Some recipients read mail as text; a blank
    text/plain part is what makes a report land in spam."""
    return f'{title}\n{"=" * len(title)}\n\n' + '\n'.join(lines) + '\n'
