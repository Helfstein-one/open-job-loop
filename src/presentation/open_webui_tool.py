"""
title: Open-Job-Loop Tools (Docker)
description: Ferramenta de integracao com o container job-bot para busca de vagas, relatorios e curriculo vetorial.
author: Antigravity
version: 1.1.0
"""
import urllib.request
import json

class Tools:
    def __init__(self):
        # A URL aponta para o container do job-bot na rede do docker-compose
        self.api_url = "http://job-bot:8000"

    def start_discovery(self, keywords: str, limit: int = 5) -> str:
        """
        Dispara a busca de vagas e o auto-apply em background no container job-bot.
        
        :param keywords: Palavras-chave da vaga (ex: 'Data Engineer').
        :param limit: Numero maximo de vagas para buscar.
        :return: Mensagem de status.
        """
        req = urllib.request.Request(
            f"{self.api_url}/start",
            data=json.dumps({"keywords": keywords, "limit": limit}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                return f"✅ {data.get('status')}"
        except Exception as e:
            return f"❌ Erro ao contatar job-bot: {e}"

    def get_daily_report(self) -> str:
        """
        Retorna um relatorio atualizado das vagas processadas e aplicadas.
        
        :return: Relatorio formatado em Markdown.
        """
        try:
            with urllib.request.urlopen(f"{self.api_url}/report") as response:
                data = json.loads(response.read().decode())
                if "error" in data:
                    return f"Erro na base: {data['error']}"
                
                report_str = "### Relatório de Vagas:\n"
                for status, count in data["report"].items():
                    report_str += f"- **{status}**: {count}\n"
                return report_str
        except Exception as e:
            return f"❌ Erro ao contatar job-bot: {e}"

    def update_profile(self, category: str, content: str) -> str:
        """
        Adiciona parte do seu curriculo no banco vetorial (VSS) do bot para auto-apply.
        
        :param category: Categoria (ex: 'experiencia', 'habilidades', 'idiomas').
        :param content: O texto descritivo.
        :return: Mensagem de sucesso.
        """
        req = urllib.request.Request(
            f"{self.api_url}/profile",
            data=json.dumps({"category": category, "content": content}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                if "error" in data:
                    return f"Erro ao vetorizar: {data['error']}"
                return f"✅ {data.get('message')}"
        except Exception as e:
            return f"❌ Erro ao contatar job-bot: {e}"
