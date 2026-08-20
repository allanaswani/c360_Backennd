"""Stateless silent-refresh for single-sign-on tokens.

Regression: the SPA adopts a portfolio-minted JWT (its user does NOT exist in Customer
360's database). The stock TokenRefreshView rotates + blacklists, which writes an
OutstandingToken keyed to that missing user → HTTP 500, so the SPA could never refresh
and the 30-minute access token expired into 'token_not_valid: Token is expired'. The
custom ClaimsTokenRefreshView must re-issue an access token from the refresh token
statelessly — no rotation, no blacklist, no DB row — carrying the identity claims.
"""
from datetime import timedelta

from django.test import TestCase
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

URL = '/api/auth/token/refresh/'


def _portfolio_refresh(**over):
    """A refresh token shaped like the portfolio's — enriched claims, and a user_id that
    is NOT a Customer 360 database user (the whole point of stateless SSO)."""
    r = RefreshToken()
    r['user_id'] = 987654321            # no such row in auth_user
    r['username'] = 'sso.user'
    r['email'] = 'sso@example.com'
    r['name'] = 'SSO User'
    r['is_staff'] = False
    r['is_superuser'] = False
    r['is_active'] = True
    r['groups'] = ['relationship_manager']
    r['sales_code'] = 'SC-1'
    r['branch'] = 'HQ'
    r['segment'] = 'RETAIL'
    for k, v in over.items():
        r[k] = v
    return r


class ClaimsTokenRefreshTests(TestCase):
    def test_portfolio_token_refreshes_without_db_user(self):
        token = str(_portfolio_refresh())
        res = self.client.post(URL, data={'refresh': token}, content_type='application/json')
        self.assertEqual(res.status_code, 200)          # not 500 (the old rotation failure)
        access = res.json().get('access')
        self.assertTrue(access)
        # The new access token is valid and still carries identity/RBAC claims.
        decoded = AccessToken(access)
        self.assertEqual(decoded['username'], 'sso.user')
        self.assertEqual(decoded['sales_code'], 'SC-1')
        self.assertEqual(decoded['token_type'], 'access')

    def test_refresh_does_not_rotate_or_blacklist(self):
        token = str(_portfolio_refresh())
        self.client.post(URL, data={'refresh': token}, content_type='application/json')
        # Stateless: nothing is written to the outstanding/blacklist tables.
        self.assertEqual(OutstandingToken.objects.count(), 0)

    def test_missing_refresh_is_400(self):
        res = self.client.post(URL, data={}, content_type='application/json')
        self.assertEqual(res.status_code, 400)

    def test_garbage_refresh_is_401(self):
        res = self.client.post(URL, data={'refresh': 'not-a-jwt'}, content_type='application/json')
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json().get('code'), 'token_not_valid')

    def test_expired_refresh_is_401_not_500(self):
        r = _portfolio_refresh()
        r.set_exp(lifetime=timedelta(seconds=-10))       # already expired
        res = self.client.post(URL, data={'refresh': str(r)}, content_type='application/json')
        self.assertEqual(res.status_code, 401)

    def test_new_access_authenticates_a_protected_endpoint(self):
        access = self.client.post(
            URL, data={'refresh': str(_portfolio_refresh())}, content_type='application/json'
        ).json()['access']
        # The refreshed token is accepted by the claims auth on a real endpoint.
        res = self.client.get('/api/auth/me/', HTTP_AUTHORIZATION=f'Bearer {access}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get('username'), 'sso.user')
