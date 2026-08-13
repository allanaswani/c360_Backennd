"""Seed the first administrator (Django superuser) so the Users screen is reachable.

Users are admin-provisioned through the Users API, which requires an existing
administrator — this bootstraps that first account without the Django admin. Run
once after ``seed_roles``. Idempotent (re-running resets the password).

    python manage.py bootstrap_admin --username admin --email ops@hfcb.co.ke

Password comes from --password or the C360_ADMIN_PASSWORD env var.
"""
from __future__ import annotations

import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create or update the bootstrap administrator (superuser).'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        parser.add_argument('--email', default='')
        parser.add_argument('--name', default='')
        parser.add_argument('--password', default='',
                            help='Password. Falls back to the C360_ADMIN_PASSWORD env var.')

    def handle(self, *args, **opts):
        username = opts['username'].strip()
        password = opts['password'] or os.environ.get('C360_ADMIN_PASSWORD', '')
        if not password:
            raise CommandError('Provide --password or set C360_ADMIN_PASSWORD.')

        user, created = User.objects.get_or_create(username=username)
        user.email = opts['email'] or user.email
        if opts['name']:
            user.first_name = opts['name'][:150]
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} administrator "{username}" (superuser). Sign in and manage users '
            f'from the Users screen.'))
