from django.urls import path
from . import views
from . import exam_views

urlpatterns = [
    path("", views.upload, name="upload"),
    path("confirm/", views.confirm, name="confirm"),
    path("retry/", views.retry, name="retry"),
    path("quiz/", views.quiz, name="quiz"),
    path("result/", views.result, name="result"),
    path("plan/", views.plan, name="plan"),
    path("exam/", views.exam, name="exam"),
    path("flashcards/", views.flashcards, name="flashcards"),

    # --- exam mode ---
    path("exam/new/",               exam_views.exam_create,    name="exam_create"),
    path("exam/<str:code>/",        exam_views.exam_join,      name="exam_join"),
    path("exam/<str:code>/run/",    exam_views.exam_run,       name="exam_run"),
    path("exam/<str:code>/mode/",   exam_views.exam_mode,      name="exam_mode"),
    path("exam/<str:code>/submit/", exam_views.exam_submit,    name="exam_submit"),
    path("exam/<str:code>/report/", exam_views.exam_report,    name="exam_report"),
    path("exam/<str:code>/board/",  exam_views.exam_dashboard, name="exam_dashboard"),
    path("exam/<str:code>/status/", exam_views.exam_status,    name="exam_status"),
    path("exam/<str:code>/set/",    exam_views.exam_set_mode,  name="exam_set_mode"),
]