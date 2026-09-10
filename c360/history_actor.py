"""Attributing a change to the person who made it, under stateless SSO auth.

``simple_history`` normally stores the acting user as a foreign key to
``auth.User``. Customer 360 cannot always do that: it accepts portfolio
single-sign-on tokens and reconstructs the caller as a
:class:`~c360.auth.claims.ClaimsUser` built from the token's claims, with no
database round-trip and — for a portfolio-only administrator — no local row to
point a foreign key at. Assigning that object to the FK raises, which is what
made the first attempt at this fail outright rather than silently.

So attribution happens in two steps, and the feed never has to guess:

1. :func:`acting_user` resolves a **local** ``User`` by *username*. Username is
   the identity the two products genuinely share (see the SSO claim contract);
   the numeric ``id`` in the token is the portfolio's primary key and could
   collide with an unrelated local account, so it is deliberately not used.
2. When that resolves to nothing but we do know who was acting, the
   ``pre_create_historical_record`` hook stamps the username into
   ``history_change_reason`` behind the ``actor:`` prefix. :mod:`c360.changes`
   reads it back. The alternative — dropping the name — would leave an audit
   trail that says an account was granted admin by nobody.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.dispatch import receiver
from simple_history.signals import pre_create_historical_record

#: Marker for an actor we could name but could not link to a local account.
ACTOR_PREFIX = 'actor:'


def acting_user(request, **kwargs):
    """The local ``User`` behind this request, or ``None``.

    Never raises and never hits the database when the caller is already a real
    ``User`` instance — the common case for Customer 360's own login.
    """
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    if isinstance(user, User) and user.pk:
        return user
    username = getattr(user, 'username', '') or ''
    if not username:
        return None
    return User.objects.filter(username=username).first()


def actor_name(request) -> str:
    user = getattr(request, 'user', None) if request else None
    if user is None or not getattr(user, 'is_authenticated', False):
        return ''
    return (getattr(user, 'username', '') or '')[:100]


@receiver(pre_create_historical_record)
def stamp_external_actor(sender, **kwargs):
    """Record the acting username when it could not be linked to a local account.

    Only fills an empty change reason — application code that sets its own reason
    keeps it, and a change already attributed by foreign key is left alone.
    """
    history_instance = kwargs.get('history_instance')
    if history_instance is None or getattr(history_instance, 'history_user_id', None):
        return
    if (history_instance.history_change_reason or '').strip():
        return
    name = actor_name(_current_request())
    if name:
        history_instance.history_change_reason = f'{ACTOR_PREFIX}{name}'


def _current_request():
    """The request simple_history's middleware stashed for this thread, if any."""
    from simple_history.models import HistoricalRecords
    return getattr(HistoricalRecords.context, 'request', None)
