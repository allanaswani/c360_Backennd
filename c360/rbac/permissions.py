"""API permission: a signed-in caller needs a Customer 360 role.

A valid portfolio login is not enough on its own. The caller must be a
superuser or belong to a Customer 360 role group (see ``c360.roles``), the same
way every other module is given. An anonymous request is left to the other
permission classes (``C360_REQUIRE_AUTH`` and the local-dev header seam).
"""
from rest_framework.permissions import BasePermission

from ..roles import has_c360_access


class HasC360Role(BasePermission):
    message = ('You do not have access to Customer 360. Ask an administrator '
               'to give you a Customer 360 role.')

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return True
        return has_c360_access(user)
