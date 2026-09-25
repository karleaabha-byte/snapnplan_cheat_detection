"""Exam mode models. Append to quiz/models.py, then:

    python manage.py makemigrations quiz
    python manage.py migrate

Note what is NOT stored: no frames, no video, no screenshots. Only the times
at which the classifier saw something, and what mode was in force. That is a
deliberate design choice and worth saying out loud in the write-up - it is the
difference between a monitoring tool and a surveillance archive.
"""
from django.db import models


class ExamSession(models.Model):
    """One sitting, created by an invigilator, joined by a short code."""

    MODES = [
        ("monitoring", "Monitoring"),
        ("permitted", "Lookup permitted"),
        ("paused", "Paused"),
    ]

    code = models.CharField(max_length=8, unique=True, db_index=True)
    title = models.CharField(max_length=120, default="Midterm")
    minutes = models.PositiveIntegerField(default=30)

    # Questions from your existing make_quiz() output, stored as-is.
    questions = models.JSONField(default=list, blank=True)

    mode = models.CharField(max_length=12, choices=MODES, default="monitoring")
    mode_reason = models.CharField(max_length=200, blank=True)

    owner_key = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.code})"


class ExamAttempt(models.Model):
    """One student's sitting."""

    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE,
                                related_name="attempts")
    name = models.CharField(max_length=60)

    # Per-student override. Blank means "follow the room".
    mode = models.CharField(max_length=12, blank=True, default="")
    mode_reason = models.CharField(max_length=200, blank=True)

    state = models.CharField(max_length=12, default="ok")
    last_seen = models.DateTimeField(null=True, blank=True)

    answers = models.JSONField(default=list, blank=True)
    score = models.IntegerField(default=0)

    # The proctor's output: a list of {type, seconds, mode, at, elapsed}
    events = models.JSONField(default=list, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    flag_count = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} - {self.session.code}"

    @property
    def flagged_seconds(self):
        return sum(e.get("seconds", 0) for e in self.events
                   if e.get("type") == "other_window_visible")

    @property
    def permitted_seconds(self):
        return sum(e.get("seconds", 0) for e in self.events
                   if e.get("type") == "permitted_window_visible")
