import os
import sys

from django.apps import AppConfig


class C360Config(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'c360'
    verbose_name = 'Customer 360'

    def ready(self) -> None:
        """Start the observability flush thread once, only when actually serving — never
        under management commands, migrations, the test runner or the autoreloader's
        parent process (which would double-start it)."""
        from django.conf import settings
        if not getattr(settings, 'OBSERVABILITY_BACKGROUND', True):
            return
        _NON_SERVING = {'migrate', 'makemigrations', 'collectstatic', 'test', 'shell',
                        'createsuperuser', 'loaddata', 'dumpdata', 'check'}
        if any(cmd in sys.argv for cmd in _NON_SERVING):
            return
        # Under `runserver` WITH autoreload, only the child (RUN_MAIN=true) should start
        # the thread, not the watching parent. With --noreload there is no child, so start
        # it. gunicorn sets neither flag and isn't 'runserver', so it always starts there.
        if 'runserver' in sys.argv and '--noreload' not in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return
        from .observability import start_flusher
        start_flusher()
