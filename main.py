import asyncio
import logging
import os
import re
import html
import sqlite3
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

API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8188915960"))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- DATABASE SETUP ---
DB_NAME = "users.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            sub_until TEXT,
            source TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            user_id INTEGER,
            category TEXT,
            PRIMARY KEY (user_id, category)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS regions (
            user_id INTEGER,
            region TEXT,
            PRIMARY KEY (user_id, region)
        )
    """)
    conn.commit()
    conn.close()

init_db()

def db_get_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, sub_until, source FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def db_register_user(user_id: int, source: str = "Прямой переход 🔍"):
    sub_until = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, sub_until, source) VALUES (?, ?, ?)",
        (user_id, sub_until, source)
    )
    for cat in ["design", "dev", "text", "marketing"]:
        cursor.execute("INSERT OR IGNORE INTO preferences (user_id, category) VALUES (?, ?)", (user_id, cat))
    cursor.execute("INSERT OR IGNORE INTO regions (user_id, region) VALUES (?, 'ru')", (user_id,))
    conn.commit()
    conn.close()

def is_subscription_active(user_id: int) -> bool:
    user = db_get_user(user_id)
    if not user:
        db_register_user(user_id)
        return True
    sub_until = datetime.strptime(user[1], "%Y-%m-%d %H:%M:%S")
    return datetime.now() < sub_until

def add_subscription_days(user_id: int, days: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT sub_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    now = datetime.now()
    if row:
        current_sub = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        new_sub = (current_sub + timedelta(days=days)) if current_sub > now else (now + timedelta(days=days))
    else:
        new_sub = now + timedelta(days=days)
    cursor.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (new_sub.strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

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

RSS_SOURCES = [
    {"name": "Habr Freelance", "url": "https://freelance.habr.com/tasks.rss", "default_cat": "dev", "region": "ru"},
    {"name": "FL.ru", "url": "https://www.fl.ru/rss/all.xml", "default_cat": "dev", "region": "ru"},
    {"name": "Freelance.ru", "url": "https://www.freelance.ru/rss/all", "default_cat": "dev", "region": "ru"},
    {"name": "Freelancer.com", "url": "https://www.freelancer.com/rss.xml", "default_cat": "dev", "region": "int"},
    {"name": "We Work Remotely", "url": "https://weworkremotely.com/remote-jobs.rss", "default_cat": "dev", "region": "int"},
    {"name": "Remotive", "url": "https://remotive.com/remote-jobs/feed", "default_cat": "dev", "region": "int"},
    {"name": "Jobspresso", "url": "https://jobspresso.co/feed/", "default_cat": "dev", "region": "int"}
]

sent_orders = set()
total_stars_collected = 0

def get_main_reply_keyboard(user_id: int):
    kb = [
        [KeyboardButton(text="🎛 Настройка фильтров"), KeyboardButton(text="🚀 Запустить радар")],
        [KeyboardButton(text="🎁 Бесплатный доступ"), KeyboardButton(text="ℹ️ Помощь")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_regions_keyboard(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT region FROM regions WHERE user_id = ?", (user_id,))
    selected_regs = {row[0] for row in cursor.fetchall()}
    conn.close()

    buttons = []
    for code, name in REGIONS.items():
        status = "✅ Включено" if code in selected_regs else "❌ Выключено"
        buttons.append([InlineKeyboardButton(text=f"{name}: {status}", callback_data=f"step1_reg_{code}")])
    buttons.append([InlineKeyboardButton(text="Далее: Выбор категорий ➡️", callback_data="go_step2")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_categories_keyboard(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM preferences WHERE user_id = ?", (user_id,))
    selected_cats = {row[0] for row in cursor.fetchall()}
    conn.close()

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
    return budget

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
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM preferences WHERE user_id = ?", (user_id,))
    user_cats = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT region FROM regions WHERE user_id = ?", (user_id,))
    user_regs = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    for source in RSS_SOURCES:
        if source["region"] not in user_regs:
            continue
        feed = await fetch_rss_feed(source["url"])
        if feed and hasattr(feed, "entries") and feed.entries:
            for entry in feed.entries[:10]:
                title = html.unescape(entry.title)
                link = entry.link
                summary = html.unescape(clean_html(entry.get("summary", "Описание отсутствует")))
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
                    budget = smart_ai_filter(title, summary)
                    reg_badge = "🇷🇺 RU" if source["region"] == "ru" else "🌐 GLOBAL"
                    text = (
                        f"⚡️ **Свежий заказ с биржи [{CATEGORIES[cat_code]}] ({reg_badge})**\n\n"
                        f"📌 **{title}**\n"
                        f"💰 **Бюджет:** `{budget}`\n"
                        f"🌐 **Источник:** `{source['name']}`\n\n"
                        f"📝 {summary}"
                    )
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Откликнуться на бирже", url=link)]
                    ])
                    await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="Markdown")
                    return

async def trigger_radar_activation(user_id: int, message_or_callback):
    if not is_subscription_active(user_id):
        text_fail = "🔒 **Срок действия вашей подписки истек.**\nПродлите доступ за рефералов или в разделе **🎁 Бесплатный доступ**."
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer(text_fail, reply_markup=get_main_reply_keyboard(user_id), parse_mode="Markdown")
        else:
            await message_or_callback.message.answer(text_fail, reply_markup=get_main_reply_keyboard(user_id), parse_mode="Markdown")
        return

    text_success = (
        "🚀 **Радар успешно запущен!**\n\n"
        "🛰 **Как поступают заказы:**\n"
        "• Ниже прислан **последний актуальный проект** с бирж для проверки связи.\n"
        "• Бот сканирует 7 крупных площадок каждые **45 секунд**.\n"
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
    user_data = db_get_user(user_id)
    
    args = message.text.split(maxsplit=1)
    source = "Прямой переход 🔍"
    
    if len(args) > 1:
        param = args[1].lower()
        if param.startswith("ref_"):
            try:
                referrer_id = int(param.replace("ref_", ""))
                if referrer_id != user_id:
                    conn = sqlite3.connect(DB_NAME)
                    cursor = conn.cursor()
                    cursor.execute("INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, user_id))
                    conn.commit()
                    conn.close()
                    source = "Реферал"
                    add_subscription_days(referrer_id, 7)
                    try:
                        await bot.send_message(
                            referrer_id,
                            f"🎉 **Новый реферал!**\n"
                            f"По вашей ссылке зарегистрировался новый пользователь.\n"
                            f"🎁 Вам начислено **+7 дней бесплатной подписки**!",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logging.error(f"Ошибка уведомления реферера {referrer_id}: {e}")
            except ValueError:
                pass
        elif param in ["tt", "tiktok"]:
            source = "TikTok 🎵"
        elif param in ["ig", "instagram"]:
            source = "Instagram 📸"
        elif param in ["yt", "youtube", "shorts"]:
            source = "YouTube Shorts ▶️"
        else:
            source = f"Кампания ({param})"

    if not user_data:
        db_register_user(user_id, source)

    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"🤖 **Freelance Radar AI** — ваш персональный агрегатор заказов.\n"
        f"🎁 Вам активирован пробный период на **3 дня**!\n\n"
        f"💡 **Пользуйтесь ботом бесплатно:**\n"
        f"Делитесь вашей ссылкой с коллегами и получайте **+7 дней PRO-доступа** за каждого пришедшего друга!",
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
    if not db_get_user(user_id):
        db_register_user(user_id)
        
    await message.answer(
        "📍 **Шаг 1 из 2: Выберите регионы бирж**\n\n"
        "Нажмите на кнопку, чтобы включить или отключить поиск:",
        reply_markup=get_regions_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🚀 Запустить радар")
async def btn_start_radar(message: types.Message):
    await trigger_radar_activation(message.from_user.id, message)

@dp.message(F.text.in_(["🎁 Бесплатный доступ", "💳 Подписка"]))
async def btn_subscription(message: types.Message):
    user_id = message.from_user.id
    active = is_subscription_active(user_id)
    user_data = db_get_user(user_id)
    
    until_date = user_data[1] if user_data else "3 дня"
    status_text = f"✅ Активна (до `{until_date}`)" if active else "❌ Истекла"
    
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
    ref_count = cursor.fetchone()[0]
    conn.close()

    share_text = f"https://t.me/share/url?url={ref_link}&text=Нашел%20крутого%20AI-бота,%20который%20собирает%20заказы%20со%20всех%20фриланс-бирж!"
    
    text = (
        f"🎁 **Партнерская программа & Доступ**\n\n"
        f"• **Статус подписки:** {status_text}\n\n"
        f"───\n\n"
        f"🚀 **Как пользоваться ботом бесплатно:**\n"
        f"Приглашайте коллег и друзей-фрилансеров! За каждого человека вы получаете **+7 дней PRO-доступа**.\n\n"
        f"🔗 **Ваша личная ссылка:**\n`{ref_link}`\n\n"
        f"📊 **Приглашено людей:** `{ref_count}`"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Поделиться ссылкой с другом", url=share_text)],
        [InlineKeyboardButton(text="⭐ Купить подписку за Stars (333 Stars)", callback_data="buy_stars")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(Command("testorder"))
async def test_order_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = (
        f"🔥 **Новый заказ! [💻 Разработка] (🇷🇺 RU)**\n\n"
        f"📌 **Тестовый проект: Разработка Telegram-бота на Python**\n"
        f"💰 **Бюджет:** `50 000 ₽`\n"
        f"🌐 **Источник:** `Habr Freelance (Тест)`\n\n"
        f"📝 Требуется опытный разработчик..."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Откликнуться", url="https://freelance.habr.com")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.text == "👑 Админ-панель")
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    conn.close()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_users")],
        [InlineKeyboardButton(text="📊 Источники переходов (UTM)", callback_data="admin_sources")]
    ])
    await message.answer(
        f"👑 **Админ-панель**\n\n"
        f"👥 Всего пользователей: `{total_users}`\n"
        f"⭐ Собрано звезд: `{total_stars_collected}` XTR", 
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_users")
async def admin_users_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, sub_until, source FROM users")
    rows = cursor.fetchall()
    
    msg = "👥 **Список пользователей:**\n\n"
    for r in rows:
        uid, sub_until, src = r[0], r[1], r[2]
        status = "✅ Active" if datetime.now() < datetime.strptime(sub_until, "%Y-%m-%d %H:%M:%S") else "❌ Expired"
        cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,))
        refs = cursor.fetchone()[0]
        msg += f"• ID: `{uid}` | {status} | Рефералов: `{refs}` | Откуда: `{src}`\n"
    conn.close()

    await callback.message.answer(msg, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_sources")
async def admin_sources_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT source, COUNT(*) FROM users GROUP BY source")
    rows = cursor.fetchall()
    conn.close()

    msg = "📊 **Статистика по источникам (UTM):**\n\n"
    if not rows:
        msg += "Пока нет данных о переходах."
    else:
        for src_name, count in rows:
            msg += f"• {src_name}: `{count}` чел.\n"
            
    await callback.message.answer(msg, parse_mode="Markdown")
    await callback.answer()

@dp.message(F.text == "ℹ️ Помощь")
async def btn_help(message: types.Message):
    await message.answer(
        "ℹ️ **Как работает Freelance Radar AI:**\n\n"
        "1. **Шаг 1:** Выберите регионы бирж (Россия / Международные).\n"
        "2. **Шаг 2:** Укажите сферы работы (Разработка, Дизайн и др.).\n"
        "3. **Мониторинг:** Бот проверяет 7 крупных бирж каждые 45 секунд и отправляет заказы сразу в момент их публикации!\n\n"
        "🎁 Жмите кнопку «🎁 Бесплатный доступ», чтобы приглашать друзей и получать подписку бесплатно!",
        reply_markup=get_main_reply_keyboard(message.from_user.id),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("step1_reg_"))
async def process_region_step1(callback: CallbackQuery):
    user_id = callback.from_user.id
    reg = callback.data.replace("step1_reg_", "")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT region FROM regions WHERE user_id = ?", (user_id,))
    regs = [r[0] for r in cursor.fetchall()]
    
    if reg in regs:
        if len(regs) > 1:
            cursor.execute("DELETE FROM regions WHERE user_id = ? AND region = ?", (user_id, reg))
            await callback.answer("Регион отключен")
        else:
            await callback.answer("Выберите хотя бы один регион!", show_alert=True)
            conn.close()
            return
    else:
        cursor.execute("INSERT INTO regions (user_id, region) VALUES (?, ?)", (user_id, reg))
        await callback.answer("Регион включен!")
    conn.commit()
    conn.close()
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
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM preferences WHERE user_id = ?", (user_id,))
    cats = [r[0] for r in cursor.fetchall()]
    
    if category in cats:
        cursor.execute("DELETE FROM preferences WHERE user_id = ? AND category = ?", (user_id, category))
        await callback.answer("Категория убрана")
    else:
        cursor.execute("INSERT INTO preferences (user_id, category) VALUES (?, ?)", (user_id, category))
        await callback.answer("Категория добавлена!")
    conn.commit()
    conn.close()
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
    add_subscription_days(message.from_user.id, 30)
    await message.answer("🎉 Оплата прошла успешно! Добавлено +30 дней.", reply_markup=get_main_reply_keyboard(message.from_user.id))

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
                            
                        title = html.unescape(entry.title)
                        link = entry.link
                        summary = html.unescape(clean_html(entry.get("summary", "Описание отсутствует")))
                        
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

                        budget = smart_ai_filter(title, summary)
                        
                        conn = sqlite3.connect(DB_NAME)
                        cursor = conn.cursor()
                        cursor.execute("SELECT user_id FROM users")
                        users = cursor.fetchall()
                        
                        for (u_id,) in users:
                            if is_subscription_active(u_id):
                                cursor.execute("SELECT region FROM regions WHERE user_id = ?", (u_id,))
                                u_regs = {r[0] for r in cursor.fetchall()}
                                cursor.execute("SELECT category FROM preferences WHERE user_id = ?", (u_id,))
                                u_cats = {r[0] for r in cursor.fetchall()}
                                
                                if source_region in u_regs and (not u_cats or cat_code in u_cats):
                                    try:
                                        reg_badge = "🇷🇺 RU" if source_region == "ru" else "🌐 GLOBAL"
                                        text = (
                                            f"🔥 **Новый заказ! [{CATEGORIES[cat_code]}] ({reg_badge})**\n\n"
                                            f"📌 **{title}**\n"
                                            f"💰 **Бюджет:** `{budget}`\n"
                                            f"🌐 **Источник:** `{source['name']}`\n\n"
                                            f"📝 {summary}"
                                        )
                                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                            [InlineKeyboardButton(text="💬 Откликнуться", url=link)]
                                        ])
                                        await bot.send_message(u_id, text, reply_markup=keyboard, parse_mode="Markdown")
                                        await asyncio.sleep(0.05)
                                    except Exception as e:
                                        logging.error(f"Ошибка отправки пользователю {u_id}: {e}")
                                        
                        conn.close()
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
