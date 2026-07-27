"""Username rules.

Login accepts a username *or* an email address in one field, and the SPA
decides which of allauth's two credential keys to post by looking for an "@".
Django's stock username validator permits "@", which would make a username like
``a@b`` unroutable — so drop it here. Everything else matches
``UnicodeUsernameValidator``: letters, digits, and ``. + - _``.
"""

from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _

username_validators = [
    RegexValidator(
        r'^[\w.+-]+\Z',
        _(
            'Enter a valid username. This value may contain only letters, '
            'numbers, and ./+/-/_ characters.'
        ),
        'invalid',
    )
]
