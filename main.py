import asyncio
import logging
import os
import random
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

API_TOKEN = "8849608846:AAGXWPStgM1ClXTQbkONyAneInYk0_mxF3g"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Категории заказов
CATEGORIES = {
    "design": "🎨 Дизайн & UX/UI",
    "dev": "💻 Разработка & Код",
    "text": "✍️ Тексты & Копирайтинг",
    "marketing": "📈 Маркетинг & Трафик"
}

# Хранилище: {user_id: set("dev", "design")}
user_preferences = {}

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
        f"Я **Freelance Radar AI** — твой умный агрегатор заказов.\n\n"
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
        f"Я сканирую каналы и биржи. Как только появится заказ по твоим направлениям, я сразу пришлю его сюда!",
        parse_mode="Markdown"
    )

# 📡 Фоновый симулятор/парсер заказов
async def background_order_scanner():
    """Фоновая задача, которая имитирует поиск свежих заказов с бирж и каналов"""
    # База тестовых примеров заказов для демонстрации
    sample_orders = [
        {
            "cat": "dev",
            "title": "Разработка Telegram-бота на Python",
            "budget": "15 000 руб.",
            "source": "Telegram / Фриланс Биржа",
            "desc": "Нужен бот для приема заявок с интеграцией Google Sheets."
        },
        {
            "cat": "design",
            "title": "Redesign мобильного приложения (UX/UI)",
            "budget": "$500",
            "source": "behance / фриланс чат",
            "desc": "Требуется обновить интерфейс финтех приложения под iOS/Android."
        },
        {
            "cat": "text",
            "title": "Копирайтинг для сайта криптопроекта",
            "budget": "8 000 руб.",
            "source": "Telegram-канал с заказами",
            "desc": "Написать 3 продающих текста на главную страницу."
        },
        {
            "cat": "marketing",
            "title": "Настройка таргета в Telegram Ads",
            "budget": "30 000 руб.",
            "source": "Рекламный чат",
            "desc": "Ищем специалиста с опытом продвижения образовательных курсов."
        }
    ]

    while True:
        await asyncio.sleep(45) # Проверка каждые 45 секунд для теста
        
        if not user_preferences:
            continue
            
        # Выбираем случайный заказ
        order = random.choice(sample_orders)
        cat_code = order["cat"]
        
        # Рассылаем тем пользователям, у которых выбрана эта категория
        for user_id, categories in user_preferences.items():
            if cat_code in categories:
                try:
                    text = (
                        f"🔥 **Новый заказ! [{CATEGORIES[cat_code]}]**\n\n"
                        f"📌 **{order['title']}**\n"
                        f"💰 Бюджет: `{order['budget']}`\n"
                        f"🌐 Источник: {order['source']}\n\n"
                        f"📝 {order['desc']}\n\n"
                        f"⚡️ *AI-фильтр: Соответствует вашему профилю (98% relevancy)*"
                    )
                    
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Откликнуться", url="https://t.me/example")]
                    ])
                    
                    await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

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
    # Запускаем веб-сервер и фоновый сканер заказов вместе с ботом
    asyncio.create_task(web_server())
    asyncio.create_task(background_order_scanner())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())



