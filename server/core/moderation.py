"""Shared moderation helpers (DESIGN.md M12): active-ban lookup, the
banned-user response, and the audit-log writer. Kept out of views.py so the
mod queue, the write endpoints, and the Moderation dashboard all use one
implementation."""

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from .models import ModAction


def is_moderator(user):
    """A moderator can act on the mod queue and delete others' content.
    Mapped to Django's staff flag (admins are superusers) — no custom user
    model needed for v1 (DESIGN.md §4 roles user/mod/admin)."""
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def can_ban(actor, target):
    """Ban authority (DESIGN.md M12): any moderator may ban a regular user;
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
    call at the top of a write action so reads stay open (DESIGN.md M12)."""
    ban = active_ban(user)
    if ban is None:
        return None
    return Response(
        {'error': ban_message(ban)}, status=status.HTTP_403_FORBIDDEN
    )


def log_action(actor, action, *, target_user=None, reason='',
               revision=None, talk_post=None, report=None):
    """Append one row to the moderator audit log."""
    return ModAction.objects.create(
        actor=actor,
        action=action,
        target_user=target_user,
        reason=reason or '',
        revision=revision,
        talk_post=talk_post,
        report=report,
    )
