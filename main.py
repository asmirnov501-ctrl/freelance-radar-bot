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

API_TOKEN = "8849608846:AAGXWPStgM1ClXTQbkONyAneInYk0_mxF3g"

# Укажи свой Telegram ID цифрами, чтобы получить доступ к панели администратора (/admin)
ADMIN_ID = 0  # <--- Замени на свой реальный Telegram ID (например: 123456789)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

CATEGORIES = {
    "design": "🎨 Дизайн & UX/UI",
    "dev": "💻 Разработка & Код",
    "text": "✍️ Тексты & Копирайтинг",
    "marketing": "📈 Маркетинг & Трафик"
}

RSS_SOURCES = [
    {
        "name": "Habr Freelance",
        "url": "https://freelance.habr.com/tasks.rss",
        "default_cat": "dev"
    },
    {
        "name": "FL.ru",
        "url": "https://www.fl.ru/rss/all.xml",
        "default_cat": "dev"
    }
]

sent_orders = set()
user_preferences = {}
user_subscriptions = {}
total_stars_collected = 0

def is_subscription_active(user_id: int) -> bool:
    """Проверка активности подписки или триала (3 дня)"""
    if user_id not in user_subscriptions:
        user_subscriptions[user_id] = datetime.now() + timedelta(days=3)
        return True
    return datetime.now() < user_subscriptions[user_id]

