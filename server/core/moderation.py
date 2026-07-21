"""Shared moderation helpers (DESIGN.md M12): active-ban lookup, the
banned-user response, and the audit-log writer. Kept out of views.py so the
mod queue, the write endpoints, and the Moderation dashboard all use one
implementation."""

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from .models import ModAction


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
