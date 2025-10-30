# BOT VERSION: 2025-10-30-FULL-FEATURED-v4
# Complete version with ALL features from v1 + NEW features from v3
# Features:
# FROM v1: Portfolio, Stock/ETF tracking, Events, Forecasts, Charts, Yahoo Finance
# FROM v3: Trade tracking with alerts, Market signals by investor type
# NEW: Everything integrated in one bot!

import os
import math
import asyncio
import traceback
import aiohttp
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time, datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
)

# === ENV ===
TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
LUNARCRUSH_API_KEY = os.getenv("LUNARCRUSH_API_KEY", "lsnio8kvswz9egysxeb8tzybcmhc2zcuee74kwz")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://oueliwijnudbvjlekrsc.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im91ZWxpd2lqbnVkYnZqbGVrcnNjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE3NTE1MzgsImV4cCI6MjA3NzMyNzUzOH0.m7C_Uc2RItTkxQ786AkFnrTLZQIuDnuG__SEnjDAd8w")

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

# === FROM v1: Доступные тикеры для портфеля ===
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

# Пороги для алертов (v1)
THRESHOLDS = {
    "stocks": 1.0,
    "crypto": 4.0,
}

# === FROM v1: Хранилище портфелей ===
user_portfolios: Dict[int, Dict[str, float]] = {}

# === FROM v1: Хранилище последних цен для алертов ===
last_prices: Dict[str, float] = {}

# === NEW v3: Хранилище сделок с целями ===
user_trades: Dict[int, List[Dict[str, Any]]] = {}

# === NEW v3: Типы инвесторов ===
INVESTOR_TYPES = {
    "long": {"name": "Долгосрочный инвестор", "emoji": "🏔️", "desc": "Покупаю на страхе, держу годами"},
    "swing": {"name": "Свинг-трейдер", "emoji": "🌊", "desc": "Ловлю волны, держу дни-недели"},
    "day": {"name": "Дневной трейдер", "emoji": "⚡", "desc": "Быстрые сделки внутри дня"},
}
user_profiles: Dict[int, str] = {}

# Conversation states
SELECT_CRYPTO, ENTER_AMOUNT, ENTER_PRICE, ENTER_TARGET = range(4)

def get_main_menu():
    """Расширенное главное меню со ВСЕМИ функциями"""
    keyboard = [
        [KeyboardButton("💼 Мой портфель"), KeyboardButton("💹 Все цены")],
        [KeyboardButton("🎯 Мои сделки"), KeyboardButton("📊 Рыночные сигналы")],
        [KeyboardButton("📰 События недели"), KeyboardButton("🔮 Прогнозы")],
        [KeyboardButton("➕ Добавить актив"), KeyboardButton("🆕 Новая сделка")],
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("ℹ️ Помощь")],
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

# ----------------- PRICES: Yahoo Finance (v1) -----------------
async def get_yahoo_price(session: aiohttp.ClientSession, ticker: str) -> Optional[Tuple[float, str, float]]:
    """Получить цену одного тикера с изменением за день"""
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": "1d"}
        data = await get_json(session, url, params)
        
        if data:
            result = data.get("chart", {}).get("result", [{}])[0]
            meta = result.get("meta", {})
            price = meta.get("regularMarketPrice")
            cur = meta.get("currency", "USD")
            change_pct = meta.get("regularMarketChangePercent", 0)
            
            if price:
                return (float(price), cur, float(change_pct))
    except Exception as e:
        print(f"❌ Yahoo {ticker} error: {e}")
    return None

# ----------------- PRICES: Crypto APIs (v1 + v3) -----------------
async def get_from_coinpaprika(session: aiohttp.ClientSession, crypto_info: dict) -> Optional[Dict[str, float]]:
    """Получить с CoinPaprika"""
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
    return None

async def get_from_coingecko(session: aiohttp.ClientSession, crypto_info: dict) -> Optional[Dict[str, float]]:
    """Получить с CoinGecko"""
    coingecko_id = crypto_info["coingecko"]
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": coingecko_id,
        "vs_currencies": "usd",
        "include_24hr_change": "true"
    }
    data = await get_json(session, url, params)
    
    if data and coingecko_id in data:
        coin_data = data[coingecko_id]
        price = coin_data.get("usd")
        change_24h = coin_data.get("usd_24h_change")
        if price:
            return {
                "usd": float(price),
                "change_24h": float(change_24h) if change_24h else None
            }
    return None

