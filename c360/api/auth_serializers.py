"""Admin user-management serializers — mirrors the HF Group backend.

Powers a clean Users admin screen (create users, assign role groups, reset
passwords) so administrators never depend on the Django admin UI. Roles are
Django Groups; the RM's scoping attributes (sales_code / branch / segment) live on
the Profile and are flattened to the top level here.
"""
from __future__ import annotations

import secrets
import string

from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from ..models import Profile
from ..roles import ALL_ROLE_DESCRIPTIONS, tier_for_groups


def _generate_password(length: int = 12) -> str:
    """A reasonably strong, human-shareable random password."""
    alphabet = string.ascii_letters + string.digits + '!@#$%*?'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _apply_profile(user, sales_code, branch, segment):
    """Create/update the user's Profile with the given fields (only those given).

    The post_save signal already created an (empty) Profile and cached it on the user
    instance; we update the row and refresh that cached reverse relation so a
    serializer read-back on the same instance reflects the new values, not the stale
    signal-created cache."""
    profile, _ = Profile.objects.get_or_create(user=user)
    if sales_code is not None:
        profile.sales_code = sales_code
    if branch is not None:
        profile.branch = branch
    if segment is not None:
        profile.segment = segment
    profile.save()
    user.profile = profile   # overwrite the cached one-to-one with the updated row
    return profile


class RoleSerializer(serializers.Serializer):
    """A selectable role (Django Group) for the admin Users screen dropdown."""

    name = serializers.CharField()
    description = serializers.SerializerMethodField()
    tier = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    def get_description(self, obj):
        return ALL_ROLE_DESCRIPTIONS.get(obj.name, '')

    def get_tier(self, obj):
        return tier_for_groups({obj.name})

    def get_member_count(self, obj):
        return obj.user_set.count()


class AdminUserSerializer(serializers.ModelSerializer):
    """Read/write serializer for managing users.

    - ``groups`` is read & written by role name.
    - ``password`` is write-only & optional; if omitted on create, a strong one is
      generated and returned ONCE via ``generated_password``.
    - Profile fields (sales_code/branch/segment) are flattened to the top level.
    """

    groups = serializers.SlugRelatedField(
        slug_field='name', queryset=Group.objects.all(), many=True, required=False)
    password = serializers.CharField(write_only=True, required=False, allow_blank=False)
    sales_code = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    branch = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    segment = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    role_tier = serializers.SerializerMethodField()
    generated_password = serializers.CharField(read_only=True, required=False)

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'first_name', 'last_name',
            'is_active', 'is_staff', 'is_superuser', 'groups',
            'sales_code', 'branch', 'segment',
            'role_tier', 'password', 'generated_password',
            'date_joined', 'last_login',
        )
        read_only_fields = ('id', 'role_tier', 'date_joined', 'last_login')

    def get_role_tier(self, obj):
        return tier_for_groups(obj.groups.values_list('name', flat=True))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        profile = getattr(instance, 'profile', None)
        data['sales_code'] = getattr(profile, 'sales_code', None)
        data['branch'] = getattr(profile, 'branch', None)
        data['segment'] = getattr(profile, 'segment', None)
        return data

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_username(self, value):
        qs = User.objects.filter(username__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('A user with that username already exists.')
        return value

    def validate(self, attrs):
        """Only a superuser may grant Django ``is_staff`` / ``is_superuser``, and no
        one may elevate their OWN account through this endpoint. We silently drop the
        privileged fields rather than error, so ordinary edits still succeed."""
        request = self.context.get('request')
        actor = getattr(request, 'user', None)
        actor_is_superuser = bool(actor and actor.is_superuser)
        editing_self = bool(actor and self.instance and self.instance.pk == actor.pk)
        if not actor_is_superuser or editing_self:
            attrs.pop('is_superuser', None)
            attrs.pop('is_staff', None)
        return attrs

    def create(self, validated_data):
        groups = validated_data.pop('groups', [])
        sales_code = validated_data.pop('sales_code', None)
        branch = validated_data.pop('branch', None)
        segment = validated_data.pop('segment', None)
        raw_password = validated_data.pop('password', None)

        generated = None
        if not raw_password:
            raw_password = _generate_password()
            generated = raw_password

        user = User(**validated_data)
        user.set_password(raw_password)
        user.save()                       # post_save signal auto-creates the Profile
        user.groups.set(groups)
        _apply_profile(user, sales_code, branch, segment)

        if generated:
            user.generated_password = generated   # surfaced ONCE to the admin
        return user

    def update(self, instance, validated_data):
        groups = validated_data.pop('groups', None)
        sales_code = validated_data.pop('sales_code', None)
        branch = validated_data.pop('branch', None)
        segment = validated_data.pop('segment', None)
        validated_data.pop('password', None)   # password goes through set-password

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if groups is not None:
            instance.groups.set(groups)
        if any(v is not None for v in (sales_code, branch, segment)):
            _apply_profile(instance, sales_code, branch, segment)
        return instance


class SetPasswordSerializer(serializers.Serializer):
    """Admin-initiated password reset for a user."""

    password = serializers.CharField(write_only=True, required=False, allow_blank=False)

    def validate_password(self, value):
        validate_password(value)
        return value

    def save(self, user):
        raw = self.validated_data.get('password') or _generate_password()
        user.set_password(raw)
        user.save(update_fields=['password'])
        return raw


class LogoutSerializer(serializers.Serializer):
    """Blacklist the refresh token on sign-out."""

    refresh = serializers.CharField()
