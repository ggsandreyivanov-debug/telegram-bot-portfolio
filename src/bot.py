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
CHAT_ID = os.getenv("CHAT_ID")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

LUNARCRUSH_API_KEY = os.getenv("LUNARCRUSH_API_KEY")  # пока не используем, но оставляем

if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")

if not CHAT_ID:
    print("⚠ CHAT_ID не установлен - общие алерты в один чат (price alerts summary) будут выключены")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# Доступные тикеры фондов/ETF/индексов
AVAILABLE_TICKERS = {
    "VWCE.DE": {"name": "VWCE", "type": "stock"},
    "4GLD.DE": {"name": "4GLD (Gold ETC)", "type": "stock"},
    "DE000A2T5DZ1.SG": {"name": "X IE Physical Gold ETC", "type": "stock"},
    "SPY": {"name": "S&P 500 (SPY)", "type": "stock"},
}

# Крипта
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

# Пороги для алертов
THRESHOLDS = {
    "stocks": 1.0,   # %
    "crypto": 4.0,   # %
}

# =========================================================
# =================  ДАННЫЕ В ПАМЯТИ  =====================
# =========================================================

# в памяти
user_portfolios: Dict[int, Dict[str, float]] = {}
user_trades: Dict[int, List[Dict[str, Any]]] = {}
user_profiles: Dict[int, str] = {}

# разговорные стейты
SELECT_CRYPTO, ENTER_AMOUNT, ENTER_PRICE, ENTER_TARGET = range(4)
SELECT_ASSET_TYPE, SELECT_ASSET, ENTER_ASSET_AMOUNT = range(4, 7)

# =========================================================
# =================  ВСПОМОГАТЕЛЬНОЕ ХРАНИЛИЩЕ  ===========
# =========================================================

def get_data_directory() -> Path:
    """Определить безопасную директорию для данных с проверкой прав записи."""
    possible_dirs = [
        Path("/home/claude/bot_data"),
        Path("/opt/render/project/src/bot_data"),
        Path("./bot_data"),
        Path(tempfile.gettempdir()) / "bot_data",
    ]

    for dir_path in possible_dirs:
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            test_file = dir_path / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
            print(f"✅ Using data directory: {dir_path}")
            return dir_path
        except (OSError, PermissionError) as e:
            print(f"⚠️  Cannot use {dir_path}: {e}")
            continue

    raise RuntimeError("❌ Cannot find writable directory for data storage!")

DATA_DIR = get_data_directory()
CACHE_FILE = DATA_DIR / "price_cache.json"
PORTFOLIO_FILE = DATA_DIR / "portfolios.json"
TRADES_FILE = DATA_DIR / "trades.json"

# =========================================================
# ==================  SUPABASE STORAGE  ===================
# =========================================================

