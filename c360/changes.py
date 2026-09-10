"""Reads the change audit — who edited what, and what the value was before.

Customer 360's existing :class:`~c360.models.AuditEvent` trail answers *what was
accessed*: every API call and UI interaction, with the route, status and latency.
That is an activity log, and it deliberately says nothing about state, because
almost everything this app serves is read-only warehouse data.

The handful of things it can *change*, though, are the sensitive ones: who holds
an account, what role they hold, which book (``sales_code``) an RM is pointed at,
and the recommendation outcomes that become the model's training labels. Those
are tracked by ``simple_history`` shadow tables (see :mod:`c360.models`), and this
module reads them back as one merged feed — enumerate the historical models, pull
each one's recent rows, merge-sort by time, and diff every update against its
predecessor so the feed says *which field changed and from what*, not merely that
"something was edited".

The approach mirrors ``apps/observability/audit.py`` in the HF Group backend, so
an administrator moving between the two products reads the same kind of feed.
"""
from __future__ import annotations

from django.apps import apps as django_apps
from django.db import DatabaseError

from .history_actor import ACTOR_PREFIX

ACTION_LABELS = {'+': 'created', '~': 'updated', '-': 'deleted'}

# Never diff or display these — they are the audit bookkeeping itself.
_HISTORY_META = {
    'history_id', 'history_date', 'history_change_reason',
    'history_type', 'history_user', 'history_user_id', 'history_relation',
}

# Fields that must never reach the feed even though they are tracked. `password`
# is already excluded from the history table itself (see c360.models); this is the
# second belt in case a future model adds a secret-bearing field.
_REDACTED_FIELDS = {'password', 'token', 'secret', 'code'}

# Friendlier labels than Django's verbose_name for the models we track.
_MODEL_LABELS = {
    'user': 'User account',
    'profile': 'RM scope & allocation',
    'recommendationfeedback': 'Recommendation outcome',
}


def auditable_models() -> list[dict]:
    """Every model in the project that keeps history, sorted for display."""
    out = []
    for model in django_apps.get_models():
        manager = getattr(model, 'history', None)
        history_model = getattr(manager, 'model', None)
        if history_model is None or not hasattr(history_model, 'history_type'):
            continue
        name = model._meta.model_name
        out.append({
            'app_label': model._meta.app_label,
            'model': name,
            'verbose': _MODEL_LABELS.get(name, str(model._meta.verbose_name).title()),
            'history_model': history_model,
            'model_class': model,
        })
    out.sort(key=lambda r: (r['app_label'], r['model']))
    return out


def describe(record, entry) -> str:
    """A human label for the record this history row belongs to.

    ``__str__`` on a historical row is simple_history's own "<obj> as of <date>",
    so the underlying model's ``__str__`` is used where it can be reconstructed,
    and the primary key otherwise. A stale foreign key must never break the feed.
    """
    for get in (lambda: str(record.instance), lambda: str(record)):
        try:
            text = get()
        except Exception:                                   # noqa: BLE001
            continue
        if text:
            return text[:200]
    return str(getattr(record, entry['model_class']._meta.pk.attname, '') or '')


def changed_fields(record, model_class=None) -> list[dict]:
    """``[{field, label, old, new}]`` for an update; ``[]`` for a create or delete.

    A create has nothing to compare against and a delete's "change" is the deletion
    itself, so only ``~`` rows are diffed.
    """
    if record.history_type != '~':
        return []
    try:
        previous = record.prev_record
    except (DatabaseError, AttributeError):
        return []
    if previous is None:
        return []
    try:
        delta = record.diff_against(previous, excluded_fields=tuple(_HISTORY_META))
    except Exception:                                       # noqa: BLE001
        # A schema change can make an old row undiffable; that must not cost us
        # the rest of the feed.
        return []
    return [
        {
            'field': change.field,
            'label': _FIELD_LABELS.get(change.field, change.field.replace('_', ' ').capitalize()),
            'old': _render(change.field, change.old, model_class),
            'new': _render(change.field, change.new, model_class),
        }
        for change in delta.changes
    ]


# Field names that read badly as "Is staff" / "Sales code".
_FIELD_LABELS = {
    'is_staff': 'Staff access',
    'is_superuser': 'Superuser',
    'is_active': 'Account active',
    'groups': 'Roles',
    'sales_code': 'Sales code (book)',
    'kra_pin_status': 'KRA PIN',
}


def _render(field: str, value, model_class=None) -> str | None:
    if field in _REDACTED_FIELDS:
        return '••••••'
    if value is None:
        return None
    if value is True or value is False:
        return 'yes' if value else 'no'
    # A many-to-many diff arrives as the through-table rows —
    # ``[{'user': 5, 'group': 11}]`` — which is meaningless to a reader. Resolve
    # it to the names of the related objects, which is what the change actually was.
    if isinstance(value, list):
        names = _m2m_names(field, value, model_class)
        if names is not None:
            return ', '.join(names) if names else 'none'
    return str(value)[:300]


