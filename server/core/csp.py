"""Where Content-Security-Policy violation reports land.

A CSP violation leaves **no server-side trace**: the browser blocks the
resource and moves on, the page renders subtly wrong, and the user leaves.
Nothing in the request log distinguishes it from a normal page view. So
without this endpoint the only way to learn the policy is broken is for
someone to tell you — which is the same failure the logging config in
`settings.py` exists to fix, one layer out.

**Reports go to the log, not to a table.** This is a public, unauthenticated
write path, so a model would let anyone grow the database at will; the log is
already rotated, already shipped, and already the place the 500s go. Nothing
here is worth keeping longer than the logs are kept.

**Two formats, one of them not in use yet.** The `report-uri` directive posts
`application/csp-report`: one report wrapped in a `csp-report` key. The
Reporting API's `report-to` posts `application/reports+json`: a batched
*array* with camelCase keys. Only the first is enabled — `report-to` measured
worse than useless, see the note in `settings.SECURE_CSP` — but the second is
parsed here anyway, because `report-uri` is deprecated and the day it goes
away is a day reports stop arriving with no error anywhere. Handling the
successor costs twenty lines now and makes that migration one settings line.
"""

import logging

from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .throttles import CspReportThrottle

logger = logging.getLogger(__name__)

# `SECURE_CSP` has to spell the same path out as a literal — the policy is
# built at import time, before the URLConf exists — so a test asserts the two
# agree rather than leaving them to drift.
REPORT_ROUTE = 'core:csp-report'

# A batch is capped rather than trusted: `report-to` sends an array whose
# length the client chooses.
MAX_REPORTS = 10

# Long enough to identify a script or an origin, short enough that a hostile
# report can't write pages into the log. Data URLs are the reason this is
# needed at all — they can be megabytes.
MAX_FIELD = 200

# Browser extensions inject scripts into every page and their blocked loads
# are reported as violations of *our* policy, which is the well-known reason
# CSP reporting gets abandoned as noise. These schemes are never anything we
# shipped, so dropping them is what keeps the log readable enough to be worth
# reading.
EXTENSION_SCHEMES = (
    'chrome-extension:',
    'moz-extension:',
    'safari-extension:',
    'safari-web-extension:',
    'webkit-masked-url:',
)


class CspReportParser(JSONParser):
    """`report-uri` bodies. JSON with a content type DRF doesn't know."""

    media_type = 'application/csp-report'


class ReportingApiParser(JSONParser):
    """`report-to` bodies, same deal."""

    media_type = 'application/reports+json'


def _clip(value):
    if value in (None, ''):
        return None
    text = str(value)
    return text[:MAX_FIELD] + '…' if len(text) > MAX_FIELD else text


def _from_report_uri(body):
    """`{"csp-report": {"violated-directive": ..., ...}}`"""
    report = body.get('csp-report')
    if not isinstance(report, dict):
        return None
    return {
        'directive': _clip(
            report.get('effective-directive')
            or report.get('violated-directive')
        ),
        'blocked': _clip(report.get('blocked-uri')),
        'document': _clip(report.get('document-uri')),
        'source': _clip(report.get('source-file')),
        'line': report.get('line-number'),
    }


def _from_reporting_api(entry):
    """`[{"type": "csp-violation", "body": {"effectiveDirective": ...}}]`"""
    if not isinstance(entry, dict) or entry.get('type') != 'csp-violation':
        return None
    report = entry.get('body')
    if not isinstance(report, dict):
        return None
    return {
        'directive': _clip(report.get('effectiveDirective')),
        'blocked': _clip(report.get('blockedURL')),
        'document': _clip(report.get('documentURL') or entry.get('url')),
        'source': _clip(report.get('sourceFile')),
        'line': report.get('lineNumber'),
    }


def _parsed(data):
    """Both wire formats, normalised to a list of flat dicts."""
    if isinstance(data, dict):
        one = _from_report_uri(data)
        return [one] if one else []
    if isinstance(data, list):
        found = []
        for entry in data[:MAX_REPORTS]:
            report = _from_reporting_api(entry)
            if report:
                found.append(report)
        return found
    return []


def _is_noise(report):
    blocked = (report.get('blocked') or '').lower()
    source = (report.get('source') or '').lower()
    return blocked.startswith(EXTENSION_SCHEMES) or source.startswith(
        EXTENSION_SCHEMES
    )


@api_view(['POST'])
# No authentication: the browser sends these with no credentials and no CSRF
# token, and DRF's SessionAuthentication would reject a session cookie that
# happened to ride along on the same-origin post.
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([CspReportParser, ReportingApiParser, JSONParser])
@throttle_classes([CspReportThrottle])
def csp_report(request):
    """Log CSP violations. Always 204 — the browser discards the response,
    and a report that can't be parsed is not the reporter's problem."""
    for report in _parsed(request.data):
        if _is_noise(report):
            continue
        logger.warning(
            'CSP violation: %s blocked %s on %s (source %s:%s)',
            report['directive'],
            report['blocked'],
            report['document'],
            report['source'],
            report['line'],
        )
    return Response(status=204)
