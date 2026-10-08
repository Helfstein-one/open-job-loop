import os
import asyncio
import logging
from typing import List, Dict
import duckdb
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

class ProfileManager:
    """
    Gerencia o currículo e dados da entrevista do usuário, transformando em Embeddings 
    e salvando no DuckDB VSS para o bot Playwright consultar.
    """
    def __init__(self, db_path: str = "open_job_loop.duckdb", embedding_model: str = "nomic-embed-text"):
        self.db_path = db_path
        self.embedding_model = embedding_model
        # Usando a API compatível com OpenAI fornecida pelo Ollama local
        self.client = AsyncOpenAI(base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), api_key="ollama")
        
    def _get_connection(self):
        conn = duckdb.connect(self.db_path, read_only=False)
        conn.execute("INSTALL vss; LOAD vss;")
        conn.execute("SET hnsw_enable_experimental_persistence=true;")
        return conn

    def initialize_schema(self):
        """Cria as tabelas e o índice VSS para busca vetorial."""
        conn = self._get_connection()
        # A extensão VSS do DuckDB usa FLOAT[N] para arrays de tamanho fixo
        # nomic-embed-text usa 768 dimensões
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                id UUID DEFAULT uuid(),
                category VARCHAR,
                content TEXT,
                embedding FLOAT[768]
            );
        """)
        
        # Criação do índice HNSW para buscas ultra rápidas
        # Dropamos se existir para garantir atualização
        try:
            conn.execute("DROP INDEX IF EXISTS idx_profile_emb;")
            conn.execute("""
                CREATE INDEX idx_profile_emb ON user_profile USING HNSW (embedding)
                WITH (metric = 'cosine');
            """)
        except Exception as e:
            logger.warning(f"Aviso ao criar índice HNSW: {e}")
            
        conn.close()

    async def get_embedding(self, text: str) -> List[float]:
        """Chama o Ollama local para gerar o vetor do texto."""
        response = await self.client.embeddings.create(
            model=self.embedding_model,
            input=text
        )
        return response.data[0].embedding

    async def add_resume_chunk(self, category: str, content: str):
        """Adiciona um trecho de experiência ou habilidade no banco."""
        emb = await self.get_embedding(content)
        
        conn = self._get_connection()
        # Inserimos o vetor castado corretamente para o array float do DuckDB
        conn.execute(
            "INSERT INTO user_profile (category, content, embedding) VALUES (?, ?, ?::FLOAT[768])",
            (category, content, emb)
        )
        conn.execute("CHECKPOINT;")
        conn.close()
        logger.info(f"Chunk '{category}' adicionado ao VSS com sucesso.")

    async def search_profile(self, query: str, limit: int = 3) -> List[Dict]:
        """
        Dada uma pergunta do formulário do LinkedIn (ex: 'How many years of Python?'),
        busca no banco a parte do currículo que responde isso.
        """
        query_emb = await self.get_embedding(query)
        
        conn = self._get_connection()
        # VSS no DuckDB: array_distance ou list_cosine_distance dependendo da versão
        # Com o index criado, podemos usar a métrica HNSW com ORDER BY
        res = conn.execute("""
            SELECT category, content, list_cosine_distance(embedding, ?::FLOAT[768]) as dist
            FROM user_profile
            ORDER BY dist ASC
            LIMIT ?;
        """, (query_emb, limit)).fetchall()
        conn.close()
        
        results = []
        for row in res:
            results.append({"category": row[0], "content": row[1], "distance": row[2]})
        return results

if __name__ == "__main__":
    # Teste rápido de sanidade
    logging.basicConfig(level=logging.INFO)
    manager = ProfileManager()
    manager.initialize_schema()
    print("Schema do VSS inicializado com sucesso!")
