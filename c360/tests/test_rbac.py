"""RBAC scoping — must fail closed. An RM never sees the whole book by default."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from c360.rbac.scoping import Scope, customer_visible, resolve_scope


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
