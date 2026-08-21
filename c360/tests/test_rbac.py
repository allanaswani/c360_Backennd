"""RBAC scoping — must fail closed. An RM never sees the whole book by default."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from c360.rbac.scoping import Scope, customer_visible, resolve_scope, staff_hidden
from c360.rbac.staff import is_staff_from_fields
from c360.warehouse.mock import seed
from c360.warehouse.mock.mock_gateway import MockWarehouse


def _req(headers):
    return SimpleNamespace(headers=headers)


class ScopingTests(SimpleTestCase):
    def test_management_sees_whole_book(self):
        scope = resolve_scope(_req({}))
        self.assertEqual(scope.role, 'management')
        self.assertIsNone(scope.sales_codes)
        self.assertTrue(scope.is_whole_book)
        self.assertTrue(scope.can_view_portfolio())

    def test_rm_scoped_to_own_codes(self):
        scope = resolve_scope(_req({'X-C360-Role': 'rm', 'X-C360-Sales-Codes': 'SC-1042,SC-1077'}))
        self.assertEqual(scope.role, 'rm')
        self.assertEqual(scope.sales_codes, ['SC-1042', 'SC-1077'])
        self.assertFalse(scope.is_whole_book)

    def test_rm_without_codes_fails_closed_not_open(self):
        # No codes must resolve to an EMPTY book, never the whole book.
        scope = resolve_scope(_req({'X-C360-Role': 'rm'}))
        self.assertEqual(scope.sales_codes, [])
        self.assertFalse(scope.is_whole_book)

    def test_customer_visibility(self):
        rm = Scope('rm', ['SC-1042'])
        self.assertTrue(customer_visible(rm, {'sales_code': 'SC-1042'}))
        self.assertFalse(customer_visible(rm, {'sales_code': 'SC-9999'}))
        mgmt = Scope('management', None)
        self.assertTrue(customer_visible(mgmt, {'sales_code': 'SC-9999'}))


class StaffSieveTests(SimpleTestCase):
    """HF-staff customers are superuser-only — invisible to RMs, to management, and even
    to role-tier admins (ceo/exco/hfdi_admin). Only a Django superuser may see them."""

    def test_rule_fires_on_any_signal(self):
        self.assertTrue(is_staff_from_fields(employer='HOUSING FINANCE CO'))
        self.assertTrue(is_staff_from_fields(employer='hf group ltd'))   # case-insensitive
        self.assertTrue(is_staff_from_fields(segment='STAFF'))
        self.assertTrue(is_staff_from_fields(bank_employee_id='EMP20481'))
        self.assertTrue(is_staff_from_fields(explicit=True))

    def test_rule_ignores_placeholders_and_outsiders(self):
        self.assertFalse(is_staff_from_fields(employer='Self-employed'))
        self.assertFalse(is_staff_from_fields(employer='Safaricom PLC'))
        self.assertFalse(is_staff_from_fields(segment='PRIVATE'))
        self.assertFalse(is_staff_from_fields(bank_employee_id='MIG_CIS'))   # migration placeholder
        self.assertFalse(is_staff_from_fields(bank_employee_id=None))
        self.assertFalse(is_staff_from_fields())

    def test_staff_visible_to_superuser_only(self):
        staff = {'is_staff': True}
        self.assertTrue(staff_hidden(Scope('rm', ['SC-1']), staff))                     # RM: hidden
        self.assertTrue(staff_hidden(Scope('management', None), staff))                 # manager: hidden
        # A role-tier admin (is_admin, e.g. ceo/exco) is NOT enough — still hidden.
        self.assertTrue(staff_hidden(Scope('management', None, is_admin=True), staff))
        # Only a Django superuser sees the staff record.
        self.assertFalse(staff_hidden(Scope('management', None, is_superuser=True), staff))
        self.assertFalse(staff_hidden(Scope('rm', ['SC-1']), {'is_staff': False}))      # non-staff: visible

    def test_superuser_header_grants_staff_lens_admin_does_not(self):
        # X-C360-Superuser grants the staff lens; X-C360-Admin only grants the ops lens.
        self.assertTrue(resolve_scope(_req({'X-C360-Superuser': 'true'})).is_superuser)
        self.assertFalse(resolve_scope(_req({'X-C360-Admin': 'true'})).is_superuser)   # admin ≠ superuser
        self.assertTrue(resolve_scope(_req({'X-C360-Admin': 'true'})).is_admin)        # but admin lens stands
        self.assertFalse(resolve_scope(_req({})).is_superuser)
        # An RM is never superuser, even if the header is spoofed.
        self.assertFalse(resolve_scope(_req({'X-C360-Role': 'rm', 'X-C360-Superuser': 'true'})).is_superuser)

    def test_mock_search_and_detail_hide_staff_from_non_admin(self):
        gw = MockWarehouse()
        cust = seed.CUSTOMERS[0]
        cid, original = cust['cust_id'], cust['staff']
        cust['staff'] = True   # flag this customer as HF staff for the test
        try:
            admin_ids = {r['cust_id'] for r in gw.search_customers(cust['name'], sales_codes=None, include_staff=True)}
            rm_ids = {r['cust_id'] for r in gw.search_customers(cust['name'], sales_codes=None, include_staff=False)}
            self.assertIn(cid, admin_ids)        # admin finds the staff customer
            self.assertNotIn(cid, rm_ids)        # RM never sees it in search
            self.assertTrue(gw.get_customer(cid)['is_staff'])   # detail is flagged → view 404s for non-admin
        finally:
            cust['staff'] = original
