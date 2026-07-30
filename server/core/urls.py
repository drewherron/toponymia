from django.urls import path

from . import dashboard, views

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
        'places/<slug:slug>/geometry/',
        views.place_geometry,
        name='place-geometry',
    ),
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
    path(
        'places/<slug:slug>/protection/',
        views.article_protection,
        name='article-protection',
    ),
    path(
        'places/<slug:slug>/delete/',
        views.article_delete,
        name='article-delete',
    ),
    path(
        'places/<slug:slug>/restore/',
        views.article_restore,
        name='article-restore',
    ),
    path('places/<slug:slug>/talk/', views.talk, name='talk'),
    path(
        'talk/<int:thread_id>/posts/',
        views.talk_reply,
        name='talk-reply',
    ),
    path(
        'talk/<int:thread_id>/',
        views.talk_thread_delete,
        name='talk-thread-delete',
    ),
    path(
        'talk/posts/<int:post_id>/',
        views.talk_post_edit,
        name='talk-post-edit',
    ),
    path(
        'talk/posts/<int:post_id>/delete/',
        views.talk_post_delete,
        name='talk-post-delete',
    ),
    path('account/close/', views.close_account, name='account-close'),
    path('reports/', views.create_report, name='report-create'),
    path('mod/reports/', views.mod_reports, name='mod-reports'),
    path(
        'mod/reports/<int:report_id>/action/',
        views.mod_report_action,
        name='mod-report-action',
    ),
    path(
        'mod/revisions/<int:revision_id>/restore/',
        views.mod_revision_restore,
        name='mod-revision-restore',
    ),
    path(
        'mod/talk/posts/<int:post_id>/restore/',
        views.mod_talk_post_restore,
        name='mod-talk-post-restore',
    ),
    path('mod/users/', dashboard.mod_users, name='mod-users'),
    path(
        'mod/users/<int:user_id>/',
        dashboard.mod_user_detail,
        name='mod-user-detail',
    ),
    path(
        'mod/users/<int:user_id>/ban/',
        dashboard.mod_ban_user,
        name='mod-ban-user',
    ),
    path(
        'mod/users/<int:user_id>/unban/',
        dashboard.mod_unban_user,
        name='mod-unban-user',
    ),
    path(
        'mod/users/<int:user_id>/role/',
        dashboard.mod_set_role,
        name='mod-set-role',
    ),
    path('mod/reporters/', dashboard.mod_reporters, name='mod-reporters'),
    path('mod/audit/', dashboard.mod_audit, name='mod-audit'),
]
