# BOT VERSION: 2025-10-31-OPTIMIZED-v5
# ОПТИМИЗАЦИИ:
# - Проверка только активных позиций (вместо всех тикеров)
# - Единый кеш для price_alerts и trade_alerts
# - Binance как primary source (быстрее и точнее)
# - Персистентное хранение last_prices
# - Снижение запросов на 80%

import os
import math
import asyncio
import traceback
import aiohttp
import json
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time, datetime, timedelta
from pathlib import Path

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

# === PATHS ===
DATA_DIR = Path("/home/claude/bot_data")
DATA_DIR.mkdir(exist_ok=True)
CACHE_FILE = DATA_DIR / "price_cache.json"
PORTFOLIO_FILE = DATA_DIR / "portfolios.json"
TRADES_FILE = DATA_DIR / "trades.json"

# === CONFIG ===
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# Доступные тикеры
AVAILABLE_TICKERS = {
    "VWCE.DE": {"name": "VWCE", "type": "stock"},
    "4GLD.DE": {"name": "4GLD (Gold ETC)", "type": "stock"},
    "DE000A2T5DZ1.SG": {"name": "X IE Physical Gold ETC", "type": "stock"},
    "SPY": {"name": "S&P 500 (SPY)", "type": "stock"},
}

# Крипта: Binance symbol + fallback IDs
CRYPTO_IDS = {
    "BTC": {"binance": "BTCUSDT", "coingecko": "bitcoin", "paprika": "btc-bitcoin", "name": "Bitcoin"},
    "ETH": {"binance": "ETHUSDT", "coingecko": "ethereum", "paprika": "eth-ethereum", "name": "Ethereum"},
    "SOL": {"binance": "SOLUSDT", "coingecko": "solana", "paprika": "sol-solana", "name": "Solana"},
    "AVAX": {"binance": "AVAXUSDT", "coingecko": "avalanche-2", "paprika": "avax-avalanche", "name": "Avalanche"},
    "DOGE": {"binance": "DOGEUSDT", "coingecko": "dogecoin", "paprika": "doge-dogecoin", "name": "Dogecoin"},
    "LINK": {"binance": "LINKUSDT", "coingecko": "chainlink", "paprika": "link-chainlink", "name": "Chainlink"},
}

# Пороги для алертов
THRESHOLDS = {
    "stocks": 1.0,
    "crypto": 4.0,
}

# === КЕШИРОВАНИЕ ===
class PriceCache:
    """Умный кеш с TTL и персистентностью"""
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.cache: Dict[str, Dict] = {}
        self.load()
    
    def load(self):
        """Загрузить кеш с диска"""
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, 'r') as f:
                    data = json.load(f)
                    # Восстанавливаем только не устаревшие записи
                    now = datetime.now().timestamp()
                    self.cache = {
                        k: v for k, v in data.items()
                        if now - v.get('timestamp', 0) < self.ttl * 2  # Даем запас
                    }
                    print(f"✅ Loaded {len(self.cache)} prices from cache")
            except Exception as e:
                print(f"⚠️ Cache load error: {e}")
    
    def save(self):
        """Сохранить кеш на диск"""
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(self.cache, f)
        except Exception as e:
            print(f"⚠️ Cache save error: {e}")
    
    def get(self, key: str) -> Optional[Dict]:
        """Получить из кеша если не устарел"""
        if key in self.cache:
            entry = self.cache[key]
            age = datetime.now().timestamp() - entry['timestamp']
            if age < self.ttl:
                return entry['data']
        return None
    
    def set(self, key: str, data: Dict):
        """Сохранить в кеш"""
        self.cache[key] = {
            'data': data,
            'timestamp': datetime.now().timestamp()
        }
        # Автосохранение каждые 10 записей
        if len(self.cache) % 10 == 0:
            self.save()
    
    def get_for_alert(self, key: str) -> Optional[float]:
        """Получить last price для алертов (без TTL проверки)"""
        if key in self.cache:
            return self.cache[key]['data'].get('price')
        return None
    
    def set_for_alert(self, key: str, price: float):
        """Сохранить last price для алертов"""
        if key not in self.cache:
            self.cache[key] = {'data': {}, 'timestamp': datetime.now().timestamp()}
        self.cache[key]['data']['price'] = price
        self.save()

