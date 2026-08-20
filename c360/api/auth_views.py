"""Authentication & admin user management — mirrors the HF Group backend.

Auth is JWT (``rest_framework_simplejwt``): the SPA posts credentials to
``/auth/api/token/`` for an access + refresh pair, sends the access token as a
Bearer header, silently refreshes on 401, and blacklists the refresh token on
sign-out. Users are **admin-provisioned** — there is no self-service registration;
administrators create accounts and assign role Groups through the Users API here,
which replaces the Django admin UI.

Customer 360 is used by relationship managers and management, never through the
Django admin, so this is the one supported way to mint accounts.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from ..auth.claims import apply_claims
from ..models import BRANCH_CHOICES, OTP, SEGMENT_CHOICES
from ..otp import dev_code, issue_otp, verify_otp
from ..rbac.scoping import resolve_scope
from ..roles import ROLE_ADMIN, tier_for_groups
from .auth_serializers import (
    AdminUserSerializer,
    LogoutSerializer,
    RoleSerializer,
    SetPasswordSerializer,
)

# The signed ticket that carries a *pending* (password-verified) login between the
# two 2FA steps. It is NOT an access token — it only proves step 1 succeeded and
# expires quickly, so the emailed OTP must be entered promptly.
_MFA_SALT = 'c360.login.mfa'
_MFA_TICKET_MAX_AGE = 600  # seconds (10 min) to complete the OTP step


def _issue_tokens(user: User) -> dict:
    # Embed identity + RBAC claims so a Customer 360 token is interchangeable with a
    # portfolio-issued one — both authenticate through the same claims-based path.
    refresh = RefreshToken.for_user(user)
    apply_claims(refresh, user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


class ClaimsTokenRefreshView(APIView):
    """Stateless silent-refresh: verify the refresh token and mint a fresh access token
    carrying the same identity/RBAC claims — WITHOUT rotation or blacklisting.

    Why not the stock ``TokenRefreshView``: with ``ROTATE_REFRESH_TOKENS`` +
    ``BLACKLIST_AFTER_ROTATION`` it writes an ``OutstandingToken`` keyed to a database
    user. A portfolio-minted single-sign-on token has no such user in Customer 360's DB,
    so rotation raises a foreign-key error (HTTP 500) and the SPA can NEVER refresh —
    the 30-minute access token simply expires and every call returns
    ``token_not_valid: Token is expired``. Customer 360 already trusts the token's claims
    (see ``auth/claims.py``); here we only verify the refresh token's signature + expiry
    and re-issue an access token. ``RefreshToken.access_token`` copies every non-reserved
    claim, so identity/RBAC ride along. Works identically for a portfolio token and a
    Customer-360-issued one (both are enriched with the same claims), touches no database
    row that could be missing, and cannot 500. The response is ``{"access": ...}`` only;
    the client keeps its existing (still-valid, up to 7-day) refresh token."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        raw = (request.data or {}).get('refresh')
        if not raw:
            return Response({'detail': 'A refresh token is required.', 'code': 'token_not_valid'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            refresh = RefreshToken(raw)   # verifies signature, expiry, type, blacklist
        except TokenError:
            return Response({'detail': 'Token is invalid or expired', 'code': 'token_not_valid'},
                            status=status.HTTP_401_UNAUTHORIZED)
        return Response({'access': str(refresh.access_token)})


def _mask_email(email: str) -> str:
    if not email or '@' not in email:
        return 'your email'
    name, domain = email.split('@', 1)
    shown = name[0] + '***' if name else '***'
    return f'{shown}@{domain}'


def _err(status_code: int, detail: str) -> Response:
    return Response({'error': {'status': status_code, 'detail': detail}}, status=status_code)


class LoginStartView(APIView):
    """Step 1 of sign-in: verify the password, then email a one-time code.

    Returns a signed ``ticket`` (not a token) the client sends back with the code.
    When 2FA is disabled (``C360_LOGIN_MFA=false``) this issues tokens directly."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request: Request):
        username = str(request.data.get('username', '')).strip()
        password = str(request.data.get('password', ''))
        if not username or not password:
            return _err(400, 'Username and password are required.')
        user = authenticate(request, username=username, password=password)
        if user is None:
            return _err(401, 'Wrong username or password.')
        if not user.is_active:
            return _err(403, 'This account is inactive. Contact your administrator.')

        if not settings.C360_LOGIN_MFA:
            return Response({'mfa_required': False, **_issue_tokens(user)})

        code = issue_otp(user, OTP.PURPOSE_LOGIN)
        ticket = signing.dumps({'uid': user.id}, salt=_MFA_SALT)
        return Response({'mfa_required': True, 'ticket': ticket,
                         'sent_to': _mask_email(user.email), **dev_code(code)})


class LoginVerifyView(APIView):
    """Step 2 of sign-in: exchange the ticket + emailed code for a JWT pair."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_verify'

    def post(self, request: Request):
        ticket = request.data.get('ticket')
        code = request.data.get('otp')
        if not ticket or not code:
            return _err(400, 'The sign-in code is required.')
        try:
            data = signing.loads(ticket, salt=_MFA_SALT, max_age=_MFA_TICKET_MAX_AGE)
        except signing.SignatureExpired:
            return _err(400, 'This sign-in timed out. Please start again.')
        except signing.BadSignature:
            return _err(400, 'Invalid sign-in. Please start again.')
        user = User.objects.filter(pk=data.get('uid'), is_active=True).first()
        if not user:
            return _err(401, 'Account unavailable. Please start again.')
        if not verify_otp(user, OTP.PURPOSE_LOGIN, str(code)):
            return _err(400, 'That code is wrong or has expired.')
        return Response(_issue_tokens(user))


class PasswordResetRequestView(APIView):
    """Forgot password (step 1): email a reset code. Always returns 200 so the
    endpoint never reveals which usernames/emails exist."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_request'

    def post(self, request: Request):
        ident = str(request.data.get('username') or request.data.get('email') or '').strip()
        resp = {'detail': 'If that account exists, a reset code has been sent to its email.'}
        if ident:
            user = User.objects.filter(
                Q(username__iexact=ident) | Q(email__iexact=ident), is_active=True).first()
            if user:
                code = issue_otp(user, OTP.PURPOSE_RESET)
                resp.update(dev_code(code))
        return Response(resp)


class PasswordResetConfirmView(APIView):
    """Forgot password (step 2): verify the code and set a new password."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_verify'

    def post(self, request: Request):
        ident = str(request.data.get('username') or request.data.get('email') or '').strip()
        code = str(request.data.get('code', '')).strip()
        new_password = str(request.data.get('new_password', ''))
        if not ident or not code or not new_password:
            return _err(400, 'Account, code and new password are all required.')
        user = User.objects.filter(
            Q(username__iexact=ident) | Q(email__iexact=ident), is_active=True).first()
        if not user or not verify_otp(user, OTP.PURPOSE_RESET, code):
            return _err(400, 'That reset code is wrong or has expired.')
        try:
            validate_password(new_password, user)
        except ValidationError as exc:
            return _err(400, ' '.join(exc.messages))
        user.set_password(new_password)
        user.save(update_fields=['password'])
        return Response({'detail': 'Password updated. You can now sign in.'})


def _me_payload(user: User, request: Request | None = None) -> dict:
    """Identity + roles + resolved scope — the SPA's boot/gating payload."""
    groups = list(user.groups.values_list('name', flat=True))
    # A bare superuser carries no role group; surface them as 'admin' rather than the
    # group-derived default ('officer') so the UI labels them correctly.
    tier = ROLE_ADMIN if user.is_superuser else tier_for_groups(groups)
    profile = getattr(user, 'profile', None)
    scope = resolve_scope(request) if request is not None else None
    is_admin = bool(user.is_staff or user.is_superuser)
    return {
        'username': user.username,
        'name': (user.get_full_name() or user.username),
        'email': user.email,
        'groups': groups,
        'role_tier': tier,
        'is_admin': is_admin,
        'profile': {
            'sales_code': getattr(profile, 'sales_code', None),
            'branch': getattr(profile, 'branch', None),
            'segment': getattr(profile, 'segment', None),
        },
        'scope': None if scope is None else {
            'role': scope.role,
            'whole_book': scope.is_whole_book,
            'can_view_portfolio': scope.can_view_portfolio(),
        },
    }


class MeView(APIView):
    """Current identity for the signed-in token, or 401."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return Response(_me_payload(request.user, request))


class LogoutAPIView(generics.GenericAPIView):
    """Blacklist the refresh token so it can't be exchanged again."""

    permission_classes = [IsAuthenticated]
    serializer_class = LogoutSerializer

    def post(self, request: Request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data['refresh']).blacklist()
        except Exception:
            pass  # already expired/blacklisted — sign-out is idempotent
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Admin user management — the clean replacement for the Django admin UI.
# Only administrators (staff or superuser) may manage users.
# ===========================================================================
class IsAdministrator(BasePermission):
    """Staff or superuser may manage users."""

    message = 'Administrator privileges are required.'

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.is_active and (u.is_staff or u.is_superuser))


class AdminUserListCreateView(ListCreateAPIView):
    """GET: list users (with roles + profile). POST: create a user and assign roles.

    When no password is supplied the serializer generates one and surfaces it once
    as ``generated_password`` so the admin can share it out-of-band (we never email
    passwords). The new user changes it after first sign-in (later phase)."""

    permission_classes = [IsAuthenticated, IsAdministrator]
    serializer_class = AdminUserSerializer

    def get_queryset(self):
        qs = (User.objects.all().select_related('profile')
              .prefetch_related('groups').order_by('username'))
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(username__icontains=search) | Q(email__icontains=search)
                | Q(first_name__icontains=search) | Q(last_name__icontains=search))
        role = self.request.query_params.get('role')
        if role:
            qs = qs.filter(groups__name=role)
        active = self.request.query_params.get('is_active')
        if active in ('true', 'false'):
            qs = qs.filter(is_active=(active == 'true'))
        return qs