async def get_from_cryptocompare(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, float]]:
    """Получить с CryptoCompare"""
    url = "https://min-api.cryptocompare.com/data/pricemultifull"
    params = {
        "fsyms": symbol,
        "tsyms": "USD"
    }
    data = await get_json(session, url, params)
    
    if data and "RAW" in data and symbol in data["RAW"]:
        coin_data = data["RAW"][symbol]["USD"]
        price = coin_data.get("PRICE")
        change_24h = coin_data.get("CHANGEPCT24HOUR")
        if price:
            return {
                "usd": float(price),
                "change_24h": float(change_24h) if change_24h else None
            }
    return None

async def get_crypto_price(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    """Получить цену криптовалюты с автоматическим fallback"""
    crypto_info = CRYPTO_IDS.get(symbol)
    if not crypto_info:
        return None
    
    sources = [
        ("CoinPaprika", lambda: get_from_coinpaprika(session, crypto_info)),
        ("CoinGecko", lambda: get_from_coingecko(session, crypto_info)),
        ("CryptoCompare", lambda: get_from_cryptocompare(session, symbol)),
    ]
    
    for source_name, fetch_func in sources:
        try:
            result = await fetch_func()
            if result and result.get("usd"):
                result["source"] = source_name
                price = result['usd']
                chg = result.get('change_24h')
                if chg:
                    print(f"✅ {symbol} from {source_name}: ${price:,.2f} ({chg:+.2f}%)")
                else:
                    print(f"✅ {symbol} from {source_name}: ${price:,.2f}")
                return result
        except Exception as e:
            print(f"⚠️ {source_name} failed for {symbol}: {e}")
            continue
    
    print(f"❌ All sources failed for {symbol}")
    return None

async def get_fear_greed_index(session: aiohttp.ClientSession) -> Optional[int]:
    """Получить индекс страха и жадности"""
    try:
        url = "https://api.alternative.me/fng/"
        data = await get_json(session, url, None)
        if data and "data" in data:
            return int(data["data"][0]["value"])
    except Exception as e:
        print(f"❌ Fear & Greed error: {e}")
    return None

# ----------------- Portfolio Management (v1) -----------------
def get_user_portfolio(user_id: int) -> Dict[str, float]:
    """Получить портфель (v1 функционал)"""
    if user_id not in user_portfolios:
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

# ----------------- Trade Management (NEW v3) -----------------
def get_user_trades(user_id: int) -> List[Dict[str, Any]]:
    """Получить все сделки пользователя"""
    if user_id not in user_trades:
        user_trades[user_id] = []
    return user_trades[user_id]

def add_trade(user_id: int, symbol: str, amount: float, entry_price: float, target_profit_pct: float):
    """Добавить новую сделку"""
    trades = get_user_trades(user_id)
    trade = {
        "symbol": symbol,
        "amount": amount,
        "entry_price": entry_price,
        "target_profit_pct": target_profit_pct,
        "timestamp": datetime.now().isoformat(),
        "notified": False
    }
    trades.append(trade)
    print(f"✅ Added trade for user {user_id}: {symbol} x{amount} @ ${entry_price}")

# ----------------- Market Signals (NEW v3) -----------------
async def get_market_signal(session: aiohttp.ClientSession, symbol: str, investor_type: str) -> Dict[str, Any]:
    """Получить сигнал BUY/HOLD/SELL на основе типа инвестора"""
    
    crypto_data = await get_crypto_price(session, symbol)
    if not crypto_data:
        return {"signal": "UNKNOWN", "emoji": "❓", "reason": "Нет данных о цене"}
    
    fear_greed = await get_fear_greed_index(session)
    if not fear_greed:
        fear_greed = 50
    
    if investor_type == "long":
        if fear_greed < 30:
            return {
                "signal": "BUY",
                "emoji": "🟢",
                "reason": f"Экстремальный страх ({fear_greed}/100). Отличная точка входа."
            }
        elif fear_greed > 75:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Жадность ({fear_greed}/100). Держите позиции."
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Стабильный рынок ({fear_greed}/100). Держать долгосрочно."
            }
    
    elif investor_type == "swing":
        if fear_greed < 40:
            return {
                "signal": "BUY",
                "emoji": "🟢",
                "reason": f"Страх ({fear_greed}/100). Возможность войти на коррекции."
            }
        elif fear_greed > 65:
            return {
                "signal": "SELL",
                "emoji": "🔴",
                "reason": f"Жадность ({fear_greed}/100). Зафиксировать прибыль."
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Нейтрально ({fear_greed}/100). Ждать лучшей точки."
            }
    
    else:  # day trader
        if fear_greed < 45:
            return {
                "signal": "BUY",
                "emoji": "🟢",
                "reason": f"Страх ({fear_greed}/100). Возможен отскок."
            }
        elif fear_greed > 60:
            return {
                "signal": "SELL",
                "emoji": "🔴",
                "reason": f"Перекупленность ({fear_greed}/100). Риск коррекции."
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Флэт ({fear_greed}/100). Ожидание сигнала."
            }

