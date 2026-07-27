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
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
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
        etymology = entry.get('etymology_md', '').strip()
        if etymology:
            return _truncate(f'{entry["name"]}: {_plain_text(etymology)}')
    body = content.get('body_md', '').strip()
    if body:
        return _truncate(_plain_text(body))
    return (
        f'What does the name “{place.display_name}” mean? Read or start '
        f'the etymology of {place.display_name} on Toponymia.'
    )


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
    return HttpResponse(html, status=status)


def index(request):
    return _render(
        request,
        title=DEFAULT_TITLE,
        description=DEFAULT_DESCRIPTION,
        path='/',
    )


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


def fallback(request):
    """Anything that isn't a known route serves the shell as a 404: the
    client only ever creates / and /place/<slug>, so other URLs should
    read as missing to crawlers while still rendering the app."""
    return _render(
        request,
        title=DEFAULT_TITLE,
        description=DEFAULT_DESCRIPTION,
        path='/',
        status=404,
    )


def sitemap(request):
    places = (
        published_places()
        .select_related('article__current_revision')
        .order_by('slug')
    )
    entries = [(request.build_absolute_uri('/'), None)]
    entries += [
        (
            request.build_absolute_uri(f'/place/{p.slug}'),
            p.article.current_revision.created,
        )
        for p in places
    ]
    urls = []
    for loc, lastmod in entries:
        lastmod_xml = (
            f'<lastmod>{lastmod.date().isoformat()}</lastmod>' if lastmod else ''
        )
        urls.append(f'<url><loc>{escape(loc)}</loc>{lastmod_xml}</url>')
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + ''.join(urls)
        + '</urlset>'
    )
    return HttpResponse(body, content_type='application/xml')


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
