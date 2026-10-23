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
user_preferences = {}
user_regions = {}
user_subscriptions = {}
user_referrals = {}
total_stars_collected = 0

def is_subscription_active(user_id: int) -> bool:
    if user_id not in user_subscriptions:
        user_subscriptions[user_id] = datetime.now() + timedelta(days=3)
        return True
    return datetime.now() < user_subscriptions[user_id]

def add_subscription_days(user_id: int, days: int):
    now = datetime.now()
    if user_id in user_subscriptions and user_subscriptions[user_id] > now:
        user_subscriptions[user_id] += timedelta(days=days)
    else:
        user_subscriptions[user_id] = now + timedelta(days=days)

def get_main_reply_keyboard(user_id: int):
    kb = [
        [KeyboardButton(text="🎛 Настройка фильтров"), KeyboardButton(text="🚀 Запустить радар")],
        [KeyboardButton(text="🎁 Бесплатный доступ"), KeyboardButton(text="ℹ️ Помощь")]
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
    budget_match = re.search(r
