import asyncio
from playwright.async_api import async_playwright
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def login_and_save_state(state_file: str = "linkedin_state.json"):
    """
    Abre um navegador visível para o usuário fazer login no LinkedIn.
    Salva a sessão para ser usada pelo Auto-Apply depois.
    """
    logger.info("Iniciando navegador para autenticação...")
    
    async with async_playwright() as p:
        # headless=False para o usuário poder ver a tela e interagir
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        logger.info("Navegando para o LinkedIn...")
        await page.goto("https://www.linkedin.com/login")
        
        print("\n" + "="*50)
        print("🛑 ATENÇÃO: Faça o login no navegador que acabou de abrir.")
        print("Após fazer login com sucesso e ver o feed do LinkedIn,")
        print("volte para este terminal e pressione ENTER.")
        print("="*50 + "\n")
        
        # Espera o usuário confirmar manualmente no console
        # Usamos input bloqueante executado em um thread pool para não travar o loop do asyncio
        await asyncio.to_thread(input, "Pressione ENTER aqui após fazer o login no navegador: ")
        
        # Salva o estado atual (cookies, local storage)
        await context.storage_state(path=state_file)
        
        logger.info(f"✅ Sessão salva com sucesso em '{state_file}'. O robô agora pode navegar invisível!")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(login_and_save_state())
