import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

API_TOKEN = "8849608846:AAGXWPStgM1ClXTQbkONyAneInYk0_mxF3g"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

user_preferences = {}

def get_categories_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🎨 Дизайн & UX/UI", callback_data="cat_design")],
        [InlineKeyboardButton(text="💻 Разработка & Код", callback_data="cat_dev")],
        [InlineKeyboardButton(text="✍️ Тексты & Копирайтинг", callback_data="cat_text")],
        [InlineKeyboardButton(text="📈 Маркетинг & Трафик", callback_data="cat_marketing")],
        [InlineKeyboardButton(text="✅ Готово / Запустить радар", callback_data="start_radar")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = []
        
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"Я **Freelance Radar AI** — ищу свежие заказы на биржах и в Telegram-каналах.\n"
        f"Я отсекаю спам и присылаю только целевые проекты.\n\n"
        f"**Выбери свои направления работы:**",
        reply_markup=get_categories_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("cat_"))
async def process_category(callback: CallbackQuery):
    user_id = callback.from_user.id
    category = callback.data.replace("cat_", "")
    
    if user_id not in user_preferences:
        user_preferences[user_id] = []
        
    if category in user_preferences[user_id]:
        user_preferences[user_id].remove(category)
        await callback.answer("Категория удалена")
    else:
        user_preferences[user_id].append(category)
        await callback.answer("Категория добавлена!")

@dp.callback_query(F.data == "start_radar")
async def start_radar(callback: CallbackQuery):
    user_id = callback.from_user.id
    selected = user_preferences.get(user_id, [])
    
    if not selected:
        await callback.answer("Пожалуйста, выберите хотя бы одну категорию!", show_alert=True)
        return
        
    await callback.message.edit_text(
        "🚀 **Радар успешно запущен!**\n\n"
        "Как только появится подходящий заказ, я сразу пришлю его сюда.\n"
        "Первые 3 дня доступа — **БЕСПЛАТНО**.",
        parse_mode="Markdown"
    )

# Мини-сервер для бесплатного тарифа Render Web Service
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
    await web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

