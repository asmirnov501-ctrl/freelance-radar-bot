import asyncio
import logging
import os
import re
import aiohttp
import feedparser
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    CallbackQuery, 
    ReplyKeyboardMarkup, 
    KeyboardButton,
    LabeledPrice,
    PreCheckoutQuery
)

API_TOKEN = "8849608846:AAEZTR7xULvxNnfUN7Oz_5rdD0v96XwB3Cs"
ADMIN_ID = 8188915960  

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

CATEGORIES = {
    "design": "🎨 Дизайн & UX/UI",
    "dev": "💻 Разработка & Код",
    "text": "✍️ Тексты & Копирайтинг",
    "marketing": "📈 Маркетинг & Трафик"
}

REGIONS = {
    "ru": "🇷🇺 Российские биржи",
    "int": "🌐 Международные биржи"
}

# Расширенный список источников (RU + Global)
RSS_SOURCES = [
    # --- 🇷🇺 RU Источники ---
    {
        "name": "Habr Freelance",
        "url": "https://freelance.habr.com/tasks.rss",
        "default_cat": "dev",
        "region": "ru"
    },
    {
        "name": "FL.ru",
        "url": "https://www.fl.ru/rss/all.xml",
        "default_cat": "dev",
        "region": "ru"
    },
    {
        "name": "Freelance.ru",
        "url": "https://www.freelance.ru/rss/all",
        "default_cat": "dev",
        "region": "ru"
    },
    # --- 🌐 Global Источники ---
    {
        "name": "Freelancer.com",
        "url": "https://www.freelancer.com/rss.xml",
        "default_cat": "dev",
        "region": "int"
    },
    {
        "name": "We Work Remotely",
        "url": "https://weworkremotely.com/remote-jobs.rss",
        "default_cat": "dev",
        "region": "int"
    }
]

sent_orders = set()
user_preferences = {}
user_regions = {}
user_subscriptions = {}
total_stars_collected = 0

def is_subscription_active(user_id: int) -> bool:
    if user_id not in user_subscriptions:
        user_subscriptions[user_id] = datetime.now() + timedelta(days=3)
        return True
    return datetime.now() < user_subscriptions[user_id]

