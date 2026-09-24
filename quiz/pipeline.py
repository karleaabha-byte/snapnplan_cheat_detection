"""Pages -> text and/or images -> LLM -> one MCQ quiz + flashcards.

One or several pages (up to MAX_PAGES) make ONE combined quiz.
Each page is OCR'd if it is printed/handwritten and readable,
otherwise sent as a photo.

Several free AI providers are asked in parallel; the first good
answer wins, so one busy provider never makes the student wait.

  text pages (OCR worked) : Groq + Mistral + Gemini
  photos (diagrams, messy handwriting) : Mistral + Gemini (vision)

Only the fast first-choice models are asked straight away; the
backup Gemini models join if nobody has answered after a few seconds.
"""
import base64
import io
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                wait)

import pytesseract
from django.conf import settings
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image, ImageOps
from pydantic import BaseModel

if settings.TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD

MAX_PAGES = 10
MIN_OCR_CHARS = 80   # less text than this = OCR failed
MIN_OCR_CONF = 60    # mean Tesseract word confidence (0-100)

FALLBACK_MODELS = [          # backup Gemini models (all free tier)
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]
HEDGE_AFTER = 6   # seconds before the backup models join the race

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODELS = ["mistral-small-latest", "pixtral-12b-latest"]

JSON_SHAPE = (
    'Reply with ONLY a JSON object of this shape: {"topic": str, '
    '"questions": [{"question": str, "concept": str, '
    '"options": [4 strings], '
    '"explanation": str, "answer_index": int}], '
    '"flashcards": [{"front": str, "back": str}]}'
)


class Question(BaseModel):
    question: str
    concept: str         # short name of the idea tested (for study plans)
    options: list[str]
    explanation: str     # BEFORE answer_index: work it out, then pick
    answer_index: int


class Card(BaseModel):
    front: str
    back: str


class Quiz(BaseModel):
    topic: str
    questions: list[Question]
    flashcards: list[Card]


class QuizError(Exception):
    pass


def _open(path, max_side):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def ocr(path):
    """Return (text, mean word confidence). One Tesseract pass."""
    im = _open(path, 2000).convert("L")
    d = pytesseract.image_to_data(
        im, output_type=pytesseract.Output.DICT)
    lines, confs = {}, []
    for i, word in enumerate(d["text"]):
        if not word.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append(word)
        if float(d["conf"][i]) >= 0:
            confs.append(float(d["conf"][i]))
    text = "\n".join(" ".join(ws) for ws in lines.values())
    mean = sum(confs) / len(confs) if confs else 0.0
    return text, mean


DIAGRAM_FOCUS = (
    "The material is a diagram. First work out which concept, "
    "process or system it illustrates, then ask about THAT concept: "
    "what each labelled part does, how the process works step by "
    "step, cause and effect, what happens if something changes."
)

TEXT_FOCUS = (
    "Ask about the key concepts, definitions, reasons, processes "
    "and relationships in the material."
)

RULES = (
    "Test understanding of the SUBJECT, like an exam would. "
    "NEVER ask about the page itself: no questions about colours, "
    "arrows, shapes, layout, positions, fonts, handwriting, or what "
    "a symbol or colour 'represents' on the page. "
    "Mix difficulty: about 40% recall, 40% understanding and "
    "20% application questions. "
    "Each question has exactly 4 options, one correct; the wrong "
    "options must be plausible, not obviously silly. "
    "answer_index is the correct option's position (0-3). "
    "concept is a 2-5 word name of the idea the question tests "
    "(e.g. 'MLE of binomial p'); reuse the same name for questions "
    "on the same idea. "
    "Write the explanation BEFORE choosing answer_index: for any "
    "calculation, do the working step by step in the explanation, "
    "then set answer_index to the option that equals your result. "
    "The correct answer must be one of the 4 options, and the "
    "explanation must agree with answer_index. Double-check sums. "
    "Base questions on the material; you may use standard textbook "
    "knowledge of the same topic to explain, but do not ask about "
    "things the material does not cover. "
    "Also write 8 flashcards: front is a key term or short question, "
    "back is a clear answer in at most 25 words. "
    "Set topic to a short title for the material. "
    "If the material is unreadable or has no study content, "
    "return empty questions and flashcards."
)


def _instructions(n, n_cards, has_diagram, n_pages):
    focus = TEXT_FOCUS
    if has_diagram:
        focus += " For any diagram: " + DIAGRAM_FOCUS
    pages = ""
    if n_pages > 1:
        pages = (f"The material has {n_pages} pages (marked PAGE 1, "
                 "PAGE 2, ... or given as images in order). Spread the "
                 "questions and flashcards across ALL pages. ")
    rules = RULES.replace("Also write 8 flashcards",
                          f"Also write {n_cards} flashcards")
    return (
        "You are a teacher writing a revision quiz from a student's "
        f"study material. Write exactly {n} multiple-choice "
        f"questions. {pages}{focus} {rules}"
    )


