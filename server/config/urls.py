from django.contrib import admin
from django.urls import include, path, re_path

from core import spa

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('core.urls')),
    path('_allauth/', include('allauth.headless.urls')),
    # Built SPA + SEO surface (core/spa.py). WhiteNoise serves the asset
    # files from WEB_DIST before URL resolution, so the catch-all only
    # sees genuinely unknown paths (and answers with a 404 shell).
    path('robots.txt', spa.robots, name='robots'),
    path('sitemap.xml', spa.sitemap, name='sitemap'),
    path('', spa.index, name='spa-index'),
    path('index.html', spa.index_html, name='spa-index-html'),
    path('terms', spa.terms, name='spa-terms'),
    path('privacy', spa.privacy, name='spa-privacy'),
    path('place/<slug:slug>', spa.place, name='spa-place'),
    re_path(r'^.*$', spa.fallback, name='spa-fallback'),
]
