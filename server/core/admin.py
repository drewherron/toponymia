from django.contrib import admin

from .models import Article, Place, PlaceName, Revision


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'feature_class', 'anchor_level', 'slug')
    search_fields = ('display_name', 'slug', 'wikidata_qid')


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ('place', 'protection_level', 'created')


@admin.register(Revision)
class RevisionAdmin(admin.ModelAdmin):
    list_display = ('id', 'article', 'author', 'created', 'comment')
    list_select_related = ('article__place', 'author')


@admin.register(PlaceName)
class PlaceNameAdmin(admin.ModelAdmin):
    list_display = ('name', 'language', 'is_endonym', 'place')
    search_fields = ('name',)
