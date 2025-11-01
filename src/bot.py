import os
import math
import asyncio
import traceback
import json
import sys
import tempfile
import shutil
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

import pandas as pd
import numpy as np

# теханализ
try:
    from ta.momentum import RSIIndicator
    from ta.trend import MACD, SMAIndicator
    TA_AVAILABLE = True
except ImportError:
    TA_AVAILABLE = False

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================================================
# ===============  ENV & GLOBAL CONFIG  ===================
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # общий канал для алертов резких движений
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")  # для событий недели

if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")

if not CHAT_ID:
    print("⚠ CHAT_ID не установлен - суммарные алерты в общий чат будут пропущены")

if FINNHUB_API_KEY:
    print("✅ FINNHUB_API_KEY: Set")
else:
    print("⚠ FINNHUB_API_KEY not set - /events будет ограничен")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# тикеры фондового рынка / ETF / индекс
AVAILABLE_TICKERS = {
    "VWCE.DE": {"name": "VWCE", "type": "stock"},
    "4GLD.DE": {"name": "4GLD (Gold ETC)", "type": "stock"},
    "DE000A2T5DZ1.SG": {"name": "X IE Physical Gold ETC", "type": "stock"},
    "SPY": {"name": "S&P 500 (SPY)", "type": "stock"},
}

# крипта
CRYPTO_IDS = {
    "BTC": {
        "binance": "BTCUSDT",
        "coingecko": "bitcoin",
        "paprika": "btc-bitcoin",
        "name": "Bitcoin",
    },
    "ETH": {
        "binance": "ETHUSDT",
        "coingecko": "ethereum",
        "paprika": "eth-ethereum",
        "name": "Ethereum",
    },
    "SOL": {
        "binance": "SOLUSDT",
        "coingecko": "solana",
        "paprika": "sol-solana",
        "name": "Solana",
    },
    "AVAX": {
        "binance": "AVAXUSDT",
        "coingecko": "avalanche-2",
        "paprika": "avax-avalanche",
        "name": "Avalanche",
    },
    "DOGE": {
        "binance": "DOGEUSDT",
        "coingecko": "dogecoin",
        "paprika": "doge-dogecoin",
        "name": "Dogecoin",
    },
    "LINK": {
        "binance": "LINKUSDT",
        "coingecko": "chainlink",
        "paprika": "link-chainlink",
        "name": "Chainlink",
    },
}

# алерты
THRESHOLDS = {
    "stocks": 1.0,   # %
    "crypto": 4.0,   # %
}

# профили инвестора
INVESTOR_TYPES = {
    "long": {
        "name": "Долгосрочный инвестор",
        "emoji": "🏔️",
        "desc": "Покупаю на страхе, держу годами",
    },
    "swing": {
        "name": "Свинг-трейдер",
        "emoji": "🌊",
        "desc": "Ловлю волны, держу дни-недели",
    },
    "day": {
        "name": "Дневной трейдер",
        "emoji": "⚡",
        "desc": "Быстрые сделки внутри дня",
    },
}

# параметры сигналов: пороги для страха, RSI, т.д.
SIGNAL_THRESHOLDS = {
    "long": {
        "extreme_fear": 25,
        "take_profit": 80,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "volume_min": 1.2,  # не критично
    },
    "swing": {
        "buy_dip": 40,
        "sell_pump": 65,
        "rsi_oversold": 35,
        "rsi_overbought": 65,
        "volume_min": 1.4,
    },
    "day": {
        "scalp_buy": 45,
        "scalp_sell": 58,
        "rsi_oversold": 40,
        "rsi_overbought": 60,
        "volume_min": 1.5,
    },
}

# =========================================================
# =================  IN-MEMORY STATE  =====================
# =========================================================

user_portfolios: Dict[int, Dict[str, float]] = {}
user_trades: Dict[int, List[Dict[str, Any]]] = {}
user_profiles: Dict[int, str] = {}

SELECT_CRYPTO, ENTER_AMOUNT, ENTER_PRICE, ENTER_TARGET = range(4)
SELECT_ASSET_TYPE, SELECT_ASSET, ENTER_ASSET_AMOUNT = range(4, 7)

# фоновые таски (для Supabase пушей) -> ждём при shutdown
active_tasks: set[asyncio.Task] = set()

# =========================================================
# =================  DATA DIR & FILES  ====================
# =========================================================

def get_data_directory() -> Path:
    possible_dirs = [
        Path("/home/claude/bot_data"),
        Path("/opt/render/project/src/bot_data"),
        Path("./bot_data"),
        Path(tempfile.gettempdir()) / "bot_data",
    ]
    for d in possible_dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test_file = d / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
            print(f"✅ Using data directory: {d}")
            return d
        except (OSError, PermissionError) as e:
            print(f"⚠️ Cannot use {d}: {e}")
            continue
    raise RuntimeError("❌ No writable data directory")

DATA_DIR = get_data_directory()
CACHE_FILE = DATA_DIR / "price_cache.json"
PORTFOLIO_FILE = DATA_DIR / "portfolios.json"
TRADES_FILE = DATA_DIR / "trades.json"

# =========================================================
# ==================  SUPABASE STORAGE  ===================
# =========================================================

