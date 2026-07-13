from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('health/', views.health, name='health'),
    path('me/', views.me, name='me'),
    path('resolve/', views.resolve, name='resolve'),
    path('places/<slug:slug>/', views.place_detail, name='place-detail'),
    path(
        'places/<slug:slug>/article/',
        views.article_edit,
        name='article-edit',
    ),
]
