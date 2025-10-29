import os
import math
import json
import asyncio
import traceback
import aiohttp
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === ENV ===
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")
if not CHAT_ID:
    print("⚠ CHAT_ID не установлен - автоматические уведомления будут отключены")

# === CONFIG ===
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# Доступные тикеры для отслеживания
AVAILABLE_TICKERS = {
    "VWCE.DE": {"name": "VWCE", "type": "stock"},
    "4GLD.DE": {"name": "4GLD (Gold ETC)", "type": "stock"},
    "DE000A2T5DZ1.SG": {"name": "X IE Physical Gold ETC", "type": "stock"},
    "SPY": {"name": "S&P 500 (SPY)", "type": "stock"},
}

# Крипта: CoinGecko id + CoinPaprika id
CRYPTO_IDS = {
    "BTC": {"coingecko": "bitcoin", "paprika": "btc-bitcoin", "name": "Bitcoin"},
    "ETH": {"coingecko": "ethereum", "paprika": "eth-ethereum", "name": "Ethereum"},
    "SOL": {"coingecko": "solana", "paprika": "sol-solana", "name": "Solana"},
    "AVAX": {"coingecko": "avalanche-2", "paprika": "avax-avalanche", "name": "Avalanche"},
    "DOGE": {"coingecko": "dogecoin", "paprika": "doge-dogecoin", "name": "Dogecoin"},
    "LINK": {"coingecko": "chainlink", "paprika": "link-chainlink", "name": "Chainlink"},
}

# Пороги для алертов
THRESHOLDS = {
    "stocks": 1.0,
    "crypto": 4.0,
}

# Хранилище портфелей (в памяти, можно заменить на файл/БД)
user_portfolios: Dict[int, Dict[str, float]] = {}

# Хранилище последних цен для алертов
last_prices: Dict[str, float] = {}

# Главное меню
def get_main_menu():
    keyboard = [
        [KeyboardButton("💼 Мой портфель"), KeyboardButton("💹 Все цены")],
        [KeyboardButton("➕ Добавить актив"), KeyboardButton("➖ Удалить актив")],
        [KeyboardButton("⚙️ Настройки алертов"), KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ----------------- HTTP helpers -----------------
async def get_json(session: aiohttp.ClientSession, url: str, params=None) -> Optional[Dict[str, Any]]:
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT) as r:
            if r.status != 200:
                print(f"⚠ {url} -> HTTP {r.status}")
                return None
            data = await r.json()
            return data
    except Exception as e:
        print(f"❌ get_json({url}) error: {e}")
        return None

# ----------------- PRICES: Yahoo Finance -----------------
async def get_yahoo_price(session: aiohttp.ClientSession, ticker: str) -> Optional[Tuple[float, str]]:
    """Получить цену одного тикера"""
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": "1d"}
        data = await get_json(session, url, params)
        
        if data:
            result = data.get("chart", {}).get("result", [{}])[0]
            meta = result.get("meta", {})
            price = meta.get("regularMarketPrice")
            cur = meta.get("currency", "USD")
            if price:
                return (float(price), cur)
    except Exception as e:
        print(f"❌ Yahoo {ticker} error: {e}")
    return None

# ----------------- PRICES: CoinPaprika -----------------
async def get_crypto_price(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, float]]:
    """Получить цену криптовалюты через CoinPaprika"""
    try:
        crypto_info = CRYPTO_IDS.get(symbol)
        if not crypto_info:
            return None
        
        paprika_id = crypto_info["paprika"]
        url = f"https://api.coinpaprika.com/v1/tickers/{paprika_id}"
        data = await get_json(session, url, None)
        
        if data:
            quotes = data.get("quotes", {}).get("USD", {})
            price = quotes.get("price")
            change_24h = quotes.get("percent_change_24h")
            if price:
                return {
                    "usd": float(price),
                    "change_24h": float(change_24h) if change_24h else None
                }
    except Exception as e:
        print(f"❌ CoinPaprika {symbol} error: {e}")
    return None

