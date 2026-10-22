"""Naukri screener (chatbot apply) form resolver.

The resolver is split in two layers:

* pure answer resolution (:func:`resolve_answer`) - no browser required, fully
  unit tested, driven by ``config/profile_data.json`` and the self-training
  ``QuestionMemory``;
* a thin Playwright adapter (:class:`FormResolver`) that reads the modal DOM,
  applies the answer and remembers what it learned.

Anything the bot cannot answer is written to ``config/qa_memory.json`` with
status ``pending`` so the user can fill it in once.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .logging_setup import get_logger
from .qa_store import QuestionEntry, QuestionMemory, normalize_question

logger = get_logger("forms")

TRUTHY = {"yes", "y", "true", "1", "ok", "sure", "available", "willing"}
FALSY = {"no", "n", "false", "0"}
YES_NO_OPTIONS = ("Yes", "No")
FIELD_TEXT = "text"
FIELD_NUMBER = "number"
FIELD_TEXTAREA = "textarea"
FIELD_SELECT = "select"
FIELD_RADIO = "radio"
FIELD_CHECKBOX = "checkbox"

QUESTION_SELECTORS = (
    ".question-wrapper, .bot-form-item, div[class*='question']"
)
LABEL_SELECTORS = "label, .label-text, span[class*='title']"
TEXT_INPUT_SELECTORS = (
    "input[type='text'], input[type='number'], input:not([type]), textarea"
)


@dataclass
class FieldSpec:
    """A screen question as understood by the answer engine."""

    question: str
    kind: str = FIELD_TEXT
    options: List[str] = field(default_factory=list)
    mandatory: bool = False

    @property
    def normalized(self) -> str:
        return normalize_question(self.question)


@dataclass
class Resolution:
    """Result of filling a screener form."""

    ok: bool
    answered: List[Dict[str, str]] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)
    error: str = ""

    def first_unresolved(self) -> str:
        return self.unresolved[0] if self.unresolved else self.error or "unknown question"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = _format_value(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return None


def _numbers_in(text: str) -> List[float]:
    return [float(m) for m in re.findall(r"\d+(?:\.\d+)?", text or "")]


def match_option(answer: Any, options: Sequence[str]) -> Optional[str]:
    """Pick the option that best represents ``answer``.

    Handles exact text, Yes/No aliases, and numeric ranges such as a notice
    period of 75 days against ``["Immediate", "30 Days", "60 Days", "90 Days"]``.
    """
    if not options:
        return None
    value = _format_value(answer)
    if not value:
        return None

    lowered = [str(o).strip().lower() for o in options]

    # 1. Exact / case-insensitive match.
    for option, option_lower in zip(options, lowered):
        if option_lower == value.lower():
            return str(option)

    # 2. Yes/No aliases.
    boolean = _as_bool(value)
    if boolean is not None:
        wanted = {"yes", "true", "1"} if boolean else {"no", "false", "0"}
        for option, option_lower in zip(options, lowered):
            if option_lower in wanted:
                return str(option)

    # 3. Numbers: choose the closest option, preferring one that covers the value.
    value_numbers = _numbers_in(value)
    if value_numbers:
        target = value_numbers[0]
        numeric_options: List[Tuple[float, str]] = []
        for option in options:
            numbers = _numbers_in(str(option))
            if numbers:
                numeric_options.append((numbers[0], str(option)))
        if numeric_options:
            exact = [opt for num, opt in numeric_options if num == target]
            if exact:
                return exact[0]
            covering = [(num, opt) for num, opt in numeric_options if num >= target]
            if covering:
                return min(covering, key=lambda item: item[0])[1]
            return min(numeric_options, key=lambda item: abs(item[0] - target))[1]

    # 4. Substring containment.
    for option, option_lower in zip(options, lowered):
        if value.lower() in option_lower or option_lower in value.lower():
            return str(option)

    # 5. Fuzzy match.
    closest = difflib.get_close_matches(value.lower(), lowered, n=1, cutoff=0.8)
    if closest:
        return str(options[lowered.index(closest[0])])
    return None


def _tokens_match(key: str, normalized_question: str) -> bool:
    """True when every word of ``key`` appears as a whole word in the question."""
    tokens = [t for t in key.split() if len(t) >= 4]
    if not tokens:
        return False
    return all(
        re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", normalized_question)
        for token in tokens
    )


def _custom_answer(question: str, profile: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Match ``custom_answers`` keys by exact text, substring or whole words."""
    custom = profile.get("custom_answers") or {}
    if not isinstance(custom, dict):
        return None
    normalized = normalize_question(question)
    best: Optional[Tuple[float, str, str]] = None
    for raw_question, raw_answer in custom.items():
        if raw_answer in (None, ""):
            continue
        key = normalize_question(str(raw_question))
        if not key:
            continue
        if key == normalized:
            return str(raw_answer), "profile"
        if len(key) >= 8 and (key in normalized or normalized in key):
            return str(raw_answer), "profile"
        if _tokens_match(key, normalized):
            return str(raw_answer), "profile"
        score = difflib.SequenceMatcher(None, key, normalized).ratio()
        if score >= 0.88 and (best is None or score > best[0]):
            best = (score, str(raw_answer), "profile")
    if best:
        return best[1], best[2]
    return None


