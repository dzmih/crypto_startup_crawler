import asyncio
from playwright.async_api import async_playwright

async def main():
    print("Запуск браузера...")
    async with async_playwright() as p:
        # Запуск браузера с сохранением сессии в папку twitter_profile
        context = await p.chromium.launch_persistent_context(
            user_data_dir="twitter_profile",
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        # launch_persistent_context автоматически создает одну страницу (вкладку)
        pages = context.pages
        page = pages[0] if pages else await context.new_page()
        
        print("Открываю страницу логина X.com...")
        await page.goto("https://x.com/login")
        
        print("\n" + "="*50)
        print("ПОЖАЛУЙСТА, ВЫПОЛНИТЕ РУЧНОЙ ВХОД В АККАУНТ.")
        print("После успешной авторизации и загрузки ленты, просто закройте окно браузера.")
        print("="*50 + "\n")
        
        # Ждем, пока контекст или страница не будут закрыты
        try:
            while not page.is_closed():
                await asyncio.sleep(1)
        except Exception:
            pass

        print("Браузер закрыт. Сессия сохранена в папку twitter_profile.")

if __name__ == "__main__":
    asyncio.run(main())
