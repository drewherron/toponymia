from django.conf import settings
from django.contrib.gis.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class Place(models.Model):
    """The anchor entity an article attaches to."""

    class AnchorLevel(models.TextChoices):
        WIKIDATA = 'wikidata'
        OSM = 'osm'
        NAME = 'name'

    slug = models.SlugField(max_length=120, unique=True)
    wikidata_qid = models.CharField(
        max_length=32, null=True, blank=True, unique=True
    )
    osm_type = models.CharField(max_length=8, null=True, blank=True)
    osm_id = models.BigIntegerField(null=True, blank=True)
    anchor_level = models.CharField(max_length=8, choices=AnchorLevel.choices)
    display_name = models.CharField(max_length=255)
    feature_class = models.CharField(max_length=40)
    # Representative geometry cached from Overpass, for the highlight
    # overlay and proximity cache lookups. Nullable: *area* relations cache
    # centroid+bbox only (full member geometry can be huge).
    #
    # NOT survey-grade — deliberately thinned on write by
    # resolve.simplified(), to a tolerance scaled to the feature's own
    # extent so the error stays sub-pixel at the zoom that frames it
    # (Mississippi: 292 kB -> 47 kB). Fine for drawing and for the
    # intersects/dwithin filters; do not use it to measure anything.
    geometry = models.GeometryField(geography=True, null=True, blank=True)
    centroid = models.PointField(geography=True)
    # A point guaranteed to lie ON the feature — the click that created
    # the place. The bbox-derived centroid of a long/curvy feature can sit
    # far off it, so article dots hang here instead. Null → use centroid.
    label_point = models.PointField(geography=True, null=True, blank=True)
    bbox = models.PolygonField(geography=True, null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['osm_type', 'osm_id'],
                name='unique_osm_element',
                condition=models.Q(osm_id__isnull=False),
            ),
        ]

    def __str__(self):
        return f'{self.display_name} ({self.anchor_level})'


class PlaceSlug(models.Model):
    """Every URL slug a Place answers to (see docs/slug-renames.md).

    A place has exactly one canonical slug and any number of aliases left behind
    by renames; an alias 301s to the canonical. This table's unique `slug` index
    is the *single* enforcement point for global slug uniqueness across canonical
    and alias alike — `Place.slug` is a denormalized cache of the canonical row,
    kept in sync only in the two write paths (creation via slugs.mint_place,
    rename via the rename_place command). Reads never write it.
    """

    place = models.ForeignKey(
        Place, on_delete=models.CASCADE, related_name='slugs'
    )
    slug = models.SlugField(max_length=120, unique=True)
    is_canonical = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['place'],
                condition=models.Q(is_canonical=True),
                name='one_canonical_slug_per_place',
            ),
        ]

    def __str__(self):
        tag = 'canonical' if self.is_canonical else 'alias'
        return f'{self.slug} ({tag})'


@receiver(post_save, sender=Place)
def ensure_canonical_slug(sender, instance, created, **kwargs):
    """Guarantee every new Place has a canonical PlaceSlug mirroring its slug.

    A signal rather than an explicit mint helper so the invariant holds for
    *every* creation path — resolve, tests, the shell — with no call-site
    coordination. get_or_create keeps it idempotent; renames update the slug on
    an existing Place (created=False) and manage their own PlaceSlug rows, so
    this only ever fires at birth.
    """
    if not created:
        return
    PlaceSlug.objects.get_or_create(
        slug=instance.slug,
        defaults={'place': instance, 'is_canonical': True},
    )


class Article(models.Model):
    """The wiki article for a Place. Content lives in
    Revision snapshots; this row is the stable identity + pointer."""

    class Protection(models.TextChoices):
        NONE = 'none'
        REGISTERED = 'registered'
        ADMIN = 'admin'

    place = models.OneToOneField(
        Place, on_delete=models.CASCADE, related_name='article'
    )
    current_revision = models.ForeignKey(
        'Revision',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='+',
    )
    protection_level = models.CharField(
        max_length=16, choices=Protection.choices, default=Protection.NONE
    )
    # Soft delete for the whole article: the place reads as a
    # plain stub to the public while every revision stays untouched. A new
    # write clears the flag (the write IS the restore), so this can never
    # conflict with later content. Distinct from Revision.suppressed, which
    # hides one abusive revision and survives any rewrite; and from revert,
    # which is the content-correctness tool.
    deleted = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Article: {self.place.display_name}'


