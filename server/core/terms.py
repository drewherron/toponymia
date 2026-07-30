"""The Terms of Use version recorded against each signup.

The document itself is TERMS.md at the repo root, served to the client by the
SPA's /terms route. What matters legally is not just that a user agreed but
*which* text they agreed to, so every acceptance stores this version string and
TERMS.md's git history supplies the matching text.

This is a constant rather than something parsed out of the file at import time
because the server should not depend on the repo layout at runtime — but a test
(`TermsVersionTests`) asserts it still matches TERMS.md's "Last updated" line,
so the two cannot drift apart unnoticed.
"""

import re
from pathlib import Path

from django.conf import settings

TERMS_VERSION = '2026-08-01'

TERMS_PATH = Path(settings.BASE_DIR).parent / 'TERMS.md'

_LAST_UPDATED_RE = re.compile(r'^\*Last updated (\d{4}-\d{2}-\d{2})\.\*$', re.M)


def documented_version():
    """The version TERMS.md declares, or None if the file isn't readable.

    Only used by the drift test — the file ships with the source checkout but
    isn't guaranteed to be present next to a deployed server.
    """
    try:
        text = TERMS_PATH.read_text('utf-8')
    except OSError:
        return None
    match = _LAST_UPDATED_RE.search(text)
    return match.group(1) if match else None
