"""Wire exam mode into the Django project. Run once, from the project root:

    python setup_exam.py

It edits quiz/models.py and quiz/urls.py for you, and checks that every file
exam mode needs is actually in place. Safe to run twice - it detects its own
edits and does nothing the second time. It backs up both files first.

It does NOT touch your existing views, models or URLs.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

APP = "quiz"

MODELS_LINE = "from .exam_models import ExamSession, ExamAttempt   # noqa: F401"
URLS_IMPORT = "from . import exam_views"
URL_LINES = """    path("exam/new/",               exam_views.exam_create,    name="exam_create"),
    path("exam/<str:code>/",        exam_views.exam_join,      name="exam_join"),
    path("exam/<str:code>/run/",    exam_views.exam_run,       name="exam_run"),
    path("exam/<str:code>/mode/",   exam_views.exam_mode,      name="exam_mode"),
    path("exam/<str:code>/submit/", exam_views.exam_submit,    name="exam_submit"),
    path("exam/<str:code>/report/", exam_views.exam_report,    name="exam_report"),
    path("exam/<str:code>/board/",  exam_views.exam_dashboard, name="exam_dashboard"),
    path("exam/<str:code>/status/", exam_views.exam_status,    name="exam_status"),
    path("exam/<str:code>/set/",    exam_views.exam_set_mode,  name="exam_set_mode"),
"""

ok = True


def say(good, msg, hint=""):
    global ok
    if not good:
        ok = False
    print(f"  [{'ok' if good else 'XX'}] {msg}")
    if hint and not good:
        print(f"       {hint}")


# --------------------------------------------------------------- locate ----
root = Path.cwd()
if not (root / "manage.py").exists():
    sys.exit(f"No manage.py in {root}.\n"
             "Run this from the project root (the folder holding manage.py).")

app = root / APP
if not app.is_dir():
    sys.exit(f"No '{APP}' folder in {root}. Is the app called something else?")

print(f"project: {root}\n")

# ---------------------------------------------------------- check files ----
print("files exam mode needs:")
for rel, why in [
    (f"{APP}/exam_views.py", "the views"),
    (f"{APP}/exam_models.py", "the models"),
    (f"{APP}/static/{APP}/proctor.js", "the browser module"),
    (f"{APP}/static/{APP}/screen_model/model.json", "the model"),
    (f"{APP}/templates/{APP}/exam_run.html", "the exam page"),
    (f"{APP}/templates/{APP}/exam_dashboard.html", "the dashboard"),
    (f"{APP}/templates/{APP}/exam_join.html", "the join page"),
    (f"{APP}/templates/{APP}/exam_create.html", "the create page"),
    (f"{APP}/templates/{APP}/exam_report.html", "the report page"),
]:
    p = root / rel
    say(p.exists(), f"{rel}  ({why})", "missing - copy it in first")

shards = list((root / APP / "static" / APP / "screen_model").glob("*.bin"))
say(bool(shards), f"{len(shards)} weight shard(s)",
    "model.json alone is not enough - the .bin files hold the weights")

pj = root / APP / "static" / APP / "proctor.js"
if pj.exists():
    txt = pj.read_text(encoding="utf-8", errors="replace")
    say("loadGraphModel" in txt and "RAW 0-255" in txt,
        "proctor.js is the current version",
        "This is an OLD proctor.js. It normalises the input twice and cannot "
        "load a graph model. Download the newest one (about 16 KB).")

if not ok:
    sys.exit("\nFix the above first, then run this again. Nothing was changed.")

# --------------------------------------------------------- edit models ----
print("\nedits:")
models_py = app / "models.py"
src = models_py.read_text(encoding="utf-8")
if "exam_models import" in src:
    print("  [--] models.py already imports the exam models")
else:
    shutil.copy(models_py, models_py.with_suffix(".py.bak"))
    if not src.endswith("\n"):
        src += "\n"
    src += f"\n\n# --- exam mode ---\n{MODELS_LINE}\n"
    models_py.write_text(src, encoding="utf-8")
    print("  [ok] models.py: added the exam model import "
          "(backup: models.py.bak)")

# ----------------------------------------------------------- edit urls ----
urls_py = app / "urls.py"
if not urls_py.exists():
    sys.exit(f"No {urls_py}. Create it, or tell me where your URLs live.")

src = urls_py.read_text(encoding="utf-8")
changed = False

if URLS_IMPORT not in src:
    shutil.copy(urls_py, urls_py.with_suffix(".py.bak"))
    # Put it straight after the last existing import, so it cannot land above
    # a `from django...` line and look out of place.
    lines = src.splitlines(keepends=True)
    last = max((i for i, l in enumerate(lines)
                if l.startswith(("import ", "from "))), default=-1)
    lines.insert(last + 1, URLS_IMPORT + "\n")
    src = "".join(lines)
    changed = True
    print("  [ok] urls.py: added `from . import exam_views`")
else:
    print("  [--] urls.py already imports exam_views")

if 'name="exam_create"' in src or "name='exam_create'" in src:
    print("  [--] urls.py already has the exam routes")
else:
    m = re.search(r"^urlpatterns\s*=\s*\[", src, re.M)
    if not m:
        sys.exit("Could not find `urlpatterns = [` in urls.py. "
                 "Add the nine path() lines by hand - see exam_urls_snippet.py")

    # Walk to the bracket that closes urlpatterns, ignoring nested ones.
    depth, close = 0, None
    for i in range(m.end() - 1, len(src)):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                close = i
                break
    if close is None:
        sys.exit("urlpatterns list is not closed. Fix urls.py and re-run.")

    if not urls_py.with_suffix(".py.bak").exists():
        shutil.copy(urls_py, urls_py.with_suffix(".py.bak"))

    head = src[:close].rstrip()
    if not head.endswith((",", "[")):
        head += ","          # the previous entry may be missing its comma
    src = head + "\n\n    # --- exam mode ---\n" + URL_LINES + src[close:]
    changed = True
    print("  [ok] urls.py: added the nine exam routes")

if "from django.urls import path" not in src and "import path" not in src:
    src = "from django.urls import path\n" + src
    changed = True
    print("  [ok] urls.py: added `from django.urls import path`")

if changed:
    urls_py.write_text(src, encoding="utf-8")
    print("       (backup: urls.py.bak)")

# ------------------------------------------------------------- compile ----
print("\nsyntax check:")
import py_compile
for f in (models_py, urls_py, app / "exam_views.py", app / "exam_models.py"):
    try:
        py_compile.compile(str(f), doraise=True)
        print(f"  [ok] {f.relative_to(root)}")
    except py_compile.PyCompileError as e:
        print(f"  [XX] {f.relative_to(root)}")
        print(f"       {e}")
        print(f"       Restore with: copy {f.name}.bak {f.name}")
        ok = False

print("\n" + "=" * 62)
if ok:
    print("Done. Now run:\n")
    print("    python manage.py makemigrations quiz")
    print("    python manage.py migrate")
    print("    python manage.py runserver\n")
    print("Then open  http://127.0.0.1:8000/exam/new/")
else:
    print("Something did not compile. The .bak files hold the originals.")
