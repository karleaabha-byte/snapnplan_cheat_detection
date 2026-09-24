from django.urls import path
from . import views

urlpatterns = [
    path("", views.upload, name="upload"),
    path("confirm/", views.confirm, name="confirm"),
    path("retry/", views.retry, name="retry"),
    path("quiz/", views.quiz, name="quiz"),
    path("result/", views.result, name="result"),
    path("plan/", views.plan, name="plan"),
    path("exam/", views.exam, name="exam"),
    path("flashcards/", views.flashcards, name="flashcards"),
]