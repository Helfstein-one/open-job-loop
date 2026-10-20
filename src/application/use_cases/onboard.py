import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pypdf
import yaml
from openai import AsyncOpenAI
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TargetJobProfile(BaseModel):
    target_roles: List[str] = Field(
        default_factory=list, description="Recommended target job titles"
    )
    seniority_level: str = Field(default="Senior", description="Inferred seniority level")
    suggested_keywords: str = Field(
        default="Python Engineer", description="Recommended search keywords for LinkedIn"
    )
    estimated_salary_range: str = Field(
        default="$120k - $160k", description="Estimated target salary range"
    )
    key_strengths: List[str] = Field(
        default_factory=list, description="Top candidate technical strengths"
    )
    suggested_questions: List[Dict[str, str]] = Field(
        default_factory=list, description="Adaptive grill-me questions to calibrate preferences"
    )


class ProfilerAgent:
    """
    Agente Perfilador:
    1. Lê o perfil do LinkedIn (via URL com Playwright ou arquivo PDF exportado).
    2. Analisa o histórico técnico e formula um Perfil Alvo de Vagas (Target Job Profile).
    3. Conduz uma entrevista interativa (grill-me) no terminal para calibrar preferências.
    4. Gera o profile.yaml consolidado e sincroniza no DuckDB VSS.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: str = "llama3.2:3b",
        db_path: str = "data/open_job_loop.duckdb",
        mock_mode: bool = False,
    ):
        self.base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self.model = model
        self.db_path = db_path
        self.mock_mode = mock_mode
        self.client = AsyncOpenAI(base_url=self.base_url, api_key="ollama")

    def extract_from_pdf(self, pdf_path: str) -> str:
        """Extrai texto bruto de um PDF de perfil exportado do LinkedIn."""
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"Arquivo PDF '{pdf_path}' não encontrado.")

        reader = pypdf.PdfReader(str(path))
        text_parts = []
        for idx, page in enumerate(reader.pages):
            content = page.extract_text()
            if content:
                text_parts.append(content)
        return "\n\n".join(text_parts).strip()

    async def extract_from_url(
        self,
        url: str,
        state_file: str = "data/linkedin_state.json",
        headless: bool = True,
    ) -> str:
        """Navega até o perfil do LinkedIn do usuário usando sessão salva e extrai o texto principal."""
        actual_state_file = None
        for candidate_path in [state_file, "linkedin_state.json", "data/linkedin_state.json"]:
            if Path(candidate_path).exists():
                actual_state_file = candidate_path
                break

        async with async_playwright() as p:
            if actual_state_file:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context(storage_state=actual_state_file)
            else:
                logger.warning(
                    "Nenhuma sessão salva encontrada para extração de URL. Tentando acesso anônimo."
                )
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()

            page = await context.new_page()
            logger.info(f"Acessando perfil LinkedIn em: {url}")
            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(3000)

            # Extração de seções principais
            body_text = (
                await page.inner_text("main")
                if await page.locator("main").count() > 0
                else await page.inner_text("body")
            )

            # Acessar overlay de informações de contato para extrair e-mail, telefone e portfolio
            contact_text = ""
            clean_url = url.split("?")[0].rstrip("/")
            contact_url = f"{clean_url}/overlay/contact-info/"
            try:
                logger.info(f"Acessando overlay de contato em: {contact_url}")
                await page.goto(contact_url, timeout=15000)
                await page.wait_for_timeout(2000)
                contact_text = await page.inner_text("body")
            except Exception as e:
                logger.warning(f"Não foi possível acessar overlay de contato: {e}")

            await browser.close()
            return f"{body_text}\n\n--- CONTACT INFO ---\n{contact_text}".strip()

    async def analyze_profile(self, raw_text: str) -> Dict[str, Any]:
        """
        Usa o modelo local para analisar o perfil do LinkedIn, extrair atributos e
        formular a recomendação de Perfil de Vaga Alvo + perguntas adaptativas.
        """
        if self.mock_mode or len(raw_text.strip()) == 0:
            return {
                "candidate": {
                    "name": "Mauricio Helfstein",
                    "title": "Staff Data Engineer / Tech Lead",
                    "years_of_experience": 10,
                    "location": "São Paulo, Brazil",
                    "summary": "Staff Data Engineer with 10+ years of experience in distributed data pipelines, Python, Spark, and DuckDB.",
                },
                "target_profile": {
                    "target_roles": [
                        "Staff Data Engineer",
                        "Lead Data Platform Engineer",
                        "Principal Data Architect",
                    ],
                    "seniority_level": "Staff / Lead",
                    "suggested_keywords": "Staff Data Engineer Python Spark",
                    "estimated_salary_range": "$130k - $160k USD / year",
                    "key_strengths": [
                        "Python & Async architectures",
                        "DuckDB & Apache Spark",
                        "Distributed streaming systems",
                    ],
                    "suggested_questions": [
                        {
                            "id": "target_roles",
                            "question": "Os cargos sugeridos acima (Staff Data Engineer / Lead Data Platform) estão alinhados ao que você busca?",
                            "default": "Sim, foco em Staff Data Engineer e Lead Data Platform.",
                        },
                        {
                            "id": "salary",
                            "question": "Qual é a sua faixa de pretensão salarial pretendida (em USD anual ou BRL mensal)?",
                            "default": "$140,000 USD / ano (ou R$ 38.000 BRL / mês PJ)",
                        },
                        {
                            "id": "work_model",
                            "question": "Qual o seu modelo de trabalho preferido (100% Remoto, Híbrido, Aberto a Relocation)?",
                            "default": "100% Remoto",
                        },
                        {
                            "id": "visa",
                            "question": "Você possui cidadania/visto de trabalho ou necessita de patrocínio (Visa Sponsorship) para vagas nos EUA/Europa?",
                            "default": "Brasileiro nativo; necessito de sponsorship para EUA/Europa.",
                        },
                    ],
                },
            }

        prompt = f"""You are an elite Tech Career Coach & Profiler Agent.
