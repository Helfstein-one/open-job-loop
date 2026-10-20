from __future__ import annotations

"""
Greenhouse ATS Adapter.
Supports boards.greenhouse.io, job-boards.greenhouse.io, and embedded Greenhouse boards.
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


class GreenhouseAdapter(BaseATSAdapter):
    """
    Automated form-filler adapter for Greenhouse job applications.
    """

    channel = ApplicationChannel.GREENHOUSE

    def matches_url(self, url: str) -> bool:
        if not url:
            return False
        if "greenhouse" in url.lower():
            return True
        netloc = urlparse(url).netloc.lower()
        return "greenhouse.io" in netloc or "grnh.se" in netloc

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

        logs.append(f"Acessando Greenhouse ATS: {url}")
        logger.info(f"Navegando para vaga Greenhouse: {url}")

        try:
            if (
                url
                and not url.startswith("mock://")
                and (page.url != url or url.startswith("file://"))
            ):
                await page.goto(url, timeout=30000)
                await asyncio.sleep(0.5)

            # Ensure we are scrolled to the application form
            app_form = page.locator(
                "#application, #application_form, #app_body, form#new_job_application"
            ).first
            if await app_form.count() > 0:
                await app_form.scroll_into_view_if_needed()

            # 1. First & Last Name
            first_name_input = page.locator(
                "#first_name, input[name='job_application[first_name]']"
            ).first
            last_name_input = page.locator(
                "#last_name, input[name='job_application[last_name]']"
            ).first
            full_name_input = page.locator("#name, input[name='job_application[name]']").first

            candidate_name = await active_learning.get_or_learn_answer(
                "Qual é o seu nome completo?", job_context
            )
            parts = candidate_name.split(maxsplit=1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""

            if await first_name_input.count() > 0 and await first_name_input.is_visible():
                await first_name_input.fill(first_name)
                fields_filled += 1
                logs.append(f"Preenchido First Name: {first_name}")

            if await last_name_input.count() > 0 and await last_name_input.is_visible():
                await last_name_input.fill(last_name)
                fields_filled += 1
                logs.append(f"Preenchido Last Name: {last_name}")

            if (
                await full_name_input.count() > 0
                and await full_name_input.is_visible()
                and await first_name_input.count() == 0
            ):
                await full_name_input.fill(candidate_name)
                fields_filled += 1
                logs.append(f"Preenchido Name: {candidate_name}")

            # 2. Email
            email_input = page.locator("#email, input[name='job_application[email]']").first
            if await email_input.count() > 0 and await email_input.is_visible():
                email_val = await active_learning.get_or_learn_answer(
                    "Qual é o seu e-mail?", job_context
                )
                await email_input.fill(email_val)
                fields_filled += 1
                logs.append(f"Preenchido Email: {email_val}")

            # 3. Phone
            phone_input = page.locator("#phone, input[name='job_application[phone]']").first
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
                    logger.info(f"Currículo anexado com sucesso ({resume_path})")
            elif resume_path:
                logs.append(f"Aviso: Arquivo de currículo '{resume_path}' não encontrado no disco.")

            # 5. Custom Questions (text inputs, textareas, selects)
            custom_fields = await page.locator("div.field, div.custom-question").all()
            for field in custom_fields:
                label_el = field.locator("label").first
                if await label_el.count() == 0:
                    continue

                label_text = (await label_el.inner_text()).strip()
                if not label_text:
                    continue

                # Skip standard fields already filled
                lbl_lower = label_text.lower()
                if any(
                    x in lbl_lower
                    for x in ["first name", "last name", "email", "phone", "resume", "cv"]
                ):
                    continue

                input_el = field.locator("input[type='text'], input:not([type]), textarea").first
                select_el = field.locator("select").first

                if await input_el.count() > 0 and await input_el.is_visible():
                    val = await input_el.input_value()
                    if not val:
                        ans = await active_learning.get_or_learn_answer(label_text, job_context)
                        await input_el.fill(str(ans))
                        fields_filled += 1
                        logs.append(f"Campo customizado preenchido: '{label_text}' -> '{ans}'")
                elif await select_el.count() > 0 and await select_el.is_visible():
                    # Pick best option
                    ans = await active_learning.get_or_learn_answer(label_text, job_context)
                    # Try to select by label or value
                    try:
                        await select_el.select_option(label=ans)
                        fields_filled += 1
                        logs.append(f"Opção selecionada em '{label_text}': {ans}")
                    except Exception:
                        try:
                            # Fallback: select second option (first is usually empty placeholder)
                            options = await select_el.locator("option").all()
                            if len(options) > 1:
                                opt_val = await options[1].get_attribute("value")
                                if opt_val:
                                    await select_el.select_option(value=opt_val)
                                    fields_filled += 1
                                    logs.append(f"Opção padrão selecionada em '{label_text}'")
                        except Exception:
                            pass

            # 6. Submission or Dry Run
            submit_btn = page.locator(
                "#submit_app, input[type='submit'], button[type='submit']"
            ).first
            if dry_run:
                logs.append(
                    "Modo DRY-RUN ativado: formulário preenchido com sucesso sem submissão final."
                )
                logger.info(
                    f"DRY-RUN Greenhouse concluído para {job_context.get('title')}. Total campos: {fields_filled}"
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
                logs.append("Candidatura submetida oficialmente no Greenhouse.")
                logger.info("Candidatura Greenhouse submetida com sucesso.")
                return ApplicationResult(
                    success=True,
                    channel=self.channel,
                    job_id=job_context.get("id"),
                    fields_filled=fields_filled,
                    logs=logs,
                    dry_run=False,
                )
            else:
                logs.append("Botão de submissão não encontrado ou não visível.")
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
            err = f"Erro no preenchimento Greenhouse: {e}"
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
