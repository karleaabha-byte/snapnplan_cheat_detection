"""Exam mode: server side.

Drop these into quiz/views.py (and the urls at the bottom into quiz/urls.py).

The invigilator sets one mode for a session; every student's browser polls it
every few seconds and switches. Polling rather than WebSockets on purpose:
PythonAnywhere's free tier has no persistent sockets, and a 5 second poll is
more than fast enough for "everyone may look things up now".

An ExamSession is identified by a short code the invigilator reads out, so no
accounts are needed for a classroom demo.
"""
import json
import secrets
from datetime import timedelta

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import ExamSession, ExamAttempt   # see models.py below


MODES = {"monitoring", "permitted", "paused"}


# ----------------------------------------------------------- invigilator ---

def exam_create(request):
    """Invigilator starts a session and gets a code to read to the room."""
    if request.method != "POST":
        return render(request, "quiz/exam_create.html")

    minutes = int(request.POST.get("minutes", 30) or 30)
    session = ExamSession.objects.create(
        code=secrets.token_hex(3).upper(),      # e.g. 'A3F19C'
        title=request.POST.get("title", "Midterm").strip() or "Midterm",
        minutes=max(1, min(minutes, 240)),
        owner_key=_owner_key(request),
    )
    return redirect("exam_dashboard", code=session.code)


def exam_dashboard(request, code):
    """Live grid of students. Polls exam_status for updates."""
    session = get_object_or_404(ExamSession, code=code)
    if session.owner_key != _owner_key(request):
        return HttpResponseBadRequest("Not your session.")
    return render(request, "quiz/exam_dashboard.html", {"session": session})


@require_POST
def exam_set_mode(request, code):
    """Invigilator flips the whole room, or one student, into another mode."""
    session = get_object_or_404(ExamSession, code=code)
    if session.owner_key != _owner_key(request):
        return HttpResponseBadRequest("Not your session.")

    mode = request.POST.get("mode", "")
    if mode not in MODES:
        return HttpResponseBadRequest("Unknown mode.")
    reason = request.POST.get("reason", "")[:200]
    attempt_id = request.POST.get("attempt")

    if attempt_id:                               # one student
        attempt = get_object_or_404(ExamAttempt, id=attempt_id, session=session)
        attempt.mode = mode
        attempt.mode_reason = reason
        attempt.save(update_fields=["mode", "mode_reason"])
    else:                                        # the whole room
        session.mode = mode
        session.mode_reason = reason
        session.save(update_fields=["mode", "mode_reason"])
        session.attempts.update(mode="", mode_reason="")   # clear overrides

    return JsonResponse({"ok": True, "mode": mode})


def exam_status(request, code):
    """Dashboard polls this: every student's current state."""
    session = get_object_or_404(ExamSession, code=code)
    now = timezone.now()
    rows = []
    for a in session.attempts.all().order_by("name"):
        last = a.last_seen
        stale = last is None or (now - last) > timedelta(seconds=20)
        rows.append({
            "id": a.id,
            "name": a.name,
            "state": "offline" if stale else a.state,
            "mode": a.mode or session.mode,
            "flags": a.flag_count,
            "submitted": a.submitted_at is not None,
        })
    return JsonResponse({
        "mode": session.mode,
        "reason": session.mode_reason,
        "students": rows,
    })


# --------------------------------------------------------------- student ---

def exam_join(request, code):
    """Student enters their name and starts."""
    session = get_object_or_404(ExamSession, code=code)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()[:60]
        if not name:
            return render(request, "quiz/exam_join.html",
                          {"session": session, "error": "Enter your name."})
        attempt = ExamAttempt.objects.create(session=session, name=name)
        request.session[f"attempt_{session.code}"] = attempt.id
        return redirect("exam_run", code=code)
    return render(request, "quiz/exam_join.html", {"session": session})


def exam_run(request, code):
    """The exam itself. Questions come from your existing quiz pipeline."""
    session = get_object_or_404(ExamSession, code=code)
    attempt = _attempt(request, session)
    if attempt is None:
        return redirect("exam_join", code=code)
    return render(request, "quiz/exam_run.html", {
        "session": session,
        "attempt": attempt,
        "questions": session.questions,          # JSONField from make_quiz()
    })


def exam_mode(request, code):
    """Polled by proctor.js. Per-student override wins over the room mode.

    Doubles as the heartbeat: this is where we learn a student is still
    connected, and what their proctor is currently seeing.
    """
    session = get_object_or_404(ExamSession, code=code)
    attempt = _attempt(request, session)
    if attempt is None:
        return JsonResponse({"mode": "monitoring"})

    state = request.GET.get("state", "")
    fields = ["last_seen"]
    attempt.last_seen = timezone.now()
    if state in {"ok", "warning", "flagged", "permitted", "paused", "stopped"}:
        attempt.state = state
        fields.append("state")
    attempt.save(update_fields=fields)

    mode = attempt.mode or session.mode
    reason = attempt.mode_reason or session.mode_reason
    return JsonResponse({"mode": mode, "reason": reason})


@require_POST
def exam_submit(request, code):
    """Answers plus the proctor's event log. No frames, ever."""
    session = get_object_or_404(ExamSession, code=code)
    attempt = _attempt(request, session)
    if attempt is None:
        return HttpResponseBadRequest("No attempt in progress.")

    try:
        payload = json.loads(request.body.decode() or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Bad payload.")

    answers = payload.get("answers", [])
    report = payload.get("report", {})
    events = report.get("events", [])

    score = sum(
        1 for i, q in enumerate(session.questions)
        if i < len(answers) and answers[i] == q.get("answer_index"))

    attempt.answers = answers
    attempt.score = score
    attempt.events = events
    attempt.stats = report.get("stats", {})
    attempt.flag_count = sum(
        1 for e in events if e.get("type") == "other_window_visible")
    attempt.state = "stopped"
    attempt.submitted_at = timezone.now()
    attempt.save()

    return JsonResponse({"ok": True, "score": score,
                         "total": len(session.questions),
                         "flags": attempt.flag_count})


def exam_report(request, code):
    """What the student sees afterwards: score and their own timeline."""
    session = get_object_or_404(ExamSession, code=code)
    attempt = _attempt(request, session)
    if attempt is None:
        return redirect("exam_join", code=code)
    return render(request, "quiz/exam_report.html",
                  {"session": session, "attempt": attempt})


# ----------------------------------------------------------------- utils ---

def _owner_key(request):
    """Cheap ownership token so an invigilator keeps control without login."""
    key = request.session.get("invigilator_key")
    if not key:
        key = secrets.token_hex(16)
        request.session["invigilator_key"] = key
    return key


def _attempt(request, session):
    aid = request.session.get(f"attempt_{session.code}")
    if not aid:
        return None
    return ExamAttempt.objects.filter(id=aid, session=session).first()


# ------------------------------------------------------------------ urls ---
# Add to quiz/urls.py:
#
# path("exam/new/",              views.exam_create,   name="exam_create"),
# path("exam/<str:code>/",       views.exam_join,     name="exam_join"),
# path("exam/<str:code>/run/",   views.exam_run,      name="exam_run"),
# path("exam/<str:code>/mode/",  views.exam_mode,     name="exam_mode"),
# path("exam/<str:code>/submit/",views.exam_submit,   name="exam_submit"),
# path("exam/<str:code>/report/",views.exam_report,   name="exam_report"),
# path("exam/<str:code>/board/", views.exam_dashboard,name="exam_dashboard"),
# path("exam/<str:code>/status/",views.exam_status,   name="exam_status"),
# path("exam/<str:code>/set/",   views.exam_set_mode, name="exam_set_mode"),
