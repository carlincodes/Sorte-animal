from django.urls import path

from jogo import views

urlpatterns = [
    path("", views.inicio, name="inicio"),
    path("s/<str:codigo>/", views.sala, name="sala"),
    path("s/<str:codigo>/painel/", views.painel, name="painel"),
    path("api/sala/<str:codigo>/estado/", views.api_estado, name="api_estado"),
    path("api/sala/<str:codigo>/abrir/", views.api_abrir, name="api_abrir"),
    path("api/sala/<str:codigo>/sortear/", views.api_sortear, name="api_sortear"),
    path("api/sala/<str:codigo>/apostar/", views.api_apostar, name="api_apostar"),
]
