"""Auth profile — mirrors the HF Group ``Profile`` (branch / segment / sales_code).

Roles live in Django Groups (see ``c360.roles``); this profile carries the
scoping attributes RBAC filters on: the RM's ``sales_code`` (their book) and the
``branch`` / ``segment`` they belong to. A profile is auto-created for every user
so the admin Users screen and the scope resolver can always read one.

Branch / segment choice lists are the canonical HF Group values, so an account
provisioned here can never store a branch the warehouse wouldn't recognise.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

BRANCH_CHOICES = [
    ('KISII BRANCH', 'KISII BRANCH'), ('NYERI BRANCH', 'NYERI BRANCH'),
    ('BURUBURU BRANCH', 'BURUBURU BRANCH'), ('NYALI BRANCH', 'NYALI BRANCH'),
    ('GILL HOUSE BRANCH', 'GILL HOUSE BRANCH'), ('NANYUKI BRANCH', 'NANYUKI BRANCH'),
    ('SAMEER BRANCH', 'SAMEER BRANCH'), ('NAKURU BRANCH', 'NAKURU BRANCH'),
    ('HEAD OFFICE BRANCH', 'HEAD OFFICE BRANCH'), ('MACHAKOS BRANCH', 'MACHAKOS BRANCH'),
    ('NAIVASHA BRANCH', 'NAIVASHA BRANCH'), ('KISUMU BRANCH', 'KISUMU BRANCH'),
    ('HURLINGHAM BRANCH', 'HURLINGHAM BRANCH'), ('TRM BRANCH', 'TRM BRANCH'),
    ('KITENGELA BRANCH', 'KITENGELA BRANCH'), ('ELDORET BRANCH', 'ELDORET BRANCH'),
    ('KENYATTA BRANCH', 'KENYATTA BRANCH'), ('REHANI BRANCH', 'REHANI BRANCH'),
    ('HF WHIZZ BRANCH', 'HF WHIZZ BRANCH'), ('MOMBASA BRANCH', 'MOMBASA BRANCH'),
    ('RIVERROAD BRANCH', 'RIVERROAD BRANCH'), ('EMBU BRANCH', 'EMBU BRANCH'),
    ('MERU BRANCH', 'MERU BRANCH'), ('THIKA BRANCH', 'THIKA BRANCH'),
    ('KOMAROCK BRANCH', 'KOMAROCK BRANCH'), ('WESTLANDS BRANCH', 'WESTLANDS BRANCH'),
    ('RONGAI BRANCH', 'RONGAI BRANCH'),
]

SEGMENT_CHOICES = [
    ('FINANCIAL INSTITUTIONS', 'FINANCIAL INSTITUTIONS'),
    ('INSTITUTIONAL BANKING', 'INSTITUTIONAL BANKING'),
    ('INTERNAL ACCOUNTS', 'INTERNAL ACCOUNTS'),
    ('PB', 'PB'), ('SCHEME', 'SCHEME'), ('BUSINESS BANKING', 'BUSINESS BANKING'),
    ('COMMERCIAL', 'COMMERCIAL'), ('ULTIMATE', 'ULTIMATE'),
    ('PROJECT FINANCE', 'PROJECT FINANCE'), ('VIRTUAL', 'VIRTUAL'),
    ('STAFF', 'STAFF'), ('DIASPORA', 'DIASPORA'), ('unsegmented', 'unsegmented'),
]


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    sales_code = models.TextField(blank=True, null=True)
    branch = models.CharField(choices=BRANCH_CHOICES, max_length=32, blank=True, null=True)
    segment = models.CharField(choices=SEGMENT_CHOICES, max_length=32, blank=True, null=True)

    def __str__(self) -> str:  # pragma: no cover - admin/debug convenience
        return str(self.user.username)


class OTP(models.Model):
    """A one-time passcode for login 2FA and password reset. Short-lived, single-use,
    delivered by email (console backend in dev). Mirrors the HF Group OTP model."""

    PURPOSE_LOGIN = 'login'
    PURPOSE_RESET = 'reset'
    PURPOSE_CHOICES = [(PURPOSE_LOGIN, 'Login 2FA'), (PURPOSE_RESET, 'Password reset')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='otps')
    code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=16, choices=PURPOSE_CHOICES, default=PURPOSE_LOGIN)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=['user', 'purpose', '-created_at'])]

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class RecommendationFeedback(models.Model):
    """The outcome-logging loop — the missing ground truth for the model.

    Today the propensity model learns from *ownership* look-alikes (a cold-start proxy)
    because nothing records what actually happened after a recommendation was shown.
    This captures exactly that: an RM marks whether they pitched a recommendation and
    how the customer responded. Once enough of these accumulate, ``training_labels()``
    turns ``accepted`` (positive) / ``declined``+``not_relevant`` (negative) into REAL
    conversion labels, and the model retrains on outcomes instead of proxies — which is
    what finally yields a true precision / uplift number.

    We snapshot the model's ``score`` and ``engine_version`` at the moment of the
    recommendation, so later we can measure whether higher propensity really did convert
    better (the acceptance-by-score-decile stat) — i.e. validate the model in production.
    """

    OUTCOME_PITCHED = 'pitched'
    OUTCOME_ACCEPTED = 'accepted'
    OUTCOME_DECLINED = 'declined'
    OUTCOME_NOT_RELEVANT = 'not_relevant'
    OUTCOME_CHOICES = [
        (OUTCOME_PITCHED, 'Pitched — awaiting decision'),
        (OUTCOME_ACCEPTED, 'Accepted / taken up'),
        (OUTCOME_DECLINED, 'Declined by customer'),
        (OUTCOME_NOT_RELEVANT, 'Not relevant (model was off)'),
    ]
    # Outcomes that are usable as supervised labels (pitched is still pending → excluded).
    POSITIVE_OUTCOMES = {OUTCOME_ACCEPTED}
    NEGATIVE_OUTCOMES = {OUTCOME_DECLINED, OUTCOME_NOT_RELEVANT}

    cust_id = models.CharField(max_length=64, db_index=True)
    product = models.CharField(max_length=64)
    product_name = models.CharField(max_length=120, blank=True, default='')
    domain = models.CharField(max_length=32, default='HFCB')

    # Snapshot of the recommendation as shown, so outcomes tie back to a model version.
    score = models.FloatField(null=True, blank=True)
    rule_id = models.CharField(max_length=32, blank=True, default='')
    engine_version = models.CharField(max_length=32, blank=True, default='')

    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default=OUTCOME_PITCHED)
    note = models.TextField(blank=True, default='')

    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='recommendation_feedback')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['cust_id', 'product']),
            models.Index(fields=['outcome']),
        ]
        # One current outcome per (customer, product, RM) — re-marking updates it in
        # place rather than piling up rows; history still lives in updated_at.
        constraints = [
            models.UniqueConstraint(fields=['cust_id', 'product', 'recorded_by'],
                                    name='uniq_feedback_cust_product_rm'),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f'{self.cust_id}/{self.product}: {self.outcome}'

    @property
    def label(self) -> int | None:
        """Supervised label: 1 accepted, 0 declined/not-relevant, None if pending."""
        if self.outcome in self.POSITIVE_OUTCOMES:
            return 1
        if self.outcome in self.NEGATIVE_OUTCOMES:
            return 0
        return None


class HealthSnapshot(models.Model):
    """A point-in-time capture of the warehouse health report, so the admin Data-health
    page can chart trends (freshness over time, per-source row counts, latency) — a
    native, in-app equivalent of a Grafana board, inside Customer 360's own auth. Written
    on a throttle when an admin views the page, and by the ``capture_health`` command."""

    captured_at = models.DateTimeField(default=timezone.now, db_index=True)
    days_behind = models.IntegerField(null=True, blank=True)   # freshness at capture
    payload = models.JSONField()                               # the full health_report

    class Meta:
        db_table = 'c360_health_snapshot'
        ordering = ['-captured_at']

    def __str__(self) -> str:  # pragma: no cover
        return f'health @ {self.captured_at:%Y-%m-%d %H:%M} (days_behind={self.days_behind})'


class AuditEvent(models.Model):
    """One recorded activity — a server-side API action or a client-side interaction
    (click / navigation). Written OFF the request hot path (buffered + bulk-inserted by
    the observability flusher), so recording never blocks a user. Timestamped in real
    system time. Retention is bounded (see the flusher) so the table can't grow without
    limit under daily/hourly traffic."""

    KIND_API = 'api'
    KIND_PAGE = 'page_view'
    KIND_CLICK = 'click'
    KIND_NAV = 'nav'
    KIND_AUTH = 'auth'

    ts = models.DateTimeField(default=timezone.now, db_index=True)
    user_id = models.IntegerField(null=True, blank=True)     # who (resolved from the JWT)
    username = models.CharField(max_length=150, blank=True, default='')
    kind = models.CharField(max_length=16, default=KIND_API)
    method = models.CharField(max_length=8, blank=True, default='')
    route = models.CharField(max_length=200, blank=True, default='')   # normalised URL pattern
    path = models.CharField(max_length=300, blank=True, default='')    # raw path (truncated)
    status = models.IntegerField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    target = models.CharField(max_length=200, blank=True, default='')  # e.g. customer id / element
    ip = models.GenericIPAddressField(null=True, blank=True)
    session = models.CharField(max_length=64, blank=True, default='')
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'c360_audit_event'
        ordering = ['-ts']
        indexes = [
            models.Index(fields=['-ts']),
            models.Index(fields=['user_id', '-ts']),
            models.Index(fields=['kind', '-ts']),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f'{self.ts:%Y-%m-%d %H:%M:%S} {self.username or self.user_id} {self.kind} {self.route}'


class MetricMinute(models.Model):
    """A per-minute, per-worker rollup of request metrics, flushed from the in-memory
    collector (never a DB write on the request hot path). One row per (minute, instance)
    so multiple gunicorn workers don't clobber each other; the read API aggregates across
    instances. Latency percentiles are computed from a reservoir sample of the minute.
    A gap in the minute series for a live instance = downtime."""

    minute = models.DateTimeField(db_index=True)          # truncated to the minute (UTC)
    instance = models.CharField(max_length=40)            # host:pid — the worker
    count = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)               # 5xx
    client_errors = models.IntegerField(default=0)        # 4xx
    sum_ms = models.BigIntegerField(default=0)            # for an exact weighted mean on read
    p50_ms = models.IntegerField(default=0)
    p95_ms = models.IntegerField(default=0)
    p99_ms = models.IntegerField(default=0)
    max_ms = models.IntegerField(default=0)
    by_status = models.JSONField(default=dict, blank=True)   # {'2xx':n,'4xx':n,'5xx':n}

    class Meta:
        db_table = 'c360_metric_minute'
        ordering = ['-minute']
        constraints = [models.UniqueConstraint(fields=['minute', 'instance'], name='uniq_metric_minute_instance')]
        indexes = [models.Index(fields=['-minute'])]

    def __str__(self) -> str:  # pragma: no cover
        return f'{self.minute:%Y-%m-%d %H:%M} {self.instance} n={self.count} p95={self.p95_ms}ms'


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Every user gets a Profile on creation. We never email a password here — the
    admin Users screen surfaces the generated password once for out-of-band sharing
    (see the SECURITY note in the reference), and password reset is a later phase."""
    if created:
        Profile.objects.get_or_create(user=instance)