def make_quiz(paths, labels):
    """paths, labels: one entry per page.

    Returns (quiz dict, route description).
    """
    if not settings.GEMINI_API_KEY:
        raise QuizError("GEMINI_API_KEY is not set in .env")
    k = len(paths)
    n = min(settings.NUM_QUESTIONS + 2 * (k - 1), 15)
    n_cards = min(8 + 4 * (k - 1), 20)

    # OCR the text pages in parallel (Tesseract runs outside Python)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ocrs = list(pool.map(
            lambda pl: ocr(pl[0]) if pl[1] != "diagrams" else None,
            zip(paths, labels)))
    if any(ocrs):
        print(f"[ocr] {time.time() - t0:.1f}s for {k} page(s)")

    texts, images, n_text = [], [], 0
    for i, (path, res) in enumerate(zip(paths, ocrs), start=1):
        if res and len(res[0]) >= MIN_OCR_CHARS \
                and res[1] >= MIN_OCR_CONF:
            texts.append(f"PAGE {i}:\n{res[0]}")
            n_text += 1
        else:
            images.append(_jpeg(path))

    prompt = _instructions(n, n_cards, "diagrams" in labels, k)
    text = "\n\n".join(texts) if texts else None
    quiz, used = _race(prompt, text=text, images=images)

    if k == 1:
        if images and labels[0] == "diagrams":
            route = f"Diagram -> {used} (vision)"
        elif images:
            route = f"OCR unreadable -> photo sent to {used}"
        else:
            route = f"OCR ({len(text)} characters) -> {used}"
    else:
        route = (f"{k} pages: {n_text} read by OCR, "
                 f"{len(images)} sent as photos -> {used}")

    good = []
    for q in quiz.questions:
        why = _problem(q)
        if why:
            print(f"[check] dropped a question: {why}")
        else:
            good.append(q.model_dump())
    print(f"[check] {len(good)} of {len(quiz.questions)} questions kept")
    if not good:
        raise QuizError(
            "Couldn't make questions from this page. "
            "Try a clearer photo.")

    cards = []
    for c in quiz.flashcards:
        if c.front.strip() and c.back.strip():
            cards.append(c.model_dump())

    result = {
        "topic": quiz.topic,
        "questions": good,
        "flashcards": cards,
    }
    return result, route


# Phrases that only appear when the AI confused itself mid-answer.
# Kept narrow on purpose: normal explanations ("should be set to
# zero") must NOT match.
RED_FLAGS = ("not an option", "none of the options", "recalculat",
             "re-evaluat", "closest correct", "closest plausible",
             "needs re-evaluation")


def _problem(q):
    """Why this question should be dropped, or "" if it's fine."""
    if len(q.options) != 4:
        return f"{len(q.options)} options"
    if not 0 <= q.answer_index < 4:
        return f"answer_index {q.answer_index}"
    if len(set(o.strip().lower() for o in q.options)) < 4:
        return "duplicate options"
    expl = q.explanation.lower()
    for flag in RED_FLAGS:
        if flag in expl:
            return f"confused explanation ('{flag}')"
    return ""


# ------------------------------------------------------------ racing

def ask_ai(prompt, schema, text):
    """Text-only request for any pydantic schema (used by planner.py).

    Returns (parsed object, provider name).
    """
    shape = ("Reply with ONLY a JSON object matching this JSON schema: "
             + json.dumps(schema.model_json_schema()))
    return _race(prompt, text=text, schema=schema, shape=shape)


def _race(prompt, text=None, images=(), schema=None, shape=JSON_SHAPE):
    """Ask the providers in parallel; return (result, provider name).

    Groq only reads text, so it joins only when there are no photos.
    """
    schema = schema or Quiz
    first, later = [], []
    if not images and settings.GROQ_API_KEY:
        first.append(("Groq",
                      lambda: _groq(prompt, text, schema, shape)))
    if settings.MISTRAL_API_KEY:
        first.append(("Mistral", lambda: _mistral(
            prompt, text, images, schema, shape)))
    gem = [settings.GEMINI_MODEL] + [
        m for m in FALLBACK_MODELS if m != settings.GEMINI_MODEL]
    for i, model in enumerate(gem):
        job = (model, lambda m=model: _gemini(
            m, prompt, text, images, schema))
        (first if i == 0 else later).append(job)

    if schema is Quiz:
        # an answer with no usable questions doesn't win the race:
        # it counts as a failure, so the other providers keep going
        first = [(n, _must_have_questions(fn, n)) for n, fn in first]
        later = [(n, _must_have_questions(fn, n)) for n, fn in later]

    errors = []
    for round_no in range(2):
        if round_no:
            time.sleep(3)
            first, later = first + later, []   # 2nd round: everyone
        result = _run(first, later, errors)
        if result:
            return result
        codes = {getattr(e, "code", None) for _, e in errors}
        if codes and codes <= {400, 401, 403}:
            raise QuizError("The AI providers rejected the request. "
                            "Check the API keys in .env.")
    raise QuizError("All AI providers are busy right now. "
                    "Press Try again in a minute.")