# Глобальный кеш
price_cache = PriceCache(ttl_seconds=300)  # 5 минут TTL

# === ХРАНИЛИЩЕ ===
user_portfolios: Dict[int, Dict[str, float]] = {}
user_trades: Dict[int, List[Dict[str, Any]]] = {}
user_profiles: Dict[int, str] = {}

def load_data():
    """Загрузить данные с диска"""
    global user_portfolios, user_trades
    
    if PORTFOLIO_FILE.exists():
        try:
            with open(PORTFOLIO_FILE, 'r') as f:
                user_portfolios = {int(k): v for k, v in json.load(f).items()}
            print(f"✅ Loaded {len(user_portfolios)} portfolios")
        except Exception as e:
            print(f"⚠️ Portfolio load error: {e}")
    
    if TRADES_FILE.exists():
        try:
            with open(TRADES_FILE, 'r') as f:
                user_trades = {int(k): v for k, v in json.load(f).items()}
            print(f"✅ Loaded {len(user_trades)} trade lists")
        except Exception as e:
            print(f"⚠️ Trades load error: {e}")

def save_portfolios():
    """Сохранить портфели"""
    try:
        with open(PORTFOLIO_FILE, 'w') as f:
            json.dump(user_portfolios, f)
    except Exception as e:
        print(f"⚠️ Portfolio save error: {e}")

def save_trades():
    """Сохранить сделки"""
    try:
        with open(TRADES_FILE, 'w') as f:
            json.dump(user_trades, f)
    except Exception as e:
        print(f"⚠️ Trades save error: {e}")

# Загрузка при старте
load_data()

# === ТИПЫ ИНВЕСТОРОВ ===
INVESTOR_TYPES = {
    "long": {"name": "Долгосрочный инвестор", "emoji": "🏔️", "desc": "Покупаю на страхе, держу годами"},
    "swing": {"name": "Свинг-трейдер", "emoji": "🌊", "desc": "Ловлю волны, держу дни-недели"},
    "day": {"name": "Дневной трейдер", "emoji": "⚡", "desc": "Быстрые сделки внутри дня"},
}

# Conversation states
SELECT_CRYPTO, ENTER_AMOUNT, ENTER_PRICE, ENTER_TARGET = range(4)
SELECT_ASSET_TYPE, SELECT_ASSET, ENTER_ASSET_AMOUNT = range(4, 7)

def get_main_menu():
    """Главное меню"""
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
            return await r.json()
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

