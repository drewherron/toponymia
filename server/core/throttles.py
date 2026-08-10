"""Per-endpoint rate limits. UserRateThrottle keys by user
id when authenticated and by client IP otherwise, so a single scope caps
both logged-in and anonymous callers. Rates live in settings'
DEFAULT_THROTTLE_RATES under the matching scope name."""

from rest_framework.permissions import SAFE_METHODS
from rest_framework.throttling import UserRateThrottle


class ResolveThrottle(UserRateThrottle):
    # Guards the Overpass round-trip behind /api/resolve.
    scope = 'resolve'


class WriteThrottle(UserRateThrottle):
    # Article edits and reverts.
    scope = 'write'


class ReportThrottle(UserRateThrottle):
    scope = 'report'


class CspReportThrottle(UserRateThrottle):
    """The one endpoint the *browser* posts to, unauthenticated.

    Deliberately tight. A violation repeats on every page load, so one report
    teaches as much as a thousand, and this is the only write path on the site
    that anyone can reach without an account.
    """

    scope = 'csp-report'


class TalkThrottle(UserRateThrottle):
    # New threads, replies, and post edits.
    scope = 'talk'


class TalkWriteThrottle(TalkThrottle):
    """TalkThrottle for a view that also serves public reads.

    The thread list and thread creation share one URL, so throttling it
    wholesale would bill every anonymous read of a discussion to the same
    40/min write bucket. Safe methods pass straight through and are left to
    the default anon/user rates; only the POST counts.
    """

    def allow_request(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return super().allow_request(request, view)