def _skill_answer(question: str, profile: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Answer "years of experience in <skill>" questions."""
    skills: Dict[str, Any] = profile.get("skills_experience") or {}
    if not skills:
        return None
    normalized = normalize_question(question)
    if not re.search(r"(experience|expertise|knowledge|proficiency|worked|hands.?on)", normalized):
        return None

    best_skill: Optional[str] = None
    best_score = 0.0
    for skill in skills:
        skill_norm = normalize_question(str(skill))
        if not skill_norm:
            continue
        if re.search(r"\b" + re.escape(skill_norm) + r"\b", normalized):
            return _format_value(skills[skill]), "skill"
        score = difflib.SequenceMatcher(None, skill_norm, normalized).ratio()
        if score > best_score:
            best_score, best_skill = score, skill

    # The question is usually much longer than the skill name; look at the
    # trailing fragment ("... experience in spring boot?") instead.
    tail = re.split(r"(?:in|with|of|on)\s+", normalized)[-1]
    for skill in skills:
        skill_norm = normalize_question(str(skill))
        score = difflib.SequenceMatcher(None, skill_norm, tail).ratio()
        if score >= 0.75 and score > best_score:
            best_score, best_skill = score, skill
    if best_skill and best_score >= 0.75:
        return _format_value(skills[best_skill]), "skill"
    return None


def _rule_answer(question: str, profile: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Apply built-in rules derived from ``profile_data.json``."""
    q = normalize_question(question)

    def profile_value(key: str, default: Any = None) -> Any:
        return profile.get(key, default)

    if re.search(r"(current|present).*(ctc|salary|compensation)|fixed.*ctc", q):
        value = profile_value("current_ctc_lpa")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"(expected|desired).*(ctc|salary|compensation)", q):
        value = profile_value("expected_ctc_lpa")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"notice period|joining time|how soon.*join|available to join", q):
        value = profile_value("notice_period_days")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"relocat|willing to move|open to move", q):
        value = profile_value("willing_to_relocate")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"shift|night shift|rotational", q):
        value = profile_value("willing_to_work_shifts")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"\btravel\b|onsite|on-site", q):
        value = profile_value("willing_to_travel")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"current (city|location)|present (city|location)|where.*based", q):
        value = profile_value("current_city")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"gender|sex\b", q):
        value = profile_value("gender")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"(highest )?(qualification|degree|education)", q):
        value = profile_value("highest_qualification")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"(graduation|passing|pass out).*(year)|year of (graduation|passing)", q):
        value = profile_value("graduation_year")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"date of birth|birth date|\bdob\b", q):
        value = profile_value("date_of_birth")
        if value is not None:
            return _format_value(value), "rule"
    if re.search(r"current (designation|title|role)|job title", q):
        value = profile_value("current_designation")
        if value is not None:
            return _format_value(value), "rule"

    skill_result = _skill_answer(question, profile)
    if skill_result:
        return skill_result

    if re.search(r"total.*(experience|exp)\b|years? of experience|work experience", q):
        value = profile_value("total_experience_years")
        if value is not None:
            return _format_value(value), "rule"

    return None