def _m2m_names(field: str, rows: list, model_class) -> list[str] | None:
    """Names of the related objects in a tracked m2m diff, or ``None`` if this
    isn't one we can resolve (in which case the caller falls back to raw text)."""
    if model_class is None or not all(isinstance(r, dict) for r in rows):
        return None
    try:
        related = model_class._meta.get_field(field).related_model
    except Exception:                                       # noqa: BLE001
        return None
    if related is None:
        return None
    own = model_class._meta.model_name
    ids = []
    for row in rows:
        for key, val in row.items():
            if key != own:
                ids.append(val)
    if not ids:
        return []
    try:
        objects = related.objects.filter(pk__in=ids)
        found = {obj.pk: str(obj) for obj in objects}
    except DatabaseError:
        return None
    # An id with no surviving row is reported as the id, never dropped — a deleted
    # role is still part of what changed.
    return sorted(found.get(i, f'#{i}') for i in ids)


def _actor(record) -> tuple[str | None, str | None, str, bool]:
    """``(display name, username, reason, external)`` for one history row.

    A change made by a portfolio SSO identity has no local account to hang a
    foreign key on, so :mod:`c360.history_actor` stamps the username into the
    change reason behind an ``actor:`` prefix. Unpick that here so the feed shows
    a name either way, and flags which ones are external.
    """
    user = record.history_user
    reason = record.history_change_reason or ''
    if user is not None:
        return (user.get_full_name() or user.username), user.username, reason, False
    if reason.startswith(ACTOR_PREFIX):
        name = reason[len(ACTOR_PREFIX):].strip()
        return name, name, '', True
    return None, None, reason, False


def serialise(record, entry, with_changes: bool = True) -> dict:
    """One feed row from one historical record."""
    display, username, reason, external = _actor(record)
    changes = changed_fields(record, entry['model_class']) if with_changes else []
    return {
        'id': f"{entry['model']}:{record.history_id}",
        'app_label': entry['app_label'],
        'model': entry['model'],
        'model_label': entry['verbose'],
        'object_id': getattr(record, entry['model_class']._meta.pk.attname, None),
        'object_label': describe(record, entry),
        'action': ACTION_LABELS.get(record.history_type, record.history_type),
        'action_code': record.history_type,
        'user': display,
        'username': username,
        # True when the actor was authenticated by a portfolio token and has no
        # local account — the name is still real, it just isn't linkable here.
        'external_actor': external,
        'reason': reason,
        'when': record.history_date.isoformat(),
        'changes': changes,
        # An update whose tracked fields all matched the previous row — a re-save
        # that changed nothing. Saying so is honest; an "updated" row with an empty
        # change list just looks like the diff is broken.
        'no_op': record.history_type == '~' and with_changes and not changes,
    }


def feed(since=None, until=None, model=None, username=None, action=None,
         search=None, limit=100, with_changes=True) -> list[dict]:
    """Merged, newest-first change feed across every history table.

    Each table contributes at most ``limit`` rows before the merge, which keeps
    this to one small indexed query per model rather than a union across all of
    them. ``search`` is applied after the merge because it spans rendered labels,
    not columns.
    """
    rows = []
    for entry in auditable_models():
        if model and entry['model'] != model:
            continue
        qs = entry['history_model'].objects.all()
        if since is not None:
            qs = qs.filter(history_date__gte=since)
        if until is not None:
            qs = qs.filter(history_date__lte=until)
        if username:
            # Match linked actors by their account, and external (SSO-only) actors
            # by the name stamped into the change reason — otherwise filtering by
            # user would silently hide exactly the changes hardest to attribute.
            from django.db.models import Q
            qs = qs.filter(Q(history_user__username__iexact=username)
                           | Q(history_change_reason__iexact=f'{ACTOR_PREFIX}{username}'))
        if action:
            qs = qs.filter(history_type=action)
        try:
            recent = list(qs.select_related('history_user').order_by('-history_date')[:limit])
        except DatabaseError:
            # A history table that was never migrated on this database must not
            # take the whole feed down with it.
            continue
        rows.extend((record, entry) for record in recent)

    rows.sort(key=lambda pair: pair[0].history_date, reverse=True)
    out = [serialise(r, e, with_changes=with_changes) for r, e in rows[:limit]]
    if search:
        needle = search.lower()
        out = [
            row for row in out
            if needle in (row['object_label'] or '').lower()
            or needle in (row['model_label'] or '').lower()
            or needle in (row['username'] or '').lower()
            or any(needle in (c['field'] or '').lower() for c in row['changes'])
        ]
    return out


def summary(since=None) -> dict:
    """Counts by action and by model for the header strip on the audit screen."""
    by_action = {'created': 0, 'updated': 0, 'deleted': 0}
    by_model: dict[str, int] = {}
    actors: set[str] = set()
    for entry in auditable_models():
        qs = entry['history_model'].objects.all()
        if since is not None:
            qs = qs.filter(history_date__gte=since)
        try:
            for code, label in ACTION_LABELS.items():
                n = qs.filter(history_type=code).count()
                if n:
                    by_action[label] += n
                    by_model[entry['verbose']] = by_model.get(entry['verbose'], 0) + n
            actors.update(
                qs.exclude(history_user__isnull=True)
                  .values_list('history_user__username', flat=True).distinct()
            )
        except DatabaseError:
            continue
    return {
        'by_action': by_action,
        'by_model': [{'model': k, 'count': v} for k, v in sorted(by_model.items(), key=lambda kv: -kv[1])],
        'total': sum(by_action.values()),
        'actors': len(actors),
    }
