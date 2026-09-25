"""Add an 'Exam mode' link to the SnapNPlan navigation.

    python add_nav_link.py

Idempotent, backs up base.html first. Note the label is 'Exam mode', not
'Exam' - there is already an 'Exam plan' link pointing at the countdown
planner, and two things called Exam in one navbar is a demo waiting to go
wrong.
"""
import re
import shutil
import sys
from pathlib import Path

BASE = Path("quiz/templates/quiz/base.html")
LINK = '      <a class="navlink" href="{% url \'exam_create\' %}">Exam mode</a>\n'

if not Path("manage.py").exists():
    sys.exit("Run this from the project root (the folder with manage.py).")
if not BASE.exists():
    sys.exit(f"{BASE} not found.")

src = BASE.read_text(encoding="utf-8")

if "exam_create" in src:
    print("base.html already links to exam mode. Nothing to do.")
    raise SystemExit(0)

lines = src.splitlines(keepends=True)

# Insert just inside <nav class="topnav">, so it sits with the other links
# rather than wherever a text match happens to land.
nav = next((i for i, l in enumerate(lines) if 'class="topnav"' in l), None)
if nav is None:
    sys.exit('Could not find <nav class="topnav"> in base.html. '
             "Add this line inside your nav by hand:\n\n" + LINK)

shutil.copy(BASE, BASE.with_suffix(".html.bak"))
lines.insert(nav + 1, LINK)
BASE.write_text("".join(lines), encoding="utf-8")

# Show the result so it can be eyeballed without opening the file.
print("added to base.html (backup: base.html.bak)\n")
for i in range(max(0, nav - 1), min(len(lines), nav + 5)):
    mark = ">>" if i == nav + 1 else "  "
    print(f"{mark} {lines[i].rstrip()}")

opens = len(re.findall(r"<nav\b", "".join(lines)))
closes = "".join(lines).count("</nav>")
if opens != closes:
    print(f"\nWARNING: {opens} <nav> vs {closes} </nav> - check the file.")
