"""Personalised study plans.

1. make_study_plan: after one quiz -> weak concepts (from the wrong
   answers), how to revise them, a revision schedule and extra
   practice questions on exactly those concepts.
2. make_exam_plan: from every quiz taken on this device -> a
   day-by-day plan until the exam date, weakest concepts first.

Which concepts are weak is worked out HERE from the answers (plain
counting), not guessed by the AI; the AI only writes the advice.
"""
import datetime as dt
from collections import defaultdict

from pydantic import BaseModel

from .pipeline import Question, QuizError, ask_ai, _problem

MAX_EXAM_DAYS = 30


# ------------------------------------------------------------ schemas

class WeakTopic(BaseModel):
    concept: str
    why: str             # what the mistake shows the student mixed up
    how_to_revise: str   # one concrete revision action


class Step(BaseModel):
    when: str            # "Today", "In 3 days", "In 1 week"
    task: str


class StudyPlan(BaseModel):
    summary: str
    weak_topics: list[WeakTopic]
    strengths: list[str]
    schedule: list[Step]
    practice: list[Question]


class Day(BaseModel):
    date: str            # YYYY-MM-DD
    focus: str
    tasks: list[str]
    minutes: int


class ExamPlan(BaseModel):
    overview: str
    days: list[Day]
    tips: list[str]


# ------------------------------------------------------ concept stats

def concept_stats(results):
    """{concept: [right, total]} from one quiz's marked answers."""
    stats = defaultdict(lambda: [0, 0])
    for r in results:
        c = (r.get("concept") or "General").strip()
        stats[c][0] += int(r["right"])
        stats[c][1] += 1
    return dict(stats)


def merge_history(history):
    """Combine every past quiz into per-concept totals, weakest first."""
    total = defaultdict(lambda: [0, 0])
    for h in history:
        for c, (right, n) in h["concepts"].items():
            total[c][0] += right
            total[c][1] += n
    rows = [(c, r, n) for c, (r, n) in total.items()]
    rows.sort(key=lambda x: (x[1] / x[2], -x[2]))   # lowest score first
    return rows


# --------------------------------------------------- plan after a quiz

def make_study_plan(quiz, results):
    stats = concept_stats(results)
    weak = [c for c, (r, n) in stats.items() if r < n]
    strong = [c for c, (r, n) in stats.items() if r == n]

    lines = []
    for r in results:
        mark = "RIGHT" if r["right"] else "WRONG"
        picked = ("no answer" if r["picked"] is None
                  else r["options"][r["picked"]])
        lines.append(
            f"- [{mark}] ({r.get('concept', 'General')}) {r['question']}\n"
            f"  student chose: {picked}\n"
            f"  correct: {r['options'][r['answer_index']]}")
    material = (f"QUIZ TOPIC: {quiz['topic']}\n"
                f"WEAK CONCEPTS: {', '.join(weak) or 'none'}\n"
                f"STRONG CONCEPTS: {', '.join(strong) or 'none'}\n"
                "ANSWERS:\n" + "\n".join(lines))

    n_practice = 3 if weak else 2
    prompt = (
        "You are a friendly, encouraging tutor. A student just took the "
        "quiz below. Write a short personalised study plan. "
        "summary: 1-2 sentences on how they did and what to focus on. "
        "weak_topics: one entry per WEAK CONCEPT listed (use exactly "
        "those names); 'why' explains what their wrong answer shows "
        "they mixed up, 'how_to_revise' is one concrete action. "
        "strengths: the strong concepts, briefly. "
        "schedule: exactly 3 steps with when = 'Today', 'In 3 days' "
        "and 'In 1 week' (spaced repetition). "
        f"practice: exactly {n_practice} NEW multiple-choice questions "
        "on the weak concepts (or on the whole topic if none are "
        "weak), each with concept, 4 options, an explanation written "
        "before answer_index, and answer_index 0-3. Do the working "
        "for any calculation before choosing the answer."
    )
    plan, used = ask_ai(prompt, StudyPlan, material)
    print(f"[plan] study plan by {used}")

    out = plan.model_dump()
    out["practice"] = [q.model_dump() for q in plan.practice
                       if not _problem(q)]
    out["weak_names"] = weak
    out["stats"] = [{"concept": c, "right": r, "total": n}
                    for c, (r, n) in stats.items()]
    return out


# ------------------------------------------------------ exam countdown

def make_exam_plan(history, exam_date, minutes_per_day, today=None):
    today = today or dt.date.today()
    days_left = (exam_date - today).days
    if days_left < 1:
        raise QuizError("Pick an exam date after today.")
    if days_left > MAX_EXAM_DAYS:
        raise QuizError(f"Pick an exam date within {MAX_EXAM_DAYS} days.")
    if not history:
        raise QuizError("Take at least one quiz first, so I know "
                        "what you're studying.")

    rows = merge_history(history)
    topics = sorted({h["topic"] for h in history})
    concept_lines = [f"- {c}: {r}/{n} correct ({round(100 * r / n)}%)"
                     for c, r, n in rows]
    dates = [(today + dt.timedelta(days=i)).isoformat()
             for i in range(days_left)]
    material = (
        f"TODAY: {today.isoformat()}\n"
        f"EXAM DATE: {exam_date.isoformat()} ({days_left} days away)\n"
        f"STUDY TIME PER DAY: about {minutes_per_day} minutes\n"
        f"TOPICS STUDIED: {'; '.join(topics)}\n"
        "CONCEPT SCORES FROM THEIR QUIZZES (weakest first):\n"
        + "\n".join(concept_lines))

    prompt = (
        "You are an expert study coach. Build a day-by-day revision "
        "plan from today until the day before the exam. Give exactly "
        f"one entry for EACH of these dates, in order: {', '.join(dates)}. "
        "Put the weakest concepts first and come back to them again "
        "a few days later (spaced repetition). Mix activities: "
        "re-reading notes, flashcards, retaking quizzes, practice "
        "problems, explaining a concept out loud. Keep each day within "
        "the study time. The last day before the exam is a light "
        "full review plus rest, no new material. "
        "Each day: date (YYYY-MM-DD), focus (a few words), 2-4 short "
        "tasks, minutes. overview: 2 sentences on the strategy. "
        "tips: 3 short exam tips."
    )
    plan, used = ask_ai(prompt, ExamPlan, material)
    print(f"[plan] exam plan by {used}")

    # keep only the real dates, in order, one entry per date
    by_date = {d.date: d for d in plan.days}
    days = []
    for i, iso in enumerate(dates):
        d = by_date.get(iso)
        if d is None:
            continue
        day = d.model_dump()
        when = today + dt.timedelta(days=i)
        day["label"] = ("Today" if i == 0 else "Tomorrow" if i == 1
                        else when.strftime("%a %d %b"))
        days.append(day)
    if not days:
        raise QuizError("Couldn't build the plan. Try again.")
    return {
        "overview": plan.overview, "tips": plan.tips, "days": days,
        "exam_date": exam_date.isoformat(), "days_left": days_left,
        "made_on": today.isoformat(), "minutes": minutes_per_day,
        "weakest": [{"concept": c, "pct": round(100 * r / n)}
                    for c, r, n in rows[:5]],
    }