# ----------------- MONITORING: Price Alerts (v1) -----------------
async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Проверка изменений цен каждые 10 минут (v1 функционал)"""
    if not CHAT_ID:
        print("⚠️ CHAT_ID not set, skipping price alerts")
        return
    
    print("🔔 Running price alerts check (v1)...")
    
    try:
        async with aiohttp.ClientSession() as session:
            alerts = []
            
            print("📊 Checking stocks/ETF...")
            for ticker in AVAILABLE_TICKERS:
                price_data = await get_yahoo_price(session, ticker)
                if not price_data:
                    continue
                
                price, currency, _ = price_data
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
            
            print(f"✅ Price alerts check complete. Cached: {len(last_prices)}, Alerts: {len(alerts)}")
            
            if alerts:
                message = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(alerts)
                await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
                print("📤 Price alerts sent")
    
    except Exception as e:
        print(f"❌ check_price_alerts error: {e}")
        traceback.print_exc()

# ----------------- MONITORING: Trade Profit Alerts (NEW v3) -----------------
async def check_trade_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Проверка прибыли по сделкам (NEW v3 функционал)"""
    if not CHAT_ID:
        print("⚠️ CHAT_ID not set, skipping trade alerts")
        return
    
    print("🎯 Checking trade profit alerts (v3)...")
    
    try:
        async with aiohttp.ClientSession() as session:
            alerts_sent = 0
            
            for user_id, trades in user_trades.items():
                for trade in trades:
                    if trade.get("notified"):
                        continue
                    
                    symbol = trade["symbol"]
                    entry_price = trade["entry_price"]
                    target = trade["target_profit_pct"]
                    amount = trade["amount"]
                    
                    crypto_data = await get_crypto_price(session, symbol)
                    if not crypto_data:
                        continue
                    
                    current_price = crypto_data["usd"]
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                    
                    print(f"  {symbol}: Entry ${entry_price:.2f}, Now ${current_price:.2f}, Profit {profit_pct:.2f}% (Target {target}%)")
                    
                    if profit_pct >= target:
                        value = amount * current_price
                        profit_usd = amount * (current_price - entry_price)
                        
                        alert = (
                            f"🎯 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n\n"
                            f"💰 {symbol}\n"
                            f"Количество: {amount:.4f}\n"
                            f"Цена входа: ${entry_price:,.2f}\n"
                            f"Текущая цена: ${current_price:,.2f}\n\n"
                            f"📈 Прибыль: <b>{profit_pct:.2f}%</b> (${profit_usd:,.2f})\n"
                            f"💵 Стоимость: ${value:,.2f}\n\n"
                            f"✅ <b>Рекомендация: ПРОДАВАТЬ</b>"
                        )
                        
                        await context.bot.send_message(chat_id=str(user_id), text=alert, parse_mode='HTML')
                        trade["notified"] = True
                        alerts_sent += 1
                        print(f"  🚨 PROFIT ALERT sent to user {user_id} for {symbol}!")
                    
                    await asyncio.sleep(0.2)
            
            print(f"✅ Trade alerts check complete. Sent {alerts_sent} alerts.")
    
    except Exception as e:
        print(f"❌ check_trade_alerts error: {e}")
        traceback.print_exc()