class Revision(models.Model):
    """Full JSON snapshot per edit (wiki core). content:
    { body_md, names: [ { name, language, from_languages[], is_endonym,
      etymology_md, references[] } ], derivations: [ { term, note, url } ],
      see_also: [] }"""

    article = models.ForeignKey(
        Article, on_delete=models.CASCADE, related_name='revisions'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='revisions'
    )
    created = models.DateTimeField(auto_now_add=True)
    comment = models.CharField(max_length=255, blank=True)
    content = models.JSONField()
    # Soft-hide for abuse: a suppressed revision drops out
    # of *public* history but stays visible to moderators and preserved in
    # the record. Distinct from revert, which is the content-correctness
    # tool; the current revision can't be suppressed (revert it first).
    suppressed = models.DateTimeField(null=True, blank=True)
    suppressed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['-created', '-id']

    def __str__(self):
        return f'r{self.id} of {self.article}'


class TalkThread(models.Model):
    """A discussion topic about a Place. Attached to the
    Place, not the Article, so a stub can be discussed before anyone
    writes it. Author/timestamps of the conversation live on the posts."""

    place = models.ForeignKey(
        Place, on_delete=models.CASCADE, related_name='talk_threads'
    )
    title = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)
    # Soft delete: a removed thread drops out of the list
    # rather than vanishing from the record.
    deleted = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['created', 'id']

    def __str__(self):
        return f'{self.title} ({self.place.display_name})'


class TalkPost(models.Model):
    """One message in a thread. Flat within the thread; chronological."""

    thread = models.ForeignKey(
        TalkThread, on_delete=models.CASCADE, related_name='posts'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='talk_posts',
    )
    body_md = models.TextField()
    created = models.DateTimeField(auto_now_add=True)
    edited = models.DateTimeField(null=True, blank=True)
    # Soft delete: the post stays as a tombstone so the
    # thread reads coherently; body is withheld once deleted.
    deleted = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['created', 'id']

    def __str__(self):
        return f'post {self.id} in {self.thread}'


class PlaceName(models.Model):
    """Materialized from the *current* revision's names[] — the relational
    query surface for map filtering and search. Rewritten on every edit;
    never edited directly. Language is a bare ISO 639 code for now (the
    Language table arrives with the language-filter work)."""

    place = models.ForeignKey(
        Place, on_delete=models.CASCADE, related_name='names'
    )
    name = models.CharField(max_length=255)
    language = models.CharField(max_length=16, blank=True)
    is_endonym = models.BooleanField(default=False)
    from_languages = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['place', 'name', 'language'],
                name='unique_place_name_language',
            ),
        ]
        indexes = [models.Index(fields=['name'])]

    def __str__(self):
        return f'{self.name} [{self.language or "?"}]'


