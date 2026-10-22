"""Job relevance scoring.

Matching runs twice: cheaply on the search-results card (title + experience)
and again after the detail page opens when the JD is available. The second pass
catches jobs whose title looks fine but whose description is irrelevant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

from .logging_setup import get_logger

logger = get_logger("matcher")


@dataclass
class MatchResult:
    matched: bool
    reason: str
    score: int = 0

    def __bool__(self) -> bool:  # allows ``if result:`` in older call sites
        return self.matched


class JobMatcher:
    def __init__(self, config: Dict[str, Any]):
        experience = config.get("experience_filter") or {}
        self.min_user_exp = float(experience.get("min_years", 0))
        self.max_user_exp = float(experience.get("max_years", 99))
        self.mandatory = [str(k).lower() for k in config.get("mandatory_keywords", []) if str(k).strip()]
        self.preferred = [str(k).lower() for k in config.get("preferred_keywords", []) if str(k).strip()]
        self.blacklist = [str(k).lower() for k in config.get("blacklist_keywords", []) if str(k).strip()]
        self.blacklist_title_only = bool(config.get("blacklist_title_only", False))
        self.title_keywords = [str(k).lower() for k in config.get("title_keywords", []) if str(k).strip()]
        self.require_all_mandatory = bool(config.get("require_all_mandatory", False))
        self.min_score = int(config.get("minimum_match_score", 0))

    # ------------------------------------------------------------------
    def parse_experience(self, exp_text: str) -> Tuple[float, float]:
        """Parse Naukri strings: '1-3 Yrs', '0-2 Yrs', '3+ Yrs', 'Fresher'."""
        text = (exp_text or "").lower()
        if not text or "not disclosed" in text:
            return (0.0, 99.0)
        if "fresher" in text:
            return (0.0, 1.0)
        numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
        if not numbers:
            return (0.0, 99.0)
        if len(numbers) >= 2:
            return (min(numbers[0], numbers[1]), max(numbers[0], numbers[1]))
        if "+" in text:
            return (numbers[0], 99.0)
        return (numbers[0], numbers[0] + 2.0)

    @staticmethod
    def _pattern(keyword: str) -> re.Pattern[str]:
        # Lookarounds instead of \b so tokens like ".net", "c#" and "node.js"
        # still match without "java" matching "javascript".
        return re.compile(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])")

    @classmethod
    def _hits(cls, content: str, keywords: Iterable[str]) -> List[str]:
        return [k for k in keywords if cls._pattern(k).search(content)]

    # ------------------------------------------------------------------
    def evaluate_job(
        self,
        title: str,
        exp_text: str,
        jd_snippet: str = "",
        location: str = "",
    ) -> MatchResult:
        """Score a job and return a human-readable match decision."""
        title = title or ""
        title_content = title.lower()
        content = f"{title} {jd_snippet}".lower()
        blacklist_content = title_content if self.blacklist_title_only else content

        # 1. Blacklist -----------------------------------------------------
        for bad_word in self.blacklist:
            if self._pattern(bad_word).search(blacklist_content):
                return MatchResult(False, f"Contains blacklisted keyword: '{bad_word}'")

        # 2. Mandatory stack ----------------------------------------------
        if self.mandatory:
            hits = self._hits(content, self.mandatory)
            if not hits:
                return MatchResult(
                    False,
                    "Missing mandatory skills (" + ", ".join(self.mandatory[:4]) + ")",
                )
            if self.require_all_mandatory and len(hits) < len(self.mandatory):
                missing = [k for k in self.mandatory if k not in hits]
                return MatchResult(False, "Missing mandatory skills: " + ", ".join(missing))
        else:
            hits = []

        # 3. Title relevance ----------------------------------------------
        if self.title_keywords and not self._hits(title_content, self.title_keywords):
            return MatchResult(False, "Role title is unrelated to target roles")
        title_hits = self._hits(title_content, self.mandatory) if self.mandatory else []

        # 4. Experience window --------------------------------------------
        job_min, job_max = self.parse_experience(exp_text)
        if job_min > self.max_user_exp:
            return MatchResult(
                False,
                f"Job requires {job_min:g}-{job_max:g} Yrs (over your {self.max_user_exp:g} Yr limit)",
            )
        if job_max < self.min_user_exp:
            return MatchResult(
                False,
                f"Job targets {job_min:g}-{job_max:g} Yrs (below your {self.min_user_exp:g} Yr minimum)",
            )

        # 5. Score ---------------------------------------------------------
        preferred_hits = self._hits(content, self.preferred)
        score = 20
        score += 12 * len(title_hits)
        score += 8 * len(hits)
        score += 5 * len(preferred_hits)
        if job_min <= self.min_user_exp <= job_max or job_min <= self.max_user_exp <= job_max:
            score += 10
        if self.min_score and score < self.min_score:
            return MatchResult(False, f"Match score {score} is below minimum {self.min_score}", score)

        detail = []
        if hits:
            detail.append("skills: " + ", ".join(hits[:3]))
        if preferred_hits:
            detail.append("bonus: " + ", ".join(preferred_hits[:3]))
        if location:
            detail.append(f"location: {location}")
        reason = f"Match (score {score})" + (" | " + "; ".join(detail) if detail else "")
        return MatchResult(True, reason, score)
