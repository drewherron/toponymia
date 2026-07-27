"""Shared moderation helpers: active-ban lookup, the
banned-user response, and the audit-log writer. Kept out of views.py so the
mod queue, the write endpoints, and the Moderation dashboard all use one
implementation."""

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from .models import BannedEmail, ModAction


def is_moderator(user):
    """A moderator can act on the mod queue and delete others' content.
    Mapped to Django's staff flag (admins are superusers) — no custom user
    model needed for v1 (roles user/mod/admin)."""
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def is_admin(user):
    """An admin (Django superuser) additionally holds the destructive-ish
    grants a moderator doesn't: role changes, the whole-user roster, and
    article deletion."""
    return user.is_authenticated and user.is_superuser


def can_ban(actor, target):
    """Ban authority: any moderator may ban a regular user;
    nobody may ban a superuser; only a superuser may ban another moderator
    (a staff account). You can't ban yourself."""
    if actor.id == target.id:
        return False
    if target.is_superuser:
        return False
    if is_moderator(target):  # staff, but not superuser (handled above)
        return actor.is_superuser
    return is_moderator(actor)


def can_set_role(actor, target):
    """Role authority (2026-07-14): **only a superuser** may promote a user to
    moderator or demote one back. A moderator who could promote peers could
    manufacture allies, and one who could demote could neutralise whoever is
    reviewing them — so the grant stays with the admin. Nobody may change a
    superuser's role, and nobody may change their own (that is what keeps the
    last admin from locking themselves out)."""
    if not actor.is_authenticated or not actor.is_superuser:
        return False
    if actor.id == target.id:
        return False
    return not target.is_superuser


def active_ban(user):
    """The user's currently-effective ban, if any: not lifted and not
    expired. Returns the most recent such row, else None."""
    if not getattr(user, 'is_authenticated', False):
        return None
    now = timezone.now()
    return (
        user.bans.filter(lifted__isnull=True)
        .filter(Q(expires__isnull=True) | Q(expires__gt=now))
        .order_by('-created')
        .first()
    )


def active_email_ban(email):
    """The currently-effective registration block for `email`, if any: not
    lifted and not expired, matched case-insensitively. Returns the most recent
    such row, else None. The single lookup the signup adapter and the sync
    helpers below all share."""
    if not email:
        return None
    now = timezone.now()
    return (
        BannedEmail.objects.filter(email__iexact=email.strip())
        .filter(lifted__isnull=True)
        .filter(Q(expires__isnull=True) | Q(expires__gt=now))
        .order_by('-created')
        .first()
    )


def _user_emails(user):
    """Every address tied to an account — its allauth EmailAddress rows plus the
    User.email field — lowercased and de-duplicated. Verification is mandatory,
    so in practice each of these is a confirmed address the user controls."""
    from allauth.account.models import EmailAddress

    emails = {
        email.lower()
        for email in EmailAddress.objects.filter(user=user).values_list(
            'email', flat=True
        )
        if email
    }
    if user.email:
        emails.add(user.email.lower())
    return emails


def block_user_emails(user, actor, *, reason='', expires=None):
    """Snapshot a banned account's addresses into the registration blocklist,
    mirroring the ban's own expiry. Idempotent per address: an address that
    already has an active block is left untouched rather than duplicated."""
    for email in _user_emails(user):
        if active_email_ban(email) is not None:
            continue
        BannedEmail.objects.create(
            email=email,
            banned_user=user,
            created_by=actor,
            reason=reason,
            expires=expires,
        )


def lift_user_email_blocks(user, actor):
    """Lift every active registration block on a user's addresses — the inverse
    of block_user_emails, run when an account is unbanned so re-registration
    reopens with the account."""
    now = timezone.now()
    BannedEmail.objects.filter(
        email__in=list(_user_emails(user)), lifted__isnull=True
    ).filter(Q(expires__isnull=True) | Q(expires__gt=now)).update(
        lifted=now, lifted_by=actor
    )


def ban_message(ban):
    """A human-readable suspension notice for the blocked write response."""
    when = (
        'permanently'
        if ban.expires is None
        else f'until {ban.expires.date().isoformat()}'
    )
    reason = f' Reason: {ban.reason}' if ban.reason else ''
    return f'Your account is suspended {when}.{reason}'


def banned_response(user):
    """A 403 with the suspension notice if the user is banned, else None —
    call at the top of a write action so reads stay open."""
    ban = active_ban(user)
    if ban is None:
        return None
    return Response(
        {'error': ban_message(ban)}, status=status.HTTP_403_FORBIDDEN
    )


def log_action(actor, action, *, target_user=None, reason='',
               article=None, revision=None, talk_post=None, report=None):
    """Append one row to the moderator audit log."""
    return ModAction.objects.create(
        actor=actor,
        action=action,
        target_user=target_user,
        reason=reason or '',
        article=article,
        revision=revision,
        talk_post=talk_post,
        report=report,
    )