class Report(models.Model):
    """A flag on a Revision or a TalkPost for moderator attention. Exactly one
    target is set; the reason is the reporter's note. Status drives the mod
    queue."""

    class Status(models.TextChoices):
        OPEN = 'open'
        RESOLVED = 'resolved'
        DISMISSED = 'dismissed'

    class Category(models.TextChoices):
        SPAM = 'spam'
        VANDALISM = 'vandalism'
        HARASSMENT = 'harassment'
        PERSONAL_INFO = 'personal_info'
        OTHER = 'other'

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='reports',
    )
    category = models.CharField(
        max_length=16, choices=Category.choices, default=Category.OTHER
    )
    revision = models.ForeignKey(
        Revision,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reports',
    )
    talk_post = models.ForeignKey(
        TalkPost,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reports',
    )
    reason = models.CharField(max_length=500, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN
    )
    created = models.DateTimeField(auto_now_add=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created', '-id']
        constraints = [
            # Exactly one target: XOR of the two nullable FKs.
            models.CheckConstraint(
                name='report_exactly_one_target',
                condition=(
                    models.Q(revision__isnull=False, talk_post__isnull=True)
                    | models.Q(revision__isnull=True, talk_post__isnull=False)
                ),
            ),
            # One open report per user per target — re-reporting is a no-op.
            models.UniqueConstraint(
                fields=['reporter', 'revision'],
                condition=models.Q(
                    revision__isnull=False, status='open'
                ),
                name='one_open_report_per_revision',
            ),
            models.UniqueConstraint(
                fields=['reporter', 'talk_post'],
                condition=models.Q(
                    talk_post__isnull=False, status='open'
                ),
                name='one_open_report_per_post',
            ),
        ]

    def __str__(self):
        target = self.revision_id and f'r{self.revision_id}' or (
            f'post {self.talk_post_id}'
        )
        return f'report on {target} ({self.status})'


class Ban(models.Model):
    """An account sanction. A banned user is blocked from
    every write endpoint until the ban is lifted or expires; reading is
    always allowed. Rows are never deleted — an unban sets `lifted` — so the
    account's sanction history is preserved. `expires` null = permanent.
    No IP fields in v1 (IP-ban deferred; see M12)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='bans',
    )
    reason = models.CharField(max_length=500, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    # Null = permanent. A time-limited ban lapses on its own.
    expires = models.DateTimeField(null=True, blank=True)
    # An explicit unban; keeps the row for the audit trail.
    lifted = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['-created', '-id']

    def is_active(self, now=None):
        from django.utils import timezone

        now = now or timezone.now()
        if self.lifted is not None:
            return False
        return self.expires is None or self.expires > now

    def __str__(self):
        return f'ban on {self.user_id} ({"active" if self.is_active() else "inactive"})'


class BannedEmail(models.Model):
    """A registration blocklist entry. When an account is banned its email
    address(es) are snapshotted here so the same address can't be used to open
    a fresh account — the custom account adapter refuses a signup whose email
    has an active row. Together with allauth's unique-email rule, this is what
    makes an account ban outlive the single login it was placed on.

    Keyed by the email *string*, deliberately not a FK to the user or the Ban:
    `Ban.user` is CASCADE, so a deleted account takes its bans with it, and the
    whole point of a durable email ban is that it must stand even then. So
    enforcement matches on `email`; `banned_user` is provenance only (SET_NULL)
    and never consulted to decide whether a block applies.

    Mirrors Ban's lifecycle: `expires` null = permanent, so a time-limited ban
    lapses the email block with it; an explicit un-block sets `lifted`; rows are
    never deleted, preserving the history. No IP fields (IP-ban deferred; see
    Ban)."""

    # Stored lowercased by the write helpers; matched case-insensitively.
    email = models.EmailField(db_index=True)
    reason = models.CharField(max_length=500, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    # Provenance only — see the class docstring. SET_NULL, never CASCADE: the
    # block outlives the account it came from.
    banned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    # Null = permanent; a time-limited entry lapses on its own, matching the ban.
    expires = models.DateTimeField(null=True, blank=True)
    # An explicit un-block; keeps the row for the audit trail.
    lifted = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['-created', '-id']

    def is_active(self, now=None):
        from django.utils import timezone

        now = now or timezone.now()
        if self.lifted is not None:
            return False
        return self.expires is None or self.expires > now

    def __str__(self):
        state = 'active' if self.is_active() else 'inactive'
        return f'email ban on {self.email} ({state})'


class ModAction(models.Model):
    """An append-only audit log of moderator actions — the
    backbone of the Moderation dashboard's decision view. One row per
    take-down, restore, ban, unban, or report resolution, linking the acting
    moderator, the affected user, and the specific content where relevant."""

    class Action(models.TextChoices):
        DELETE_POST = 'delete_post'
        RESTORE_POST = 'restore_post'
        SUPPRESS_REVISION = 'suppress_revision'
        RESTORE_REVISION = 'restore_revision'
        DELETE_THREAD = 'delete_thread'
        DELETE_ARTICLE = 'delete_article'
        RESTORE_ARTICLE = 'restore_article'
        REVERT_ARTICLE = 'revert_article'
        BAN_USER = 'ban_user'
        UNBAN_USER = 'unban_user'
        PROMOTE_MOD = 'promote_mod'
        DEMOTE_MOD = 'demote_mod'
        RESOLVE_REPORT = 'resolve_report'
        DISMISS_REPORT = 'dismiss_report'

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='+',
    )
    action = models.CharField(max_length=24, choices=Action.choices)
    # The user whose account or content was acted on — powers the
    # actor-centric "problem users" view. Null for actions with no subject.
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_mod_actions',
    )
    reason = models.CharField(max_length=500, blank=True)
    # Optional pointers to the exact content acted on, for dashboard links.
    article = models.ForeignKey(
        Article, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    revision = models.ForeignKey(
        Revision, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    talk_post = models.ForeignKey(
        TalkPost, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    report = models.ForeignKey(
        Report, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created', '-id']

    def __str__(self):
        return f'{self.action} by {self.actor_id} on {self.target_user_id}'


class TermsAcceptance(models.Model):
    """A user's affirmative agreement to a specific version of the Terms.

    The whole CC BY-SA licensing model in TERMS.md §2 rests on contributors
    having actually accepted the Terms, and "clickwrap" only holds up if you
    can show the agreement happened. One row is written per acceptance, at
    signup, by core.forms.TermsSignupForm — the form's `terms` checkbox is
    required, so a row existing means the box was ticked on a request the
    server validated.

    Append-only and versioned rather than a flag on the user: if the Terms
    change materially and everyone has to re-accept, that's another row, with
    the original agreement still on record.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='terms_acceptances',
    )
    # core.terms.TERMS_VERSION at the time of acceptance — matches the
    # "Last updated" date of the TERMS.md revision the user was shown.
    version = models.CharField(max_length=32)
    accepted = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-accepted', '-id']

    def __str__(self):
        return f'{self.user_id} accepted terms {self.version}'
