"""Auth flow — JWT login, scope from role Groups, admin user management.

Mirrors the HF Group model: users are admin-provisioned (role = Django Group),
authenticate for a JWT pair, and their book scope is resolved server-side from
their tier + Profile. Mock mode: seed officer SC-1077 services HF-100571.
"""
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from c360.models import OTP, Profile
from c360.warehouse.factory import get_gateway


def _pin_mock():
    settings.C360['DATA_MODE'] = 'mock'
    get_gateway.cache_clear()
    cache.clear()   # reset per-scope throttle counters between tests


def _login(client, username, password):
    """Drive the two-step 2FA sign-in and return the JWT pair (tokens).

    Tests run with DEBUG=False so the OTP isn't echoed; read it from the DB instead."""
    start = client.post('/api/auth/login/', {'username': username, 'password': password}, format='json')
    assert start.status_code == 200, start.content
    body = start.json()
    if not body.get('mfa_required'):
        return body   # 2FA disabled → tokens already issued
    code = OTP.objects.filter(user__username=username, purpose=OTP.PURPOSE_LOGIN).latest('created_at').code
    verify = client.post('/api/auth/login/verify/', {'ticket': body['ticket'], 'otp': code}, format='json')
    assert verify.status_code == 200, verify.content
    return verify.json()


def _make_user(username, password='Kanjo!2026x', *, group=None, sales_code=None,
               email='', is_staff=False, is_superuser=False):
    user = User.objects.create_user(username=username, password=password, email=email,
                                    is_staff=is_staff, is_superuser=is_superuser)
    if group:
        g, _ = Group.objects.get_or_create(name=group)
        user.groups.add(g)
    if sales_code is not None:
        # Mutate via the cached related accessor so resolve_scope (which reads
        # user.profile on this same instance) sees the value, not a stale cache.
        user.profile.sales_code = sales_code
        user.profile.save()
    return user


class JwtFlowTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()
        _make_user('rm.brian', group='c360_rm', sales_code='SC-1077', email='brian@hfcb.co.ke')

    def test_login_2fa_me_and_logout(self):
        pair = _login(self.c, 'rm.brian', 'Kanjo!2026x')
        access, refresh = pair['access'], pair['refresh']

        self.c.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        me = self.c.get('/api/auth/me/')
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['role_tier'], 'officer')
        self.assertEqual(me.json()['profile']['sales_code'], 'SC-1077')
        self.assertEqual(me.json()['scope']['role'], 'rm')

        out = self.c.post('/api/auth/logout/', {'refresh': refresh}, format='json')
        self.assertEqual(out.status_code, 204)
        again = self.c.post('/api/auth/token/refresh/', {'refresh': refresh}, format='json')
        self.assertEqual(again.status_code, 401)

    def test_login_step1_returns_ticket_not_token(self):
        r = self.c.post('/api/auth/login/', {'username': 'rm.brian', 'password': 'Kanjo!2026x'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['mfa_required'])
        self.assertIn('ticket', r.json())
        self.assertNotIn('access', r.json())   # no token until the code is verified

    def test_wrong_code_rejected(self):
        start = self.c.post('/api/auth/login/', {'username': 'rm.brian', 'password': 'Kanjo!2026x'}, format='json')
        r = self.c.post('/api/auth/login/verify/', {'ticket': start.json()['ticket'], 'otp': '000000'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_bad_credentials_rejected(self):
        r = self.c.post('/api/auth/login/', {'username': 'rm.brian', 'password': 'nope'}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_me_requires_token(self):
        self.assertEqual(self.c.get('/api/auth/me/').status_code, 401)


class PasswordResetTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()
        _make_user('rm.faith', group='c360_rm', email='faith@hfcb.co.ke')

    def test_reset_flow_sets_new_password(self):
        req = self.c.post('/api/auth/password-reset/', {'username': 'rm.faith'}, format='json')
        self.assertEqual(req.status_code, 200)
        code = OTP.objects.filter(user__username='rm.faith', purpose=OTP.PURPOSE_RESET).latest('created_at').code
        conf = self.c.post('/api/auth/password-reset/confirm/',
                           {'username': 'rm.faith', 'code': code, 'new_password': 'Brandnew!2026x'}, format='json')
        self.assertEqual(conf.status_code, 200, conf.content)
        # The new password now signs in; the old one no longer authenticates.
        self.assertEqual(
            self.c.post('/api/auth/login/', {'username': 'rm.faith', 'password': 'Kanjo!2026x'}, format='json').status_code, 401)
        pair = _login(self.c, 'rm.faith', 'Brandnew!2026x')
        self.assertIn('access', pair)

    def test_reset_request_never_reveals_unknown_account(self):
        r = self.c.post('/api/auth/password-reset/', {'username': 'nobody'}, format='json')
        self.assertEqual(r.status_code, 200)   # same generic response

    def test_wrong_reset_code_rejected(self):
        self.c.post('/api/auth/password-reset/', {'username': 'rm.faith'}, format='json')
        r = self.c.post('/api/auth/password-reset/confirm/',
                        {'username': 'rm.faith', 'code': '000000', 'new_password': 'Whatever!2026x'}, format='json')
        self.assertEqual(r.status_code, 400)


class ScopeTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def test_rm_can_view_any_customer_but_not_level1(self):
        # It's Customer 360: an RM looks up ANY customer, not only their own book.
        user = _make_user('rm.brian', group='c360_rm', sales_code='SC-1077')
        self.c.force_authenticate(user=user)
        self.assertEqual(self.c.get('/api/customers/HF-100238/').status_code, 200)   # not their book
        self.assertEqual(self.c.get('/api/customers/HF-100571/').status_code, 200)
        # …but the whole-book Level 1 portfolio + worklist stay management-only.
        self.assertEqual(self.c.get('/api/portfolio/overview/?period=30D').status_code, 403)
        self.assertEqual(self.c.get('/api/portfolio/worklist/').status_code, 403)

    def test_management_sees_whole_book_and_level1(self):
        user = _make_user('analyst', group='c360_management')
        self.c.force_authenticate(user=user)
        self.assertEqual(self.c.get('/api/customers/HF-100238/').status_code, 200)
        self.assertEqual(self.c.get('/api/portfolio/overview/?period=30D').status_code, 200)


class AdminUserApiTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()
        Group.objects.get_or_create(name='c360_rm')   # assignable role must exist
        self.admin = _make_user('admin', is_staff=True, is_superuser=True)

    def test_admin_creates_user_with_generated_password(self):
        self.c.force_authenticate(user=self.admin)
        r = self.c.post('/api/auth/users/', {
            'username': 'rm.faith', 'email': 'faith@hfcb.co.ke', 'first_name': 'Faith',
            'groups': ['c360_rm'], 'sales_code': 'SC-1108', 'branch': 'THIKA BRANCH',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertTrue(body['generated_password'])           # surfaced once
        self.assertEqual(body['role_tier'], 'officer')
        self.assertEqual(body['sales_code'], 'SC-1108')
        # The new user can authenticate with the generated password (step 1 accepts it).
        login = self.c.post('/api/auth/login/', {
            'username': 'rm.faith', 'password': body['generated_password']}, format='json')
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()['mfa_required'])

    def test_admin_set_password(self):
        target = _make_user('rm.temp', group='c360_rm')
        self.c.force_authenticate(user=self.admin)
        r = self.c.post(f'/api/auth/users/{target.pk}/set-password/', {'password': 'NewPass!2026x'}, format='json')
        self.assertEqual(r.status_code, 200)
        target.refresh_from_db()
        self.assertTrue(target.check_password('NewPass!2026x'))

    def test_non_admin_cannot_manage_users(self):
        plain = _make_user('rm.brian', group='c360_rm')
        self.c.force_authenticate(user=plain)
        self.assertEqual(self.c.get('/api/auth/users/').status_code, 403)

    def test_staff_admin_cannot_mint_superuser(self):
        # A staff (not superuser) admin is caught by the privilege-escalation guard.
        staff = _make_user('desk.admin', is_staff=True, is_superuser=False)
        self.c.force_authenticate(user=staff)
        r = self.c.post('/api/auth/users/', {
            'username': 'sneaky', 'is_superuser': True, 'is_staff': True}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        created = User.objects.get(username='sneaky')
        self.assertFalse(created.is_superuser)
        self.assertFalse(created.is_staff)

    def test_soft_delete_deactivates(self):
        target = _make_user('rm.leaver', group='c360_rm')
        self.c.force_authenticate(user=self.admin)
        r = self.c.delete(f'/api/auth/users/{target.pk}/')
        self.assertEqual(r.status_code, 204)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_roles_and_meta_endpoints(self):
        Group.objects.get_or_create(name='c360_rm')
        self.c.force_authenticate(user=self.admin)
        roles = self.c.get('/api/auth/roles/')
        self.assertEqual(roles.status_code, 200)
        self.assertTrue(any(r['name'] == 'c360_rm' for r in roles.json()))
        meta = self.c.get('/api/auth/user-meta/')
        self.assertEqual(meta.status_code, 200)
        self.assertIn('THIKA BRANCH', meta.json()['branches'])
