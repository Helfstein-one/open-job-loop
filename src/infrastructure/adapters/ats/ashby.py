from __future__ import annotations

"""
Ashby ATS Adapter.
Supports jobs.ashbyhq.com forms and application workflows.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.application.use_cases.active_learning import ActiveLearningService
from src.domain.models import ApplicationChannel
from src.infrastructure.adapters.ats.base import ApplicationResult, BaseATSAdapter

logger = logging.getLogger(__name__)


class AshbyAdapter(BaseATSAdapter):
    """
    Automated form-filler adapter for Ashby job applications.
    """

    channel = ApplicationChannel.ASHBY

    def matches_url(self, url: str) -> bool:
        if not url:
            return False
        if "ashby" in url.lower():
            return True
        netloc = urlparse(url).netloc.lower()
        return "ashbyhq.com" in netloc

    async def fill_and_submit(
        self,
        page: Any,
        url: str,
        job_context: Dict[str, Any],
        active_learning: ActiveLearningService,
        resume_path: Optional[str] = None,
        dry_run: bool = False,
    ) -> ApplicationResult:
        logs: List[str] = []
        fields_filled = 0

        logs.append(f"Acessando Ashby ATS: {url}")
        logger.info(f"Navegando para vaga Ashby: {url}")

        try:
            if (
                url
                and not url.startswith("mock://")
                and (page.url != url or url.startswith("file://"))
            ):
                await page.goto(url, timeout=30000)
                await asyncio.sleep(0.5)

            # If there is an apply button that opens the form, click it
            apply_btn = page.locator(
                "button:has-text('Apply for this job'), a:has-text('Apply for this job')"
            ).first
            if await apply_btn.count() > 0 and await apply_btn.is_visible():
                await apply_btn.click()
                await asyncio.sleep(1.5)

            # 1. Candidate Name
            candidate_name = await active_learning.get_or_learn_answer(
                "Qual é o seu nome completo?", job_context
            )
            name_input = page.locator("input[name='name'], input[name='fullName'], #name").first
            first_name_input = page.locator("input[name='firstName'], #firstName").first
            last_name_input = page.locator("input[name='lastName'], #lastName").first

            if await name_input.count() > 0 and await name_input.is_visible():
                await name_input.fill(candidate_name)
                fields_filled += 1
                logs.append(f"Preenchido Name: {candidate_name}")
            elif await first_name_input.count() > 0 and await first_name_input.is_visible():
                parts = candidate_name.split(maxsplit=1)
                await first_name_input.fill(parts[0])
                fields_filled += 1
                if len(parts) > 1 and await last_name_input.count() > 0:
                    await last_name_input.fill(parts[1])
                    fields_filled += 1
                logs.append(f"Preenchido First/Last Name: {candidate_name}")

            # 2. Email
            email_input = page.locator("input[name='email'], input[type='email']").first
            if await email_input.count() > 0 and await email_input.is_visible():
                email_val = await active_learning.get_or_learn_answer(
                    "Qual é o seu e-mail?", job_context
                )
                await email_input.fill(email_val)
                fields_filled += 1
                logs.append(f"Preenchido Email: {email_val}")

            # 3. Phone
            phone_input = page.locator("input[name='phone'], input[type='tel']").first
            if await phone_input.count() > 0 and await phone_input.is_visible():
                phone_val = await active_learning.get_or_learn_answer(
                    "Qual é o seu telefone / WhatsApp?", job_context
                )
                await phone_input.fill(phone_val)
                fields_filled += 1
                logs.append(f"Preenchido Phone: {phone_val}")

            # 4. Resume upload
            if resume_path and Path(resume_path).exists():
                file_input = page.locator("input[type='file']").first
                if await file_input.count() > 0:
                    await file_input.set_input_files(str(Path(resume_path).resolve()))
                    fields_filled += 1
                    logs.append(f"Currículo anexado: {resume_path}")
                    logger.info(f"Currículo Ashby anexado com sucesso ({resume_path})")

            # 5. Dynamic Ashby questions
            ashby_fields = await page.locator("div[data-ashby-field], form div:has(label)").all()
            for field in ashby_fields:
                label_el = field.locator("label").first
                if await label_el.count() == 0:
                    continue

                label_text = (await label_el.inner_text()).strip()
                if not label_text:
                    continue

                lbl_lower = label_text.lower()
                if any(x in lbl_lower for x in ["name", "email", "phone", "resume"]):
                    continue

                input_el = field.locator("input[type='text'], textarea").first
                if await input_el.count() > 0 and await input_el.is_visible():
                    val = await input_el.input_value()
                    if not val:
                        ans = await active_learning.get_or_learn_answer(label_text, job_context)
                        await input_el.fill(str(ans))
                        fields_filled += 1
                        logs.append(f"Campo Ashby preenchido: '{label_text}' -> '{ans}'")

            # 6. Submit or Dry Run
            submit_btn = page.locator(
                "button[type='submit'], button:has-text('Submit Application')"
            ).first
            if dry_run:
                logs.append("Modo DRY-RUN ativado: formulário Ashby validado sem submissão final.")
                logger.info(
                    f"DRY-RUN Ashby concluído para {job_context.get('title')}. Total campos: {fields_filled}"
                )
                return ApplicationResult(
                    success=True,
                    channel=self.channel,
                    job_id=job_context.get("id"),
                    fields_filled=fields_filled,
                    logs=logs,
                    dry_run=True,
                )

            if await submit_btn.count() > 0 and await submit_btn.is_visible():
                await submit_btn.click()
                await asyncio.sleep(3)
                logs.append("Candidatura submetida oficialmente no Ashby.")
                return ApplicationResult(
                    success=True,
                    channel=self.channel,
                    job_id=job_context.get("id"),
                    fields_filled=fields_filled,
                    logs=logs,
                    dry_run=False,
                )
            else:
                logs.append("Botão de submissão Ashby não encontrado.")
                return ApplicationResult(
                    success=False,
                    channel=self.channel,
                    job_id=job_context.get("id"),
                    fields_filled=fields_filled,
                    logs=logs,
                    dry_run=False,
                    error_message="Submit button not found",
                )

        except Exception as e:
            err = f"Erro no preenchimento Ashby: {e}"
            logs.append(err)
            logger.error(err)
            return ApplicationResult(
                success=False,
                channel=self.channel,
                job_id=job_context.get("id"),
                fields_filled=fields_filled,
                logs=logs,
                dry_run=dry_run,
                error_message=str(e),
            )
