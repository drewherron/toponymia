import json

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.geos import Polygon
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Cast
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import resolve as resolution
from .articles import save_edit
from .models import (
    Article,
    ModAction,
    Place,
    Report,
    Revision,
    TalkPost,
    TalkThread,
)
from .moderation import (
    active_ban,
    ban_message,
    banned_response,
    is_admin,
    is_moderator,
    log_action,
)
from .overpass import QID_RE, OverpassError
from .serializers import (
    ArticleDeleteSerializer,
    ArticleEditSerializer,
    ProtectionSerializer,
    ReportActionSerializer,
    ReportSerializer,
    RevertSerializer,
    TalkPostSerializer,
    TalkThreadSerializer,
)
from .slugs import place_by_slug
from .throttles import (
    ReportThrottle,
    ResolveThrottle,
    TalkThrottle,
    WriteThrottle,
)

MAX_HIGHLIGHTS = 500
MAX_SEARCH_RESULTS = 8
MAX_REPORTS = 100


def published_places():
    """Places whose article is live — written *and* not deleted (M13).

    The single definition of "has an article" for every public listing
    surface (highlights, search, random, sitemap), so a deleted article
    can't linger in one of them because a filter was missed.
    """
    return Place.objects.filter(
        article__current_revision__isnull=False,
        article__deleted__isnull=True,
    )


def can_edit_article(user, article):
    """Article protection. Anonymous editing is already
    disallowed everywhere, so `none`/`registered` gate the same set (any
    logged-in user); `admin` restricts edits and reverts to moderators.
    A stub with no Article row yet is unprotected."""
    if not user.is_authenticated:
        return False
    if article is None:
        return True
    if article.protection_level == Article.Protection.ADMIN:
        return is_moderator(user)
    return True


@api_view(['GET'])
def health(request):
    return Response({'status': 'ok'})


@api_view(['GET'])
@ensure_csrf_cookie
def me(request):
    """Session probe for the SPA; also plants the CSRF cookie the client
    needs before it can POST to /api or /_allauth."""
    user = request.user
    if not user.is_authenticated:
        return Response({'user': None})
    ban = active_ban(user)
    return Response(
        {
            'user': {
                'id': user.id,
                'username': user.username,
                'is_moderator': is_moderator(user),
                # Admins get the role controls and the whole-roster view in
                # the Moderation dashboard.
                'is_admin': user.is_superuser,
                # The SPA shows a suspension banner and hides write
                # affordances when this is set.
                'suspended': ban_message(ban) if ban is not None else None,
            }
        }
    )


def _place_json(place):
    return {
        'id': place.id,
        'slug': place.slug,
        'display_name': place.display_name,
        'feature_class': place.feature_class,
        'anchor_level': place.anchor_level,
        'wikidata_qid': place.wikidata_qid,
        'osm_type': place.osm_type,
        'osm_id': place.osm_id,
        'centroid': [place.centroid.x, place.centroid.y],
        # A point guaranteed to lie on the feature — where search/deep
        # links should fly the map. bbox extent lets the client fit big
        # features (a river) instead of zooming to one point on them.
        'label_point': (
            [place.label_point.x, place.label_point.y]
            if place.label_point
            else None
        ),
        'bbox': list(place.bbox.extent) if place.bbox else None,
    }


