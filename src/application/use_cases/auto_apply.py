import asyncio
import json
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import duckdb
from playwright.async_api import async_playwright

from src.application.use_cases.profile import ProfileManager
from src.application.use_cases.active_learning import ActiveLearningService
from src.infrastructure.adapters.ats.router import get_ats_adapter, detect_channel
from src.domain.models import ApplicationChannel
from src.infrastructure.adapters.database import setup_duckdb_logging

logger = logging.getLogger(__name__)


class AutoApplyWorker:
    """
    Worker autônomo multi-canal para preenchimento de candidaturas no LinkedIn (Easy Apply)
    e ATS externos (Greenhouse, Lever, Ashby).
    Utiliza RAG sobre DuckDB VSS e Active Learning em tempo real para responder dinamicamente.
    """

    def __init__(
        self,
        db_path: str = "data/open_job_loop.duckdb",
        headless: bool = True,
        dry_run: bool = False,
        mock_mode: bool = False,
        state_file: str = "data/linkedin_state.json",
        resume_path: Optional[str] = "data/resume.pdf",
        interactive_active_learning: bool = False,
        llm_model: str = "qwen2.5-coder:3b",
    ):
        self.db_path = db_path
        self.headless = headless
        self.dry_run = dry_run
        self.mock_mode = mock_mode
        self.state_file = state_file
        self.resume_path = resume_path
        self.interactive_active_learning = interactive_active_learning
        self.llm_model = llm_model

    async def run(self, limit: Optional[int] = None) -> Dict[str, Any]:
        logger.info(
            f"Iniciando Worker de Auto-Apply multi-canal (dry_run={self.dry_run}, headless={self.headless}, resume={self.resume_path})..."
        )
        conn = duckdb.connect(self.db_path, read_only=False)
        try:
            conn.execute("INSTALL vss; LOAD vss; SET hnsw_enable_experimental_persistence=true;")
        except Exception:
            pass

        # Garantir que a tabela existe mesmo em bancos novos
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_postings (
                id VARCHAR PRIMARY KEY,
                title VARCHAR NOT NULL,
                company VARCHAR NOT NULL,
                location VARCHAR,
                url VARCHAR NOT NULL,
                raw_description TEXT NOT NULL,
                truncated_description TEXT,
                content_hash VARCHAR(64) UNIQUE NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'INGESTED',
                fit_score INTEGER,
                recommendation VARCHAR,
                rationale TEXT,
                matched_skills VARCHAR,
                missing_skills VARCHAR,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                ingested_at TIMESTAMP,
                triaged_at TIMESTAMP,
                created_at TIMESTAMP
            );
        """)

        cols_available = [c[0].lower() for c in conn.execute("DESCRIBE job_postings;").fetchall()]
        select_fields = ["id", "title", "company", "url"]
        if "evaluation" in cols_available:
            select_fields.append("evaluation")
        elif "rationale" in cols_available:
            select_fields.extend(["rationale", "matched_skills", "missing_skills"])

        query = f"SELECT {', '.join(select_fields)} FROM job_postings WHERE status = 'SHORTLISTED'"
        if limit:
            query += f" LIMIT {limit}"
        res = conn.execute(query).fetchall()

        if not res:
            logger.info("Nenhuma vaga com status 'SHORTLISTED' aguardando candidatura.")
            conn.close()
            return {"processed": 0, "applied": 0, "failed": 0, "skipped": 0}

        logger.info(f"Encontradas {len(res)} vagas para aplicar.")
        try:
            conn.execute("ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS error_message TEXT;")
            conn.execute("ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS channel VARCHAR;")
            conn.execute("ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS application_log TEXT;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS application_audit_log (
                    id UUID PRIMARY KEY DEFAULT uuid(),
                    job_id VARCHAR NOT NULL,
                    job_title VARCHAR NOT NULL,
                    company VARCHAR NOT NULL,
                    channel VARCHAR NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR NOT NULL,
                    fields_submitted JSON NOT NULL,
                    logs TEXT
                );
            """)
        except Exception:
            pass

        # Garantir criação do log operacional
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id UUID PRIMARY KEY DEFAULT uuid(),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    level VARCHAR NOT NULL,
                    module VARCHAR NOT NULL,
                    message TEXT NOT NULL,
                    metadata JSON
                );
            """)
        except Exception:
            pass

        setup_duckdb_logging(self.db_path)

        profile_manager = ProfileManager(
            db_path=self.db_path,
            mock_mode=self.mock_mode,
            llm_model=self.llm_model,
        )
        active_learning = ActiveLearningService(
            profile_manager=profile_manager,
            interactive=self.interactive_active_learning,
        )

        stats = {"processed": len(res), "applied": 0, "failed": 0, "skipped": 0}

        # Resolve state file location
        actual_state_file = None
        for candidate_path in [self.state_file, "linkedin_state.json", "data/linkedin_state.json"]:
            if Path(candidate_path).exists():
                actual_state_file = candidate_path
                break

        async with async_playwright() as p:
            if actual_state_file:
                logger.info(f"Usando sessão autenticada salva ({actual_state_file}).")
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context(storage_state=actual_state_file)
            else:
                logger.warning("Nenhum arquivo de sessão (linkedin_state.json) encontrado. Navegando sem autenticação prévia.")
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context()

            page = await context.new_page()

            for job_row in res:
                job_id, title, company, url = job_row[0], job_row[1], job_row[2], job_row[3]
                rationale, matched_skills, missing_skills = "", "", ""
                if "evaluation" in cols_available and len(job_row) > 4:
                    eval_raw = job_row[4]
                    if eval_raw:
                        try:
                            eval_data = json.loads(eval_raw) if isinstance(eval_raw, str) else eval_raw
                            rationale = eval_data.get("reasoning", "")
                            matched = eval_data.get("matched_skills", [])
                            matched_skills = ", ".join(matched) if isinstance(matched, list) else str(matched)
                            missing = eval_data.get("missing_skills", [])
                            missing_skills = ", ".join(missing) if isinstance(missing, list) else str(missing)
                        except Exception:
                            pass
                elif len(job_row) >= 7:
                    rationale = job_row[4] or ""
                    matched_skills = job_row[5] or ""
                    missing_skills = job_row[6] or ""

                logger.info(f"Iniciando fluxo para vaga: '{title}' na '{company}'")
                submitted_fields: list[dict[str, Any]] = []
                job_ctx = {
                    "id": job_id,
                    "title": title,
                    "company": company,
                    "rationale": rationale or "",
                    "matched_skills": matched_skills or "",
                    "missing_skills": missing_skills or "",
                }

                try:
                    # 1. Direct ATS Detection
                    ats_adapter = get_ats_adapter(url)
                    if ats_adapter:
                        logger.info(f"Vaga no ATS externo ({ats_adapter.channel.value}) detectada para {title}.")
                        app_res = await ats_adapter.fill_and_submit(
                            page=page,
                            url=url,
                            job_context=job_ctx,
                            active_learning=active_learning,
                            resume_path=self.resume_path,
                            dry_run=self.dry_run,
                        )
                        new_status = ("APPLIED_DRY_RUN" if self.dry_run else "APPLIED") if app_res.success else "FAILED_TO_APPLY"
                        if app_res.success:
                            stats["applied"] += 1
                        else:
                            stats["failed"] += 1

                        app_log = "\n".join(app_res.logs)
                        conn.execute(
                            "UPDATE job_postings SET status = ?, channel = ?, application_log = ?, error_message = ? WHERE id = ?",
                            (new_status, app_res.channel.value, app_log, app_res.error_message, job_id),
                        )
                        conn.execute("CHECKPOINT;")
                        continue

                    # 2. LinkedIn Page Flow
                    await page.goto(url, timeout=30000)
                    await asyncio.sleep(1.5)

                    easy_apply_button = page.locator("button:has-text('Easy Apply'), button:has-text('Candidatura simplificada')").first
                    if await easy_apply_button.count() > 0 and await easy_apply_button.is_visible():
                        logger.info(f"Botão Easy Apply detectado para {title}. Abrindo modal...")
                        await easy_apply_button.click()
                        await asyncio.sleep(2)

                        # Loop pelas telas da modal de candidatura
                        max_pages = 6
                        for page_idx in range(max_pages):
                            # Resume file upload inside modal if present
                            if self.resume_path and Path(self.resume_path).exists():
                                file_input = page.locator("input[type='file']").first
                                if await file_input.count() > 0:
                                    try:
                                        await file_input.set_input_files(str(Path(self.resume_path).resolve()))
                                        logger.info(f"  [Anexo CV] Currículo anexado na modal: {self.resume_path}")
                                    except Exception:
                                        pass

                            # Identifica labels de campos de entrada (input, textarea, select)
                            labels = await page.locator("label.artdeco-text-input--label, label[for]").all()
                            for label in labels:
                                question_text = (await label.inner_text()).strip()
                                input_id = await label.get_attribute("for")
                                if input_id and question_text:
                                    input_element = page.locator(f"#{input_id}")
                                    if await input_element.count() > 0:
                                        tag_name = await input_element.evaluate("el => el.tagName.toLowerCase()")
                                        if tag_name == "select":
                                            val = await input_element.input_value()
                                            if not val or val in ["Select an option", ""]:
                                                answer = await active_learning.get_or_learn_answer(
                                                    question_text,
                                                    job_context=job_ctx,
                                                )
                                                selected = False
                                                for opt in await input_element.locator("option").all():
                                                    opt_text = (await opt.inner_text()).strip()
                                                    if (
                                                        answer.lower() == opt_text.lower()
                                                        or answer.lower() in opt_text.lower()
                                                        or opt_text.lower() in answer.lower()
                                                    ):
                                                        opt_val = await opt.get_attribute("value")
                                                        if opt_val:
                                                            await input_element.select_option(value=opt_val)
                                                            selected = True
                                                            break
                                                if not selected:
                                                    try:
                                                        await input_element.select_option(label=str(answer))
                                                    except Exception:
                                                        pass
                                                submitted_fields.append({
                                                    "question": question_text,
                                                    "answer": str(answer),
                                                    "type": "select",
                                                })
                                                logger.info(f"  [Dropdown Selecionado] '{question_text}' -> '{answer}'")
                                        elif await input_element.is_editable():
                                            current_val = await input_element.input_value()
                                            if not current_val:
                                                answer = await active_learning.get_or_learn_answer(
                                                    question_text,
                                                    job_context=job_ctx,
                                                )
                                                await input_element.fill(str(answer))
                                                submitted_fields.append({
                                                    "question": question_text,
                                                    "answer": str(answer),
                                                    "type": "text",
                                                })
                                                logger.info(f"  [Campo Respondido (Active Learning RAG)] '{question_text}' -> '{answer}'")

                            # Identifica perguntas de rádio/opções (fieldset)
                            fieldsets = await page.locator("fieldset").all()
                            for fs in fieldsets:
                                legend = fs.locator("legend").first
                                if await legend.count() > 0:
                                    q_text = (await legend.inner_text()).strip()
                                    if q_text:
                                        answer = await active_learning.get_or_learn_answer(q_text, job_context=job_ctx)
                                        target_radio = fs.locator(f"label:has-text('{answer}')").first
                                        if await target_radio.count() > 0:
                                            try:
                                                await target_radio.click()
                                                submitted_fields.append({
                                                    "question": q_text,
                                                    "answer": str(answer),
                                                    "type": "radio",
                                                })
                                                logger.info(f"  [Radio Selecionado] '{q_text}' -> '{answer}'")
                                            except Exception:
                                                pass

                            submit_button = page.locator(
                                "button:has-text('Submit application'), button:has-text('Enviar candidatura')"
                            ).first
                            review_button = page.locator(
                                "button:has-text('Review'), button:has-text('Revisar')"
                            ).first
                            next_button = page.locator(
                                "button:has-text('Next'), button:has-text('Avançar')"
                            ).first

                            if await submit_button.count() > 0 and await submit_button.is_visible():
                                if self.dry_run:
                                    logger.info(f"  [DRY-RUN] Tela final de submissão alcançada para {title}. Submissão evitada.")
                                    break
                                else:
                                    logger.info(f"Submetendo candidatura oficial para {title}...")
                                    await submit_button.click()
                                    await asyncio.sleep(2)
                                    break
                            elif await review_button.count() > 0 and await review_button.is_visible():
                                await review_button.click()
                                await asyncio.sleep(1.5)
                            elif await next_button.count() > 0 and await next_button.is_visible():
                                await next_button.click()
                                await asyncio.sleep(1.5)
                            else:
                                logger.info(f"Fim de fluxo de passos do modal para {title}.")
                                break

                        new_status = "APPLIED_DRY_RUN" if self.dry_run else "APPLIED"
                        channel_val = ApplicationChannel.LINKEDIN_EASY_APPLY.value
                        conn.execute(
                            "UPDATE job_postings SET status = ?, channel = ?, application_log = ? WHERE id = ?",
                            (new_status, channel_val, f"Easy Apply completed (dry_run={self.dry_run})", job_id),
                        )
                        conn.execute(
                            """
                            INSERT INTO application_audit_log (job_id, job_title, company, channel, status, fields_submitted, logs, applied_at)
                            VALUES (?, ?, ?, ?, ?, ?::JSON, ?, CURRENT_TIMESTAMP);
                            """,
                            (
                                job_id,
                                title,
                                company,
                                channel_val,
                                new_status,
                                json.dumps(submitted_fields, ensure_ascii=False),
                                f"Easy Apply completed (dry_run={self.dry_run})",
                            ),
                        )
                        conn.execute("CHECKPOINT;")
                        stats["applied"] += 1
                        logger.info(f"✅ Candidatura finalizada com sucesso ({new_status}) para {title}")
                    else:
                        # 3. Check for external application redirection
                        ext_apply = page.locator("button.jobs-apply-button, a.jobs-apply-button, a:has-text('Apply')").first
                        if await ext_apply.count() > 0:
                            ext_href = await ext_apply.get_attribute("href")
                            if ext_href and get_ats_adapter(ext_href):
                                dest_adapter = get_ats_adapter(ext_href)
                                logger.info(f"Redirecionamento ATS detectado ({dest_adapter.channel.value}): {ext_href}")
                                app_res = await dest_adapter.fill_and_submit(
                                    page=page,
                                    url=ext_href,
                                    job_context=job_ctx,
                                    active_learning=active_learning,
                                    resume_path=self.resume_path,
                                    dry_run=self.dry_run,
                                )
                                new_status = ("APPLIED_DRY_RUN" if self.dry_run else "APPLIED") if app_res.success else "FAILED_TO_APPLY"
                                if app_res.success:
                                    stats["applied"] += 1
                                else:
                                    stats["failed"] += 1
                                app_log = "\n".join(app_res.logs)
                                conn.execute(
                                    "UPDATE job_postings SET status = ?, channel = ?, application_log = ?, error_message = ? WHERE id = ?",
                                    (new_status, app_res.channel.value, app_log, app_res.error_message, job_id),
                                )
                                conn.execute(
                                    """
                                    INSERT INTO application_audit_log (job_id, job_title, company, channel, status, fields_submitted, logs, applied_at)
                                    VALUES (?, ?, ?, ?, ?, ?::JSON, ?, CURRENT_TIMESTAMP);
                                    """,
                                    (
                                        job_id,
                                        title,
                                        company,
                                        app_res.channel.value,
                                        new_status,
                                        json.dumps([{"log": l} for l in app_res.logs], ensure_ascii=False),
                                        app_log,
                                    ),
                                )
                                conn.execute("CHECKPOINT;")
                                continue

                        logger.info(f"Sem botão de Easy Apply ou ATS suportado para {title}. Marcando como NEEDS_REVIEW.")
                        conn.execute(
                            "UPDATE job_postings SET status = 'NEEDS_REVIEW', channel = 'EXTERNAL_MANUAL' WHERE id = ?",
                            (job_id,),
                        )
                        conn.execute(
                            """
                            INSERT INTO application_audit_log (job_id, job_title, company, channel, status, fields_submitted, logs, applied_at)
                            VALUES (?, ?, ?, ?, 'SKIPPED_EXTERNAL_MANUAL', ?::JSON, ?, CURRENT_TIMESTAMP);
                            """,
                            (
                                job_id,
                                title,
                                company,
                                "EXTERNAL_MANUAL",
                                json.dumps([], ensure_ascii=False),
                                "No Easy Apply button or supported ATS found. Marked as NEEDS_REVIEW.",
                            ),
                        )
                        conn.execute("CHECKPOINT;")
                        stats["skipped"] += 1

                except Exception as e:
                    logger.error(f"❌ Erro durante candidatura em {title}: {e}")
                    conn.execute(
                        "UPDATE job_postings SET status = 'FAILED_TO_APPLY', error_message = ? WHERE id = ?",
                        (str(e)[:500], job_id),
                    )
                    try:
                        conn.execute(
                            """
                            INSERT INTO application_audit_log (job_id, job_title, company, channel, status, fields_submitted, logs, applied_at)
                            VALUES (?, ?, ?, ?, 'FAILED_TO_APPLY', ?::JSON, ?, CURRENT_TIMESTAMP);
                            """,
                            (
                                job_id,
                                title,
                                company,
                                "ERROR",
                                json.dumps(submitted_fields, ensure_ascii=False),
                                str(e),
                            ),
                        )
                    except Exception:
                        pass
                    conn.execute("CHECKPOINT;")
                    stats["failed"] += 1

            await browser.close()

        conn.close()
        logger.info(f"Auto-Apply concluído: {stats}")
        return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    worker = AutoApplyWorker(dry_run=True)
    asyncio.run(worker.run())
