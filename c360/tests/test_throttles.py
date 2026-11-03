"""Sign-in throttles count per account, so users behind one proxy don't share a limit."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.settings import api_settings
from rest_framework.test import APIClient

RATES = {'login': '2/hour', 'login_ip': '100/hour',
         'otp_request': '20/hour', 'otp_request_ip': '600/hour',
         'otp_verify': '10/hour', 'otp_verify_ip': '600/hour'}


class SignInThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self._rates = api_settings.DEFAULT_THROTTLE_RATES
        from rest_framework.throttling import SimpleRateThrottle
        self._cls_rates = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = RATES
        for n in ('ann', 'ben', 'cat'):
            User.objects.create_user(n, f'{n}@example.test', 'pw-Strong-123')
        self.c = APIClient()

    def tearDown(self):
        from rest_framework.throttling import SimpleRateThrottle
        SimpleRateThrottle.THROTTLE_RATES = self._cls_rates
        cache.clear()

    def _login(self, user, pw='wrong'):
        return self.c.post('/api/auth/login/', {'username': user, 'password': pw}, format='json')

    def test_one_account_is_limited(self):
        self.assertEqual(self._login('ann').status_code, 401)
        self.assertEqual(self._login('ann').status_code, 401)
        self.assertEqual(self._login('ann').status_code, 429)

    def test_other_accounts_from_same_ip_are_not(self):
        for _ in range(2):
            self._login('ann')
        self.assertEqual(self._login('ann').status_code, 429)
        self.assertNotEqual(self._login('ben').status_code, 429)
        self.assertNotEqual(self._login('cat').status_code, 429)

    def test_username_case_does_not_reset_the_count(self):
        self._login('ann'); self._login('ANN')
        self.assertEqual(self._login(' Ann ').status_code, 429)
