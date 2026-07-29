#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    # DEBUG defaults to off in settings.py so a forgotten variable in
    # production fails safe. This is the development entry point, so turn it
    # back on here — runserver, tests, and shell keep working with no setup.
    # Production runs gunicorn against config.wsgi and never reaches this.
    # An explicit DJANGO_DEBUG in the environment still wins, so a deployment
    # that sets DJANGO_DEBUG=0 gets accurate `manage.py check --deploy`
    # results rather than the dev default.
    os.environ.setdefault('DJANGO_DEBUG', '1')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
