# BOT VERSION: 2025-11-01-STABLE-v6
# - Гибридное хранилище (Supabase + локально)
# - Безопасное завершение (graceful shutdown)
# - Умные алерты по ценам и таргет-профиту
# - Личные профили риска (long/swing/day)
# - Кеширование и троттлинг API
# - Хотфикс для python-telegram-bot под Python 3.13
# - /events теперь не заглушка: даёт недельный календарь рынков (акции + крипта)

import os
import math
import asyncio
import traceback
import aiohttp
from aiohttp import web
import json
import sys
import tempfile
import shutil
import signal
from typing import Dict, Any, Optional, Tuple, List
from datetime import time as dt_time, datetime, timedelta, timezone
from pathlib import Path

import telegram
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    Updater,  # нужен для хотфикса
)

# ========= ENV =========

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # чат куда летят общие алерты
LUNARCRUSH_API_KEY = os.getenv("LUNARCRUSH_API_KEY")  # пока не используется, но оставим
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")
if not CHAT_ID:
    print("⚠ CHAT_ID не установлен - общие алерты в общий канал не будут отправляться")

# ========= SUPABASE STORAGE =========

class SupabaseStorage:
    """Хранилище в Supabase (persist между рестартами/деплоями)"""

    def __init__(self, url: Optional[str], key: Optional[str]):
        self.url = url
        self.key = key
        self.session: Optional[aiohttp.ClientSession] = None
        self.headers = (
            {
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            if key
            else {}
        )
        self.enabled = bool(url and key)
        if self.enabled:
            print("✅ Supabase storage enabled")
        else:
            print("⚠️  Supabase storage disabled (no credentials)")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def load_portfolios(self) -> Dict[int, Dict[str, float]]:
        """user_id -> {ticker: amount}"""
        if not self.enabled:
            return {}
        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/portfolios?select=*"
            async with session.get(
                url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    portfolios: Dict[int, Dict[str, float]] = {}
                    for row in data:
                        try:
                            user_id = int(row["user_id"])
                            assets = row["assets"]
                            if isinstance(assets, dict):
                                portfolios[user_id] = assets
                        except (KeyError, ValueError, TypeError) as e:
                            print(f"⚠️ Invalid portfolio row: {e}")
                            continue
                    print(f"✅ Loaded {len(portfolios)} portfolios from Supabase")
                    return portfolios
                else:
                    body = await response.text()
                    print(f"⚠️ Supabase load portfolios: HTTP {response.status}")
                    print(f"   Response: {body[:200]}")
                    return {}
        except Exception as e:
            print(f"⚠️ Supabase load portfolios error: {e}")
            return {}

    async def save_portfolio(self, user_id: int, assets: Dict[str, float]):
        """асинхронная отправка, не блокирует основной поток"""
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
            async with session.post(
                url,
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status in [200, 201, 204]:
                    return
                else:
                    body = await response.text()
                    print(f"⚠️ Supabase save portfolio: HTTP {response.status}")
                    print(f"   Response: {body[:200]}")
        except Exception as e:
            print(f"⚠️ Supabase save portfolio error: {e}")

    async def load_trades(self) -> Dict[int, List[Dict[str, Any]]]:
        """user_id -> [trade, ...]"""
        if not self.enabled:
            return {}
        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/trades?select=*&order=created_at.desc"
            async with session.get(
                url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    trades_out: Dict[int, List[Dict[str, Any]]] = {}
                    for row in data:
                        try:
                            user_id = int(row["user_id"])
                            trades_out.setdefault(user_id, []).append(
                                {
                                    "id": row["id"],
                                    "symbol": row["symbol"],
                                    "amount": float(row["amount"]),
                                    "entry_price": float(row["entry_price"]),
                                    "target_profit_pct": float(
                                        row["target_profit_pct"]
                                    ),
                                    "notified": bool(row.get("notified", False)),
                                    "timestamp": row.get(
                                        "created_at", datetime.utcnow().isoformat()
                                    ),
                                }
                            )
                        except (KeyError, ValueError, TypeError) as e:
                            print(f"⚠️ Invalid trade row: {e}")
                            continue
                    total_trades = sum(len(t) for t in trades_out.values())
                    print(f"✅ Loaded {total_trades} trades from Supabase")
                    return trades_out
                else:
                    body = await response.text()
                    print(f"⚠️ Supabase load trades: HTTP {response.status}")
                    print(f"   Response: {body[:200]}")
                    return {}
        except Exception as e:
            print(f"⚠️ Supabase load trades error: {e}")
            return {}

    async def add_trade(
        self,
        user_id: int,
        symbol: str,
        amount: float,
        entry_price: float,
        target_profit_pct: float,
    ) -> bool:
        """записать сделку в Supabase (async fire-and-forget)"""
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
            async with session.post(
                url, headers=self.headers, json=data, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status in [200, 201, 204]:
                    return True
                else:
                    body = await response.text()
                    print(f"⚠️ Supabase add trade: HTTP {response.status}")
                    print(f"   Response: {body[:200]}")
                    return False
        except Exception as e:
            print(f"⚠️ Supabase add trade error: {e}")
            return False

    async def update_trade_notified(self, trade_id: int):
        """отметить что по сделке уже был отправлен алерт"""
        if not self.enabled:
            return
        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/trades?id=eq.{trade_id}"
            data = {"notified": True}
            async with session.patch(
                url, headers=self.headers, json=data, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status in [200, 204]:
                    return
                else:
                    body = await response.text()
                    print(f"⚠️ Supabase update trade: HTTP {response.status}")
                    print(f"   Response: {body[:200]}")
        except Exception as e:
            print(f"⚠️ Supabase update trade error: {e}")


supabase_storage = SupabaseStorage(SUPABASE_URL, SUPABASE_KEY)

# ========= ДИРЕКТОРИЯ ДАННЫХ / ЛОКАЛЬНЫЕ ФАЙЛЫ =========

def get_data_directory() -> Path:
    """
    Безопасно выбираем директорию для данных (Render может давать разные пути с разными правами).
    Проверяем, что реально можем писать.
    """
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

# ========= КОНФИГ ЗАПРОСОВ ВНЕШНИХ API =========

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# ========= ДОСТУПНЫЕ АКТИВЫ =========

AVAILABLE_TICKERS = {
    "VWCE.DE": {"name": "VWCE", "type": "stock"},
    "4GLD.DE": {"name": "4GLD (Gold ETC)", "type": "stock"},
    "DE000A2T5DZ1.SG": {"name": "X IE Physical Gold ETC", "type": "stock"},
    "SPY": {"name": "S&P 500 (SPY)", "type": "stock"},
}

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

THRESHOLDS = {
    "stocks": 1.0,   # % движения за период наблюдения для алерта
    "crypto": 4.0,   # % движения за период наблюдения для алерта
}

# ========= КЕШ ЦЕН =========

class PriceCache:
    """
    TTL-кеш цен.
    - уменьшает кол-во вызовов внешних API
    - переживает рестарты (сохраняет файл)
    - запоминает последние цены для алертов
    """

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.stats = {"api_calls": 0, "cache_hits": 0}
        self.load()

    def load(self):
        """поднять кеш из файла"""
        if not CACHE_FILE.exists():
            return
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                print("⚠️ Invalid cache format, resetting")
                return

            now_ts = datetime.now().timestamp()
            valid_entries = 0
            for k, v in data.items():
                if not isinstance(v, dict) or "timestamp" not in v or "data" not in v:
                    continue
                try:
                    ts_val = float(v["timestamp"])
                    if now_ts - ts_val < self.ttl * 2:
                        self.cache[k] = v
                        valid_entries += 1
                except (ValueError, TypeError):
                    continue

            print(f"✅ Loaded {valid_entries} valid prices from cache")
        except json.JSONDecodeError as e:
            print(f"⚠️ Cache JSON corrupted: {e}, resetting")
            self.cache = {}
        except Exception as e:
            print(f"⚠️ Cache load error: {e}")
            self.cache = {}

    def save(self):
        """атомарная запись кеша"""
        temp_file = CACHE_FILE.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                json.dump(self.cache, f, indent=2)
            shutil.move(str(temp_file), str(CACHE_FILE))
        except Exception as e:
            print(f"⚠️ Cache save error: {e}")
            try:
                temp_file.unlink(missing_ok=True)
            except:
                pass

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """вернуть данные если они ещё не протухли"""
        if key not in self.cache:
            return None
        entry = self.cache[key]
        if "timestamp" not in entry:
            del self.cache[key]
            return None
        try:
            age = datetime.now().timestamp() - float(entry["timestamp"])
        except (ValueError, TypeError):
            del self.cache[key]
            return None
        if age < self.ttl:
            self.stats["cache_hits"] += 1
            return entry.get("data")
        return None

    def set(self, key: str, data: Dict[str, Any]):
        """сохранить в кеш"""
        self.cache[key] = {
            "data": data,
            "timestamp": datetime.now().timestamp(),
        }
        self.stats["api_calls"] += 1
        # авто-сейв каждые 10 записей
        if len(self.cache) % 10 == 0:
            self.save()

    def get_for_alert(self, key: str) -> Optional[float]:
        """
        получить последнюю зафиксированную цену без TTL-проверки
        (нужно для %изменения между циклами алерта)
        """
        if key in self.cache:
            data = self.cache[key].get("data", {})
            price = data.get("price")
            if price is not None:
                try:
                    return float(price)
                except (ValueError, TypeError):
                    return None
        return None

    def set_for_alert(self, key: str, price: float):
        """запомнить цену для дальнейшего сравнения"""
        try:
            price = float(price)
            if math.isnan(price) or math.isinf(price):
                print(f"⚠️ Invalid price value for {key}: {price}")
                return
        except (ValueError, TypeError):
            print(f"⚠️ Cannot convert price to float for {key}: {price}")
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

# ========= РАНТАЙМ-ХРАНИЛИЩЕ В ПАМЯТИ =========

user_portfolios: Dict[int, Dict[str, float]] = {}
user_trades: Dict[int, List[Dict[str, Any]]] = {}
user_profiles: Dict[int, str] = {}  # user_id -> "long"/"swing"/"day"

# ========= LOAD / SAVE ДАННЫХ =========

def _safe_json_read(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"⚠️ JSON corrupted in {path}: {e}")
        return None
    except Exception as e:
        print(f"⚠️ Read error {path}: {e}")
        return None

def _atomic_json_write(path: Path, data: Any):
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        shutil.move(str(tmp), str(path))
    except Exception as e:
        print(f"⚠️ Write error {path}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except:
            pass

async def async_load_supabase_data():
    """
    Асинхронно забираем портфели и сделки из Supabase.
    Возвращаем их наружу (не пишем глобалы тут).
    """
    portfolios = await supabase_storage.load_portfolios()
    trades = await supabase_storage.load_trades()
    return portfolios, trades

def load_data():
    """
    Грузим данные на старте процесса.
    Логика:
    1. Пытаемся создать временный event loop и через него вытянуть Supabase (если он сконфигурен).
       Мы НЕ трогаем текущий running loop. Мы создаём свой, закрываем его.
    2. Если Supabase дал что-то осмысленное — берём это как truth.
    3. Если Supabase пустой или отключён — падаем в локальные файлы.
    """
    global user_portfolios, user_trades

    # 1. Supabase (если доступен)
    supabase_portfolios: Dict[int, Dict[str, float]] = {}
    supabase_trades: Dict[int, List[Dict[str, Any]]] = {}
    try:
        # создаём временный loop, чтоб не лезть в main loop приложения
        tmp_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(tmp_loop)
            supabase_portfolios, supabase_trades = tmp_loop.run_until_complete(
                async_load_supabase_data()
            )
        finally:
            # обязательно закрываем
            tmp_loop.run_until_complete(supabase_storage.close())
            tmp_loop.close()
            # снимаем event loop, чтобы не залип
            asyncio.set_event_loop(None)
    except Exception as e:
        print(f"⚠️ Supabase load error: {e}")
        print("   Will try local fallback...")

    if supabase_portfolios:
        user_portfolios = supabase_portfolios
        print(f"✅ Loaded {len(user_portfolios)} portfolios from Supabase")
    if supabase_trades:
        user_trades = supabase_trades
        total = sum(len(t) for t in user_trades.values())
        print(f"✅ Loaded {total} trades from Supabase")

    # 2. fallback на локальные файлы, если по какой-то причине данные пустые
    if not user_portfolios:
        raw_p = _safe_json_read(PORTFOLIO_FILE)
        if isinstance(raw_p, dict):
            restored = {}
            for k, v in raw_p.items():
                try:
                    uid = int(k)
                    if isinstance(v, dict):
                        restored[uid] = v
                except (ValueError, TypeError):
                    continue
            user_portfolios = restored
            print(f"✅ Loaded {len(user_portfolios)} portfolios from local file")

    if not user_trades:
        raw_t = _safe_json_read(TRADES_FILE)
        if isinstance(raw_t, dict):
            restored_t = {}
            for k, v in raw_t.items():
                try:
                    uid = int(k)
                    if isinstance(v, list):
                        restored_t[uid] = v
                except (ValueError, TypeError):
                    continue
            user_trades = restored_t
            print(f"✅ Loaded {len(user_trades)} trade lists from local file")

def save_portfolios_local_only():
    _atomic_json_write(PORTFOLIO_FILE, user_portfolios)

def save_trades_local_only():
    _atomic_json_write(TRADES_FILE, user_trades)

def save_portfolio(user_id: int, portfolio: Dict[str, float]):
    """
    ГИБРИДНОЕ СОХРАНЕНИЕ ПОРТФЕЛЯ
    - обновляем в памяти
    - пишем в локальный json (синхронно)
    - пушим в Supabase (асинхронно fire-and-forget в текущий running loop)
    """
    user_portfolios[user_id] = portfolio
    save_portfolios_local_only()

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(supabase_storage.save_portfolio(user_id, portfolio))
    except RuntimeError:
        # если вызывается до запуска основного лупа — просто молча пропустим async часть
        pass
    except Exception as e:
        print(f"⚠️ Supabase async save error: {e}")

def save_trades():
    """
    сохраняем user_trades в локалку.
    Supabase заливаем точечно через add_trade().
    """
    save_trades_local_only()

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

def get_user_trades(user_id: int) -> List[Dict[str, Any]]:
    if user_id not in user_trades:
        user_trades[user_id] = []
    return user_trades[user_id]

def add_trade(
    user_id: int, symbol: str, amount: float, entry_price: float, target_profit_pct: float
):
    """
    ГИБРИД:
    - добавляем сделку в память
    - сохраняем локально
    - пушим в Supabase асинхронно
    """
    trades = get_user_trades(user_id)
    trade = {
        "symbol": symbol,
        "amount": amount,
        "entry_price": entry_price,
        "target_profit_pct": target_profit_pct,
        "timestamp": datetime.now().isoformat(),
        "notified": False,
    }
    trades.append(trade)
    save_trades()
    print(f"✅ Added trade for user {user_id}: {symbol} x{amount} @ ${entry_price}")

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(
            supabase_storage.add_trade(
                user_id, symbol, amount, entry_price, target_profit_pct
            )
        )
    except RuntimeError:
        pass
    except Exception as e:
        print(f"⚠️ Supabase async add trade error: {e}")

def get_all_active_assets() -> Dict[str, List[int]]:
    """
    Собирает тикеры, которые реально держат пользователи
    (нужно для периодической проверки алертов,
     чтобы не дёргать по пустым активам)
    """
    active_assets: Dict[str, List[int]] = {}

    for uid, portfolio in user_portfolios.items():
        for ticker, qty in portfolio.items():
            try:
                if float(qty) > 0:
                    active_assets.setdefault(ticker, [])
                    if uid not in active_assets[ticker]:
                        active_assets[ticker].append(uid)
            except (ValueError, TypeError):
                continue

    for uid, trades in user_trades.items():
        for t in trades:
            sym = t.get("symbol")
            if not sym:
                continue
            active_assets.setdefault(sym, [])
            if uid not in active_assets[sym]:
                active_assets[sym].append(uid)

    return active_assets

# ========= ПОЛЬЗОВАТЕЛЬСКИЕ ПРОФИЛИ РИСКА =========

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

SELECT_CRYPTO, ENTER_AMOUNT, ENTER_PRICE, ENTER_TARGET = range(4)
SELECT_ASSET_TYPE, SELECT_ASSET, ENTER_ASSET_AMOUNT = range(4, 7)

def get_main_menu():
    keyboard = [
        [KeyboardButton("💼 Мой портфель"), KeyboardButton("💹 Все цены")],
        [KeyboardButton("🎯 Мои сделки"), KeyboardButton("📊 Рыночные сигналы")],
        [KeyboardButton("📰 События недели"), KeyboardButton("🔮 Прогнозы")],
        [KeyboardButton("➕ Добавить актив"), KeyboardButton("🆕 Новая сделка")],
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ========= HTTP HELPERS =========

async def get_json(
    session: aiohttp.ClientSession, url: str, params=None
) -> Optional[Dict[str, Any]]:
    try:
        async with session.get(
            url, params=params, headers=HEADERS, timeout=TIMEOUT
        ) as r:
            if r.status != 200:
                print(f"⚠ {url} -> HTTP {r.status}")
                return None
            return await r.json()
    except Exception as e:
        print(f"❌ get_json({url}) error: {e}")
        return None

# ========= ПОЛУЧЕНИЕ ЦЕН АКЦИЙ / ETF (Yahoo) =========

async def get_yahoo_price(
    session: aiohttp.ClientSession, ticker: str
) -> Optional[Tuple[float, str, float]]:
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

        if price is not None:
            try:
                price = float(price)
                change_pct = float(change_pct) if change_pct is not None else 0.0
                if not math.isnan(price) and not math.isinf(price):
                    return (price, cur, change_pct)
            except (ValueError, TypeError):
                pass
    except Exception as e:
        print(f"❌ Yahoo {ticker} error: {e}")
    return None

# ========= ПОЛУЧЕНИЕ ЦЕН КРИПТЫ =========

async def get_crypto_price_raw(
    session: aiohttp.ClientSession, symbol: str
) -> Optional[Dict[str, Any]]:
    """
    Пытаемся по цепочке:
    1) Binance
    2) CoinPaprika
    3) CoinGecko
    Возвращаем {"usd": ..., "change_24h": ..., "source": "..."}
    """
    crypto_info = CRYPTO_IDS.get(symbol)
    if not crypto_info:
        return None

    # BINANCE
    try:
        binance_symbol = crypto_info["binance"]
        url = "https://api.binance.com/api/v3/ticker/24hr"
        params = {"symbol": binance_symbol}

        print(f"🔍 Trying Binance for {symbol}...")
        async with session.get(
            url, params=params, timeout=TIMEOUT
        ) as response:
            print(f"   Binance response status: {response.status}")
            if response.status == 200:
                data = await response.json()
                price = float(data.get("lastPrice", 0))
                change_24h = float(data.get("priceChangePercent", 0))
                if price > 0 and not math.isnan(price) and not math.isinf(price):
                    print(
                        f"✅ {symbol} from Binance: ${price:,.2f} ({change_24h:+.2f}%)"
                    )
                    return {
                        "usd": price,
                        "change_24h": change_24h
                        if not math.isnan(change_24h)
                        else None,
                        "source": "Binance",
                    }
                else:
                    print(f"⚠️ Binance invalid price for {symbol}: {price}")
            else:
                if response.status == 429:
                    print(f"⚠️ Binance rate limit for {symbol}")
                elif response.status in (403, 418):
                    print(f"⚠️ Binance blocked/banned for {symbol}")
                else:
                    print(f"⚠️ Binance HTTP {response.status} for {symbol}")
    except asyncio.TimeoutError:
        print(f"⚠️ Binance timeout for {symbol}")
    except aiohttp.ClientError as e:
        print(f"⚠️ Binance connection error for {symbol}: {e}")
    except Exception as e:
        print(f"⚠️ Binance failed for {symbol}: {type(e).__name__}: {e}")

    # COINPAPRIKA
    try:
        paprika_id = crypto_info["paprika"]
        url = f"https://api.coinpaprika.com/v1/tickers/{paprika_id}"
        data = await get_json(session, url, None)

        if data:
            quotes = data.get("quotes", {}).get("USD", {})
            price = quotes.get("price")
            change_24h = quotes.get("percent_change_24h")
            if price:
                try:
                    price = float(price)
                    if price > 0 and not math.isnan(price):
                        print(f"✅ {symbol} from CoinPaprika: ${price:,.2f}")
                        return {
                            "usd": price,
                            "change_24h": float(change_24h)
                            if change_24h and not math.isnan(float(change_24h))
                            else None,
                            "source": "CoinPaprika",
                        }
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"⚠️ CoinPaprika failed for {symbol}: {e}")

    # COINGECKO
    try:
        coingecko_id = crypto_info["coingecko"]
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": coingecko_id,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        }
        data = await get_json(session, url, params)

        if data and coingecko_id in data:
            coin_data = data[coingecko_id]
            price = coin_data.get("usd")
            change_24h = coin_data.get("usd_24h_change")
            if price:
                try:
                    price = float(price)
                    if price > 0 and not math.isnan(price):
                        print(f"✅ {symbol} from CoinGecko: ${price:,.2f}")
                        return {
                            "usd": price,
                            "change_24h": float(change_24h)
                            if change_24h and not math.isnan(float(change_24h))
                            else None,
                            "source": "CoinGecko",
                        }
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"⚠️ CoinGecko failed for {symbol}: {e}")

    print(f"❌ All sources failed for {symbol}")
    return None

async def get_crypto_price(
    session: aiohttp.ClientSession, symbol: str, use_cache: bool = True
) -> Optional[Dict[str, Any]]:
    cache_key = f"crypto_{symbol}"
    if use_cache:
        cached = price_cache.get(cache_key)
        if cached:
            print(f"📦 {symbol} from cache: ${cached['usd']:,.2f}")
            return cached

    result = await get_crypto_price_raw(session, symbol)
    if result:
        price_cache.set(cache_key, result)
    return result

async def get_fear_greed_index(session: aiohttp.ClientSession) -> Optional[int]:
    """
    Индекс страха и жадности крипторынка.
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

# ========= СИГНАЛЫ ПО РЫНКУ =========

async def get_market_signal(
    session: aiohttp.ClientSession, symbol: str, investor_type: str
) -> Dict[str, Any]:
    """
    Возвращает {signal, emoji, reason} для символа (крипта)
    """
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
                "reason": f"Экстремальный страх ({fear_greed}/100). Отличная точка входа.",
            }
        elif fear_greed > 75:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Жадность ({fear_greed}/100). Держите позиции.",
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Стабильный рынок ({fear_greed}/100). Держать долгосрочно.",
            }

    elif investor_type == "swing":
        if fear_greed < 40:
            return {
                "signal": "BUY",
                "emoji": "🟢",
                "reason": f"Страх ({fear_greed}/100). Возможность войти на коррекции.",
            }
        elif fear_greed > 65:
            return {
                "signal": "SELL",
                "emoji": "🔴",
                "reason": f"Жадность ({fear_greed}/100). Зафиксировать прибыль.",
            }
        else:
            return {
                "signal": "HOLD",
                "emoji": "🟡",
                "reason": f"Нейтрально ({fear_greed}/100). Ждать лучшей точки.",
            }

    else:  # day trader
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
                "reason": f"Флэт ({fear_greed}/100). Ожидание сигнала.",
            }

# ========= АЛЕРТЫ =========

async def check_all_alerts(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодический джоб.
    Делает:
    - price alert для активов, которые кто-то держит
    - trade alert, когда достигнута цель прибыли
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
            price_alerts: List[str] = []
            trade_alerts: Dict[int, List[str]] = {}

            for asset, user_ids in active_assets.items():
                # Акции / ETF
                if asset in AVAILABLE_TICKERS:
                    price_data = await get_yahoo_price(session, asset)
                    if not price_data:
                        print(f"  ⚠️ {asset}: No price data available")
                        continue

                    price, currency, _change_pct = price_data
                    cache_key = f"alert_stock_{asset}"

                    old_price = price_cache.get_for_alert(cache_key)
                    if old_price and old_price > 0:
                        try:
                            change_pct = ((price - old_price) / old_price) * 100
                            print(
                                f"  {asset}: {old_price:.2f} -> {price:.2f} ({change_pct:+.2f}%)"
                            )
                            if abs(change_pct) >= THRESHOLDS["stocks"]:
                                name = AVAILABLE_TICKERS[asset]["name"]
                                emoji = "📈" if change_pct > 0 else "📉"
                                price_alerts.append(
                                    f"{emoji} <b>{name}</b>: {change_pct:+.2f}%\n"
                                    f"Цена: {price:.2f} {currency}"
                                )
                                print(
                                    f"  🚨 ALERT! {name} changed by {change_pct:+.2f}%"
                                )
                        except (ValueError, ZeroDivisionError) as e:
                            print(f"  ⚠️ {asset}: Calculation error - {e}")
                    else:
                        print(f"  {asset}: First check, storing price {price:.2f}")

                    price_cache.set_for_alert(cache_key, price)

                # Крипта
                elif asset in CRYPTO_IDS:
                    crypto_data = await get_crypto_price(
                        session, asset, use_cache=False
                    )
                    if not crypto_data:
                        print(f"  ⚠️ {asset}: No crypto data available")
                        continue

                    current_price = crypto_data["usd"]
                    cache_key = f"alert_crypto_{asset}"

                    # price alert
                    old_price = price_cache.get_for_alert(cache_key)
                    if old_price and old_price > 0:
                        try:
                            change_pct = (
                                (current_price - old_price) / old_price
                            ) * 100
                            print(
                                f"  {asset}: ${old_price:,.2f} -> ${current_price:,.2f} ({change_pct:+.2f}%)"
                            )
                            if abs(change_pct) >= THRESHOLDS["crypto"]:
                                emoji = "🚀" if change_pct > 0 else "⚠️"
                                price_alerts.append(
                                    f"{emoji} <b>{asset}</b>: {change_pct:+.2f}%\n"
                                    f"Цена: ${current_price:,.2f}"
                                )
                                print(
                                    f"  🚨 PRICE ALERT! {asset} changed by {change_pct:+.2f}%"
                                )
                        except (ValueError, ZeroDivisionError) as e:
                            print(f"  ⚠️ {asset}: Calculation error - {e}")
                    else:
                        print(
                            f"  {asset}: First check, storing price ${current_price:,.2f}"
                        )

                    price_cache.set_for_alert(cache_key, current_price)

                    # trade profit alerts
                    for uid in user_ids:
                        trades = get_user_trades(uid)
                        for trade in trades:
                            if trade.get("symbol") != asset or trade.get(
                                "notified", False
                            ):
                                continue
                            try:
                                entry_price = float(trade["entry_price"])
                                target = float(trade["target_profit_pct"])
                                amount = float(trade["amount"])
                                if entry_price <= 0:
                                    continue

                                profit_pct = (
                                    (current_price - entry_price)
                                    / entry_price
                                    * 100
                                )

                                print(
                                    f"  Trade check: {asset} for user {uid}: {profit_pct:.2f}% (target {target}%)"
                                )

                                if profit_pct >= target:
                                    value = amount * current_price
                                    profit_usd = amount * (
                                        current_price - entry_price
                                    )

                                    alert_text = (
                                        "🎯 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n\n"
                                        f"💰 {asset}\n"
                                        f"Количество: {amount:.4f}\n"
                                        f"Цена входа: ${entry_price:,.2f}\n"
                                        f"Текущая цена: ${current_price:,.2f}\n\n"
                                        f"📈 Прибыль: <b>{profit_pct:.2f}%</b> (${profit_usd:,.2f})\n"
                                        f"💵 Стоимость: ${value:,.2f}\n\n"
                                        f"✅ <b>Рекомендация: ПРОДАВАТЬ</b>"
                                    )

                                    trade_alerts.setdefault(uid, []).append(
                                        alert_text
                                    )
                                    trade["notified"] = True
                                    print(
                                        f"  🚨 PROFIT ALERT for user {uid}: {asset} +{profit_pct:.2f}%!"
                                    )
                            except (
                                ValueError,
                                TypeError,
                                KeyError,
                                ZeroDivisionError,
                            ) as e:
                                print(
                                    f"  ⚠️ Trade processing error for {asset}: {e}"
                                )
                                continue

                await asyncio.sleep(0.2)

            # локально фиксируем, что сделки были notified
            if trade_alerts:
                save_trades()

            # кеш цен тоже обновляем на диск
            price_cache.save()

            # price alerts -> общий канал
            if price_alerts:
                message = (
                    "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(price_alerts)
                )
                try:
                    await context.bot.send_message(
                        chat_id=CHAT_ID, text=message, parse_mode="HTML"
                    )
                    print(f"📤 Sent {len(price_alerts)} price alerts")
                except Exception as e:
                    print(f"⚠️ Failed to send price alerts to CHAT_ID: {e}")

            # trade alerts -> каждому юзеру отдельно в ЛС
            total_trade_alerts = sum(len(v) for v in trade_alerts.values())
            for uid, alerts_list in trade_alerts.items():
                for alert in alerts_list:
                    try:
                        await context.bot.send_message(
                            chat_id=str(uid),
                            text=alert,
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        print(
                            f"⚠️ Failed to send alert to user {uid}: {e}"
                        )

            if total_trade_alerts:
                print(
                    f"📤 Sent {total_trade_alerts} trade alerts to {len(trade_alerts)} users"
                )

            cache_stats = price_cache.get_stats()
            print(f"📊 Cache stats: {cache_stats}")

            print(
                "✅ Alerts check complete. Active assets: "
                f"{len(active_assets)}, Price alerts: {len(price_alerts)}, "
                f"Trade alerts: {total_trade_alerts}"
            )

            price_cache.reset_stats()

    except Exception as e:
        print(f"❌ check_all_alerts error: {e}")
        traceback.print_exc()

# ========= HANDLERS =========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_profiles:
        user_profiles[uid] = "long"

    await update.message.reply_text(
        "👋 <b>Trading Bot v6 (stable)</b>\n\n"
        "<b>Что я умею:</b>\n"
        "• 💼 Портфель (акции + крипта)\n"
        "• 🎯 Сделки с целевой прибылью\n"
        "• 📊 Рыночные сигналы BUY/HOLD/SELL под твой стиль\n"
        "• 🔔 Алерты по цене и по целям прибыли\n"
        "• 📰 Экономические события недели (акции + крипта)\n\n"
        "<b>Текущие улучшения:</b>\n"
        "• Защита от деления на ноль\n"
        "• Атомарная запись файлов\n"
        "• Персистентное хранилище (Supabase + локально)\n"
        "• Оптимизированные запросы к API\n"
        "• Исправлен python-telegram-bot под Python 3.13\n\n"
        "Жми кнопки ниже 👇",
        parse_mode="HTML",
        reply_markup=get_main_menu(),
    )

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    portfolio = get_user_portfolio(uid)

    if not portfolio or all(v == 0 for v in portfolio.values()):
        await update.message.reply_text(
            "💼 Ваш портфель пуст!\n\n"
            "Используйте <b>➕ Добавить актив</b>",
            parse_mode="HTML",
        )
        return

    try:
        lines = ["💼 <b>Ваш портфель:</b>\n"]
        total_value_usd = 0

        async with aiohttp.ClientSession() as session:
            # акции / ETF
            stock_items = [
                (k, v) for k, v in portfolio.items() if k in AVAILABLE_TICKERS
            ]
            if stock_items and any(v > 0 for _, v in stock_items):
                lines.append("<b>📊 Акции/ETF:</b>")
                lines.append("<pre>")
                lines.append("Актив          Кол-во    Цена        Сумма")
                lines.append("─" * 50)

                for ticker, qty in stock_items:
                    if qty == 0:
                        continue
                    price_data = await get_yahoo_price(session, ticker)
                    if price_data:
                        price, cur, _chg = price_data
                        value = price * qty

                        name = AVAILABLE_TICKERS[ticker]["name"][:14].ljust(14)
                        qty_str = f"{qty:.2f}".rjust(8)
                        price_str = f"{price:.2f}".rjust(8)
                        value_str = f"{value:.2f} {cur}".rjust(12)

                        lines.append(
                            f"{name} {qty_str} {price_str} {value_str}"
                        )

                        # грубо считаем всё в USD
                        if cur == "USD":
                            total_value_usd += value
                        elif cur == "EUR":
                            total_value_usd += value * 1.1
                    await asyncio.sleep(0.3)

                lines.append("</pre>")

            # крипта
            crypto_items = [
                (k, v) for k, v in portfolio.items() if k in CRYPTO_IDS
            ]
            if crypto_items and any(v > 0 for _, v in crypto_items):
                lines.append("\n<b>₿ Криптовалюты:</b>")
                lines.append("<pre>")
                lines.append("Монета    Кол-во      Цена          Сумма")
                lines.append("─" * 50)

                for symbol, qty in crypto_items:
                    if qty == 0:
                        continue

                    crypto_data = await get_crypto_price(session, symbol)
                    if crypto_data:
                        p = crypto_data["usd"]
                        chg = crypto_data.get("change_24h")
                        value = p * qty
                        total_value_usd += value

                        sym_str = symbol.ljust(9)
                        qty_str = f"{qty:.4f}".rjust(10)
                        price_str = f"${p:,.2f}".rjust(12)
                        value_str = f"${value:,.2f}".rjust(12)
                        chg_emoji = (
                            "📈" if chg and chg >= 0 else "📉" if chg else ""
                        )
                        lines.append(
                            f"{sym_str} {qty_str} {price_str} {value_str} {chg_emoji}"
                        )
                    await asyncio.sleep(0.2)

                lines.append("</pre>")

        if total_value_usd > 0:
            lines.append(
                f"\n<b>💰 Общая стоимость: ~${total_value_usd:,.2f}</b>"
            )

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML"
        )
    except Exception as e:
        print(f"❌ portfolio error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_all_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Riga time: сейчас зима => UTC+2
        riga_tz = timezone(timedelta(hours=2))
        now = datetime.now(riga_tz)
        timestamp = now.strftime("%H:%M:%S %d.%m.%Y")

        lines = [
            "💹 <b>Все цены</b>\n",
            f"🕐 Данные: <b>{timestamp}</b> (Рига)\n",
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
                    name = info["name"][:16].ljust(16)
                    price_str = f"{price:.2f} {cur}".ljust(10)
                    if change_pct != 0:
                        chg_emoji = "↗" if change_pct >= 0 else "↘"
                        chg_str = f"{chg_emoji}{abs(change_pct):.1f}%".rjust(7)
                    else:
                        chg_str = "0.0%".rjust(7)
                    lines.append(
                        f"│ {name} │ {price_str} │ {chg_str} │"
                    )
                else:
                    name = info["name"][:16].ljust(16)
                    lines.append(
                        f"│ {name} │ {'н/д'.ljust(10)} │ {'N/A'.rjust(7)} │"
                    )
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
                        p = crypto_data["usd"]
                        chg = crypto_data.get("change_24h")
                        source = crypto_data.get("source", "Unknown")[:8]

                        sym_str = symbol.ljust(6)
                        price_str = f"${p:,.2f}".ljust(12)
                        if chg is not None and not math.isnan(chg):
                            chg_emoji = "↗" if chg >= 0 else "↘"
                            chg_str = f"{chg_emoji}{abs(chg):.1f}%".rjust(7)
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

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML"
        )

    except Exception as e:
        print(f"❌ all_prices error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_my_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    trades = get_user_trades(uid)

    if not trades:
        await update.message.reply_text(
            "🎯 У вас нет открытых сделок\n\n"
            "Используйте <b>🆕 Новая сделка</b>",
            parse_mode="HTML",
        )
        return

    try:
        await update.message.reply_text("🔄 Обновляю данные...")

        lines = ["🎯 <b>Ваши сделки:</b>\n"]

        async with aiohttp.ClientSession() as session:
            total_value = 0.0
            total_profit = 0.0

            for i, trade in enumerate(trades, 1):
                try:
                    symbol = trade["symbol"]
                    entry_price = float(trade["entry_price"])
                    amount = float(trade["amount"])
                    target = float(trade["target_profit_pct"])
                except (KeyError, ValueError, TypeError) as e:
                    print(f"⚠️ Invalid trade data: {e}")
                    continue

                crypto_data = await get_crypto_price(session, symbol)
                if crypto_data:
                    current_price = crypto_data["usd"]
                    if entry_price > 0:
                        profit_pct = (
                            (current_price - entry_price)
                            / entry_price
                            * 100
                        )
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
                        lines.append(
                            f"├ Вход: ${entry_price:,.2f} → Сейчас: ${current_price:,.2f}"
                        )
                        lines.append(
                            f"├ Прибыль: <b>{profit_pct:+.2f}%</b> (${profit_usd:+,.2f})"
                        )
                        lines.append(
                            f"├ Цель: {target}% {'✅' if profit_pct >= target else '⏳'}"
                        )
                        lines.append(
                            f"└ Стоимость: ${value_now:,.2f}\n"
                        )

                await asyncio.sleep(0.2)

            if total_value > 0:
                initial_val = total_value - total_profit
                if initial_val > 0:
                    total_profit_pct = (total_profit / initial_val) * 100
                    lines.append("━━━━━━━━━━━━━━━━")
                    lines.append(
                        f"💰 <b>Общая стоимость: ${total_value:,.2f}</b>"
                    )
                    lines.append(
                        f"📊 <b>Общая прибыль: {total_profit_pct:+.2f}% "
                        f"(${total_profit:+,.2f})</b>"
                    )

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML"
        )

    except Exception as e:
        print(f"❌ my_trades error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_market_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    investor_type = user_profiles.get(uid, "long")
    profile_info = INVESTOR_TYPES[investor_type]

    await update.message.reply_text(
        f"🔄 Анализирую рынок для {profile_info['emoji']} {profile_info['name']}..."
    )

    try:
        lines = [
            "📊 <b>Рыночные сигналы</b>\n",
            f"Профиль: {profile_info['emoji']} <b>{profile_info['name']}</b>\n",
        ]

        async with aiohttp.ClientSession() as session:
            fear_greed = await get_fear_greed_index(session)
            if fear_greed is not None:
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

                lines.append(
                    f"📈 Fear & Greed: <b>{fear_greed}/100</b> ({fg_status})\n"
                )

            for symbol in ["BTC", "ETH", "SOL", "AVAX"]:
                sig = await get_market_signal(session, symbol, investor_type)
                lines.append(f"{sig['emoji']} <b>{symbol}: {sig['signal']}</b>")
                lines.append(f"   └ {sig['reason']}\n")
                await asyncio.sleep(0.2)

        lines.append("\n<i>⚠️ Не является финансовой рекомендацией</i>")

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML"
        )
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
    current_info = INVESTOR_TYPES[current_type]

    await update.message.reply_text(
        "👤 <b>Ваш профиль</b>\n\n"
        f"Текущий: {current_info['emoji']} <b>{current_info['name']}</b>\n"
        f"<i>{current_info['desc']}</i>\n\n"
        "Выберите тип для персонализированных сигналов:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )

async def profile_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    investor_type = query.data.replace("profile_", "")
    uid = query.from_user.id
    user_profiles[uid] = investor_type

    t = INVESTOR_TYPES[investor_type]
    await query.edit_message_text(
        "✅ <b>Профиль обновлён!</b>\n\n"
        f"{t['emoji']} <b>{t['name']}</b>\n"
        f"<i>{t['desc']}</i>\n\n"
        "Теперь рыночные сигналы адаптированы под ваш стиль!",
        parse_mode="HTML",
    )

# ========= СОБЫТИЯ НЕДЕЛИ =========
#
# Делаем оффлайн-расписание ключевых событий,
# которые обычно волнуют рынок: макро/ставки, отчётность фондового рынка,
# крипто-триггеры. Это не live feed, это структура.
#
# Логика:
# - Берём текущую дату (по Риге) и строим список событий на 7 дней вперёд.
# - Показываем таблицами.

def _get_week_window_riga(days_ahead: int = 7):
    riga_tz = timezone(timedelta(hours=2))
    start = datetime.now(riga_tz).date()
    end = start + timedelta(days=days_ahead)
    return start, end

def _format_date(d: datetime.date) -> str:
    # dd.mm
    return f"{d.day:02d}.{d.month:02d}"

def _generate_weekly_events():
    """
    Возвращает словарь категорий:
    {
      "macro": [ {date, title, impact, note}, ...],
      "equity": [...],
      "crypto": [...],
    }

    impact: "Высокий", "Средний", ...
    """

    # сейчас просто примеры типовых штук, которые реально двигают рынок.
    # ты потом можешь их менять руками под реальную неделю.
    macro_templates = [
        {
            "title": "FOMC / Решение по ставке",
            "impact": "Критический",
            "note": "Любой намёк на повышение ставки → давление на акции и крипту",
        },
        {
            "title": "Данные по инфляции (CPI)",
            "impact": "Высокий",
            "note": "Инфляция выше прогноза → риск ужесточения политики ФРС",
        },
        {
            "title": "Отчёт по безработице (Nonfarm Payrolls)",
            "impact": "Высокий",
            "note": "Сильный рынок труда → ФРС может быть жёстче",
        },
    ]

    equity_templates = [
        {
            "title": "Отчёт Apple / Big Tech Earnings",
            "impact": "Высокий",
            "note": "Сильная выручка → поддержка индексов SPY / QQQ",
        },
        {
            "title": "Отчёт крупных банков США",
            "impact": "Средний",
            "note": "Сентимент по экономике и кредитованию",
        },
    ]

    crypto_templates = [
        {
            "title": "Халвинг / редукция эмиссии",
            "impact": "Критический",
            "note": "Дефицит предложения BTC → бычий нарратив",
        },
        {
            "title": "ETF по BTC/ETH (регуляторные решения)",
            "impact": "Высокий",
            "note": "Одобрение → прилив институциональных денег",
        },
        {
            "title": "Сетевой апгрейд L2 / снижение комиссий",
            "impact": "Средний",
            "note": "Улучшение юзкейсов → интерес к экосистеме",
        },
    ]

    # мы распределим эти шаблоны по дням недели просто по порядку
    start_date, end_date = _get_week_window_riga()

    day_list = []
    d = start_date
    while d <= end_date:
        day_list.append(d)
        d += timedelta(days=1)

    macro_events = []
    equity_events = []
    crypto_events = []

    # раскладываем по дням циклически
    for idx, day in enumerate(day_list):
        if idx < len(macro_templates):
            m = macro_templates[idx]
            macro_events.append(
                {
                    "date": day,
                    "title": m["title"],
                    "impact": m["impact"],
                    "note": m["note"],
                }
            )

        if idx < len(equity_templates):
            e = equity_templates[idx]
            equity_events.append(
                {
                    "date": day,
                    "title": e["title"],
                    "impact": e["impact"],
                    "note": e["note"],
                }
            )

        if idx < len(crypto_templates):
            c = crypto_templates[idx]
            crypto_events.append(
                {
                    "date": day,
                    "title": c["title"],
                    "impact": c["impact"],
                    "note": c["note"],
                }
            )

    return {
        "macro": macro_events,
        "equity": equity_events,
        "crypto": crypto_events,
    }

def _format_events_table(title: str, rows: List[Dict[str, Any]]) -> List[str]:
    """
    Рисуем ASCII табличку:
    Дата | Событие | Импакт
    + комментарий (note) отдельной строкой
    """
    out: List[str] = []
    out.append(f"<b>{title}:</b>")
    out.append("<pre>")
    out.append("┌────────┬─────────────────────────────┬────────────┐")
    out.append("│ Дата   │ Событие                     │ Импакт     │")
    out.append("├────────┼─────────────────────────────┼────────────┤")
    for r in rows:
        date_str = _format_date(r["date"]).ljust(6)
        event_name = r["title"][:27].ljust(27)
        impact = r["impact"][:10].ljust(10)
        out.append(
            f"│ {date_str} │ {event_name} │ {impact} │"
        )
        # коммент под строкой
        note_lines = []
        note = r["note"]
        # лёгкий wrap на 41 символ
        while len(note) > 41:
            note_lines.append(note[:41])
            note = note[41:]
        note_lines.append(note)
        for nl in note_lines:
            out.append(f"│        │ {nl.ljust(27)} │            │")
    out.append("└────────┴─────────────────────────────┴────────────┘")
    out.append("</pre>")
    return out

async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Выдаёт календарь ближайших рыночных триггеров на ~неделю:
    - макро / ставки / инфляция
    - отчётность компаний (важно для фондового рынка)
    - крипто-драйверы (ETF, халвинг, апгрейды сетей)
    Это оффлайн структура, которую можно потом кастомизировать.
    """
    try:
        events = _generate_weekly_events()

        lines: List[str] = []
        lines.append("📰 <b>События недели</b>\n")
        lines.append(
            "Это ключевые вещи, на которые обычно смотрят фонды и крипта.\n"
        )

        if events["macro"]:
            lines += _format_events_table("📊 Макро / ставки / инфляция", events["macro"])
            lines.append("")

        if events["equity"]:
            lines += _format_events_table("📈 Отчётность и фондовый рынок", events["equity"])
            lines.append("")

        if events["crypto"]:
            lines += _format_events_table("₿ Крипто-триггеры", events["crypto"])
            lines.append("")

        lines.append(
            "<i>Примечание: это ориентиры. Это не финансовая рекомендация.</i>"
        )

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML"
        )

    except Exception as e:
        print(f"❌ events error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка")

async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔮 <b>Прогнозы</b>\n\n"
        "Используй 📊 <b>Рыночные сигналы</b> — они адаптируются под твой профиль риска.\n"
        "Я НЕ даю индивидуальный финансовый совет.\n",
        parse_mode="HTML",
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add TICKER QTY
    """
    if len(context.args) != 2:
        await update.message.reply_text(
            "❌ Формат: <code>/add TICKER КОЛИЧЕСТВО</code>",
            parse_mode="HTML",
        )
        return

    ticker = context.args[0].upper()
    try:
        quantity = float(context.args[1].replace(",", "."))
        if quantity <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Количество должно быть > 0")
        return

    if ticker not in AVAILABLE_TICKERS and ticker not in CRYPTO_IDS:
        await update.message.reply_text(
            "❌ Неизвестный тикер: "
            f"{ticker}\n\n"
            "Доступные: VWCE.DE, 4GLD.DE, DE000A2T5DZ1.SG, SPY, BTC, ETH, SOL, AVAX, DOGE, LINK"
        )
        return

    uid = update.effective_user.id
    pf = get_user_portfolio(uid)
    pf[ticker] = pf.get(ticker, 0) + quantity
    save_portfolio(uid, pf)

    name = (
        AVAILABLE_TICKERS.get(ticker, {}).get("name")
        or CRYPTO_IDS.get(ticker, {}).get("name")
        or ticker
    )

    await update.message.reply_text(
        f"✅ Добавлено: <b>{quantity} {name}</b>\n"
        f"Теперь у вас: {pf[ticker]:.4f}",
        parse_mode="HTML",
    )

# ====== Мультишаговое добавление актива через кнопки ======

async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "📊 Акции / ETF", callback_data="asset_stocks"
            )
        ],
        [
            InlineKeyboardButton(
                "₿ Криптовалюты", callback_data="asset_crypto"
            )
        ],
    ]
    await update.message.reply_text(
        "➕ <b>Добавить актив</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_ASSET_TYPE

async def add_asset_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    asset_type = query.data.replace("asset_", "")
    context.user_data["asset_type"] = asset_type

    keyboard = []
    if asset_type == "stocks":
        context.user_data["asset_category"] = "stocks"
        for ticker, info in AVAILABLE_TICKERS.items():
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{info['name']} ({ticker})",
                        callback_data=f"addticker_{ticker}",
                    )
                ]
            )
    else:
        context.user_data["asset_category"] = "crypto"
        for symbol, info in CRYPTO_IDS.items():
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{info['name']} ({symbol})",
                        callback_data=f"addcrypto_{symbol}",
                    )
                ]
            )

    type_emoji = "📊" if asset_type == "stocks" else "₿"
    type_name = (
        "Акции / ETF" if asset_type == "stocks" else "Криптовалюты"
    )
    await query.edit_message_text(
        f"{type_emoji} <b>{type_name}</b>\n\nВыберите актив:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_ASSET

async def add_asset_select_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("addticker_"):
        ticker = query.data.replace("addticker_", "")
        context.user_data["selected_asset"] = ticker
        name = AVAILABLE_TICKERS[ticker]["name"]
        emoji = "📊"
    else:
        symbol = query.data.replace("addcrypto_", "")
        context.user_data["selected_asset"] = symbol
        name = CRYPTO_IDS[symbol]["name"]
        emoji = "₿"

    await query.edit_message_text(
        f"✅ Выбрано: {emoji} <b>{name}</b>\n\n"
        f"Введите количество:",
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
        asset_category = context.user_data["asset_category"]

        if asset_category == "stocks":
            name = AVAILABLE_TICKERS[asset]["name"]
            emoji = "📊"
        else:
            name = CRYPTO_IDS[asset]["name"]
            emoji = "₿"

        pf = get_user_portfolio(uid)
        old_amount = pf.get(asset, 0)
        pf[asset] = old_amount + amount
        save_portfolio(uid, pf)

        await update.message.reply_text(
            "✅ <b>Добавлено!</b>\n\n"
            f"{emoji} <b>{name}</b>\n"
            f"Добавлено: {amount:.4f}\n"
            f"Было: {old_amount:.4f}\n"
            f"Стало: {pf[asset]:.4f}",
            parse_mode="HTML",
            reply_markup=get_main_menu(),
        )
        context.user_data.clear()
        return ConversationHandler.END
    except:
        await update.message.reply_text(
            "❌ Введите число\nНапример: <code>10</code> или <code>0.5</code>",
            parse_mode="HTML",
        )
        return ENTER_ASSET_AMOUNT

async def add_asset_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Отменено", reply_markup=get_main_menu()
    )
    context.user_data.clear()
    return ConversationHandler.END

# ====== Мультишаговая новая сделка ======

async def cmd_new_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for symbol in CRYPTO_IDS.keys():
        name = CRYPTO_IDS[symbol]["name"]
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{name} ({symbol})", callback_data=f"trade_{symbol}"
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
    query = update.callback_query
    await query.answer()

    symbol = query.data.replace("trade_", "")
    context.user_data["trade_symbol"] = symbol

    await query.edit_message_text(
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
            crypto_data = await get_crypto_price(
                session, symbol, use_cache=False
            )

        if crypto_data:
            current_price = crypto_data["usd"]
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
    except:
        await update.message.reply_text("❌ Введите число")
        return ENTER_AMOUNT

async def trade_enter_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # обработка кнопки
    if update.callback_query:
        query = update.callback_query
        await query.answer()

        if query.data == "price_continue":
            price = context.user_data.get("trade_price")
            await query.edit_message_text(
                f"✅ Цена: <b>${price:,.4f}</b>\n\n"
                f"Введите целевую прибыль (%):",
                parse_mode="HTML",
            )
            return ENTER_TARGET

    # обработка ручного ввода
    try:
        price = float(update.message.text.replace(",", ""))
        if price <= 0:
            raise ValueError()
        context.user_data["trade_price"] = price

        await update.message.reply_text(
            f"✅ Цена: <b>${price:,.4f}</b>\n\n"
            f"Введите целевую прибыль (%):",
            parse_mode="HTML",
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

        uid = update.effective_user.id
        symbol = context.user_data["trade_symbol"]
        amount = context.user_data["trade_amount"]
        price = context.user_data["trade_price"]

        add_trade(uid, symbol, amount, price, target)

        await update.message.reply_text(
            "✅ <b>Сделка добавлена!</b>\n\n"
            f"💰 {symbol}\n"
            f"Количество: {amount:.4f}\n"
            f"Цена: ${price:,.2f}\n"
            f"Цель: +{target}%",
            parse_mode="HTML",
            reply_markup=get_main_menu(),
        )

        context.user_data.clear()
        return ConversationHandler.END
    except:
        await update.message.reply_text("❌ Введите число")
        return ENTER_TARGET

async def trade_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Отменено", reply_markup=get_main_menu()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Помощь - Trading Bot v6</b>\n\n"
        "<b>Функции:</b>\n"
        "• /add TICKER КОЛ-ВО\n"
        "• 💼 Мой портфель\n"
        "• 🎯 Мои сделки\n"
        "• 📊 Рыночные сигналы\n"
        "• 📰 События недели\n"
        "• 👤 Мой профиль\n\n"
        "<b>Алерты:</b>\n"
        "• Резкие движения цены\n"
        "• Достижение твоей целевой прибыли по сделке\n\n"
        "<b>Тех:</b>\n"
        "• Персистентное хранилище\n"
        "• Кеширование\n"
        "• Graceful shutdown\n"
        "• Хотфикс PTB под Python 3.13\n",
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

# ========= HEALTH CHECK HTTP SERVER =========

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"✅ Health check server running on port {port}")
    return runner

# ========= HOTFIX ДЛЯ python-telegram-bot на Python 3.13 =========
#
# Баг:
#   В PTB 20.x есть класс Updater с __slots__.
#   На Python 3.13 внутри .build() пытаются проставить приватный атрибут
#   _Updater__polling_cleanup_cb, которого нет в __slots__ → падение.
#
# Решение:
#   ДО того как Application.builder().build() создаст Updater,
#   мы расширяем Updater.__slots__ этим именем.

# === PATCH for PTB 20.x + Python 3.13 ===
import inspect
import telegram.ext as ext

def monkeypatch_updater_slots():
    """
    Расширяет класс Updater, совместимый с Python 3.13,
    создавая подкласс с нужным слотом и подменяя ext.Updater.
    """
    try:
        Upd = ext.Updater

        # Проверяем, что поле действительно отсутствует
        if "_Updater__polling_cleanup_cb" in getattr(Upd, "__slots__", ()):
            print("🐒 PTB hotfix: slot already present, nothing to patch")
            return

        print("🐒 Rebuilding Updater class for Python 3.13 compatibility...")

        class FixedUpdater(Upd):  # type: ignore
            __slots__ = getattr(Upd, "__slots__", ()) + ("_Updater__polling_cleanup_cb",)

            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                # инициализация нового поля, чтобы не было AttributeError
                object.__setattr__(self, "_Updater__polling_cleanup_cb", None)

        # подменяем глобал в telegram.ext
        ext.Updater = FixedUpdater
        print("✅ PTB Updater successfully patched for Python 3.13")

    except Exception as e:
        print(f"⚠️ PTB hotfix failed: {e}")
        import traceback; traceback.print_exc()


# ========= MAIN RUNTIME =========

def main():
    # поднимем данные перед стартом
    load_data()

    print("=" * 60)
    print("🚀 Starting Trading Bot v6 (stable)")
    print("=" * 60)
    print(f"Python version: {sys.version}")
    print(f"Telegram bot version: {telegram.__version__}")
    print("=" * 60)
    print("✅ Core features:")
    print("  • Persistent storage (Supabase + local)")
    print("  • Smart caching / rate limiting")
    print("  • Alerts (price / take-profit)")
    print("  • Graceful shutdown")
    print("  • Weekly market events")
    print("  • PTB hotfix for Python 3.13")
    print("=" * 60)
    print(f"✅ BOT_TOKEN: {TOKEN[:10]}...")
    print(f"✅ CHAT_ID: {CHAT_ID if CHAT_ID else 'Not set'}")
    print(f"✅ DATA_DIR: {DATA_DIR}")
    print("🔧 Setting up signal handlers...")

    # обязательно пропатчить Updater до билда Application
    monkeypatch_updater_slots()

    print("🔧 Building Telegram Application...")
    try:
        app = Application.builder().token(TOKEN).build()
        print("✅ Application built successfully")
    except Exception as e:
        print(f"❌ FATAL: Failed to build application: {e}")
        sys.exit(1)

    print("🔧 Registering handlers...")

    # диалоги (ConversationHandler) для сделок и активов
    trade_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^🆕 Новая сделка$"), cmd_new_trade
            )
        ],
        states={
            SELECT_CRYPTO: [
                CallbackQueryHandler(
                    trade_select_crypto, pattern="^trade_"
                )
            ],
            ENTER_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, trade_enter_amount
                )
            ],
            ENTER_PRICE: [
                CallbackQueryHandler(
                    trade_enter_price, pattern="^price_"
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, trade_enter_price
                ),
            ],
            ENTER_TARGET: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, trade_enter_target
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", trade_cancel)],
    )

    add_asset_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^➕ Добавить актив$"), cmd_add_asset
            )
        ],
        states={
            SELECT_ASSET_TYPE: [
                CallbackQueryHandler(
                    add_asset_select_type, pattern="^asset_"
                )
            ],
            SELECT_ASSET: [
                CallbackQueryHandler(
                    add_asset_select_item, pattern="^add(ticker|crypto)_"
                )
            ],
            ENTER_ASSET_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_asset_enter_amount,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", add_asset_cancel)],
    )

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("help", cmd_help))

    # диалоги
    app.add_handler(trade_conv)
    app.add_handler(add_asset_conv)
    app.add_handler(
        CallbackQueryHandler(profile_select, pattern="^profile_")
    )

    # кнопки главного меню
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons)
    )

    # ошибки
    app.add_error_handler(on_error)

    print("✅ All handlers registered")

    # джоба алертов
    job_queue = app.job_queue
    if job_queue and CHAT_ID:
        print("🔧 Setting up alerts job...")
        # каждые 10 минут
        job_queue.run_repeating(
            check_all_alerts, interval=600, first=60
        )
        print("✅ UNIFIED alerts (price + trade): ENABLED")
    else:
        if not CHAT_ID:
            print("⚠️  Alerts DISABLED (CHAT_ID not set)")
        else:
            print("⚠️  Alerts DISABLED (job_queue not available)")

    print("=" * 60)
    print("🔄 Starting bot polling...")
    print("=" * 60)

    async def run_bot_with_health():
        """
        Запускаем:
        - aiohttp health-check сервер
        - Telegram-поллинг
        - graceful shutdown через сигнал
        """
        health_runner = await start_health_server()

        shutdown_event = asyncio.Event()

        def signal_handler_inner(sig, frame):
            print(f"\n⚠️  Received signal {sig}, initiating shutdown...")
            asyncio.get_event_loop().call_soon_threadsafe(
                shutdown_event.set
            )

        signal.signal(signal.SIGINT, signal_handler_inner)
        signal.signal(signal.SIGTERM, signal_handler_inner)

        try:
            async with app:
                await app.start()
                await app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
                print("✅ Bot polling started successfully")
                print("Press Ctrl+C to stop gracefully...")

                await shutdown_event.wait()

        finally:
            print("🛑 Stopping bot...")

            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                    print("  ✅ Updater stopped")
            except Exception as e:
                print(f"  ⚠️ Error stopping updater: {e}")

            try:
                if app.running:
                    await app.stop()
                    print("  ✅ Application stopped")
            except Exception as e:
                print(f"  ⚠️ Error stopping application: {e}")

            print("🛑 Stopping health server...")
            try:
                await health_runner.cleanup()
                print("  ✅ Health server stopped")
            except Exception as e:
                print(f"  ⚠️ Error stopping health server: {e}")

            print("💾 Saving final state...")
            try:
                price_cache.save()
                save_portfolios_local_only()
                save_trades()
                print("  ✅ Data saved")
            except Exception as e:
                print(f"  ⚠️ Error saving data: {e}")

            try:
                await supabase_storage.close()
                print("  ✅ Supabase session closed")
            except Exception as e:
                print(f"  ⚠️ Error closing Supabase: {e}")

            print("👋 Bot stopped gracefully")

    try:
        asyncio.run(run_bot_with_health())
    except KeyboardInterrupt:
        print("\n⚠️  Keyboard interrupt received")
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
