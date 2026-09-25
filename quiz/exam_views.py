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
import os
import secrets
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import ExamSession, ExamAttempt   # see models.py below


MODES = {"monitoring", "permitted", "paused"}


# Sample questions, so a session is sittable the moment it is created and the
# proctoring can be demonstrated without a quiz pipeline in the way.
#
# TO USE YOUR OWN QUIZ: in exam_create below, replace DEMO_QUESTIONS with a
# call to your generator, e.g.
#
#     from .pipeline import make_quiz
#     questions = make_quiz(source_text, n=10)
#
# Each question needs `question`, `options` and `answer_index` (0-based).
# exam_run.html also accepts `text`/`prompt` and `choices`, so most existing
# shapes work unchanged.
DEMO_QUESTIONS = [
    {"question": "Which data structure gives O(1) average lookup by key?",
     "options": ["Array", "Hash table", "Linked list", "Binary search tree"],
     "answer_index": 1},
    {"question": "What is the worst-case time complexity of quicksort?",
     "options": ["O(n)", "O(n log n)", "O(n^2)", "O(log n)"],
     "answer_index": 2},
    {"question": "A stack is best described as:",
     "options": ["First in, first out", "Last in, first out",
                 "Sorted on insert", "Random access"],
     "answer_index": 1},
    {"question": "Which traversal of a binary search tree yields sorted order?",
     "options": ["Pre-order", "In-order", "Post-order", "Level-order"],
     "answer_index": 1},
    {"question": "Big-O notation describes:",
     "options": ["Exact running time", "An upper bound on growth",
                 "Memory used", "Number of lines of code"],
     "answer_index": 1},
]


# ------------------------------------------------------------- the gate ---
#
# WHAT THIS IS, AND WHAT IT IS NOT
#
# A shared passcode that stops a student wandering into /exam/new/ and
# starting sessions. It is NOT authentication: everyone who knows it is the
# same person as far as the app is concerned, it cannot be revoked for one
# individual, and a determined student who watches an invigilator type it has
# it forever.
#
# The production answer is Django's own auth with a staff check:
#
#     from django.contrib.auth.decorators import user_passes_test
#     @user_passes_test(lambda u: u.is_staff)
#     def exam_create(request): ...
#
# That gives real accounts, per-person revocation and an audit trail of who
# started which exam. Say so plainly in the write-up rather than implying this
# gate is more than it is.
#
# Note what was ALREADY protected before this gate existed: exam_dashboard and
# exam_set_mode compare owner_key against the browser session, so knowing a
# session code never let a student open the dashboard or pause the room. The
# only gap was who may CREATE a session.

MAX_TRIES = 5


def _configured_passcode():
    """settings.EXAM_PASSCODE, else the EXAM_PASSCODE env var, else None.

    Returning None fails CLOSED - exam mode refuses to open rather than
    falling back to a default passcode. A default would be worse than no gate
    at all, because it would look protected while being public.
    """
    code = getattr(settings, "EXAM_PASSCODE", None) or os.environ.get(
        "EXAM_PASSCODE", "")
    return code.strip() or None


def _gate(request):
    """Returns a response if the visitor must be stopped, else None."""
    passcode = _configured_passcode()
    if passcode is None:
        return render(request, "quiz/exam_gate.html", {"unconfigured": True})

    if request.session.get("is_invigilator"):
        return None

    tries = request.session.get("exam_tries", 0)
    if tries >= MAX_TRIES:
        return render(request, "quiz/exam_gate.html", {"locked": True})

    if request.method == "POST" and "passcode" in request.POST:
        given = request.POST.get("passcode", "").strip()
        # compare_digest rather than == so the comparison does not finish
        # early on the first wrong character.
        if secrets.compare_digest(given.encode("utf-8"),
                                  passcode.encode("utf-8")):
            request.session["is_invigilator"] = True
            request.session["exam_tries"] = 0
            return redirect("exam_create")
        request.session["exam_tries"] = tries + 1
        return render(request, "quiz/exam_gate.html",
                      {"error": "That passcode is not right.",
                       "left": MAX_TRIES - tries - 1})

    return render(request, "quiz/exam_gate.html", {})


# ----------------------------------------------------------- invigilator ---

def exam_create(request):
    """Invigilator starts a session and gets a code to read to the room."""
    blocked = _gate(request)
    if blocked is not None:
        return blocked

    if request.method != "POST":
        return render(request, "quiz/exam_create.html")

    minutes = int(request.POST.get("minutes", 30) or 30)
    session = ExamSession.objects.create(
        code=secrets.token_hex(3).upper(),      # e.g. 'A3F19C'
        title=request.POST.get("title", "Midterm").strip() or "Midterm",
        minutes=max(1, min(minutes, 240)),
        owner_key=_owner_key(request),
        questions=DEMO_QUESTIONS,      # <- swap for your own generator
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
