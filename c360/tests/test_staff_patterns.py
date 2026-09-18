"""The staff employer list must grow at a rebrand, never shrink.

This list decides whose account is hidden from non-admins. Under-listing leaks a
colleague's 360 to every relationship manager, which is the exact data the sieve
exists to protect.

It has already happened once. A repo-wide rebrand rename replaced the bare word
HFDI here with the new trading name. HFDI contains no 'HFC', so no other token
caught it, and every colleague whose employer reads HFDI silently stopped being
recognised as staff. Nothing failed, because nothing asserted this list's contents:
the brand guard skips this line by design, and "do not flag" is not the same as
"safe to change".

So these tests name the values. A rename that removes one fails here.
"""
from django.test import SimpleTestCase

from c360.rbac.staff import STAFF_EMPLOYER_PATTERNS, is_staff_from_fields

#: Every name the warehouse may hold in dim_customer.employer. Records go back years
#: and carry whatever was typed at the time, so a superseded name is not obsolete
#: here - it is the only thing identifying the colleagues onboarded under it.
HISTORICAL = ('HOUSING FINANCE', 'HF GROUP', 'HFC', 'HF BANK', 'HFDI', 'HFBI',
              'HF FOUNDATION', 'HF CUSTODY', 'HF INSURANCE')

#: Names in force after the rebrand.
CURRENT = ('HFCB', 'HFCB PROPERTIES')


class PatternListTests(SimpleTestCase):
    def test_every_historical_name_is_still_listed(self):
        missing = [n for n in HISTORICAL if n not in STAFF_EMPLOYER_PATTERNS]
        self.assertEqual(missing, [], msg=(
            '\n\nA name was removed from STAFF_EMPLOYER_PATTERNS.\n'
            'The list grows at a rebrand and never shrinks: dim_customer.employer holds\n'
            'whatever was typed when each record was opened, so dropping a superseded\n'
            'name stops recognising every colleague onboarded under it and exposes their\n'
            'own account to every RM.\n'))

    def test_the_post_rebrand_names_are_listed(self):
        for name in CURRENT:
            self.assertIn(name, STAFF_EMPLOYER_PATTERNS, name)

    def test_hfdi_specifically(self):
        """Named on its own because this is the one that was lost, and because it is
        the one no other token covers."""
        self.assertIn('HFDI', STAFF_EMPLOYER_PATTERNS)
        self.assertFalse(any('HFC' in p and p != 'HFC' and 'HFDI'.startswith(p)
                             for p in STAFF_EMPLOYER_PATTERNS))


class DetectionTests(SimpleTestCase):
    """The rule is a substring match, so these are the employer values as recorded."""

    def test_an_hfdi_employee_is_staff(self):
        for employer in ('HFDI', 'HFDI LTD', 'hfdi limited', ' HFDI '):
            self.assertTrue(is_staff_from_fields(employer=employer), employer)

    def test_post_rebrand_employees_are_staff(self):
        for employer in ('HFCB', 'HFCB BANK', 'HFCB PROPERTIES', 'hfcb properties ltd'):
            self.assertTrue(is_staff_from_fields(employer=employer), employer)

    def test_historical_spellings_are_still_staff(self):
        for employer in ('HOUSING FINANCE COMPANY', 'HFC', 'HFC LTD', 'HF BANK',
                         'HFBI', 'HF INSURANCE AGENCY'):
            self.assertTrue(is_staff_from_fields(employer=employer), employer)

    def test_an_ordinary_employer_is_not_staff(self):
        """The sieve must not over-reach either: hiding ordinary customers from RMs
        is how they were once shown 'Customer not found' on live accounts."""
        for employer in ('SAFARICOM', 'KENYA POWER', 'SELF EMPLOYED', 'FARMER',
                         'EQUITY BANK', 'HELB', None, ''):
            self.assertFalse(is_staff_from_fields(employer=employer), employer)

    def test_the_staff_segment_signal_still_works(self):
        self.assertTrue(is_staff_from_fields(segment='STAFF'))
        self.assertTrue(is_staff_from_fields(segment='EMPLOYEES'))
        self.assertFalse(is_staff_from_fields(segment='RETAIL'))