@api_view(['POST'])
@throttle_classes([ResolveThrottle])
def resolve(request):
    data = request.data
    name = data.get('name')
    # the English-first label the client displayed (optional)
    name_en = data.get('name_en')
    feature_class = data.get('class')
    lng_lat = data.get('lngLat')
    zoom = data.get('zoom')
    # optional Wikidata hint from a caller that already knows the entity
    qid = data.get('qid')

    if (
        not isinstance(name, str) or not name.strip()
        or not (isinstance(name_en, str) or name_en is None)
        or not isinstance(feature_class, str) or not feature_class
        or not isinstance(lng_lat, (list, tuple)) or len(lng_lat) != 2
        or not all(isinstance(c, (int, float)) for c in lng_lat)
        or not (isinstance(zoom, (int, float)) or zoom is None)
        or not (qid is None or (isinstance(qid, str) and QID_RE.match(qid)))
    ):
        return Response(
            {
                'error': 'expected {name, class, lngLat: [lng, lat], '
                         'zoom?, qid?}'
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    name_en = name_en.strip() or None if name_en else None
    lng, lat = lng_lat
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return Response(
            {'error': 'lngLat out of range'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        place, created = resolution.resolve(
            name.strip(), feature_class, lng, lat, zoom, name_en, qid=qid
        )
    except OverpassError:
        return Response(
            {'error': 'resolution service unavailable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response({'place': _place_json(place), 'created': created})


@api_view(['GET'])
def highlights(request):
    """Places with articles in the viewport, as centroid GeoJSON.

    The client recolors basemap labels matching `names`
    and paints dots at the centroids in all-articles mode. Inclusion is
    tested against cached geometry/bbox too, so a river whose centroid is
    far away still lights its labels inside the viewport.
    """
    raw = request.query_params.get('bbox', '')
    try:
        min_lng, min_lat, max_lng, max_lat = (
            float(value) for value in raw.split(',')
        )
    except ValueError:
        return Response(
            {'error': 'expected bbox=minLng,minLat,maxLng,maxLat'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    min_lat, max_lat = max(min_lat, -90.0), min(max_lat, 90.0)
    # A wrapped or antimeridian-crossing viewport degenerates to the full
    # longitude range: over-fetches a little, never misses a highlight.
    if not (-180 <= min_lng < max_lng <= 180):
        min_lng, max_lng = -180.0, 180.0
    if min_lat >= max_lat:
        return Response({'type': 'FeatureCollection', 'features': []})

    viewport = Polygon.from_bbox((min_lng, min_lat, max_lng, max_lat))
    viewport.srid = 4326
    # Cast the geography columns to planar geometry: a viewport is a
    # lon/lat rectangle, but geography edges are great-circle arcs, which
    # misbehave for boxes wider than 180 degrees.
    planar = GeometryField(srid=4326)
    places = published_places().annotate(
        geometry_plane=Cast('geometry', planar),
        bbox_plane=Cast('bbox', planar),
        centroid_plane=Cast('centroid', planar),
    ).filter(
        Q(geometry_plane__intersects=viewport)
        | Q(bbox_plane__intersects=viewport)
        | Q(centroid_plane__intersects=viewport)
    ).prefetch_related('names')[:MAX_HIGHLIGHTS]

    features = []
    for place in places:
        names = {place.display_name}
        names.update(entry.name for entry in place.names.all())
        features.append(
            {
                'type': 'Feature',
                'geometry': json.loads(
                    (place.label_point or place.centroid).geojson
                ),
                'properties': {
                    'slug': place.slug,
                    'display_name': place.display_name,
                    'feature_class': place.feature_class,
                    'names': sorted(names),
                },
            }
        )
    return Response({'type': 'FeatureCollection', 'features': features})


@api_view(['GET'])
def search(request):
    """Find our own articles by any of their names.

    Only places with a published article are returned — the "everything
    else on Earth" half of the search box is the client-side geocoder.
    Matches the display name and the materialized PlaceNames, so the
    French exonym finds the place too; `matched_name` says which alias
    hit when the display name itself didn't.
    """
    query = request.query_params.get('q', '').strip()
    if len(query) < 2:
        return Response({'results': []})
    places = (
        published_places()
        .filter(
            Q(display_name__icontains=query)
            | Q(names__name__icontains=query)
        )
        .annotate(
            prefix_rank=Case(
                When(display_name__istartswith=query, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by('prefix_rank', 'display_name', 'id')
        .distinct()
        .prefetch_related('names')[:MAX_SEARCH_RESULTS]
    )
    lowered = query.lower()
    results = []
    for place in places:
        matched_name = None
        if lowered not in place.display_name.lower():
            matched_name = next(
                (
                    entry.name
                    for entry in place.names.all()
                    if lowered in entry.name.lower()
                ),
                None,
            )
        results.append({**_place_json(place), 'matched_name': matched_name})
    return Response({'results': results})


@api_view(['GET'])
def random_article(request):
    """A random place that has an article; null when the wiki is empty.
    order_by('?') is fine at wiki scale."""
    place = published_places().order_by('?').first()
    return Response({'place': _place_json(place) if place else None})


def _article_json(article):
    revision = article.current_revision
    if revision is None:
        return None
    return {
        'content': revision.content,
        'revision_id': revision.id,
        'author': revision.author.username,
        'created': revision.created.isoformat(),
        'comment': revision.comment,
        'protection_level': article.protection_level,
    }


@api_view(['GET'])
def place_detail(request, slug):
    place = place_by_slug(
        slug,
        Place.objects.select_related('article__current_revision__author'),
    )
    article = getattr(place, 'article', None)
    # A deleted article reads as a plain stub to everyone but an admin, who
    # gets the content back plus the banner and Restore.
    hidden = (
        article is not None
        and article.deleted is not None
        and not is_admin(request.user)
    )
    return Response(
        {
            'place': _place_json(place),
            'article': (
                _article_json(article)
                if article is not None and not hidden
                else None
            ),
            # Top-level so a locked *stub* (Article row, no revision yet)
            # still reports its protection to gate the write button.
            'protection_level': (
                article.protection_level
                if article
                else Article.Protection.NONE
            ),
            # Admin-only: null for everyone else, so the public can't even
            # tell a deleted article from a never-written one.
            'deleted': (
                {
                    'at': article.deleted.isoformat(),
                    'by': (
                        article.deleted_by.username
                        if article.deleted_by
                        else None
                    ),
                }
                if article is not None
                and article.deleted is not None
                and is_admin(request.user)
                else None
            ),
        }
    )


@api_view(['GET'])
def place_geometry(request, slug):
    """The place's cached course as bare GeoJSON, for the focus highlight.

    Deliberately its own endpoint rather than a field on place_detail:
    the detail response is fetched on every article open, and the
    Mississippi's geometry is 47 kB. This is fetched only when the user
    presses "zoom to place", so most readers never pay for it.

    `{"geometry": null}` for a place that caches none — area relations
    (cities, countries) store centroid+bbox only, so they simply don't
    highlight.
    """
    place = place_by_slug(slug, Place.objects.only('geometry'))
    response = Response(
        {
            'geometry': (
                json.loads(place.geometry.geojson) if place.geometry else None
            )
        }
    )
    # Only changes when the place is re-resolved, which mints no new URL —
    # so allow a short cache, not an indefinite one.
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def _revision_json(revision, current_id, with_content=False):
    data = {
        'id': revision.id,
        'author': revision.author.username,
        'created': revision.created.isoformat(),
        'comment': revision.comment,
        'is_current': revision.id == current_id,
        # Only ever true in responses to moderators — public lists filter
        # suppressed revisions out entirely.
        'suppressed': revision.suppressed is not None,
    }
    if with_content:
        data['content'] = revision.content
    return data


@api_view(['GET'])
def revision_list(request, slug):
    """Edit history of a place's article, newest first. A place without
    an article has an empty history rather than a 404 — the History tab
    is shown for stubs too."""
    place = place_by_slug(slug, Place.objects.select_related('article'))
    article = getattr(place, 'article', None)
    if article is None:
        return Response({'revisions': []})
    # A deleted article is a stub to the public — and that has to include its
    # history, or the content stays readable in the History tab and the
    # deletion means nothing.
    if article.deleted is not None and not is_admin(request.user):
        return Response({'revisions': []})
    revisions = article.revisions.select_related('author')
    # Suppressed revisions are hidden from public history but stay visible
    # to moderators.
    if not is_moderator(request.user):
        revisions = revisions.filter(suppressed__isnull=True)
    return Response(
        {
            'revisions': [
                _revision_json(revision, article.current_revision_id)
                for revision in revisions
            ]
        }
    )


@api_view(['GET'])
def revision_detail(request, slug, revision_id):
    place = place_by_slug(slug)
    revision = get_object_or_404(
        Revision.objects.select_related('author', 'article'),
        id=revision_id,
        article__place=place,
    )
    if revision.suppressed is not None and not is_moderator(request.user):
        return Response(status=status.HTTP_404_NOT_FOUND)
    # Same reason as revision_list: a deleted article's snapshots must not be
    # readable by slug+id either.
    if revision.article.deleted is not None and not is_admin(request.user):
        return Response(status=status.HTTP_404_NOT_FOUND)
    return Response(
        {
            'revision': _revision_json(
                revision,
                revision.article.current_revision_id,
                with_content=True,
            )
        }
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([WriteThrottle])
def article_revert(request, slug):
    """Revert = a new revision copying an old snapshot."""
    blocked = banned_response(request.user)
    if blocked is not None:
        return blocked
    serializer = RevertSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    place = place_by_slug(slug, Place.objects.select_related('article'))
    article = getattr(place, 'article', None)
    if not can_edit_article(request.user, article):
        return Response(
            {'error': 'this article is protected'},
            status=status.HTTP_403_FORBIDDEN,
        )
    old = get_object_or_404(
        Revision,
        id=serializer.validated_data['revision_id'],
        article__place=place,
    )
    if article.current_revision_id == old.id:
        return Response(
            {'error': 'already the current revision'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    comment = (
        serializer.validated_data['comment']
        or f'Reverted to revision {old.id}'
    )
    revision = save_edit(place, request.user, old.content, comment)
    # Revert is a content tool anyone may use, but in a moderator's hands it
    # is also the most destructive one available and used to leave no trace
    # at all — a rogue mod could blank the wiki through this path and the
    # audit log would be empty.
    if is_moderator(request.user):
        log_action(
            request.user, ModAction.Action.REVERT_ARTICLE,
            target_user=old.author, reason=comment,
            article=revision.article, revision=revision,
        )
    return Response({'article': _article_json(revision.article)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([WriteThrottle])
def article_delete(request, slug):
    """Soft-delete a whole article (admin only).

    "This article shouldn't exist" — distinct from revert (content is wrong)
    and from suppression (one revision is abusive). Every revision stays in
    the table untouched; the place simply reads as a stub until an admin
    restores it or anyone writes a new revision.
    """
    if not is_admin(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    serializer = ArticleDeleteSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    place = place_by_slug(
        slug,
        Place.objects.select_related('article__current_revision__author'),
    )
    article = getattr(place, 'article', None)
    if article is None or article.current_revision_id is None:
        return Response(
            {'error': 'this place has no article to delete'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    reason = serializer.validated_data.get('reason', '')
    if article.deleted is None:
        article.deleted = timezone.now()
        article.deleted_by = request.user
        article.save(update_fields=['deleted', 'deleted_by'])
    log_action(
        request.user, ModAction.Action.DELETE_ARTICLE,
        target_user=article.current_revision.author, reason=reason,
        article=article, revision=article.current_revision,
    )
    return Response({'deleted': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([WriteThrottle])
def article_restore(request, slug):
    """Un-delete an article — the inverse of article_delete (admin only).

    Note this restores the article *as it was*: any revision suppressed
    while it was down stays suppressed, because that flag is its own.
    """
    if not is_admin(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    place = place_by_slug(
        slug,
        Place.objects.select_related('article__current_revision__author'),
    )
    article = getattr(place, 'article', None)
    if article is None:
        return Response(status=status.HTTP_404_NOT_FOUND)
    if article.deleted is not None:
        article.deleted = None
        article.deleted_by = None
        article.save(update_fields=['deleted', 'deleted_by'])
        log_action(
            request.user, ModAction.Action.RESTORE_ARTICLE,
            target_user=(
                article.current_revision.author
                if article.current_revision
                else None
            ),
            article=article, revision=article.current_revision,
        )
    return Response({'article': _article_json(article)})


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
@throttle_classes([WriteThrottle])
def article_edit(request, slug):
    blocked = banned_response(request.user)
    if blocked is not None:
        return blocked
    place = place_by_slug(slug, Place.objects.select_related('article'))
    if not can_edit_article(request.user, getattr(place, 'article', None)):
        return Response(
            {'error': 'this article is protected'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = ArticleEditSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    revision = save_edit(
        place,
        request.user,
        serializer.validated_data['content'],
        serializer.validated_data['comment'],
    )
    return Response({'article': _article_json(revision.article)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def article_protection(request, slug):
    """Set an article's protection level — moderators only.
    Creates the Article row if the place is still a stub, so a place can
    be locked down pre-emptively."""
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    serializer = ProtectionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    place = place_by_slug(slug)
    article, _ = Article.objects.get_or_create(place=place)
    article.protection_level = serializer.validated_data['protection_level']
    article.save(update_fields=['protection_level'])
    return Response({'protection_level': article.protection_level})


def _post_json(post):
    # A soft-deleted post stays as a tombstone (thread coherence) but its
    # body is withheld from everyone.
    deleted = post.deleted is not None
    return {
        'id': post.id,
        'author': post.author.username,
        'body_md': '' if deleted else post.body_md,
        'created': post.created.isoformat(),
        'edited': post.edited.isoformat() if post.edited else None,
        'deleted': deleted,
    }


def _thread_json(thread):
    return {
        'id': thread.id,
        'title': thread.title,
        'created': thread.created.isoformat(),
        'posts': [_post_json(post) for post in thread.posts.all()],
    }


@api_view(['GET', 'POST'])
def talk(request, slug):
    """Threaded discussion for a Place. GET is public; POST (new thread
    with its opening post) needs an account."""
    place = place_by_slug(slug)
    if request.method == 'GET':
        # Deleted threads drop out of the list; deleted posts stay as
        # tombstones so replies still make sense.
        threads = place.talk_threads.filter(
            deleted__isnull=True
        ).prefetch_related('posts__author')
        return Response(
            {'threads': [_thread_json(thread) for thread in threads]}
        )

    if not request.user.is_authenticated:
        return Response(status=status.HTTP_403_FORBIDDEN)
    blocked = banned_response(request.user)
    if blocked is not None:
        return blocked
    serializer = TalkThreadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    thread = TalkThread.objects.create(
        place=place, title=serializer.validated_data['title']
    )
    TalkPost.objects.create(
        thread=thread,
        author=request.user,
        body_md=serializer.validated_data['body_md'],
    )
    return Response(
        {'thread': _thread_json(thread)}, status=status.HTTP_201_CREATED
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([TalkThrottle])
def talk_reply(request, thread_id):
    blocked = banned_response(request.user)
    if blocked is not None:
        return blocked
    thread = get_object_or_404(TalkThread, id=thread_id)
    serializer = TalkPostSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    post = TalkPost.objects.create(
        thread=thread,
        author=request.user,
        body_md=serializer.validated_data['body_md'],
    )
    return Response({'post': _post_json(post)}, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
@throttle_classes([TalkThrottle])
def talk_post_edit(request, post_id):
    """Edit-own only; moderators delete via the queue instead."""
    blocked = banned_response(request.user)
    if blocked is not None:
        return blocked
    post = get_object_or_404(TalkPost, id=post_id)
    if post.author_id != request.user.id:
        return Response(
            {'error': 'you can only edit your own posts'},
            status=status.HTTP_403_FORBIDDEN,
        )
    if post.deleted is not None:
        return Response(
            {'error': 'this post has been removed'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    serializer = TalkPostSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    post.body_md = serializer.validated_data['body_md']
    post.edited = timezone.now()
    post.save(update_fields=['body_md', 'edited'])
    return Response({'post': _post_json(post)})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def talk_post_delete(request, post_id):
    """Soft-delete a post: its own author or a moderator."""
    post = get_object_or_404(
        TalkPost.objects.select_related('author'), id=post_id
    )
    is_own = post.author_id == request.user.id
    if not is_own and not is_moderator(request.user):
        return Response(
            {'error': 'you can only delete your own posts'},
            status=status.HTTP_403_FORBIDDEN,
        )
    if post.deleted is None:
        post.deleted = timezone.now()
        post.deleted_by = request.user
        post.save(update_fields=['deleted', 'deleted_by'])
        # A moderator taking down someone else's post is an audited action;
        # authors tidying their own posts are not.
        if not is_own:
            log_action(
                request.user, ModAction.Action.DELETE_POST,
                target_user=post.author, talk_post=post,
            )
    return Response({'post': _post_json(post)})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def talk_thread_delete(request, thread_id):
    """Soft-delete a whole thread — moderators only."""
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    thread = get_object_or_404(TalkThread, id=thread_id)
    if thread.deleted is None:
        thread.deleted = timezone.now()
        thread.deleted_by = request.user
        thread.save(update_fields=['deleted', 'deleted_by'])
        log_action(
            request.user, ModAction.Action.DELETE_THREAD,
            reason=f'thread "{thread.title}"',
        )
    return Response({'ok': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ReportThrottle])
def create_report(request):
    """Flag a revision or a talk post for moderator attention. Re-filing
    an already-open report of the same target is idempotent."""
    blocked = banned_response(request.user)
    if blocked is not None:
        return blocked
    serializer = ReportSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    data = serializer.validated_data
    target = data['target_type']
    lookup = {'revision': Revision, 'talk_post': TalkPost}[target]
    obj = get_object_or_404(lookup, id=data['target_id'])
    if obj.author_id == request.user.id:
        return Response(
            {'error': 'you cannot report your own content'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    report, _ = Report.objects.get_or_create(
        reporter=request.user,
        status=Report.Status.OPEN,
        **{target: obj},
        defaults={'reason': data['reason'], 'category': data['category']},
    )
    return Response(
        {'report': {'id': report.id, 'status': report.status}},
        status=status.HTTP_201_CREATED,
    )


def _revision_excerpt(content):
    """Body text when a (legacy) revision has it, else the first name
    etymology — name-only snapshots still need queue context."""
    if content.get('body_md', '').strip():
        return content['body_md'][:280]
    for entry in content.get('names', []):
        if entry.get('etymology_md', '').strip():
            return entry['etymology_md'][:280]
    return ''


def _report_json(report):
    """A queue row with just enough target context to triage without a
    round-trip: what was said, by whom, and where to find it."""
    target = None
    if report.revision_id is not None:
        revision = report.revision
        place = revision.article.place
        suppressed = revision.suppressed is not None
        target = {
            'kind': 'revision',
            'id': revision.id,
            'author': revision.author.username,
            'comment': '' if suppressed else revision.comment,
            'excerpt': '' if suppressed else _revision_excerpt(revision.content),
            'slug': place.slug,
            'place': place.display_name,
            'is_current': revision.article.current_revision_id == revision.id,
            'suppressed': suppressed,
        }
    elif report.talk_post_id is not None:
        post = report.talk_post
        place = post.thread.place
        target = {
            'kind': 'talk_post',
            'id': post.id,
            'thread_id': post.thread_id,
            'thread_title': post.thread.title,
            'author': post.author.username,
            'excerpt': '' if post.deleted else post.body_md[:280],
            'slug': place.slug,
            'place': place.display_name,
            'deleted': post.deleted is not None,
        }
    return {
        'id': report.id,
        'category': report.category,
        'reason': report.reason,
        'reporter': report.reporter.username,
        'created': report.created.isoformat(),
        'target': target,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mod_reports(request):
    """The moderator queue: open reports, newest first."""
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    reports = (
        Report.objects.filter(status=Report.Status.OPEN)
        .select_related(
            'reporter',
            'revision__author',
            'revision__article__place',
            'revision__article__current_revision',
            'talk_post__author',
            'talk_post__thread__place',
        )[:MAX_REPORTS]
    )
    return Response(
        {'reports': [_report_json(report) for report in reports]}
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mod_report_action(request, report_id):
    """Resolve a report. `delete` soft-deletes the reported target (talk
    posts only; revisions are undone by reverting), then resolves."""
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    serializer = ReportActionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    report = get_object_or_404(Report, id=report_id)
    action = serializer.validated_data['action']
    reason = serializer.validated_data.get('reason', '')
    # Whose content the report targets — recorded on every audit row so the
    # dashboard's problem-user view can aggregate by author.
    post = report.talk_post
    revision = report.revision
    target_user = post.author if post is not None else revision.author

    if action == 'delete':
        if post is None:
            return Response(
                {'error': 'only talk posts can be deleted from the queue'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if post.deleted is None:
            post.deleted = timezone.now()
            post.deleted_by = request.user
            post.save(update_fields=['deleted', 'deleted_by'])
        log_action(
            request.user, ModAction.Action.DELETE_POST,
            target_user=target_user, reason=reason, talk_post=post,
            report=report,
        )
    elif action == 'suppress':
        if revision is None:
            return Response(
                {'error': 'only revisions can be suppressed from the queue'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if revision.id == revision.article.current_revision_id:
            return Response(
                {'error': 'revert the current revision before suppressing it'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if revision.suppressed is None:
            revision.suppressed = timezone.now()
            revision.suppressed_by = request.user
            revision.save(update_fields=['suppressed', 'suppressed_by'])
        log_action(
            request.user, ModAction.Action.SUPPRESS_REVISION,
            target_user=target_user, reason=reason, revision=revision,
            report=report,
        )
    else:
        log_action(
            request.user,
            ModAction.Action.DISMISS_REPORT
            if action == 'dismiss'
            else ModAction.Action.RESOLVE_REPORT,
            target_user=target_user, reason=reason,
            talk_post=post, revision=revision, report=report,
        )

    report.status = (
        Report.Status.DISMISSED
        if action == 'dismiss'
        else Report.Status.RESOLVED
    )
    report.handled_by = request.user
    report.handled_at = timezone.now()
    report.save(update_fields=['status', 'handled_by', 'handled_at'])
    return Response({'report': {'id': report.id, 'status': report.status}})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mod_revision_restore(request, revision_id):
    """Un-suppress a revision — the inverse of a queue `suppress` (moderators
    only)."""
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    revision = get_object_or_404(
        Revision.objects.select_related('author'), id=revision_id
    )
    if revision.suppressed is not None:
        revision.suppressed = None
        revision.suppressed_by = None
        revision.save(update_fields=['suppressed', 'suppressed_by'])
        log_action(
            request.user, ModAction.Action.RESTORE_REVISION,
            target_user=revision.author, revision=revision,
        )
    return Response({'ok': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mod_talk_post_restore(request, post_id):
    """Restore a soft-deleted talk post — the inverse of a queue `delete`
    (moderators only)."""
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    post = get_object_or_404(
        TalkPost.objects.select_related('author'), id=post_id
    )
    if post.deleted is not None:
        post.deleted = None
        post.deleted_by = None
        post.save(update_fields=['deleted', 'deleted_by'])
        log_action(
            request.user, ModAction.Action.RESTORE_POST,
            target_user=post.author, talk_post=post,
        )
    return Response({'ok': True})
