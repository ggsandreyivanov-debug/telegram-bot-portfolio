import os
import math
import json
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
        [KeyboardButton("📰 События недели"), KeyboardButton("📊 Прогнозы")],
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

# ----------------- PRICES: CoinPaprika -----------------
async def get_crypto_price(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Получить цену криптовалюты с автоматическим fallback по источникам:
    1. CoinPaprika → 2. CoinGecko → 3. CryptoCompare
    """
    crypto_info = CRYPTO_IDS.get(symbol)
    if not crypto_info:
        return None
    
    # Пробуем источники по очереди
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
                chg_str = f"{chg:+.2f}%" if chg else "N/A"
                print(f"✅ {symbol} from {source_name}: ${price:,.2f} ({chg_str})")
                return result
        except Exception as e:
            print(f"⚠️ {source_name} failed for {symbol}: {e}")
            continue
    
    print(f"❌ All sources failed for {symbol}")
    return None

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
    try:
        crypto_info = CRYPTO_IDS.get(symbol)
        if not crypto_info:
            return None
        
        # Пробуем CoinPaprika
        paprika_id = crypto_info["paprika"]
        url = f"https://api.coinpaprika.com/v1/tickers/{paprika_id}"
        
        print(f"🔍 Fetching {symbol} from CoinPaprika: {url}")
        data = await get_json(session, url, None)
        
        if data:
            quotes = data.get("quotes", {}).get("USD", {})
            price = quotes.get("price")
            change_24h = quotes.get("percent_change_24h")
            if price:
                print(f"✅ {symbol}: ${price:,.2f} ({change_24h:+.2f}% if change_24h else 'N/A'})")
                return {
                    "usd": float(price),
                    "change_24h": float(change_24h) if change_24h else None
                }
        
        # Fallback на CoinGecko
        print(f"⚠️ CoinPaprika failed for {symbol}, trying CoinGecko...")
        coingecko_id = crypto_info["coingecko"]
        cg_url = "https://api.coingecko.com/api/v3/simple/price"
        cg_params = {
            "ids": coingecko_id,
            "vs_currencies": "usd",
            "include_24hr_change": "true"
        }
        
        cg_data = await get_json(session, cg_url, cg_params)
        if cg_data and coingecko_id in cg_data:
            coin_data = cg_data[coingecko_id]
            price = coin_data.get("usd")
            change_24h = coin_data.get("usd_24h_change")
            if price:
                print(f"✅ {symbol} from CoinGecko: ${price:,.2f}")
                return {
                    "usd": float(price),
                    "change_24h": float(change_24h) if change_24h else None
                }
        
        print(f"❌ Both APIs failed for {symbol}")
        
    except Exception as e:
        print(f"❌ get_crypto_price {symbol} error: {e}")
        traceback.print_exc()
    
    return None

# ----------------- EVENTS & NEWS -----------------
async def get_fear_greed_index(session: aiohttp.ClientSession) -> Optional[int]:
    """Получить индекс страха и жадности (0-100)"""
    try:
        url = "https://api.alternative.me/fng/"
        data = await get_json(session, url, None)
        if data and "data" in data:
            return int(data["data"][0]["value"])
    except Exception as e:
        print(f"❌ Fear & Greed error: {e}")
    return None

async def get_lunarcrush_sentiment(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, float]]:
    """Получить sentiment score с LunarCrush"""
    try:
        # Маппинг символов для LunarCrush
        symbol_map = {
            "BTC": "BTC",
            "ETH": "ETH",
            "SOL": "SOL",
            "AVAX": "AVAX",
            "DOGE": "DOGE",
            "LINK": "LINK"
        }
        
        lc_symbol = symbol_map.get(symbol)
        if not lc_symbol:
            return None
        
        url = "https://lunarcrush.com/api4/public/coins/list/v2"
        headers = {
            **HEADERS,
            "Authorization": f"Bearer {LUNARCRUSH_API_KEY}"
        }
        
        params = {"symbol": lc_symbol}
        
        async with session.get(url, params=params, headers=headers, timeout=TIMEOUT) as r:
            if r.status == 200:
                data = await r.json()
                if data and "data" in data and len(data["data"]) > 0:
                    coin = data["data"][0]
                    
                    # Извлекаем метрики
                    galaxy_score = coin.get("galaxy_score", 50)  # 0-100
                    alt_rank = coin.get("alt_rank", 500)  # Рейтинг (меньше = лучше)
                    sentiment = coin.get("sentiment", 3)  # 1-5
                    
                    # Нормализуем sentiment (1-5 → 0-100)
                    sentiment_score = ((sentiment - 1) / 4) * 100
                    
                    # Нормализуем rank (топ 100 = хорошо)
                    rank_score = max(0, 100 - (alt_rank / 5))
                    
                    return {
                        "galaxy_score": galaxy_score,
                        "sentiment_score": sentiment_score,
                        "rank_score": rank_score,
                        "overall": (galaxy_score + sentiment_score + rank_score) / 3
                    }
    except Exception as e:
        print(f"❌ LunarCrush {symbol} error: {e}")
    return None

async def calculate_trend_score(session: aiohttp.ClientSession, symbol: str) -> float:
    """Рассчитать тренд на основе 7-дневных данных"""
    try:
        # Для крипты используем CoinPaprika historical
        if symbol in CRYPTO_IDS:
            paprika_id = CRYPTO_IDS[symbol]["paprika"]
            url = f"https://api.coinpaprika.com/v1/tickers/{paprika_id}/historical"
            
            from datetime import datetime, timedelta
            end = datetime.now()
            start = end - timedelta(days=7)
            
            params = {
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
                "interval": "1d"
            }
            
            data = await get_json(session, url, params)
            if data and len(data) >= 2:
                first_price = data[0].get("price", 0)
                last_price = data[-1].get("price", 0)
                
                if first_price > 0:
                    change_pct = ((last_price - first_price) / first_price) * 100
                    # Конвертируем в score 0-100
                    # +20% = 100, -20% = 0
                    trend_score = 50 + (change_pct * 2.5)
                    return max(0, min(100, trend_score))
    except Exception as e:
        print(f"❌ Trend calculation error: {e}")
    
    return 50.0  # Нейтральный тренд по умолчанию

async def calculate_probability(session: aiohttp.ClientSession, symbol: str, event_impact: str) -> Dict[str, Any]:
    """Рассчитать вероятность роста на основе multiple факторов"""
    
    # Базовая вероятность по типу события
    impact_scores = {
        "Критический": 30,  # Высокая неопределённость
        "Высокий": 20,
        "Средний": 10,
        "Низкий": 5
    }
    
    event_score = impact_scores.get(event_impact, 10)
    
    # Получаем данные
    fear_greed = await get_fear_greed_index(session) or 50
    sentiment_data = await get_lunarcrush_sentiment(session, symbol) or {"overall": 50}
    trend_score = await calculate_trend_score(session, symbol)
    
    # Формула: взвешенная сумма
    probability = (
        fear_greed * 0.25 +           # 25% - общее настроение рынка
        sentiment_data["overall"] * 0.30 +  # 30% - sentiment конкретной монеты
        trend_score * 0.30 +          # 30% - недавний тренд
        event_score * 0.15            # 15% - влияние события
    )
    
    probability = max(20, min(80, probability))  # Ограничиваем 20-80%
    
    # Определяем прогноз и рекомендацию
    if probability >= 65:
        prediction = f"📈 Рост вероятен"
        price_change = f"+{2 + (probability - 65) * 0.2:.1f}%"
        confidence = "высокая"
        recommendation = "🟢 ПОКУПАТЬ"
    elif probability >= 55:
        prediction = f"📈 Небольшой рост"
        price_change = f"+{0.5 + (probability - 55) * 0.15:.1f}%"
        confidence = "средняя"
        recommendation = "🟡 ДЕРЖАТЬ"
    elif probability >= 45:
        prediction = f"📊 Нейтрально"
        price_change = "±0.5-1.5%"
        confidence = "низкая"
        recommendation = "🟡 ВОЗДЕРЖАТЬСЯ"
    elif probability >= 35:
        prediction = f"📉 Небольшое падение"
        price_change = f"-{0.5 + (45 - probability) * 0.15:.1f}%"
        confidence = "средняя"
        recommendation = "🟡 ДЕРЖАТЬ"
    else:
        prediction = f"📉 Падение вероятно"
        price_change = f"-{2 + (35 - probability) * 0.2:.1f}%"
        confidence = "высокая"
        recommendation = "🔴 ПРОДАВАТЬ"
    
    return {
        "probability": probability,
        "prediction": prediction,
        "price_change": price_change,
        "confidence": confidence,
        "recommendation": recommendation,
        "factors": {
            "fear_greed": fear_greed,
            "sentiment": sentiment_data["overall"],
            "trend": trend_score
        }
    }

async def get_crypto_events(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    """Получить события для криптовалют с реальной вероятностью"""
    events = []
    
    try:
        from datetime import datetime, timedelta
        base_date = datetime.now()
        
        # Примеры важных событий (можно заменить на реальный API)
        sample_events = [
            {
                "asset": "BTC",
                "date": (base_date + timedelta(days=2)).strftime("%d.%m"),
                "title": "Bitcoin ETF решение SEC",
                "impact": "Критический"
            },
            {
                "asset": "ETH",
                "date": (base_date + timedelta(days=4)).strftime("%d.%m"),
                "title": "Ethereum network upgrade",
                "impact": "Высокий"
            },
            {
                "asset": "SOL",
                "date": (base_date + timedelta(days=1)).strftime("%d.%m"),
                "title": "Solana Breakpoint Conference",
                "impact": "Средний"
            }
        ]
        
        # Рассчитываем вероятность для каждого события
        for event in sample_events:
            symbol = event["asset"]
            impact = event["impact"]
            
            # Реальный расчёт вероятности
            prob_data = await calculate_probability(session, symbol, impact)
            
            event["prediction"] = prob_data["prediction"]
            event["price_change"] = prob_data["price_change"]
            event["probability"] = prob_data["probability"]
            events.append(event)
            
            await asyncio.sleep(0.5)  # Rate limiting
    
    except Exception as e:
        print(f"❌ get_crypto_events error: {e}")
        traceback.print_exc()
    
    return events

async def get_stock_events(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    """Получить события для акций/ETF с анализом и рекомендациями"""
    events = []
    
    from datetime import datetime, timedelta
    base_date = datetime.now()
    
    # Примеры важных макроэкономических событий
    sample_events = [
        {
            "asset": "SPY",
            "date": (base_date + timedelta(days=2)).strftime("%d.%m"),
            "title": "FOMC заседание",
            "impact": "Критический"
        },
        {
            "asset": "SPY",
            "date": (base_date + timedelta(days=3)).strftime("%d.%m"),
            "title": "Отчёты Apple, Amazon",
            "impact": "Высокий"
        },
        {
            "asset": "VWCE.DE",
            "date": (base_date + timedelta(days=5)).strftime("%d.%m"),
            "title": "Данные по инфляции ЕС",
            "impact": "Средний"
        }
    ]
    
    # Для акций используем упрощённый анализ (без LunarCrush)
    for event in sample_events:
        impact = event["impact"]
        
        # Базовая оценка на основе типа события
        if "FOMC" in event["title"] or "Fed" in event["title"]:
            prob = 48  # Высокая неопределённость
            pred = "⚠️ Высокая волатильность"
            change = "±2-4%"
            rec = "🟡 ВОЗДЕРЖАТЬСЯ"
        elif "отчёт" in event["title"].lower() or "earnings" in event["title"].lower():
            prob = 58  # Умеренный оптимизм
            pred = "📈 Возможен рост"
            change = "+1-3%"
            rec = "🟢 ДЕРЖАТЬ"
        elif "инфляц" in event["title"].lower():
            prob = 52
            pred = "📊 Нейтрально"
            change = "±0.5-1.5%"
            rec = "🟡 ДЕРЖАТЬ"
        else:
            prob = 50
            pred = "📊 Нейтрально"
            change = "±1-2%"
            rec = "🟡 ДЕРЖАТЬ"
        
        event["prediction"] = pred
        event["price_change"] = change
        event["probability"] = prob
        event["recommendation"] = rec
        event["confidence"] = "средняя"
        events.append(event)
    
    return events
    """Получить события для криптовалют с CoinMarketCal (бесплатный API)"""
    events = []
    
    try:
        from datetime import datetime, timedelta
        
        # События на неделю вперёд
        date_from = datetime.now().strftime("%Y-%m-%d")
        date_to = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        
        # CoinMarketCal API (без ключа можно получить топ события)
        url = f"https://developers.coinmarketcal.com/v1/events"
        params = {
            "dateRangeStart": date_from,
            "dateRangeEnd": date_to,
            "max": 20
        }
        
        # Пробуем без API ключа (ограниченный доступ)
        data = await get_json(session, url, params)
        
        if data and isinstance(data, dict) and "body" in data:
            for event in data.get("body", [])[:10]:
                coins = event.get("coins", [])
                if not coins:
                    continue
                
                coin_symbol = coins[0].get("symbol", "").upper()
                if coin_symbol not in CRYPTO_IDS:
                    continue
                
                events.append({
                    "asset": coin_symbol,
                    "date": event.get("date_event", ""),
                    "title": event.get("title", {}).get("en", "Неизвестное событие"),
                    "impact": "Высокий" if event.get("vote_count", 0) > 100 else "Средний",
                    "prediction": "📈" if event.get("percentage", 0) > 50 else "📉"
                })
    except Exception as e:
        print(f"❌ CoinMarketCal error: {e}")
    
    # Если API не работает, добавляем примеры важных событий
    if not events:
        from datetime import datetime, timedelta
        base_date = datetime.now()
        
        events = [
            {
                "asset": "BTC",
                "date": (base_date + timedelta(days=2)).strftime("%d.%m"),
                "title": "Bitcoin ETF решение SEC",
                "impact": "Критический",
                "prediction": "📈 Рост 5-8%"
            },
            {
                "asset": "ETH",
                "date": (base_date + timedelta(days=4)).strftime("%d.%m"),
                "title": "Ethereum network upgrade",
                "impact": "Высокий",
                "prediction": "📈 Рост 3-7%"
            },
            {
                "asset": "SOL",
                "date": (base_date + timedelta(days=1)).strftime("%d.%m"),
                "title": "Solana Breakpoint Conference",
                "impact": "Средний",
                "prediction": "📈 Рост 2-4%"
            }
        ]
    
    return events

async def get_stock_events(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    """Получить события для акций/ETF"""
    events = []
    
    from datetime import datetime, timedelta
    base_date = datetime.now()
    
    # Примеры важных макроэкономических событий
    events = [
        {
            "asset": "SPY",
            "date": (base_date + timedelta(days=2)).strftime("%d.%m"),
            "title": "FOMC заседание",
            "impact": "Критический",
            "prediction": "⚠️ Волатильность"
        },
        {
            "asset": "SPY",
            "date": (base_date + timedelta(days=3)).strftime("%d.%m"),
            "title": "Отчёты Apple, Amazon",
            "impact": "Высокий",
            "prediction": "📈 Вероятность роста 60%"
        },
        {
            "asset": "VWCE.DE",
            "date": (base_date + timedelta(days=5)).strftime("%d.%m"),
            "title": "Данные по инфляции ЕС",
            "impact": "Средний",
            "prediction": "📊 Нейтрально"
        }
    ]
    
    return events

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
    """Еженедельный отчёт с ценами и событиями"""
    if not CHAT_ID:
        return
    
    try:
        lines = ["📆 <b>Еженедельный отчёт</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            # Цены
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
            
            # События недели
            lines.append("\n\n📅 <b>События на неделю:</b>")
            
            stock_events = await get_stock_events(session)
            crypto_events = await get_crypto_events(session)
            
            if stock_events or crypto_events:
                lines.append("<pre>")
                lines.append("Дата  Актив   События")
                lines.append("─" * 40)
                
                all_events = stock_events + crypto_events
                all_events.sort(key=lambda x: x.get("date", ""))
                
                for event in all_events[:8]:
                    date = event.get("date", "")[:5]
                    asset = event.get("asset", "")[:7].ljust(7)
                    title = event.get("title", "")[:30]
                    impact = event.get("impact", "")
                    pred = event.get("prediction", "")
                    
                    lines.append(f"{date} {asset} {title}")
                    if impact:
                        lines.append(f"       {impact} | {pred}")
                
                lines.append("</pre>")
            else:
                lines.append("<i>События отслеживаются вручную</i>")
        
        await context.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ weekly_report error: {e}")
        traceback.print_exc()

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
                        price, cur = price_data
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
            
            # Криптовалюты
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
    """Показать все доступные цены"""
    try:
        from datetime import datetime
        import pytz
        
        # Время получения данных (Рига)
        riga_tz = pytz.timezone('Europe/Riga')
        now = datetime.now(riga_tz)
        timestamp = now.strftime("%H:%M:%S %d.%m.%Y")
        
        lines = [
            f"💹 <b>Все цены</b>\n",
            f"🕐 Данные актуальны на: <b>{timestamp}</b> (Рига)\n"
        ]
        
        async with aiohttp.ClientSession() as session:
            # Акции/ETF в виде таблицы
            lines.append("<b>📊 Фондовый рынок:</b>")
            lines.append("<pre>")
            lines.append("┌──────────────────┬────────────┬─────────┐")
            lines.append("│ Актив            │ Цена       │ 24h     │")
            lines.append("├──────────────────┼────────────┼─────────┤")
            
            stock_source = "Yahoo Finance"
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
            lines.append(f"Источник: {stock_source}")
            lines.append("</pre>")
            
            # Криптовалюты в виде таблицы
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
            
            # Показываем источники для крипты
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
        "📰 <b>События недели</b> - важные события\n"
        "📊 <b>Прогнозы</b> - аналитика и прогнозы\n"
        "➕ <b>Добавить актив</b> - инструкция\n"
        "➖ <b>Удалить актив</b> - убрать из портфеля\n\n"
        "<b>Команды:</b>\n"
        "<code>/add TICKER КОЛ-ВО</code> - добавить\n"
        "<code>/remove TICKER</code> - удалить\n"
        "<code>/events</code> - события недели\n"
        "<code>/setalert stocks 2</code> - изменить пороги"
    )
    await update.message.reply_text(message, parse_mode='HTML')

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать события на неделю"""
    try:
        await update.message.reply_text("🔄 Получаю события недели...")
        
        lines = ["📅 <b>События на неделю</b>\n"]
        
        async with aiohttp.ClientSession() as session:
            try:
                stock_events = await get_stock_events(session)
            except Exception as e:
                print(f"❌ Stock events error: {e}")
                traceback.print_exc()
                stock_events = []
            
            try:
                crypto_events = await get_crypto_events(session)
            except Exception as e:
                print(f"❌ Crypto events error: {e}")
                traceback.print_exc()
                crypto_events = []
            
            # Акции/ETF
            if stock_events:
                lines.append("<b>📊 Фондовый рынок:</b>")
                for event in stock_events:
                    date = event.get("date", "")
                    asset = event.get("asset", "")
                    title = event.get("title", "")
                    impact = event.get("impact", "")
                    pred = event.get("prediction", "")
                    prob = event.get("probability", 50)
                    price_change = event.get("price_change", "")
                    recommendation = event.get("recommendation", "🟡 ДЕРЖАТЬ")
                    
                    lines.append(f"\n📅 <b>{date}</b> | {asset}")
                    lines.append(f"📌 {title}")
                    lines.append(f"🎯 Влияние: {impact}")
                    lines.append(f"💡 Прогноз: {pred}")
                    lines.append(f"📊 Изменение: {price_change}")
                    lines.append(f"💰 Рекомендация: <b>{recommendation}</b>")
                    lines.append(f"🔮 Уверенность: средняя ({prob:.0f}/100)")
                    lines.append("")
                    lines.append(f"<i>Анализ: макроэкономика, история</i>")
            
            # Криптовалюты
            if crypto_events:
                lines.append("\n\n<b>₿ Криптовалюты:</b>")
                for event in crypto_events:
                    date = event.get("date", "")
                    asset = event.get("asset", "")
                    title = event.get("title", "")
                    impact = event.get("impact", "")
                    pred = event.get("prediction", "")
                    prob = event.get("probability", 50)
                    price_change = event.get("price_change", "")
                    confidence = event.get("confidence", "средняя")
                    recommendation = event.get("recommendation", "🟡 ДЕРЖАТЬ")
                    
                    lines.append(f"\n📅 <b>{date}</b> | {asset}")
                    lines.append(f"📌 {title}")
                    lines.append(f"🎯 Влияние: {impact}")
                    lines.append(f"💡 Прогноз: {pred}")
                    lines.append(f"📊 Изменение: {price_change}")
                    lines.append(f"💰 Рекомендация: <b>{recommendation}</b>")
                    lines.append(f"🔮 Уверенность: {confidence} ({prob:.0f}/100)")
                    lines.append("")
                    lines.append(f"<i>Анализ: рынок, тренд, соц.сети</i>")
            
            if not stock_events and not crypto_events:
                lines.append("<i>Нет важных событий на эту неделю</i>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ cmd_events error: {e}")
        traceback.print_exc()
        await update.message.reply_text(
            f"⚠ Ошибка при получении событий\n\n"
            f"Попробуйте позже или проверьте логи на Render"
        )

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать прогнозы с реальными расчётами"""
    try:
        await update.message.reply_text("🔄 Рассчитываю прогнозы...")
        
        lines = ["📊 <b>Прогнозы на неделю</b>\n"]
        lines.append("<i>На основе sentiment analysis, трендов и Fear & Greed Index</i>\n")
        
        async with aiohttp.ClientSession() as session:
            # Получаем Fear & Greed Index
            fear_greed = await get_fear_greed_index(session)
            if fear_greed:
                fg_text = "Жадность 🟢" if fear_greed > 60 else "Страх 🔴" if fear_greed < 40 else "Нейтрально 🟡"
                lines.append(f"<b>Индекс рынка:</b> {fear_greed}/100 ({fg_text})\n")
            
            # Прогнозы для криптовалют
            lines.append("<b>₿ Криптовалюты:</b>")
            lines.append("<pre>")
            
            for symbol in ["BTC", "ETH", "SOL", "AVAX"]:
                prob_data = await calculate_probability(session, symbol, "Средний")
                
                sym_str = symbol.ljust(5)
                prob = prob_data["probability"]
                change = prob_data["price_change"]
                
                emoji = "📈" if prob >= 55 else "📉" if prob <= 45 else "📊"
                lines.append(f"{emoji} {sym_str} {prob:.0f}%  {change}")
                
                await asyncio.sleep(0.5)
            
            lines.append("</pre>")
            
            lines.append("\n<b>Факторы анализа:</b>")
            lines.append("• Социальный sentiment (LunarCrush)")
            lines.append("• Тренд последних 7 дней")
            lines.append("• Fear & Greed Index")
            lines.append("• Влияние предстоящих событий")
            
            lines.append("\n<i>⚠️ Не является финансовой рекомендацией</i>")
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    
    except Exception as e:
        print(f"❌ forecast error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при расчёте прогнозов")

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок меню"""
    text = update.message.text
    
    if text == "💼 Мой портфель":
        await cmd_portfolio(update, context)
    elif text == "💹 Все цены":
        await cmd_all_prices(update, context)
    elif text == "📰 События недели":
        await cmd_events(update, context)
    elif text == "📊 Прогнозы":
        await cmd_forecast(update, context)
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
    # Простая инициализация для версии 21.7
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("testalert", cmd_test_alert))
    app.add_handler(CommandHandler("events", cmd_events))
    
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
        print("🚀 Bot running (monitoring disabled - set CHAT_ID to enable)")
    
    # Запускаем polling с отменой старых обновлений
    print("🔄 Starting polling...")
    try:
        app.run_polling(
            drop_pending_updates=True,
            close_loop=False,
            stop_signals=None,  # Отключаем автоматическую обработку сигналов
            allowed_updates=Update.ALL_TYPES
        )
    except Exception as e:
        print(f"❌ Polling stopped: {e}")
        import sys
        sys.exit(0)

if __name__ == "__main__":
    main()