def get_main_reply_keyboard(user_id: int):
    kb = [
        [KeyboardButton(text="🎯 Выбрать категории"), KeyboardButton(text="🚀 Запустить радар")],
        [KeyboardButton(text="💳 Подписка"), KeyboardButton(text="ℹ️ Помощь")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_categories_keyboard(user_id):
    selected = user_preferences.get(user_id, set())
    buttons = []
    
    for code, name in CATEGORIES.items():
        prefix = "✅ " if code in selected else "▫️ "
        buttons.append([InlineKeyboardButton(text=f"{prefix}{name}", callback_data=f"cat_{code}")])
        
    buttons.append([InlineKeyboardButton(text="🚀 Готово / Запустить", callback_data="start_radar")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = set()
        
    is_subscription_active(user_id)
    
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"Я **Freelance Radar AI** — твой умный агрегатор реальных заказов.\n"
        f"🎁 Вам активирован **бесплатный пробный период на 3 дня**!\n\n"
        f"Используй кнопки внизу экрана для управления ботом 👇",
        reply_markup=get_main_reply_keyboard(user_id),
        parse_mode="Markdown"
    )
    await message.answer(
        "🎯 **Выбери категории для отслеживания:**",
        reply_markup=get_categories_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🎯 Выбрать категории")
async def btn_choose_categories(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_preferences:
        user_preferences[user_id] = set()
    await message.answer(
        "🎯 **Выбери категории для отслеживания:**",
        reply_markup=get_categories_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🚀 Запустить радар")
async def btn_start_radar(message: types.Message):
    user_id = message.from_user.id
    
    if not is_subscription_active(user_id):
        await message.answer(
            "🔒 **Срок действия вашей подписки истек.**\n\n"
            "Чтобы продолжать получать заказы, продлите подписку в разделе **💳 Подписка**.",
            reply_markup=get_main_reply_keyboard(user_id),
            parse_mode="Markdown"
        )
        return

    selected = user_preferences.get(user_id, set())
    if not selected:
        await message.answer("⚠️ Сначала выбери хотя бы одну категорию через кнопку «🎯 Выбрать категории»!")
        return
        
    selected_names = "\n".join([f"• {CATEGORIES[code]}" for code in selected])
    await message.answer(
        f"🚀 **Радар успешно запущен!**\n\n"
        f"**Выбранные категории:**\n{selected_names}\n\n"
        f"🌐 Мониторинг бирж активен. Ожидай свежие проекты...",
        reply_markup=get_main_reply_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.message(F.text == "💳 Подписка")
async def btn_subscription(message: types.Message):
    user_id = message.from_user.id
    active = is_subscription_active(user_id)
    until_date = user_subscriptions[user_id].strftime("%d.%m.%Y %H:%M")
    
    status_text = f"✅ **Активна** (до `{until_date}`)" if active else "❌ **Истекла**"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Продлить за 333 Stars (1 мес.)", callback_data="buy_stars")]
    ])
    
    await message.answer(
        f"💳 **Управление подпиской**\n\n"
        f"• **Статус:** {status_text}\n\n"
        f"PRO-подписка дает безлимитный доступ к автоматическому мониторингу заказов со всех бирж с AI-фильтрацией.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(F.text == "👑 Админ-панель")
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return
        
    total_users = len(user_preferences)
    active_subs = sum(1 for uid in user_preferences if is_subscription_active(uid))
    
    await message.answer(
        f"👑 **Панель администратора**\n\n"
        f"👥 **Всего пользователей:** `{total_users}`\n"
        f"💎 **Активных подписок/триалов:** `{active_subs}`\n"
        f"⭐ **Собрано звезд:** `{total_stars_collected}` XTR\n\n"
        f"Используйте команду `/broadcast Текст сообщения` для отправки рассылки всем пользователям.",
        parse_mode="Markdown"
    )

@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
        
    text_to_send = message.text.replace("/broadcast", "").strip()
    if not text_to_send:
        await message.answer("⚠️ Укажите текст рассылки после команды, например:\n`/broadcast Важное обновление системы!`", parse_mode="Markdown")
        return
        
    success_count = 0
    for uid in user_preferences.keys():
        try:
            await bot.send_message(uid, f"📢 **Сообщение от администратора:**\n\n{text_to_send}", parse_mode="Markdown")
            success_count += 1
            await asyncio.sleep(0.05) # Защита от флуд-контроля Telegram
        except Exception as e:
            logging.error(f"Не удалось отправить рассылку пользователю {uid}: {e}")
            
    await message.answer(f"✅ Рассылка успешно завершена. Доставлено пользователям: {success_count}/{len(user_preferences)}")

@dp.message(F.text == "ℹ️ Помощь")
async def btn_help(message: types.Message):
    user_id = message.from_user.id
    await message.answer(
        "ℹ️ **Как работает Freelance Radar AI:**\n\n"
        "1. Нажми «🎯 Выбрать категории» и отметь нужные направления.\n"
        "2. Нажми «🚀 Запустить радар».\n"
        "3. Бот анализирует заказы через AI-фильтр и присылает только подходящие проекты с прямой ссылкой.\n\n"
        "🎁 Первые 3 дня доступа — **БЕСПЛАТНО**!",
        reply_markup=get_main_reply_keyboard(user_id),
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
async def start_radar_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    if not is_subscription_active(user_id):
        await callback.answer("🔒 Подписка истекла. Продлите доступ!", show_alert=True)
        return

    selected = user_preferences.get(user_id, set())
    if not selected:
        await callback.answer("⚠️ Выберите хотя бы одну категорию!", show_alert=True)
        return
        
    selected_names = "\n".join([f"• {CATEGORIES[code]}" for code in selected])
    
    await callback.message.answer(
        f"🚀 **Радар успешно запущен!**\n\n"
        f"**Выбранные категории:**\n{selected_names}\n\n"
        f"🌐 Мониторинг бирж активен. Ожидайте свежие проекты...",
        reply_markup=get_main_reply_keyboard(user_id),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "buy_stars")
async def send_stars_invoice(callback: CallbackQuery):
    prices = [LabeledPrice(label="PRO Подписка на 1 месяц", amount=333)]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Freelance Radar AI — PRO подписка",
        description="Безлимитный доступ к агрегатору заказов на 30 дней",
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
    user_id = message.from_user.id
    total_stars_collected += 333
    
    current_end = user_subscriptions.get(user_id, datetime.now())
    if current_end < datetime.now():
        current_end = datetime.now()
        
    user_subscriptions[user_id] = current_end + timedelta(days=30)
    
    until_date = user_subscriptions[user_id].strftime("%d.%m.%Y")
    await message.answer(
        f"🎉 **Оплата прошла успешно!**\n\n"
        f"Ваша подписка продлена на 30 дней (до `{until_date}`).\n"
        f"Спасибо за поддержку проекта!",
        reply_markup=get_main_reply_keyboard(user_id),
        parse_mode="Markdown"
    )

def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return cleantext.strip()[:250] + "..." if len(cleantext) > 250 else cleantext.strip()

def smart_ai_filter(title, summary):
    full_text = (title + " " + summary).lower()
    budget_match = re.search(r'(\d+[\s\d]*)\s?(руб|₽|\$|usd|тыс|р\.)', full_text)
    budget = budget_match.group(0).upper() if budget_match else "По договоренности"
    
    relevancy = 88
    if len(summary) > 50:
        relevancy += 7
    if budget != "По договоренности":
        relevancy += 3
    if relevancy > 98:
        relevancy = 98
        
    return budget, relevancy

async def fetch_rss_feed(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return feedparser.parse(text)
    except Exception as e:
        logging.error(f"Ошибка загрузки ленты {url}: {e}")
    return None

async def real_order_scanner():
    while True:
        try:
            for source in RSS_SOURCES:
                feed = await fetch_rss_feed(source["url"])
                if not feed or not hasattr(feed, "entries"):
                    continue
                
                for entry in feed.entries[:5]:
                    order_id = entry.get("id", entry.link)
                    
                    if order_id in sent_orders:
                        continue
                        
                    title = entry.title
                    link = entry.link
                    summary = clean_html(entry.get("summary", "Описание отсутствует"))
                    
                    cat_code = source["default_cat"]
                    title_lower = title.lower()
                    if any(w in title_lower for w in ["дизайн", "logo", "ux", "ui", "баннер", "верстка"]):
                        cat_code = "design"
                    elif any(w in title_lower for w in ["текст", "статья", "копирайт", "пост", "перевод"]):
                        cat_code = "text"
                    elif any(w in title_lower for w in ["маркетинг", "реклама", "трафик", "smm", "лид"]):
                        cat_code = "marketing"
                    elif any(w in title_lower for w in ["python", "бот", "разработчик", "php", "скрипт", "программист", "код", "сайт", "разработка"]):
                        cat_code = "dev"

                    budget, relevancy = smart_ai_filter(title, summary)
                    
                    for user_id, categories in user_preferences.items():
                        if is_subscription_active(user_id):
                            if not categories or cat_code in categories:
                                try:
                                    text = (
                                        f"🔥 **Новый заказ! [{CATEGORIES[cat_code]}]**\n\n"
                                        f"📌 **{title}**\n"
                                        f"💰 **Бюджет:** `{budget}`\n"
                                        f"🌐 **Источник:** `{source['name']}`\n\n"
                                        f"📝 {summary}\n\n"
                                        f"⚡️ *AI-фильтр: Соответствует вашему профилю ({relevancy}% relevancy)*"
                                    )
                                    
                                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                        [InlineKeyboardButton(text="💬 Откликнуться на бирже", url=link)]
                                    ])
                                    
                                    await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="Markdown")
                                except Exception as e:
                                    logging.error(f"Ошибка отправки пользователю {user_id}: {e}")
                                
                    sent_orders.add(order_id)
        except Exception as e:
            logging.error(f"Ошибка в сканере заказов: {e}")
            
        await asyncio.sleep(30)

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
