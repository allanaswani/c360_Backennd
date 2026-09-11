"""Recommendation outcome-logging loop — record, upsert, list, stats, and the
label extraction that feeds retraining."""
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from c360.models import RecommendationFeedback


class FeedbackApiTests(TestCase):
    def setUp(self):
        self.c = APIClient()
        self.rm = User.objects.create_user('rm.brian', password='x')
        self.c.force_authenticate(user=self.rm)

    def _record(self, **over):
        body = {'cust_id': 'HF-1', 'product': 'savings', 'product_name': 'Savings Account',
                'score': 0.72, 'rule_id': 'ml.lgbm-v1', 'engine_version': 'ml.lgbm-v1',
                'outcome': 'accepted'}
        body.update(over)
        return self.c.post('/api/recommendations/feedback/', body, format='json')

    def test_record_requires_auth(self):
        self.c.force_authenticate(user=None)
        self.assertEqual(self._record().status_code, 401)

    def test_record_and_upsert(self):
        r1 = self._record()
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertEqual(r1.json()['recorded_by_name'], 'rm.brian')
        # Re-marking the same (customer, product) updates in place, not a new row.
        r2 = self._record(outcome='declined')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(RecommendationFeedback.objects.filter(cust_id='HF-1', product='savings').count(), 1)
        self.assertEqual(RecommendationFeedback.objects.get(cust_id='HF-1', product='savings').outcome, 'declined')

    def test_invalid_outcome_rejected(self):
        self.assertEqual(self._record(outcome='banana').status_code, 400)

    def test_list_by_customer(self):
        self._record()
        self._record(product='mortgage', outcome='pitched')
        r = self.c.get('/api/recommendations/feedback/?cust_id=HF-1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['results']), 2)

    def test_stats_acceptance_and_bands(self):
        # Two accepted (high band) + one declined (low band).
        self._record(cust_id='A', score=0.9, outcome='accepted')
        self._record(cust_id='B', score=0.85, outcome='accepted')
        self._record(cust_id='C', score=0.1, outcome='declined')
        r = self.c.get('/api/recommendations/feedback/stats/')
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d['labelled'], 3)
        self.assertEqual(d['accepted'], 2)
        self.assertAlmostEqual(d['acceptance_rate'], 2 / 3, places=3)
        # The 80-100% band should show higher acceptance than the 0-20% band.
        bands = {b['band']: b for b in d['acceptance_by_score_band']}
        self.assertEqual(bands['80-100%']['acceptance_rate'], 1.0)
        self.assertEqual(bands['0-20%']['acceptance_rate'], 0.0)


class LabelExtractionTests(TestCase):
    def test_labelled_rows_maps_outcomes_to_labels(self):
        # Feedback for a mock customer → feature rows + supervised labels.
        from django.conf import settings
        from c360.ml.feedback_labels import labelled_rows
        from c360.warehouse.factory import get_gateway
        settings.C360['DATA_MODE'] = 'mock'
        get_gateway.cache_clear()

        RecommendationFeedback.objects.create(cust_id='HF-100238', product='savings', outcome='accepted')
        RecommendationFeedback.objects.create(cust_id='HF-100571', product='savings', outcome='declined')
        RecommendationFeedback.objects.create(cust_id='HF-100238', product='mortgage', outcome='pitched')  # pending → excluded

        rows = labelled_rows(get_gateway())
        self.assertIn('savings', rows)
        labels = sorted(y for _, y in rows['savings'])
        self.assertEqual(labels, [0, 1])          # one declined (0), one accepted (1)
        self.assertNotIn('mortgage', rows)        # pending outcome contributes no label
        # Feature rows are clean (no scoring-only helper key).
        for row, _ in rows['savings']:
            self.assertNotIn('_held', row)


class FeedbackUnderPortfolioSsoTests(TestCase):
    """The RMs who actually log outcomes arrive on a portfolio SSO token and have no
    account in this database. Recording used to assign that caller straight to a
    ForeignKey, which raised — so every outcome they logged was thrown away with a
    500 while the panel optimistically showed the button as set."""

    def setUp(self):
        from c360.tests.test_sso import _pin_mock
        _pin_mock()
        self.c = APIClient()
        from c360.tests.test_sso import _portfolio_token
        self.token = _portfolio_token(user_id=99002, username='brian.rm',
                                      groups=['c360_rm'], sales_code='SC-1077')
        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def _post(self, outcome, product='savings'):
        return self.c.post('/api/recommendations/feedback/', {
            'cust_id': 'HF-100238', 'product': product, 'product_name': 'Savings',
            'domain': 'HFCB', 'score': 0.42, 'rule_id': 'ml.savings',
            'engine_version': 'v3', 'outcome': outcome,
        }, format='json')

    def test_sso_user_outcome_is_stored(self):
        r = self._post('accepted')
        self.assertIn(r.status_code, (200, 201), r.content[:300])
        self.assertEqual(RecommendationFeedback.objects.count(), 1)
        row = RecommendationFeedback.objects.get()
        self.assertEqual(row.outcome, 'accepted')
        self.assertEqual(row.recorded_by_username, 'brian.rm')
        self.assertIsNone(row.recorded_by)          # no local account to link to
        self.assertEqual(r.json()['recorded_by_name'], 'brian.rm')

    def test_remarking_updates_in_place_rather_than_piling_up(self):
        """The uniqueness rule is keyed on the username. Keyed on the nullable FK,
        every re-mark by an SSO user would insert a new row (NULLs don't collide)
        and the same recommendation would be counted repeatedly as a label."""
        self._post('pitched')
        self._post('accepted')
        self._post('declined')
        self.assertEqual(RecommendationFeedback.objects.count(), 1)
        self.assertEqual(RecommendationFeedback.objects.get().outcome, 'declined')

    def test_two_rms_keep_separate_marks_on_the_same_recommendation(self):
        from c360.tests.test_sso import _portfolio_token
        self._post('accepted')
        other = _portfolio_token(user_id=99003, username='faith.rm',
                                 groups=['c360_rm'], sales_code='SC-2088')
        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {other}')
        self._post('declined')
        self.assertEqual(RecommendationFeedback.objects.count(), 2)

    def test_mine_filter_works_for_an_sso_caller(self):
        self._post('accepted')
        r = self.c.get('/api/recommendations/feedback/?cust_id=HF-100238&mine=1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['results']), 1)

    def test_outcome_becomes_a_training_label(self):
        """The whole point: an SSO-logged outcome has to reach the stats the model
        is judged on. Before the fix these were all zero no matter how many times
        an RM marked something."""
        self._post('accepted')
        self._post('declined', product='mortgage')
        stats = self.c.get('/api/recommendations/feedback/stats/').json()
        self.assertEqual(stats['labelled'], 2)
        self.assertEqual(stats['accepted'], 1)
        self.assertEqual(stats['acceptance_rate'], 0.5)
