"""Serving the built SPA (web/dist) with server-rendered meta tags — the
SEO half deferred from M6. As a pure SPA every URL ships identical empty
HTML, so a shared /place/<slug> link shows nothing in previews or search
results; these views inject the per-place <title>/description/og:* into
index.html at request time. Asset files are WhiteNoise's job
(WHITENOISE_ROOT = WEB_DIST in settings) — only index.html renders here.
"""

import re
from pathlib import Path

from django.conf import settings
from django.http import (
    Http404,
    HttpResponse,
    HttpResponsePermanentRedirect,
    StreamingHttpResponse,
)
from django.middleware.csp import get_nonce
from django.utils.html import escape

from .models import Place
from .slugs import place_by_slug
from .views import published_places

DEFAULT_TITLE = 'Toponymia'
DEFAULT_DESCRIPTION = (
    'A map-based wiki about the origins and meanings of place names. '
    'Find a place on the map and read where its name comes from.'
)

# index.html is read once per build: the cache is keyed on mtime so a
# fresh `npm run build` is picked up without restarting the server.
_index_cache = {'mtime': None, 'html': ''}


def _index_html():
    path = Path(settings.WEB_DIST) / 'index.html'
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _index_cache['mtime'] != mtime:
        _index_cache['mtime'] = mtime
        _index_cache['html'] = path.read_text('utf-8')
    return _index_cache['html']


