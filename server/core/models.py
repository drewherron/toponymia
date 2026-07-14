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
