import json
from math import isfinite

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.geos import Polygon
from django.db.models import (
    BooleanField,
    Case,
    Exists,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Value,
    When,
)
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
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from . import accounts
from . import resolve as resolution
from .articles import save_edit
from .feature_classes import DisallowedFeatureClass, disallowed_message
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
from .notify import notify_new_report, notify_report_outcome
from .overpass import QID_RE, OverpassError
from .overpass import budget as overpass_budget
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
    TalkWriteThrottle,
    WriteThrottle,
)

# What one resolve request may spend on Overpass, retries and every
# follow-up call included. Comfortably inside gunicorn's --timeout (60 s in
# deploy/box/toponymia.service) so the worker is never killed mid-request,
# and short enough that a click either answers or fails while someone is
# still watching. The calls that decide identity run first, so when the
# budget does run out it costs cached geometry and a slug qualifier rather
# than the resolution itself.
RESOLVE_OVERPASS_BUDGET_S = 25

MAX_HIGHLIGHTS = 500
# Contributions ship in one response rather than per viewport, so this cap
# is the whole lens, not a page of it. Well clear of what a person edits by
# hand; past it the client says the view is partial rather than quietly
# dropping the tail.
MAX_CONTRIBUTIONS = 500
MAX_SEARCH_RESULTS = 8
MAX_REPORTS = 100
# Revision history and talk are both unbounded in the data model: a
# vandal-and-revert war produces hundreds of revisions in an afternoon, and
# nothing caps threads per place or posts per thread. Both endpoints are
# public, so the whole response used to be built in memory on demand.
#
# History is paginated rather than merely capped — dropping old revisions
# would make them unreachable, and a wiki's history has to stay complete.
# Talk is capped per request with an explicit `has_more`, since threads read
# oldest-first and a page boundary is the natural place to stop.
#
# The history page size is set by what reads well in a side pane, not by
# what the query can afford: 100 rows is a wall of near-identical bylines
# you have to scroll past to reach "Load more", and the recent edits are
# what anyone actually came for. 25 fills the pane about once over.
MAX_REVISIONS_PER_PAGE = 25
MAX_TALK_THREADS = 50
MAX_TALK_POSTS = 200
# Longest search term we'll run. Every query becomes an unanchored ILIKE
# against display_name and every PlaceName, so length is paid for per row —
# and the endpoint is anonymous. Comfortably past the longest real toponym
# (Taumatawhakatangihangakoauauotamateaturipukakapikimaungahoronukupokaiwhen
# uakitanatahu, 85 characters). Over-long input is truncated rather than
# rejected: nothing is named this much, so it degrades to "no results"
# instead of erroring on someone who pasted junk into the search box.
MAX_SEARCH_QUERY = 120


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


def published_q():
    """`published_places`'s test as a Q, for combining under an OR.

    Same definition, expressed so it can sit inside a larger filter —
    contributions needs "published AND edited by you, OR talked on by
    you", which a pre-filtered queryset can't express.
    """
    return Q(
        article__current_revision__isnull=False,
        article__deleted__isnull=True,
    )


def has_article_case():
    """`published_q()` as an annotatable boolean, for tagging a dot's tier.

    The two dot endpoints both need "does anyone's article stand here?" as
    a value rather than a filter — highlights to order by it, both to name
    the tier. One definition, so a hollow ring means the same thing under
    "All articles" and under the contributions lens.
    """
    return Case(
        When(published_q(), then=Value(True)),
        default=Value(False),
        output_field=BooleanField(),
    )


def live_talk(**extra):
    """Exists() over talk that's still standing, for a Place OuterRef.

    "Discussed" means a live post in a live thread: deleting either takes
    the discussion off every listing surface, the same way deleting an
    article takes it out of `published_places`. `extra` narrows it further
    (contributions passes `author=`).
    """
    return Exists(
        TalkPost.objects.filter(
            thread__place=OuterRef('pk'),
            thread__deleted__isnull=True,
            deleted__isnull=True,
            **extra,
        )
    )


