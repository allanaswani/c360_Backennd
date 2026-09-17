"""The related-party register — DIFFERENT parties, and the role between them.

Deliberately distinct from the linked-parties tests: those cover the SAME legal
person found under several customer numbers (national-ID matching). These cover the
curated ``public.relationship`` register, which names a role between two different
parties — a company's directors and signatories, the companies a person sits on.
The lakehouse cannot answer that at all: ``dim_customer`` carries a free-text
employer and an occupation picklist, neither of which is a role at a named company.

The live register sits on a LAN segment this box cannot reach, so the gateway is
exercised against a fake Postgres cursor shaped exactly like the real table —
varchar ids, space-padded ``relationship_type``, blank (not NULL) expiry on open
rows — and the HTTP contract is exercised in mock mode.
"""
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from c360 import relationships as rel_shape
from c360.tests.test_sso import _pin_mock
from c360.warehouse.gateway import WarehouseGateway
from c360.warehouse.trino.trino_gateway import TrinoWarehouse


class RoleVocabularyTests(SimpleTestCase):
    def test_types_are_trimmed_and_labelled(self):
        # The live table stores CHAR-padded types ('DIRECTOR    ').
        self.assertEqual(rel_shape.normalise('DIRECTOR    '), 'DIRECTOR')
        self.assertEqual(rel_shape.normalise(None), '')
        self.assertEqual(rel_shape.label('POA'), 'Power of attorney')
        self.assertEqual(rel_shape.label('SCHEME_MEMB'), 'Scheme member')
        self.assertEqual(rel_shape.label('HUSBAND_WIFE'), 'Spouse')

    def test_unmapped_type_still_reads_as_words(self):
        # 23 types today, but the register is maintained by another team. A type we
        # have not seen must not surface on screen as a raw upper-case code.
        self.assertEqual(rel_shape.label('JOINT_OWNER'), 'Joint Owner')

    def test_corporate_and_personal_are_separated(self):
        self.assertTrue(rel_shape.is_corporate(['DIRECTOR']))
        self.assertTrue(rel_shape.is_corporate(['HUSBAND_WIFE', 'DIRECTOR']))
        self.assertFalse(rel_shape.is_corporate(['HUSBAND_WIFE']))
        self.assertFalse(rel_shape.is_corporate([]))

    def test_unresolvable_counterparty_keeps_the_row_with_no_name(self):
        """The register outlives records. A relationship to an id we cannot name is
        still a fact about this customer: keep the row, leave the name empty, and
        let the panel say so — never invent a name for it."""
        m = rel_shape.shape_member(
            {'cust_id': '404404', 'roles': ['SIGNATORY'], 'direction': 'inbound'}, None)
        self.assertIsNone(m['name'])
        self.assertEqual(m['role_labels'], ['Signatory'])
        self.assertFalse(m['personal'])

    def test_scope_and_staff_markers_are_carried(self):
        """The service-side scope + staff sieve read these keys. Dropping them in the
        shaper would make every related party fail an RM's book check, and the panel
        would silently empty for exactly the people it is meant for."""
        m = rel_shape.shape_member(
            {'cust_id': '1', 'roles': ['DIRECTOR'], 'direction': 'outbound'},
            {'name': 'ACME', 'sales_code': 'SC-1', 'is_staff': True, 'staff': True})
        self.assertEqual(m['sales_code'], 'SC-1')
        self.assertTrue(m['is_staff'])

    def test_shape_returns_none_when_empty(self):
        self.assertIsNone(rel_shape.shape([]))

    def test_named_parties_sort_above_unnameable_ones(self):
        out = rel_shape.shape([
            rel_shape.shape_member({'cust_id': '9', 'roles': ['DIRECTOR']}, None),
            rel_shape.shape_member({'cust_id': '1', 'roles': ['DIRECTOR']}, {'name': 'A', 'value': 10}),
            rel_shape.shape_member({'cust_id': '2', 'roles': ['DIRECTOR']}, {'name': 'B', 'value': 900}),
        ])
        self.assertEqual([m['cust_id'] for m in out['members']], ['2', '1', '9'])
        self.assertEqual(out['corporate_count'], 3)


