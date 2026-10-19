from __future__ import annotations
"""
Active Learning module for Open Job Loop.

Detects low confidence or unmapped ATS form questions in real time,
prompts the user via CLI or callback, and persists verified answers
to DuckDB VSS for continuous learning across applications.
"""

import logging
from typing import Any, Callable, Dict, Optional
from rich.console import Console
from rich.prompt import Prompt

from src.application.use_cases.profile import ProfileManager

logger = logging.getLogger(__name__)


class ActiveLearningService:
    """
    Active Learning coordinator for screening questions and form inputs.
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        min_confidence: float = 0.70,
        interactive: bool = True,
        input_fn: Optional[Callable[[str], str]] = None,
        console: Optional[Console] = None,
    ) -> None:
        self.profile_manager = profile_manager
        self.min_confidence = min_confidence
        self.interactive = interactive
        self.input_fn = input_fn
        self.console = console or Console()

    async def get_or_learn_answer(
        self,
        question: str,
        job_context: Optional[Dict[str, Any]] = None,
        field_type: str = "text",
    ) -> str:
        """
        Synthesizes an answer using ProfileManager RAG.
        If confidence < min_confidence, triggers Active Learning to ask the candidate,
        indexes the answer into DuckDB VSS, and returns the learned answer.
        """
        synthesized_ans, confidence = await self.profile_manager.synthesize_answer_with_confidence(
            question=question,
            job_context=job_context,
        )

        logger.debug(
            f"ActiveLearning check for question '{question}': confidence={confidence:.2f} (threshold={self.min_confidence:.2f})"
        )

        # High confidence match -> return directly
        if confidence >= self.min_confidence and synthesized_ans != "N/A":
            return synthesized_ans

        # Low confidence or missing -> trigger Active Learning
        logger.info(
            f"⚠️ Baixa confiança ({confidence:.2f}) na resposta para: '{question}'"
        )

        import sys
        if not self.input_fn and (not self.interactive or not sys.stdin.isatty()):
            logger.info("Modo não-interativo ou non-TTY ativo: utilizando melhor inferência disponível.")
            return synthesized_ans if synthesized_ans != "N/A" else "Yes"

        # Ask candidate
        prompt_msg = (
            f"\n[bold yellow]❓ Pergunta inédita do ATS:[/bold yellow] [white]{question}[/white]\n"
            f"[dim]Sugestão inicial do LLM: '{synthesized_ans}'[/dim]\n"
            f"Sua resposta (ou pressione Enter para aceitar a sugestão): "
        )

        user_answer = ""
        if self.input_fn:
            user_answer = self.input_fn(question).strip()
        else:
            try:
                self.console.print(prompt_msg, end="")
                user_answer = input().strip()
            except (EOFError, KeyboardInterrupt, OSError, Exception):
                user_answer = ""

        final_answer = user_answer if user_answer else synthesized_ans
        if final_answer == "N/A":
            final_answer = "Yes"

        # If candidate provided a new verified answer, persist to DuckDB VSS
        if user_answer:
            try:
                chunk_content = f"Question: {question}\nAnswer: {user_answer}"
                await self.profile_manager.add_chunk(
                    category="ActiveLearning",
                    content=chunk_content,
                )
                self.console.print(
                    f"[green]✔ Resposta salva no perfil para futuras candidaturas:[/green] [white]{user_answer}[/white]"
                )
            except Exception as e:
                logger.error(f"Erro ao salvar resposta no DuckDB VSS: {e}")

        return final_answer
