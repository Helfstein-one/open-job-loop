import asyncio
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import duckdb
from playwright.async_api import async_playwright

from src.application.use_cases.profile import ProfileManager

logger = logging.getLogger(__name__)


class AutoApplyWorker:
    """
    Worker autônomo baseado em Playwright para preenchimento de candidaturas Easy Apply no LinkedIn.
    Utiliza RAG sobre DuckDB VSS para responder dinamicamente às perguntas do recrutador.
    """

    def __init__(
        self,
        db_path: str = "open_job_loop.duckdb",
        headless: bool = True,
        dry_run: bool = False,
        mock_mode: bool = False,
        state_file: str = "data/linkedin_state.json",
    ):
        self.db_path = db_path
        self.headless = headless
        self.dry_run = dry_run
        self.mock_mode = mock_mode
        self.state_file = state_file

    async def run(self, limit: Optional[int] = None) -> Dict[str, Any]:
        logger.info(f"Iniciando Worker de Auto-Apply (dry_run={self.dry_run}, headless={self.headless})...")
        conn = duckdb.connect(self.db_path, read_only=False)

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

        query = (
            "SELECT id, title, company, url, rationale, matched_skills, missing_skills "
            "FROM job_postings WHERE status = 'SHORTLISTED'"
        )
        if limit:
            query += f" LIMIT {limit}"
        res = conn.execute(query).fetchall()

        if not res:
            logger.info("Nenhuma vaga com status 'SHORTLISTED' aguardando candidatura.")
            conn.close()
            return {"processed": 0, "applied": 0, "failed": 0, "skipped": 0}

        logger.info(f"Encontradas {len(res)} vagas para aplicar.")
        profile_manager = ProfileManager(db_path=self.db_path, mock_mode=self.mock_mode)

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

            for job in res:
                job_id, title, company, url, rationale, matched_skills, missing_skills = job
                logger.info(f"Iniciando fluxo para vaga: '{title}' na '{company}'")
                job_ctx = {
                    "title": title,
                    "company": company,
                    "rationale": rationale or "",
                    "matched_skills": matched_skills or "",
                    "missing_skills": missing_skills or "",
                }

                try:
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
                            # Identifica labels de campos de entrada de texto
                            labels = await page.locator("label.artdeco-text-input--label, label[for]").all()
                            for label in labels:
                                question_text = (await label.inner_text()).strip()
                                input_id = await label.get_attribute("for")
                                if input_id and question_text:
                                    input_element = page.locator(f"#{input_id}")
                                    if await input_element.count() > 0 and await input_element.is_editable():
                                        current_val = await input_element.input_value()
                                        if not current_val:
                                            answer = await profile_manager.synthesize_answer(
                                                question_text,
                                                job_context=job_ctx,
                                            )
                                            await input_element.fill(str(answer))
                                            logger.info(f"  [Campo Respondido (Multi-Agent RAG)] '{question_text}' -> '{answer}'")

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
                        conn.execute("UPDATE job_postings SET status = ? WHERE id = ?", (new_status, job_id))
                        conn.execute("CHECKPOINT;")
                        stats["applied"] += 1
                        logger.info(f"✅ Candidatura finalizada com sucesso ({new_status}) para {title}")
                    else:
                        logger.info(f"Sem botão de Easy Apply disponível para {title}.")
                        stats["skipped"] += 1

                except Exception as e:
                    logger.error(f"❌ Erro durante candidatura em {title}: {e}")
                    conn.execute(
                        "UPDATE job_postings SET status = 'FAILED_TO_APPLY', error_message = ? WHERE id = ?",
                        (str(e)[:500], job_id),
                    )
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