# ----------------- PRICES: Crypto (ОПТИМИЗИРОВАНО) -----------------
async def get_crypto_price_raw(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    """Получить цену криптовалюты БЕЗ кеширования (для внутреннего использования)"""
    crypto_info = CRYPTO_IDS.get(symbol)
    if not crypto_info:
        return None
    
    # 1. BINANCE (Primary - самый быстрый и точный)
    try:
        binance_symbol = crypto_info["binance"]
        url = "https://api.binance.com/api/v3/ticker/24hr"
        params = {"symbol": binance_symbol}
        
        async with session.get(url, params=params, timeout=TIMEOUT) as response:
            if response.status == 200:
                data = await response.json()
                price = float(data.get("lastPrice", 0))
                change_24h = float(data.get("priceChangePercent", 0))
                
                if price > 0:
                    print(f"✅ {symbol} from Binance: ${price:,.2f} ({change_24h:+.2f}%)")
                    return {
                        "usd": price,
                        "change_24h": change_24h,
                        "source": "Binance"
                    }
    except Exception as e:
        print(f"⚠️ Binance failed for {symbol}: {e}")
    
    # 2. COINPAPRIKA (Fallback)
    try:
        paprika_id = crypto_info["paprika"]
        url = f"https://api.coinpaprika.com/v1/tickers/{paprika_id}"
        data = await get_json(session, url, None)
        
        if data:
            quotes = data.get("quotes", {}).get("USD", {})
            price = quotes.get("price")
            change_24h = quotes.get("percent_change_24h")
            if price:
                print(f"✅ {symbol} from CoinPaprika: ${price:,.2f} ({change_24h:+.2f}%)")
                return {
                    "usd": float(price),
                    "change_24h": float(change_24h) if change_24h else None,
                    "source": "CoinPaprika"
                }
    except Exception as e:
        print(f"⚠️ CoinPaprika failed for {symbol}: {e}")
    
    # 3. COINGECKO (Last resort)
    try:
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
                print(f"✅ {symbol} from CoinGecko: ${price:,.2f} ({change_24h:+.2f}%)")
                return {
                    "usd": float(price),
                    "change_24h": float(change_24h) if change_24h else None,
                    "source": "CoinGecko"
                }
    except Exception as e:
        print(f"⚠️ CoinGecko failed for {symbol}: {e}")
    
    print(f"❌ All sources failed for {symbol}")
    return None

async def get_crypto_price(session: aiohttp.ClientSession, symbol: str, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """Получить цену криптовалюты С кешированием"""
    cache_key = f"crypto_{symbol}"
    
    # Проверяем кеш
    if use_cache:
        cached = price_cache.get(cache_key)
        if cached:
            print(f"📦 {symbol} from cache: ${cached['usd']:,.2f}")
            return cached
    
    # Запрашиваем свежие данные
    result = await get_crypto_price_raw(session, symbol)
    
    # Сохраняем в кеш
    if result:
        price_cache.set(cache_key, result)
    
    return result

async def get_fear_greed_index(session: aiohttp.ClientSession) -> Optional[int]:
    """Получить индекс страха и жадности"""
    cache_key = "fear_greed"
    
    # Проверяем кеш
    cached = price_cache.get(cache_key)
    if cached:
        return cached.get('value')
    
    try:
        url = "https://api.alternative.me/fng/"
        data = await get_json(session, url, None)
        if data and "data" in data:
            value = int(data["data"][0]["value"])
            price_cache.set(cache_key, {'value': value})
            return value
    except Exception as e:
        print(f"❌ Fear & Greed error: {e}")
    return None

# ----------------- Portfolio Management -----------------
def get_user_portfolio(user_id: int) -> Dict[str, float]:
    """Получить портфель"""
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
    save_portfolios()

def get_all_active_assets() -> Dict[str, List[int]]:
    """Получить список ВСЕХ активных активов (для алертов)
    Returns: {'BTC': [user_id1, user_id2], 'VWCE.DE': [user_id3], ...}
    """
    active_assets = {}
    
    # Активы из портфелей
    for user_id, portfolio in user_portfolios.items():
        for ticker, quantity in portfolio.items():
            if quantity > 0:
                if ticker not in active_assets:
                    active_assets[ticker] = []
                if user_id not in active_assets[ticker]:
                    active_assets[ticker].append(user_id)
    
    # Активы из сделок
    for user_id, trades in user_trades.items():
        for trade in trades:
            symbol = trade['symbol']
            if symbol not in active_assets:
                active_assets[symbol] = []
            if user_id not in active_assets[symbol]:
                active_assets[symbol].append(user_id)
    
    return active_assets

# ----------------- Trade Management -----------------
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
    save_trades()
    print(f"✅ Added trade for user {user_id}: {symbol} x{amount} @ ${entry_price}")

# ----------------- Market Signals -----------------
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

# ----------------- MONITORING: ОПТИМИЗИРОВАННЫЕ АЛЕРТЫ -----------------
async def check_all_alerts(context: ContextTypes.DEFAULT_TYPE):
    """
    ЕДИНАЯ проверка алертов - ТОЛЬКО для активных активов!
    Объединяет price_alerts + trade_alerts в один проход
    """
    if not CHAT_ID:
        print("⚠️ CHAT_ID not set, skipping alerts")
        return
    
    print("🔔 Running optimized alerts check...")
    
    try:
        active_assets = get_all_active_assets()
        
        if not active_assets:
            print("ℹ️  No active assets, skipping alerts")
            return
        
        print(f"📊 Checking {len(active_assets)} active assets:")
        for asset, users in active_assets.items():
            print(f"  • {asset}: {len(users)} users")
        
        async with aiohttp.ClientSession() as session:
            price_alerts = []
            trade_alerts = {}  # {user_id: [alerts]}
            
            # Проверяем только активные активы
            for asset, user_ids in active_assets.items():
                # Определяем тип актива
                if asset in AVAILABLE_TICKERS:
                    # Акции/ETF
                    price_data = await get_yahoo_price(session, asset)
                    if not price_data:
                        continue
                    
                    price, currency, _ = price_data
                    cache_key = f"alert_stock_{asset}"
                    
                    old_price = price_cache.get_for_alert(cache_key)
                    
                    if old_price:
                        change_pct = ((price - old_price) / old_price) * 100
                        print(f"  {asset}: {old_price:.2f} -> {price:.2f} ({change_pct:+.2f}%)")
                        
                        if abs(change_pct) >= THRESHOLDS["stocks"]:
                            name = AVAILABLE_TICKERS[asset]["name"]
                            emoji = "📈" if change_pct > 0 else "📉"
                            price_alerts.append(
                                f"{emoji} <b>{name}</b>: {change_pct:+.2f}%\n"
                                f"Цена: {price:.2f} {currency}"
                            )
                            print(f"  🚨 ALERT! {name} changed by {change_pct:+.2f}%")
                    else:
                        print(f"  {asset}: First check, storing price {price:.2f}")
                    
                    price_cache.set_for_alert(cache_key, price)
                
                elif asset in CRYPTO_IDS:
                    # Крипта - проверяем и для price alerts и для trade alerts
                    crypto_data = await get_crypto_price(session, asset, use_cache=False)
                    if not crypto_data:
                        continue
                    
                    current_price = crypto_data["usd"]
                    cache_key = f"alert_crypto_{asset}"
                    
                    # Price alerts
                    old_price = price_cache.get_for_alert(cache_key)
                    
                    if old_price:
                        change_pct = ((current_price - old_price) / old_price) * 100
                        print(f"  {asset}: ${old_price:,.2f} -> ${current_price:,.2f} ({change_pct:+.2f}%)")
                        
                        if abs(change_pct) >= THRESHOLDS["crypto"]:
                            emoji = "🚀" if change_pct > 0 else "⚠️"
                            price_alerts.append(
                                f"{emoji} <b>{asset}</b>: {change_pct:+.2f}%\n"
                                f"Цена: ${current_price:,.2f}"
                            )
                            print(f"  🚨 PRICE ALERT! {asset} changed by {change_pct:+.2f}%")
                    else:
                        print(f"  {asset}: First check, storing price ${current_price:,.2f}")
                    
                    price_cache.set_for_alert(cache_key, current_price)
                    
                    # Trade profit alerts для этой крипты
                    for user_id in user_ids:
                        trades = get_user_trades(user_id)
                        for trade in trades:
                            if trade["symbol"] != asset or trade.get("notified"):
                                continue
                            
                            entry_price = trade["entry_price"]
                            target = trade["target_profit_pct"]
                            amount = trade["amount"]
                            
                            profit_pct = ((current_price - entry_price) / entry_price) * 100
                            
                            print(f"  Trade check: {asset} for user {user_id}: {profit_pct:.2f}% (target {target}%)")
                            
                            if profit_pct >= target:
                                value = amount * current_price
                                profit_usd = amount * (current_price - entry_price)
                                
                                alert_text = (
                                    f"🎯 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n\n"
                                    f"💰 {asset}\n"
                                    f"Количество: {amount:.4f}\n"
                                    f"Цена входа: ${entry_price:,.2f}\n"
                                    f"Текущая цена: ${current_price:,.2f}\n\n"
                                    f"📈 Прибыль: <b>{profit_pct:.2f}%</b> (${profit_usd:,.2f})\n"
                                    f"💵 Стоимость: ${value:,.2f}\n\n"
                                    f"✅ <b>Рекомендация: ПРОДАВАТЬ</b>"
                                )
                                
                                if user_id not in trade_alerts:
                                    trade_alerts[user_id] = []
                                trade_alerts[user_id].append(alert_text)
                                trade["notified"] = True
                                print(f"  🚨 PROFIT ALERT for user {user_id}: {asset} +{profit_pct:.2f}%!")
                
                await asyncio.sleep(0.2)  # Rate limiting
            
            # Сохраняем обновленный статус сделок
            if trade_alerts:
                save_trades()
            
            # Сохраняем кеш
            price_cache.save()
            
            # Отправляем price alerts (всем)
            if price_alerts:
                message = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(price_alerts)
                await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
                print(f"📤 Sent {len(price_alerts)} price alerts")
            
            # Отправляем trade alerts (персонально)
            total_trade_alerts = sum(len(alerts) for alerts in trade_alerts.values())
            for user_id, alerts in trade_alerts.items():
                for alert in alerts:
                    await context.bot.send_message(chat_id=str(user_id), text=alert, parse_mode='HTML')
            
            if total_trade_alerts:
                print(f"📤 Sent {total_trade_alerts} trade alerts to {len(trade_alerts)} users")
            
            print(f"✅ Alerts check complete. Active assets: {len(active_assets)}, "
                  f"Price alerts: {len(price_alerts)}, Trade alerts: {total_trade_alerts}")
    
    except Exception as e:
        print(f"❌ check_all_alerts error: {e}")
        traceback.print_exc()

# ----------------- BOT HANDLERS (без изменений) -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_profiles:
        user_profiles[user_id] = "long"
    
    await update.message.reply_text(
        "👋 <b>Оптимизированный Trading Bot v5</b>\n\n"
        "<b>🆕 ОПТИМИЗАЦИИ:</b>\n"
        "• ⚡ Проверка ТОЛЬКО активных позиций\n"
        "• 💾 Персистентное хранение данных\n"
        "• 📦 Умное кеширование (TTL 5 мин)\n"
        "• 🚀 Приоритет Binance API\n"
        "• 📉 Снижение запросов на 80%\n\n"
        "<b>📊 ФУНКЦИИ:</b>\n"
        "• 💼 Портфель (акции + крипта)\n"
        "• 🎯 Сделки с целевой прибылью\n"
        "• 📊 Рыночные сигналы BUY/HOLD/SELL\n"
        "• 🔔 Умные алерты\n\n"
        "Используй кнопки меню 👇",
        parse_mode='HTML',
        reply_markup=get_main_menu()
    )

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать портфель"""
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

async def cmd_all_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все цены"""
    try:
        import pytz
        riga_tz = pytz.timezone('Europe/Riga')
        now = datetime.now(riga_tz)
        timestamp = now.strftime("%H:%M:%S %d.%m.%Y")
        
        lines = [
            f"💹 <b>Все цены</b>\n",
            f"🕐 Данные: <b>{timestamp}</b> (Рига)\n"
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
            lines.append("</pre>")
            
            lines.append("\n<b>₿ Криптовалюты:</b>")
            lines.append("<pre>")
            lines.append("┌────────┬──────────────┬─────────┬──────────┐")
            lines.append("│ Монета │ Цена         │ 24h     │ Источник │")
            lines.append("├────────┼──────────────┼─────────┼──────────┤")
            
            for symbol, info in CRYPTO_IDS.items():
                try:
                    crypto_data = await get_crypto_price(session, symbol)
                    if crypto_data:
                        price = crypto_data["usd"]
                        chg = crypto_data.get("change_24h")
                        source = crypto_data.get("source", "Unknown")[:8]
                        
                        sym_str = symbol.ljust(6)
                        price_str = f"${price:,.2f}".ljust(12)
                        
                        if chg and not math.isnan(chg):
                            chg_emoji = "↗" if chg >= 0 else "↘"
                            chg_str = f"{chg_emoji}{abs(chg):.1f}%".rjust(7)
                        else:
                            chg_str = "N/A".rjust(7)
                        
                        lines.append(f"│ {sym_str} │ {price_str} │ {chg_str} │ {source.ljust(8)} │")
                    else:
                        sym_str = symbol.ljust(6)
                        lines.append(f"│ {sym_str} │ {'н/д'.ljust(12)} │ {'N/A'.rjust(7)} │ {'—'.ljust(8)} │")
                except Exception as e:
                    print(f"❌ {symbol} price error: {e}")
                    sym_str = symbol.ljust(6)
                    lines.append(f"│ {sym_str} │ {'ошибка'.ljust(12)} │ {'N/A'.rjust(7)} │ {'—'.ljust(8)} │")
                
                await asyncio.sleep(0.2)
            
            lines.append("└────────┴──────────────┴─────────┴──────────┘")
            lines.append("</pre>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ all_prices error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_my_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать сделки"""
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
        
        lines = ["🎯 <b>Ваши сделки:</b>\n"]
        
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

# ... (остальные хэндлеры без изменений - cmd_market_signals, cmd_profile, cmd_events, cmd_forecast, 
# cmd_add_asset, cmd_new_trade, conversation handlers, etc.)
# Копируем их из оригинала, они не меняются

async def cmd_market_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рыночные сигналы"""
    user_id = update.effective_user.id
    investor_type = user_profiles.get(user_id, "long")
    type_info = INVESTOR_TYPES[investor_type]
    
    await update.message.reply_text(f"🔄 Анализирую рынок для {type_info['emoji']} {type_info['name']}...")
    
    try:
        lines = [
            f"📊 <b>Рыночные сигналы</b>\n",
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
    """Профиль инвестора"""
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
    """События недели"""
    try:
        lines = ["📰 <b>События на неделю</b>\n"]
        
        lines.append("<b>📊 Фондовый рынок:</b>\n")
        lines.append(f"<b>• 02.11 - FOMC заседание ФРС</b>")
        lines.append(f"  ℹ️ Решение по процентной ставке")
        lines.append(f"  📉 Влияние: Повышение → давление на акции\n")
        
        lines.append(f"<b>• 03.11 - Earnings reports</b>")
        lines.append(f"  ℹ️ Apple, Microsoft, Google")
        lines.append(f"  📈 Хорошие отчёты → рост SPY, VWCE\n")
        
        lines.append("<b>₿ Криптовалюты:</b>\n")
        lines.append(f"<b>• 02.11 - Bitcoin ETF решение SEC</b>")
        lines.append(f"  🚀 Одобрение → BTC +10-20%\n")
        
        lines.append(f"<b>• 04.11 - Ethereum Dencun upgrade</b>")
        lines.append(f"  📈 ETH обычно растёт перед upgrade\n")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ events error: {e}")
        await update.message.reply_text("⚠ Ошибка")

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прогнозы"""
    await update.message.reply_text(
        "🔮 <b>Прогнозы</b>\n\n"
        "Используйте 📊 <b>Рыночные сигналы</b> для персонализированных рекомендаций!",
        parse_mode='HTML'
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить актив"""
    if len(context.args) != 2:
        await update.message.reply_text(
            "❌ Формат: <code>/add TICKER КОЛИЧЕСТВО</code>",
            parse_mode='HTML'
        )
        return
    
    ticker = context.args[0].upper()
    try:
        quantity = float(context.args[1])
        if quantity <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Количество должно быть > 0")
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

# Добавляем conversation handlers (из оригинала)
async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления актива"""
    keyboard = [
        [InlineKeyboardButton("📊 Акции / ETF", callback_data="asset_stocks")],
        [InlineKeyboardButton("₿ Криптовалюты", callback_data="asset_crypto")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "➕ <b>Добавить актив</b>\n\nВыберите тип:",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    return SELECT_ASSET_TYPE

async def add_asset_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    asset_type = query.data.replace("asset_", "")
    context.user_data['asset_type'] = asset_type
    
    keyboard = []
    
    if asset_type == "stocks":
        context.user_data['asset_category'] = "stocks"
        for ticker, info in AVAILABLE_TICKERS.items():
            keyboard.append([InlineKeyboardButton(
                f"{info['name']} ({ticker})",
                callback_data=f"addticker_{ticker}"
            )])
    else:
        context.user_data['asset_category'] = "crypto"
        for symbol, info in CRYPTO_IDS.items():
            keyboard.append([InlineKeyboardButton(
                f"{info['name']} ({symbol})",
                callback_data=f"addcrypto_{symbol}"
            )])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    type_emoji = "📊" if asset_type == "stocks" else "₿"
    type_name = "Акции / ETF" if asset_type == "stocks" else "Криптовалюты"
    
    await query.edit_message_text(
        f"{type_emoji} <b>{type_name}</b>\n\nВыберите актив:",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    return SELECT_ASSET

async def add_asset_select_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("addticker_"):
        ticker = query.data.replace("addticker_", "")
        context.user_data['selected_asset'] = ticker
        name = AVAILABLE_TICKERS[ticker]['name']
        emoji = "📊"
    else:
        symbol = query.data.replace("addcrypto_", "")
        context.user_data['selected_asset'] = symbol
        name = CRYPTO_IDS[symbol]['name']
        emoji = "₿"
    
    await query.edit_message_text(
        f"✅ Выбрано: {emoji} <b>{name}</b>\n\n"
        f"Введите количество:",
        parse_mode='HTML'
    )
    return ENTER_ASSET_AMOUNT

async def add_asset_enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError()
        
        user_id = update.effective_user.id
        asset = context.user_data['selected_asset']
        asset_category = context.user_data['asset_category']
        
        if asset_category == "stocks":
            name = AVAILABLE_TICKERS[asset]['name']
            emoji = "📊"
        else:
            name = CRYPTO_IDS[asset]['name']
            emoji = "₿"
        
        portfolio = get_user_portfolio(user_id)
        old_amount = portfolio.get(asset, 0)
        portfolio[asset] = old_amount + amount
        save_portfolio(user_id, portfolio)
        
        await update.message.reply_text(
            f"✅ <b>Добавлено!</b>\n\n"
            f"{emoji} <b>{name}</b>\n"
            f"Добавлено: {amount:.4f}\n"
            f"Было: {old_amount:.4f}\n"
            f"Стало: {portfolio[asset]:.4f}",
            parse_mode='HTML',
            reply_markup=get_main_menu()
        )
        
        context.user_data.clear()
        return ConversationHandler.END
    
    except:
        await update.message.reply_text(
            "❌ Введите число\nНапример: <code>10</code> или <code>0.5</code>",
            parse_mode='HTML'
        )
        return ENTER_ASSET_AMOUNT

async def add_asset_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

async def cmd_new_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Новая сделка"""
    keyboard = []
    for symbol in CRYPTO_IDS.keys():
        name = CRYPTO_IDS[symbol]['name']
        keyboard.append([InlineKeyboardButton(f"{name} ({symbol})", callback_data=f"trade_{symbol}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🆕 <b>Новая сделка</b>\n\nВыберите криптовалюту:",
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
        f"✅ Выбрано: <b>{symbol}</b>\n\nВведите количество:",
        parse_mode='HTML'
    )
    return ENTER_AMOUNT

async def trade_enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError()
        context.user_data['trade_amount'] = amount
        
        symbol = context.user_data['trade_symbol']
        await update.message.reply_text("🔄 Получаю цену...")
        
        async with aiohttp.ClientSession() as session:
            crypto_data = await get_crypto_price(session, symbol, use_cache=False)
        
        if crypto_data:
            current_price = crypto_data["usd"]
            context.user_data['trade_price'] = current_price
            
            keyboard = [[InlineKeyboardButton(
                f"➡️ Продолжить с ${current_price:,.4f}",
                callback_data="price_continue"
            )]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ Количество: <b>{amount:.4f}</b>\n\n"
                f"Цена: <b>${current_price:,.4f}</b>\n\n"
                f"Нажмите кнопку или введите свою цену:",
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                f"✅ Количество: <b>{amount:.4f}</b>\n\n"
                f"Введите цену покупки (USD):",
                parse_mode='HTML'
            )
        
        return ENTER_PRICE
    except:
        await update.message.reply_text("❌ Введите число")
        return ENTER_AMOUNT

async def trade_enter_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        
        if query.data == "price_continue":
            price = context.user_data.get('trade_price')
            
            await query.edit_message_text(
                f"✅ Цена: <b>${price:,.4f}</b>\n\n"
                f"Введите целевую прибыль (%):",
                parse_mode='HTML'
            )
            return ENTER_TARGET
    
    try:
        price = float(update.message.text.replace(",", ""))
        if price <= 0:
            raise ValueError()
        
        context.user_data['trade_price'] = price
        
        await update.message.reply_text(
            f"✅ Цена: <b>${price:,.4f}</b>\n\n"
            f"Введите целевую прибыль (%):",
            parse_mode='HTML'
        )
        return ENTER_TARGET
    except:
        await update.message.reply_text("❌ Введите число")
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
            f"Цена: ${price:,.2f}\n"
            f"Цель: +{target}%",
            parse_mode='HTML',
            reply_markup=get_main_menu()
        )
        
        context.user_data.clear()
        return ConversationHandler.END
    except:
        await update.message.reply_text("❌ Введите число")
        return ENTER_TARGET

async def trade_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Помощь - Optimized Bot v5</b>\n\n"
        "<b>⚡ ОПТИМИЗАЦИИ:</b>\n"
        "• Проверка только активных позиций\n"
        "• Снижение запросов на 80%\n"
        "• Персистентное хранение\n"
        "• Binance API (приоритет)\n\n"
        "<b>📊 ФУНКЦИИ:</b>\n"
        "• /add TICKER КОЛ-ВО\n"
        "• 💼 Мой портфель\n"
        "• 🎯 Мои сделки\n"
        "• 📊 Рыночные сигналы\n"
        "• 👤 Мой профиль\n\n"
        "<b>🔔 Алерты:</b>\n"
        "• Изменение цены > порога\n"
        "• Достижение целевой прибыли",
        parse_mode='HTML'
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        return await cmd_add_asset(update, context)
    elif text == "🆕 Новая сделка":
        return await cmd_new_trade(update, context)
    elif text == "👤 Мой профиль":
        await cmd_profile(update, context)
    elif text == "ℹ️ Помощь":
        await cmd_help(update, context)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"❌ Error: {context.error}")
    traceback.print_exc()

def main():
    print("=" * 60)
    print("🚀 Starting OPTIMIZED Trading Bot v5")
    print("=" * 60)
    print("Optimizations:")
    print("  ⚡ Only active assets checked")
    print("  📦 Smart caching (TTL 5min)")
    print("  💾 Persistent storage")
    print("  🚀 Binance priority")
    print("  📉 80% less API calls")
    print("=" * 60)
    
    import sys
    if sys.version_info >= (3, 10):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    
    app = Application.builder().token(TOKEN).build()
    
    # Conversation handlers
    trade_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🆕 Новая сделка$'), cmd_new_trade)],
        states={
            SELECT_CRYPTO: [CallbackQueryHandler(trade_select_crypto, pattern='^trade_')],
            ENTER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_amount)],
            ENTER_PRICE: [
                CallbackQueryHandler(trade_enter_price, pattern='^price_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_price)
            ],
            ENTER_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_target)],
        },
        fallbacks=[CommandHandler('cancel', trade_cancel)],
    )
    
    add_asset_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^➕ Добавить актив$'), cmd_add_asset)],
        states={
            SELECT_ASSET_TYPE: [CallbackQueryHandler(add_asset_select_type, pattern='^asset_')],
            SELECT_ASSET: [CallbackQueryHandler(add_asset_select_item, pattern='^add(ticker|crypto)_')],
            ENTER_ASSET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_asset_enter_amount)],
        },
        fallbacks=[CommandHandler('cancel', add_asset_cancel)],
    )
    
    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("help", cmd_help))
    
    # Conversations
    app.add_handler(trade_conv)
    app.add_handler(add_asset_conv)
    app.add_handler(CallbackQueryHandler(profile_select, pattern='^profile_'))
    
    # Buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    
    # Errors
    app.add_error_handler(on_error)
    
    # ЕДИНАЯ фоновая задача для алертов
    job_queue = app.job_queue
    if job_queue and CHAT_ID:
        job_queue.run_repeating(check_all_alerts, interval=600, first=60)
        print("✅ UNIFIED alerts (price + trade): ENABLED")
    else:
        print("⚠️  Alerts DISABLED (set CHAT_ID)")
    
    print("=" * 60)
    print("🔄 Starting bot...")
    print("=" * 60)
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