def _plain_text(md):
    """Markdown → single-line plain text for meta descriptions."""
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', md)  # images → alt
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)  # links → text
    text = re.sub(r'[`*_#>|]+', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _truncate(text, limit=200):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(' ', 1)[0] + '…'


def _place_description(place):
    """First name etymology of the current revision (the article's whole
    point), legacy body text as fallback, invitation copy for stubs.

    A deleted article falls through to the stub copy — the description is
    served to crawlers and link previews, so it must not outlive the
    content it summarizes.
    """
    article = getattr(place, 'article', None)
    if article is not None and article.deleted is not None:
        article = None
    revision = article.current_revision if article else None
    content = revision.content if revision else {}
    for entry in content.get('names', []):
        # The primary hypothesis — first entry with prose. A description
        # that quoted a disputed alternative would misrepresent the article
        # to exactly the audience (crawlers, link previews) that never
        # reads the qualification next to it.
        for etymology in entry.get('etymologies', []):
            text = etymology.get('etymology_md', '').strip()
            if text:
                return _truncate(f'{entry["name"]}: {_plain_text(text)}')
    body = content.get('body_md', '').strip()
    if body:
        return _truncate(_plain_text(body))
    return (
        f'What does the name “{place.display_name}” mean? Read or start '
        f'the etymology of {place.display_name} on Toponymia.'
    )


# Bare <script> — an inline one. Vite's own tags always carry src=, so this
# matches only the hand-written snippets in web/index.html.
_INLINE_SCRIPT_RE = re.compile(r'<script(?=[\s>])(?![^>]*\ssrc=)')


def _nonced(request, html):
    """Stamp inline <script> tags with the request's CSP nonce.

    settings.SECURE_CSP sends `script-src 'self' 'nonce-…'`, so an unstamped
    inline script is silently dropped by the browser. Today that's the
    dark-mode anti-flash snippet in index.html; matching on the tag rather
    than that one snippet means a second one added later is covered too.

    Interpolating the nonce is what makes the middleware emit it at all: it's
    a LazyNonce, generated on first *access*. Note `bool(nonce)` is False
    until then — testing it as a truth value would skip the substitution here
    every time, and silently, so the check below is against None (which means
    the middleware isn't installed). Substituting via a function keeps the
    laziness honest: the nonce is generated only if there's a script to stamp.
    """
    nonce = get_nonce(request)
    if nonce is None:
        return html
    return _INLINE_SCRIPT_RE.sub(lambda _: f'<script nonce="{nonce}"', html)


def _render(request, *, title, description, path, og_type='website', status=200):
    html = _index_html()
    if html is None:
        return HttpResponse(
            'Frontend build not found — run `npm run build` in web/ '
            '(or set DJANGO_WEB_DIST).',
            status=503,
            content_type='text/plain',
        )
    canonical = request.build_absolute_uri(path)
    head = '\n'.join(
        [
            f'<meta name="description" content="{escape(description)}" />',
            f'<link rel="canonical" href="{escape(canonical)}" />',
            '<meta property="og:site_name" content="Toponymia" />',
            f'<meta property="og:type" content="{og_type}" />',
            f'<meta property="og:title" content="{escape(title)}" />',
            f'<meta property="og:description" content="{escape(description)}" />',
            f'<meta property="og:url" content="{escape(canonical)}" />',
        ]
    )
    html = re.sub(
        r'<title>.*?</title>', f'<title>{escape(title)}</title>', html, count=1
    )
    if '<!--seo-->' in html:
        html = html.replace('<!--seo-->', head)
    else:
        html = html.replace('</head>', head + '\n</head>', 1)
    response = HttpResponse(_nonced(request, html), status=status)
    # The shell must never be reused from cache. It carries a per-request CSP
    # nonce, so a cached copy pairs yesterday's nonce with today's header and
    # the inline script is dropped; it also names the content-hashed bundle,
    # so a stale copy pins the browser to the previous build until someone
    # thinks to hard-refresh. `no-cache` permits storing but forces
    # revalidation on every use; with no ETag on this response that means a
    # full refetch, which is the right trade for a document this small and
    # this per-request. Assets under /assets/ are content-hashed and stay
    # cacheable — WhiteNoise serves those, not this view.
    response['Cache-Control'] = 'no-cache'
    return response


def index(request):
    return _render(
        request,
        title=DEFAULT_TITLE,
        description=DEFAULT_DESCRIPTION,
        path='/',
    )


def index_html(request):
    """/index.html is the shell's name on disk, not a URL of this site.

    core/statics.py stops WhiteNoise serving it so this view gets the
    request instead; redirecting rather than rendering keeps one canonical
    URL for the home page. robots.txt disallows it too, for the crawlers
    that got the address from somewhere else.
    """
    return HttpResponsePermanentRedirect('/')


def place(request, slug):
    try:
        found = place_by_slug(
            slug, Place.objects.select_related('article__current_revision')
        )
    except Http404:
        return _render(
            request,
            title=DEFAULT_TITLE,
            description=DEFAULT_DESCRIPTION,
            path='/',
            status=404,
        )
    # An alias 301s to the canonical URL, so crawlers and shared links
    # converge on one address per place (see docs/slug-renames.md).
    if slug != found.slug:
        return HttpResponsePermanentRedirect(f'/place/{found.slug}')
    article = getattr(found, 'article', None)
    has_article = bool(article and article.current_revision_id)
    return _render(
        request,
        # En dash — must match the client's document.title exactly.
        title=f'{found.display_name} – Toponymia',
        description=_place_description(found),
        path=f'/place/{found.slug}',
        og_type='article' if has_article else 'website',
    )


TERMS_DESCRIPTION = (
    'The Terms of Use for Toponymia: how contributions are licensed under '
    'CC BY-SA 4.0, content standards, moderation, and copyright complaints.'
)


PRIVACY_DESCRIPTION = (
    'What Toponymia collects and why: account details, server logs and their '
    'retention, cookies, and the services your browser contacts.'
)


def terms(request):
    """/terms — a real 200 URL for the Terms of Use, not just the in-app
    dialog. Needed on both counts: the DMCA safe harbor requires the
    designated agent's contact to be publicly accessible (§512(c)(2)), and a
    linkable, crawlable address is what lets a copyright holder or a court
    find the document at all."""
    return _render(
        request,
        title='Terms of Use – Toponymia',
        description=TERMS_DESCRIPTION,
        path='/terms',
    )


def privacy(request):
    """/privacy — the Privacy Policy, same arrangement as /terms."""
    return _render(
        request,
        title='Privacy Policy – Toponymia',
        description=PRIVACY_DESCRIPTION,
        path='/privacy',
    )


def fallback(request):
    """Anything that isn't a known route serves the shell as a 404: the
    client only ever creates /, /terms, /privacy and /place/<slug>, so other
    URLs should read as missing to crawlers while still rendering the app."""
    return _render(
        request,
        title=DEFAULT_TITLE,
        description=DEFAULT_DESCRIPTION,
        path='/',
        status=404,
    )


# The sitemap spec caps a single file at 50,000 URLs. We're a long way from
# that, but the cap is what keeps this one file valid rather than silently
# over-long; crossing it means moving to the sitemap-index format, not raising
# the number.
MAX_SITEMAP_URLS = 50_000


def _sitemap_chunks(request):
    """Yield the sitemap a URL at a time. Streaming with `.iterator()` keeps
    both the whole XML string and the whole result set out of memory, so this
    stays flat as the wiki grows instead of scaling with published places."""
    yield (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    )
    yield f'<url><loc>{escape(request.build_absolute_uri("/"))}</loc></url>'
    yield f'<url><loc>{escape(request.build_absolute_uri("/terms"))}</loc></url>'
    yield (
        f'<url><loc>{escape(request.build_absolute_uri("/privacy"))}</loc>'
        '</url>'
    )
    places = (
        published_places()
        .select_related('article__current_revision')
        # Less the three fixed URLs above (root, /terms, /privacy).
        .order_by('slug')[: MAX_SITEMAP_URLS - 3]
    )
    for place in places.iterator():
        loc = escape(request.build_absolute_uri(f'/place/{place.slug}'))
        lastmod = place.article.current_revision.created
        yield (
            f'<url><loc>{loc}</loc>'
            f'<lastmod>{lastmod.date().isoformat()}</lastmod></url>'
        )
    yield '</urlset>'


def sitemap(request):
    return StreamingHttpResponse(
        _sitemap_chunks(request), content_type='application/xml'
    )


def robots(request):
    sitemap_url = request.build_absolute_uri('/sitemap.xml')
    body = (
        'User-agent: *\n'
        'Disallow: /api/\n'
        'Disallow: /admin/\n'
        'Disallow: /_allauth/\n'
        'Disallow: /index.html\n'
        '\n'
        f'Sitemap: {sitemap_url}\n'
    )
    return HttpResponse(body, content_type='text/plain')
