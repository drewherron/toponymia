"""The Moderation dashboard API — the actor-centric
and decision-centric views that complement the content-centric Reports queue.
All endpoints are moderators-only.

- `mod_users`      — users with reports or removed content against them.
- `mod_user_detail`— one user's content (removed items shown), reports, bans,
                     and the audit trail of actions against them.
- `mod_ban_user` / `mod_unban_user` — apply and lift account bans.
- `mod_reporters`  — reporters ranked by dismissed-vs-upheld, to catch abuse.
- `mod_audit`      — the global chronological ModAction feed (M13).
"""

from collections import defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Ban, ModAction, Report, Revision, TalkPost
from .moderation import (
    active_ban,
    block_user_emails,
    can_ban,
    can_set_role,
    is_moderator,
    lift_user_email_blocks,
    log_action,
)
from .serializers import AuditFilterSerializer, BanSerializer, RoleSerializer
from .views import _revision_excerpt

User = get_user_model()

# Ceiling on the ?all=1 roster. Far beyond a solo wiki's roll, but the client
# filters what it was sent, so an unbounded list would quietly become both a
# fat payload and a lying search box.
ALL_USERS_CAP = 500

# Page size for the global audit feed. The feed's job is oversight — noticing
# a burst of removals you didn't expect — so it is capped and newest-first
# rather than paginated into a browsable archive.
AUDIT_PAGE = 200


def _forbidden(request):
    """None if the caller may use the dashboard, else a 403 response."""
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    return None


def _iso(value):
    return value.isoformat() if value else None


def _user_role(user):
    if user.is_superuser:
        return 'admin'
    if user.is_staff:
        return 'moderator'
    return 'user'


def _report_author_id(report):
    """The author of a report's target — whose content was flagged."""
    if report.talk_post_id is not None:
        return report.talk_post.author_id
    return report.revision.author_id


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mod_users(request):
    """Users with any report or removed content against them, most-recently
    reported first. The list is small enough to filter live
    on the client, so everything is returned at once.

    `?all=1` (**superusers only**) widens it to every account, so an admin can
    find a clean user to promote — the moderation-shaped list can't surface
    someone with nothing against them. Capped at ALL_USERS_CAP; the client
    filter only sees what was sent, so `truncated` tells it to say so."""
    forbidden = _forbidden(request)
    if forbidden is not None:
        return forbidden
    show_all = (
        request.query_params.get('all') == '1' and request.user.is_superuser
    )

    reports = Report.objects.select_related('talk_post', 'revision')
    report_total = defaultdict(int)
    report_open = defaultdict(int)
    last_report = {}
    for report in reports:
        author_id = _report_author_id(report)
        report_total[author_id] += 1
        if report.status == Report.Status.OPEN:
            report_open[author_id] += 1
        if author_id not in last_report or report.created > last_report[author_id]:
            last_report[author_id] = report.created

    removed = defaultdict(int)
    for author_id in TalkPost.objects.filter(
        deleted__isnull=False
    ).values_list('author_id', flat=True):
        removed[author_id] += 1
    for author_id in Revision.objects.filter(
        suppressed__isnull=False
    ).values_list('author_id', flat=True):
        removed[author_id] += 1

    # Total upheld actions against each user — the chronic-offender column.
    upheld = defaultdict(int)
    for author_id in ModAction.objects.filter(
        target_user__isnull=False,
        action__in=[
            ModAction.Action.DELETE_POST,
            ModAction.Action.SUPPRESS_REVISION,
            ModAction.Action.BAN_USER,
        ],
    ).values_list('target_user_id', flat=True):
        upheld[author_id] += 1

    now = timezone.now()
    banned_ids = {
        ban.user_id
        for ban in Ban.objects.filter(
            lifted__isnull=True
        ).filter(Q(expires__isnull=True) | Q(expires__gt=now))
    }

    author_ids = (
        set(report_total) | set(removed)
    )
    truncated = False
    if show_all:
        extra = list(
            User.objects.exclude(id__in=author_ids)
            .order_by('username')
            .values_list('id', flat=True)[:ALL_USERS_CAP + 1]
        )
        truncated = len(extra) > ALL_USERS_CAP
        author_ids |= set(extra[:ALL_USERS_CAP])
    users = {u.id: u for u in User.objects.filter(id__in=author_ids)}
    rows = []
    for author_id in author_ids:
        user = users.get(author_id)
        if user is None:  # target author since deleted — skip
            continue
        rows.append({
            'id': user.id,
            'username': user.username,
            'role': _user_role(user),
            'reports_open': report_open.get(author_id, 0),
            'reports_total': report_total.get(author_id, 0),
            'removed_count': removed.get(author_id, 0),
            'upheld_actions': upheld.get(author_id, 0),
            'last_report': _iso(last_report.get(author_id)),
            'banned': author_id in banned_ids,
        })
    # Most recently reported first; users with only removed content (no
    # report timestamp) sort to the bottom. Ties break alphabetically —
    # which under ?all=1 is what orders the whole never-reported tail.
    rows.sort(key=lambda r: r['username'].lower())
    rows.sort(key=lambda r: r['last_report'] or '', reverse=True)
    return Response({'users': rows, 'truncated': truncated})