def _must_have_questions(fn, name):
    def wrapped():
        quiz = fn()
        ok = [q for q in quiz.questions if not _problem(q)]
        if not ok:
            reasons = [_problem(q) for q in quiz.questions]
            print(f"[check] {name} gave {len(quiz.questions)} questions, "
                  f"none usable {reasons[:3]}")
            raise ValueError("no usable questions")
        return quiz
    return wrapped


def _run(first, later, errors):
    pool = ThreadPoolExecutor(max_workers=len(first) + len(later))
    pending = {pool.submit(fn): name for name, fn in first}
    start = time.time()
    try:
        while pending or later:
            waited = time.time() - start
            if later and (waited >= HEDGE_AFTER or not pending):
                print("[race] no answer yet, adding backup models")
                for name, fn in later:
                    pending[pool.submit(fn)] = name
                later = []
            timeout = max(0, HEDGE_AFTER - waited) if later else None
            done, _ = wait(pending, timeout=timeout,
                           return_when=FIRST_COMPLETED)
            for job in done:
                name = pending.pop(job)
                try:
                    quiz = job.result()
                    print(f"[race] winner: {name} "
                          f"after {time.time() - start:.1f}s")
                    return quiz, name
                except Exception as e:
                    print(f"[race] {name} failed: {_short(e)}")
                    errors.append((name, e))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return None


def _short(e):
    return f"{getattr(e, 'code', '')} {e.__class__.__name__}".strip()


class HTTPStatusError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code


def _post_json(url, key, body, timeout=40):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "studyapp/1.0",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPStatusError(e.code, e.read()[:200]) from e


def _parse(reply, schema):
    obj = json.loads(reply)
    if schema is Quiz:
        obj.setdefault("topic", "Quiz")
        obj.setdefault("flashcards", [])
        for q in obj.get("questions", []):
            q.setdefault("concept", "General")
    return schema.model_validate(obj)


# ---------------------------------------------------------- providers

def _gemini(model, prompt, text, images, schema):
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    contents = [prompt]
    if text:
        contents.append("STUDY MATERIAL (text pages):\n" + text)
    for img in images:
        contents.append(
            types.Part.from_bytes(data=img, mime_type="image/jpeg"))
    for thinking in (types.ThinkingConfig(thinking_level="LOW"), None):
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.4,
            thinking_config=thinking,
            automatic_function_calling=types.
            AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            resp = client.models.generate_content(
                model=model, contents=contents, config=config)
        except genai_errors.APIError as e:
            if (e.code == 400 and thinking is not None
                    and "think" in str(e).lower()):
                continue          # retry without thinking
            raise
        result = resp.parsed
        if result is None:
            result = schema.model_validate_json(resp.text)
        return result


def _groq(prompt, text, schema, shape):
    messages = [
        {"role": "system", "content": prompt + " " + shape},
        {"role": "user", "content": "STUDY MATERIAL:\n" + text},
    ]
    last = None
    for model in GROQ_MODELS:
        body = {"model": model, "messages": messages,
                "temperature": 0.4,
                "response_format": {"type": "json_object"}}
        try:
            data = _post_json(GROQ_URL, settings.GROQ_API_KEY, body, 20)
            return _parse(data["choices"][0]["message"]["content"],
                          schema)
        except Exception as e:
            last = e
    raise last


def _mistral(prompt, text, images, schema, shape):
    if images:
        intro = "Make the quiz from this material."
        if text:
            intro += "\nSTUDY MATERIAL (text pages):\n" + text
        content = [{"type": "text", "text": intro}]
        for img in images:
            b64 = base64.b64encode(img).decode()
            url = f"data:image/jpeg;base64,{b64}"
            content.append({"type": "image_url", "image_url": url})
    else:
        content = "STUDY MATERIAL:\n" + text
    messages = [
        {"role": "system", "content": prompt + " " + shape},
        {"role": "user", "content": content},
    ]
    last = None
    for model in MISTRAL_MODELS:
        body = {"model": model, "messages": messages,
                "temperature": 0.4,
                "response_format": {"type": "json_object"}}
        try:
            data = _post_json(MISTRAL_URL, settings.MISTRAL_API_KEY,
                              body)
            return _parse(data["choices"][0]["message"]["content"],
                          schema)
        except Exception as e:
            last = e
    raise last


def _jpeg(path):
    buf = io.BytesIO()
    _open(path, 1600).save(buf, "JPEG", quality=90)
    return buf.getvalue()