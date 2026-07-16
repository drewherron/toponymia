from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('health/', views.health, name='health'),
    path('me/', views.me, name='me'),
    path('resolve/', views.resolve, name='resolve'),
    path('highlights/', views.highlights, name='highlights'),
    path('search/', views.search, name='search'),
    path('random/', views.random_article, name='random'),
    path('places/<slug:slug>/', views.place_detail, name='place-detail'),
    path(
        'places/<slug:slug>/article/',
        views.article_edit,
        name='article-edit',
    ),
    path(
        'places/<slug:slug>/revisions/',
        views.revision_list,
        name='revision-list',
    ),
    path(
        'places/<slug:slug>/revisions/<int:revision_id>/',
        views.revision_detail,
        name='revision-detail',
    ),
    path(
        'places/<slug:slug>/revert/',
        views.article_revert,
        name='article-revert',
    ),
    path('places/<slug:slug>/talk/', views.talk, name='talk'),
    path(
        'talk/<int:thread_id>/posts/',
        views.talk_reply,
        name='talk-reply',
    ),
    path(
        'talk/posts/<int:post_id>/',
        views.talk_post_edit,
        name='talk-post-edit',
    ),
]
