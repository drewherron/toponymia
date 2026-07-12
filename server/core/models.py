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
