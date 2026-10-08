import asyncio
from playwright.async_api import async_playwright
import duckdb
import logging

logger = logging.getLogger(__name__)

class AutoApplyWorker:
    def __init__(self, db_path: str = "open_job_loop.duckdb"):
        self.db_path = db_path
        
    async def run(self):
        logger.info("Iniciando Worker de Auto-Apply...")
        conn = duckdb.connect(self.db_path, read_only=False)
        
        # Look for SHORTLISTED jobs
        res = conn.execute("SELECT id, title, company, url FROM job_postings WHERE status = 'SHORTLISTED'").fetchall()
        if not res:
            logger.info("Nenhuma vaga na fila (SHORTLISTED).")
            return
            
        logger.info(f"Encontradas {len(res)} vagas para aplicar.")
        
        async with async_playwright() as p:
            import os
            state_file = "linkedin_state.json"
            
            # Decide if we have a saved session
            if os.path.exists(state_file):
                logger.info("Usando sessão autenticada salva (linkedin_state.json).")
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(storage_state=state_file)
            else:
                logger.warning("Nenhum estado de login encontrado. Navegando como anônimo.")
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                
            page = await context.new_page()
            
            for job in res:
                job_id, title, company, url = job
                logger.info(f"Aplicando para: {title} na {company}")
                
                try:
                    await page.goto(url)
                    
                    # Logica temporária: apenas verifica se o botão 'Easy Apply' existe
                    # Em breve colocaremos a extração do formulário e chamada ao RAG
                    easy_apply_button = page.locator("button:has-text('Easy Apply')").first
                    if await easy_apply_button.count() > 0 and await easy_apply_button.is_visible():
                        logger.info(f"Botão Easy Apply detectado para {title}. Iniciando fluxo...")
                        await easy_apply_button.click()
                        await asyncio.sleep(2)
                        
                        # Loop de preenchimento de páginas modais do Easy Apply
                        max_pages = 5
                        for _ in range(max_pages):
                            # Detecta os labels do formulário
                            labels = await page.locator("label.artdeco-text-input--label").all()
                            from src.application.use_cases.profile import ProfileManager
                            manager = ProfileManager(self.db_path)
                            
                            for label in labels:
                                question_text = await label.inner_text()
                                input_id = await label.get_attribute("for")
                                if input_id:
                                    input_element = page.locator(f"#{input_id}")
                                    if await input_element.is_editable() and not await input_element.input_value():
                                        # Consulta o banco vetorial para achar a resposta no currículo
                                        rag_context = await manager.search_profile(question_text, limit=1)
                                        context_str = rag_context[0]['content'] if rag_context else "Sem resposta."
                                        
                                        # (Pseudo-código do LLM para formular a resposta baseada no contexto)
                                        # answer = await llm.ask(f"Question: {question_text}, Context: {context_str}")
                                        # Temporariamente usando o próprio contexto puro truncado
                                        answer = context_str[:50] 
                                        
                                        await input_element.fill(answer)
                                        logger.info(f"Preenchido: '{question_text}' com '{answer}'")
                            
                            # Clicar em "Next" ou "Review"
                            next_button = page.locator("button:has-text('Next')").first
                            review_button = page.locator("button:has-text('Review')").first
                            submit_button = page.locator("button:has-text('Submit application')").first
                            
                            if await submit_button.count() > 0 and await submit_button.is_visible():
                                logger.info("Página de submissão alcançada. Finalizando...")
                                await submit_button.click()
                                break
                            elif await review_button.count() > 0 and await review_button.is_visible():
                                await review_button.click()
                            elif await next_button.count() > 0 and await next_button.is_visible():
                                await next_button.click()
                            else:
                                logger.warning("Nenhum botão de avançar encontrado. Cancelando.")
                                break
                            
                            await asyncio.sleep(1.5)
                        
                        conn.execute("UPDATE job_postings SET status = 'APPLIED' WHERE id = ?", (job_id,))
                        logger.info(f"✅ Sucesso: {title}")
                    else:
                        logger.info(f"Sem Easy Apply detectado (ou já aplicado) para {title}.")
                except Exception as e:
                    logger.error(f"❌ Erro ao aplicar para {title}: {e}")
                    conn.execute("UPDATE job_postings SET status = 'FAILED_TO_APPLY', error_message = ? WHERE id = ?", (str(e), job_id))
            
            await browser.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    worker = AutoApplyWorker()
    asyncio.run(worker.run())
