# Usando a imagem oficial do Playwright para Python (já com dependências do SO)
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# Copia os arquivos de configuração de dependências
COPY pyproject.toml /app/

# Instala as dependências Python
RUN pip install --no-cache-dir -e .
RUN pip install fastapi uvicorn

# Copia o código fonte
COPY src /app/src

# Cria o diretório de dados
RUN mkdir -p /app/data

# O comando inicia o servidor FastAPI do nosso job-bot
CMD ["uvicorn", "src.presentation.api:app", "--host", "0.0.0.0", "--port", "8000"]