class AdminUserDetailView(RetrieveUpdateDestroyAPIView):
    """GET/PATCH a user. DELETE deactivates (soft) rather than destroying."""

    permission_classes = [IsAuthenticated, IsAdministrator]
    serializer_class = AdminUserSerializer
    queryset = User.objects.all().select_related('profile').prefetch_related('groups')

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])


class AdminSetPasswordView(APIView):
    """POST a new password for a user (or omit to auto-generate one)."""

    permission_classes = [IsAuthenticated, IsAdministrator]

    def post(self, request: Request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw = serializer.save(user)
        return Response({'detail': 'Password updated.', 'password': raw}, status=status.HTTP_200_OK)


class RoleListView(generics.ListAPIView):
    """List assignable roles (Django Groups) for the Users screen dropdown."""

    permission_classes = [IsAuthenticated, IsAdministrator]
    serializer_class = RoleSerializer
    pagination_class = None

    def get_queryset(self):
        return Group.objects.all().order_by('name')


class UserMetaChoicesView(APIView):
    """Branch & segment option lists for the Users screen dropdowns — the canonical
    Profile choice lists, so the form can never store a value RBAC would reject."""

    permission_classes = [IsAuthenticated, IsAdministrator]

    def get(self, request: Request):
        return Response({
            'branches': [value for value, _label in BRANCH_CHOICES],
            'segments': [value for value, _label in SEGMENT_CHOICES],
        })