def _ban_json(ban):
    return {
        'id': ban.id,
        'reason': ban.reason,
        'created': _iso(ban.created),
        'created_by': ban.created_by.username if ban.created_by else None,
        'expires': _iso(ban.expires),
        'lifted': _iso(ban.lifted),
        'lifted_by': ban.lifted_by.username if ban.lifted_by else None,
        'active': ban.is_active(),
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mod_user_detail(request, user_id):
    """Everything a moderator needs to judge one account: their content (with
    removed items shown in full), reports against them, ban history, and the
    audit trail of actions taken."""
    forbidden = _forbidden(request)
    if forbidden is not None:
        return forbidden
    user = get_object_or_404(User, id=user_id)

    posts = (
        TalkPost.objects.filter(author=user)
        .select_related('thread__place')
        .order_by('-created')[:100]
    )
    talk_posts = [{
        'id': p.id,
        'thread_id': p.thread_id,
        'thread_title': p.thread.title,
        'slug': p.thread.place.slug,
        'place': p.thread.place.display_name,
        # Removed content is shown in full to moderators (that's the point).
        'body_md': p.body_md,
        'created': _iso(p.created),
        'deleted': p.deleted is not None,
    } for p in posts]

    revisions = (
        Revision.objects.filter(author=user)
        .select_related('article__place', 'article__current_revision')
        .order_by('-created')[:100]
    )
    revs = [{
        'id': r.id,
        'slug': r.article.place.slug,
        'place': r.article.place.display_name,
        'comment': r.comment,
        'excerpt': _revision_excerpt(r.content),
        'created': _iso(r.created),
        'is_current': r.article.current_revision_id == r.id,
        'suppressed': r.suppressed is not None,
    } for r in revisions]

    reports = (
        Report.objects.filter(
            Q(talk_post__author=user) | Q(revision__author=user)
        )
        .select_related('reporter')
        .order_by('-created')[:100]
    )
    reports_against = [{
        'id': rep.id,
        'category': rep.category,
        'reason': rep.reason,
        'status': rep.status,
        'reporter': rep.reporter.username,
        'created': _iso(rep.created),
        'target_kind': 'talk_post' if rep.talk_post_id else 'revision',
    } for rep in reports]

    actions = (
        ModAction.objects.filter(target_user=user)
        .select_related('actor')
        .order_by('-created')[:100]
    )
    audit = [{
        'id': a.id,
        'action': a.action,
        'actor': a.actor.username if a.actor else None,
        'reason': a.reason,
        'created': _iso(a.created),
    } for a in actions]

    return Response({
        'id': user.id,
        'username': user.username,
        'role': _user_role(user),
        'date_joined': _iso(user.date_joined),
        'bans': [_ban_json(b) for b in user.bans.all()],
        'can_ban': can_ban(request.user, user),
        'can_set_role': can_set_role(request.user, user),
        'talk_posts': talk_posts,
        'revisions': revs,
        'reports_against': reports_against,
        'audit': audit,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mod_ban_user(request, user_id):
    """Ban an account. Body: reason, expires_days (0/absent =
    permanent), remove_content (also soft-remove all their content)."""
    forbidden = _forbidden(request)
    if forbidden is not None:
        return forbidden
    target = get_object_or_404(User, id=user_id)
    if not can_ban(request.user, target):
        return Response(
            {'error': 'you do not have authority to ban this account'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = BanSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    reason = serializer.validated_data['reason'] or ''
    days = serializer.validated_data['expires_days'] or 0
    expires = timezone.now() + timedelta(days=days) if days > 0 else None

    ban = Ban.objects.create(
        user=target, created_by=request.user, reason=reason, expires=expires,
    )
    # Block the account's address(es) from opening a fresh account, for as long
    # as the ban itself lasts — the durable half of the sanction.
    block_user_emails(
        target, request.user, reason=reason, expires=expires,
    )
    removed = 0
    if serializer.validated_data['remove_content']:
        removed = _remove_all_content(target, request.user)
    log_action(
        request.user, ModAction.Action.BAN_USER,
        target_user=target, reason=reason,
    )
    return Response(
        {'ban': _ban_json(ban), 'removed_content': removed},
        status=status.HTTP_201_CREATED,
    )


def _remove_all_content(user, actor):
    """Soft-delete all of a user's talk posts and suppress all their
    non-current revisions. Returns how many items were removed."""
    now = timezone.now()
    count = 0
    for post in TalkPost.objects.filter(author=user, deleted__isnull=True):
        post.deleted = now
        post.deleted_by = actor
        post.save(update_fields=['deleted', 'deleted_by'])
        count += 1
    revisions = (
        Revision.objects.filter(author=user, suppressed__isnull=True)
        .select_related('article')
    )
    for revision in revisions:
        if revision.id == revision.article.current_revision_id:
            continue  # can't suppress a live article's current text
        revision.suppressed = now
        revision.suppressed_by = actor
        revision.save(update_fields=['suppressed', 'suppressed_by'])
        count += 1
    return count


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mod_set_role(request, user_id):
    """Promote a user to moderator or demote one back (superuser only —
    see `can_set_role`). Body: role ∈ user|moderator. Moderator is Django's
    `is_staff`, so this is the one write that changes what the account is
    rather than what it has done."""
    forbidden = _forbidden(request)
    if forbidden is not None:
        return forbidden
    target = get_object_or_404(User, id=user_id)
    if not can_set_role(request.user, target):
        return Response(
            {'error': 'only an admin may change an account’s role, and not '
                      'their own'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = RoleSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    promote = serializer.validated_data['role'] == 'moderator'
    if target.is_staff != promote:
        target.is_staff = promote
        target.save(update_fields=['is_staff'])
        log_action(
            request.user,
            ModAction.Action.PROMOTE_MOD if promote
            else ModAction.Action.DEMOTE_MOD,
            target_user=target,
            reason=serializer.validated_data['reason'] or '',
        )
    return Response({'id': target.id, 'role': _user_role(target)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mod_unban_user(request, user_id):
    """Lift every active ban on an account."""
    forbidden = _forbidden(request)
    if forbidden is not None:
        return forbidden
    target = get_object_or_404(User, id=user_id)
    if not can_ban(request.user, target):
        return Response(
            {'error': 'you do not have authority over this account'},
            status=status.HTTP_403_FORBIDDEN,
        )
    ban = active_ban(target)
    if ban is None:
        return Response({'ok': True, 'lifted': False})
    now = timezone.now()
    target.bans.filter(
        lifted__isnull=True
    ).filter(Q(expires__isnull=True) | Q(expires__gt=now)).update(
        lifted=now, lifted_by=request.user
    )
    # Reopen re-registration for the account's address(es) alongside the ban.
    lift_user_email_blocks(target, request.user)
    log_action(
        request.user, ModAction.Action.UNBAN_USER, target_user=target,
    )
    return Response({'ok': True, 'lifted': True})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mod_reporters(request):
    """Reporters ranked by dismissed reports — a high dismissed count is the
    signature of report-button abuse."""
    forbidden = _forbidden(request)
    if forbidden is not None:
        return forbidden
    rows = (
        Report.objects.values('reporter_id', 'reporter__username')
        .annotate(
            total=Count('id'),
            open=Count('id', filter=Q(status=Report.Status.OPEN)),
            resolved=Count('id', filter=Q(status=Report.Status.RESOLVED)),
            dismissed=Count('id', filter=Q(status=Report.Status.DISMISSED)),
        )
        .order_by('-dismissed', '-total')
    )
    reporters = [{
        'id': row['reporter_id'],
        'username': row['reporter__username'],
        'total': row['total'],
        'open': row['open'],
        'resolved': row['resolved'],
        'dismissed': row['dismissed'],
    } for row in rows]
    return Response({'reporters': reporters})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mod_audit(request):
    """The global chronological audit feed.

    The per-user trail in `mod_user_detail` answers "what was done to *this*
    account"; it can't answer "is a moderator quietly working through every
    article on the wiki", which is the question that needs a single stream.
    Optional `?actor=<id>` / `?target=<id>` narrow it; `?action=` filters to
    one kind.
    """
    forbidden = _forbidden(request)
    if forbidden is not None:
        return forbidden
    # Blank params are dropped rather than rejected: `?actor=` has always
    # meant "no filter", and the serializer would read it as a bad integer.
    serializer = AuditFilterSerializer(
        data={
            key: value
            for key, value in request.query_params.items()
            if value != ''
        }
    )
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    filters = serializer.validated_data

    rows = ModAction.objects.select_related(
        'actor', 'target_user', 'article__place', 'revision', 'talk_post',
    )
    if 'actor' in filters:
        rows = rows.filter(actor_id=filters['actor'])
    if 'target' in filters:
        rows = rows.filter(target_user_id=filters['target'])
    if 'action' in filters:
        rows = rows.filter(action=filters['action'])
    # Model Meta.ordering is already ['-created', '-id'].
    rows = rows[:AUDIT_PAGE]
    return Response({'actions': [{
        'id': a.id,
        'action': a.action,
        'actor': a.actor.username if a.actor else None,
        'target_user': a.target_user.username if a.target_user else None,
        'reason': a.reason,
        'created': _iso(a.created),
        # Where the acted-on thing lives, so a row is clickable. Articles and
        # revisions both resolve to a place; a talk post links to its place
        # too (threads hang off the Place, not the Article).
        'place_slug': (
            a.article.place.slug if a.article_id
            else None
        ),
    } for a in rows]})
