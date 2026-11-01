import asyncio
import logging
import os
import re
import html
import json
import shutil
import hashlib
import tempfile
import time
import calendar
import sqlite3
from collections import defaultdict
import aiohttp
import feedparser
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    CallbackQuery, 
    ReplyKeyboardMarkup, 
    KeyboardButton,
    LabeledPrice,
    FSInputFile,
    PreCheckoutQuery
)

# Переменные окружения (секреты и токены задаются в настройках хостинга)
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8188915960"))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- НАСТРОЙКА БАЗЫ ДАННЫХ SQLITE ---
# Если на хостинге подключён постоянный диск, укажите DB_PATH=/путь/к/диску/users.db
DB_NAME = os.getenv("DB_PATH", "users.db")
# SKIP_UNMATCHED=1: заказы, у которых не нашлось ключевых слов категории, не рассылаются
# (по умолчанию 0, как раньше: такие заказы получают категорию источника, обычно «Разработка»)
SKIP_UNMATCHED = os.getenv("SKIP_UNMATCHED", "0") == "1"
MAX_ORDER_AGE_HOURS = 6   # старше этого возраста заказы при сканировании не рассылаются

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_orders (
            order_id TEXT PRIMARY KEY,
            source TEXT,
            ts TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            stars INTEGER,
            charge_id TEXT UNIQUE,
            ts TEXT
        )
    """)
    # один приглашённый может принести бонус только один раз
    try:
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ref_referred ON referrals(referred_id)")
    except sqlite3.IntegrityError:
        logging.warning("В referrals есть дубли referred_id, уникальный индекс не создан")
    cursor.execute("DELETE FROM sent_orders WHERE ts < ?", ((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),))
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
seeded_sources = set()

def _load_sent_orders():
    conn = sqlite3.connect(DB_NAME)
    ids = {r[0] for r in conn.execute("SELECT order_id FROM sent_orders")}
    conn.close()
    return ids

sent_orders |= _load_sent_orders()

def db_mark_sent(order_id: str, source_name: str):
    sent_orders.add(order_id)
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT OR IGNORE INTO sent_orders (order_id, source, ts) VALUES (?, ?, ?)",
                 (order_id, source_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def db_source_known(source_name: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT 1 FROM sent_orders WHERE source = ? LIMIT 1", (source_name,)).fetchone()
    conn.close()
    return row is not None

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

def esc(s) -> str:
    return html.escape(str(s), quote=False)

def clean_text(raw, limit=None):
    """HTML-сущности (в т.ч. двойные вида &amp;rsquo;) -> символы, теги убираем, пробелы схлопываем."""
    s = html.unescape(html.unescape(raw or ""))
    s = re.sub(r"<.*?>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:- ") + "..."
    return s

# Ключевые слова ищутся с границы слова, чтобы «ui» не находилось в «Build», «Quiz», «Acquisition»,
# «бот» в «Работа», а «пост» в «Поставка».
def _rx(patterns):
    return re.compile("|".join(f"(?<!\\w)(?:{p})" for p in patterns), re.IGNORECASE)

CATEGORY_RULES = [
    ("design", _rx([r"дизайн\w*", r"logo\w*", r"ux(?!\w)", r"ui(?!\w)", r"баннер\w*", r"верстк\w*",
                    r"figma", r"tilda", r"design\w*"])),
    ("text", _rx([r"текст\w*", r"стать(?:я|и|ю|ей|ями)(?!\w)", r"копирайт\w*",
                  r"пост(?:а|ы|ов|у|ом|ах)?(?!\w)", r"перевод\w*", r"рерайт\w*", r"content", r"writing"])),
    ("marketing", _rx([r"маркетинг\w*", r"реклам\w*", r"трафик\w*", r"smm", r"лид(?:а|ы|ов|у|ом)?(?!\w)",
                       r"seo", r"директ(?:а|у|ом)?(?!\w)", r"marketing"])),
    ("dev", _rx([r"python", r"бот(?:а|ы|ов|у|ом|ах)?(?!\w)", r"разработчик\w*", r"php", r"скрипт\w*",
                 r"программист\w*", r"код(?:а|ы|ов|у|ом)?(?!\w)", r"сайт\w*", r"react", r"vue",
                 r"node", r"c#", r"java(?:script)?(?!\w)", r"1[сc](?!\w)", r"developer\w*"])),
]

def detect_category(title: str, default: str):
    for code, rx in CATEGORY_RULES:
        if rx.search(title):
            return code
    return None if SKIP_UNMATCHED else default

_BUDGET_RX = re.compile(
    r"(?:[$€]\s?\d[\d\s.,]*\d|\d[\d\s.,]*\s?(?:руб\w*|₽|\$|usd|eur|€|тыс\w*|р\.))",
    re.IGNORECASE)

def smart_ai_filter(title, summary):
    """Не ИИ, а поиск суммы в тексте регулярным выражением. Валюты не конвертируются."""
    m = _BUDGET_RX.search((title + " " + summary).lower())
    return m.group(0).strip().upper() if m else "По договоренности"

def age_text(entry):
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return "", None
    ts = calendar.timegm(st)
    mins = int((time.time() - ts) // 60)
    if mins < 0:
        return "", ts
    if mins < 1:
        return "только что", ts
    if mins < 60:
        return f"{mins} мин назад", ts
    if mins < 60 * 24:
        return f"{mins // 60} ч назад", ts
    return f"{mins // (60 * 24)} дн назад", ts

async def fetch_rss_feed(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    feed = await asyncio.to_thread(feedparser.parse, text)
                    return feed
    except Exception as e:
        logging.error(f"Ошибка загрузки ленты {url}: {e}")
    return None

def parse_entry(source, entry):
    title = clean_text(entry.get("title", ""))
    summary = clean_text(entry.get("summary", "") or "Описание отсутствует", limit=250)
    published, ts = age_text(entry)
    return {
        "title": title,
        "link": entry.get("link", ""),
        "summary": summary,
        "cat": detect_category(title, source["default_cat"]),
        "budget": smart_ai_filter(title, summary),
        "published": published,
        "ts": ts,
        "source": source,
    }

def build_card(head: str, o: dict) -> str:
    badge = "🇷🇺 RU" if o["source"]["region"] == "ru" else "🌐 GLOBAL"
    lines = [
        f"<b>{head} [{esc(CATEGORIES[o['cat']])}] ({badge})</b>",
        "",
        f"📌 <b>{esc(o['title'])}</b>",
        f"💰 <b>Бюджет:</b> <code>{esc(o['budget'])}</code>",
        f"🌐 <b>Источник:</b> {esc(o['source']['name'])}",
    ]
    if o["published"]:
        lines.append(f"🕒 <b>Опубликован:</b> {esc(o['published'])}")
    lines += ["", f"📝 {esc(o['summary'])}"]
    return "\n".join(lines)

async def safe_send(user_id, text, keyboard):
    for attempt in range(2):
        try:
            await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="HTML")
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logging.error(f"Ошибка отправки пользователю {user_id}: {e}")
            return False
    return False

async def send_latest_order_immediately(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM preferences WHERE user_id = ?", (user_id,))
    user_cats = {row[0] for row in cursor.fetchall()}
    cursor.execute("SELECT region FROM regions WHERE user_id = ?", (user_id,))
    user_regs = {row[0] for row in cursor.fetchall()}
    conn.close()

    sources = [s for s in RSS_SOURCES if s["region"] in user_regs]
    feeds = await asyncio.gather(*(fetch_rss_feed(s["url"]) for s in sources))

    found = []
    for source, feed in zip(sources, feeds):
        if not feed or not getattr(feed, "entries", None):
            continue
        for entry in feed.entries[:10]:
            o = parse_entry(source, entry)
            if o["cat"] and (not user_cats or o["cat"] in user_cats):
                found.append(o)

    if not found:
        await bot.send_message(
            user_id,
            "Пока в лентах нет подходящих заказов. Как только появится новый, пришлю его сюда.")
        return

    best = max(found, key=lambda o: o["ts"] or 0)   # самый свежий из найденных
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Откликнуться на бирже", url=best["link"])]
    ])
    await safe_send(user_id, build_card("⚡️ Последний заказ", best), keyboard)

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
        "• Бот просматривает 7 площадок каждые **45 секунд**.\n"
        "• Новые заказы приходят в течение минуты после появления в ленте."
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
    is_new = user_data is None

    args = message.text.split(maxsplit=1)
    source = "Прямой переход 🔍"
    referrer_id = None

    if len(args) > 1:
        param = args[1].strip().lower()
        if param.startswith("ref_"):
            try:
                rid = int(param[4:])
                if rid != user_id:
                    referrer_id = rid
                    source = "Реферал"
            except ValueError:
                pass
        elif param in ["tt", "tiktok"]:
            source = "TikTok 🎵"
        elif param in ["ig", "instagram"]:
            source = "Instagram 📸"
        elif param in ["yt", "youtube", "shorts"]:
            source = "YouTube Shorts ▶️"
        else:
            source = f"Кампания ({param[:40]})"

    if is_new:
        db_register_user(user_id, source)
        # бонус только за нового пользователя и только существующему пригласившему
        if referrer_id and db_get_user(referrer_id):
            conn = sqlite3.connect(DB_NAME)
            cur = conn.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                               (referrer_id, user_id))
            rewarded = cur.rowcount > 0
            conn.commit()
            conn.close()
            if rewarded:
                add_subscription_days(referrer_id, 7)
                try:
                    await bot.send_message(
                        referrer_id,
                        "🎉 <b>Новый реферал!</b>\n"
                        "По вашей ссылке зарегистрировался новый пользователь.\n"
                        "🎁 Вам начислено <b>+7 дней бесплатной подписки</b>!",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logging.error(f"Ошибка уведомления реферера {referrer_id}: {e}")

    greeting = ("🎁 Вам активирован пробный период на <b>3 дня</b>!" if is_new
                else "С возвращением! Ваши настройки сохранены.")
    await message.answer(
        f"Привет, {esc(message.from_user.first_name)}! 👋\n\n"
        f"🤖 <b>Freelance Radar AI</b> — ваш персональный агрегатор заказов.\n"
        f"{greeting}\n\n"
        f"💡 <b>Пользуйтесь ботом бесплатно:</b>\n"
        f"Делитесь вашей ссылкой с коллегами и получайте <b>+7 дней PRO-доступа</b> за каждого пришедшего друга!",
        reply_markup=get_main_reply_keyboard(user_id),
        parse_mode="HTML"
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
    cursor.execute("SELECT COALESCE(SUM(stars), 0) FROM payments")
    total_stars_collected = cursor.fetchone()[0]
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
    
    lines = []
    for r in rows:
        uid, sub_until, src = r[0], r[1], r[2]
        status = "✅ Active" if datetime.now() < datetime.strptime(sub_until, "%Y-%m-%d %H:%M:%S") else "❌ Expired"
        cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,))
        refs = cursor.fetchone()[0]
        lines.append(f"• ID: {uid} | {status} | Рефералов: {refs} | Откуда: {src}")
    conn.close()

    # Telegram режет сообщения длиннее 4096 символов, поэтому шлём частями
    chunk = "👥 Список пользователей:\n\n"
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            await callback.message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    await callback.message.answer(chunk)
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

    msg = "📊 Статистика по источникам (UTM):\n\n"
    if not rows:
        msg += "Пока нет данных о переходах."
    else:
        for src_name, count in rows:
            msg += f"• {src_name}: {count} чел.\n"
            
    await callback.message.answer(msg)
    await callback.answer()

@dp.message(F.text == "ℹ️ Помощь")
async def btn_help(message: types.Message):
    await message.answer(
        "ℹ️ **Как работает Freelance Radar AI:**\n\n"
        "1. **Шаг 1:** Выберите регионы бирж (Россия / Международные).\n"
        "2. **Шаг 2:** Укажите сферы работы (Разработка, Дизайн и др.).\n"
        "3. **Мониторинг:** Бот просматривает 7 площадок каждые 45 секунд и присылает новые заказы в течение минуты после появления в ленте.\n\n"
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
    pay = message.successful_payment
    conn = sqlite3.connect(DB_NAME)
    cur = conn.execute("INSERT OR IGNORE INTO payments (user_id, stars, charge_id, ts) VALUES (?, ?, ?, ?)",
                       (message.from_user.id, pay.total_amount, pay.telegram_payment_charge_id,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    first_time = cur.rowcount > 0
    conn.commit()
    conn.close()
    if first_time:
        add_subscription_days(message.from_user.id, 30)
        try:
            await backup_db(force=True)   # оплату терять нельзя: копия сразу
        except Exception as e:
            logging.error(f"Бэкап после оплаты не удался: {e}")
    await message.answer("🎉 Оплата прошла успешно! Добавлено +30 дней.", reply_markup=get_main_reply_keyboard(message.from_user.id))

async def broadcast_order(o: dict):
    if not o["cat"]:
        return
    region = o["source"]["region"]
    conn = sqlite3.connect(DB_NAME)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    users = [r[0] for r in conn.execute("SELECT user_id FROM users WHERE sub_until > ?", (now,))]
    regs, cats = defaultdict(set), defaultdict(set)
    for uid, reg in conn.execute("SELECT user_id, region FROM regions"):
        regs[uid].add(reg)
    for uid, cat in conn.execute("SELECT user_id, category FROM preferences"):
        cats[uid].add(cat)
    conn.close()

    text = build_card("🔥 Новый заказ!", o)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Откликнуться", url=o["link"])]])
    for uid in users:
        if region in regs[uid] and (not cats[uid] or o["cat"] in cats[uid]):
            await safe_send(uid, text, keyboard)
            await asyncio.sleep(0.05)

async def process_source(source):
    feed = await fetch_rss_feed(source["url"])
    if not feed or not getattr(feed, "entries", None):
        return
    pending = []
    for entry in feed.entries[:15]:
        key = f"{source['name']}|{entry.get('id') or entry.get('link')}"
        if key not in sent_orders:
            pending.append((key, entry))
    if not pending:
        return

    # Первый проход по источнику после старта процесса: молча запоминаем текущую ленту.
    # Так после деплоя, перезапуска или восстановления базы из копии пользователи не получают
    # десятки старых заказов разом (цена: заказы, вышедшие за время простоя, не рассылаются).
    if source["name"] not in seeded_sources:
        seeded_sources.add(source["name"])
        for key, _ in pending:
            db_mark_sent(key, source["name"])
        return

    for key, entry in reversed(pending):   # от старых к новым
        db_mark_sent(key, source["name"])
        o = parse_entry(source, entry)
        if o["ts"] and time.time() - o["ts"] > MAX_ORDER_AGE_HOURS * 3600:
            continue
        await broadcast_order(o)

async def real_order_scanner():
    while True:
        try:
            results = await asyncio.gather(*(process_source(s) for s in RSS_SOURCES), return_exceptions=True)
            for source, res in zip(RSS_SOURCES, results):
                if isinstance(res, Exception):
                    logging.error(f"Ошибка при обработке источника {source['name']}: {res}")
        except Exception as e:
            logging.error(f"Ошибка в сканере заказов: {e}")
        await asyncio.sleep(45)

# --- РЕЗЕРВНАЯ КОПИЯ БАЗЫ В TELEGRAM ---
# На бесплатном Render файлы пропадают при деплое и перезапуске. Поэтому бот присылает
# администратору файл users.db (только если данные изменились) и закрепляет его в чате.
# При старте с пустой базой бот скачивает закреплённый файл и восстанавливает данные.
BACKUP_EVERY_SEC = 120
BACKUP_TABLES = ("users", "referrals", "preferences", "regions", "payments")
_backup_lock = asyncio.Lock()
_last_hash = None
_last_backup_msg = None

def _users_count() -> int:
    conn = sqlite3.connect(DB_NAME)
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n

def _state_hash() -> str:
    conn = sqlite3.connect(DB_NAME)
    data = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in BACKUP_TABLES}
    conn.close()
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()

async def backup_db(force: bool = False):
    global _last_hash, _last_backup_msg
    async with _backup_lock:
        # пустую базу не сохраняем: она не должна затереть хорошую копию
        if await asyncio.to_thread(_users_count) == 0:
            return
        h = await asyncio.to_thread(_state_hash)
        if not force and h == _last_hash:
            return
        tmp = os.path.join(tempfile.mkdtemp(), "users.db")
        def snapshot():
            s = sqlite3.connect(DB_NAME); d = sqlite3.connect(tmp)
            s.backup(d); d.close(); s.close()
        await asyncio.to_thread(snapshot)
        msg = await bot.send_document(
            ADMIN_ID, FSInputFile(tmp, filename="users.db"),
            caption=f"🗄 Резервная копия базы, {datetime.now().strftime('%d.%m %H:%M')}",
            disable_notification=True)
        try:
            await bot.unpin_all_chat_messages(ADMIN_ID)
            await bot.pin_chat_message(ADMIN_ID, msg.message_id, disable_notification=True)
        except Exception as e:
            logging.error(f"Не удалось закрепить копию базы: {e}")
        if _last_backup_msg:
            try:
                await bot.delete_message(ADMIN_ID, _last_backup_msg)
            except Exception:
                pass
        _last_backup_msg, _last_hash = msg.message_id, h

async def restore_db_if_needed():
    """Если база пустая (свежий деплой), берём закреплённую копию из чата с администратором."""
    if await asyncio.to_thread(_users_count) > 0:
        return
    try:
        chat = await bot.get_chat(ADMIN_ID)
        doc = chat.pinned_message.document if chat.pinned_message else None
        if not doc or not (doc.file_name or "").startswith("users"):
            logging.warning("Закреплённой копии базы нет, стартуем с чистой базы")
            return
        tmp = os.path.join(tempfile.mkdtemp(), "restore.db")
        await bot.download(doc.file_id, destination=tmp)
        chk = sqlite3.connect(tmp)
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        cnt = chk.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        chk.close()
        if not ok or cnt == 0:
            logging.warning("Копия базы повреждена или пуста, не восстанавливаем")
            return
        shutil.copyfile(tmp, DB_NAME)
        init_db()                      # добавит таблицы, которых не было в старой копии
        sent_orders.clear()
        sent_orders.update(_load_sent_orders())
        logging.info(f"База восстановлена из копии: {cnt} пользователей")
        await bot.send_message(ADMIN_ID, f"♻️ База восстановлена из резервной копии: {cnt} пользователей.")
    except Exception as e:
        logging.error(f"Не удалось восстановить базу: {e}")

async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_EVERY_SEC)
        try:
            await backup_db()
        except Exception as e:
            logging.error(f"Ошибка резервного копирования: {e}")

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
    await restore_db_if_needed()
    background = [asyncio.create_task(web_server()), asyncio.create_task(real_order_scanner()),
                  asyncio.create_task(backup_loop())]
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