Analyze the following raw LinkedIn profile text of a candidate:
---
{raw_text[:6000]}
---

Extract the candidate's core details and synthesize a Target Job Profile (ideal roles, seniority, keywords, salary benchmark) along with 3 to 4 adaptive grill-me questions to calibrate their preferences.

Return STRICT JSON matching this structure:
{{
  "candidate": {{
    "name": "string",
    "title": "string",
    "years_of_experience": 10,
    "location": "string",
    "summary": "string"
  }},
  "target_profile": {{
    "target_roles": ["Role 1", "Role 2"],
    "seniority_level": "Senior / Staff",
    "suggested_keywords": "string",
    "estimated_salary_range": "string",
    "key_strengths": ["Strength 1", "Strength 2"],
    "suggested_questions": [
      {{
        "id": "roles",
        "question": "text",
        "default": "text"
      }},
      {{
        "id": "salary",
        "question": "text",
        "default": "text"
      }},
      {{
        "id": "work_model",
        "question": "text",
        "default": "text"
      }},
      {{
        "id": "visa",
        "question": "text",
        "default": "text"
      }}
    ]
  }}
}}
Output ONLY valid JSON. Zero conversational text.
"""
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a structured JSON extraction engine."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            content = resp.choices[0].message.content.strip()
            # Clean markdown codeblocks if present
            if "```json" in content:
                content = content.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in content:
                content = content.split("```", 1)[1].split("```", 1)[0].strip()
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                parsed = {}
            if "candidate" not in parsed:
                parsed["candidate"] = {}

            # Extração determinística de dados de contato do texto bruto
            email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", raw_text)
            if email_match:
                parsed["candidate"]["email"] = email_match.group(0)

            phone_match = re.search(r"(\+?55\s?\(?\d{2}\)?\s?\d{4,5}[-\s]?\d{4})", raw_text)
            if phone_match:
                parsed["candidate"]["phone"] = phone_match.group(0).strip()

            linkedin_match = re.search(r"linkedin\.com/in/[\w\-]+", raw_text)
            if linkedin_match:
                parsed["candidate"]["linkedin_url"] = f"https://www.{linkedin_match.group(0)}"

            github_match = re.search(r"github\.com/[\w\-]+", raw_text)
            if github_match:
                parsed["candidate"]["github_url"] = f"https://{github_match.group(0)}"

            return parsed
        except Exception as e:
            logger.warning(f"Erro ao analisar perfil via LLM ({e}). Usando fallback heurístico.")
            return {
                "candidate": {
                    "name": "Candidato",
                    "title": "Senior Engineer",
                    "years_of_experience": 7,
                    "location": "Brasil",
                    "summary": raw_text[:300].strip(),
                },
                "target_profile": {
                    "target_roles": ["Senior Software Engineer", "Staff Engineer"],
                    "seniority_level": "Senior / Staff",
                    "suggested_keywords": "Senior Python Engineer",
                    "estimated_salary_range": "$120k - $150k",
                    "key_strengths": ["Software Engineering"],
                    "suggested_questions": [
                        {
                            "id": "roles",
                            "question": "Quais cargos você deseja buscar?",
                            "default": "Senior Software Engineer",
                        },
                        {
                            "id": "salary",
                            "question": "Qual sua pretensão salarial?",
                            "default": "$130,000 USD / ano",
                        },
                    ],
                },
            }

    def compile_profile_yaml(
        self,
        analysis: Dict[str, Any],
        answers: Dict[str, str],
        output_path: str = "data/profile.yaml",
    ) -> Dict[str, Any]:
        """Consolida os dados brutos e as respostas do grill-me em data/profile.yaml."""
        cand = analysis.get("candidate", {})
        target = analysis.get("target_profile", {})

        profile_data = {
            "candidate": {
                "name": cand.get("name", "Candidato"),
                "email": answers.get("email") or cand.get("email") or "candidato@example.com",
                "phone": answers.get("phone") or cand.get("phone") or "+55 11 99999-9999",
                "location": answers.get("location") or cand.get("location") or "Brasil",
                "title": cand.get("title", "Staff Data Engineer"),
                "years_of_experience": cand.get("years_of_experience", 10),
                "linkedin_url": cand.get("linkedin_url", ""),
                "github_url": cand.get("github_url", ""),
            },
            "target_job_profile": {
                "roles": target.get("target_roles", []),
                "seniority": target.get("seniority_level", "Staff"),
                "search_keywords": answers.get(
                    "search_keywords", target.get("suggested_keywords", "")
                ),
                "salary_target": answers.get("salary", target.get("estimated_salary_range", "")),
                "work_preference": answers.get("work_model", "100% Remoto"),
            },
            "work_authorization": {
                "details": answers.get(
                    "visa", "Brasileiro; necessita de sponsorship para EUA/Europa"
                ),
                "authorized_in_brazil": True,
                "requires_sponsorship_us": "sponsorship" in answers.get("visa", "").lower(),
            },
            "summary": cand.get("summary", ""),
            "skills": {
                "core_strengths": target.get("key_strengths", []),
            },
            "standard_answers": [
                {
                    "question": "What is your target role or desired positions?",
                    "answer": ", ".join(target.get("target_roles", [])),
                },
                {
                    "question": "What are your salary or compensation expectations?",
                    "answer": answers.get("salary", target.get("estimated_salary_range", "")),
                },
                {
                    "question": "What is your preferred work arrangement?",
                    "answer": answers.get("work_model", "Remote"),
                },
                {
                    "question": "Will you require visa sponsorship?",
                    "answer": "Yes" if "sponsorship" in answers.get("visa", "").lower() else "No",
                },
                {
                    "question": "How many years of relevant experience do you have?",
                    "answer": str(cand.get("years_of_experience", 10)),
                },
            ],
        }

        # Salva o arquivo no disco
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(profile_data, f, allow_unicode=True, sort_keys=False)

        logger.info(f"Perfil consolidado salvo em {output_path}")
        return profile_data
