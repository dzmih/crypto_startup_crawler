import asyncio
import json
from playwright.async_api import async_playwright, TimeoutError
import ollama

PROFILES_TO_SCRAPE = [
    "Paradigm",
    "a16zcrypto",
    "Dragonfly_xyz"
]

async def filter_profile_with_ai(profile_data):
    prompt = f"""Проанализируй твиты и био. Найди технологические Web3 стартапы на ранней стадии. Отсеивай мемкоины, NFT и спам. Верни строго JSON: {{"is_valuable": true/false, "project_category": "string", "reason": "string"}}
    
Био: {profile_data['bio']}
Твиты: {profile_data['tweets']}
"""
    try:
        client = ollama.AsyncClient()
        response = await client.generate(
            model='llama3',
            prompt=prompt,
            format='json',
            stream=False
        )
        ai_response = json.loads(response['response'])
        return ai_response
    except Exception as e:
        print(f"Ошибка при работе с Ollama для {profile_data['username']}: {e}")
        return None

async def scrape_profile(context, username):
    print(f"Парсинг профиля: {username}...")
    page = await context.new_page()
    profile_data = {
        "username": username,
        "bio": "",
        "tweets": []
    }
    try:
        await page.goto(f"https://x.com/{username}", timeout=30000)
        
        # Ждем загрузки био
        try:
            bio_element = await page.wait_for_selector('[data-testid="UserBio"]', timeout=10000)
            if bio_element:
                profile_data['bio'] = await bio_element.inner_text()
        except TimeoutError:
            print(f"[{username}] Био не найдено или таймаут.")

        # Ждем загрузки твитов
        try:
            await page.wait_for_selector('[data-testid="tweetText"]', timeout=15000)
            tweet_elements = await page.query_selector_all('[data-testid="tweetText"]')
            # Берем первые 5
            for tweet in tweet_elements[:5]:
                text = await tweet.inner_text()
                profile_data['tweets'].append(text)
        except TimeoutError:
            print(f"[{username}] Твиты не найдены или таймаут.")
            
    except Exception as e:
        print(f"[{username}] Ошибка при парсинге: {e}")
    finally:
        await page.close()
        
    return profile_data

async def main():
    results = []
    async with async_playwright() as p:
        print("Запускаю браузер (headless=True)...")
        context = await p.chromium.launch_persistent_context(
            user_data_dir="twitter_profile",
            channel="chrome",
            headless=True, # Поставим True, можно изменить на False для отладки
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        for username in PROFILES_TO_SCRAPE:
            data = await scrape_profile(context, username)
            
            # Если удалось собрать хоть какие-то данные
            if data['bio'] or data['tweets']:
                print(f"[{username}] Анализ через Ollama...")
                ai_result = await filter_profile_with_ai(data)
                
                final_data = {
                    "scraped_data": data,
                    "ai_analysis": ai_result
                }
                results.append(final_data)
            else:
                print(f"[{username}] Недостаточно данных для парсинга, возможно блокировка или неверный селектор.")
                
        await context.close()
        
    print("\n--- ИТОГОВЫЙ РЕЗУЛЬТАТ ---")
    print(json.dumps(results, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
