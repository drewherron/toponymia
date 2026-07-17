"""Per-endpoint rate limits (DESIGN.md §6). UserRateThrottle keys by user
id when authenticated and by client IP otherwise, so a single scope caps
both logged-in and anonymous callers. Rates live in settings'
DEFAULT_THROTTLE_RATES under the matching scope name."""

from rest_framework.throttling import UserRateThrottle


class ResolveThrottle(UserRateThrottle):
    # Guards the Overpass round-trip behind /api/resolve.
    scope = 'resolve'


class WriteThrottle(UserRateThrottle):
    # Article edits and reverts.
    scope = 'write'


class ReportThrottle(UserRateThrottle):
    scope = 'report'


class TalkThrottle(UserRateThrottle):
    # New threads, replies, and post edits.
    scope = 'talk'
