"""Ensure every canonical role (auth.Group) exists. Idempotent.

Run once after migrate so the admin Users screen can assign roles. Mirrors the HF
Group ``seed_roles`` command.
"""
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from c360.roles import ALL_ROLE_DESCRIPTIONS, ALL_ROLES


class Command(BaseCommand):
    help = 'Create the canonical HF roles (Django Groups) in the default database.'

    def handle(self, *args, **options):
        created = existing = 0
        for name in ALL_ROLES:
            _, was_created = Group.objects.get_or_create(name=name)
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  + created group '{name}'"))
            else:
                existing += 1
                self.stdout.write(f"  = group '{name}' already present")
        self.stdout.write(self.style.SUCCESS(
            f'Done. {created} created, {existing} existed, {len(ALL_ROLES)} total.'))