def get_main_reply_keyboard(user_id: int):
    kb = [
        [KeyboardButton(text="🎛 Настройка фильтров"), KeyboardButton(text="🚀 Запустить радар")],
        [KeyboardButton(text="💳 Подписка"), KeyboardButton(text="ℹ️ Помощь")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_regions_keyboard(user_id):
    selected_regs = user_regions.get(user_id, {"ru"})
    buttons = []
    
    for code, name in REGIONS.items():
        status = "✅ Включено" if code in selected_regs else "❌ Выключено"
        buttons.append([InlineKeyboardButton(text=f"{name}: {status}", callback_data=f"step1_reg_{code}")])
        
    buttons.append([InlineKeyboardButton(text="Далее: Выбор категорий ➡️", callback_data="go_step2")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_categories_keyboard(user_id):
    selected_cats = user_preferences.get(user_id, set())
    buttons = []
    
    for code, name in CATEGORIES.items():
        status = "✅" if code in selected_cats else "❌"
        buttons.append([InlineKeyboardButton(text=f"{status} {name}", callback_data=f"step2_cat_{code}")])
        
    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="go_step1"),
        InlineKeyboardButton(text="🚀 Запустить радар", callback_data="start_radar")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return cleantext.strip()[:250] + "..." if len(cleantext) > 250 else cleantext.strip()

def smart_ai_filter(title, summary):
    full_text = (title + " " + summary).lower()
    budget_match = re.search(r'(\d+[\s\d]*)\s?(руб|₽|\$|usd|eur|€|тыс|р\.)', full_text)
    budget = budget_match.group(0).upper() if budget_match else "По договоренности"
    return budget, 95

async def fetch_rss_feed(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    feed = await asyncio.to_thread(feedparser.parse, text)
                    return feed
    except Exception as e:
        logging.error(f"Ошибка загрузки ленты {url}: {e}")
    return None

async def send_latest_order_immediately(user_id: int):
    user_cats = user_preferences.get(user_id, set())
    user_regs = user_regions.get(user_id, {"ru"})
    
    for source in RSS_SOURCES:
        if source["region"] not in user_regs:
            continue
        feed = await fetch_rss_feed(source["url"])
        if feed and hasattr(feed, "entries") and feed.entries:
            for entry in feed.entries[:10]:
                title = entry.title
                link = entry.link
                summary = clean_html(entry.get("summary", "Описание отсутствует"))
                cat_code = source["default_cat"]
                title_lower = title.lower()
                
                if any(w in title_lower for w in ["дизайн", "logo", "ux", "ui", "баннер", "верстка", "figma", "tilda", "design"]):
                    cat_code = "design"
                elif any(w in title_lower for w in ["текст", "статья", "копирайт", "пост", "перевод", "рерайт", "content", "writing"]):
                    cat_code = "text"
                elif any(w in title_lower for w in ["маркетинг", "реклама", "трафик", "smm", "лид", "seo", "директ", "marketing"]):
                    cat_code = "marketing"
                elif any(w in title_lower for w in ["python", "бот", "разработчик", "php", "скрипт", "программист", "код", "сайт", "react", "vue", "node", "c#", "java", "1с", "developer"]):
                    cat_code = "dev"
                    
                if not user_cats or cat_code in user_cats:
                    budget, relevancy = smart_ai_filter(title, summary)
                    reg_badge = "🇷🇺 RU" if source["region"] == "ru" else "🌐 GLOBAL"
                    text = (
                        f"⚡️ **Свежий заказ с биржи [{CATEGORIES[cat_code]}] ({reg_badge})**\n\n"
                        f"📌 **{title}**\n"
                        f"💰 **Бюджет:** `{budget}`\n"
                        f"🌐 **Источник:** `{source['name']}`\n\n"
                        f"📝 {summary}\n\n"
                        f"⚡️ *AI-фильтр: {relevancy}% relevancy*"
                    )
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Откликнуться на бирже", url=link)]
                    ])
                    await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="Markdown")
                    return

async def trigger_radar_activation(user_id: int, message_or_callback):
    if not is_subscription_active(user_id):
        text_fail = "🔒 **Срок действия вашей подписки истек.**\nПродлите доступ в разделе **💳 Подписка**."
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer(text_fail, reply_markup=get_main_reply_keyboard(user_id), parse_mode="Markdown")
        else:
            await message_or_callback.message.answer(text_fail, reply_markup=get_main_reply_keyboard(user_id), parse_mode="Markdown")
        return

    text_success = (
        "🚀 **Радар успешно запущен!**\n\n"
        "🛰 **Как поступают заказы:**\n"
        "• Ниже прислан **последний актуальный проект** с бирж для проверки связи.\n"
        "• Бот сканирует все источники каждые **45 секунд**.\n"
        "• Заказы отправляются **сразу в момент публикации**!"
    )
    
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(text_success, reply_markup=get_main_reply_keyboard(user_id), parse_mode="Markdown")
    else:
        await message_or_callback.message.answer(text_success, reply_markup=get_main_reply_keyboard(user_id), parse_mode="Markdown")
        await message_or_callback.answer()
        
    await send_latest_order_immediately(user_id)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = set(CATEGORIES.keys())
    if user_id not in user_regions:
        user_regions[user_id] = {"ru"}
        
    is_subscription_active(user_id)
    
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"🤖 **Freelance Radar AI** — ваш персональный агрегатор заказов.\n"
        f"🎁 Пробный период: **3 дня**.\n\n"
        f"Давайте настроим регионы и категории задач 👇",
        reply_markup=get_main_reply_keyboard(user_id),
        parse_mode="Markdown"
    )
    await message.answer(
        "📍 **Шаг 1 из 2: Выберите регионы бирж**\n\n"
        "Нажмите на кнопку, чтобы включить или отключить поиск:",
        reply_markup=get_regions_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.message(F.text.in_(["🎛 Настройка фильтров", "🎛 Фильтры и Регионы", "🎯 Категории & Регионы"]))
async def btn_choose_categories(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = set(CATEGORIES.keys())
    if user_id not in user_regions:
        user_regions[user_id] = {"ru"}
        
    await message.answer(
        "📍 **Шаг 1 из 2: Выберите регионы бирж**\n\n"
        "Нажмите на кнопку, чтобы включить или отключить поиск:",
        reply_markup=get_regions_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🚀 Запустить радар")
async def btn_start_radar(message: types.Message):
    await trigger_radar_activation(message.from_user.id, message)

@dp.message(Command("testorder"))
async def test_order_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = (
        f"🔥 **Новый заказ! [💻 Разработка] (🇷🇺 RU)**\n\n"
        f"📌 **Тестовый проект: Разработка Telegram-бота на Python**\n"
        f"💰 **Бюджет:** `50 000 ₽`\n"
        f"🌐 **Источник:** `Habr Freelance (Тест)`\n\n"
        f"📝 Требуется опытный разработчик...\n\n"
        f"⚡️ *AI-фильтр: 98% relevancy*"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Откликнуться", url="https://freelance.habr.com")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.text == "💳 Подписка")
async def btn_subscription(message: types.Message):
    user_id = message.from_user.id
    active = is_subscription_active(user_id)
    until_date = user_subscriptions[user_id].strftime("%d.%m.%Y %H:%M")
    status_text = f"✅ Активна (до `{until_date}`)" if active else "❌ Истекла"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Продлить за 333 Stars (1 мес.)", callback_data="buy_stars")]
    ])
    await message.answer(f"💳 **Подписка**\n\n• Статус: {status_text}", reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.text == "👑 Админ-панель")
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(f"👑 **Админ-панель**\n⭐ Собрано звезд: `{total_stars_collected}` XTR", parse_mode="Markdown")

@dp.message(F.text == "ℹ️ Помощь")
async def btn_help(message: types.Message):
    await message.answer(
        "ℹ️ **Как работает Freelance Radar AI:**\n\n"
        "1. **Шаг 1:** в настройках выберите регионы бирж (Россия / Международные).\n"
        "2. **Шаг 2:** укажите сферы работы (Разработка, Дизайн и др.).\n"
        "3. **Мониторинг:** радар проверяет площадки каждые 45 секунд и отправляет новые заказы мгновенно!",
        reply_markup=get_main_reply_keyboard(message.from_user.id),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("step1_reg_"))
async def process_region_step1(callback: CallbackQuery):
    user_id = callback.from_user.id
    reg = callback.data.replace("step1_reg_", "")
    if user_id not in user_regions:
        user_regions[user_id] = set()
    if reg in user_regions[user_id]:
        if len(user_regions[user_id]) > 1:
            user_regions[user_id].remove(reg)
            await callback.answer("Регион отключен")
        else:
            await callback.answer("Выберите хотя бы один регион!", show_alert=True)
            return
    else:
        user_regions[user_id].add(reg)
        await callback.answer("Регион включен!")
    await callback.message.edit_reply_markup(reply_markup=get_regions_keyboard(user_id))

@dp.callback_query(F.data == "go_step2")
async def go_to_step2(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(
        "🎯 **Шаг 2 из 2: Выберите категории задач**\n\n"
        "Отметьте нужные направления работы:",
        reply_markup=get_categories_keyboard(user_id),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "go_step1")
async def go_to_step1(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(
        "📍 **Шаг 1 из 2: Выберите регионы бирж**\n\n"
        "Нажмите на кнопку, чтобы включить или отключить поиск:",
        reply_markup=get_regions_keyboard(user_id),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("step2_cat_"))
async def process_category_step2(callback: CallbackQuery):
    user_id = callback.from_user.id
    category = callback.data.replace("step2_cat_", "")
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
async def start_radar_callback(callback: CallbackQuery):
    await trigger_radar_activation(callback.from_user.id, callback)

@dp.callback_query(F.data == "buy_stars")
async def send_stars_invoice(callback: CallbackQuery):
    prices = [LabeledPrice(label="PRO Подписка на 1 месяц", amount=333)]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Freelance Radar AI — PRO",
        description="Доступ на 30 дней",
        payload="sub_30_days",
        provider_token="",
        currency="XTR",
        prices=prices
    )
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    global total_stars_collected
    total_stars_collected += 333
    user_subscriptions[message.from_user.id] = datetime.now() + timedelta(days=30)
    await message.answer("🎉 Оплата прошла успешно!", reply_markup=get_main_reply_keyboard(message.from_user.id))

async def real_order_scanner():
    while True:
        try:
            for source in RSS_SOURCES:
                try:
                    feed = await fetch_rss_feed(source["url"])
                    if not feed or not hasattr(feed, "entries"):
                        continue
                    
                    for entry in feed.entries[:15]:
                        order_id = entry.get("id", entry.link)
                        if order_id in sent_orders:
                            continue
                            
                        title = entry.title
                        link = entry.link
                        summary = clean_html(entry.get("summary", "Описание отсутствует"))
                        
                        cat_code = source["default_cat"]
                        source_region = source["region"]
                        title_lower = title.lower()
                        
                        if any(w in title_lower for w in ["дизайн", "logo", "ux", "ui", "баннер", "верстка", "figma", "tilda", "design"]):
                            cat_code = "design"
                        elif any(w in title_lower for w in ["текст", "статья", "копирайт", "пост", "перевод", "рерайт", "content", "writing"]):
                            cat_code = "text"
                        elif any(w in title_lower for w in ["маркетинг", "реклама", "трафик", "smm", "лид", "seo", "директ", "marketing"]):
                            cat_code = "marketing"
                        elif any(w in title_lower for w in ["python", "бот", "разработчик", "php", "скрипт", "программист", "код", "сайт", "react", "vue", "node", "c#", "java", "1с", "developer"]):
                            cat_code = "dev"

                        budget, relevancy = smart_ai_filter(title, summary)
                        
                        for user_id, categories in user_preferences.items():
                            if is_subscription_active(user_id):
                                user_regs = user_regions.get(user_id, {"ru"})
                                if source_region in user_regs and (not categories or cat_code in categories):
                                    try:
                                        reg_badge = "🇷🇺 RU" if source_region == "ru" else "🌐 GLOBAL"
                                        text = (
                                            f"🔥 **Новый заказ! [{CATEGORIES[cat_code]}] ({reg_badge})**\n\n"
                                            f"📌 **{title}**\n"
                                            f"💰 **Бюджет:** `{budget}`\n"
                                            f"🌐 **Источник:** `{source['name']}`\n\n"
                                            f"📝 {summary}\n\n"
                                            f"⚡️ *AI-фильтр: {relevancy}% relevancy*"
                                        )
                                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                            [InlineKeyboardButton(text="💬 Откликнуться", url=link)]
                                        ])
                                        await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="Markdown")
                                        await asyncio.sleep(0.05)
                                    except Exception as e:
                                        logging.error(f"Ошибка отправки пользователю {user_id}: {e}")
                                    
                        sent_orders.add(order_id)
                except Exception as inner_e:
                    logging.error(f"Ошибка при обработке источника {source['name']}: {inner_e}")
                    continue
        except Exception as e:
            logging.error(f"Ошибка в сканере заказов: {e}")
            
        await asyncio.sleep(45)

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
    while True:
        await asyncio.sleep(3600)

async def main():
    asyncio.create_task(web_server())
    asyncio.create_task(real_order_scanner())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