def resolve_answer(
    spec: FieldSpec,
    profile: Dict[str, Any],
    memory: Optional[QuestionMemory] = None,
) -> Optional[Tuple[str, str]]:
    """Return ``(answer, source)`` or ``None`` when the question is unknown.

    Resolution order: hand-written ``custom_answers`` -> learned memory ->
    built-in rules. Choice fields only accept answers that map to an option.
    """
    candidates: List[Tuple[str, str]] = []
    custom = _custom_answer(spec.question, profile)
    if custom:
        candidates.append(custom)
    if memory is not None:
        entry: Optional[QuestionEntry] = memory.lookup(spec.question)
        if entry and entry.answer not in (None, ""):
            candidates.append((str(entry.answer), "memory"))
    rule = _rule_answer(spec.question, profile)
    if rule:
        candidates.append(rule)

    for answer, source in candidates:
        if spec.kind in (FIELD_SELECT, FIELD_RADIO):
            option = match_option(answer, spec.options)
            if option is not None:
                return option, source
            logger.debug(
                "Answer %r from %s has no matching option for %r (%s)",
                answer,
                source,
                spec.question,
                spec.options,
            )
            continue
        if spec.kind == FIELD_NUMBER:
            numbers = _numbers_in(answer)
            if numbers:
                return _format_value(numbers[0]), source
            continue
        return answer, source
    return None


