from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import asyncio
from src.cli import app as cli_app
from src.application.use_cases.auto_apply import AutoApplyWorker
from src.application.use_cases.profile import ProfileManager
import duckdb

app = FastAPI(title="Job-Bot API")

class DiscoveryRequest(BaseModel):
    keywords: str
    limit: int = 5

class ProfileRequest(BaseModel):
    category: str
    content: str

def run_discovery_task(keywords: str, limit: int):
    import subprocess
    cmd = f"python -m src.cli run --no-mock --keywords '{keywords}' --limit {limit}"
    subprocess.run(cmd, shell=True)
    # Depois de rodar a busca, aciona o Worker para aplicar na fila
    worker = AutoApplyWorker(db_path="/app/data/open_job_loop.duckdb")
    asyncio.run(worker.run())

@app.post("/start")
async def start_discovery(request: DiscoveryRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(run_discovery_task, request.keywords, request.limit)
    return {"status": "Busca e Auto-Apply iniciados em background."}

@app.get("/report")
async def get_report():
    try:
        conn = duckdb.connect('/app/data/open_job_loop.duckdb', read_only=True)
        res = conn.execute("SELECT status, COUNT(*) FROM job_postings GROUP BY status").fetchall()
        report = {}
        for row in res:
            report[row[0]] = row[1]
        return {"report": report}
    except Exception as e:
        return {"error": str(e)}

@app.post("/profile")
async def update_profile(request: ProfileRequest):
    try:
        manager = ProfileManager(db_path="/app/data/open_job_loop.duckdb")
        manager.initialize_schema()
        await manager.add_resume_chunk(request.category, request.content)
        return {"status": "Success", "message": f"'{request.category}' vetorizado no DuckDB VSS."}
    except Exception as e:
        return {"error": str(e)}

@app.get("/health")
async def health():
    return {"status": "running"}
