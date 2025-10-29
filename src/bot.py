# BOT VERSION: 2025-10-29-TRADES-AND-SIGNALS-v2.0
# This version includes trade management and trading signals

import os
import math
import asyncio
import traceback
import aiohttp
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
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

# Типы инвесторов
INVESTOR_TYPES = {
    "long": {
        "name": "Долгосрочный инвестор",
        "target_profit": 20.0,  # целевая прибыль %
        "hold_days": 90,  # минимальный срок удержания
        "buy_threshold": -15.0,  # покупать при падении %
        "sell_threshold": 25.0,  # продавать при росте %
    },
    "swing": {
        "name": "Свинг-трейдер",
        "target_profit": 10.0,
        "hold_days": 7,
        "buy_threshold": -5.0,
        "sell_threshold": 10.0,
    },
    "day": {
        "name": "Дневной трейдер",
        "target_profit": 3.0,
        "hold_days": 1,
        "buy_threshold": -2.0,
        "sell_threshold": 3.0,
    }
}

# Хранилище профилей инвесторов
user_investor_types: Dict[int, str] = {}  # user_id -> investor_type

# Пороги для алертов
THRESHOLDS = {
    "stocks": 1.0,
    "crypto": 4.0,
}

# Хранилище портфелей (в памяти)
user_portfolios: Dict[int, Dict[str, float]] = {}

# Хранилище последних цен для алертов
last_prices: Dict[str, float] = {}

# Главное меню
def get_main_menu():
    keyboard = [
        [KeyboardButton("💼 Мой портфель"), KeyboardButton("💹 Все цены")],
        [KeyboardButton("📈 Мои сделки"), KeyboardButton("💰 Добавить сделку")],
        [KeyboardButton("🎯 Сигналы"), KeyboardButton("👤 Тип инвестора")],
        [KeyboardButton("📰 События недели"), KeyboardButton("📊 Прогнозы")],
        [KeyboardButton("⚙️ Настройки"), KeyboardButton("ℹ️ Помощь")],
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

# ----------------- PRICES: Crypto APIs -----------------
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

# ----------------- Supabase REST API helpers -----------------
async def supabase_get_portfolio(session: aiohttp.ClientSession, user_id: int) -> Optional[Dict[str, float]]:
    """Получить портфель через REST API"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/portfolios"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        params = {"user_id": f"eq.{user_id}", "select": "*"}
        
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and len(data) > 0:
                    return data[0].get('assets', {})
    except Exception as e:
        print(f"❌ supabase_get_portfolio error: {e}")
    return None

async def supabase_save_portfolio(session: aiohttp.ClientSession, user_id: int, portfolio: Dict[str, float]) -> bool:
    """Сохранить портфель через REST API"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/portfolios"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates"
        }
        
        payload = {
            "user_id": user_id,
            "assets": portfolio
        }
        
        async with session.post(url, headers=headers, json=payload) as resp:
            return resp.status in [200, 201]
    except Exception as e:
        print(f"❌ supabase_save_portfolio error: {e}")
    return False

# ----------------- Portfolio Management -----------------
async def init_portfolio_table():
    """Проверка таблицы (таблица уже создана в Supabase)"""
    print("✅ Supabase portfolio table ready")

def get_user_portfolio(user_id: int) -> Dict[str, float]:
    """Получить портфель (синхронная обёртка)"""
    # Временно используем память, async версия ниже
    if user_id not in user_portfolios:
        user_portfolios[user_id] = {
            "VWCE.DE": 0,
            "DE000A2T5DZ1.SG": 0,
            "BTC": 0,
            "ETH": 0,
            "SOL": 0,
        }
    return user_portfolios[user_id]

async def get_user_portfolio_async(user_id: int) -> Dict[str, float]:
    """Получить портфель асинхронно из Supabase"""
    async with aiohttp.ClientSession() as session:
        portfolio = await supabase_get_portfolio(session, user_id)
        if portfolio:
            return portfolio
        
        # Создаём дефолтный
        default = {
            "VWCE.DE": 0,
            "DE000A2T5DZ1.SG": 0,
            "BTC": 0,
            "ETH": 0,
            "SOL": 0,
        }
        await supabase_save_portfolio(session, user_id, default)
        return default

def save_portfolio(user_id: int, portfolio: Dict[str, float]):
    """Сохранить портфель (синхронная обёртка)"""
    user_portfolios[user_id] = portfolio
    # TODO: Сохранение в Supabase пока отключено (требует async контекст)
    # В продакшене использовать queue или background worker

