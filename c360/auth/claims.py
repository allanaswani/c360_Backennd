"""Stateless, claims-based authentication — trust the portfolio's identity.

Customer 360 and the HF portfolio backend share one ``SECRET_KEY`` (the SimpleJWT
``SIGNING_KEY``), so a token minted by either validates in the other. The portfolio
is the authoritative identity provider — it owns the accounts, role Groups and the
Profile (branch / segment / sales_code) — and embeds those into the token
(``apps/authentication/token_serializers.py``). Here we read them back and build a
lightweight *acting user* from the claims alone, with **no database round-trip**.

Why claims and not a shared DB: the portfolio's user store is PostgreSQL on a host
this box can't always reach, and coupling the two databases would be fragile. A
short-lived access token that already carries identity + roles is the robust,
standard resource-server pattern — Customer 360 stays runnable and testable on its
own, yet accepts a portfolio single-sign-on token unchanged.

Keep the claim shape in sync with the portfolio's ``embed_identity_claims`` — the
two must agree field-for-field. Customer 360's own login (``api/auth_views.py``)
embeds the identical claims via :func:`apply_claims`, so both token sources flow
through one authentication path.
"""
from __future__ import annotations

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.settings import api_settings


def apply_claims(token, user):
    """Embed the shared identity + RBAC claims onto ``token`` (a RefreshToken).

    SimpleJWT copies these onto the derived access token, so the Bearer token the
    SPA sends carries them too. Mirrors the portfolio's ``embed_identity_claims``.
    """
    profile = getattr(user, 'profile', None)
    token['username'] = user.username
    token['email'] = user.email or ''
    token['name'] = user.get_full_name() or user.username
    token['is_staff'] = bool(user.is_staff)
    token['is_superuser'] = bool(user.is_superuser)
    token['is_active'] = bool(getattr(user, 'is_active', True))
    token['groups'] = list(user.groups.values_list('name', flat=True))
    token['sales_code'] = getattr(profile, 'sales_code', None)
    token['branch'] = getattr(profile, 'branch', None)
    token['segment'] = getattr(profile, 'segment', None)
    return token


class _ClaimGroups:
    """Minimal stand-in for ``user.groups`` backed by a list of group names.

    Supports only the access pattern Customer 360 actually uses —
    ``user.groups.values_list('name', flat=True)`` — plus iteration, so RBAC
    resolution never touches the database for a claims-authenticated user.
    """

    def __init__(self, names):
        self._names = [str(n) for n in (names or [])]

    def values_list(self, *fields, flat=False):
        if flat:
            return list(self._names)
        return [(n,) for n in self._names]

    def __iter__(self):
        return iter(self._names)

    def all(self):
        return list(self._names)


class _ClaimProfile:
    """The subset of ``Profile`` the app reads: sales_code / branch / segment."""

    def __init__(self, sales_code=None, branch=None, segment=None):
        self.sales_code = sales_code
        self.branch = branch
        self.segment = segment


class ClaimsUser:
    """An authenticated user reconstructed from a verified token's claims.

    Exposes exactly the interface Customer 360 relies on (``rbac.scoping`` and the
    ``/auth/me/`` payload): identity fields, ``is_staff`` / ``is_superuser``, a
    ``.groups`` accessor and a ``.profile``. It is not a Django model instance and
    is never saved — it exists only for the life of the request.
    """

    is_authenticated = True
    is_anonymous = False

    def __init__(self, token):
        self._token = token
        self.id = token.get(api_settings.USER_ID_CLAIM)
        self.pk = self.id
        self.username = token.get('username') or ''
        self.email = token.get('email') or ''
        self._name = token.get('name') or self.username
        self.is_staff = bool(token.get('is_staff', False))
        self.is_superuser = bool(token.get('is_superuser', False))
        # A token is only issued to an active user; revocation of the longer-lived
        # session is handled by refresh-token rotation/blacklist, not here.
        self.is_active = bool(token.get('is_active', True))
        self.groups = _ClaimGroups(token.get('groups'))
        self.profile = _ClaimProfile(
            token.get('sales_code'), token.get('branch'), token.get('segment'))

    def get_full_name(self):
        return self._name

    def get_short_name(self):
        return self.username

    def get_username(self):
        return self.username

    def __str__(self):
        return self.username


class ClaimsJWTAuthentication(JWTAuthentication):
    """JWT auth that builds a stateless :class:`ClaimsUser` from enriched tokens.

    Enriched tokens (issued by the portfolio *or* by Customer 360's own login)
    carry a ``username`` claim → we build the acting user from the token, so a
    portfolio single-sign-on token authenticates here with no shared user DB. A
    bare token without identity claims falls back to the standard DB-backed lookup,
    keeping any legacy/local token working.
    """

    def get_user(self, validated_token):
        if validated_token.get('username') is not None:
            return ClaimsUser(validated_token)
        return super().get_user(validated_token)