class _RegisterPG:
    """The curated Postgres. Only the register query answers; anything else the
    gateway asks of Postgres (the RM allocation, say) returns nothing, so a stray
    fixture row can never be mistaken for an allocation."""

    OPEN_ROWS = [
        {'other': '102010', 'rel': 'DIRECTOR    ', 'direction': 'outbound'},
        {'other': '102010', 'rel': 'SIGNATORY   ', 'direction': 'outbound'},
        {'other': '900001', 'rel': 'REPRESENTS  ', 'direction': 'inbound'},
    ]

    def __init__(self, rows=None, raises=False):
        self.rows = self.OPEN_ROWS if rows is None else rows
        self.raises = raises
        self.seen = []

    def execute(self, sql, params=None):
        if 'public.relationship' not in sql:
            return []
        if self.raises:
            raise RuntimeError('relation "public.relationship" does not exist')
        self.seen.append((sql, params))
        return self.rows


class _RegisterTrino:
    """dim_customer holds 102010. 900001 is an id the register names and the
    customer master no longer has."""

    def execute(self, sql, params=None):
        if 'dim_customer' in sql:
            # The presence probe selects the id alone; the identity query selects
            # full_name and the rest.
            if 'full_name' not in sql:
                return [{'id': 102010}]
            return [{'id': 102010, 'full_name': 'ZAWADI ENTERPRISES LTD  ',
                     'customer_segment': 'SME', 'account_branch_name': 'KENCOM',
                     'created_emp_id': '77', 'created_emp_name': 'A RM',
                     'employer': None, 'fk_bankemployeeid': None}]
        if 'eom_deposits' in sql:
            return [{'c': 102010, 'v': 400000, 'n': 2}]
        if 'eom_loans' in sql:
            return [{'c': 102010, 'v': 100000, 'n': 1}]
        return []


class LiveRegisterTests(SimpleTestCase):
    def test_reads_both_sides_and_merges_roles_per_party(self):
        gw = TrinoWarehouse(_RegisterTrino(), postgres=_RegisterPG())
        out = gw.get_related_parties('100571')
        by_id = {m['cust_id']: m for m in out['members']}
        # A pair registered twice — a director who also signs — is ONE row carrying
        # both roles, not the same name printed twice.
        self.assertEqual(sorted(by_id['102010']['role_labels']), ['Director', 'Signatory'])
        self.assertEqual(by_id['102010']['name'], 'ZAWADI ENTERPRISES LTD')
        self.assertEqual(by_id['102010']['value'], 500000)
        self.assertEqual(out['count'], 2)
        self.assertEqual(out['basis'], 'Related-party register')

    def test_open_rows_only_and_both_columns_searched(self):
        pg = _RegisterPG()
        TrinoWarehouse(_RegisterTrino(), postgres=pg).get_related_parties('100571')
        sql, params = pg.seen[0]
        # The register is directional, so the customer is looked for on both sides.
        self.assertIn('origin_customer = %s', sql)
        self.assertIn('related_customer = %s', sql)
        # The 167 closed rows carry a non-blank varchar expiry and are excluded.
        self.assertIn("COALESCE(expiry_date, '') = ''", sql)
        self.assertEqual(params, ('100571', '100571'))

    def test_id_the_master_no_longer_holds_is_kept_unnamed(self):
        """``_aggregate_customers`` names anything it is handed, falling back to
        'Customer 900001' — which reads as a resolved record and quietly asserts the
        customer exists. The presence probe is what stops that."""
        gw = TrinoWarehouse(_RegisterTrino(), postgres=_RegisterPG())
        by_id = {m['cust_id']: m for m in gw.get_related_parties('100571')['members']}
        self.assertIsNone(by_id['900001']['name'])
        self.assertEqual(by_id['900001']['role_labels'], ['Represents'])

    def test_self_edges_are_dropped(self):
        pg = _RegisterPG(rows=[{'other': '100571', 'rel': 'DIRECTOR', 'direction': 'outbound'}])
        self.assertIsNone(TrinoWarehouse(_RegisterTrino(), postgres=pg).get_related_parties('100571'))

    def test_absent_table_or_dead_postgres_is_not_an_error(self):
        """A deployment may point at a database that has no register. The panel then
        shows nothing — a missing optional source never takes the page down with it."""
        gw = TrinoWarehouse(_RegisterTrino(), postgres=_RegisterPG(raises=True))
        self.assertIsNone(gw.get_related_parties('100571'))

    def test_no_postgres_wired_returns_none(self):
        self.assertIsNone(TrinoWarehouse(_RegisterTrino(), postgres=None).get_related_parties('100571'))

    def test_non_numeric_customer_id_returns_none(self):
        gw = TrinoWarehouse(_RegisterTrino(), postgres=_RegisterPG())
        self.assertIsNone(gw.get_related_parties('not-a-number'))

    def test_base_gateway_default_is_none(self):
        """The register is optional: a gateway that does not implement it inherits a
        default that returns nothing, so the panel simply does not render."""
        self.assertIsNone(WarehouseGateway.get_related_parties(object(), 'HF-102010'))


