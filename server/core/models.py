from django.conf import settings
from django.contrib.gis.db import models


class Place(models.Model):
    """The anchor entity an article attaches to (DESIGN.md §3/§4)."""

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
    # overlay and proximity cache lookups. Nullable: relations only cache
    # centroid+bbox for now (full member geometry can be huge).
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


class Article(models.Model):
    """The wiki article for a Place (DESIGN.md §4). Content lives in
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

    class Meta:
        ordering = ['-created', '-id']

    def __str__(self):
        return f'r{self.id} of {self.article}'


class TalkThread(models.Model):
    """A discussion topic about a Place (DESIGN.md §4/§6). Attached to the
    Place, not the Article, so a stub can be discussed before anyone
    writes it. Author/timestamps of the conversation live on the posts."""

    place = models.ForeignKey(
        Place, on_delete=models.CASCADE, related_name='talk_threads'
    )
    title = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)
    # Soft delete (DESIGN.md §6): a removed thread drops out of the list
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
    # Soft delete (DESIGN.md §6): the post stays as a tombstone so the
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
    """A flag on a Revision or a TalkPost for moderator attention
    (DESIGN.md §4/§6). Exactly one target is set; the reason is the
    reporter's note. Status drives the mod queue."""

    class Status(models.TextChoices):
        OPEN = 'open'
        RESOLVED = 'resolved'
        DISMISSED = 'dismissed'

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='reports',
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
