"""Cross-system single sign-on — Customer 360 trusts portfolio-issued tokens.

The portfolio backend mints a JWT carrying identity + RBAC claims, signed with the
shared key. Customer 360 must authenticate that token and resolve the caller's book
scope from the claims alone — with **no matching row in its own user table** (the
portfolio owns the accounts). These tests mint such tokens directly (standing in for
the portfolio) and assert the resource-server behaviour end-to-end.
"""
from __future__ import annotations

import time

import jwt as pyjwt
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from c360.warehouse.factory import get_gateway


def _pin_mock():
    settings.C360['DATA_MODE'] = 'mock'
    get_gateway.cache_clear()
    cache.clear()


def _portfolio_token(*, user_id, username, groups, is_staff=False, is_superuser=False,
                     sales_code=None, branch=None, segment=None, name=None, email=''):
    """A token as the portfolio would mint it — signed with the shared key, carrying
    the identity + RBAC claims, for a user that need NOT exist in Customer 360."""
    tok = AccessToken()
    tok['user_id'] = user_id
    tok['username'] = username
    tok['name'] = name or username
    tok['email'] = email
    tok['is_staff'] = is_staff
    tok['is_superuser'] = is_superuser
    tok['groups'] = list(groups)
    tok['sales_code'] = sales_code
    tok['branch'] = branch
    tok['segment'] = segment
    return str(tok)


class PortfolioSsoTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def test_portfolio_management_token_authenticates_with_no_local_user(self):
        # A CEO on the portfolio — no such account exists in Customer 360's DB.
        token = _portfolio_token(user_id=99001, username='jane.ceo', groups=['ceo'],
                                 name='Jane CEO', email='jane@hfcb.co.ke')
        self.assertFalse(User.objects.filter(username='jane.ceo').exists())
        self.assertFalse(User.objects.filter(pk=99001).exists())

        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        me = self.c.get('/api/auth/me/')
        self.assertEqual(me.status_code, 200, me.content)
        body = me.json()
        self.assertEqual(body['username'], 'jane.ceo')
        self.assertEqual(body['role_tier'], 'admin')          # ceo → admin tier
        self.assertEqual(body['scope']['role'], 'management')
        self.assertTrue(body['scope']['can_view_portfolio'])

        # Whole-book Level 1 is reachable with this token — no C360 account needed.
        self.assertEqual(self.c.get('/api/portfolio/overview/?period=30D').status_code, 200)
        # …and still nothing was written to the local user table.
        self.assertFalse(User.objects.filter(username='jane.ceo').exists())

    def test_portfolio_rm_token_sees_customer_not_level1(self):
        token = _portfolio_token(user_id=99002, username='brian.rm',
                                 groups=['branch_portfolio'], sales_code='SC-1077')
        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        me = self.c.get('/api/auth/me/')
        self.assertEqual(me.status_code, 200, me.content)
        self.assertEqual(me.json()['role_tier'], 'officer')   # branch_portfolio → officer
        self.assertEqual(me.json()['scope']['role'], 'rm')
        self.assertEqual(me.json()['profile']['sales_code'], 'SC-1077')

        # An RM can open any customer's 360…
        self.assertEqual(self.c.get('/api/customers/HF-100238/').status_code, 200)
        # …but the whole-book Level 1 stays management-only.
        self.assertEqual(self.c.get('/api/portfolio/overview/?period=30D').status_code, 403)

    def test_superuser_claim_grants_management(self):
        token = _portfolio_token(user_id=99003, username='root', groups=[], is_superuser=True)
        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        me = self.c.get('/api/auth/me/')
        self.assertEqual(me.status_code, 200, me.content)
        self.assertEqual(me.json()['role_tier'], 'admin')
        self.assertTrue(me.json()['is_admin'])
        self.assertEqual(me.json()['scope']['role'], 'management')

    def test_token_signed_with_wrong_key_is_rejected(self):
        # A token NOT signed with the shared key must not authenticate — this is what
        # makes cross-system trust safe.
        now = int(time.time())
        payload = {
            'token_type': 'access', 'exp': now + 300, 'iat': now, 'jti': 'forged',
            'user_id': 99004, 'username': 'mallory', 'groups': ['ceo'],
        }
        forged = pyjwt.encode(payload, 'not-the-shared-key', algorithm='HS256')
        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {forged}')
        self.assertEqual(self.c.get('/api/auth/me/').status_code, 401)

    def test_unrecognised_group_falls_back_to_least_privilege(self):
        # A group C360 doesn't know about must not accidentally grant management.
        token = _portfolio_token(user_id=99005, username='temp', groups=['some_new_role'])
        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        me = self.c.get('/api/auth/me/')
        self.assertEqual(me.status_code, 200, me.content)
        self.assertEqual(me.json()['role_tier'], 'officer')
        self.assertEqual(self.c.get('/api/portfolio/overview/?period=30D').status_code, 403)