def _dot_feature(place, kind):
    """One place as a point Feature, the shape the map's dot layers read.

    `names` carries every alias so the client can match the place against
    whatever the basemap happens to label it. The point is `label_point`
    when we have one — a guaranteed-on-the-feature click — falling back to
    the bbox centroid, which for a long river can sit off it entirely.

    `kind` distinguishes the two highlight tiers ('article' vs 'talk'),
    and means the same thing on every layer that reads it: whether anyone
    has written the place, not what the viewer did there. A place you only
    talked on that someone else has since written up is an 'article'.
    """
    names = {place.display_name}
    names.update(entry.name for entry in place.names.all())
    return {
        'type': 'Feature',
        'geometry': json.loads((place.label_point or place.centroid).geojson),
        'properties': {
            'slug': place.slug,
            'display_name': place.display_name,
            'feature_class': place.feature_class,
            'names': sorted(names),
            'kind': kind,
        },
    }


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
    # Rides along on the probe the SPA already makes, rather than costing a
    # second round trip to allauth's config endpoint for one boolean. Sent
    # logged-out too — that's the reader who might try to register.
    signups_open = not settings.PRELAUNCH
    if not user.is_authenticated:
        return Response({'user': None, 'signups_open': signups_open})
    ban = active_ban(user)
    return Response(
        {
            'signups_open': signups_open,
            'user': {
                'id': user.id,
                'username': user.username,
                # Shown in the account panel so the address can be checked
                # before it is changed.
                'email': user.email,
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
    """Click resolution. Anonymous callers get the database half only:
    a place we already know opens instantly, but creating one — which means
    an outbound Overpass query and a permanent row — needs an account.

    The asymmetry is deliberate. The Overpass traffic leaves under our
    server's IP, so an anonymous script could get us banned from the public
    instances and take the core interaction down with it; tying that spend
    to an account (email-verified, bannable) keeps the cost attributable
    without putting a login wall in front of ordinary browsing.
    """
    banned = banned_response(request.user)
    if banned is not None:
        return banned

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

    allow_create = request.user.is_authenticated
    try:
        # Bound every Overpass call this request makes to one shared
        # deadline. Without it the retry ladders add up past gunicorn's
        # --timeout and the worker is killed mid-request, which reaches the
        # user as a 502 rather than as the 503 below. Management commands
        # call resolve() without a budget on purpose: they can wait, and
        # nothing is killing them.
        with overpass_budget(RESOLVE_OVERPASS_BUDGET_S):
            place, created = resolution.resolve(
                name.strip(), feature_class, lng, lat, zoom, name_en,
                qid=qid, allow_create=allow_create,
            )
    except DisallowedFeatureClass:
        # The UI never offers these, so reaching here means a hand-made
        # request or a category the allowlist hasn't been taught yet. Name
        # the class: the second case is a bug report we want to receive.
        # Built from the request's own value, not from the exception, so no
        # exception object ever reaches a response body.
        return Response(
            {
                'error': disallowed_message(feature_class),
                'reason': 'disallowed_class',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    except OverpassError:
        return Response(
            {'error': 'resolution service unavailable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if place is None:
        # Anonymous, and we don't already know this place. 401 rather than
        # 403: the client turns it into a sign-in prompt, not an error.
        return Response(
            {
                'error': 'sign in to look up a place for the first time',
                'reason': 'signin_required',
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return Response({'place': _place_json(place), 'created': created})


@api_view(['GET'])
def highlights(request):
    """Places with articles — or only discussion — in the viewport.

    The client recolors basemap labels matching `names`
    and paints dots at the centroids in all-articles mode. Inclusion is
    tested against cached geometry/bbox too, so a river whose centroid is
    far away still lights its labels inside the viewport.

    Two tiers, told apart by each feature's `kind`. 'article' is a live
    article: amber label text, filled dot. 'talk' is a place someone has
    opened a discussion on without writing it yet — a wanted page. Those
    get much darker amber label text and a hollow dot, outlined where an
    article is filled, so the symbol promises discussion rather than an
    article and clicking through to a stub isn't a surprise. A place with
    both is an 'article': the article is the stronger claim, and its talk
    is one tab away.
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
    # float() also accepts 'nan' and 'inf'. NaN in particular slips past
    # every range guard below (all comparisons against it are False) and
    # reaches GEOS, which raises on the degenerate ring — a 500 from a
    # query string. Reject non-finite values up front.
    if not all(
        isfinite(value)
        for value in (min_lng, min_lat, max_lng, max_lat)
    ):
        return Response(
            {'error': 'bbox values must be finite numbers'},
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
    places = Place.objects.annotate(
        geometry_plane=Cast('geometry', planar),
        bbox_plane=Cast('bbox', planar),
        centroid_plane=Cast('centroid', planar),
        has_article=has_article_case(),
        has_talk=live_talk(),
    ).filter(
        Q(has_article=True) | Q(has_talk=True)
    ).filter(
        Q(geometry_plane__intersects=viewport)
        | Q(bbox_plane__intersects=viewport)
        | Q(centroid_plane__intersects=viewport)
    # Articles first, so a dense viewport that hits the cap loses wanted
    # pages rather than written ones. `id` only makes the cut deterministic.
    ).prefetch_related('names').order_by('-has_article', 'id')[:MAX_HIGHLIGHTS]

    features = [
        _dot_feature(place, 'article' if place.has_article else 'talk')
        for place in places
    ]
    return Response({'type': 'FeatureCollection', 'features': features})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def contributions(request):
    """Every place the signed-in user has worked on, as centroid GeoJSON.

    The map's "your contributions" lens. Unlike `highlights` this is not
    viewport-scoped: the question it answers is "where have I been?",
    which a viewport query can't answer without the user first guessing
    where to look. One person's contributions are a small set, so the
    whole footprint ships at once and the client frames `bbox`.

    Contributing means either authoring a revision or posting to talk.
    Talk attaches to the Place, not the Article, so a discussion on a
    stub counts and lands a dot on a place with nothing written yet —
    but an edit only counts while its article is actually live, so a
    deleted article stays out of this listing like every other one.

    Dots carry the same `kind` as `highlights`, and it answers the same
    question there as here: whether the place has been written, not what
    you did on it. So a place you only argued about, that someone else has
    since written up, is a filled dot — the ring means "still unwritten"
    everywhere it appears, rather than quietly meaning "you only talked"
    on this one layer.
    """
    user = request.user
    edited = Revision.objects.filter(
        article__place=OuterRef('pk'), author=user
    )
    # A deleted post is withheld from the thread it's in, so it shouldn't
    # keep pinning a dot to the map either.
    talked = live_talk(author=user)
    places = (
        Place.objects.annotate(has_article=has_article_case())
        .filter((Q(has_article=True) & Q(Exists(edited))) | Q(talked))
        .prefetch_related('names')
        .order_by('display_name', 'id')[: MAX_CONTRIBUTIONS + 1]
    )
    places = list(places)
    truncated = len(places) > MAX_CONTRIBUTIONS
    features = [
        _dot_feature(place, 'article' if place.has_article else 'talk')
        for place in places[:MAX_CONTRIBUTIONS]
    ]

    # Framing box over the dots themselves, so the client's fitBounds
    # lands on exactly what it's about to draw. Planar min/max: a set
    # spanning the antimeridian frames the long way round, which is
    # merely a wide view, not a wrong one.
    bbox = None
    if features:
        lngs = [f['geometry']['coordinates'][0] for f in features]
        lats = [f['geometry']['coordinates'][1] for f in features]
        bbox = [min(lngs), min(lats), max(lngs), max(lats)]

    return Response(
        {
            'type': 'FeatureCollection',
            'features': features,
            'bbox': bbox,
            'truncated': truncated,
        }
    )


@api_view(['GET'])
def search(request):
    """Find our own articles by any of their names.

    Only places with a published article are returned — the "everything
    else on Earth" half of the search box is the client-side geocoder.
    Matches the display name and the materialized PlaceNames, so the
    French exonym finds the place too; `matched_name` says which alias
    hit when the display name itself didn't.
    """
    query = request.query_params.get('q', '').strip()[:MAX_SEARCH_QUERY]
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
    """A random place that has an article; null when there's none to give.

    `?not=<slug>` drops the article the reader is already on, so the button
    always takes them somewhere — on a small wiki an unfiltered draw keeps
    landing on the open one. The slug the client holds is always canonical
    (every place reaches it through `_place_json`), so a plain `slug` match
    is enough; an alias would simply fail to exclude, which is the old
    behaviour. A one-article wiki leaves nothing to draw and answers with
    the same null an empty one does.

    order_by('?') is fine at wiki scale.
    """
    places = published_places()
    exclude = request.GET.get('not')
    if exclude:
        places = places.exclude(slug=exclude)
    place = places.order_by('?').first()
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
            # Drives the count on the Talk tab, so a stub with discussion
            # on it doesn't read as empty before you click through. Counts
            # live threads only — the same filter the talk listing applies,
            # since a number that disagrees with the list under it is worse
            # than no number. Deliberately *not* the map's wanted-page test,
            # which also demands a live post: a thread whose posts are all
            # deleted still lists as a tombstone and still belongs in this
            # count, while there's nothing readable there for the map to
            # advertise.
            'talk_thread_count': place.talk_threads.filter(
                deleted__isnull=True
            ).count(),
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


def _revision_json(revision, current_id, with_content=False,
                   for_moderator=False, reported=False):
    """One history row.

    A suppressed revision still renders publicly, as a tombstone: author and
    timestamp survive, the comment and the snapshot do not. That split is
    deliberate and it is a licensing requirement, not a UI preference — the
    history *is* how we satisfy CC BY-SA attribution for text that is still
    live. An editor whose good prose survives in the current article, under a
    later editor's revision, is owed credit for it even if a separate edit of
    theirs was abusive enough to hide. Dropping their rows entirely would
    leave us distributing their words with the attribution stripped.

    The snapshot itself is never served for a suppressed revision (see
    `revision_detail`), so nothing hidden leaks through the tombstone.
    """
    suppressed = revision.suppressed is not None
    data = {
        'id': revision.id,
        'author': revision.author.username,
        'created': revision.created.isoformat(),
        # The comment is authored text like any other, so a suppressed row
        # withholds it — an edit summary is a perfectly good place to put a
        # slur, and hiding the snapshot while printing the summary would
        # defeat the whole removal.
        'comment': '' if suppressed and not for_moderator else revision.comment,
        'is_current': revision.id == current_id,
        'suppressed': suppressed,
        # Viewer-relative, as on a talk post — see `_post_json`.
        'reported': reported,
    }
    if with_content:
        data['content'] = revision.content
    return data


# Same shape as a populated page, so the client has one contract to code
# against whether or not the place has an article.
_EMPTY_HISTORY = {'revisions': [], 'total': 0, 'offset': 0, 'has_more': False}


@api_view(['GET'])
def revision_list(request, slug):
    """Edit history of a place's article, newest first. A place without
    an article has an empty history rather than a 404 — the History tab
    is shown for stubs too."""
    place = place_by_slug(slug, Place.objects.select_related('article'))
    article = getattr(place, 'article', None)
    if article is None:
        return Response(_EMPTY_HISTORY)
    # A deleted article is a stub to the public — and that has to include its
    # history, or the content stays readable in the History tab and the
    # deletion means nothing.
    if article.deleted is not None and not is_admin(request.user):
        return Response(_EMPTY_HISTORY)
    revisions = article.revisions.select_related('author')
    # Suppressed revisions stay in the list for everyone — as tombstones for
    # the public (see `_revision_json`), in full for moderators. They are not
    # filtered out: the row is the attribution record for text that may still
    # be live, and a history with gaps in it also tells a reader exactly which
    # revisions were hidden, which is the opposite of discreet.
    moderator = is_moderator(request.user)

    total = revisions.count()
    try:
        offset = int(request.query_params.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, min(offset, total))
    # One extra row tells us whether another page exists without a second
    # COUNT, and is discarded before serializing.
    window = list(revisions[offset : offset + MAX_REVISIONS_PER_PAGE + 1])
    has_more = len(window) > MAX_REVISIONS_PER_PAGE
    window = window[:MAX_REVISIONS_PER_PAGE]
    reported_ids = reported_target_ids(request.user, 'revision', window)
    return Response(
        {
            'revisions': [
                _revision_json(
                    revision,
                    article.current_revision_id,
                    for_moderator=moderator,
                    reported=revision.id in reported_ids,
                )
                for revision in window
            ],
            'total': total,
            'offset': offset,
            'has_more': has_more,
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
                for_moderator=is_moderator(request.user),
                reported=revision.id
                in reported_target_ids(request.user, 'revision', [revision]),
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

    With one exception, which is a refusal rather than a surprise: if the
    *current* revision is suppressed, restoring would republish the very text
    that was hidden — the article pane renders the current snapshot without
    consulting the flag. That combination is what bulk removal leaves behind
    when it deletes an article a banned account solely authored, so it is a
    reachable state, not a theoretical one. Restore the revision or revert
    past it first, deliberately.
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
    current = article.current_revision
    if current is not None and current.suppressed is not None:
        return Response(
            {'error': 'this article’s current revision is suppressed; '
                      'restore that revision first, or the removed text '
                      'goes back up with the article'},
            status=status.HTTP_400_BAD_REQUEST,
        )
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


# What a removed post's byline reads as publicly. Square brackets can't be
# registered as a username (`core/validators.py`), so this can never collide
# with a real account.
DELETED_AUTHOR = '[deleted]'


def reported_target_ids(user, field, objects):
    """Which of `objects` this user has already reported, as a set of ids.

    One query for a whole page rather than one per row — a thread can carry
    eighty posts, and a per-row EXISTS would cost eighty queries to render a
    marker.

    Status is deliberately *not* filtered. The client shows this as a
    permanent "reported" marker, so it has to survive a moderator closing the
    report: the point is that the reporter has had their say on this target,
    not that the report is still open. `create_report` matches on the same
    unfiltered lookup, so the button and the server agree.
    """
    if not user.is_authenticated or not objects:
        return frozenset()
    return frozenset(
        Report.objects.filter(
            reporter=user, **{f'{field}_id__in': [o.id for o in objects]}
        ).values_list(f'{field}_id', flat=True)
    )


def _post_json(post, for_moderator=False, reported=False):
    # A soft-deleted post stays as a tombstone (thread coherence) but its body
    # is withheld from everyone, and publicly its byline goes too: naming the
    # author of removed abuse pins the abuse to them on a page anyone can
    # read, which is the harm the removal was for. Unlike a revision there is
    # no attribution to preserve here — the text isn't being republished — so
    # the byline can go where a revision's can't. Moderators still see who
    # wrote it, and the row is untouched in the database.
    deleted = post.deleted is not None
    hidden = deleted and not for_moderator
    return {
        'id': post.id,
        'author': DELETED_AUTHOR if hidden else post.author.username,
        'body_md': '' if deleted else post.body_md,
        'created': post.created.isoformat(),
        'edited': post.edited.isoformat() if post.edited else None,
        'deleted': deleted,
        # Viewer-relative: "you have reported this", not "this was reported".
        # Never a count and never true for anyone but the reporter — telling
        # the room a post is under review invites a pile-on and tips off its
        # author, and independent reports are signal the queue wants.
        'reported': reported,
    }


def _thread_json(thread, for_moderator=False, reported_ids=frozenset()):
    # `post_window` is the capped prefetch (MAX_TALK_POSTS + 1) set up in
    # `talk`, not the whole thread — the bound is enforced in SQL, so an
    # enormous thread never lands in memory. A thread past the cap is
    # truncated rather than hidden: the opening posts carry the discussion,
    # and `posts_truncated` tells the client to say so.
    # A thread built by POST (below) has no prefetch, so fall back to the same
    # bounded query rather than an unbounded `posts.all()`.
    posts = getattr(thread, 'post_window', None)
    if posts is None:
        posts = list(
            thread.posts.select_related('author')[: MAX_TALK_POSTS + 1]
        )
    return {
        'id': thread.id,
        'title': thread.title,
        'created': thread.created.isoformat(),
        'posts': [
            _post_json(
                post,
                for_moderator=for_moderator,
                reported=post.id in reported_ids,
            )
            for post in posts[:MAX_TALK_POSTS]
        ],
        'posts_truncated': len(posts) > MAX_TALK_POSTS,
    }


@api_view(['GET', 'POST'])
# The default anon/user rates are listed explicitly because @throttle_classes
# replaces them rather than adding to them — dropping them here would leave
# the public thread list with no limit at all. TalkWriteThrottle puts thread
# creation in the same 40/min bucket as replies and post edits while letting
# reads past.
@throttle_classes([AnonRateThrottle, UserRateThrottle, TalkWriteThrottle])
def talk(request, slug):
    """Threaded discussion for a Place. GET is public; POST (new thread
    with its opening post) needs an account."""
    place = place_by_slug(slug)
    if request.method == 'GET':
        # Deleted threads drop out of the list; deleted posts stay as
        # tombstones so replies still make sense.
        threads = place.talk_threads.filter(
            deleted__isnull=True
        ).prefetch_related(
            Prefetch(
                'posts',
                # One past the cap so _thread_json can tell "exactly full"
                # from "truncated" without a per-thread COUNT. Django turns
                # the slice into a ROW_NUMBER() window partitioned by thread,
                # so this is one query bounded per thread, not per place.
                # `to_attr` is required: writing a sliced queryset back into
                # the normal prefetch cache re-filters it, which slicing
                # forbids.
                queryset=TalkPost.objects.select_related('author')[
                    : MAX_TALK_POSTS + 1
                ],
                to_attr='post_window',
            )
        )
        window = list(threads[: MAX_TALK_THREADS + 1])
        has_more = len(window) > MAX_TALK_THREADS
        shown = window[:MAX_TALK_THREADS]
        # Every post the response will actually serialize, so the reported
        # lookup is one query for the page however many threads it holds.
        visible = [
            post
            for thread in shown
            for post in (getattr(thread, 'post_window', None) or [])[
                :MAX_TALK_POSTS
            ]
        ]
        reported_ids = reported_target_ids(
            request.user, 'talk_post', visible
        )
        return Response(
            {
                'threads': [
                    _thread_json(
                        thread,
                        for_moderator=is_moderator(request.user),
                        reported_ids=reported_ids,
                    )
                    for thread in shown
                ],
                'has_more': has_more,
            }
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
    # A moderator can be deleting a post they reported themselves; the
    # tombstone hides the report affordance anyway, but this response replaces
    # the client's copy of the post, so it should not quietly clear the flag.
    return Response(
        {
            'post': _post_json(
                post,
                for_moderator=is_moderator(request.user),
                reported=post.id
                in reported_target_ids(request.user, 'talk_post', [post]),
            )
        }
    )


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
        # Attributed to whoever opened it, like every other removal: an
        # unattributed action never reaches the dashboard, so a deleted
        # thread used to vanish with nothing anywhere to undo it from.
        log_action(
            request.user, ModAction.Action.DELETE_THREAD,
            target_user=thread.starter(),
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
    # Any prior report by this reporter on this target ends it, open or not.
    # A closed report used to be re-filable — the partial unique constraints
    # only cover `status='open'` — which let a reporter who disliked a
    # dismissal file the same complaint again, and again, each one a fresh
    # queue row and a fresh moderator email. It also has to work this way for
    # the client's `reported` marker to be honest: the marker is permanent, so
    # the endpoint behind it must be too.
    previous = Report.objects.filter(
        reporter=request.user, **{target: obj}
    ).first()
    if previous is not None:
        return Response(
            {'report': {'id': previous.id, 'status': previous.status}},
            status=status.HTTP_201_CREATED,
        )
    # Still get_or_create for the first filing: the open-report constraints
    # are what make two simultaneous submissions one row.
    report, created = Report.objects.get_or_create(
        reporter=request.user,
        status=Report.Status.OPEN,
        **{target: obj},
        defaults={'reason': data['reason'], 'category': data['category']},
    )
    # Only on creation: re-filing is idempotent above, and should be silent
    # here too. notify_new_report never raises — the report is already saved,
    # and a mail failure must not turn that into an error for the reporter.
    if created:
        place = (
            obj.article.place if target == 'revision' else obj.thread.place
        )
        notify_new_report(
            report, url=request.build_absolute_uri(f'/place/{place.slug}')
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
        for etymology in entry.get('etymologies', []):
            if etymology.get('etymology_md', '').strip():
                return etymology['etymology_md'][:280]
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
    # After the save, and never in its way: the decision is recorded and
    # audited by this point, and notify_report_outcome swallows its own
    # failures, so a dead mailer cannot cost a moderator their action.
    place = post.thread.place if post is not None else revision.article.place
    notify_report_outcome(
        report,
        action,
        actor=request.user,
        url=request.build_absolute_uri(f'/place/{place.slug}'),
    )
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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mod_talk_thread_restore(request, thread_id):
    """Put a soft-deleted thread back — the inverse of `talk_thread_delete`
    (moderators only).

    Posts removed individually stay removed: deleting the thread hid the
    conversation wholesale, and undoing that shouldn't quietly undo the
    narrower judgements made inside it.
    """
    if not is_moderator(request.user):
        return Response(status=status.HTTP_403_FORBIDDEN)
    thread = get_object_or_404(TalkThread, id=thread_id)
    if thread.deleted is not None:
        thread.deleted = None
        thread.deleted_by = None
        thread.save(update_fields=['deleted', 'deleted_by'])
        log_action(
            request.user, ModAction.Action.RESTORE_THREAD,
            target_user=thread.starter(),
            reason=f'thread "{thread.title}"',
        )
    return Response({'ok': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def close_account(request):
    """Close the signed-in account (see core/accounts.py for what that means).

    Confirmed by password rather than a checkbox: it is the most destructive
    control in the product and, for a contributor, it is not reversible. The
    sentinel username is random, and while the old name is recorded — retired
    in ReservedUsername so nobody can take it over — that row is deliberately
    not linked back to the account, so it is no route to undoing any of this.

    Refused while a ban is active. Otherwise closing an account would be a
    way to shed a sanction and scramble the moderation trail mid-review;
    BannedEmail would still block re-registration, but the account's own
    history should stay legible until the ban is resolved.
    """
    if active_ban(request.user) is not None:
        return Response(
            {'error': 'A suspended account cannot be closed. Contact support.'},
            status=status.HTTP_403_FORBIDDEN,
        )
    password = request.data.get('password') or ''
    if not request.user.check_password(password):
        return Response(
            {'error': 'That password is not correct.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    user = request.user
    # End the session before the row changes under it.
    logout(request)
    outcome, username = accounts.close(user)
    return Response({'outcome': outcome, 'username': username})