class SupabaseStorage:
    """
    Асинхронная работа с Supabase.
    Используем как "источник правды", локальные файлы — fallback.
    """

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
            print("⚠️  Supabase storage disabled (no credentials)")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def load_portfolios(self) -> Dict[int, Dict[str, float]]:
        """
        Получить все портфели {user_id: {ticker: amount}}.
        """
        if not self.enabled:
            return {}

        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/portfolios?select=*"
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    out: Dict[int, Dict[str, float]] = {}
                    for row in data:
                        try:
                            uid = int(row["user_id"])
                            assets = row["assets"]
                            if isinstance(assets, dict):
                                out[uid] = assets
                        except (KeyError, ValueError, TypeError) as e:
                            print(f"⚠️ Invalid portfolio row: {e}")
                            continue
                    print(f"✅ Loaded {len(out)} portfolios from Supabase")
                    return out
                else:
                    body = await resp.text()
                    print(f"⚠️ Supabase load_portfolios HTTP {resp.status} body={body[:200]}")
                    return {}
        except Exception as e:
            print(f"⚠️ Supabase load_portfolios error: {e}")
            return {}

    async def save_portfolio(self, user_id: int, assets: Dict[str, float]):
        """
        Upsert портфеля в Supabase.
        """
        if not self.enabled:
            return

        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/portfolios"
            data = {
                "user_id": user_id,
                "assets": assets,
                "updated_at": datetime.utcnow().isoformat(),
            }
            headers = {**self.headers, "Prefer": "resolution=merge-duplicates"}
            async with session.post(url, headers=headers, json=data,
                                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status not in [200, 201, 204]:
                    body = await resp.text()
                    print(f"⚠️ Supabase save_portfolio HTTP {resp.status} body={body[:200]}")
        except Exception as e:
            print(f"⚠️ Supabase save_portfolio error: {e}")

    async def load_trades(self) -> Dict[int, List[Dict[str, Any]]]:
        """
        Получить все сделки по всем юзерам:
        { user_id: [ {symbol, amount, entry_price, target_profit_pct, ...}, ... ] }
        """
        if not self.enabled:
            return {}

        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/trades?select=*&order=created_at.desc"
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    rows = await resp.json()
                    result: Dict[int, List[Dict[str, Any]]] = {}
                    for row in rows:
                        try:
                            uid = int(row["user_id"])
                            if uid not in result:
                                result[uid] = []
                            result[uid].append({
                                "id": row["id"],
                                "symbol": row["symbol"],
                                "amount": float(row["amount"]),
                                "entry_price": float(row["entry_price"]),
                                "target_profit_pct": float(row["target_profit_pct"]),
                                "notified": bool(row.get("notified", False)),
                                "timestamp": row.get("created_at", datetime.utcnow().isoformat()),
                            })
                        except (KeyError, ValueError, TypeError) as e:
                            print(f"⚠️ Invalid trade row: {e}")
                            continue
                    total = sum(len(v) for v in result.values())
                    print(f"✅ Loaded {total} trades from Supabase")
                    return result
                else:
                    body = await resp.text()
                    print(f"⚠️ Supabase load_trades HTTP {resp.status} body={body[:200]}")
                    return {}
        except Exception as e:
            print(f"⚠️ Supabase load_trades error: {e}")
            return {}

    async def add_trade(
        self,
        user_id: int,
        symbol: str,
        amount: float,
        entry_price: float,
        target_profit_pct: float,
    ) -> bool:
        """
        Добавить сделку (insert).
        """
        if not self.enabled:
            return False
        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/trades"
            data = {
                "user_id": user_id,
                "symbol": symbol,
                "amount": amount,
                "entry_price": entry_price,
                "target_profit_pct": target_profit_pct,
            }
            async with session.post(url, headers=self.headers, json=data,
                                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status in [200, 201, 204]:
                    return True
                else:
                    body = await resp.text()
                    print(f"⚠️ Supabase add_trade HTTP {resp.status} body={body[:200]}")
                    return False
        except Exception as e:
            print(f"⚠️ Supabase add_trade error: {e}")
            return False

    async def update_trade_notified(self, trade_id: int):
        """
        Пометить сделку как notified.
        """
        if not self.enabled:
            return
        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/trades?id=eq.{trade_id}"
            data = {"notified": True}
            async with session.patch(url, headers=self.headers, json=data,
                                     timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status not in [200, 204]:
                    body = await resp.text()
                    print(f"⚠️ Supabase update_trade_notified HTTP {resp.status} body={body[:200]}")
        except Exception as e:
            print(f"⚠️ Supabase update_trade_notified error: {e}")


supabase_storage = SupabaseStorage(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# ======================  CACHE  ==========================
# =========================================================

class PriceCache:
    """Кеш котировок с TTL и сохранением на диск."""

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
                print("⚠️ Invalid cache format, skip")
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
                except (ValueError, TypeError):
                    continue
                if now_ts - ts < self.ttl * 2:
                    self.cache[k] = v
                    valid += 1
            print(f"✅ Loaded {valid} valid prices from cache")
        except Exception as e:
            print(f"⚠️ Cache load error: {e}")

    def save(self):
        tmp = CACHE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.cache, indent=2))
            shutil.move(str(tmp), str(CACHE_FILE))
        except Exception as e:
            print(f"⚠️ Cache save error: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except:
                pass

    def get(self, key: str) -> Optional[Dict]:
        entry = self.cache.get(key)
        if not entry:
            return None
        try:
            age = datetime.now().timestamp() - float(entry["timestamp"])
        except (KeyError, ValueError, TypeError):
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
        if price_val is None:
            return None
        try:
            return float(price_val)
        except (ValueError, TypeError):
            return None

    def set_for_alert(self, key: str, price: float):
        try:
            price = float(price)
            if math.isnan(price) or math.isinf(price) or price <= 0:
                print(f"⚠️ Invalid alert price for {key}: {price}")
                return
        except (ValueError, TypeError):
            print(f"⚠️ Cannot convert alert price for {key}: {price}")
            return
        if key not in self.cache:
            self.cache[key] = {"data": {}, "timestamp": datetime.now().timestamp()}
        self.cache[key]["data"]["price"] = price
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
# ================  ЗАГРУЗКА/СОХРАНЕНИЕ ДАННЫХ  ===========
# =========================================================

def _load_local_files_if_empty():
    """Фоллбек: грузим локальные portfolios/trades если из Supabase ничего не пришло."""
    global user_portfolios, user_trades

    # portfolios
    if not user_portfolios and PORTFOLIO_FILE.exists():
        try:
            raw = PORTFOLIO_FILE.read_text()
            data = json.loads(raw)
            if isinstance(data, dict):
                tmp: Dict[int, Dict[str, float]] = {}
                for k, v in data.items():
                    try:
                        uid = int(k)
                        if isinstance(v, dict):
                            tmp[uid] = v
                    except (ValueError, TypeError):
                        continue
                user_portfolios = tmp
                print(f"✅ Loaded {len(user_portfolios)} portfolios from local file")
        except Exception as e:
            print(f"⚠️ Local portfolio load error: {e}")

    # trades
    if not user_trades and TRADES_FILE.exists():
        try:
            raw = TRADES_FILE.read_text()
            data = json.loads(raw)
            if isinstance(data, dict):
                tmp2: Dict[int, List[Dict[str, Any]]] = {}
                for k, v in data.items():
                    try:
                        uid = int(k)
                        if isinstance(v, list):
                            tmp2[uid] = v
                    except (ValueError, TypeError):
                        continue
                user_trades = tmp2
                print(f"✅ Loaded {len(user_trades)} trade lists from local file")
        except Exception as e:
            print(f"⚠️ Local trades load error: {e}")


async def load_data_on_start():
    """Асинхронная инициализация данных при запуске (post_init)."""
    global user_portfolios, user_trades

    try:
        sp_portfolios = await supabase_storage.load_portfolios()
        if sp_portfolios:
            user_portfolios = sp_portfolios
    except Exception as e:
        print(f"⚠️ Supabase portfolios init error: {e}")

    try:
        sp_trades = await supabase_storage.load_trades()
        if sp_trades:
            user_trades = sp_trades
    except Exception as e:
        print(f"⚠️ Supabase trades init error: {e}")

    # fallback
    _load_local_files_if_empty()


def save_portfolios_local():
    try:
        tmp = PORTFOLIO_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(user_portfolios, indent=2))
        shutil.move(str(tmp), str(PORTFOLIO_FILE))
    except Exception as e:
        print(f"⚠️ Portfolio save error: {e}")
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
        print(f"⚠️ Trades save error: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except:
            pass


def save_portfolio_hybrid(user_id: int, portfolio: Dict[str, float]):
    """
    Сохранить портфель:
    - мгновенно в память
    - локально на диск
    - асинхронно в Supabase
    """
    user_portfolios[user_id] = portfolio
    save_portfolios_local()

    # пушим в Supabase не блокируя UI
    async def _push():
        await supabase_storage.save_portfolio(user_id, portfolio)

    # отдадим задачу в общий event loop позже через asyncio.create_task.
    asyncio.create_task(_push())


def add_trade_hybrid(
    user_id: int,
    symbol: str,
    amount: float,
    entry_price: float,
    target_profit_pct: float,
):
    """
    Добавить сделку:
    - память
    - локальный файл
    - асинхронно Supabase
    """
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
        await supabase_storage.add_trade(
            user_id, symbol, amount, entry_price, target_profit_pct
        )

    asyncio.create_task(_push())

# =========================================================
# ==================== УТИЛИТЫ РЫНКА ======================
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

async def get_yahoo_price(session: aiohttp.ClientSession, ticker: str) -> Optional[Tuple[float, str, float]]:
    """
    Вернёт (price, currency, change_pct_24h)
    """
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": "1d"}
        data = await get_json(session, url, params)
        if not data:
            return None

        result = data.get("chart", {}).get("result", [{}])[0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        cur = meta.get("currency", "USD")
        change_pct = meta.get("regularMarketChangePercent", 0)

        if price is None:
            return None
        try:
            price = float(price)
            change_pct = float(change_pct) if change_pct is not None else 0.0
            if math.isnan(price) or math.isinf(price):
                return None
        except (ValueError, TypeError):
            return None

        return (price, cur, change_pct)

    except Exception as e:
        print(f"❌ Yahoo {ticker} error: {e}")
        return None

async def get_crypto_price_raw(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Цена крипты без кеша.
    """
    info = CRYPTO_IDS.get(symbol)
    if not info:
        return None

    # 1. Binance
    try:
        binance_symbol = info["binance"]
        url = "https://api.binance.com/api/v3/ticker/24hr"
        params = {"symbol": binance_symbol}

        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = float(data.get("lastPrice", 0))
                change_24h = float(data.get("priceChangePercent", 0))
                if price > 0 and not math.isnan(price) and not math.isinf(price):
                    return {
                        "usd": price,
                        "change_24h": change_24h if not math.isnan(change_24h) else None,
                        "source": "Binance",
                    }
            else:
                # fallback дальше
                pass
    except Exception as e:
        print(f"⚠️ Binance failed {symbol}: {e}")

    # 2. CoinPaprika
    try:
        paprika_id = info["paprika"]
        url = f"https://api.coinpaprika.com/v1/tickers/{paprika_id}"
        data = await get_json(session, url, None)
        if data:
            quotes = data.get("quotes", {}).get("USD", {})
            price = quotes.get("price")
            chg = quotes.get("percent_change_24h")
            if price:
                price = float(price)
                if price > 0 and not math.isnan(price):
                    return {
                        "usd": price,
                        "change_24h": float(chg) if chg is not None and not math.isnan(float(chg)) else None,
                        "source": "CoinPaprika",
                    }
    except Exception as e:
        print(f"⚠️ CoinPaprika failed {symbol}: {e}")

    # 3. CoinGecko
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
            price = coin.get("usd")
            chg = coin.get("usd_24h_change")
            if price:
                price = float(price)
                if price > 0 and not math.isnan(price):
                    return {
                        "usd": price,
                        "change_24h": float(chg) if chg is not None and not math.isnan(float(chg)) else None,
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
    """
    Индекс страха/жадности (крипта).
    """
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
# ===================== ПОЛЬЗОВАТЕЛЬСКИЕ ДАННЫЕ  ==========
# =========================================================

def get_user_portfolio(user_id: int) -> Dict[str, float]:
    """
    Возвращает портфель (и создаёт базовый, если пусто).
    """
    if user_id not in user_portfolios:
        user_portfolios[user_id] = {
            "VWCE.DE": 0,
            "DE000A2T5DZ1.SG": 0,
            "BTC": 0,
            "ETH": 0,
            "SOL": 0,
        }
    return user_portfolios[user_id]

def get_all_active_assets() -> Dict[str, List[int]]:
    """
    Собираем список активов, по которым есть позиции или сделки.
    Нужно для алертов.
    """
    active_assets: Dict[str, List[int]] = {}

    # портфели
    for uid, pf in user_portfolios.items():
        for ticker, qty in pf.items():
            try:
                if float(qty) > 0:
                    active_assets.setdefault(ticker, [])
                    if uid not in active_assets[ticker]:
                        active_assets[ticker].append(uid)
            except (ValueError, TypeError):
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

def get_user_trades(user_id: int) -> List[Dict[str, Any]]:
    if user_id not in user_trades:
        user_trades[user_id] = []
    return user_trades[user_id]

# =========================================================
# ======================= СИГНАЛЫ РЫНКА ===================
# =========================================================

async def get_market_signal(session: aiohttp.ClientSession, symbol: str, investor_type: str) -> Dict[str, Any]:
    data = await get_crypto_price(session, symbol)
    if not data:
        return {"signal": "UNKNOWN", "emoji": "❓", "reason": "Нет данных о цене"}

    fear_greed = await get_fear_greed_index(session)
    if not fear_greed:
        fear_greed = 50

    # очень простая логика, но разная по профилю
    if investor_type == "long":
        if fear_greed < 30:
            return {
                "signal": "BUY",
                "emoji": "🟢",
                "reason": f"Экстремальный страх ({fear_greed}/100). Хорошая долгосрочная точка входа.",
            }
        elif fear_greed > 75:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Жадность ({fear_greed}/100). Просто держать.",
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Рынок в норме ({fear_greed}/100). Держать.",
            }

    elif investor_type == "swing":
        if fear_greed < 40:
            return {
                "signal": "BUY",
                "emoji": "🟢",
                "reason": f"Страх ({fear_greed}/100). Можно зайти на коррекции.",
            }
        elif fear_greed > 65:
            return {
                "signal": "SELL",
                "emoji": "🔴",
                "reason": f"Жадность ({fear_greed}/100). Зафиксировать волну.",
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Плоско ({fear_greed}/100). Ждать движения.",
            }

    else:  # day
        if fear_greed < 45:
            return {
                "signal": "BUY",
                "emoji": "🟢",
                "reason": f"Страх ({fear_greed}/100). Возможен отскок.",
            }
        elif fear_greed > 60:
            return {
                "signal": "SELL",
                "emoji": "🔴",
                "reason": f"Перекупленность ({fear_greed}/100). Риск коррекции.",
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Флэт ({fear_greed}/100). Без чёткого сигнала.",
            }

# =========================================================
# ======================== АЛЕРТЫ =========================
# =========================================================

async def check_all_alerts(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодический джоб:
    1. Алерты по резкому движению цены (в общий чат CHAT_ID)
    2. Алерты по достижению цели сделки (лично юзерам)
    """
    if not context.application:
        return

    bot = context.application.bot

    print("🔔 Running alerts check...")

    # какие активы надо вообще смотреть
    try:
        active_assets = get_all_active_assets()
    except Exception as e:
        print(f"⚠️ active_assets error: {e}")
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
                if not pdata:
                    continue
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
                    print(f"  {asset}: first price seen {price:.2f}")

                price_cache.set_for_alert(cache_key, price)

            # крипта
            elif asset in CRYPTO_IDS:
                cdata = await get_crypto_price(session, asset, use_cache=False)
                if not cdata:
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

                # сделки юзеров
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
                        except (KeyError, ValueError, TypeError):
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
                                f"🎯 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n\n"
                                f"💰 {asset}\n"
                                f"Кол-во: {amount:.4f}\n"
                                f"Вход: ${entry_price:,.2f}\n"
                                f"Сейчас: ${current_price:,.2f}\n\n"
                                f"📈 Прибыль: <b>{profit_pct:.2f}%</b> "
                                f"(${profit_usd:,.2f})\n"
                                f"💵 Стоимость позиции: ${value_now:,.2f}\n\n"
                                f"✅ <b>Рекомендация: ПРОДАВАТЬ</b>"
                            )

                            trade_alerts.setdefault(uid, []).append(alert_text)
                            tr["notified"] = True
                            print(f"  🚨 PROFIT ALERT uid={uid} {asset} +{profit_pct:.2f}%")

            # чуть притормозим, чтобы не долбить API
            await asyncio.sleep(0.2)

    # если сделки обновились -> сохранить локально
    if trade_alerts:
        save_trades_local()

    # сохранить кеш
    price_cache.save()

    # отправить резкие движения (общий канал)
    if price_alerts and CHAT_ID:
        msg = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(price_alerts)
        try:
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
            print(f"📤 Sent {len(price_alerts)} price alerts to {CHAT_ID}")
        except Exception as e:
            print(f"⚠️ Failed to send price alerts to CHAT_ID: {e}")

    # отправить достигнутые таргеты в личку
    sent_trade_alerts = 0
    for uid, alerts in trade_alerts.items():
        for text in alerts:
            try:
                await bot.send_message(chat_id=str(uid), text=text, parse_mode="HTML")
                sent_trade_alerts += 1
            except Exception as e:
                print(f"⚠️ Failed to send trade alert to {uid}: {e}")

    if sent_trade_alerts:
        print(f"📤 Sent {sent_trade_alerts} trade alerts to {len(trade_alerts)} users")

    cache_stats = price_cache.get_stats()
    print(f"📊 Cache stats: {cache_stats}")
    price_cache.reset_stats()
    print("✅ Alerts check done\n")

# =========================================================
# ==================== HANDLERS ===========================
# =========================================================

def get_main_menu():
    keyboard = [
        [KeyboardButton("💼 Мой портфель"), KeyboardButton("💹 Все цены")],
        [KeyboardButton("🎯 Мои сделки"), KeyboardButton("📊 Рыночные сигналы")],
        [KeyboardButton("📰 События недели"), KeyboardButton("🔮 Прогнозы")],
        [KeyboardButton("➕ Добавить актив"), KeyboardButton("🆕 Новая сделка")],
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_profiles:
        user_profiles[uid] = "long"

    await update.message.reply_text(
        "👋 <b>Trading Bot v5 (PTB21)</b>\n\n"
        "<b>Функции:</b>\n"
        "• 💼 Портфель (акции + крипта)\n"
        "• 🎯 Сделки с целевой прибылью\n"
        "• 📊 Рыночные сигналы BUY/HOLD/SELL\n"
        "• 🔔 Умные алерты\n\n"
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
        lines = ["💼 <b>Ваш портфель:</b>\n"]
        total_value_usd = 0

        async with aiohttp.ClientSession() as session:
            # Фондовый блок
            stock_items = [(t, q) for t, q in portfolio.items() if t in AVAILABLE_TICKERS and q > 0]
            if stock_items:
                lines.append("<b>📊 Акции/ETF:</b>")
                lines.append("<pre>")
                lines.append("Актив          Кол-во    Цена        Сумма")
                lines.append("─" * 50)

                for ticker, quantity in stock_items:
                    pdata = await get_yahoo_price(session, ticker)
                    if pdata:
                        price, cur, _chg = pdata
                        value = price * quantity
                        name = AVAILABLE_TICKERS[ticker]["name"][:14].ljust(14)
                        qty_str = f"{quantity:.2f}".rjust(8)
                        price_str = f"{price:.2f}".rjust(8)
                        val_str = f"{value:.2f} {cur}".rjust(12)
                        lines.append(f"{name} {qty_str} {price_str} {val_str}")

                        # примитивная конвертация: EUR -> USD ~1.1, остальное считаем USD
                        if cur == "USD":
                            total_value_usd += value
                        elif cur == "EUR":
                            total_value_usd += value * 1.1

                    await asyncio.sleep(0.25)

                lines.append("</pre>")

            # Крипта
            crypto_items = [(s, q) for s, q in portfolio.items() if s in CRYPTO_IDS and q > 0]
            if crypto_items:
                lines.append("\n<b>₿ Криптовалюты:</b>")
                lines.append("<pre>")
                lines.append("Монета    Кол-во      Цена          Сумма")
                lines.append("─" * 50)

                for symbol, quantity in crypto_items:
                    cdata = await get_crypto_price(session, symbol)
                    if cdata:
                        price = cdata["usd"]
                        chg = cdata.get("change_24h")
                        value = price * quantity
                        total_value_usd += value

                        sym_str = symbol.ljust(9)
                        qty_str = f"{quantity:.4f}".rjust(10)
                        price_str = f"${price:,.2f}".rjust(12)
                        val_str = f"${value:,.2f}".rjust(12)
                        chg_emoji = "📈" if chg and chg >= 0 else "📉" if chg else ""
                        lines.append(f"{sym_str} {qty_str} {price_str} {val_str} {chg_emoji}")

                    await asyncio.sleep(0.2)

                lines.append("</pre>")

        if total_value_usd > 0:
            lines.append(f"\n<b>💰 Общая стоимость: ~${total_value_usd:,.2f}</b>")

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

        lines = [
            f"💹 <b>Все цены</b>\n",
            f"🕐 Данные: <b>{timestamp}</b> (Рига)\n",
        ]

        async with aiohttp.ClientSession() as session:
            # STOCKS
            lines.append("<b>📊 Фондовый рынок:</b>")
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

                    lines.append(f"│ {name} │ {price_str} │ {chg_str} │")
                else:
                    name = info["name"][:16].ljust(16)
                    lines.append(f"│ {name} │ {'н/д'.ljust(10)} │ {'N/A'.rjust(7)} │")

                await asyncio.sleep(0.25)

            lines.append("└──────────────────┴────────────┴─────────┘")
            lines.append("</pre>")

            # CRYPTO
            lines.append("\n<b>₿ Криптовалюты:</b>")
            lines.append("<pre>")
            lines.append("┌────────┬──────────────┬─────────┬──────────┐")
            lines.append("│ Монета │ Цена         │ 24h     │ Источник │")
            lines.append("├────────┼──────────────┼─────────┼──────────┤")

            for symbol, info in CRYPTO_IDS.items():
                try:
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

                        lines.append(
                            f"│ {sym_str} │ {price_str} │ {chg_str} │ {source.ljust(8)} │"
                        )
                    else:
                        sym_str = symbol.ljust(6)
                        lines.append(
                            f"│ {sym_str} │ {'н/д'.ljust(12)} │ {'N/A'.rjust(7)} │ {'—'.ljust(8)} │"
                        )
                except Exception as e:
                    print(f"❌ {symbol} price error: {e}")
                    sym_str = symbol.ljust(6)
                    lines.append(
                        f"│ {sym_str} │ {'ошибка'.ljust(12)} │ {'N/A'.rjust(7)} │ {'—'.ljust(8)} │"
                    )

                await asyncio.sleep(0.2)

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

        lines = ["🎯 <b>Ваши сделки:</b>\n"]
        total_value = 0.0
        total_profit = 0.0

        async with aiohttp.ClientSession() as session:
            for i, tr in enumerate(trades, start=1):
                try:
                    symbol = tr["symbol"]
                    entry_price = float(tr["entry_price"])
                    amount = float(tr["amount"])
                    target = float(tr["target_profit_pct"])
                except (KeyError, ValueError, TypeError):
                    continue

                cdata = await get_crypto_price(session, symbol)
                if not cdata:
                    continue

                current_price = cdata["usd"]
                if entry_price <= 0:
                    continue

                profit_pct = ((current_price - entry_price) / entry_price) * 100
                profit_usd = amount * (current_price - entry_price)
                value_now = amount * current_price

                total_value += value_now
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
                lines.append(
                    f"├ Прибыль: <b>{profit_pct:+.2f}%</b> (${profit_usd:+,.2f})"
                )
                lines.append(
                    f"├ Цель: {target}% {'✅' if profit_pct >= target else '⏳'}"
                )
                lines.append(f"└ Стоимость: ${value_now:,.2f}\n")

                await asyncio.sleep(0.2)

        if total_value > 0:
            initial_value = total_value - total_profit
            if initial_value > 0:
                total_profit_pct = (total_profit / initial_value) * 100
                lines.append("━━━━━━━━━━━━━━━━")
                lines.append(f"💰 <b>Общая стоимость: ${total_value:,.2f}</b>")
                lines.append(
                    f"📊 <b>Общая прибыль: {total_profit_pct:+.2f}% (${total_profit:+,.2f})</b>"
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
        lines = [
            f"📊 <b>Рыночные сигналы</b>\n",
            f"Профиль: {inv_info['emoji']} <b>{inv_info['name']}</b>\n",
        ]

        async with aiohttp.ClientSession() as session:
            fg = await get_fear_greed_index(session)
            if fg is not None:
                if fg < 25:
                    fg_status = "😱 Экстремальный страх"
                elif fg < 45:
                    fg_status = "😰 Страх"
                elif fg < 55:
                    fg_status = "😐 Нейтрально"
                elif fg < 75:
                    fg_status = "😃 Жадность"
                else:
                    fg_status = "🤑 Экстремальная жадность"

                lines.append(f"📈 Fear & Greed: <b>{fg}/100</b> ({fg_status})\n")

            for symbol in ["BTC", "ETH", "SOL", "AVAX"]:
                sig = await get_market_signal(session, symbol, inv_type)
                lines.append(f"{sig['emoji']} <b>{symbol}: {sig['signal']}</b>")
                lines.append(f"   └ {sig['reason']}\n")
                await asyncio.sleep(0.2)

        lines.append("\n<i>⚠️ Это не финансовая рекомендация</i>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        print(f"❌ market_signals error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении сигналов")

# Типы профилей
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
        f"Теперь сигналы адаптированы под ваш стиль.",
        parse_mode="HTML",
    )

# === Добавление актива (портфель) ===

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Быстрый вариант: /add TICKER КОЛ-ВО
    """
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
    except ValueError:
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

# --- Пошаговое добавление актива через кнопки ---

async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Акции / ETF", callback_data="asset_stocks")],
        [InlineKeyboardButton("₿ Криптовалюты", callback_data="asset_crypto")],
    ]
    await update.message.reply_text(
        "➕ <b>Добавить актив</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_ASSET_TYPE

async def add_asset_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    asset_type = q.data.replace("asset_", "")
    context.user_data["asset_type"] = asset_type

    keyboard = []

    if asset_type == "stocks":
        context.user_data["asset_category"] = "stocks"
        for ticker, info in AVAILABLE_TICKERS.items():
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{info['name']} ({ticker})", callback_data=f"addticker_{ticker}"
                    )
                ]
            )
    else:
        context.user_data["asset_category"] = "crypto"
        for symbol, info in CRYPTO_IDS.items():
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{info['name']} ({symbol})", callback_data=f"addcrypto_{symbol}"
                    )
                ]
            )

    type_emoji = "📊" if asset_type == "stocks" else "₿"
    type_name = "Акции / ETF" if asset_type == "stocks" else "Криптовалюты"

    await q.edit_message_text(
        f"{type_emoji} <b>{type_name}</b>\n\nВыберите актив:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
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

# --- Пошаговое открытие новой сделки ---

async def cmd_new_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for symbol, info in CRYPTO_IDS.items():
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{info['name']} ({symbol})", callback_data=f"trade_{symbol}"
                )
            ]
        )

    await update.message.reply_text(
        "🆕 <b>Новая сделка</b>\n\nВыберите криптовалюту:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
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

            kb = [
                [
                    InlineKeyboardButton(
                        f"➡️ Продолжить с ${current_price:,.4f}",
                        callback_data="price_continue",
                    )
                ]
            ]
            await update.message.reply_text(
                f"✅ Количество: <b>{amount:.4f}</b>\n\n"
                f"Цена: <b>${current_price:,.4f}</b>\n\n"
                f"Нажмите кнопку или введите свою цену:",
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
    # вариант через кнопку
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

    # или ввод вручную
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
            f"✅ <b>Сделка добавлена!</b>\n\n"
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

# --- События недели / прогнозы / помощь ---

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сводка недели: макро + крипто + крупные отчёты.
    Это не онлайн-веб, а статический блок, который можно руками апдейтить раз в день/неделю.
    """
    # пример структуры: фондовый рынок / макро / крипта
    # ты потом просто меняешь тексты внутри этого хэндлера
    lines = [
        "📰 <b>События недели</b>\n",
        "<b>📊 Фондовый рынок:</b>\n",
        "• Заседание ФРС / решение по ставке\n"
        "  Волатильность индексов (SPY, VWCE), рост доллара → давление на акции.\n",
        "• Отчёты Big Tech\n"
        "  Сильная выручка/маржа = поддержка индекса S&P500 (SPY).\n"
        "  Слабые прогнозы = давление на индекс, риск коррекции.\n",
        "<b>₿ Криптовалюты:</b>\n",
        "• Решения по Bitcoin ETF / потоки в фонды\n"
        "  Приток капитала в BTC → рост всего рынка.\n",
        "• Крупные апгрейды сетей (L2, снижение комиссий)\n"
        "  Улучшение экономики газа = позитив для ETH/экосистем.\n",
        "<b>🧠 Что смотреть лично тебе:</b>\n"
        "• Если ты long-инвестор: ищи страх (панические свечи вниз, FUD).\n"
        "• Если ты swing: смотри перегрев после новостей (жадность и памп).\n",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔮 <b>Прогнозы</b>\n\n"
        "Смотри <b>📊 Рыночные сигналы</b>.\n"
        "Они уже учитывают твой стиль (долгий / свинг / внутри дня).\n",
        parse_mode="HTML",
    )

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
        "• 👤 Мой профиль\n\n"
        "<b>Алерты:</b>\n"
        "• Резкие движения цены\n"
        "• Достижение твоей целевой прибыли по сделке\n",
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

# =========================================================
# ================== HEALTH CHECK SERVER ==================
# =========================================================

async def health_handler(_request):
    return web.Response(text="OK", status=200)

async def start_health_server(application: Application):
    """
    Запускаем aiohttp сервер для Render health checks.
    Храним runner в application.bot_data["health_runner"].
    """
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

# post_init и post_stop – это хуки из PTB 21.x+
# run_polling() сам вызовет post_init(), а потом при остановке вызовет post_stop().

async def app_post_init(application: Application):
    """
    Тут мы делаем всё, что надо один раз при запуске:
    - грузим портфели/сделки
    - поднимаем health-сервер
    - добавляем job_queue задачу
    """
    print("🔁 post_init: loading data...")
    await load_data_on_start()
    print("🔁 post_init: data loaded")

    # health server для Render
    await start_health_server(application)

    # job_queue (алерты каждые 10 минут)
    if CHAT_ID:
        print("🔁 post_init: scheduling alerts job (10m)...")
    else:
        print("🔁 post_init: CHAT_ID not set, alerts summary -> disabled")

    application.job_queue.run_repeating(
        check_all_alerts,
        interval=600,          # каждые 10 минут
        first=60,              # первая проверка через минуту
        name="alerts_job",
    )

    print("✅ post_init complete")

async def app_post_stop(application: Application):
    """
    Тут мы красиво останавливаемся:
    - шатаем health server
    - сохраняем данные на диск
    - закрываем supabase сессию
    """
    print("🛑 post_stop: shutdown started")

    # health server down
    await stop_health_server(application)

    # финально всё сохранить
    try:
        print("💾 Saving final state...")
        price_cache.save()
        save_portfolios_local()
        save_trades_local()
        print("  ✅ Local data saved")
    except Exception as e:
        print(f"  ⚠️ Error saving data: {e}")

    try:
        await supabase_storage.close()
        print("  ✅ Supabase session closed")
    except Exception as e:
        print(f"  ⚠️ Error closing Supabase: {e}")

    print("👋 post_stop: done")

# =========================================================
# ========================= MAIN ==========================
# =========================================================

def main():
    print("============================================================")
    print("🚀 Starting FIXED Trading Bot v5 (PTB21+)")
    print("============================================================")
    print(f"Python version: {sys.version}")
    # версия либы мы не можем узнать до билда Application, так что ниже пропустим
    print("============================================================")
    print("✅ Features:")
    print("  • Portfolios with hybrid storage (Supabase + local)")
    print("  • Trade tracking with profit targets")
    print("  • Market signals per profile")
    print("  • Price/target alerts via job queue")
    print("  • Graceful shutdown via post_stop()")
    print("============================================================")
    print(f"✅ BOT_TOKEN: {TOKEN[:10]}...")
    print(f"✅ CHAT_ID: {CHAT_ID if CHAT_ID else 'Not set'}")
    print(f"✅ DATA_DIR: {DATA_DIR}")
    print("============================================================")

    # Строим Application для PTB21
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(app_post_init)
        .post_stop(app_post_stop)
        .build()
    )

    # Хэндлеры команд
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("add", cmd_add))

    # Хэндлеры профиля
    application.add_handler(CallbackQueryHandler(profile_select, pattern="^profile_"))

    # Диалог: новая сделка
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

    # Диалог: добавить актив в портфель
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

    # Кнопки главного меню
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons)
    )

    # Ошибки
    application.add_error_handler(on_error)

    # Готово. Дальше просто run_polling.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
