from __future__ import annotations

"""
Cascade Job Evaluator for Open Job Loop.

Implements a 3-tier cascade evaluation architecture for extreme efficiency:
- Tier 0: DuckDB VSS Cosine Similarity Pre-check (0ms LLM)
- Tier 1: Fast Gatekeeper using ultra-lightweight LLM (llama3.2:1b, ~150ms)
- Tier 2: Deep Technical Evaluation using Qwen 2.5 Coder (3b or 7b) via Instructor
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import duckdb
from openai import AsyncOpenAI

from src.application.use_cases.profile import compute_mock_embedding
from src.domain.models import CandidateProfile, MatchEvaluation, Recommendation
from src.infrastructure.adapters.llm_evaluator import (
    JobFitEvaluator,
    format_candidate_profile,
)

logger = logging.getLogger(__name__)

DEFAULT_FAST_MODEL = "llama3.2:1b"
DEFAULT_DEEP_MODEL = "qwen2.5-coder:3b"
DEEP_HIGH_PRECISION_MODEL = "qwen2.5-coder:7b"


class CascadeJobEvaluator(JobFitEvaluator):
    """
    3-Tier Cascade Job Evaluator:
    1. Tier 0: DuckDB VSS semantic distance pre-check.
    2. Tier 1: Fast binary triage with llama3.2:1b.
    3. Tier 2: Comprehensive technical scoring with qwen2.5-coder:3b (or 7b).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model_fast: str = DEFAULT_FAST_MODEL,
        model_deep: str = DEFAULT_DEEP_MODEL,
        api_key: str = "ollama",
        timeout: float = 30.0,
        fast_timeout: float = 8.0,
        score_threshold: int = 60,
        vss_min_similarity: float = 0.35,
        db_path: Optional[str] = None,
        enable_vss_prefilter: bool = True,
        enable_fast_gatekeeper: bool = True,
        mock_mode: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            model=model_deep,
            api_key=api_key,
            timeout=timeout,
            score_threshold=score_threshold,
            **kwargs,
        )
        self.model_fast = model_fast
        self.model_deep = model_deep
        self.fast_timeout = fast_timeout
        self.vss_min_similarity = vss_min_similarity
        self.db_path = db_path
        self.enable_vss_prefilter = enable_vss_prefilter
        self.enable_fast_gatekeeper = enable_fast_gatekeeper
        self.mock_mode = mock_mode

        self.fast_raw_client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)

    async def evaluate_fit(
        self,
        job_description: str,
        candidate_profile: str | CandidateProfile | dict[str, Any],
    ) -> MatchEvaluation:
        """
        Evaluate candidate fit using 3-tier cascade execution.
        """
        if not job_description or not job_description.strip():
            raise ValueError("job_description cannot be empty or whitespace-only")

        formatted_profile = format_candidate_profile(candidate_profile)
        if not formatted_profile or not formatted_profile.strip():
            raise ValueError("candidate_profile cannot be empty or whitespace-only")

        # ----------------------------------------------------------------------
        # Tier 0: DuckDB VSS Vector Pre-Check (0ms LLM)
        # ----------------------------------------------------------------------
        if (
            self.enable_vss_prefilter
            and self.db_path
            and (self.mock_mode or Path(self.db_path).exists())
        ):
            vss_score = await self._check_vss_similarity(job_description)
            if vss_score is not None and vss_score < self.vss_min_similarity:
                logger.info(
                    "Tier 0 (VSS) discarded job: similarity %.2f < threshold %.2f",
                    vss_score,
                    self.vss_min_similarity,
                )
                return MatchEvaluation(
                    fit_score=max(0, int(vss_score * 50)),
                    recommendation=Recommendation.DISCARD,
                    reasoning=f"[Tier 0 VSS] Descarte preliminar: similaridade semântica {vss_score:.2f} abaixo do limiar {self.vss_min_similarity:.2f}.",
                    matched_skills=[],
                    missing_skills=["Escopo fora do perfil"],
                )

        # ----------------------------------------------------------------------
        # Tier 1: Fast Gatekeeper (llama3.2:1b, ~150ms)
        # ----------------------------------------------------------------------
        if self.enable_fast_gatekeeper and self.model_fast:
            gatekeeper_pass, gatekeeper_reason = await self._run_fast_gatekeeper(
                job_description=job_description,
                candidate_profile=candidate_profile,
            )
            if not gatekeeper_pass:
                logger.info(
                    "Tier 1 (Gatekeeper %s) discarded job: %s",
                    self.model_fast,
                    gatekeeper_reason,
                )
                return MatchEvaluation(
                    fit_score=35,
                    recommendation=Recommendation.DISCARD,
                    reasoning=f"[Tier 1 Gatekeeper {self.model_fast}] Descarte rápido: {gatekeeper_reason}",
                    matched_skills=[],
                    missing_skills=[],
                )

        # ----------------------------------------------------------------------
        # Tier 2: Deep Technical Evaluation (Qwen 2.5 Coder via Instructor)
        # ----------------------------------------------------------------------
        logger.info(
            "Advancing job to Tier 2 (Deep Evaluator %s)...",
            self.model,
        )
        return await super().evaluate_fit(
            job_description=job_description,
            candidate_profile=candidate_profile,
        )

    async def _check_vss_similarity(self, job_description: str) -> Optional[float]:
        """Query DuckDB VSS for semantic proximity between job and candidate profile."""
        try:
            snippet = job_description[:1000]
            if self.mock_mode:
                emb = compute_mock_embedding(snippet)
            else:
                resp = await self.fast_raw_client.embeddings.create(
                    model="nomic-embed-text",
                    input=snippet,
                )
                emb = resp.data[0].embedding

            conn = duckdb.connect(self.db_path, read_only=True)
            try:
                conn.execute("INSTALL vss; LOAD vss;")
                res = conn.execute(
                    """
                    SELECT 1.0 - array_cosine_distance(embedding, ?::FLOAT[768]) as sim
                    FROM user_profile
                    WHERE embedding IS NOT NULL
                    ORDER BY sim DESC
                    LIMIT 1;
                    """,
                    [emb],
                ).fetchone()
                if res and res[0] is not None:
                    return float(res[0])
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("VSS pre-check skipped due to error: %s", exc)
        return None

    async def _run_fast_gatekeeper(
        self,
        job_description: str,
        candidate_profile: str | CandidateProfile | dict[str, Any],
    ) -> tuple[bool, str]:
        """
        Fast binary triage prompt with minimal token overhead (~15 tokens output).
        """
        if isinstance(candidate_profile, CandidateProfile):
            target_role = candidate_profile.target_role
            skills = ", ".join(candidate_profile.primary_skills[:6])
            seniority = f"{candidate_profile.years_experience} years"
        elif isinstance(candidate_profile, dict):
            target_role = candidate_profile.get("target_role", "Software Engineer")
            skills = ", ".join(candidate_profile.get("primary_skills", [])[:6])
            seniority = f"{candidate_profile.get('years_experience', 5)} years"
        else:
            target_role = "Candidate Profile"
            skills = ""
            seniority = ""

        # Use first 1500 chars to cover title, intro, and key qualifications
        snippet = job_description[:1500].strip()

        prompt = (
            f"You are a fast job triage gatekeeper.\n"
            f"Candidate: Role='{target_role}', Experience='{seniority}', Core Skills='{skills}'.\n\n"
            f"Job Posting:\n{snippet}\n\n"
            f"Is this job posting a plausible match for candidate's field and seniority level?\n"
            f"If it is in a completely different field (e.g. Sales, Marketing, HR, Finance) or mismatched seniority (e.g. Intern), answer 'NO: <brief reason>'.\n"
            f"Otherwise, answer 'YES: <brief reason>'.\n"
            f"Answer:"
        )

        try:
            async with asyncio.timeout(self.fast_timeout):
                resp = await self.fast_raw_client.chat.completions.create(
                    model=self.model_fast,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a concise triage classifier. Output only YES: reason or NO: reason.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=35,
                    temperature=0.0,
                )
                output = resp.choices[0].message.content or ""
                output = output.strip()
                if output.upper().startswith("NO"):
                    reason = output[3:].strip(" :,-") or "Mismatched role or domain"
                    return False, reason
                return True, output[4:].strip(" :,-") or "Plausible technical fit"
        except Exception as exc:
            logger.warning("Fast gatekeeper fallback to pass due to exception: %s", exc)
            # Fail-open so no viable candidates are lost on transient network glitches
            return True, f"Gatekeeper bypassed: {exc}"