# ---------------------------------------------------------------------------
# Playwright adapter
# ---------------------------------------------------------------------------
class FormResolver:
    """Reads the screener modal, fills answers and trains ``QuestionMemory``."""

    def __init__(self, profile_data: Dict[str, Any], memory: Optional[QuestionMemory] = None):
        self.profile = profile_data
        self.memory = memory if memory is not None else QuestionMemory()
        self.autolearn = bool(profile_data.get("qa_memory_autolearn", True))

    # -- DOM inspection -----------------------------------------------------
    def _read_fields(self, modal) -> List[Tuple[Any, FieldSpec]]:
        blocks = modal.locator(QUESTION_SELECTORS).all()
        fields: List[Tuple[Any, FieldSpec]] = []
        seen: set[str] = set()

        for block in blocks:
            question = self._read_question(block)
            if not question:
                continue
            normalized = normalize_question(question)
            if normalized in seen:
                continue
            seen.add(normalized)

            kind, options = self._describe_field(block, question)
            mandatory = "*" in question or block.locator("[required]").count() > 0
            fields.append(
                (block, FieldSpec(question=question, kind=kind, options=options, mandatory=mandatory))
            )
        return fields

    @staticmethod
    def _read_question(block) -> str:
        """First label/span that is not itself a control label (Yes/No etc.)."""
        labels = block.locator(LABEL_SELECTORS)
        try:
            count = labels.count()
        except Exception:
            return ""
        for index in range(count):
            candidate = labels.nth(index)
            try:
                if candidate.locator("input, select, textarea").count() > 0:
                    continue
                text = candidate.inner_text().strip()
                if text:
                    return text
            except Exception:
                continue
        try:
            return labels.first.inner_text().strip()
        except Exception:
            return ""

    @classmethod
    def _describe_field(cls, block, question: str = "") -> Tuple[str, List[str]]:
        select = block.locator("select")
        if select.count() > 0:
            try:
                options = [o.strip() for o in select.first.locator("option").all_inner_texts() if o.strip()]
            except Exception:
                options = []
            return FIELD_SELECT, options

        radios = block.locator("input[type='radio']")
        if radios.count() > 0:
            options: List[str] = []
            try:
                options = [t.strip() for t in block.locator("label").all_inner_texts() if t.strip()]
            except Exception:
                pass
            if not options:
                try:
                    options = [
                        (radios.nth(i).get_attribute("value") or "").strip()
                        for i in range(min(radios.count(), 10))
                    ]
                except Exception:
                    options = []
            question_norm = normalize_question(question)
            options = [o for o in options if o and normalize_question(o) != question_norm]
            return FIELD_RADIO, options

        checkboxes = block.locator("input[type='checkbox']")
        if checkboxes.count() > 0:
            return FIELD_CHECKBOX, list(YES_NO_OPTIONS)

        if block.locator("textarea").count() > 0:
            return FIELD_TEXTAREA, []

        number_input = block.locator("input[type='number']")
        if number_input.count() > 0:
            return FIELD_NUMBER, []

        if block.locator(TEXT_INPUT_SELECTORS).count() > 0:
            return FIELD_TEXT, []

        return FIELD_TEXT, []

    # -- DOM interaction ----------------------------------------------------
    @staticmethod
    def _fill(block, field: FieldSpec, answer: str) -> bool:
        try:
            if field.kind == FIELD_SELECT:
                select = block.locator("select").first
                try:
                    select.select_option(label=answer)
                except Exception:
                    select.select_option(value=answer)
                return True

            if field.kind == FIELD_RADIO:
                labels = block.locator("label")
                for index in range(labels.count()):
                    candidate = labels.nth(index)
                    if candidate.inner_text().strip().lower() == answer.strip().lower():
                        candidate.click()
                        return True
                radios = block.locator("input[type='radio']")
                for index in range(radios.count()):
                    if (radios.nth(index).get_attribute("value") or "").strip().lower() == answer.lower():
                        radios.nth(index).check()
                        return True
                return False

            if field.kind == FIELD_CHECKBOX:
                checkbox = block.locator("input[type='checkbox']").first
                if _as_bool(answer) is False:
                    if checkbox.is_checked():
                        checkbox.uncheck()
                else:
                    checkbox.check()
                return True

            target = block.locator(TEXT_INPUT_SELECTORS).first
            target.fill(answer)
            return True
        except Exception as exc:  # pragma: no cover - browser dependent
            logger.warning("Could not fill %r (%s): %s", field.question, field.kind, exc)
            return False

    # -- public API ---------------------------------------------------------
    def resolve_and_fill(
        self,
        modal,
        company: str = "",
        job_title: str = "",
    ) -> Resolution:
        """Fill every readable question; return what was answered/unresolved."""
        result = Resolution(ok=False)
        try:
            fields = self._read_fields(modal)
        except Exception as exc:  # pragma: no cover - browser dependent
            logger.exception("Failed to read screener questions: %s", exc)
            result.error = f"Could not read screener: {exc}"
            return result

        if not fields:
            result.ok = True
            logger.debug("Screener modal has no recognised questions.")
            return result

        logger.info("Screener contains %d question(s); resolving...", len(fields))

        for block, spec in fields:
            resolved = resolve_answer(spec, self.profile, self.memory)
            if resolved is None:
                self.memory.record_question(
                    spec.question,
                    field_kind=spec.kind,
                    options=spec.options,
                    company=company,
                    job_title=job_title,
                )
                if spec.mandatory:
                    logger.warning("Unanswered mandatory question: %r", spec.question)
                    result.unresolved.append(spec.question)
                else:
                    logger.info("Skipping optional unknown question: %r", spec.question)
                continue

            answer, source = resolved
            if not self._fill(block, spec, answer):
                if spec.mandatory:
                    logger.warning("Could not apply answer to %r", spec.question)
                    result.unresolved.append(spec.question)
                continue

            result.answered.append({"question": spec.question, "answer": answer, "source": source})
            logger.info("Answered %r -> %r (%s)", spec.question, answer, source)

            if source == "memory":
                self.memory.mark_used(spec.question)
            elif self.autolearn and source in {"rule", "skill"}:
                # Auto-train: remember the concrete wording and answer so the
                # next occurrence is answered without re-running the rules.
                self.memory.learn(
                    spec.question,
                    answer,
                    source="auto",
                    field_kind=spec.kind,
                    options=spec.options,
                    company=company,
                    job_title=job_title,
                )

        result.ok = not result.unresolved
        self.memory.maybe_save()
        return result


def load_profile_with_answers(path: str) -> Dict[str, Any]:
    """Load profile JSON, tolerating a missing/invalid file for diagnostics."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
