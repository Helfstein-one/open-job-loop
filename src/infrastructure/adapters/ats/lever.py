from __future__ import annotations
"""
Lever ATS Adapter.
Supports jobs.lever.co forms and application flows.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.domain.models import ApplicationChannel
from src.infrastructure.adapters.ats.base import BaseATSAdapter, ApplicationResult
from src.application.use_cases.active_learning import ActiveLearningService

logger = logging.getLogger(__name__)


class LeverAdapter(BaseATSAdapter):
    """
    Automated form-filler adapter for Lever job applications.
    """

    channel = ApplicationChannel.LEVER

    def matches_url(self, url: str) -> bool:
        if not url:
            return False
        if "lever" in url.lower():
            return True
        netloc = urlparse(url).netloc.lower()
        return "lever.co" in netloc

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

        # Ensure we are on the apply page if needed
        apply_url = url
        if not url.startswith("file://") and not url.startswith("mock://"):
            apply_url = url if url.endswith("/apply") else url.rstrip("/") + "/apply"
        logs.append(f"Acessando Lever ATS: {apply_url}")
        logger.info(f"Navegando para vaga Lever: {apply_url}")

        try:
            if url and not url.startswith("mock://") and (page.url != apply_url or url.startswith("file://")):
                await page.goto(apply_url, timeout=30000)
                await asyncio.sleep(0.5)

            # If redirected back or button "Apply for this job" exists, click it
            apply_btn = page.locator("a.postings-btn:has-text('Apply'), a:has-text('Apply for this job')").first
            if await apply_btn.count() > 0 and await apply_btn.is_visible():
                await apply_btn.click()
                await asyncio.sleep(1.5)

            # 1. Full Name
            name_input = page.locator("input[name='name']").first
            if await name_input.count() > 0 and await name_input.is_visible():
                candidate_name = await active_learning.get_or_learn_answer("Qual é o seu nome completo?", job_context)
                await name_input.fill(candidate_name)
                fields_filled += 1
                logs.append(f"Preenchido Name: {candidate_name}")

            # 2. Email
            email_input = page.locator("input[name='email']").first
            if await email_input.count() > 0 and await email_input.is_visible():
                email_val = await active_learning.get_or_learn_answer("Qual é o seu e-mail?", job_context)
                await email_input.fill(email_val)
                fields_filled += 1
                logs.append(f"Preenchido Email: {email_val}")

            # 3. Phone
            phone_input = page.locator("input[name='phone']").first
            if await phone_input.count() > 0 and await phone_input.is_visible():
                phone_val = await active_learning.get_or_learn_answer("Qual é o seu telefone / WhatsApp?", job_context)
                await phone_input.fill(phone_val)
                fields_filled += 1
                logs.append(f"Preenchido Phone: {phone_val}")

            # 4. Current Company / Org
            org_input = page.locator("input[name='org']").first
            if await org_input.count() > 0 and await org_input.is_visible():
                org_val = await active_learning.get_or_learn_answer("Qual é a sua empresa atual?", job_context)
                await org_input.fill(org_val)
                fields_filled += 1
                logs.append(f"Preenchido Current Company: {org_val}")

            # 5. Social URLs
            url_mappings = {
                "input[name='urls[LinkedIn]']": "Qual é a URL do seu perfil LinkedIn?",
                "input[name='urls[GitHub]']": "Qual é o seu perfil do GitHub?",
                "input[name='urls[Portfolio]']": "Qual é o link do seu portfólio / website?",
            }
            for selector, q in url_mappings.items():
                loc = page.locator(selector).first
                if await loc.count() > 0 and await loc.is_visible():
                    val = await active_learning.get_or_learn_answer(q, job_context)
                    if val and val != "N/A":
                        await loc.fill(val)
                        fields_filled += 1
                        logs.append(f"Preenchido link ({selector}): {val}")

            # 6. Resume Upload
            if resume_path and Path(resume_path).exists():
                file_input = page.locator("input[type='file'][name='resume'], input[type='file']").first
                if await file_input.count() > 0:
                    await file_input.set_input_files(str(Path(resume_path).resolve()))
                    fields_filled += 1
                    logs.append(f"Currículo anexado: {resume_path}")
                    logger.info(f"Currículo Lever anexado com sucesso ({resume_path})")

            # 7. Custom Questions
            custom_questions = await page.locator("div.application-question, .custom-question, ul.application-questions li").all()
            for q_elem in custom_questions:
                label_el = q_elem.locator(".text, label").first
                if await label_el.count() == 0:
                    continue

                q_text = (await label_el.inner_text()).strip()
                if not q_text:
                    continue

                input_el = q_elem.locator("input[type='text'], input:not([type]), textarea").first
                if await input_el.count() > 0 and await input_el.is_visible():
                    curr = await input_el.input_value()
                    if not curr:
                        ans = await active_learning.get_or_learn_answer(q_text, job_context)
                        await input_el.fill(str(ans))
                        fields_filled += 1
                        logs.append(f"Pergunta Lever respondida: '{q_text}' -> '{ans}'")

            # 8. Submit or Dry Run
            submit_btn = page.locator("button#btn-submit, button.template-btn-submit, button:has-text('Submit application')").first
            if dry_run:
                logs.append("Modo DRY-RUN ativado: formulário Lever validado sem submissão final.")
                logger.info(f"DRY-RUN Lever concluído para {job_context.get('title')}. Total campos: {fields_filled}")
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
                logs.append("Candidatura submetida oficialmente no Lever.")
                return ApplicationResult(
                    success=True,
                    channel=self.channel,
                    job_id=job_context.get("id"),
                    fields_filled=fields_filled,
                    logs=logs,
                    dry_run=False,
                )
            else:
                logs.append("Botão de submissão Lever não encontrado.")
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
            err = f"Erro no preenchimento Lever: {e}"
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
