"""Brand names must come from ``c360/brand.py``, never from a literal.

The group has rebranded and a superseded entity name on a screen or in an emailed
report carries a regulatory penalty. The failure mode is not "somebody forgets to
rename" — it is "somebody renames thirty places and misses the tooltip, the PDF
footer and one error message". These tests are the thing that notices.

They scan the source rather than the payloads deliberately: a payload test only
covers the paths the suite happens to exercise, and the names that get missed are
precisely the ones on the rarely-rendered path.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from django.test import SimpleTestCase

from c360 import brand

C360 = Path(__file__).resolve().parent.parent

#: The entity names that must never be typed into a string literal.
BRAND_WORDS = ('HFCB', 'HFDI', 'HFBI')

#: Files that are allowed to contain them, and why.
ALLOWED_FILES = {
    'brand.py',          # the source of truth itself
}

#: Directories excluded from the scan.
SKIP_DIRS = {
    'migrations',        # historical records; the stored values are what was pitched
                         # under the name in force at the time
    'tests',             # this file, and fixtures that pin the contracts below
    '__pycache__',
}

#: Substrings whose presence makes an occurrence a CONTRACT, not display copy.
#: Renaming any of these would break the warehouse, the portfolio's JWT claims, the
#: API path, or URLs and audit rows going back months.
CONTRACT_MARKERS = (
    'hfdi_client_data', 'hfdi_mortgage_data', 'hfdi_lead_data',
    'hfdi_lead_followup_data', 'hfdi_admin',
    "'HFDI-", 'HFDI-(', 'HFDI-<',            # the customer-id prefix, and its examples
    'domains/hfcb', "'hfcb'", 'HFCBDomain',  # the API domain key and its type
    # The staff-employer match list. It does NOT move with the brand - it grows.
    # Matched against dim_customer.employer, which holds whatever was typed when each
    # record was opened; dropping a superseded name stops recognising every colleague
    # onboarded under it and leaks their 360 to non-admins.
    'STAFF_EMPLOYER_PATTERNS', 'C360_STAFF_EMPLOYER_PATTERNS',
    # The stored column default. Existing rows are a record of what was pitched under
    # the name in force at the time; new rows get the current brand (feedback_views).
    "default='HFCB'",
)

_STRING_RE = re.compile(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")


def _offending_lines(path: Path) -> list[tuple[int, str]]:
    """Lines where a brand word appears inside a string literal, minus contracts.

    A contract marker counts when it appears on the line OR in the three lines above
    it. A call such as::

        STAFF_EMPLOYER_PATTERNS = _csv_env(
            'C360_STAFF_EMPLOYER_PATTERNS',
            'HOUSING FINANCE,HF GROUP,HFC,...',
        )

    puts the marker and the value on different lines, and it is the value that
    carries the brand words.
    """
    out: list[tuple[int, str]] = []
    lines = io.open(path, encoding='utf-8').read().splitlines()
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith('#'):
            continue
        window = chr(10).join(lines[max(0, n - 4):n])
        if any(marker in window for marker in CONTRACT_MARKERS):
            continue
        for literal in _STRING_RE.findall(line):
            # Docstrings open with triple quotes and are not matched by the regex, so
            # what is left is genuinely a value the code builds or returns.
            if any(word in literal for word in BRAND_WORDS):
                out.append((n, line.rstrip()))
                break
    return out


class BrandLiteralTests(SimpleTestCase):
    def test_no_brand_name_is_typed_into_a_string_literal(self):
        offenders: list[str] = []
        for path in sorted(C360.rglob('*.py')):
            if path.name in ALLOWED_FILES:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            for line_no, text in _offending_lines(path):
                offenders.append(f'{path.relative_to(C360)}:{line_no}: {text.strip()}')
        self.assertEqual(offenders, [], msg=(
            '\n\nBrand names must be read from c360/brand.py, not typed.\n'
            'A rebrand has to land everywhere at once - a name missed in a tooltip or a\n'
            'report footer is a compliance exposure. Replace the literal with the\n'
            'matching constant, or add it to CONTRACT_MARKERS if it is a warehouse\n'
            'table, an API key or an id prefix that must NOT move with the brand.\n\n'
            + '\n'.join(offenders)))

    def test_the_brand_module_actually_defines_what_the_app_uses(self):
        for name in ('BANK', 'PROPERTY', 'INSURANCE', 'GROUP', 'DIGITAL'):
            value = getattr(brand, name)
            self.assertTrue(value and isinstance(value, str), name)

    def test_domain_labels_cover_every_value_row(self):
        """The frontend colour map keys off these exact strings, so a label that
        exists on one side and not the other loses its colour silently."""
        self.assertEqual(set(brand.DOMAIN_LABELS), {'bank', 'digital', 'property', 'insurance'})

    def test_derived_labels_follow_the_entity_name(self):
        """PROPERTY_CLIENT_SEGMENT is built from PROPERTY, so renaming the entity
        renames the segment too rather than leaving a half-renamed phrase."""
        self.assertIn(brand.PROPERTY, brand.PROPERTY_CLIENT_SEGMENT)
        self.assertIn(brand.BANK, brand.REPORT_SENDER_NAME)

    def test_the_customer_id_prefix_is_not_brand_copy(self):
        """HFDI- is in URLs, audit rows and logged outcomes going back months. It is
        an identifier that happens to look like a brand name, and it must NOT move
        when the brand does."""
        from c360 import hfdi
        self.assertEqual(hfdi.PREFIX, 'HFDI-')
        self.assertEqual(hfdi.format_id(415), 'HFDI-415')