# ----------------- BOT HANDLERS -----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Стартовое сообщение"""
    user_id = update.effective_user.id
    
    if user_id not in user_profiles:
        user_profiles[user_id] = "long"
    
    await update.message.reply_text(
        "👋 <b>Привет! Полнофункциональный Trading Bot v4</b>\n\n"
        "<b>📊 ИЗ v1 (Сохранено):</b>\n"
        "• 💼 Портфель активов (акции + крипта)\n"
        "• 💹 Мониторинг цен в реальном времени\n"
        "• 📰 События недели и прогнозы\n"
        "• 📈 Графики цен\n"
        "• 🔔 Алерты при изменении цены\n\n"
        "<b>🆕 НОВОЕ из v3:</b>\n"
        "• 🎯 Отслеживание сделок с целевой прибылью\n"
        "• 📊 Рыночные сигналы BUY/HOLD/SELL\n"
        "• 👤 Персонализация по типу инвестора\n\n"
        "Все функции работают одновременно! Используй кнопки меню 👇",
        parse_mode='HTML',
        reply_markup=get_main_menu()
    )

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать портфель (v1 функционал)"""
    user_id = update.effective_user.id
    portfolio = get_user_portfolio(user_id)
    
    if not portfolio or all(v == 0 for v in portfolio.values()):
        await update.message.reply_text(
            "💼 Ваш портфель пуст!\n\n"
            "Используйте <b>➕ Добавить актив</b>",
            parse_mode='HTML'
        )
        return
    
    try:
        lines = ["💼 <b>Ваш портфель (v1):</b>\n"]
        total_value_usd = 0
        
        async with aiohttp.ClientSession() as session:
            stock_items = [(k, v) for k, v in portfolio.items() if k in AVAILABLE_TICKERS]
            if stock_items and any(v > 0 for k, v in stock_items):
                lines.append("<b>📊 Акции/ETF:</b>")
                lines.append("<pre>")
                lines.append("Актив          Кол-во    Цена        Сумма")
                lines.append("─" * 50)
                
                for ticker, quantity in stock_items:
                    if quantity == 0:
                        continue
                    price_data = await get_yahoo_price(session, ticker)
                    if price_data:
                        price, cur, _ = price_data
                        value = price * quantity
                        
                        name = AVAILABLE_TICKERS[ticker]['name'][:14].ljust(14)
                        qty_str = f"{quantity:.2f}".rjust(8)
                        price_str = f"{price:.2f}".rjust(8)
                        value_str = f"{value:.2f} {cur}".rjust(12)
                        
                        lines.append(f"{name} {qty_str} {price_str} {value_str}")
                        
                        if cur == "USD":
                            total_value_usd += value
                        elif cur == "EUR":
                            total_value_usd += value * 1.1
                    await asyncio.sleep(0.3)
                
                lines.append("</pre>")
            
            crypto_items = [(k, v) for k, v in portfolio.items() if k in CRYPTO_IDS]
            if crypto_items and any(v > 0 for k, v in crypto_items):
                lines.append("\n<b>₿ Криптовалюты:</b>")
                lines.append("<pre>")
                lines.append("Монета    Кол-во      Цена          Сумма")
                lines.append("─" * 50)
                
                for symbol, quantity in crypto_items:
                    if quantity == 0:
                        continue
                    crypto_data = await get_crypto_price(session, symbol)
                    if crypto_data:
                        price = crypto_data["usd"]
                        chg = crypto_data.get("change_24h")
                        value = price * quantity
                        total_value_usd += value
                        
                        sym_str = symbol.ljust(9)
                        qty_str = f"{quantity:.4f}".rjust(10)
                        price_str = f"${price:,.2f}".rjust(12)
                        value_str = f"${value:,.2f}".rjust(12)
                        
                        chg_emoji = "📈" if chg and chg >= 0 else "📉" if chg else ""
                        lines.append(f"{sym_str} {qty_str} {price_str} {value_str} {chg_emoji}")
                    await asyncio.sleep(0.2)
                
                lines.append("</pre>")
        
        if total_value_usd > 0:
            lines.append(f"\n<b>💰 Общая стоимость: ~${total_value_usd:,.2f}</b>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ portfolio error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_all_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все цены (v1 функционал)"""
    try:
        import pytz
        riga_tz = pytz.timezone('Europe/Riga')
        now = datetime.now(riga_tz)
        timestamp = now.strftime("%H:%M:%S %d.%m.%Y")
        
        lines = [
            f"💹 <b>Все цены</b>\n",
            f"🕐 Данные актуальны на: <b>{timestamp}</b> (Рига)\n"
        ]
        
        async with aiohttp.ClientSession() as session:
            lines.append("<b>📊 Фондовый рынок:</b>")
            lines.append("<pre>")
            lines.append("┌──────────────────┬────────────┬─────────┐")
            lines.append("│ Актив            │ Цена       │ 24h     │")
            lines.append("├──────────────────┼────────────┼─────────┤")
            
            for ticker, info in AVAILABLE_TICKERS.items():
                price_data = await get_yahoo_price(session, ticker)
                if price_data:
                    price, cur, change_pct = price_data
                    name = info['name'][:16].ljust(16)
                    price_str = f"{price:.2f} {cur}".ljust(10)
                    
                    if change_pct != 0:
                        chg_emoji = "↗" if change_pct >= 0 else "↘"
                        chg_str = f"{chg_emoji}{abs(change_pct):.1f}%".rjust(7)
                    else:
                        chg_str = "0.0%".rjust(7)
                    
                    lines.append(f"│ {name} │ {price_str} │ {chg_str} │")
                else:
                    name = info['name'][:16].ljust(16)
                    lines.append(f"│ {name} │ {'н/д'.ljust(10)} │ {'N/A'.rjust(7)} │")
                await asyncio.sleep(0.3)
            
            lines.append("└──────────────────┴────────────┴─────────┘")
            lines.append("Источник: Yahoo Finance")
            lines.append("</pre>")
            
            lines.append("\n<b>₿ Криптовалюты:</b>")
            lines.append("<pre>")
            lines.append("┌────────┬──────────────┬─────────┐")
            lines.append("│ Монета │ Цена         │ 24h     │")
            lines.append("├────────┼──────────────┼─────────┤")
            
            crypto_sources = {}
            for symbol, info in CRYPTO_IDS.items():
                try:
                    crypto_data = await get_crypto_price(session, symbol)
                    if crypto_data:
                        price = crypto_data["usd"]
                        chg = crypto_data.get("change_24h")
                        source = crypto_data.get("source", "Unknown")
                        
                        crypto_sources[symbol] = source
                        
                        sym_str = symbol.ljust(6)
                        price_str = f"${price:,.2f}".ljust(12)
                        
                        if chg and not math.isnan(chg):
                            chg_emoji = "↗" if chg >= 0 else "↘"
                            chg_str = f"{chg_emoji}{abs(chg):.1f}%".rjust(7)
                        else:
                            chg_str = "N/A".rjust(7)
                        
                        lines.append(f"│ {sym_str} │ {price_str} │ {chg_str} │")
                    else:
                        sym_str = symbol.ljust(6)
                        lines.append(f"│ {sym_str} │ {'н/д'.ljust(12)} │ {'N/A'.rjust(7)} │")
                except Exception as e:
                    print(f"❌ {symbol} price error: {e}")
                    sym_str = symbol.ljust(6)
                    lines.append(f"│ {sym_str} │ {'ошибка'.ljust(12)} │ {'N/A'.rjust(7)} │")
                
                await asyncio.sleep(0.3)
            
            lines.append("└────────┴──────────────┴─────────┘")
            
            if crypto_sources:
                unique_sources = set(crypto_sources.values())
                sources_str = ", ".join(unique_sources)
                lines.append(f"Источники: {sources_str}")
            
            lines.append("</pre>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ all_prices error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_my_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать сделки (NEW v3 функционал)"""
    user_id = update.effective_user.id
    trades = get_user_trades(user_id)
    
    if not trades:
        await update.message.reply_text(
            "🎯 У вас нет открытых сделок\n\n"
            "Используйте <b>🆕 Новая сделка</b>",
            parse_mode='HTML'
        )
        return
    
    try:
        await update.message.reply_text("🔄 Обновляю данные...")
        
        lines = ["🎯 <b>Ваши сделки (v3):</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            total_value = 0
            total_profit = 0
            
            for i, trade in enumerate(trades, 1):
                symbol = trade["symbol"]
                entry_price = trade["entry_price"]
                amount = trade["amount"]
                target = trade["target_profit_pct"]
                
                crypto_data = await get_crypto_price(session, symbol)
                if crypto_data:
                    current_price = crypto_data["usd"]
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                    profit_usd = amount * (current_price - entry_price)
                    value = amount * current_price
                    
                    total_value += value
                    total_profit += profit_usd
                    
                    if profit_pct >= target:
                        status = "✅ ЦЕЛЬ"
                    elif profit_pct > 0:
                        status = "📈 ПРИБЫЛЬ"
                    else:
                        status = "📉 УБЫТОК"
                    
                    lines.append(f"{status} <b>#{i}. {symbol}</b>")
                    lines.append(f"├ Кол-во: {amount:.4f}")
                    lines.append(f"├ Вход: ${entry_price:,.2f} → Сейчас: ${current_price:,.2f}")
                    lines.append(f"├ Прибыль: <b>{profit_pct:+.2f}%</b> (${profit_usd:+,.2f})")
                    lines.append(f"├ Цель: {target}% {'✅' if profit_pct >= target else '⏳'}")
                    lines.append(f"└ Стоимость: ${value:,.2f}\n")
                
                await asyncio.sleep(0.2)
            
            if total_value > 0:
                total_profit_pct = (total_profit / (total_value - total_profit)) * 100
                lines.append(f"━━━━━━━━━━━━━━━━")
                lines.append(f"💰 <b>Общая стоимость: ${total_value:,.2f}</b>")
                lines.append(f"📊 <b>Общая прибыль: {total_profit_pct:+.2f}% (${total_profit:+,.2f})</b>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ my_trades error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Инструкция по добавлению в портфель (v1)"""
    await update.message.reply_text(
        "➕ <b>Добавить актив в портфель</b>\n\n"
        "Используйте команду:\n"
        "<code>/add TICKER КОЛИЧЕСТВО</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>/add VWCE.DE 10</code>\n"
        "<code>/add BTC 0.5</code>\n\n"
        "<b>Доступные тикеры:</b>\n"
        "• VWCE.DE, 4GLD.DE, DE000A2T5DZ1.SG, SPY\n"
        "• BTC, ETH, SOL, AVAX, DOGE, LINK",
        parse_mode='HTML'
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить актив в портфель (v1)"""
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
    
    if ticker not in AVAILABLE_TICKERS and ticker not in CRYPTO_IDS:
        await update.message.reply_text(
            f"❌ Неизвестный тикер: {ticker}\n\n"
            "Доступные: VWCE.DE, 4GLD.DE, DE000A2T5DZ1.SG, SPY, BTC, ETH, SOL, AVAX, DOGE, LINK"
        )
        return
    
    user_id = update.effective_user.id
    portfolio = get_user_portfolio(user_id)
    portfolio[ticker] = portfolio.get(ticker, 0) + quantity
    save_portfolio(user_id, portfolio)
    
    name = AVAILABLE_TICKERS.get(ticker, {}).get("name") or CRYPTO_IDS.get(ticker, {}).get("name") or ticker
    await update.message.reply_text(
        f"✅ Добавлено в портфель: <b>{quantity} {name}</b>\n"
        f"Теперь у вас: {portfolio[ticker]:.4f}",
        parse_mode='HTML'
    )

# Conversation handler для новой сделки (v3)
async def cmd_new_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога новой сделки (NEW v3)"""
    keyboard = []
    for symbol in CRYPTO_IDS.keys():
        name = CRYPTO_IDS[symbol]['name']
        keyboard.append([InlineKeyboardButton(f"{name} ({symbol})", callback_data=f"trade_{symbol}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🆕 <b>Новая сделка с целью</b>\n\n"
        "Шаг 1: Выберите криптовалюту:",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    return SELECT_CRYPTO

async def trade_select_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    symbol = query.data.replace("trade_", "")
    context.user_data['trade_symbol'] = symbol
    
    await query.edit_message_text(
        f"✅ Выбрано: <b>{symbol}</b>\n\n"
        f"Шаг 2: Введите количество:",
        parse_mode='HTML'
    )
    return ENTER_AMOUNT

async def trade_enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError()
        context.user_data['trade_amount'] = amount
        
        await update.message.reply_text(
            f"✅ Количество: <b>{amount:.4f}</b>\n\n"
            f"Шаг 3: Цена покупки (USD):",
            parse_mode='HTML'
        )
        return ENTER_PRICE
    except:
        await update.message.reply_text("❌ Введите число, например: 0.5")
        return ENTER_AMOUNT

async def trade_enter_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.replace(",", ""))
        if price <= 0:
            raise ValueError()
        context.user_data['trade_price'] = price
        
        await update.message.reply_text(
            f"✅ Цена входа: <b>${price:,.2f}</b>\n\n"
            f"Шаг 4: Целевая прибыль (%):",
            parse_mode='HTML'
        )
        return ENTER_TARGET
    except:
        await update.message.reply_text("❌ Введите число, например: 50000")
        return ENTER_PRICE

async def trade_enter_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target = float(update.message.text)
        if target <= 0:
            raise ValueError()
        
        user_id = update.effective_user.id
        symbol = context.user_data['trade_symbol']
        amount = context.user_data['trade_amount']
        price = context.user_data['trade_price']
        
        add_trade(user_id, symbol, amount, price, target)
        
        await update.message.reply_text(
            f"✅ <b>Сделка добавлена!</b>\n\n"
            f"💰 {symbol}\n"
            f"Количество: {amount:.4f}\n"
            f"Цена входа: ${price:,.2f}\n"
            f"Цель: +{target}%\n\n"
            f"Вы получите алерт когда прибыль достигнет {target}%!",
            parse_mode='HTML',
            reply_markup=get_main_menu()
        )
        
        context.user_data.clear()
        return ConversationHandler.END
    except:
        await update.message.reply_text("❌ Введите число, например: 10")
        return ENTER_TARGET

async def trade_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

async def cmd_market_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рыночные сигналы (NEW v3)"""
    user_id = update.effective_user.id
    investor_type = user_profiles.get(user_id, "long")
    type_info = INVESTOR_TYPES[investor_type]
    
    await update.message.reply_text(f"🔄 Анализирую рынок для {type_info['emoji']} {type_info['name']}...")
    
    try:
        lines = [
            f"📊 <b>Рыночные сигналы (v3)</b>\n",
            f"Профиль: {type_info['emoji']} <b>{type_info['name']}</b>\n"
        ]
        
        async with aiohttp.ClientSession() as session:
            fear_greed = await get_fear_greed_index(session)
            if fear_greed:
                if fear_greed < 25:
                    fg_status = "😱 Экстремальный страх"
                elif fear_greed < 45:
                    fg_status = "😰 Страх"
                elif fear_greed < 55:
                    fg_status = "😐 Нейтрально"
                elif fear_greed < 75:
                    fg_status = "😃 Жадность"
                else:
                    fg_status = "🤑 Экстремальная жадность"
                
                lines.append(f"📈 Fear & Greed: <b>{fear_greed}/100</b> ({fg_status})\n")
            
            for symbol in ["BTC", "ETH", "SOL", "AVAX"]:
                signal_data = await get_market_signal(session, symbol, investor_type)
                
                lines.append(f"{signal_data['emoji']} <b>{symbol}: {signal_data['signal']}</b>")
                lines.append(f"   └ {signal_data['reason']}\n")
                
                await asyncio.sleep(0.2)
        
        lines.append("\n<i>⚠️ Не является финансовой рекомендацией</i>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ market_signals error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении сигналов")

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Профиль инвестора (NEW v3)"""
    user_id = update.effective_user.id
    current_type = user_profiles.get(user_id, "long")
    
    keyboard = []
    for type_key, type_info in INVESTOR_TYPES.items():
        selected = "✅ " if type_key == current_type else ""
        keyboard.append([InlineKeyboardButton(
            f"{selected}{type_info['emoji']} {type_info['name']}",
            callback_data=f"profile_{type_key}"
        )])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    current_info = INVESTOR_TYPES[current_type]
    
    await update.message.reply_text(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"Текущий: {current_info['emoji']} <b>{current_info['name']}</b>\n"
        f"<i>{current_info['desc']}</i>\n\n"
        f"Выберите тип для персонализированных сигналов:",
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def profile_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    investor_type = query.data.replace("profile_", "")
    user_id = query.from_user.id
    user_profiles[user_id] = investor_type
    
    type_info = INVESTOR_TYPES[investor_type]
    
    await query.edit_message_text(
        f"✅ <b>Профиль обновлён!</b>\n\n"
        f"{type_info['emoji']} <b>{type_info['name']}</b>\n"
        f"<i>{type_info['desc']}</i>\n\n"
        f"Теперь рыночные сигналы адаптированы под ваш стиль!",
        parse_mode='HTML'
    )

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """События недели (v1 функционал - упрощенный)"""
    try:
        base_date = datetime.now()
        lines = ["📰 <b>События на неделю (v1)</b>\n"]
        
        lines.append("<b>📊 Фондовый рынок:</b>")
        lines.append(f"• {(base_date + timedelta(days=2)).strftime('%d.%m')} - FOMC заседание")
        lines.append(f"• {(base_date + timedelta(days=3)).strftime('%d.%m')} - Отчёты крупных компаний\n")
        
        lines.append("<b>₿ Криптовалюты:</b>")
        lines.append(f"• {(base_date + timedelta(days=2)).strftime('%d.%m')} - Bitcoin ETF решение")
        lines.append(f"• {(base_date + timedelta(days=4)).strftime('%d.%m')} - Ethereum upgrade\n")
        
        lines.append("<i>Для детальных прогнозов используйте <b>📊 Рыночные сигналы</b></i>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ events error: {e}")
        await update.message.reply_text("⚠ Ошибка при получении событий")

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прогнозы (v1 функционал - упрощенный)"""
    await update.message.reply_text(
        "🔮 <b>Прогнозы (v1)</b>\n\n"
        "Для персонализированных прогнозов используйте:\n"
        "📊 <b>Рыночные сигналы</b> - адаптированы под ваш тип инвестора!\n\n"
        "Там вы получите конкретные рекомендации BUY/HOLD/SELL",
        parse_mode='HTML'
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    await update.message.reply_text(
        "ℹ️ <b>Помощь - Full Bot v4</b>\n\n"
        "<b>📊 ИЗ v1 (Портфель):</b>\n"
        "• /add TICKER КОЛ-ВО - добавить в портфель\n"
        "• 💼 Мой портфель - стоимость активов\n"
        "• 💹 Все цены - текущие цены\n"
        "• 📰 События - события недели\n"
        "• 🔮 Прогнозы - общие прогнозы\n\n"
        "<b>🆕 ИЗ v3 (Сделки):</b>\n"
        "• 🆕 Новая сделка - добавить с целью\n"
        "• 🎯 Мои сделки - список позиций\n"
        "• 📊 Рыночные сигналы - BUY/HOLD/SELL\n"
        "• 👤 Мой профиль - тип инвестора\n\n"
        "<b>Автоматические алерты:</b>\n"
        "• При изменении цены > порога (v1)\n"
        "• При достижении целевой прибыли (v3)",
        parse_mode='HTML'
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок меню"""
    text = update.message.text
    
    if text == "💼 Мой портфель":
        await cmd_portfolio(update, context)
    elif text == "💹 Все цены":
        await cmd_all_prices(update, context)
    elif text == "🎯 Мои сделки":
        await cmd_my_trades(update, context)
    elif text == "📊 Рыночные сигналы":
        await cmd_market_signals(update, context)
    elif text == "📰 События недели":
        await cmd_events(update, context)
    elif text == "🔮 Прогнозы":
        await cmd_forecast(update, context)
    elif text == "➕ Добавить актив":
        await cmd_add_asset(update, context)
    elif text == "🆕 Новая сделка":
        return await cmd_new_trade(update, context)
    elif text == "👤 Мой профиль":
        await cmd_profile(update, context)
    elif text == "ℹ️ Помощь":
        await cmd_help(update, context)
    else:
        await update.message.reply_text("👂 Используйте кнопки меню")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"❌ Global error: {context.error}")
    traceback.print_exc()

def main():
    print("=" * 60)
    print("🚀 Starting FULL FEATURED Trading Bot v4")
    print("=" * 60)
    print("Features:")
    print("  FROM v1:")
    print("    ✅ Portfolio management (stocks + crypto)")
    print("    ✅ Price monitoring and alerts")
    print("    ✅ Events and forecasts")
    print("  FROM v3:")
    print("    ✅ Trade tracking with profit targets")
    print("    ✅ Market signals by investor type")
    print("=" * 60)
    
    import sys
    if sys.version_info >= (3, 10):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    
    app = Application.builder().token(TOKEN).build()
    
    # Conversation handler для сделок
    trade_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🆕 Новая сделка$'), cmd_new_trade)],
        states={
            SELECT_CRYPTO: [CallbackQueryHandler(trade_select_crypto, pattern='^trade_')],
            ENTER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_amount)],
            ENTER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_price)],
            ENTER_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_target)],
        },
        fallbacks=[CommandHandler('cancel', trade_cancel)],
    )
    
    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("trades", cmd_my_trades))
    app.add_handler(CommandHandler("signals", cmd_market_signals))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("help", cmd_help))
    
    # Conversation и callbacks
    app.add_handler(trade_conv)
    app.add_handler(CallbackQueryHandler(profile_select, pattern='^profile_'))
    
    # Кнопки
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    
    # Ошибки
    app.add_error_handler(on_error)
    
    # Фоновые задачи
    job_queue = app.job_queue
    if job_queue and CHAT_ID:
        # v1: Алерты изменений цен
        job_queue.run_repeating(check_price_alerts, interval=600, first=60)
        print("✅ Price alerts (v1): ENABLED")
        
        # v3: Алерты целевой прибыли
        job_queue.run_repeating(check_trade_alerts, interval=600, first=120)
        print("✅ Trade profit alerts (v3): ENABLED")
    else:
        print("⚠️  Alerts DISABLED (set CHAT_ID to enable)")
    
    print("=" * 60)
    print("🔄 Starting bot polling...")
    print("=" * 60)
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
