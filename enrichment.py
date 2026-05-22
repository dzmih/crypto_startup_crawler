import asyncio
import httpx
from bs4 import BeautifulSoup
import re
import json
import logging

GITHUB_REPO_REGEX = re.compile(r"github\.com/([^/]+)/([^/\s\?\#]+)")

async def fetch_website_data(url: str) -> str:
    """
    Переходит по ссылке, скачивает HTML, извлекает title, description и текст.
    """
    if not url:
        return ""
        
    try:
        # Используем таймаут и фолловинг редиректов (для t.co)
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            # Маскируемся под браузер
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            }
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Удаляем скрипты и стили
            for script in soup(["script", "style", "noscript"]):
                script.extract()
                
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            
            desc_meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
            desc = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""
            
            text = soup.get_text(separator=' ', strip=True)
            # Ограничиваем длину текста
            text = text[:2000]
            
            parts = []
            if title: parts.append(f"Title: {title}")
            if desc: parts.append(f"Description: {desc}")
            if text: parts.append(f"Content: {text}")
            
            return "\n".join(parts)
            
    except Exception as e:
        logging.debug(f"Failed to fetch website {url}: {e}")
        return ""

async def fetch_github_stats(owner: str, repo: str) -> str:
    """
    Запрашивает статистику репозитория через GitHub API.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "CryptoStartupRadar/1.0"
            }
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                stats = {
                    "stars": data.get("stargazers_count"),
                    "forks": data.get("forks_count"),
                    "language": data.get("language"),
                    "description": data.get("description"),
                    "updated_at": data.get("updated_at")
                }
                return json.dumps(stats, ensure_ascii=False)
            else:
                logging.debug(f"GitHub API error for {owner}/{repo}: {resp.status_code}")
                return ""
    except Exception as e:
        logging.debug(f"Failed to fetch github stats for {owner}/{repo}: {e}")
        return ""

async def enrich_project(external_url: str, bio_text: str) -> tuple[str, str]:
    """
    Собирает данные сайта и GitHub-статистику.
    Возвращает кортеж (website_text, github_stats).
    """
    website_text = ""
    github_stats = ""
    
    # 1. Парсим сайт
    if external_url:
        website_text = await fetch_website_data(external_url)
        
    # 2. Ищем GitHub ссылку в bio или на самом сайте
    combined_text = f"{external_url} {bio_text} {website_text}"
    github_match = GITHUB_REPO_REGEX.search(combined_text)
    
    if github_match:
        owner = github_match.group(1)
        repo = github_match.group(2)
        # Очищаем repo от возможных суффиксов
        repo = re.sub(r'[^a-zA-Z0-9_\-\.]', '', repo)
        if owner and repo:
            github_stats = await fetch_github_stats(owner, repo)
            
    return website_text, github_stats