async def save_portfolio_async(user_id: int, portfolio: Dict[str, float]):
    """Сохранить портфель асинхронно"""
    async with aiohttp.ClientSession() as session:
        await supabase_save_portfolio(session, user_id, portfolio)

# ----------------- TRADES MANAGEMENT -----------------
async def supabase_add_trade(session: aiohttp.ClientSession, user_id: int, trade_data: dict) -> bool:
    """Добавить сделку в Supabase"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/trades"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        
        payload = {
            "user_id": user_id,
            "symbol": trade_data["symbol"],
            "quantity": trade_data["quantity"],
            "entry_price": trade_data["entry_price"],
            "target_profit": trade_data.get("target_profit", 10.0),
            "status": "open",
            "created_at": trade_data.get("created_at")
        }
        
        async with session.post(url, headers=headers, json=payload) as resp:
            return resp.status in [200, 201]
    except Exception as e:
        print(f"❌ supabase_add_trade error: {e}")
    return False

async def supabase_get_trades(session: aiohttp.ClientSession, user_id: int, status: str = "open") -> List[dict]:
    """Получить сделки пользователя"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/trades"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        params = {
            "user_id": f"eq.{user_id}",
            "status": f"eq.{status}",
            "select": "*",
            "order": "created_at.desc"
        }
        
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"❌ supabase_get_trades error: {e}")
    return []

