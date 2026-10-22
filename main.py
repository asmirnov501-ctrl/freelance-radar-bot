import asyncio
import logging
import os
import re
import feedparser
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

API_TOKEN = "8849608846:AAGXWPStgM1ClXTQbkONyAneInYk0_mxF3g"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Доступные категории
CATEGORIES = {
    "design": "🎨 Дизайн & UX/UI",
    "dev": "💻 Разработка & Код",
    "text": "✍️ Тексты & Копирайтинг",
    "marketing": "📈 Маркетинг & Трафик"
}

# Список реальных RSS-лент фриланс-источников
RSS_SOURCES = [
    {
        "name": "Habr Freelance",
        "url": "https://freelance.habr.com/tasks.rss",
        "default_cat": "dev"
    }
]

# Множество для хранения ID уже отправленных заказов (чтобы не дублировать)
sent_orders = set()
user_preferences = {}

def clean_html(raw_html):
    """Очистка текста от HTML-тегов"""
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return cleantext.strip()[:300] + "..." if len(cleantext) > 300 else cleantext.strip()

def get_categories_keyboard(user_id):
    selected = user_preferences.get(user_id, set())
    buttons = []
    
    for code, name in CATEGORIES.items():
        prefix = "✅ " if code in selected else "▫️ "
        buttons.append([InlineKeyboardButton(text=f"{prefix}{name}", callback_data=f"cat_{code}")])
        
    buttons.append([InlineKeyboardButton(text="🚀 Запустить радар", callback_data="start_radar")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = set()
        
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"Я **Freelance Radar AI** — твой умный агрегатор реальных заказов.\n\n"
        f"🎯 **Выбери категории для отслеживания:**",
        reply_markup=get_categories_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("cat_"))
async def process_category(callback: CallbackQuery):
    user_id = callback.from_user.id
    category = callback.data.replace("cat_", "")
    
    if user_id not in user_preferences:
        user_preferences[user_id] = set()
        
    if category in user_preferences[user_id]:
        user_preferences[user_id].remove(category)
        await callback.answer("Категория убрана")
    else:
        user_preferences[user_id].add(category)
        await callback.answer("Категория добавлена!")
        
    await callback.message.edit_reply_markup(reply_markup=get_categories_keyboard(user_id))

@dp.callback_query(F.data == "start_radar")
async def start_radar(callback: CallbackQuery):
    user_id = callback.from_user.id
    selected = user_preferences.get(user_id, set())
    
    if not selected:
        await callback.answer("⚠️ Выберите хотя бы одну категорию!", show_alert=True)
        return
        
    selected_names = "\n".join([f"• {CATEGORIES[code]}" for code in selected])
    
    await callback.message.edit_text(
        f"🚀 **Радар успешно запущен!**\n\n"
        f"**Выбранные категории:**\n{selected_names}\n\n"
        f"🌐 Мониторинг бирж и каналов активен. Ожидайте свежие проекты...",
        parse_mode="Markdown"
    )

# 📡 Настоящий фоновый парсер заказов
async def real_order_scanner():
    while True:
        try:
            for source in RSS_SOURCES:
                feed = feedparser.parse(source["url"])
                
                for entry in feed.entries[:5]: # Берем последние 5 записей
                    order_id = entry.get("id", entry.link)
                    
                    if order_id in sent_orders:
                        continue
                        
                    title = entry.title
                    link = entry.link
                    summary = clean_html(entry.get("summary", "Описание отсутствует"))
                    
                    # Простейшая фильтрация по ключевым словам для категории
                    cat_code = source["default_cat"]
                    title_lower = title.lower()
                    if any(w in title_lower for w in ["дизайн", "logo", "ux", "ui", "баннер"]):
                        cat_code = "design"
                    elif any(w in title_lower for w in ["текст", "статья", "копирайт", "пост"]):
                        cat_code = "text"
                    elif any(w in title_lower for w in ["маркетинг", "реклама", "трафик", "smm"]):
                        cat_code = "marketing"
                    
                    # Рассылка подписчикам
                    for user_id, categories in user_preferences.items():
                        if cat_code in categories:
                            try:
                                text = (
                                    f"🔥 **Новый реальный заказ! [{CATEGORIES[cat_code]}]**\n\n"
                                    f"📌 **{title}**\n"
                                    f"🌐 Источник: `{source['name']}`\n\n"
                                    f"📝 {summary}\n\n"
                                    f"⚡️ *AI-Status: Настоящий заказ с биржи*"
                                )
                                
                                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="💬 Откликнуться на бирже", url=link)]
                                ])
                                
                                await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="Markdown")
                            except Exception as e:
                                logging.error(f"Ошибка отправки пользователю {user_id}: {e}")
                                
                    sent_orders.add(order_id)
        except Exception as e:
            logging.error(f"Ошибка при парсинге RSS: {e}")
            
        await asyncio.sleep(60) # Проверка каждые 60 секунд

# Мини-сервер для Render Web Service
async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    asyncio.create_task(web_server())
    asyncio.create_task(real_order_scanner())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
