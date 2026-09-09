"""Subsidiary-CRM shaping: property-lead stage ranking + insurance-CRM record."""
from __future__ import annotations

from django.test import SimpleTestCase

from c360 import crm


class FurthestStageTests(SimpleTestCase):
    def test_sealed_wins_over_failed(self):
        label, idx, kind = crm.furthest_stage(['failed', 'holding', 'SEALED', 'prospect'])
        self.assertEqual(label, 'Sealed')
        self.assertEqual(idx, 3)
        self.assertEqual(kind, 'won')

    def test_mixed_case_normalised(self):
        label, idx, kind = crm.furthest_stage(['PROSPECT', 'prospect'])
        self.assertEqual(label, 'Prospect')
        self.assertEqual(idx, 0)
        self.assertEqual(kind, 'active')

    def test_empty_defaults_to_prospect(self):
        label, idx, kind = crm.furthest_stage([])
        self.assertEqual(label, 'Prospect')
        self.assertEqual((idx, kind), (0, 'active'))

    def test_only_failed_is_lost(self):
        label, idx, kind = crm.furthest_stage(['failed', 'failed'])
        self.assertEqual(label, 'Not converted')
        self.assertEqual(kind, 'lost')

    def test_progress_beats_failure(self):
        # a customer who reached 'meeting' but also has a failed lead is still active
        _, idx, kind = crm.furthest_stage(['failed', 'meeting'])
        self.assertEqual((idx, kind), (1, 'active'))


class ShapePropertyLeadsTests(SimpleTestCase):
    def test_summary(self):
        out = crm.shape_property_leads(['prospect', 'SEALED', 'holding'], 12, 5)
        self.assertEqual(out['lead_count'], 3)
        self.assertEqual(out['stage'], 'Sealed')
        self.assertEqual(out['stage_index'], 3)
        self.assertEqual(out['stage_kind'], 'won')
        self.assertEqual(out['funnel'], ['Prospect', 'Engaged', 'Booked', 'Sealed'])
        self.assertEqual(out['followups'], 12)
        self.assertEqual(out['followups_successful'], 5)
        self.assertEqual(out['bridge'], 'phone')


class ShapeInsuranceCrmTests(SimpleTestCase):
    def test_email_becomes_name(self):
        out = crm.shape_insurance_crm({
            'risk_manager': 'robert.mugo@hfgroup.co.ke',
            'sales_person': 'Eric ouma Otieno',
            'occupation': 'RETIRED BANKER', 'branch': 'Buruburu Branch', 'location': None})
        self.assertEqual(out['risk_manager'], 'Robert Mugo')
        self.assertEqual(out['agent'], 'Eric ouma Otieno')
        self.assertEqual(out['occupation'], 'RETIRED BANKER')
        self.assertEqual(out['branch'], 'Buruburu Branch')

    def test_all_blank_returns_none(self):
        self.assertIsNone(crm.shape_insurance_crm(
            {'risk_manager': '  ', 'sales_person': None, 'occupation': '', 'branch': None, 'location': None}))

    def test_plain_name_risk_manager_kept(self):
        out = crm.shape_insurance_crm({'risk_manager': 'Jane  Doe', 'occupation': 'TEACHER'})
        self.assertEqual(out['risk_manager'], 'Jane Doe')
        self.assertEqual(out['occupation'], 'TEACHER')