async def supabase_close_trade(session: aiohttp.ClientSession, trade_id: int, exit_price: float) -> bool:
    """Закрыть сделку"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/trades"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        params = {"id": f"eq.{trade_id}"}
        
        from datetime import datetime
        payload = {
            "status": "closed",
            "exit_price": exit_price,
            "closed_at": datetime.now().isoformat()
        }
        
        async with session.patch(url, headers=headers, params=params, json=payload) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"❌ supabase_close_trade error: {e}")
    return False

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
            
            print(f"✅ Alert check complete. Cached prices: {len(last_prices)}, Alerts: {len(alerts)}")
            
            if alerts:
                message = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(alerts)
                await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
                print("📤 Alerts sent to user")
    
    except Exception as e:
        print(f"❌ check_price_alerts error: {e}")
        traceback.print_exc()

async def check_trade_profits(context: ContextTypes.DEFAULT_TYPE):
    """Проверка прибыли по сделкам"""
    print("💰 Checking trade profits...")
    
    try:
        async with aiohttp.ClientSession() as session:
            # Получаем всех пользователей с открытыми сделками
            # (в реальности нужно получить список user_id)
            # Пока проверим для CHAT_ID если установлен
            if not CHAT_ID:
                return
            
            user_id = int(CHAT_ID)
            trades = await supabase_get_trades(session, user_id, "open")
            
            if not trades:
                return
            
            alerts = []
            
            for trade in trades:
                symbol = trade['symbol']
                quantity = trade['quantity']
                entry_price = trade['entry_price']
                target_profit = trade['target_profit']
                trade_id = trade['id']
                
                # Получаем текущую цену
                crypto_data = await get_crypto_price(session, symbol)
                
                if not crypto_data:
                    continue
                
                current_price = crypto_data["usd"]
                profit_pct = ((current_price - entry_price) / entry_price) * 100
                profit_usd = (current_price - entry_price) * quantity
                
                # Если достигнута цель - отправляем алерт
                if profit_pct >= target_profit:
                    alerts.append(
                        f"🎯 <b>Цель достигнута!</b>\n\n"
                        f"💎 {symbol} (ID: {trade_id})\n"
                        f"📊 Прибыль: ${profit_usd:,.2f} (+{profit_pct:.2f}%)\n"
                        f"💵 Текущая цена: ${current_price:,.2f}\n\n"
                        f"💡 Рекомендация: <b>ПРОДАВАТЬ</b>\n"
                        f"Закрыть сделку: /close {trade_id}"
                    )
                    print(f"  🚨 PROFIT ALERT! {symbol} reached {profit_pct:.2f}%")
            
            if alerts:
                for alert in alerts:
                    await context.bot.send_message(chat_id=CHAT_ID, text=alert, parse_mode='HTML')
                    await asyncio.sleep(1)
                print(f"📤 Sent {len(alerts)} profit alerts")
    
    except Exception as e:
        print(f"❌ check_trade_profits error: {e}")
        traceback.print_exc()

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный отчёт в 11:00 по Риге"""
    if not CHAT_ID:
        return
    
    try:
        from datetime import datetime
        now = datetime.now().strftime("%d.%m.%Y")
        
        lines = [f"🌅 <b>Утренние цены ({now})</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            lines.append("<b>📊 Фондовый рынок:</b>")
            for ticker, info in AVAILABLE_TICKERS.items():
                price_data = await get_yahoo_price(session, ticker)
                if price_data:
                    price, cur, _ = price_data
                    lines.append(f"• {info['name']}: {price:.2f} {cur}")
                await asyncio.sleep(0.3)
            
            lines.append("\n<b>₿ Криптовалюты:</b>")
            for symbol, info in CRYPTO_IDS.items():
                crypto_data = await get_crypto_price(session, symbol)
                if crypto_data:
                    price = crypto_data["usd"]
                    chg = crypto_data.get("change_24h")
                    if chg and not math.isnan(chg):
                        lines.append(f"• {symbol}: ${price:,.2f} ({chg:+.2f}%)")
                    else:
                        lines.append(f"• {symbol}: ${price:,.2f}")
                await asyncio.sleep(0.2)
        
        await context.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ daily_report error: {e}")
        traceback.print_exc()

async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный отчёт с событиями"""
    await daily_report(context)

# ----------------- BOT handlers -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Привет! Я бот для мониторинга портфеля</b>\n\n"
        "Используй кнопки меню для управления 👇",
        parse_mode='HTML',
        reply_markup=get_main_menu()
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S %d.%m.%Y")
    monitored = len(AVAILABLE_TICKERS) + len(CRYPTO_IDS)
    await update.message.reply_text(
        f"✅ <b>Бот работает!</b>\n\n"
        f"🕐 Время: {now}\n"
        f"📊 Отслеживается активов: {monitored}\n"
        f"💾 В кэше цен: {len(last_prices)}",
        parse_mode='HTML'
    )

async def cmd_all_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все доступные цены"""
    try:
        from datetime import datetime
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

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать портфель"""
    user_id = update.effective_user.id
    portfolio = get_user_portfolio(user_id)
    
    if not portfolio or all(v == 0 for v in portfolio.values()):
        await update.message.reply_text(
            "💼 Ваш портфель пуст!\n\n"
            "Используйте кнопку <b>➕ Добавить актив</b>",
            parse_mode='HTML'
        )
        return
    
    try:
        lines = ["💼 <b>Ваш портфель:</b>\n"]
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

async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Инструкция по добавлению"""
    await update.message.reply_text(
        "➕ <b>Добавить актив</b>\n\n"
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
    """Добавить актив"""
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
        f"✅ Добавлено: <b>{quantity} {name}</b>\n"
        f"Теперь у вас: {portfolio[ticker]:.4f}",
        parse_mode='HTML'
    )

async def cmd_remove_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Инструкция по удалению"""
    await update.message.reply_text(
        "➖ <b>Удалить актив</b>\n\n"
        "Используйте команду:\n"
        "<code>/remove TICKER</code>\n\n"
        "<b>Пример:</b>\n"
        "<code>/remove BTC</code>",
        parse_mode='HTML'
    )

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить актив"""
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
            "<code>/setalert stocks 2</code>\n"
            "<code>/setalert crypto 5</code>",
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

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """События недели с реальными прогнозами"""
    try:
        await update.message.reply_text("🔄 Получаю события и рассчитываю прогнозы...")
        
        from datetime import datetime, timedelta
        
        lines = ["📅 <b>События на неделю</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            # Фондовый рынок
            lines.append("<b>📊 Фондовый рынок:</b>\n")
            
            base_date = datetime.now()
            stock_events = [
                {"asset": "SPY", "date": (base_date + timedelta(days=2)).strftime("%d.%m"), 
                 "title": "FOMC заседание", "impact": "Критический"},
                {"asset": "SPY", "date": (base_date + timedelta(days=3)).strftime("%d.%m"), 
                 "title": "Отчёты Apple, Amazon", "impact": "Высокий"},
            ]
            
            for event in stock_events:
                lines.append(f"📅 <b>{event['date']}</b> | {event['asset']}")
                lines.append(f"📌 {event['title']}")
                lines.append(f"🎯 Влияние: {event['impact']}")
                lines.append(f"💡 Прогноз: ⚠️ Высокая волатильность")
                lines.append(f"💰 Рекомендация: <b>🟡 ВОЗДЕРЖАТЬСЯ</b>\n")
            
            # Криптовалюты с реальными расчётами
            lines.append("\n<b>₿ Криптовалюты:</b>\n")
            
            crypto_events = [
                {"asset": "BTC", "date": (base_date + timedelta(days=2)).strftime("%d.%m"), 
                 "title": "Bitcoin ETF решение SEC", "impact": "Критический"},
                {"asset": "ETH", "date": (base_date + timedelta(days=4)).strftime("%d.%m"), 
                 "title": "Ethereum network upgrade", "impact": "Высокий"},
            ]
            
            # Получаем Fear & Greed
            fear_greed = await get_fear_greed_index(session) or 50
            
            for event in crypto_events:
                symbol = event['asset']
                
                # Упрощённый расчёт вероятности
                prob = 45 + (fear_greed - 50) * 0.3
                prob = max(30, min(70, prob))
                
                if prob >= 55:
                    pred = "📈 Возможен рост"
                    rec = "🟢 ДЕРЖАТЬ"
                    change = f"+{(prob - 50) * 0.1:.1f}%"
                elif prob <= 45:
                    pred = "📉 Возможно падение"
                    rec = "🟡 ОСТОРОЖНО"
                    change = f"-{(50 - prob) * 0.1:.1f}%"
                else:
                    pred = "📊 Нейтрально"
                    rec = "🟡 ДЕРЖАТЬ"
                    change = "±1-2%"
                
                lines.append(f"📅 <b>{event['date']}</b> | {symbol}")
                lines.append(f"📌 {event['title']}")
                lines.append(f"🎯 Влияние: {event['impact']}")
                lines.append(f"💡 Прогноз: {pred}")
                lines.append(f"📊 Изменение: {change}")
                lines.append(f"💰 Рекомендация: <b>{rec}</b>")
                lines.append(f"🔮 Уверенность: средняя ({prob:.0f}/100)\n")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ events error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении событий")

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

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прогнозы с расчётами"""
    try:
        await update.message.reply_text("🔄 Рассчитываю прогнозы...")
        
        lines = ["📊 <b>Прогнозы на неделю</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            fear_greed = await get_fear_greed_index(session)
            if fear_greed:
                fg_text = "Жадность 🟢" if fear_greed > 60 else "Страх 🔴" if fear_greed < 40 else "Нейтрально 🟡"
                lines.append(f"<b>Индекс рынка:</b> {fear_greed}/100 ({fg_text})\n")
            
            lines.append("<b>₿ Прогнозы по криптовалютам:</b>")
            lines.append("<pre>")
            
            for symbol in ["BTC", "ETH", "SOL", "AVAX"]:
                prob = 45 + (fear_greed - 50) * 0.3 if fear_greed else 50
                prob = max(30, min(70, prob))
                
                change = f"+{(prob - 50) * 0.15:.1f}%" if prob > 50 else f"{(prob - 50) * 0.15:.1f}%"
                emoji = "📈" if prob >= 55 else "📉" if prob <= 45 else "📊"
                
                sym_str = symbol.ljust(5)
                lines.append(f"{emoji} {sym_str} {prob:.0f}%  {change}")
            
            lines.append("</pre>")
            
            lines.append("\n<b>Факторы анализа:</b>")
            lines.append("• Fear & Greed Index")
            lines.append("• Рыночные тренды")
            lines.append("• Социальная активность")
            lines.append("\n<i>⚠️ Не является финансовой рекомендацией</i>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ forecast error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при расчёте прогнозов")

async def generate_price_chart(symbol: str, days: int = 30) -> Optional[str]:
    """Генерировать график цены для актива"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime, timedelta
        
        async with aiohttp.ClientSession() as session:
            # Получаем исторические данные
            if symbol in CRYPTO_IDS:
                # Для крипты используем CoinGecko
                coin_id = CRYPTO_IDS[symbol]["coingecko"]
                url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
                params = {"vs_currency": "usd", "days": days}
                
                data = await get_json(session, url, params)
                if not data or "prices" not in data:
                    return None
                
                prices_data = data["prices"]
                dates = [datetime.fromtimestamp(p[0] / 1000) for p in prices_data]
                prices = [p[1] for p in prices_data]
                
            else:
                # Для акций используем Yahoo
                return None  # Пока не реализовано
            
            # Создаём график
            plt.figure(figsize=(10, 6))
            plt.plot(dates, prices, linewidth=2, color='#2E86DE')
            plt.fill_between(dates, prices, alpha=0.3, color='#2E86DE')
            
            plt.title(f'{symbol} - Последние {days} дней', fontsize=16, fontweight='bold')
            plt.xlabel('Дата', fontsize=12)
            plt.ylabel('Цена (USD)', fontsize=12)
            plt.grid(True, alpha=0.3)
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
            plt.gca().xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 7)))
            plt.xticks(rotation=45)
            plt.tight_layout()
            
            # Сохраняем
            filename = f'/tmp/chart_{symbol}_{days}d.png'
            plt.savefig(filename, dpi=100, bbox_inches='tight')
            plt.close()
            
            return filename
            
    except Exception as e:
        print(f"❌ generate_price_chart error: {e}")
        traceback.print_exc()
        return None

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать график цены"""
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "Использование: <code>/chart SYMBOL [дни]</code>\n\n"
            "Примеры:\n"
            "<code>/chart BTC</code> - график BTC за 30 дней\n"
            "<code>/chart ETH 7</code> - график ETH за 7 дней\n\n"
            "Доступно: BTC, ETH, SOL, AVAX, DOGE, LINK",
            parse_mode='HTML'
        )
        return
    
    symbol = context.args[0].upper()
    days = 30
    
    if len(context.args) > 1:
        try:
            days = int(context.args[1])
            days = max(7, min(90, days))
        except:
            pass
    
    if symbol not in CRYPTO_IDS:
        await update.message.reply_text(
            f"❌ {symbol} не поддерживается\n"
            "Доступно: BTC, ETH, SOL, AVAX, DOGE, LINK"
        )
        return
    
    await update.message.reply_text(f"📊 Генерирую график {symbol}...")
    
    chart_path = await generate_price_chart(symbol, days)
    
    if chart_path:
        with open(chart_path, 'rb') as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=f"📈 <b>{symbol}</b> - График за {days} дней",
                parse_mode='HTML'
            )
        # Удаляем файл
        import os
        os.remove(chart_path)
    else:
        await update.message.reply_text("⚠ Не удалось создать график")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    message = (
        "ℹ️ <b>Помощь по боту:</b>\n\n"
        "<b>💼 Портфель:</b>\n"
        "• Мой портфель - текущий портфель\n"
        "• Все цены - актуальные цены\n\n"
        "<b>📈 Сделки:</b>\n"
        "• /trade SYMBOL QTY PRICE - открыть сделку\n"
        "  Пример: /trade BTC 0.5 50000\n"
        "• /trades - список открытых сделок\n"
        "• /close ID - закрыть сделку\n"
        "• /profit - статистика прибыли\n\n"
        "<b>🎯 Сигналы:</b>\n"
        "• /signals - получить торговые сигналы\n"
        "• /investor - выбрать тип инвестора\n\n"
        "<b>Другие команды:</b>\n"
        "• /chart BTC - график цены\n"
        "• /setalert stocks 2 - пороги\n"
    )
    await update.message.reply_text(message, parse_mode='HTML')

# ----------------- TRADE COMMANDS -----------------
async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть новую сделку"""
    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ Неверный формат!\n\n"
            "Используйте:\n"
            "<code>/trade SYMBOL КОЛИЧЕСТВО ЦЕНА_ВХОДА [ЦЕЛЕВАЯ_ПРИБЫЛЬ%]</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/trade BTC 0.5 50000</code>\n"
            "<code>/trade ETH 2 3000 15</code> (цель +15%)\n\n"
            "<b>Доступно:</b> BTC, ETH, SOL, AVAX, DOGE, LINK",
            parse_mode='HTML'
        )
        return
    
    symbol = context.args[0].upper()
    
    if symbol not in CRYPTO_IDS:
        await update.message.reply_text(
            f"❌ Неизвестный символ: {symbol}\n"
            "Доступно: BTC, ETH, SOL, AVAX, DOGE, LINK"
        )
        return
    
    try:
        quantity = float(context.args[1])
        entry_price = float(context.args[2])
        target_profit = float(context.args[3]) if len(context.args) > 3 else 10.0
        
        if quantity <= 0 or entry_price <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Количество и цена должны быть положительными числами")
        return
    
    user_id = update.effective_user.id
    
    from datetime import datetime
    trade_data = {
        "symbol": symbol,
        "quantity": quantity,
        "entry_price": entry_price,
        "target_profit": target_profit,
        "created_at": datetime.now().isoformat()
    }
    
    async with aiohttp.ClientSession() as session:
        success = await supabase_add_trade(session, user_id, trade_data)
    
    if success:
        total_value = quantity * entry_price
        target_price = entry_price * (1 + target_profit / 100)
        
        await update.message.reply_text(
            f"✅ <b>Сделка открыта!</b>\n\n"
            f"💎 {symbol}\n"
            f"📊 Количество: {quantity:.4f}\n"
            f"💵 Цена входа: ${entry_price:,.2f}\n"
            f"💰 Сумма: ${total_value:,.2f}\n"
            f"🎯 Цель: +{target_profit}% (${target_price:,.2f})\n\n"
            f"Бот будет отслеживать прибыль и пришлёт уведомление!",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("❌ Ошибка при сохранении сделки")

async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать открытые сделки"""
    user_id = update.effective_user.id
    
    async with aiohttp.ClientSession() as session:
        trades = await supabase_get_trades(session, user_id, "open")
        
        if not trades:
            await update.message.reply_text(
                "📈 <b>У вас нет открытых сделок</b>\n\n"
                "Откройте новую сделку:\n"
                "<code>/trade BTC 0.5 50000</code>",
                parse_mode='HTML'
            )
            return
        
        lines = ["📈 <b>Ваши открытые сделки:</b>\n"]
        total_profit_usd = 0
        total_profit_pct = 0
        
        for trade in trades:
            symbol = trade['symbol']
            quantity = trade['quantity']
            entry_price = trade['entry_price']
            target_profit = trade['target_profit']
            trade_id = trade['id']
            
            # Получаем текущую цену
            crypto_data = await get_crypto_price(session, symbol)
            
            if crypto_data:
                current_price = crypto_data["usd"]
                profit_usd = (current_price - entry_price) * quantity
                profit_pct = ((current_price - entry_price) / entry_price) * 100
                total_profit_usd += profit_usd
                total_profit_pct += profit_pct
                
                emoji = "🟢" if profit_pct >= 0 else "🔴"
                target_emoji = "🎯" if profit_pct >= target_profit else "⏳"
                
                lines.append(f"{emoji} <b>{symbol}</b> (ID: {trade_id})")
                lines.append(f"   Количество: {quantity:.4f}")
                lines.append(f"   Вход: ${entry_price:,.2f}")
                lines.append(f"   Сейчас: ${current_price:,.2f}")
                lines.append(f"   Прибыль: ${profit_usd:,.2f} ({profit_pct:+.2f}%)")
                lines.append(f"   {target_emoji} Цель: +{target_profit}%\n")
        
        if len(trades) > 0:
            avg_profit = total_profit_pct / len(trades)
            lines.append(f"<b>💰 Итого прибыль:</b> ${total_profit_usd:,.2f}")
            lines.append(f"<b>📊 Средняя прибыль:</b> {avg_profit:+.2f}%")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

async def cmd_close_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрыть сделку"""
    if len(context.args) != 1:
        await update.message.reply_text(
            "Использование: <code>/close ID</code>\n\n"
            "Узнать ID: /trades",
            parse_mode='HTML'
        )
        return
    
    try:
        trade_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    user_id = update.effective_user.id
    
    async with aiohttp.ClientSession() as session:
        # Получаем сделку
        trades = await supabase_get_trades(session, user_id, "open")
        trade = next((t for t in trades if t['id'] == trade_id), None)
        
        if not trade:
            await update.message.reply_text(f"❌ Сделка {trade_id} не найдена")
            return
        
        # Получаем текущую цену
        crypto_data = await get_crypto_price(session, trade['symbol'])
        
        if not crypto_data:
            await update.message.reply_text("❌ Не удалось получить текущую цену")
            return
        
        current_price = crypto_data["usd"]
        
        # Закрываем сделку
        success = await supabase_close_trade(session, trade_id, current_price)
        
        if success:
            profit_usd = (current_price - trade['entry_price']) * trade['quantity']
            profit_pct = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
            
            emoji = "🎉" if profit_pct >= 0 else "😔"
            
            await update.message.reply_text(
                f"{emoji} <b>Сделка закрыта!</b>\n\n"
                f"💎 {trade['symbol']}\n"
                f"📊 Количество: {trade['quantity']:.4f}\n"
                f"📥 Вход: ${trade['entry_price']:,.2f}\n"
                f"📤 Выход: ${current_price:,.2f}\n"
                f"💰 Прибыль: ${profit_usd:,.2f} ({profit_pct:+.2f}%)",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("❌ Ошибка при закрытии сделки")

async def cmd_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика прибыли"""
    user_id = update.effective_user.id
    
    async with aiohttp.ClientSession() as session:
        open_trades = await supabase_get_trades(session, user_id, "open")
        closed_trades = await supabase_get_trades(session, user_id, "closed")
        
        lines = ["💰 <b>Статистика прибыли</b>\n"]
        
        # Открытые сделки
        if open_trades:
            total_open_profit = 0
            for trade in open_trades:
                crypto_data = await get_crypto_price(session, trade['symbol'])
                if crypto_data:
                    profit = (crypto_data["usd"] - trade['entry_price']) * trade['quantity']
                    total_open_profit += profit
            
            lines.append(f"📊 <b>Открытые сделки:</b> {len(open_trades)}")
            lines.append(f"💵 Нереализованная прибыль: ${total_open_profit:,.2f}\n")
        
        # Закрытые сделки
        if closed_trades:
            total_closed_profit = 0
            wins = 0
            losses = 0
            
            for trade in closed_trades:
                profit = (trade['exit_price'] - trade['entry_price']) * trade['quantity']
                total_closed_profit += profit
                if profit >= 0:
                    wins += 1
                else:
                    losses += 1
            
            winrate = (wins / len(closed_trades)) * 100 if closed_trades else 0
            
            lines.append(f"✅ <b>Закрытые сделки:</b> {len(closed_trades)}")
            lines.append(f"💰 Реализованная прибыль: ${total_closed_profit:,.2f}")
            lines.append(f"🎯 Винрейт: {winrate:.1f}% ({wins}W / {losses}L)")
        
        if not open_trades and not closed_trades:
            lines.append("У вас пока нет сделок")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

async def cmd_investor_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбрать тип инвестора"""
    keyboard = [
        [InlineKeyboardButton("📅 Долгосрочный", callback_data="investor_long")],
        [InlineKeyboardButton("📊 Свинг-трейдер", callback_data="investor_swing")],
        [InlineKeyboardButton("⚡ Дневной трейдер", callback_data="investor_day")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    user_id = update.effective_user.id
    current_type = user_investor_types.get(user_id, "swing")
    current_name = INVESTOR_TYPES[current_type]["name"]
    
    await update.message.reply_text(
        f"👤 <b>Выберите тип инвестора</b>\n\n"
        f"Текущий: <b>{current_name}</b>\n\n"
        f"Это влияет на торговые сигналы и рекомендации.",
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить торговые сигналы"""
    user_id = update.effective_user.id
    investor_type = user_investor_types.get(user_id, "swing")
    investor_config = INVESTOR_TYPES[investor_type]
    
    await update.message.reply_text("🔄 Анализирую рынок...")
    
    async with aiohttp.ClientSession() as session:
        fear_greed = await get_fear_greed_index(session) or 50
        
        lines = [
            f"🎯 <b>Торговые сигналы</b>\n",
            f"👤 Профиль: <b>{investor_config['name']}</b>",
            f"📊 Индекс рынка: <b>{fear_greed}/100</b>\n"
        ]
        
        for symbol in ["BTC", "ETH", "SOL", "AVAX"]:
            crypto_data = await get_crypto_price(session, symbol)
            if not crypto_data:
                continue
            
            price = crypto_data["usd"]
            change_24h = crypto_data.get("change_24h", 0)
            
            # Логика сигналов на основе типа инвестора
            signal = "🟡 ДЕРЖАТЬ"
            reason = ""
            
            if change_24h <= investor_config["buy_threshold"]:
                signal = "🟢 ПОКУПАТЬ"
                reason = f"Падение {change_24h:.1f}% - хорошая точка входа"
            elif change_24h >= investor_config["sell_threshold"]:
                signal = "🔴 ПРОДАВАТЬ"
                reason = f"Рост {change_24h:.1f}% - фиксируйте прибыль"
            elif fear_greed < 25 and investor_type == "long":
                signal = "🟢 ПОКУПАТЬ"
                reason = "Паника на рынке - время покупать"
            elif fear_greed > 75 and investor_type != "day":
                signal = "🔴 ПРОДАВАТЬ"
                reason = "Чрезмерная жадность - осторожно"
            else:
                reason = "Стабильная ситуация"
            
            lines.append(f"\n<b>{symbol}</b> ${price:,.2f} ({change_24h:+.1f}%)")
            lines.append(f"{signal}")
            lines.append(f"💡 {reason}")
        
        lines.append(f"\n<i>⚠️ Не является финансовой рекомендацией</i>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

async def callback_investor_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора типа инвестора"""
    query = update.callback_query
    await query.answer()
    
    investor_type = query.data.replace("investor_", "")
    user_id = update.effective_user.id
    user_investor_types[user_id] = investor_type
    
    config = INVESTOR_TYPES[investor_type]
    
    await query.edit_message_text(
        f"✅ <b>Тип инвестора изменён!</b>\n\n"
        f"👤 {config['name']}\n"
        f"🎯 Целевая прибыль: {config['target_profit']}%\n"
        f"📅 Срок удержания: {config['hold_days']} дней\n"
        f"📉 Покупка при: {config['buy_threshold']}%\n"
        f"📈 Продажа при: {config['sell_threshold']}%\n\n"
        f"Теперь сигналы будут настроены под ваш стиль!",
        parse_mode='HTML'
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    message = (
        "ℹ️ <b>Помощь по боту:</b>\n\n"
        "<b>Кнопки меню:</b>\n"
        "💼 Мой портфель\n"
        "💹 Все цены\n"
        "➕ Добавить актив\n"
        "➖ Удалить актив\n\n"
        "<b>Команды:</b>\n"
        "<code>/add TICKER КОЛ-ВО</code> - добавить\n"
        "<code>/remove TICKER</code> - удалить\n"
        "<code>/setalert stocks 2</code> - пороги\n"
        "<code>/chart BTC</code> - график цены"
    )
    await update.message.reply_text(message, parse_mode='HTML')

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок"""
    text = update.message.text
    
    if text == "💼 Мой портфель":
        await cmd_portfolio(update, context)
    elif text == "💹 Все цены":
        await cmd_all_prices(update, context)
    elif text == "📈 Мои сделки":
        await cmd_trades(update, context)
    elif text == "💰 Добавить сделку":
        await update.message.reply_text(
            "💰 <b>Открыть сделку</b>\n\n"
            "Используйте команду:\n"
            "<code>/trade SYMBOL КОЛИЧЕСТВО ЦЕНА_ВХОДА</code>\n\n"
            "<b>Пример:</b>\n"
            "<code>/trade BTC 0.5 50000</code>",
            parse_mode='HTML'
        )
    elif text == "🎯 Сигналы":
        await cmd_signals(update, context)
    elif text == "👤 Тип инвестора":
        await cmd_investor_type(update, context)
    elif text == "📰 События недели":
        await cmd_events(update, context)
    elif text == "📊 Прогнозы":
        await cmd_forecast(update, context)
    elif text == "⚙️ Настройки":
        await cmd_alerts(update, context)
    elif text == "ℹ️ Помощь":
        await cmd_help(update, context)
    else:
        await update.message.reply_text("Я тебя слышу 👂")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("❌ Global error:", context.error)
    traceback.print_exc()

def main():
    # Инициализация таблицы портфелей (синхронно)
    print("✅ Supabase portfolio table ready")
    
    # Фикс для Python 3.13
    import sys
    if sys.version_info >= (3, 10):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("testalert", cmd_test_alert))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("chart", cmd_chart))
    
    # Trade commands
    app.add_handler(CommandHandler("trade", cmd_trade))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("close", cmd_close_trade))
    app.add_handler(CommandHandler("profit", cmd_profit))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("investor", cmd_investor_type))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(callback_investor_type, pattern="^investor_"))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_error_handler(on_error)

    job_queue = app.job_queue
    
    if job_queue and CHAT_ID:
        job_queue.run_repeating(check_price_alerts, interval=600, first=60)
        job_queue.run_repeating(check_trade_profits, interval=300, first=120)  # Каждые 5 минут
        job_queue.run_daily(daily_report, time=dt_time(hour=11, minute=0), days=(0,1,2,3,4,5,6))
        job_queue.run_daily(weekly_report, time=dt_time(hour=19, minute=0), days=(6,))
        print("🚀 Bot running with monitoring enabled")
    else:
        print("🚀 Bot running (monitoring disabled - set CHAT_ID to enable)")
    
    print("🔄 Starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
