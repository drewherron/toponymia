"""WhiteNoise, minus the SPA shell.

WHITENOISE_ROOT is the Vite build, so WhiteNoise would serve every file in
it verbatim — including index.html, the one file that must *not* be served
verbatim. core/spa.py owns that file: it injects the per-place SEO meta and
stamps the inline script with the request's CSP nonce. Straight off disk it
arrives with neither, so /index.html is a second, worse copy of the home
page whose dark-mode anti-flash snippet the browser silently drops.

WhiteNoise resolves a URL two ways depending on `autorefresh` (which
follows DEBUG), so hiding the shell takes an override on each: the prebuilt
dictionary when it's off, the per-request disk lookup when it's on. With it
hidden, /index.html falls through to the URLconf, which redirects it to /.
"""

from whitenoise.middleware import WhiteNoiseMiddleware

# Exact match, not a suffix: STATIC_ROOT is mounted under /static/, and an
# index.html anywhere under there is a genuine static file.
SHELL_URL = '/index.html'


class ShellExcludedWhiteNoise(WhiteNoiseMiddleware):
    def add_file_to_dictionary(self, url, path, stat_cache=None):
        if url != SHELL_URL:
            super().add_file_to_dictionary(url, path, stat_cache)

    def find_file(self, url):
        if url != SHELL_URL:
            return super().find_file(url)
