"""Persistent, self-training store for screener questions.

The bot applies to jobs whose Naukri screener contains questions it has never
seen. Instead of dropping those jobs silently, every unanswered mandatory
question is written to ``config/qa_memory.json`` (status ``pending``).

The user then fills in the ``answer`` value once::

    "do you have a valid passport": {
      "question": "Do you have a valid passport?",
      "answer": "Yes",
      ...
    }

On the next run the answer is reused automatically. Questions that the built-in
rules *can* answer are stored as well (status ``learned``), so wording variants
are answered instantly without re-running the rule heuristics.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from src.logging_setup import get_logger
except ImportError:  # pragma: no cover - direct module execution
    from logging_setup import get_logger  # type: ignore

logger = get_logger("qa")

DEFAULT_MEMORY_PATH = Path("config/qa_memory.json")
FUZZY_CUTOFF = 0.92
MAX_STORED_OPTIONS = 20

# Ordering words that differ between otherwise identical questions.
_NOISE_PREFIX = re.compile(
    r"^\s*(?:q(?:uestion)?\s*\d+[\).\:-]?|\d+[\).\:-])\s*", re.IGNORECASE
)
_PUNCTUATION = re.compile(r"[^\w\s+#./-]")
_WHITESPACE = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """Lowercase, de-number and squash a question so variants share a key."""
    text = (text or "").strip()
    text = _NOISE_PREFIX.sub("", text)
    text = text.replace("&", " and ")
    text = text.replace("*", " ")
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip().lower()
    return text


def question_fingerprint(text: str) -> str:
    """Stable key for a question (used as the JSON object key)."""
    normalized = normalize_question(text)
    return normalized.rstrip(" ?.:")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class QuestionEntry:
    """One remembered screener question."""

    question: str
    answer: Optional[str] = None
    field_kind: str = "text"
    options: List[str] = field(default_factory=list)
    status: str = "pending"  # pending | answered | learned
    times_asked: int = 0
    times_used: int = 0
    first_seen: str = field(default_factory=_now_iso)
    last_seen: str = field(default_factory=_now_iso)
    last_company: str = ""
    last_job: str = ""
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, question: str, data: Dict[str, Any]) -> "QuestionEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        payload.setdefault("question", question)
        options = payload.get("options") or []
        payload["options"] = [str(o) for o in options][:MAX_STORED_OPTIONS]
        return cls(**payload)


class QuestionMemory:
    """Thread-safe JSON store of screener questions and learned answers."""

    def __init__(self, path: str | os.PathLike[str] | None = None, autosave: bool = True):
        self.path = Path(path or os.getenv("QA_MEMORY_PATH", DEFAULT_MEMORY_PATH))
        self.autosave = autosave
        self._lock = threading.RLock()
        self._questions: Dict[str, QuestionEntry] = {}
        self._dirty = False
        self.load()

    # ------------------------------------------------------------------ I/O
    def load(self) -> None:
        with self._lock:
            self._questions = {}
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Could not read QA memory %s: %s", self.path, exc)
                return
            questions = raw.get("questions", raw) if isinstance(raw, dict) else {}
            if not isinstance(questions, dict):
                logger.error("QA memory %s has an invalid shape; starting empty.", self.path)
                return
            for key, value in questions.items():
                if not isinstance(value, dict):
                    continue
                entry = QuestionEntry.from_dict(str(key), value)
                self._questions[key] = entry
            logger.debug("Loaded %d remembered question(s) from %s", len(self._questions), self.path)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "updated_at": _now_iso(),
                "questions": {
                    key: entry.to_dict() for key, entry in sorted(self._questions.items())
                },
            }
            fd, tmp_name = tempfile.mkstemp(
                prefix=".qa_memory_", suffix=".json", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                os.replace(tmp_name, self.path)
            except Exception:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
            self._dirty = False

    def maybe_save(self) -> None:
        if self.autosave and self._dirty:
            self.save()

    # --------------------------------------------------------------- lookup
    def get(self, question: str) -> Optional[QuestionEntry]:
        key = question_fingerprint(question)
        with self._lock:
            entry = self._questions.get(key)
            if entry is not None:
                return entry
            if not self._questions:
                return None
            match = difflib.get_close_matches(
                key, list(self._questions.keys()), n=1, cutoff=FUZZY_CUTOFF
            )
            return self._questions[match[0]] if match else None

    def lookup(self, question: str) -> Optional[QuestionEntry]:
        """Return a remembered entry that already has an answer."""
        entry = self.get(question)
        if entry and entry.answer not in (None, ""):
            return entry
        return None

    def pending(self) -> List[QuestionEntry]:
        with self._lock:
            return [e for e in self._questions.values() if e.answer in (None, "")]

    def all_entries(self) -> List[QuestionEntry]:
        with self._lock:
            return list(self._questions.values())

    # --------------------------------------------------------------- writes
    def record_question(
        self,
        question: str,
        field_kind: str = "text",
        options: Optional[Iterable[str]] = None,
        company: str = "",
        job_title: str = "",
    ) -> QuestionEntry:
        """Remember that a question was seen (increments ``times_asked``)."""
        key = question_fingerprint(question)
        if not key:
            return QuestionEntry(question=question or "")
        with self._lock:
            entry = self._questions.get(key)
            if entry is None:
                entry = QuestionEntry(
                    question=question.strip(),
                    field_kind=field_kind or "text",
                    options=[str(o) for o in (options or [])][:MAX_STORED_OPTIONS],
                )
                self._questions[key] = entry
            entry.times_asked += 1
            entry.last_seen = _now_iso()
            if company:
                entry.last_company = company
            if job_title:
                entry.last_job = job_title
            if options and not entry.options:
                entry.options = [str(o) for o in options][:MAX_STORED_OPTIONS]
            if entry.answer in (None, "") and entry.status != "pending":
                entry.status = "pending"
            self._dirty = True
        self.maybe_save()
        return entry

    def learn(
        self,
        question: str,
        answer: Any,
        source: str = "auto",
        field_kind: str = "text",
        options: Optional[Iterable[str]] = None,
        company: str = "",
        job_title: str = "",
        overwrite: bool = False,
    ) -> Optional[QuestionEntry]:
        """Store an answer for a question.

        Auto-learned answers never overwrite a value the user supplied by hand
        unless ``overwrite`` is True.
        """
        answer_text = "" if answer is None else str(answer).strip()
        if not answer_text:
            return None
        key = question_fingerprint(question)
        if not key:
            return None
        with self._lock:
            entry = self._questions.get(key) or self.record_question(
                question,
                field_kind=field_kind,
                options=options,
                company=company,
                job_title=job_title,
            )
            if entry.answer and not overwrite and entry.status in {"answered", "learned"}:
                entry.times_used += 1
                entry.last_seen = _now_iso()
                self._dirty = True
            else:
                entry.answer = answer_text
                entry.source = source
                entry.status = "learned" if source == "auto" else "answered"
                entry.last_seen = _now_iso()
                if options and not entry.options:
                    entry.options = [str(o) for o in options][:MAX_STORED_OPTIONS]
                self._dirty = True
        self.maybe_save()
        return entry

    def mark_used(self, question: str) -> None:
        entry = self.get(question)
        if entry is None:
            return
        with self._lock:
            entry.times_used += 1
            entry.last_seen = _now_iso()
            if entry.answer not in (None, "") and entry.status == "pending":
                entry.status = "answered"
            self._dirty = True
        self.maybe_save()

    def pending_report(self) -> str:
        """Human-readable Markdown summary of questions awaiting an answer."""
        pending = sorted(self.pending(), key=lambda e: (-e.times_asked, e.question))
        if not pending:
            return ""
        lines = [
            "# Screener questions that need an answer",
            "",
            f"Generated: {_now_iso()}",
            "",
            "Add the missing `answer` values to `config/qa_memory.json`, then the",
            "bot will apply to these jobs on the next run.",
            "",
            "| Times asked | Question | Options | Last seen |",
            "| ---: | --- | --- | --- |",
        ]
        for entry in pending:
            options = ", ".join(entry.options) if entry.options else "-"
            lines.append(
                f"| {entry.times_asked} | {entry.question} | {options} | "
                f"{entry.last_company or '-'} |"
            )
        lines.append("")
        return "\n".join(lines)
