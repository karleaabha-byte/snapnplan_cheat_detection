import datetime as dt
import shutil
import uuid
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.shortcuts import redirect, render
from PIL import Image

from .classifier import classify
from .pipeline import MAX_PAGES, QuizError, make_quiz
from .planner import (MAX_EXAM_DAYS, concept_stats, make_exam_plan,
                      make_study_plan)

MAX_HISTORY = 30

NAMES = {
    "handwritten": "Handwritten notes",
    "printed": "Printed text",
    "diagrams": "Diagram",
}


def _media(rel):
    return Path(settings.MEDIA_ROOT) / rel


def upload(request):
    if request.method != "POST":
        return render(request, "quiz/upload.html",
                      {"max_pages": MAX_PAGES})

    files = request.FILES.getlist("page")
    if not files:
        return _upload_error(request, "Choose at least one photo.")
    if len(files) > MAX_PAGES:
        return _upload_error(
            request, f"Up to {MAX_PAGES} pages at a time, please.")

    folder = _media("uploads")
    folder.mkdir(parents=True, exist_ok=True)
    pages = []
    for f in files:
        try:
            Image.open(f).verify()   # reject anything not an image
        except Exception:
            return _upload_error(
                request, f"{f.name} isn't an image.")
        ext = Path(f.name).suffix.lower() or ".jpg"
        name = f"{uuid.uuid4().hex}{ext}"
        f.seek(0)
        with open(folder / name, "wb") as out:
            for chunk in f.chunks():
                out.write(chunk)
        probs = classify(folder / name)
        pred, conf = next(iter(probs.items()))
        pages.append({"file": f"uploads/{name}", "name": f.name,
                      "pred": pred, "conf": conf, "probs": probs,
                      "label": pred})

    request.session["pages"] = pages
    unsure = [p for p in pages
              if p["conf"] < settings.CONFIDENCE_THRESHOLD]
    if unsure:
        return redirect("confirm")   # ask about the unsure pages only
    return _build_quiz(request)


def _upload_error(request, msg, can_retry=False):
    return render(request, "quiz/upload.html", {
        "error": msg, "can_retry": can_retry, "max_pages": MAX_PAGES})


def confirm(request):
    pages = request.session.get("pages")
    if not pages:
        return redirect("upload")
    threshold = settings.CONFIDENCE_THRESHOLD

    if request.method == "POST":
        for i, p in enumerate(pages):
            label = request.POST.get(f"label_{i}")
            if label not in NAMES:
                continue
            p["label"] = label
            if label != p["pred"]:
                # model was wrong: keep the photo as training data
                dst = _media("corrections") / label
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy(_media(p["file"]), dst)
        request.session["pages"] = pages
        return _build_quiz(request)

    rows = []
    for i, p in enumerate(pages):
        rows.append({
            "i": i, "name": p["name"],
            "url": settings.MEDIA_URL + p["file"],
            "unsure": p["conf"] < threshold,
            "pred": p["pred"], "pred_name": NAMES[p["pred"]],
            "conf": round(p["conf"] * 100),
            "options": [(k, NAMES[k], round(p["probs"].get(k, 0) * 100))
                        for k in NAMES],
        })
    return render(request, "quiz/confirm.html", {"rows": rows})


def retry(request):
    """The AI was busy: try again with the same pages and labels."""
    if request.method != "POST" or not request.session.get("pages"):
        return redirect("upload")
    return _build_quiz(request)


def _build_quiz(request):
    pages = request.session["pages"]
    try:
        quiz, route = make_quiz([_media(p["file"]) for p in pages],
                                [p["label"] for p in pages])
    except QuizError as e:
        return _upload_error(request, str(e), can_retry=True)

    counts = Counter(NAMES[p["label"]] for p in pages)
    if len(pages) == 1:
        label = NAMES[pages[0]["label"]]
    else:
        label = ", ".join(f"{c} {n}" for n, c in counts.items())
    threshold = settings.CONFIDENCE_THRESHOLD
    auto = all(p["label"] == p["pred"] and p["conf"] >= threshold
               for p in pages)
    request.session["quiz"] = {
        **quiz, "id": uuid.uuid4().hex,
        "route": route, "label": label, "auto": auto,
        "conf": round(min(p["conf"] for p in pages) * 100),
        "n_pages": len(pages),
    }
    return redirect("quiz")


