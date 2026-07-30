"""Closing an account.

The wiki cannot simply delete a contributor. `Revision.author`,
`TalkPost.author` and `Report.reporter` are all PROTECT, so `user.delete()`
raises ProtectedError for anyone who has ever edited — and that is deliberate,
because TERMS.md §2 makes the revision history the attribution mechanism and
the CC BY-SA grant irrevocable. Contributions have to stay.

So closing an account means one of two things:

- **Never contributed** — nothing points at the row, so it is deleted outright
  (taking its EmailAddress and TermsAcceptance rows with it, both CASCADE).
- **Contributed** — the account is *anonymized*: the username is replaced with
  an opaque `[deleted-…]` sentinel, the email address is removed, the password
  is made unusable and the account is deactivated. The edits stay where they
  are, now credited to a name that identifies nobody.

CC BY-SA 4.0 §3(a)(3) expressly contemplates this: a licensor may ask that
attribution information be removed, and the licensee must comply as far as is
practicable. Dropping the name is a request the licence anticipates, not a
breach of it.

The sentinel is per-account, not one shared `[deleted]` user: usernames are
unique, and merging every departed contributor into a single author would
destroy the revision history as an audit trail. Square brackets are outside
`core.validators.username_validators` (`^[\\w.+-]+\\Z`), so a sentinel can
never be registered through signup or impersonated.

Anonymizing also *retires* the original username (`ReservedUsername`), because
renaming the account would otherwise hand the old name back to the pool while
the archive that used it stays public. Retirement is permanent and applies to
everyone, the closing user included — once the email and password are gone
there is nothing left that could prove the name was ever theirs, so there is no
one to make an exception for. Closing an account is not a way to change your
username and keep it.
"""

import secrets

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Report, ReservedUsername, Revision, TalkPost

SENTINEL_PREFIX = '[deleted-'


def has_contributions(user):
    """Whether anything PROTECTed points at this user — i.e. whether the row
    can be deleted at all."""
    return (
        Revision.objects.filter(author=user).exists()
        or TalkPost.objects.filter(author=user).exists()
        or Report.objects.filter(reporter=user).exists()
    )


def _sentinel_username():
    """An unused `[deleted-…]` name. Random rather than sequential so the
    sentinel doesn't leak the order accounts were closed in."""
    users = get_user_model().objects
    while True:
        candidate = f'{SENTINEL_PREFIX}{secrets.token_hex(3)}]'
        if not users.filter(username=candidate).exists():
            return candidate


def username_is_reserved(username):
    """Whether `username` is retired by a past closure — matched
    case-insensitively, ignoring reservations that have been given an expiry
    and passed it. The single lookup the signup adapter shares."""
    if not username:
        return False
    return (
        ReservedUsername.objects.filter(username__iexact=username.strip())
        .filter(Q(expires__isnull=True) | Q(expires__gt=timezone.now()))
        .exists()
    )


def reserve_username(username, *, expires=None):
    """Retire `username`, permanently unless given an expiry. Idempotent: a
    name already reserved is left with the reservation it has."""
    username = (username or '').strip().lower()
    if not username or username_is_reserved(username):
        return None
    return ReservedUsername.objects.create(username=username, expires=expires)


@transaction.atomic
def anonymize(user):
    """Strip the identity, keep the row (and so the contributions).

    Atomic because it is now several writes: a half-applied closure could
    retire a username while leaving the account using it, or clear the identity
    without retiring the name.
    """
    # Before the name is overwritten, and only on this path: an account with no
    # contributions is deleted outright, leaving no history to misattribute and
    # so no reason to hold its name.
    reserve_username(user.username)
    user.username = _sentinel_username()
    user.email = ''
    user.first_name = ''
    user.last_name = ''
    user.set_unusable_password()
    user.is_active = False
    # Staff rights should not survive on a closed account.
    user.is_staff = False
    user.is_superuser = False
    user.save()
    EmailAddress.objects.filter(user=user).delete()
    return user.username


def close(user):
    """Close `user`. Returns ('deleted', None) or ('anonymized', <username>).

    Callers are responsible for authenticating the request and for refusing
    the operation while a ban is active — see core.views.close_account.
    """
    if has_contributions(user):
        return 'anonymized', anonymize(user)
    user.delete()
    return 'deleted', None