# ----------------- Portfolio Management -----------------
def get_user_portfolio(user_id: int) -> Dict[str, float]:
    """Получить портфель пользователя"""
    if user_id not in user_portfolios:
        # Дефолтный портфель
        user_portfolios[user_id] = {
            "VWCE.DE": 0,
            "DE000A2T5DZ1.SG": 0,
            "BTC": 0,
            "ETH": 0,
            "SOL": 0,
        }
    return user_portfolios[user_id]

def save_portfolio(user_id: int, portfolio: Dict[str, float]):
    """Сохранить портфель"""
    user_portfolios[user_id] = portfolio

# ----------------- MONITORING LOGIC -----------------
async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Проверка цен каждые 10 минут"""
    if not CHAT_ID:
        print("⚠️ CHAT_ID not set, skipping alerts")
        return
    
    print("🔔 Running price alerts check...")
    
    try:
        async with aiohttp.ClientSession() as session:
            alerts = []
            
            # Проверяем акции/ETF
            print("📊 Checking stocks/ETF...")
            for ticker in AVAILABLE_TICKERS:
                price_data = await get_yahoo_price(session, ticker)
                if not price_data:
                    continue
                
                price, currency = price_data
                cache_key = f"stock_{ticker}"
                
                if cache_key in last_prices:
                    old_price = last_prices[cache_key]
                    change_pct = ((price - old_price) / old_price) * 100
                    print(f"  {ticker}: {old_price:.2f} -> {price:.2f} ({change_pct:+.2f}%)")
                    
                    if abs(change_pct) >= THRESHOLDS["stocks"]:
                        name = AVAILABLE_TICKERS[ticker]["name"]
                        emoji = "📈" if change_pct > 0 else "📉"
                        alerts.append(
                            f"{emoji} <b>{name}</b>: {change_pct:+.2f}%\n"
                            f"Цена: {price:.2f} {currency}"
                        )
                        print(f"  🚨 ALERT! {name} changed by {change_pct:+.2f}%")
                else:
                    print(f"  {ticker}: First check, storing price {price:.2f}")
                
                last_prices[cache_key] = price
                await asyncio.sleep(0.3)
            
            # Проверяем криптовалюты
            print("₿ Checking crypto...")
            for symbol in CRYPTO_IDS:
                crypto_data = await get_crypto_price(session, symbol)
                if not crypto_data:
                    continue
                
                price = crypto_data["usd"]
                cache_key = f"crypto_{symbol}"
                
                if cache_key in last_prices:
                    old_price = last_prices[cache_key]
                    change_pct = ((price - old_price) / old_price) * 100
                    print(f"  {symbol}: ${old_price:,.2f} -> ${price:,.2f} ({change_pct:+.2f}%)")
                    
                    if abs(change_pct) >= THRESHOLDS["crypto"]:
                        emoji = "🚀" if change_pct > 0 else "⚠️"
                        alerts.append(
                            f"{emoji} <b>{symbol}</b>: {change_pct:+.2f}%\n"
                            f"Цена: ${price:,.2f}"
                        )
                        print(f"  🚨 ALERT! {symbol} changed by {change_pct:+.2f}%")
                else:
                    print(f"  {symbol}: First check, storing price ${price:,.2f}")
                
                last_prices[cache_key] = price
                await asyncio.sleep(0.2)
            
            print(f"✅ Alert check complete. Cached prices: {len(last_prices)}, Alerts: {len(alerts)}")
            
            if alerts:
                message = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(alerts)
                await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
                print("📤 Alerts sent to user")
    
    except Exception as e:
        print(f"❌ check_price_alerts error: {e}")
        traceback.print_exc()

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный отчёт"""
    if not CHAT_ID:
        return
    
    try:
        from datetime import datetime
        now = datetime.now().strftime("%d.%m.%Y")
        
        lines = [f"🌅 <b>Утренний отчёт ({now})</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            lines.append("<b>📊 Фондовый рынок:</b>")
            for ticker, info in AVAILABLE_TICKERS.items():
                price_data = await get_yahoo_price(session, ticker)
                if price_data:
                    price, cur = price_data
                    lines.append(f"• {info['name']}: {price:.2f} {cur}")
                await asyncio.sleep(0.3)
            
            lines.append("\n<b>₿ Криптовалюты:</b>")
            for symbol, info in CRYPTO_IDS.items():
                crypto_data = await get_crypto_price(session, symbol)
                if crypto_data:
                    price = crypto_data["usd"]
                    chg = crypto_data.get("change_24h")
                    if chg:
                        lines.append(f"• {symbol}: ${price:,.2f} ({chg:+.2f}%)")
                    else:
                        lines.append(f"• {symbol}: ${price:,.2f}")
                await asyncio.sleep(0.2)
        
        await context.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ daily_report error: {e}")

async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный отчёт"""
    await daily_report(context)

# ----------------- BOT handlers -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Привет! Я бот для мониторинга портфеля</b>\n\n"
        "Используй кнопки меню для управления 👇",
        parse_mode='HTML',
        reply_markup=get_main_menu()
    )

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать портфель пользователя"""
    user_id = update.effective_user.id
    portfolio = get_user_portfolio(user_id)
    
    if not portfolio or all(v == 0 for v in portfolio.values()):
        await update.message.reply_text(
            "💼 Ваш портфель пуст!\n\n"
            "Используйте кнопку <b>➕ Добавить актив</b> для добавления активов.",
            parse_mode='HTML'
        )
        return
    
    try:
        lines = ["💼 <b>Ваш портфель:</b>\n"]
        total_value_usd = 0
        
        async with aiohttp.ClientSession() as session:
            # Акции/ETF
            stock_items = [(k, v) for k, v in portfolio.items() if k in AVAILABLE_TICKERS]
            if stock_items:
                lines.append("<b>📊 Акции/ETF:</b>")
                for ticker, quantity in stock_items:
                    if quantity == 0:
                        continue
                    price_data = await get_yahoo_price(session, ticker)
                    if price_data:
                        price, cur = price_data
                        value = price * quantity
                        lines.append(
                            f"• {AVAILABLE_TICKERS[ticker]['name']}: {quantity:.2f} шт\n"
                            f"  Цена: {price:.2f} {cur} | Сумма: {value:.2f} {cur}"
                        )
                        if cur == "USD":
                            total_value_usd += value
                        elif cur == "EUR":
                            total_value_usd += value * 1.1  # Примерный курс
                    await asyncio.sleep(0.3)
            
            # Криптовалюты
            crypto_items = [(k, v) for k, v in portfolio.items() if k in CRYPTO_IDS]
            if crypto_items:
                lines.append("\n<b>₿ Криптовалюты:</b>")
                for symbol, quantity in crypto_items:
                    if quantity == 0:
                        continue
                    crypto_data = await get_crypto_price(session, symbol)
                    if crypto_data:
                        price = crypto_data["usd"]
                        chg = crypto_data.get("change_24h")
                        value = price * quantity
                        total_value_usd += value
                        
                        chg_str = f" ({chg:+.2f}%)" if chg else ""
                        lines.append(
                            f"• {symbol}: {quantity:.4f}\n"
                            f"  Цена: ${price:,.2f}{chg_str} | Сумма: ${value:,.2f}"
                        )
                    await asyncio.sleep(0.2)
        
        if total_value_usd > 0:
            lines.append(f"\n<b>💰 Общая стоимость: ~${total_value_usd:,.2f}</b>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ portfolio error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_all_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все доступные цены"""
    try:
        lines = ["💹 <b>Все цены:</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            lines.append("<b>📊 Фондовый рынок:</b>")
            for ticker, info in AVAILABLE_TICKERS.items():
                price_data = await get_yahoo_price(session, ticker)
                if price_data:
                    price, cur = price_data
                    lines.append(f"• {info['name']}: {price:.2f} {cur}")
                else:
                    lines.append(f"• {info['name']}: н/д")
                await asyncio.sleep(0.3)
            
            lines.append("\n<b>₿ Криптовалюты:</b>")
            for symbol, info in CRYPTO_IDS.items():
                crypto_data = await get_crypto_price(session, symbol)
                if crypto_data:
                    price = crypto_data["usd"]
                    chg = crypto_data.get("change_24h")
                    if chg:
                        lines.append(f"• {symbol}: ${price:,.2f} ({chg:+.2f}%)")
                    else:
                        lines.append(f"• {symbol}: ${price:,.2f}")
                else:
                    lines.append(f"• {symbol}: н/д")
                await asyncio.sleep(0.2)
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ all_prices error: {e}")
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить актив в портфель"""
    await update.message.reply_text(
        "➕ <b>Добавить актив</b>\n\n"
        "Используйте команду:\n"
        "<code>/add TICKER КОЛИЧЕСТВО</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>/add VWCE.DE 10</code> - 10 акций VWCE\n"
        "<code>/add BTC 0.5</code> - 0.5 BTC\n"
        "<code>/add ETH 2</code> - 2 ETH\n\n"
        "<b>Доступные тикеры:</b>\n"
        "• VWCE.DE, 4GLD.DE, DE000A2T5DZ1.SG, SPY\n"
        "• BTC, ETH, SOL, AVAX, DOGE, LINK",
        parse_mode='HTML'
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /add TICKER QUANTITY"""
    if len(context.args) != 2:
        await update.message.reply_text(
            "❌ Неверный формат!\n"
            "Используйте: <code>/add TICKER КОЛИЧЕСТВО</code>",
            parse_mode='HTML'
        )
        return
    
    ticker = context.args[0].upper()
    try:
        quantity = float(context.args[1])
        if quantity <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Количество должно быть положительным числом")
        return
    
    # Проверка существования тикера
    if ticker not in AVAILABLE_TICKERS and ticker not in CRYPTO_IDS:
        await update.message.reply_text(
            f"❌ Неизвестный тикер: {ticker}\n\n"
            "Доступные тикеры: VWCE.DE, 4GLD.DE, DE000A2T5DZ1.SG, SPY, BTC, ETH, SOL, AVAX, DOGE, LINK"
        )
        return
    
    user_id = update.effective_user.id
    portfolio = get_user_portfolio(user_id)
    portfolio[ticker] = portfolio.get(ticker, 0) + quantity
    save_portfolio(user_id, portfolio)
    
    name = AVAILABLE_TICKERS.get(ticker, {}).get("name") or CRYPTO_IDS.get(ticker, {}).get("name") or ticker
    await update.message.reply_text(
        f"✅ Добавлено: <b>{quantity} {name}</b>\n"
        f"Теперь у вас: {portfolio[ticker]:.4f}",
        parse_mode='HTML'
    )

async def cmd_remove_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить актив из портфеля"""
    user_id = update.effective_user.id
    portfolio = get_user_portfolio(user_id)
    
    if not portfolio or all(v == 0 for v in portfolio.values()):
        await update.message.reply_text("💼 Ваш портфель пуст!")
        return
    
    assets = [f"• <code>/remove {k}</code> - {v:.4f}" for k, v in portfolio.items() if v > 0]
    await update.message.reply_text(
        "➖ <b>Удалить актив</b>\n\n"
        "Используйте команду:\n"
        "<code>/remove TICKER</code>\n\n"
        "<b>Ваши активы:</b>\n" + "\n".join(assets),
        parse_mode='HTML'
    )

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /remove TICKER"""
    if len(context.args) != 1:
        await update.message.reply_text("❌ Используйте: <code>/remove TICKER</code>", parse_mode='HTML')
        return
    
    ticker = context.args[0].upper()
    user_id = update.effective_user.id
    portfolio = get_user_portfolio(user_id)
    
    if ticker not in portfolio or portfolio[ticker] == 0:
        await update.message.reply_text(f"❌ {ticker} не найден в вашем портфеле")
        return
    
    portfolio[ticker] = 0
    save_portfolio(user_id, portfolio)
    await update.message.reply_text(f"✅ {ticker} удалён из портфеля")

async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки алертов"""
    message = (
        "⚙️ <b>Настройки алертов:</b>\n\n"
        f"<b>Фондовый рынок:</b> ±{THRESHOLDS['stocks']}%\n"
        f"<b>Криптовалюты:</b> ±{THRESHOLDS['crypto']}%\n\n"
        "<b>📅 Расписание:</b>\n"
        "• Проверка: каждые 10 минут\n"
        "• Утренний отчёт: 11:00 (Рига)\n"
        "• Недельный отчёт: Вс 19:00 (Рига)\n\n"
        f"💾 В кэше: {len(last_prices)} цен\n\n"
        "Изменить: <code>/setalert stocks 2</code>\n"
        "Тест: <code>/testalert</code>"
    )
    await update.message.reply_text(message, parse_mode='HTML')

async def cmd_test_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестовая проверка алертов"""
    await update.message.reply_text("🔄 Запускаю проверку алертов...")
    await check_price_alerts(context)
    await update.message.reply_text(
        f"✅ Проверка завершена!\n"
        f"💾 В кэше: {len(last_prices)} цен\n\n"
        f"Смотрите логи Render для деталей."
    )

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменить пороги алертов"""
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "Использование: <code>/setalert [stocks|crypto] [процент]</code>\n\n"
            "Примеры:\n"
            "<code>/setalert stocks 2</code> — алерты для акций при ±2%\n"
            "<code>/setalert crypto 5</code> — алерты для крипты при ±5%",
            parse_mode='HTML'
        )
        return
    
    asset_type = context.args[0].lower()
    try:
        threshold = float(context.args[1])
        if threshold <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Процент должен быть положительным числом")
        return
    
    if asset_type not in ["stocks", "crypto"]:
        await update.message.reply_text("❌ Тип должен быть 'stocks' или 'crypto'")
        return
    
    THRESHOLDS[asset_type] = threshold
    name = "акций/ETF" if asset_type == "stocks" else "криптовалют"
    await update.message.reply_text(
        f"✅ Порог алертов для {name} установлен: ±{threshold}%",
        parse_mode='HTML'
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    message = (
        "ℹ️ <b>Помощь по боту:</b>\n\n"
        "<b>Кнопки меню:</b>\n"
        "💼 <b>Мой портфель</b> - показать ваши активы\n"
        "💹 <b>Все цены</b> - все доступные котировки\n"
        "➕ <b>Добавить актив</b> - инструкция\n"
        "➖ <b>Удалить актив</b> - убрать из портфеля\n\n"
        "<b>Команды:</b>\n"
        "<code>/add TICKER КОЛ-ВО</code> - добавить\n"
        "<code>/remove TICKER</code> - удалить\n"
        "<code>/setalert stocks 2</code> - изменить пороги"
    )
    await update.message.reply_text(message, parse_mode='HTML')

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок меню"""
    text = update.message.text
    
    if text == "💼 Мой портфель":
        await cmd_portfolio(update, context)
    elif text == "💹 Все цены":
        await cmd_all_prices(update, context)
    elif text == "➕ Добавить актив":
        await cmd_add_asset(update, context)
    elif text == "➖ Удалить актив":
        await cmd_remove_asset(update, context)
    elif text == "⚙️ Настройки алертов":
        await cmd_alerts(update, context)
    elif text == "ℹ️ Помощь":
        await cmd_help(update, context)
    else:
        await update.message.reply_text("Я тебя слышу 👂")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("❌ Global error:", context.error)
    traceback.print_exc()

def main():
    # Обходной путь для python-telegram-bot 20.6 + Python 3.13
    from telegram.ext import ApplicationBuilder
    
    # Создаём приложение без автоматического создания Updater
    builder = ApplicationBuilder()
    builder.token(TOKEN)
    
    # Отключаем автоматическое создание updater для избежания ошибки
    try:
        app = builder.build()
    except AttributeError:
        # Fallback для старых версий
        import telegram.ext._applicationbuilder as ab
        # Патчим build метод
        original_build = ab.ApplicationBuilder.build
        def patched_build(self):
            self._updater = None  # Отключаем updater
            return original_build(self)
        ab.ApplicationBuilder.build = patched_build
        app = builder.build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("testalert", cmd_test_alert))
    
    # Обработка кнопок
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_error_handler(on_error)

    # Планировщик
    job_queue = app.job_queue
    
    if job_queue and CHAT_ID:
        job_queue.run_repeating(check_price_alerts, interval=600, first=60)
        job_queue.run_daily(daily_report, time=dt_time(hour=11, minute=0), days=(0,1,2,3,4,5,6))
        job_queue.run_daily(weekly_report, time=dt_time(hour=19, minute=0), days=(6,))
        print("🚀 Bot running with monitoring enabled")
    else:
        print("🚀 Bot running (monitoring disabled)")
    
    # Запускаем polling вручную
    print("🔄 Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