class SupabaseStorage:
    def __init__(self, url: Optional[str], key: Optional[str]):
        self.url = url
        self.key = key
        self.session: Optional[aiohttp.ClientSession] = None
        self.enabled = bool(url and key)
        if self.enabled:
            self.headers = {
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            print("✅ Supabase storage enabled")
        else:
            self.headers = {}
            print("⚠️ Supabase storage disabled")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def load_portfolios(self) -> Dict[int, Dict[str, float]]:
        if not self.enabled:
            return {}
        try:
            s = await self._get_session()
            url = f"{self.url}/rest/v1/portfolios?select=*"
            async with s.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"⚠️ load_portfolios HTTP {resp.status} {body[:200]}")
                    return {}
                data = await resp.json()
                result: Dict[int, Dict[str, float]] = {}
                for row in data:
                    try:
                        uid = int(row["user_id"])
                        assets = row["assets"]
                        if isinstance(assets, dict):
                            result[uid] = assets
                    except Exception as e:
                        print(f"⚠️ bad portfolio row: {e}")
                print(f"✅ Loaded {len(result)} portfolios from Supabase")
                return result
        except Exception as e:
            print(f"⚠️ load_portfolios err: {e}")
            return {}

    async def save_portfolio(self, user_id: int, assets: Dict[str, float]):
        if not self.enabled:
            return
        try:
            s = await self._get_session()
            url = f"{self.url}/rest/v1/portfolios"
            data = {
                "user_id": user_id,
                "assets": assets,
                "updated_at": datetime.utcnow().isoformat(),
            }
            headers = {**self.headers, "Prefer": "resolution=merge-duplicates"}
            async with s.post(url, headers=headers, json=data,
                              timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status not in (200, 201, 204):
                    body = await resp.text()
                    print(f"⚠️ save_portfolio HTTP {resp.status} {body[:200]}")
        except Exception as e:
            print(f"⚠️ save_portfolio err: {e}")

    async def load_trades(self) -> Dict[int, List[Dict[str, Any]]]:
        if not self.enabled:
            return {}
        try:
            s = await self._get_session()
            url = f"{self.url}/rest/v1/trades?select=*&order=created_at.desc"
            async with s.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"⚠️ load_trades HTTP {resp.status} {body[:200]}")
                    return {}
                rows = await resp.json()
                out: Dict[int, List[Dict[str, Any]]] = {}
                for row in rows:
                    try:
                        uid = int(row["user_id"])
                        out.setdefault(uid, []).append({
                            "id": row["id"],
                            "symbol": row["symbol"],
                            "amount": float(row["amount"]),
                            "entry_price": float(row["entry_price"]),
                            "target_profit_pct": float(row["target_profit_pct"]),
                            "notified": bool(row.get("notified", False)),
                            "timestamp": row.get("created_at", datetime.utcnow().isoformat()),
                        })
                    except Exception as e:
                        print(f"⚠️ bad trade row: {e}")
                print(f"✅ Loaded {sum(len(v) for v in out.values())} trades from Supabase")
                return out
        except Exception as e:
            print(f"⚠️ load_trades err: {e}")
            return {}

    async def add_trade(
        self,
        user_id: int,
        symbol: str,
        amount: float,
        entry_price: float,
        target_profit_pct: float,
    ) -> bool:
        if not self.enabled:
            return False
        try:
            s = await self._get_session()
            url = f"{self.url}/rest/v1/trades"
            data = {
                "user_id": user_id,
                "symbol": symbol,
                "amount": amount,
                "entry_price": entry_price,
                "target_profit_pct": target_profit_pct,
            }
            async with s.post(url, headers=self.headers, json=data,
                              timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status in (200, 201, 204):
                    return True
                body = await resp.text()
                print(f"⚠️ add_trade HTTP {resp.status} {body[:200]}")
                return False
        except Exception as e:
            print(f"⚠️ add_trade err: {e}")
            return False

    async def update_trade_notified(self, trade_id: int):
        if not self.enabled:
            return
        try:
            s = await self._get_session()
            url = f"{self.url}/rest/v1/trades?id=eq.{trade_id}"
            data = {"notified": True}
            async with s.patch(url, headers=self.headers, json=data,
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    print(f"⚠️ update_trade_notified HTTP {resp.status} {body[:200]}")
        except Exception as e:
            print(f"⚠️ update_trade_notified err: {e}")

supabase_storage = SupabaseStorage(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# ======================  PRICE CACHE  ====================
# =========================================================

class PriceCache:
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.cache: Dict[str, Dict] = {}
        self.stats = {"api_calls": 0, "cache_hits": 0}
        self.load()

    def load(self):
        if not CACHE_FILE.exists():
            return
        try:
            raw = CACHE_FILE.read_text()
            data = json.loads(raw)
            if not isinstance(data, dict):
                print("⚠️ Invalid cache file structure")
                return
            now_ts = datetime.now().timestamp()
            valid = 0
            for k, v in data.items():
                if not isinstance(v, dict):
                    continue
                ts = v.get("timestamp")
                if ts is None:
                    continue
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                # не тащим совсем древнее
                if now_ts - ts < self.ttl * 2:
                    self.cache[k] = v
                    valid += 1
            print(f"✅ Loaded {valid} cached entries")
        except Exception as e:
            print(f"⚠️ cache load err: {e}")

    def save(self):
        tmp = CACHE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.cache, indent=2))
            shutil.move(str(tmp), str(CACHE_FILE))
        except Exception as e:
            print(f"⚠️ cache save err: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except:
                pass

    def _safe_price_ok(self, x: Any) -> bool:
        if not isinstance(x, (int, float)):
            return False
        if math.isinf(x) or math.isnan(x) or x <= 0:
            return False
        return True

    def get(self, key: str) -> Optional[Dict]:
        entry = self.cache.get(key)
        if not entry:
            return None
        try:
            age = datetime.now().timestamp() - float(entry["timestamp"])
        except Exception:
            self.cache.pop(key, None)
            return None
        if age < self.ttl:
            self.stats["cache_hits"] += 1
            return entry.get("data")
        return None

    def set(self, key: str, data: Dict):
        self.cache[key] = {
            "data": data,
            "timestamp": datetime.now().timestamp(),
        }
        self.stats["api_calls"] += 1
        if len(self.cache) % 10 == 0:
            self.save()

    def get_for_alert(self, key: str) -> Optional[float]:
        entry = self.cache.get(key)
        if not entry:
            return None
        price_val = entry.get("data", {}).get("price")
        if not self._safe_price_ok(price_val):
            return None
        return float(price_val)

    def set_for_alert(self, key: str, price: float):
        if not self._safe_price_ok(price):
            print(f"⚠️ invalid alert price for {key}: {price}")
            return
        if key not in self.cache:
            self.cache[key] = {"data": {}, "timestamp": datetime.now().timestamp()}
        self.cache[key]["data"]["price"] = float(price)
        self.save()

    def get_stats(self) -> str:
        total = self.stats["api_calls"] + self.stats["cache_hits"]
        if total == 0:
            return "No requests yet"
        hit_rate = (self.stats["cache_hits"] / total) * 100
        return f"API calls: {self.stats['api_calls']}, Cache hits: {self.stats['cache_hits']} ({hit_rate:.1f}%)"

    def reset_stats(self):
        self.stats = {"api_calls": 0, "cache_hits": 0}

price_cache = PriceCache(ttl_seconds=300)

# =========================================================
# ============ LOAD / SAVE USER DATA (LOCAL+REMOTE) =======
# =========================================================

def _fallback_local_load():
    global user_portfolios, user_trades
    # portfolios
    if not user_portfolios and PORTFOLIO_FILE.exists():
        try:
            raw = PORTFOLIO_FILE.read_text()
            data = json.loads(raw)
            tmp: Dict[int, Dict[str, float]] = {}
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        uid = int(k)
                        if isinstance(v, dict):
                            tmp[uid] = v
                    except Exception:
                        pass
            user_portfolios = tmp
            print(f"✅ Loaded {len(user_portfolios)} portfolios from local file")
        except Exception as e:
            print(f"⚠️ local portfolio load err: {e}")

    # trades
    if not user_trades and TRADES_FILE.exists():
        try:
            raw = TRADES_FILE.read_text()
            data = json.loads(raw)
            tmp2: Dict[int, List[Dict[str, Any]]] = {}
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        uid = int(k)
                        if isinstance(v, list):
                            tmp2[uid] = v
                    except Exception:
                        pass
            user_trades = tmp2
            print(f"✅ Loaded {len(user_trades)} trade lists from local file")
        except Exception as e:
            print(f"⚠️ local trades load err: {e}")

async def load_data_on_start():
    global user_portfolios, user_trades
    try:
        sp_pf = await supabase_storage.load_portfolios()
        if sp_pf:
            user_portfolios = sp_pf
    except Exception as e:
        print(f"⚠️ init portfolios err: {e}")

    try:
        sp_tr = await supabase_storage.load_trades()
        if sp_tr:
            user_trades = sp_tr
    except Exception as e:
        print(f"⚠️ init trades err: {e}")

    _fallback_local_load()

def save_portfolios_local():
    try:
        tmp = PORTFOLIO_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(user_portfolios, indent=2))
        shutil.move(str(tmp), str(PORTFOLIO_FILE))
    except Exception as e:
        print(f"⚠️ portfolio save err: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except:
            pass

def save_trades_local():
    try:
        tmp = TRADES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(user_trades, indent=2))
        shutil.move(str(tmp), str(TRADES_FILE))
    except Exception as e:
        print(f"⚠️ trades save err: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except:
            pass

def _track_bg_task(coro: asyncio.Future):
    """ helper: оборачиваем create_task так, чтобы таски попадали в active_tasks и снимались по завершению """
    task = asyncio.create_task(coro)
    active_tasks.add(task)
    def _done(_):
        active_tasks.discard(task)
    task.add_done_callback(_done)

def save_portfolio_hybrid(user_id: int, portfolio: Dict[str, float]):
    # в память
    user_portfolios[user_id] = portfolio
    # на диск
    save_portfolios_local()
    # supabase async
    async def _push():
        try:
            await supabase_storage.save_portfolio(user_id, portfolio)
        except Exception as e:
            print(f"⚠️ Background save_portfolio error: {e}")
    _track_bg_task(_push())

def add_trade_hybrid(
    user_id: int,
    symbol: str,
    amount: float,
    entry_price: float,
    target_profit_pct: float,
):
    trades = user_trades.setdefault(user_id, [])
    trade = {
        "symbol": symbol,
        "amount": amount,
        "entry_price": entry_price,
        "target_profit_pct": target_profit_pct,
        "timestamp": datetime.utcnow().isoformat(),
        "notified": False,
    }
    trades.append(trade)
    save_trades_local()

    async def _push():
        try:
            await supabase_storage.add_trade(
                user_id, symbol, amount, entry_price, target_profit_pct
            )
        except Exception as e:
            print(f"⚠️ Background add_trade error: {e}")
    _track_bg_task(_push())

# =========================================================
# ================== PRICE FETCH HELPERS ==================
# =========================================================

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

def _safe_float(x: Any) -> Optional[float]:
    try:
        val = float(x)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except Exception:
        return None

async def get_yahoo_price(session: aiohttp.ClientSession, ticker: str) -> Optional[Tuple[float, str, float]]:
    """returns (price, currency, change_pct_24h)"""
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": "1d"}
        data = await get_json(session, url, params)
        if not data:
            return None

        result = data.get("chart", {}).get("result", [{}])[0]
        meta = result.get("meta", {})

        price = _safe_float(meta.get("regularMarketPrice"))
        change_pct = _safe_float(meta.get("regularMarketChangePercent"))
        cur = meta.get("currency", "USD")

        if price is None:
            return None
        if change_pct is None:
            change_pct = 0.0

        return (price, cur, change_pct)

    except Exception as e:
        print(f"❌ Yahoo {ticker} error: {e}")
        return None

async def get_crypto_price_raw(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    info = CRYPTO_IDS.get(symbol)
    if not info:
        return None

    # 1) Binance
    try:
        binance_symbol = info["binance"]
        url = "https://api.binance.com/api/v3/ticker/24hr"
        params = {"symbol": binance_symbol}
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = _safe_float(data.get("lastPrice"))
                chg = _safe_float(data.get("priceChangePercent"))
                if price is not None and price > 0:
                    return {
                        "usd": price,
                        "change_24h": chg if chg is not None else None,
                        "source": "Binance",
                    }
    except Exception as e:
        print(f"⚠️ Binance failed {symbol}: {e}")

    # 2) CoinPaprika
    try:
        paprika_id = info["paprika"]
        url = f"https://api.coinpaprika.com/v1/tickers/{paprika_id}"
        data = await get_json(session, url, None)
        if data:
            quotes = data.get("quotes", {}).get("USD", {})
            price = _safe_float(quotes.get("price"))
            chg = _safe_float(quotes.get("percent_change_24h"))
            if price is not None and price > 0:
                return {
                    "usd": price,
                    "change_24h": chg,
                    "source": "CoinPaprika",
                }
    except Exception as e:
        print(f"⚠️ CoinPaprika failed {symbol}: {e}")

    # 3) CoinGecko
    try:
        cg_id = info["coingecko"]
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": cg_id,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        }
        data = await get_json(session, url, params)
        if data and cg_id in data:
            coin = data[cg_id]
            price = _safe_float(coin.get("usd"))
            chg = _safe_float(coin.get("usd_24h_change"))
            if price is not None and price > 0:
                return {
                    "usd": price,
                    "change_24h": chg,
                    "source": "CoinGecko",
                }
    except Exception as e:
        print(f"⚠️ CoinGecko failed {symbol}: {e}")

    print(f"❌ All sources failed for {symbol}")
    return None

async def get_crypto_price(session: aiohttp.ClientSession, symbol: str, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    cache_key = f"crypto_{symbol}"
    if use_cache:
        cached = price_cache.get(cache_key)
        if cached:
            return cached
    raw = await get_crypto_price_raw(session, symbol)
    if raw:
        price_cache.set(cache_key, raw)
    return raw

async def get_fear_greed_index(session: aiohttp.ClientSession) -> Optional[int]:
    cache_key = "fear_greed"
    cached = price_cache.get(cache_key)
    if cached:
        return cached.get("value")
    try:
        url = "https://api.alternative.me/fng/"
        data = await get_json(session, url, None)
        if data and "data" in data:
            value = int(data["data"][0]["value"])
            price_cache.set(cache_key, {"value": value})
            return value
    except Exception as e:
        print(f"❌ Fear & Greed error: {e}")
    return None

# =========================================================
# =========== HISTORICAL PRICE & TECHNICAL ANALYSIS =======
# =========================================================

async def get_price_history(session: aiohttp.ClientSession, symbol: str, days: int = 200) -> Optional[pd.DataFrame]:
    """
    Для крипты: берём дневные свечи с Binance.
    Возвращаем DataFrame с колонками: ts, close, volume
    """
    info = CRYPTO_IDS.get(symbol)
    if not info:
        return None
    pair = info["binance"]

    # binance klines: /api/v3/klines?symbol=BTCUSDT&interval=1d&limit=200
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": pair, "interval": "1d", "limit": min(days, 200)}
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                print(f"⚠️ klines {symbol} HTTP {resp.status}")
                return None
            raw = await resp.json()
    except Exception as e:
        print(f"⚠️ klines {symbol} err: {e}")
        return None

    # raw is list of lists:
    # [ openTime, open, high, low, close, volume, closeTime, ...]
    try:
        rows = []
        for entry in raw:
            ts = datetime.utcfromtimestamp(entry[0] / 1000.0)
            close_p = _safe_float(entry[4])
            vol = _safe_float(entry[5])
            if close_p is None or vol is None:
                continue
            rows.append((ts, close_p, vol))
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts", "close", "volume"])
        df.set_index("ts", inplace=True)
        return df
    except Exception as e:
        print(f"⚠️ klines parse {symbol} err: {e}")
        return None

def _norm(v: float, lo: float, hi: float, invert: bool = False) -> float:
    """линейная нормализация в 0..100 с зажимом, invert для случаев типа RSI overbought"""
    if hi == lo:
        return 50.0
    x = (v - lo) / (hi - lo)
    x = max(0.0, min(1.0, x))
    if invert:
        x = 1.0 - x
    return x * 100.0

async def calculate_technical_indicators(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает dict:
    {
      "rsi": float,
      "rsi_state": "oversold"/"neutral"/"overbought",
      "macd_bullish": bool,
      "sma_short_above_long": bool,
      "volume_spike": float | None,
      "trend": "uptrend"/"downtrend"/"neutral"
    }
    Если TA_AVAILABLE=False или данных не хватает -> None
    """
    if not TA_AVAILABLE:
        print("TA not available (ta lib not imported)")
        return None

    df = await get_price_history(session, symbol, days=200)
    if df is None or len(df) < 30:
        return None

    # RSI(14)
    rsi_ind = RSIIndicator(close=df["close"], window=14)
    df["rsi"] = rsi_ind.rsi()

    # MACD(12,26,9)
    macd_ind = MACD(close=df["close"], window_fast=12, window_slow=26, window_sign=9)
    df["macd"] = macd_ind.macd()
    df["macd_signal"] = macd_ind.macd_signal()

    # SMA 50 / SMA 200 (если есть столько данных)
    sma50 = SMAIndicator(close=df["close"], window=50)
    df["sma50"] = sma50.sma_indicator()
    if len(df) >= 200:
        sma200 = SMAIndicator(close=df["close"], window=200)
        df["sma200"] = sma200.sma_indicator()
    else:
        df["sma200"] = np.nan

    # volume spike
    df["vol_ma20"] = df["volume"].rolling(window=20).mean()
    df["vol_spike"] = df["volume"] / df["vol_ma20"]

    latest = df.iloc[-1]

    rsi_val = latest.get("rsi", np.nan)
    macd_now = latest.get("macd", np.nan)
    macd_sig = latest.get("macd_signal", np.nan)
    sma50_now = latest.get("sma50", np.nan)
    sma200_now = latest.get("sma200", np.nan)
    vol_spike = latest.get("vol_spike", np.nan)

    def _is_num(x):
        return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

    # RSI state
    if _is_num(rsi_val):
        if rsi_val <= 30:
            rsi_state = "oversold"
        elif rsi_val >= 70:
            rsi_state = "overbought"
        else:
            rsi_state = "neutral"
    else:
        rsi_state = "neutral"

    # MACD bullish?
    macd_bullish = False
    if _is_num(macd_now) and _is_num(macd_sig):
        macd_bullish = macd_now > macd_sig

    # Trend via SMA
    sma_short_above_long = False
    trend = "neutral"
    if _is_num(sma50_now) and _is_num(sma200_now):
        if sma50_now > sma200_now:
            sma_short_above_long = True
            trend = "uptrend"
        elif sma50_now < sma200_now:
            trend = "downtrend"
        else:
            trend = "neutral"

    # Volume spike
    volume_spike_val = None
    if _is_num(vol_spike):
        volume_spike_val = float(vol_spike)

    return {
        "rsi": float(rsi_val) if _is_num(rsi_val) else None,
        "rsi_state": rsi_state,
        "macd_bullish": macd_bullish,
        "sma_short_above_long": sma_short_above_long,
        "trend": trend,
        "volume_spike": volume_spike_val,
    }

# =========================================================
# ================== USER HELPERS =========================
# =========================================================

def get_user_portfolio(user_id: int) -> Dict[str, float]:
    if user_id not in user_portfolios:
        user_portfolios[user_id] = {
            "VWCE.DE": 0,
            "DE000A2T5DZ1.SG": 0,
            "BTC": 0,
            "ETH": 0,
            "SOL": 0,
        }
    return user_portfolios[user_id]

def get_user_trades(uid: int) -> List[Dict[str, Any]]:
    if uid not in user_trades:
        user_trades[uid] = []
    return user_trades[uid]

def get_all_active_assets() -> Dict[str, List[int]]:
    """Собирает активы, которые у кого-то реально есть (для алертов)"""
    active_assets: Dict[str, List[int]] = {}
    # портфели
    for uid, pf in user_portfolios.items():
        for ticker, qty in pf.items():
            try:
                if float(qty) > 0:
                    active_assets.setdefault(ticker, [])
                    if uid not in active_assets[ticker]:
                        active_assets[ticker].append(uid)
            except Exception:
                continue
    # сделки
    for uid, trades in user_trades.items():
        for t in trades:
            sym = t.get("symbol")
            if not sym:
                continue
            active_assets.setdefault(sym, [])
            if uid not in active_assets[sym]:
                active_assets[sym].append(uid)
    return active_assets

# =========================================================
# ================== MARKET SIGNAL LOGIC ==================
# =========================================================

def _confidence_stars(score: float) -> str:
    # score 0..100 ⇒ 1-5 звёзд
    if score >= 80:
        stars = 5
    elif score >= 65:
        stars = 4
    elif score >= 50:
        stars = 3
    elif score >= 35:
        stars = 2
    else:
        stars = 1
    return "⭐" * stars

def _score_to_signal(score: float):
    # возвращает (label, emoji)
    if score >= 70:
        return "STRONG BUY", "🟢🟢"
    if score >= 55:
        return "BUY", "🟢"
    if score >= 45:
        return "HOLD", "🟡"
    if score >= 30:
        return "SELL", "🔴"
    return "STRONG SELL", "🔴🔴"

async def build_signal_for_symbol(session: aiohttp.ClientSession, symbol: str, investor_type: str) -> Dict[str, Any]:
    """
    Возвращает:
    {
      "symbol": "BTC",
      "score": 72,
      "signal": "BUY",
      "emoji": "🟢",
      "reason_lines": [...],
      "ta": {...},  # из calculate_technical_indicators
    }
    """
    th = SIGNAL_THRESHOLDS.get(investor_type, SIGNAL_THRESHOLDS["long"])
    reason_lines: List[str] = []

    # Fear & Greed
    fg_val = await get_fear_greed_index(session)
    if fg_val is None:
        fg_val = 50
    # интерпретация
    if fg_val < 25:
        fg_note = "😱 Экстремальный страх"
    elif fg_val < 45:
        fg_note = "😰 Страх"
    elif fg_val < 55:
        fg_note = "😐 Нейтрально"
    elif fg_val < 75:
        fg_note = "😃 Жадность"
    else:
        fg_note = "🤑 Экстремальная жадность"
    reason_lines.append(f"Fear & Greed: {fg_val}/100 ({fg_note})")

    # TA
    ta_data = await calculate_technical_indicators(session, symbol)
    if ta_data is None:
        ta_data = {
            "rsi": None,
            "rsi_state": "neutral",
            "macd_bullish": False,
            "sma_short_above_long": False,
            "trend": "neutral",
            "volume_spike": None,
        }
        reason_lines.append("TA: недоступно (мало данных или нет ta lib)")
    else:
        # формируем человекочитаемый блок
        ta_lines = []
        if ta_data["rsi"] is not None:
            ta_lines.append(f"RSI: {ta_data['rsi']:.1f} ({ta_data['rsi_state']})")
        else:
            ta_lines.append("RSI: n/a")

        ta_lines.append("MACD: bullish ✅" if ta_data["macd_bullish"] else "MACD: flat/ bear ⚠️")
        ta_lines.append(f"Тренд: {ta_data['trend']}")
        if ta_data["volume_spike"] and ta_data["volume_spike"] > 1.0:
            ta_lines.append(f"Объём x{ta_data['volume_spike']:.1f}")
        reason_lines += ta_lines

    # score calc (weights)
    # 30% F&G, 25% RSI, 20% MACD, 15% Volume, 10% Trend
    score_parts = []

    # F&G score: для long чем ниже FG тем лучше (fear = good entry)
    # для day чем ближе к 50 тем лучше (волатильность => scalp)
    if investor_type == "long":
        fg_score = _norm(fg_val, 20, 80, invert=True)  # низкий F&G -> высокий балл
    elif investor_type == "swing":
        # swing хочет ловить коррекцию (ниже ~40) и фиксить >65, так что оптимально средне-низкий
        # прибьем к 30..70
        if fg_val <= th["buy_dip"]:
            fg_score = 80.0
        elif fg_val >= th["sell_pump"]:
            fg_score = 20.0
        else:
            fg_score = 60.0
    else:
        # day: он больше боится перекупа, но готов играть на импульсе
        if fg_val <= th["scalp_buy"]:
            fg_score = 75.0
        elif fg_val >= th["scalp_sell"]:
            fg_score = 25.0
        else:
            fg_score = 55.0
    score_parts.append(("fg", fg_score, 30))

    # RSI score: low RSI => buy ; high RSI => sell
    rsi_val = ta_data["rsi"]
    if rsi_val is None:
        rsi_score = 50.0
    else:
        # если rsi низкий -> хорошо для покупки
        low_thr = th.get("rsi_oversold", 30)
        hi_thr = th.get("rsi_overbought", 70)
        if rsi_val <= low_thr:
            rsi_score = 85.0
        elif rsi_val >= hi_thr:
            rsi_score = 20.0
        else:
            # середина
            rsi_score = 55.0
    score_parts.append(("rsi", rsi_score, 25))

    # MACD bullish -> позитив
    macd_score = 80.0 if ta_data["macd_bullish"] else 40.0
    score_parts.append(("macd", macd_score, 20))

    # Volume spike: для day трейдера спайк важен, для long не особо
    vol_spike = ta_data["volume_spike"]
    vol_thr = th.get("volume_min", 1.2)
    if vol_spike and vol_spike >= vol_thr:
        vol_score = 70.0 if investor_type == "day" else 60.0
    else:
        vol_score = 50.0
    score_parts.append(("vol", vol_score, 15))

    # Trend (SMA50 > SMA200 = uptrend)
    trend_score = 70.0 if ta_data["sma_short_above_long"] else 45.0
    score_parts.append(("trend", trend_score, 10))

    # финальный скор
    total_score = 0.0
    for _, val, weight in score_parts:
        total_score += val * (weight / 100.0)

    label, emoji = _score_to_signal(total_score)

    return {
        "symbol": symbol,
        "score": total_score,
        "signal": label,
        "emoji": emoji,
        "reason_lines": reason_lines,
        "ta": ta_data,
        "fg": fg_val,
    }

# =========================================================
# ======================== ALERTS =========================
# =========================================================

async def check_all_alerts(context: ContextTypes.DEFAULT_TYPE):
    """
    Джоба каждые N минут:
    1. резкие движения цены (в общий чат CHAT_ID)
    2. достижение цели сделки (в личку юзеру)
    """
    if not context.application:
        return
    bot = context.application.bot

    print("🔔 Running alerts check...")

    try:
        active_assets = get_all_active_assets()
    except Exception as e:
        print(f"⚠️ active_assets err: {e}")
        return

    if not active_assets:
        print("ℹ️  No active assets, skip alerts")
        return

    print(f"📊 {len(active_assets)} assets to check")

    price_alerts: List[str] = []
    trade_alerts: Dict[int, List[str]] = {}

    async with aiohttp.ClientSession() as session:
        for asset, user_ids in active_assets.items():
            # акции/ETF
            if asset in AVAILABLE_TICKERS:
                pdata = await get_yahoo_price(session, asset)
                if pdata:
                    price, currency, _chg = pdata
                    cache_key = f"alert_stock_{asset}"
                    old_price = price_cache.get_for_alert(cache_key)

                    if old_price and old_price > 0:
                        try:
                            change_pct = ((price - old_price) / old_price) * 100
                        except ZeroDivisionError:
                            change_pct = 0.0

                        print(f"  {asset}: {old_price:.2f}->{price:.2f} ({change_pct:+.2f}%)")

                        if abs(change_pct) >= THRESHOLDS["stocks"]:
                            name = AVAILABLE_TICKERS[asset]["name"]
                            emoji = "📈" if change_pct > 0 else "📉"
                            price_alerts.append(
                                f"{emoji} <b>{name}</b>: {change_pct:+.2f}%\n"
                                f"Цена: {price:.2f} {currency}"
                            )
                    else:
                        print(f"  {asset}: first seen {price:.2f}")

                    price_cache.set_for_alert(cache_key, price)

            # крипта
            elif asset in CRYPTO_IDS:
                cdata = await get_crypto_price(session, asset, use_cache=False)
                if not cdata:
                    await asyncio.sleep(0.2)
                    continue
                current_price = cdata["usd"]
                cache_key = f"alert_crypto_{asset}"
                old_price = price_cache.get_for_alert(cache_key)

                if old_price and old_price > 0:
                    try:
                        change_pct = ((current_price - old_price) / old_price) * 100
                    except ZeroDivisionError:
                        change_pct = 0.0

                    print(f"  {asset}: {old_price:.2f}->{current_price:.2f} ({change_pct:+.2f}%)")

                    if abs(change_pct) >= THRESHOLDS["crypto"]:
                        emoji = "🚀" if change_pct > 0 else "⚠️"
                        price_alerts.append(
                            f"{emoji} <b>{asset}</b>: {change_pct:+.2f}%\n"
                            f"Цена: ${current_price:,.2f}"
                        )
                else:
                    print(f"  {asset}: first crypto price {current_price:.2f}")

                price_cache.set_for_alert(cache_key, current_price)

                # сделки юзеров (триггер цели)
                for uid in user_ids:
                    trades = get_user_trades(uid)
                    for tr in trades:
                        if tr.get("symbol") != asset:
                            continue
                        if tr.get("notified"):
                            continue
                        try:
                            entry_price = float(tr["entry_price"])
                            target = float(tr["target_profit_pct"])
                            amount = float(tr["amount"])
                        except Exception:
                            continue
                        if entry_price <= 0:
                            continue
                        try:
                            profit_pct = ((current_price - entry_price) / entry_price) * 100
                        except ZeroDivisionError:
                            continue

                        if profit_pct >= target:
                            value_now = amount * current_price
                            profit_usd = amount * (current_price - entry_price)

                            alert_text = (
                                "🎯 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n\n"
                                f"₿ {asset}\n"
                                f"Кол-во: {amount:.4f}\n"
                                f"Вход: ${entry_price:,.2f}\n"
                                f"Сейчас: ${current_price:,.2f}\n\n"
                                f"📈 Прибыль: <b>{profit_pct:.2f}%</b> "
                                f"(${profit_usd:,.2f})\n"
                                f"💵 Стоимость позиции: ${value_now:,.2f}\n\n"
                                "💡 Рекомендация: 🟢 ПРОДАВАТЬ СЕЙЧАС"
                            )
                            trade_alerts.setdefault(uid, []).append(alert_text)
                            tr["notified"] = True
                            print(f"  🚨 PROFIT ALERT uid={uid} {asset} +{profit_pct:.2f}%")

            await asyncio.sleep(0.15)

    # update local trades after target triggers
    if trade_alerts:
        save_trades_local()

    price_cache.save()

    # резкие движения -> общий канал
    if price_alerts and CHAT_ID:
        msg = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(price_alerts)
        try:
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
            print(f"📤 Sent {len(price_alerts)} price alerts to {CHAT_ID}")
        except Exception as e:
            print(f"⚠️ Failed to send price alerts: {e}")

    # таргеты -> личка
    sent_trade_alerts = 0
    for uid, alerts in trade_alerts.items():
        for text in alerts:
            try:
                await bot.send_message(chat_id=str(uid), text=text, parse_mode="HTML")
                sent_trade_alerts += 1
            except Exception as e:
                print(f"⚠️ Failed to DM trade alert to {uid}: {e}")
    if sent_trade_alerts:
        print(f"📤 Sent {sent_trade_alerts} trade alerts to {len(trade_alerts)} users")

    cache_stats = price_cache.get_stats()
    print(f"📊 Cache stats: {cache_stats}")
    price_cache.reset_stats()
    print("✅ Alerts check done\n")

# =========================================================
# ================== FINNHUB CALENDAR =====================
# =========================================================

async def get_economic_calendar(session: aiohttp.ClientSession, days: int = 7) -> List[Dict[str, Any]]:
    """
    Важные макро события (ФРС, NFP, CPI)
    Finnhub endpoint: /calendar/economic
    """
    if not FINNHUB_API_KEY:
        return []

    today = datetime.utcnow().date()
    until = today + timedelta(days=days)

    url = "https://finnhub.io/api/v1/calendar/economic"
    params = {
        "from": today.isoformat(),
        "to": until.isoformat(),
        "token": FINNHUB_API_KEY
    }
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                print(f"⚠️ economic cal HTTP {resp.status}")
                return []
            data = await resp.json()
    except Exception as e:
        print(f"⚠️ econ cal err: {e}")
        return []

    out = []
    events = data.get("economicCalendar", []) or data.get("economicCalendar", [])
    # finnhub иногда возвращает "economicCalendar", иногда "data", поэтому:
    if not events and "data" in data:
        events = data["data"]

    high_keywords = ["FOMC", "Nonfarm", "Payrolls", "Fed", "CPI", "GDP", "Unemployment"]
    for ev in events:
        # пример структуры может отличаться, но берём date/time/impact/actual/estimate/event
        date_str = ev.get("date")
        title = ev.get("event") or ev.get("country") or "?"
        impact = ev.get("impact") or ev.get("importance") or ""
        country = ev.get("country") or ev.get("region") or ""
        # фильтр: только high impact или ключевые слова
        txt = f"{title} {impact}"
        if any(k.lower() in txt.lower() for k in high_keywords) or "High" in impact:
            out.append({
                "date": date_str,
                "title": title,
                "impact": impact,
                "country": country,
            })
    return out

TOP_EARNINGS = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META"]

async def get_earnings_calendar(session: aiohttp.ClientSession, days: int = 7) -> List[Dict[str, Any]]:
    """
    Отчётность компаний (крупные тикеры + то, что может быть в портфеле)
    Finnhub endpoint: /calendar/earnings
    """
    if not FINNHUB_API_KEY:
        return []
    today = datetime.utcnow().date()
    until = today + timedelta(days=days)

    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {
        "from": today.isoformat(),
        "to": until.isoformat(),
        "token": FINNHUB_API_KEY
    }
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                print(f"⚠️ earnings cal HTTP {resp.status}")
                return []
            data = await resp.json()
    except Exception as e:
        print(f"⚠️ earnings cal err: {e}")
        return []

    events = data.get("earningsCalendar", []) or data.get("earningsCalendar", [])
    out = []
    for ev in events:
        sym = ev.get("symbol")
        if not sym:
            continue
        # фильтр по интересу: большие бренды/тех или топ из списка
        if sym.upper() in TOP_EARNINGS or sym.upper() in AVAILABLE_TICKERS:
            out.append({
                "date": ev.get("date"),
                "symbol": sym.upper(),
                "eps_estimate": ev.get("epsEstimate"),
                "revenue_estimate": ev.get("revenueEstimate"),
            })
    return out

def format_events_block(
    econ_events: List[Dict[str, Any]],
    earn_events: List[Dict[str, Any]],
    pf: Dict[str, float],
    fg_val: Optional[int],
    now_riga_str: str,
) -> str:
    lines: List[str] = []

    lines.append("📰 <b>СОБЫТИЯ НЕДЕЛИ</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏰ {now_riga_str}")
    lines.append("")

    # Макро
    lines.append("📊 <b>ФОНДОВЫЙ РЫНОК / МАКРО</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if econ_events:
        for ev in econ_events[:6]:
            date_s = ev.get("date", "?")
            title = ev.get("title", "?")
            country = ev.get("country", "")
            impact = ev.get("impact", "")
            lines.append(
                f"📅 {date_s} | {country}\n"
                f"   {title}\n"
                f"   Влияние: {impact or '—'}"
            )
    else:
        lines.append("Нет крупных событий (или нет FINNHUB_API_KEY)")
    lines.append("")

    # Earnings
    lines.append("🏢 <b>ОТЧЁТНОСТЬ КОМПАНИЙ</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if earn_events:
        for ev in earn_events[:6]:
            sym = ev.get("symbol")
            owned_mark = "✅ У вас в портфеле" if sym in pf else "—"
            lines.append(
                f"📅 {ev.get('date','?')} | {sym}\n"
                f"   EPS est: {ev.get('eps_estimate','?')}, Rev est: {ev.get('revenue_estimate','?')}\n"
                f"   {owned_mark}"
            )
    else:
        lines.append("Нет значимых отчётов / ключевые тикеры не попали в окно")
    lines.append("")

    # Крипта
    lines.append("₿ <b>КРИПТОВАЛЮТЫ</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if fg_val is not None:
        if fg_val < 25:
            mood = "😱 Экстремальный страх → часто хорошая точка входа для долгосрока"
        elif fg_val < 45:
            mood = "😰 Страх → рынок нервничает, можно подбирать понемногу"
        elif fg_val < 75:
            mood = "😃 Жадность → рынок оптимистичен"
        else:
            mood = "🤑 Экстремальная жадность → потенциал перегрева"
        lines.append(
            f"Fear & Greed Index: {fg_val}/100\n"
            f"{mood}"
        )
    else:
        lines.append("Нет данных по настроению рынка (fear & greed)")

    # персональный контекст
    lines.append("")
    lines.append("🧠 <b>ВАЖНО ДЛЯ ВАС</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if pf:
        for ticker, qty in pf.items():
            if qty and qty > 0:
                lines.append(f"• {ticker}: активен у вас")
    else:
        lines.append("У вас пустой портфель")
    lines.append("")
    return "\n".join(lines)

# =========================================================
# ======================= UI HELPERS ======================
# =========================================================

def _bar(percent: float, length: int = 10, filled_char="🟩", empty_char="⬜") -> str:
    # percent e.g. 74.5 -> fill round(percent/100*len)
    if percent < 0:
        percent = 0
    if percent > 100:
        percent = 100
    filled = round((percent / 100.0) * length)
    return filled_char * filled + empty_char * (length - filled)

def _bar_blue(percent: float, length: int = 10) -> str:
    filled_char = "🟦"
    empty_char = "⬜"
    if percent < 0:
        percent = 0
    if percent > 100:
        percent = 100
    filled = round((percent / 100.0) * length)
    return filled_char * filled + empty_char * (length - filled)

# =========================================================
# ======================== HANDLERS =======================
# =========================================================

def get_main_menu():
    keyboard = [
        [KeyboardButton("💼 Мой портфель"), KeyboardButton("💹 Все цены")],
        [KeyboardButton("🎯 Мои сделки"), KeyboardButton("📊 Рыночные сигналы")],
        [KeyboardButton("📰 События недели"), KeyboardButton("➕ Добавить актив")],
        [KeyboardButton("🆕 Новая сделка"), KeyboardButton("👤 Мой профиль")],
        [KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_profiles:
        user_profiles[uid] = "long"

    await update.message.reply_text(
        "👋 <b>Trading Bot v6</b>\n\n"
        "<b>Функции:</b>\n"
        "• 💼 Портфель (акции + крипта)\n"
        "• 🎯 Сделки с целевой прибылью\n"
        "• 📊 Рыночные сигналы с теханализом (RSI, MACD, тренд)\n"
        "• 📰 События недели (макро, отчёты, крипто-сентимент)\n"
        "• 🔔 Умные алерты (движения цены / цель достигнута)\n\n"
        "Используй кнопки меню 👇",
        parse_mode="HTML",
        reply_markup=get_main_menu(),
    )

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    portfolio = get_user_portfolio(uid)
    if not portfolio or all(v == 0 for v in portfolio.values()):
        await update.message.reply_text(
            "💼 Ваш портфель пуст!\n\nИспользуйте <b>➕ Добавить актив</b>",
            parse_mode="HTML",
        )
        return

    try:
        async with aiohttp.ClientSession() as session:
            total_value_usd = 0.0
            stock_lines = []
            crypto_lines = []
            stock_total = 0.0
            crypto_total = 0.0

            # акции/ETF
            for ticker, qty in portfolio.items():
                if ticker not in AVAILABLE_TICKERS or qty <= 0:
                    continue
                pdata = await get_yahoo_price(session, ticker)
                if not pdata:
                    continue
                price, cur, chg = pdata
                value = price * qty

                # для total_value_usd делаем грубую конверсию
                if cur == "USD":
                    total_value_usd += value
                    stock_total += value
                elif cur == "EUR":
                    total_value_usd += value * 1.1
                    stock_total += value * 1.1
                else:
                    total_value_usd += value
                    stock_total += value

                arrow = "📈" if chg is not None and chg >= 0 else "📉"
                stock_lines.append(
                    f"{AVAILABLE_TICKERS[ticker]['name']}  {qty:.2f} шт\n"
                    f"├ {price:.2f} {cur} {arrow} {chg:+.1f}%\n"
                    f"└ Стоимость: {value:,.2f} {cur}"
                )
                await asyncio.sleep(0.15)

            # крипта
            for symbol, qty in portfolio.items():
                if symbol not in CRYPTO_IDS or qty <= 0:
                    continue
                cdata = await get_crypto_price(session, symbol)
                if not cdata:
                    continue
                price = cdata["usd"]
                chg = cdata.get("change_24h")
                value = price * qty
                total_value_usd += value
                crypto_total += value
                arrow = ""
                if chg is not None:
                    arrow = "📈" if chg >= 0 else "📉"
                crypto_lines.append(
                    f"{symbol}  {qty:.4f}\n"
                    f"├ ${price:,.2f} {arrow} {f'{chg:+.1f}%' if chg is not None else ''}\n"
                    f"└ Стоимость: ${value:,.2f}"
                )
                await asyncio.sleep(0.15)

        lines: List[str] = []
        lines.append("💼 <b>ВАШ ПОРТФЕЛЬ</b>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if stock_lines:
            lines.append("\n📊 <b>АКЦИИ / ETF</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.extend(stock_lines)
        if crypto_lines:
            lines.append("\n₿ <b>КРИПТОВАЛЮТЫ</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.extend(crypto_lines)

        if total_value_usd > 0:
            stock_pct = (stock_total / total_value_usd) * 100 if total_value_usd else 0
            crypto_pct = (crypto_total / total_value_usd) * 100 if total_value_usd else 0
            lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("💰 <b>ИТОГО</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"Общая стоимость: ${total_value_usd:,.2f}")
            lines.append("")
            lines.append("📊 Распределение:")
            lines.append(f"  Акции:  {stock_pct:.1f}% {_bar(stock_pct)}")
            lines.append(f"  Крипта: {crypto_pct:.1f}% {_bar_blue(crypto_pct)}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        print(f"❌ portfolio error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_all_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        riga_tz = timezone(timedelta(hours=2))
        now = datetime.now(riga_tz)
        timestamp = now.strftime("%H:%M:%S %d.%m.%Y")

        lines = []
        lines.append("💹 <b>ВСЕ ЦЕНЫ</b>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🕐 Данные: <b>{timestamp}</b> (Рига)")
        lines.append("")

        async with aiohttp.ClientSession() as session:
            # STOCKS
            lines.append("📊 <b>Фондовый рынок:</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("<pre>")
            lines.append("┌──────────────────┬────────────┬─────────┐")
            lines.append("│ Актив            │ Цена       │ 24h     │")
            lines.append("├──────────────────┼────────────┼─────────┤")
            for ticker, info in AVAILABLE_TICKERS.items():
                pdata = await get_yahoo_price(session, ticker)
                if pdata:
                    price, cur, chg = pdata
                    name = info["name"][:16].ljust(16)
                    price_str = f"{price:.2f} {cur}".ljust(10)
                    if chg != 0:
                        arrow = "↗" if chg >= 0 else "↘"
                        chg_str = f"{arrow}{abs(chg):.1f}%".rjust(7)
                    else:
                        chg_str = "0.0%".rjust(7)
                else:
                    name = info["name"][:16].ljust(16)
                    price_str = "н/д".ljust(10)
                    chg_str = "N/A".rjust(7)

                lines.append(f"│ {name} │ {price_str} │ {chg_str} │")
                await asyncio.sleep(0.15)
            lines.append("└──────────────────┴────────────┴─────────┘")
            lines.append("</pre>\n")

            # CRYPTO
            lines.append("₿ <b>Криптовалюты:</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("<pre>")
            lines.append("┌────────┬──────────────┬─────────┬──────────┐")
            lines.append("│ Монета │ Цена         │ 24h     │ Источник │")
            lines.append("├────────┼──────────────┼─────────┼──────────┤")

            for symbol, info in CRYPTO_IDS.items():
                cdata = await get_crypto_price(session, symbol)
                if cdata:
                    price = cdata["usd"]
                    chg = cdata.get("change_24h")
                    source = cdata.get("source", "—")[:8]
                    sym_str = symbol.ljust(6)
                    price_str = f"${price:,.2f}".ljust(12)
                    if chg is not None and not math.isnan(chg):
                        arrow = "↗" if chg >= 0 else "↘"
                        chg_str = f"{arrow}{abs(chg):.1f}%".rjust(7)
                    else:
                        chg_str = "N/A".rjust(7)
                else:
                    sym_str = symbol.ljust(6)
                    price_str = "н/д".ljust(12)
                    chg_str = "N/A".rjust(7)
                    source = "—".ljust(8)

                lines.append(
                    f"│ {sym_str} │ {price_str} │ {chg_str} │ {source.ljust(8)} │"
                )
                await asyncio.sleep(0.15)

            lines.append("└────────┴──────────────┴─────────┴──────────┘")
            lines.append("</pre>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        print(f"❌ all_prices error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_my_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    trades = get_user_trades(uid)
    if not trades:
        await update.message.reply_text(
            "🎯 У вас нет открытых сделок\n\nИспользуйте <b>🆕 Новая сделка</b>",
            parse_mode="HTML",
        )
        return

    try:
        await update.message.reply_text("🔄 Обновляю данные...")

        lines = []
        total_value = 0.0
        total_profit = 0.0

        async with aiohttp.ClientSession() as session:
            for i, tr in enumerate(trades, start=1):
                try:
                    symbol = tr["symbol"]
                    entry_price = float(tr["entry_price"])
                    amount = float(tr["amount"])
                    target = float(tr["target_profit_pct"])
                    created_ts = tr.get("timestamp")
                except Exception:
                    continue

                cdata = await get_crypto_price(session, symbol)
                if not cdata:
                    continue
                current_price = cdata["usd"]
                try:
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                except ZeroDivisionError:
                    profit_pct = 0.0

                profit_usd = amount * (current_price - entry_price)
                value_now = amount * current_price

                total_value += value_now
                total_profit += profit_usd

                # статус
                if profit_pct >= target:
                    status = "🎉 ЦЕЛЬ ДОСТИГНУТА!"
                    rec = "🟢 ПРОДАВАТЬ СЕЙЧАС"
                elif profit_pct > 0:
                    status = "📈 ПРИБЫЛЬ"
                    rec = "Держать / подтяни стоп"
                else:
                    status = "📉 УБЫТОК"
                    rec = "Подумай: усреднять или выйти"

                # прогресс к цели
                goal_progress = min(max(profit_pct / target, 0.0), 1.0) if target > 0 else 0.0
                goal_bar_blocks = round(goal_progress * 10)
                goal_bar = "🟩" * goal_bar_blocks + "⬜" * (10 - goal_bar_blocks)

                # сколько дней в сделке
                days_in_trade = "n/a"
                if created_ts:
                    try:
                        dt_open = datetime.fromisoformat(created_ts.replace("Z", "+00:00"))
                        days_in_trade = (datetime.utcnow() - dt_open).days
                        days_in_trade = f"{days_in_trade} дн."
                    except Exception:
                        pass

                # UI-блок сделки
                lines.append(f"✅ <b>#{i} · {symbol}</b>")
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"Статус: {status}")
                lines.append("")
                lines.append("💰 Позиция:")
                lines.append(f"  ├ Количество: {amount:.4f} {symbol}")
                lines.append(f"  ├ Вход: ${entry_price:,.2f}")
                lines.append(f"  ├ Сейчас: ${current_price:,.2f}")
                lines.append(f"  └ Стоимость: ${value_now:,.2f}")
                lines.append("")
                lines.append("📊 Результат:")
                lines.append(f"  ├ Прибыль: {profit_pct:+.2f}% (${profit_usd:+,.2f})")
                lines.append(f"  ├ Цель: +{target:.2f}%")
                lines.append(f"  └ Прогресс: {goal_bar} {(goal_progress*100):.0f}%")
                lines.append("")
                lines.append("💡 Рекомендация:")
                lines.append(f"  {rec}")
                lines.append("")
                lines.append(f"⏰ В сделке: {days_in_trade}")
                lines.append("")

                await asyncio.sleep(0.15)

        if total_value > 0:
            initial_value = total_value - total_profit
            if initial_value > 0:
                total_profit_pct = (total_profit / initial_value) * 100
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append("💼 <b>Сводка по всем сделкам</b>")
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"Общая стоимость: ${total_value:,.2f}")
                lines.append(
                    f"Общая прибыль: {total_profit_pct:+.2f}% (${total_profit:+,.2f})"
                )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        print(f"❌ my_trades error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_market_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    inv_type = user_profiles.get(uid, "long")
    inv_info = INVESTOR_TYPES[inv_type]

    await update.message.reply_text(
        f"🔄 Анализирую рынок для {inv_info['emoji']} {inv_info['name']}..."
    )

    try:
        async with aiohttp.ClientSession() as session:
            fg_val = await get_fear_greed_index(session)
            # Заголовок
            header_lines = []
            header_lines.append("📊 <b>РЫНОЧНЫЕ СИГНАЛЫ</b>")
            header_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            header_lines.append(f"👤 Ваш профиль: {inv_info['emoji']} <b>{inv_info['name']}</b>")
            header_lines.append(f"<i>{inv_info['desc']}</i>")
            header_lines.append("")

            if fg_val is not None:
                if fg_val < 25:
                    fg_status = "😱 Экстремальный страх"
                elif fg_val < 45:
                    fg_status = "😰 Страх"
                elif fg_val < 55:
                    fg_status = "😐 Нейтрально"
                elif fg_val < 75:
                    fg_status = "😃 Жадность"
                else:
                    fg_status = "🤑 Экстремальная жадность"
                header_lines.append(f"📈 Fear & Greed: <b>{fg_val}/100</b> ({fg_status})")
                header_lines.append("")
            else:
                header_lines.append("📈 Fear & Greed: n/a")
                header_lines.append("")

            # Сигналы по топам
            body_lines = []
            for symbol in ["BTC", "ETH", "SOL", "AVAX"]:
                sig = await build_signal_for_symbol(session, symbol, inv_type)
                label = sig["signal"]
                emoji = sig["emoji"]
                score = sig["score"]
                conf = _confidence_stars(score)

                body_lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                body_lines.append(f"₿ <b>{symbol}</b>")
                body_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                body_lines.append(f"🎯 Сигнал: {emoji} <b>{label}</b> (Score: {score:.0f}/100)")
                body_lines.append(f"🎲 Уверенность: {conf} ({len(conf)}/5)")
                body_lines.append("")
                body_lines.append("📊 Технический анализ:")
                for rl in sig["reason_lines"]:
                    body_lines.append(f"  ├ {rl}")
                body_lines.append("")
                body_lines.append("💡 Интерпретация:")
                if label in ("STRONG BUY", "BUY"):
                    body_lines.append("   Рынок даёт точку входа.")
                    if inv_type == "long":
                        body_lines.append("   Для долгосрока: усреднить вниз/докупить.")
                    elif inv_type == "swing":
                        body_lines.append("   Для свинга: можно открывать позицию на импульс.")
                    else:
                        body_lines.append("   Для внутридня: играть от лонга, но стоп обязателен.")
                elif label in ("SELL", "STRONG SELL"):
                    body_lines.append("   Рынок перегрет / риск коррекции.")
                    if inv_type == "long":
                        body_lines.append("   Долгосрок обычно не паникует. Но можно частично зафиксировать.")
                    elif inv_type == "swing":
                        body_lines.append("   Для свинга: фиксировать профит, ждать отката.")
                    else:
                        body_lines.append("   Для внутридня: шорт/фиксация, не жадничай.")
                else:
                    body_lines.append("   Нейтрально. Просто держать и не дёргаться.")
                body_lines.append("")

                await asyncio.sleep(0.2)

        footer_lines = []
        footer_lines.append("<i>⚠️ Это не финансовая рекомендация</i>")

        final_msg = "\n".join(header_lines + body_lines + footer_lines)
        await update.message.reply_text(final_msg, parse_mode="HTML")

    except Exception as e:
        print(f"❌ market_signals error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении сигналов")

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    current_type = user_profiles.get(uid, "long")

    keyboard = []
    for t_key, t_info in INVESTOR_TYPES.items():
        selected = "✅ " if t_key == current_type else ""
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{selected}{t_info['emoji']} {t_info['name']}",
                    callback_data=f"profile_{t_key}",
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    cur_info = INVESTOR_TYPES[current_type]

    await update.message.reply_text(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"Текущий: {cur_info['emoji']} <b>{cur_info['name']}</b>\n"
        f"<i>{cur_info['desc']}</i>\n\n"
        f"Выберите стиль, чтобы сигналы были персональными:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )

async def profile_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    inv_type = query.data.replace("profile_", "")
    uid = query.from_user.id
    user_profiles[uid] = inv_type
    t_info = INVESTOR_TYPES[inv_type]

    await query.edit_message_text(
        f"✅ <b>Профиль обновлён!</b>\n\n"
        f"{t_info['emoji']} <b>{t_info['name']}</b>\n"
        f"<i>{t_info['desc']}</i>\n\n"
        "Теперь сигналы и рекомендации адаптированы под твой стиль.",
        parse_mode="HTML",
    )

# --- Добавление актива в портфель (быстрая команда /add)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text(
            "❌ Формат: <code>/add TICKER КОЛИЧЕСТВО</code>",
            parse_mode="HTML",
        )
        return
    ticker = context.args[0].upper()
    try:
        qty = float(context.args[1])
        if qty <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("❌ Количество должно быть > 0")
        return

    if ticker not in AVAILABLE_TICKERS and ticker not in CRYPTO_IDS:
        await update.message.reply_text(
            "❌ Неизвестный тикер: {0}\n\n"
            "Доступные: VWCE.DE, 4GLD.DE, DE000A2T5DZ1.SG, SPY, BTC, ETH, SOL, AVAX, DOGE, LINK".format(
                ticker
            )
        )
        return

    uid = update.effective_user.id
    pf = get_user_portfolio(uid)
    old = pf.get(ticker, 0)
    pf[ticker] = old + qty
    save_portfolio_hybrid(uid, pf)

    name = (
        AVAILABLE_TICKERS.get(ticker, {}).get("name")
        or CRYPTO_IDS.get(ticker, {}).get("name")
        or ticker
    )
    await update.message.reply_text(
        f"✅ Добавлено: <b>{qty} {name}</b>\n"
        f"Теперь у вас: {pf[ticker]:.4f}",
        parse_mode="HTML",
    )

# --- Диалог 'Добавить актив' через кнопки ---

async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📊 Акции / ETF", callback_data="asset_stocks")],
        [InlineKeyboardButton("₿ Криптовалюты", callback_data="asset_crypto")],
    ]
    await update.message.reply_text(
        "➕ <b>Добавить актив</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SELECT_ASSET_TYPE

async def add_asset_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    asset_type = q.data.replace("asset_", "")
    context.user_data["asset_type"] = asset_type

    kb = []
    if asset_type == "stocks":
        context.user_data["asset_category"] = "stocks"
        for ticker, info in AVAILABLE_TICKERS.items():
            kb.append([
                InlineKeyboardButton(
                    f"{info['name']} ({ticker})",
                    callback_data=f"addticker_{ticker}"
                )
            ])
        type_emoji = "📊"; type_name = "Акции / ETF"
    else:
        context.user_data["asset_category"] = "crypto"
        for symbol, info in CRYPTO_IDS.items():
            kb.append([
                InlineKeyboardButton(
                    f"{info['name']} ({symbol})",
                    callback_data=f"addcrypto_{symbol}"
                )
            ])
        type_emoji = "₿"; type_name = "Криптовалюты"

    await q.edit_message_text(
        f"{type_emoji} <b>{type_name}</b>\n\nВыберите актив:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SELECT_ASSET

async def add_asset_select_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data.startswith("addticker_"):
        ticker = q.data.replace("addticker_", "")
        context.user_data["selected_asset"] = ticker
        name = AVAILABLE_TICKERS[ticker]["name"]
        emoji = "📊"
    else:
        symbol = q.data.replace("addcrypto_", "")
        context.user_data["selected_asset"] = symbol
        name = CRYPTO_IDS[symbol]["name"]
        emoji = "₿"

    await q.edit_message_text(
        f"✅ Выбрано: {emoji} <b>{name}</b>\n\nВведите количество:",
        parse_mode="HTML",
    )
    return ENTER_ASSET_AMOUNT

async def add_asset_enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError()
        uid = update.effective_user.id
        asset = context.user_data["selected_asset"]
        category = context.user_data["asset_category"]

        if category == "stocks":
            name = AVAILABLE_TICKERS[asset]["name"]
            emoji = "📊"
        else:
            name = CRYPTO_IDS[asset]["name"]
            emoji = "₿"

        pf = get_user_portfolio(uid)
        old_amount = pf.get(asset, 0)
        pf[asset] = old_amount + amount
        save_portfolio_hybrid(uid, pf)

        await update.message.reply_text(
            f"✅ <b>Добавлено!</b>\n\n"
            f"{emoji} <b>{name}</b>\n"
            f"Добавлено: {amount:.4f}\n"
            f"Было: {old_amount:.4f}\n"
            f"Стало: {pf[asset]:.4f}",
            parse_mode="HTML",
            reply_markup=get_main_menu(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    except Exception:
        await update.message.reply_text(
            "❌ Введите число\nНапример: <code>10</code> или <code>0.5</code>",
            parse_mode="HTML",
        )
        return ENTER_ASSET_AMOUNT

async def add_asset_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

# --- Диалог 'Новая сделка' ---

async def cmd_new_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    for symbol, info in CRYPTO_IDS.items():
        kb.append(
            [InlineKeyboardButton(f"{info['name']} ({symbol})", callback_data=f"trade_{symbol}")]
        )
    await update.message.reply_text(
        "🆕 <b>Новая сделка</b>\n\nВыберите криптовалюту:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SELECT_CRYPTO

async def trade_select_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    symbol = q.data.replace("trade_", "")
    context.user_data["trade_symbol"] = symbol

    await q.edit_message_text(
        f"✅ Выбрано: <b>{symbol}</b>\n\nВведите количество:",
        parse_mode="HTML",
    )
    return ENTER_AMOUNT

async def trade_enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError()
        context.user_data["trade_amount"] = amount

        symbol = context.user_data["trade_symbol"]
        await update.message.reply_text("🔄 Получаю цену...")

        async with aiohttp.ClientSession() as session:
            cdata = await get_crypto_price(session, symbol, use_cache=False)

        if cdata:
            current_price = cdata["usd"]
            context.user_data["trade_price"] = current_price
            kb = [[InlineKeyboardButton(
                f"➡️ Продолжить с ${current_price:,.4f}",
                callback_data="price_continue"
            )]]
            await update.message.reply_text(
                f"✅ Количество: <b>{amount:.4f}</b>\n\n"
                f"Цена: <b>${current_price:,.4f}</b>\n\n"
                f"Нажми кнопку или введи свою цену:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        else:
            await update.message.reply_text(
                f"✅ Количество: <b>{amount:.4f}</b>\n\n"
                f"Введите цену покупки (USD):",
                parse_mode="HTML",
            )
        return ENTER_PRICE

    except Exception:
        await update.message.reply_text("❌ Введите число")
        return ENTER_AMOUNT

async def trade_enter_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # вариант кнопки
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        if q.data == "price_continue":
            price = context.user_data.get("trade_price")
            await q.edit_message_text(
                f"✅ Цена: <b>${price:,.4f}</b>\n\n"
                f"Введите целевую прибыль (%):",
                parse_mode="HTML",
            )
            return ENTER_TARGET

    # ручной ввод
    try:
        price = float(update.message.text.replace(",", ""))
        if price <= 0:
            raise ValueError()
        context.user_data["trade_price"] = price

        await update.message.reply_text(
            f"✅ Цена: <b>${price:,.4f}</b>\n\nВведите целевую прибыль (%):",
            parse_mode="HTML",
        )
        return ENTER_TARGET

    except Exception:
        await update.message.reply_text("❌ Введите число")
        return ENTER_PRICE

async def trade_enter_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target = float(update.message.text)
        if target <= 0:
            raise ValueError()

        uid = update.effective_user.id
        symbol = context.user_data["trade_symbol"]
        amount = context.user_data["trade_amount"]
        price = context.user_data["trade_price"]

        add_trade_hybrid(uid, symbol, amount, price, target)

        await update.message.reply_text(
            "✅ <b>Сделка добавлена!</b>\n\n"
            f"💰 {symbol}\n"
            f"Кол-во: {amount:.4f}\n"
            f"Цена: ${price:,.2f}\n"
            f"Цель: +{target}%",
            parse_mode="HTML",
            reply_markup=get_main_menu(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    except Exception:
        await update.message.reply_text("❌ Введите число")
        return ENTER_TARGET

async def trade_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

# --- События недели (динамические с Finnhub) ---

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pf = get_user_portfolio(uid)

    riga_tz = timezone(timedelta(hours=2))
    now = datetime.now(riga_tz)
    now_str = now.strftime("%d.%m.%Y %H:%M (Рига)")

    async with aiohttp.ClientSession() as session:
        econ = await get_economic_calendar(session, days=7)
        earns = await get_earnings_calendar(session, days=7)
        fg_val = await get_fear_greed_index(session)

    text = format_events_block(econ, earns, pf, fg_val, now_str)
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Помощь</b>\n\n"
        "<b>Команды:</b>\n"
        "• /start — главное меню\n"
        "• /add TICKER КОЛ-ВО — быстро добавить актив в портфель\n\n"
        "<b>Кнопки меню:</b>\n"
        "• 💼 Мой портфель\n"
        "• 🎯 Мои сделки\n"
        "• 📊 Рыночные сигналы\n"
        "• 📰 События недели\n"
        "• 👤 Мой профиль\n"
        "• ➕ Добавить актив\n"
        "• 🆕 Новая сделка\n\n"
        "<b>Алерты:</b>\n"
        "• Резкие движения цены (в общий канал)\n"
        "• Достижение твоей целевой прибыли (лично тебе)\n\n"
        "<i>Это не финсовет</i>",
        parse_mode="HTML",
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

# =========================================================
# ================== HEALTH CHECK SERVER ==================
# =========================================================

async def health_handler(_request):
    return web.Response(text="OK", status=200)

async def start_health_server(application: Application):
    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"✅ Health check server running on port {port}")
    application.bot_data["health_runner"] = runner

async def stop_health_server(application: Application):
    runner: Optional[web.AppRunner] = application.bot_data.get("health_runner")
    if runner:
        print("🛑 Stopping health server...")
        try:
            await runner.cleanup()
            print("  ✅ Health server stopped")
        except Exception as e:
            print(f"  ⚠️ Error stopping health server: {e}")

# =========================================================
# ================== APPLICATION LIFECYCLE ================
# =========================================================

async def app_post_init(application: Application):
    print("🔁 post_init: loading data...")
    await load_data_on_start()
    print("🔁 post_init: data loaded")

    # health server
    await start_health_server(application)

    # job_queue
    if CHAT_ID:
        print("🔁 post_init: scheduling alerts job (10m)...")
    else:
        print("🔁 post_init: CHAT_ID not set, summary price alerts disabled")

    application.job_queue.run_repeating(
        check_all_alerts,
        interval=600,   # каждые 10 минут
        first=60,       # первая через минуту
        name="alerts_job",
    )

    print("✅ post_init complete")

async def app_post_stop(application: Application):
    print("🛑 post_stop: shutdown started")

    # останавливаем health server
    await stop_health_server(application)

    # ждём фоновые таски супабазы
    if active_tasks:
        print(f"⏳ Waiting for {len(active_tasks)} background tasks...")
        try:
            await asyncio.wait_for(
                asyncio.gather(*active_tasks, return_exceptions=True),
                timeout=30.0
            )
            print("  ✅ All background tasks completed")
        except asyncio.TimeoutError:
            print("  ⚠️ Timeout waiting for tasks")

    # локальное сохранение
    try:
        print("💾 Saving final state...")
        price_cache.save()
        save_portfolios_local()
        save_trades_local()
        print("  ✅ Local data saved")
    except Exception as e:
        print(f"  ⚠️ Error saving data: {e}")

    # supabase session close
    try:
        await supabase_storage.close()
        print("  ✅ Supabase session closed")
    except Exception as e:
        print(f"  ⚠️ Error closing Supabase: {e}")

    print("👋 post_stop: done")

# =========================================================
# ========================== MAIN =========================
# =========================================================

def main():
    print("============================================================")
    print("🚀 Starting Trading Bot v6 (PTB21+)")
    print("============================================================")
    print(f"Python version: {sys.version}")
    print("============================================================")
    print("✅ Features:")
    print("  • Hybrid storage (Supabase + local)")
    print("  • Trades with profit targets & alerts")
    print("  • Fear & Greed + RSI/MACD/SMA/Volume scoring")
    print("  • Dynamic weekly events (macro, earnings, crypto sentiment)")
    print("  • Better UI (bars, sections, emojis)")
    print("  • Graceful shutdown w/ background task drain")
    print("============================================================")
    print(f"✅ BOT_TOKEN: {TOKEN[:10]}...")
    print(f"✅ CHAT_ID: {CHAT_ID if CHAT_ID else 'Not set'}")
    print(f"✅ DATA_DIR: {DATA_DIR}")
    print(f"✅ TA_AVAILABLE: {TA_AVAILABLE}")
    print("============================================================")

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(app_post_init)
        .post_stop(app_post_stop)
        .build()
    )

    # команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("add", cmd_add))

    # профиль
    application.add_handler(CallbackQueryHandler(profile_select, pattern="^profile_"))

    # диалог новой сделки
    trade_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🆕 Новая сделка$"), cmd_new_trade)],
        states={
            SELECT_CRYPTO: [
                CallbackQueryHandler(trade_select_crypto, pattern="^trade_")
            ],
            ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_amount)
            ],
            ENTER_PRICE: [
                CallbackQueryHandler(trade_enter_price, pattern="^price_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_price),
            ],
            ENTER_TARGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, trade_enter_target)
            ],
        },
        fallbacks=[CommandHandler("cancel", trade_cancel)],
        name="trade_conv",
        persistent=False,
    )
    application.add_handler(trade_conv)

    # диалог добавления актива
    add_asset_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить актив$"), cmd_add_asset)
        ],
        states={
            SELECT_ASSET_TYPE: [
                CallbackQueryHandler(add_asset_select_type, pattern="^asset_")
            ],
            SELECT_ASSET: [
                CallbackQueryHandler(add_asset_select_item, pattern="^add(ticker|crypto)_")
            ],
            ENTER_ASSET_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_asset_enter_amount)
            ],
        },
        fallbacks=[CommandHandler("cancel", add_asset_cancel)],
        name="add_asset_conv",
        persistent=False,
    )
    application.add_handler(add_asset_conv)

    # кнопки меню
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons)
    )

    # ошибки
    application.add_error_handler(on_error)

    # go
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