def quiz(request):
    q = request.session.get("quiz")
    if not q:
        return redirect("upload")
    if "id" not in q:                # quiz made before this update
        q["id"] = uuid.uuid4().hex
        request.session["quiz"] = q
    if request.method != "POST":
        return render(request, "quiz/quiz.html", {"quiz": q})

    results, score = [], 0
    for i, item in enumerate(q["questions"]):
        picked = request.POST.get(f"q{i}")
        if picked is not None and picked.isdigit():
            picked = int(picked)
        else:
            picked = None
        right = picked == item["answer_index"]
        score += right
        results.append({**item, "picked": picked, "right": right})
    request.session["results"] = {"quiz_id": q["id"], "items": results,
                                  "score": score}
    _remember(request, q, results, score)
    return redirect("result")


def _remember(request, q, results, score):
    """Keep a small history of quizzes on this device for exam plans.

    Retaking the same quiz replaces its entry with the latest attempt.
    """
    history = [h for h in request.session.get("history", [])
               if h["id"] != q["id"]]
    history.append({
        "id": q["id"], "topic": q["topic"],
        "date": dt.date.today().isoformat(),
        "score": score, "total": len(results),
        "concepts": concept_stats(results),
    })
    request.session["history"] = history[-MAX_HISTORY:]


def result(request):
    q = request.session.get("quiz")
    res = request.session.get("results")
    if not q or not res or res["quiz_id"] != q["id"]:
        return redirect("quiz" if q else "upload")
    return render(request, "quiz/result.html", {
        "quiz": q, "results": res["items"], "score": res["score"],
        "total": len(res["items"]),
        "has_plan": request.session.get("plan", {}).get("quiz_id")
        == q["id"],
    })


def plan(request):
    q = request.session.get("quiz")
    res = request.session.get("results")
    if not q or not res or res["quiz_id"] != q["id"]:
        return redirect("upload")

    if request.method == "POST":
        try:
            p = make_study_plan(q, res["items"])
        except QuizError as e:
            return render(request, "quiz/plan.html",
                          {"quiz": q, "error": str(e)})
        p["quiz_id"] = q["id"]
        request.session["plan"] = p
        return redirect("plan")

    p = request.session.get("plan")
    if not p or p.get("quiz_id") != q["id"]:
        return redirect("result")
    return render(request, "quiz/plan.html", {
        "quiz": q, "plan": p, "score": res["score"],
        "total": len(res["items"]),
    })


def exam(request):
    history = request.session.get("history", [])
    today = dt.date.today()
    ctx = {
        "history": history,
        "min_date": (today + dt.timedelta(days=1)).isoformat(),
        "max_date": (today + dt.timedelta(days=MAX_EXAM_DAYS)).isoformat(),
        "plan": request.session.get("exam_plan"),
        "minute_choices": [(30, "30 min"), (45, "45 min"),
                           (60, "1 hour"), (90, "1.5 hours"),
                           (120, "2 hours"), (180, "3 hours")],
    }
    if request.method == "POST":
        try:
            exam_date = dt.date.fromisoformat(request.POST.get("date", ""))
            minutes = int(request.POST.get("minutes", "60"))
        except ValueError:
            ctx["error"] = "Pick your exam date."
            return render(request, "quiz/exam.html", ctx)
        try:
            p = make_exam_plan(history, exam_date, minutes, today)
        except QuizError as e:
            ctx["error"] = str(e)
            return render(request, "quiz/exam.html", ctx)
        request.session["exam_plan"] = p
        return redirect("exam")
    return render(request, "quiz/exam.html", ctx)


def flashcards(request):
    q = request.session.get("quiz")
    if not q:
        return redirect("upload")
    return render(request, "quiz/flashcards.html", {"quiz": q})