class RelatedPartiesApiTests(TestCase):
    """The HTTP contract, in mock mode: the register rides on /linked/ under `related`."""

    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def _related(self, cust_id, **headers):
        r = self.c.get(f'/api/customers/{cust_id}/linked/', **headers)
        self.assertEqual(r.status_code, 200)
        return r.json().get('related')

    def test_company_lists_its_officers_with_roles(self):
        rel = self._related('HF-102010', HTTP_X_C360_ADMIN='1')
        self.assertIsNotNone(rel)
        by_id = {m['cust_id']: m for m in rel['members']}
        self.assertEqual(sorted(by_id['HF-100571']['role_labels']), ['Director', 'Signatory'])
        self.assertEqual(by_id['HF-100571']['direction'], 'inbound')
        self.assertEqual(by_id['HF-100571']['name'], 'Peter Njoroge Kariuki')

    def test_person_lists_the_company(self):
        rel = self._related('HF-100571', HTTP_X_C360_ADMIN='1')
        by_id = {m['cust_id']: m for m in rel['members']}
        self.assertIn('HF-102010', by_id)
        # The same edge, read from the other side — the direction reverses.
        self.assertEqual(by_id['HF-102010']['direction'], 'outbound')

    def test_unresolvable_id_survives_to_the_client_with_no_name(self):
        rel = self._related('HF-102010', HTTP_X_C360_ADMIN='1')
        by_id = {m['cust_id']: m for m in rel['members']}
        self.assertIn('HF-109404', by_id)
        self.assertIsNone(by_id['HF-109404']['name'])
        self.assertEqual(rel['count'], len(rel['members']))

    def test_family_ties_are_withheld_but_counted(self):
        """Spouse/sibling edges are personal data about third parties with no bearing
        on a banking decision, so they are dropped from the panel — and the number
        dropped is reported, so what is on screen is never silently short."""
        rel = self._related('HF-100571', HTTP_X_C360_ADMIN='1')
        shown = {m['cust_id'] for m in rel['members']}
        self.assertNotIn('HF-101488', shown)          # the spouse edge
        self.assertEqual(rel['withheld_personal'], 1)

    def test_scope_and_staff_keys_never_reach_the_client(self):
        rel = self._related('HF-102010', HTTP_X_C360_ADMIN='1')
        self.assertTrue(rel['members'])
        for m in rel['members']:
            self.assertNotIn('sales_code', m)
            self.assertNotIn('is_staff', m)
            self.assertNotIn('staff', m)

    def test_related_parties_outside_the_book_are_not_shown(self):
        """The register is a legitimate route to a record the caller is not entitled
        to see, so it gets the same scope treatment as search. SC-1108 owns Zawadi
        itself but none of its officers, so the panel comes back empty rather than
        naming customers outside the book."""
        rel = self._related('HF-102010', HTTP_X_C360_ROLE='rm',
                            HTTP_X_C360_SALES_CODES='SC-1108')
        self.assertIsNone(rel)

    def test_related_parties_inside_the_book_are_shown(self):
        # SC-1077 owns Peter Kariuki, who is a director of Zawadi.
        rel = self._related('HF-102010', HTTP_X_C360_ROLE='rm',
                            HTTP_X_C360_SALES_CODES='SC-1077,SC-1108')
        self.assertIsNotNone(rel)
        self.assertEqual([m['cust_id'] for m in rel['members']], ['HF-100571'])
