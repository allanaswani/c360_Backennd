"""Keeping an adopted portfolio session alive.

A session adopted from the portfolio's cookie carries an access token that lives
about thirty minutes inside a cookie the browser keeps for seven days. Anyone
returning after a break therefore arrives holding an EXPIRED access token and a
perfectly good refresh token, and the client now refreshes before spending a call
on the dead one. These pin the two server behaviours that depends on.
"""
import time

import jwt
from django.conf import settings
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from c360.auth.claims import apply_claims


class PortfolioRefreshTests(TestCase):
    """A session adopted from the portfolio holds a portfolio-minted REFRESH token.
    The client now refreshes proactively once the access token has expired, so this
    endpoint has to accept a refresh token for a user with no local account."""

    def _portfolio_refresh(self):
        tok = RefreshToken()
        tok['user_id'] = 99001
        tok['username'] = 'brian.rm'
        tok['name'] = 'Brian RM'
        tok['email'] = ''
        tok['is_staff'] = False
        tok['is_superuser'] = False
        tok['groups'] = ['c360_rm']
        tok['sales_code'] = 'SC-1077'
        tok['branch'] = None
        tok['segment'] = None
        return str(tok)

    def test_refresh_works_for_a_user_with_no_local_account(self):
        c = APIClient()
        r = c.post('/api/auth/token/refresh/', {'refresh': self._portfolio_refresh()}, format='json')
        print('REFRESH STATUS:', r.status_code)
        print('BODY:', r.content[:400])
        self.assertEqual(r.status_code, 200)
        self.assertIn('access', r.json())

    def test_refreshed_access_token_still_authenticates(self):
        c = APIClient()
        r = c.post('/api/auth/token/refresh/', {'refresh': self._portfolio_refresh()}, format='json')
        self.assertEqual(r.status_code, 200)
        access = r.json()['access']
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        me = c.get('/api/auth/me/')
        print('ME STATUS:', me.status_code, me.content[:200])
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['username'], 'brian.rm')


class ExpiredAccessTokenTests(TestCase):
    def _expired(self):
        key = settings.SIMPLE_JWT.get('SIGNING_KEY') or settings.SECRET_KEY
        now = int(time.time())
        return jwt.encode({
            'token_type': 'access', 'exp': now - 7618, 'iat': now - 9000,
            'jti': 'expired-1', 'user_id': 99001, 'username': 'brian.rm',
            'name': 'Brian RM', 'email': '', 'is_staff': False,
            'is_superuser': False, 'groups': ['c360_rm'],
            'sales_code': 'SC-1077', 'branch': None, 'segment': None,
        }, key, algorithm=settings.SIMPLE_JWT.get('ALGORITHM', 'HS256'))

    def test_expired_token_is_401_and_json(self):
        """401 specifically, and a JSON envelope. The SPA's whole recovery path is
        gated on the status: anything else (a 500 from a hiccup in the auth stack,
        an HTML error page) leaves a valid session with no route back and bounces
        the user to the login screen."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {self._expired()}')
        r = c.get('/api/auth/me/')
        self.assertEqual(r.status_code, 401)
        self.assertIn('application/json', r.get('Content-Type', ''))
        self.assertIn('detail', r.json().get('error', {}))
