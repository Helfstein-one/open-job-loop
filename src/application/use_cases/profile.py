import os
import math
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any
import duckdb
from src.application.ports.user_profile import UserProfilePort
import yaml
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def compute_mock_embedding(text: str, dim: int = 768) -> List[float]:
    """
    Deterministic pseudo-semantic embedding for offline testing and fallback.
    Maps word hashes into a fixed-dimension vector with L2 normalization.
    """
    vec = [0.001] * dim
    words = text.lower().replace("?", " ").replace(",", " ").replace(".", " ").replace(":", " ").split()
    for w in words:
        if len(w) < 2:
            continue
        h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 2.0
        # Also spread into secondary slot for smoother density
        vec[(h >> 8) % dim] += 1.0

    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [round(x / norm, 6) for x in vec]


class ProfileManager(UserProfilePort):
    """
    Gerencia o perfil do candidato, dados profissionais e respostas padrão.
    Vetoriza trechos semânticos no DuckDB VSS para consulta em RAG durante auto-apply.
    """

    def __init__(
        self,
        db_path: str = "data/open_job_loop.duckdb",
        embedding_model: str = "nomic-embed-text",
        llm_model: str = "qwen2.5-coder:3b",
        mock_mode: bool = False,
        base_url: Optional[str] = None,
    ):
        self.db_path = db_path
        self.embedding_model = embedding_model
        self.llm_model = llm_model
        self.mock_mode = mock_mode
        self.base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self.client = AsyncOpenAI(base_url=self.base_url, api_key="ollama")

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        parent = Path(self.db_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(self.db_path, read_only=False)
        conn.execute("INSTALL vss; LOAD vss;")
        conn.execute("SET hnsw_enable_experimental_persistence=true;")
        return conn

    def initialize_schema(self) -> None:
        """Cria a tabela de perfil com índice vetorial HNSW."""
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                id UUID DEFAULT uuid(),
                category VARCHAR,
                content TEXT,
                embedding FLOAT[768]
            );
        """)
        try:
            conn.execute("DROP INDEX IF EXISTS idx_profile_emb;")
            conn.execute("""
                CREATE INDEX idx_profile_emb ON user_profile USING HNSW (embedding)
                WITH (metric = 'cosine');
            """)
        except Exception as e:
            logger.warning(f"Aviso ao inicializar índice HNSW VSS: {e}")
        finally:
            conn.close()

    async def get_embedding(self, text: str) -> List[float]:
        """Obtém vetor de embedding (Ollama nomic-embed-text ou mock determinístico)."""
        if self.mock_mode:
            return compute_mock_embedding(text)

        try:
            response = await self.client.embeddings.create(
                model=self.embedding_model,
                input=text,
            )
            return response.data[0].embedding
        except Exception as e:
            logger.warning(f"Falha ao chamar Ollama embedding ({e}). Usando fallback mock.")
            return compute_mock_embedding(text)

    def chunk_profile(self, data: dict) -> List[Tuple[str, str]]:
        """Divide o perfil do candidato em blocos semânticos etiquetados por categoria."""
        chunks: List[Tuple[str, str]] = []

        cand = data.get("candidate", {})
        if cand:
            bio_text = (
                f"Candidate Name: {cand.get('name', '')}. "
                f"Email: {cand.get('email', '')}. "
                f"Phone: {cand.get('phone', '')}. "
                f"Location: {cand.get('location', '')}. "
                f"Title: {cand.get('title', '')}. "
                f"Years of Experience: {cand.get('years_of_experience', '')}. "
                f"LinkedIn: {cand.get('linkedin_url', '')}."
            )
            chunks.append(("personal_bio", bio_text.strip()))

        summary = data.get("summary", "")
        if summary:
            chunks.append(("professional_summary", str(summary).strip()))

        auth = data.get("work_authorization", {})
        if auth:
            auth_text = (
                f"Work Authorization: "
                f"Authorized in Brazil: {auth.get('authorized_in_brazil')}. "
                f"Authorized in US: {auth.get('authorized_in_us')}. "
                f"Requires US Visa Sponsorship: {auth.get('requires_sponsorship_us')}. "
                f"Requires EU Visa Sponsorship: {auth.get('requires_sponsorship_eu')}."
            )
            chunks.append(("work_authorization", auth_text))

        prefs = data.get("preferences", {})
        if prefs:
            pref_text = (
                f"Compensation and Work Preferences: "
                f"Expected Salary USD: {prefs.get('expected_salary_usd')}. "
                f"Expected Salary BRL: {prefs.get('expected_salary_brl')}. "
                f"Notice Period / Availability: {prefs.get('notice_period')}. "
                f"Work Model Preference: {prefs.get('work_preference')}."
            )
            chunks.append(("compensation_preferences", pref_text))

        skills = data.get("skills", {})
        if skills:
            skill_lines = []
            for group, items in skills.items():
                if isinstance(items, list):
                    skill_lines.append(f"{group.replace('_', ' ').title()}: {', '.join(items)}")
                else:
                    skill_lines.append(f"{group}: {items}")
            chunks.append(("technical_skills", "Candidate Technical Skills: " + " | ".join(skill_lines)))

        experiences = data.get("experiences", [])
        if isinstance(experiences, list):
            for exp in experiences:
                if isinstance(exp, dict):
                    exp_text = (
                        f"Experience: {exp.get('role')} at {exp.get('company')} ({exp.get('period')}). "
                        f"Details: {exp.get('description', '')}"
                    )
                    chunks.append(("work_experience", exp_text.strip()))

        std_answers = data.get("standard_answers", [])
        if isinstance(std_answers, list):
            for item in std_answers:
                if isinstance(item, dict):
                    q = item.get("question", "")
                    a = item.get("answer", "")
                    chunks.append(("standard_qa", f"Question: {q} Answer: {a}"))

        return chunks

    async def sync_from_yaml(self, yaml_path: str) -> int:
        """Lê profile.yaml, limpa a tabela de perfil e insere todos os chunks vetorizados no VSS."""
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Arquivo de perfil '{yaml_path}' não encontrado.")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        chunks = self.chunk_profile(data)
        if not chunks:
            logger.warning(f"Nenhum chunk semântico gerado a partir de {yaml_path}")
            return 0

        self.initialize_schema()
        conn = self._get_connection()

        # Limpa os dados anteriores para garantir sincronização idempotente
        conn.execute("DELETE FROM user_profile;")

        count = 0
        for category, content in chunks:
            emb = await self.get_embedding(content)
            conn.execute(
                "INSERT INTO user_profile (category, content, embedding) VALUES (?, ?, ?::FLOAT[768])",
                (category, content, emb),
            )
            count += 1

        conn.execute("CHECKPOINT;")
        conn.close()
        logger.info(f"Sincronizados {count} blocos de perfil com sucesso no DuckDB VSS.")
        return count

    async def search_profile(self, query: str, limit: int = 3) -> List[Dict]:
        """Busca os trechos do perfil mais relevantes para a pergunta dada com base em distância cosseno."""
        query_emb = await self.get_embedding(query)
        conn = self._get_connection()

        res = conn.execute("""
            SELECT category, content, list_cosine_distance(embedding, ?::FLOAT[768]) as dist
            FROM user_profile
            ORDER BY dist ASC
            LIMIT ?;
        """, (query_emb, limit)).fetchall()
        conn.close()

        results = []
        for row in res:
            results.append({"category": row[0], "content": row[1], "distance": float(row[2])})
        return results

    async def add_chunk(self, category: str, content: str) -> None:
        """Insere um novo bloco de conhecimento ou resposta de Active Learning no DuckDB VSS."""
        self.initialize_schema()
        emb = await self.get_embedding(content)
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO user_profile (category, content, embedding) VALUES (?, ?, ?::FLOAT[768])",
            (category, content, emb),
        )
        conn.execute("CHECKPOINT;")
        conn.close()
        logger.info(f"Novo fragmento [{category}] indexado no DuckDB VSS com sucesso.")

    def _get_cached_bio(self) -> Dict[str, str]:
        if hasattr(self, "_cached_bio_data") and self._cached_bio_data:
            return self._cached_bio_data
        data = {}
        try:
            conn = self._get_connection()
            row = conn.execute("SELECT content FROM user_profile WHERE category = 'personal_bio' LIMIT 1;").fetchone()
            conn.close()
            if row and row[0]:
                text = row[0]
                import re
                m_name = re.search(r"Candidate Name:\s*([^.]+)\.", text)
                if m_name: data["name"] = m_name.group(1).strip()
                m_email = re.search(r"Email:\s*([^\s]+)", text)
                if m_email: data["email"] = m_email.group(1).rstrip(".").strip()
                m_phone = re.search(r"Phone:\s*([^.]+)\.", text)
                if m_phone: data["phone"] = m_phone.group(1).strip()
                m_linkedin = re.search(r"LinkedIn:\s*([^\s]+)", text)
                if m_linkedin: data["linkedin"] = m_linkedin.group(1).rstrip(".").strip()
        except Exception:
            pass
        self._cached_bio_data = data
        return data

    async def synthesize_answer_with_confidence(
        self,
        question: str,
        max_words: int = 30,
        job_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, float]:
        """
        Consulta DuckDB VSS e gera resposta com estimativa de confiança [0.0 a 1.0].
        """
        # Resolução determinística imediata para dados de contato
        q_lower = question.lower()
        bio_info = self._get_cached_bio()
        if any(w in q_lower for w in ["nome completo", "full name", "first name", "seu nome", "candidate name"]):
            if bio_info.get("name"):
                return bio_info["name"], 1.0

        if any(w in q_lower for w in ["e-mail", "email", "seu email"]):
            if bio_info.get("email"):
                return bio_info["email"], 1.0

        if any(w in q_lower for w in ["telefone", "phone", "whatsapp", "celular", "mobile"]):
            if bio_info.get("phone"):
                return bio_info["phone"], 1.0

        if any(w in q_lower for w in ["linkedin", "perfil do linkedin"]):
            if bio_info.get("linkedin"):
                return bio_info["linkedin"], 1.0

        matches = await self.search_profile(question, limit=4)
        if not matches:
            return "N/A", 0.0

        best_distance = matches[0].get("distance", 1.0)
        confidence = max(0.0, min(1.0, 1.0 - best_distance))

        # Sempre inclui personal_bio na base de contexto
        bio_prefix = ""
        try:
            conn = self._get_connection()
            row = conn.execute("SELECT content FROM user_profile WHERE category = 'personal_bio' LIMIT 1;").fetchone()
            conn.close()
            if row and row[0]:
                bio_prefix = f"[personal_bio] {row[0]}\n"
        except Exception:
            pass

        context_str = bio_prefix + "\n".join([f"[{m['category']}] {m['content']}" for m in matches])

        # Se estiver em modo mock ou sem Ollama, extrai de forma heurística limpa
        if self.mock_mode:
            for m in matches:
                if "Answer:" in m["content"]:
                    ans_part = m["content"].split("Answer:", 1)[1].strip()
                    return ans_part[:100], confidence
            return matches[0]["content"][:60], confidence

        job_info_str = ""
        if job_context:
            job_info_str = (
                f"\nJob & Evaluator Context:\n"
                f"- Applying for: {job_context.get('title', 'Unknown')} at {job_context.get('company', 'Unknown')}\n"
                f"- Candidate Match Strengths: {job_context.get('matched_skills', '')}\n"
                f"- Evaluator Rationale: {job_context.get('rationale', '')}\n"
            )

        prompt = (
            f"You are an assistant filling out a job application form on LinkedIn/ATS for the candidate.\n"
            f"Context from the candidate's profile and verified background:\n"
            f"{context_str}\n"
            f"{job_info_str}\n"
            f"Form Question: {question}\n\n"
            f"Instructions:\n"
            f"- Output ONLY the direct answer suitable for the form input field.\n"
            f"- If the question asks for a number (e.g., years of experience), output ONLY the numeric value or short phrase (e.g. '10').\n"
            f"- If it's a Yes/No question, output ONLY 'Yes' or 'No'.\n"
            f"- If it asks for compensation, output the concise range matching the candidate's preferences.\n"
            f"- If open-ended (e.g. 'Why this role?'), use the candidate's background and match strengths to write a strong 1-2 sentence response.\n"
            f"- Keep it concise (maximum {max_words} words). Zero conversational filler."
        )

        try:
            resp = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": "You are a concise form-filling assistant."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=60,
            )
            answer = resp.choices[0].message.content.strip().strip('"\'`')
            return answer, confidence
        except Exception as e:
            logger.warning(f"Erro ao sintetizar resposta com LLM ({e}). Usando contexto direto.")
            for m in matches:
                if "Answer:" in m["content"]:
                    return m["content"].split("Answer:", 1)[1].strip(), confidence
            return matches[0]["content"][:80], confidence

    async def synthesize_answer(
        self,
        question: str,
        max_words: int = 30,
        job_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Consulta os padrões do candidato via RAG no DuckDB VSS e gera uma resposta
        curta, objetiva e adequada para preenchimento de formulário.
        """
        answer, _ = await self.synthesize_answer_with_confidence(
            question=question,
            max_words=max_words,
            job_context=job_context,
        )
        return answer

    def list_chunks(self) -> List[Dict]:
        """Lista os chunks atualmente armazenados no banco."""
        conn = self._get_connection()
        res = conn.execute("SELECT category, content FROM user_profile ORDER BY category").fetchall()
        conn.close()
        return [{"category": r[0], "content": r[1]} for r in res]

    # Port aliases
    store_chunk = add_chunk
    search_relevant_chunks = search_profile
    get_candidate_bio = _get_cached_bio
