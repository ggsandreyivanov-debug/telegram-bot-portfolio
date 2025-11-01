# BOT VERSION: 2025-10-31-OPTIMIZED-v5-FIXED-FULL
# ИСПРАВЛЕНИЯ В ЭТОЙ ВЕРСИИ:
# - Убрано дублирование save_portfolio(), осталась гибридная версия (локально + Supabase)
# - Безопасная загрузка данных из Supabase без set_event_loop()
# - Правильная работа с таймзоной (Europe/Riga через zoneinfo)
# - Защита от division by zero
# - Правильная обработка None значений
# - Атомарная запись файлов
# - Валидация JSON при загрузке
# - Удалены hardcoded credentials
# - Graceful shutdown
# - Исправлена логика trade alerts
# - Добавлена обработка ошибок импорта
# - Добавлены расширенные логи при обращении к внешним API

import os
import sys
import math
import json
import asyncio
import traceback
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta, timezone as dt_timezone, time as dt_time
from zoneinfo import ZoneInfo  # для корректного времени в Риге

import aiohttp
from aiohttp import web

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
)

# =========================
# === SUPABASE STORAGE ====
# =========================

class SupabaseStorage:
    """Работа с Supabase для сохранения данных между деплоями"""

    def __init__(self, url: Optional[str], key: Optional[str]):
        self.url = url
        self.key = key
        self.session: Optional[aiohttp.ClientSession] = None
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        } if key else {}
        self.enabled = bool(url and key)
        if self.enabled:
            print("✅ Supabase storage enabled")
        else:
            print("⚠️  Supabase storage disabled (no credentials)")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Переиспользуемая aiohttp-сессия"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        """Закрыть сессию Supabase"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def load_portfolios(self) -> Dict[int, Dict[str, float]]:
        """Загрузить портфели из Supabase"""
        if not self.enabled:
            return {}

        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/portfolios?select=*"
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    portfolios: Dict[int, Dict[str, float]] = {}
                    for row in data:
                        try:
                            user_id = int(row['user_id'])
                            # Supabase JSONB уже приходит как dict
                            assets = row['assets']
                            if isinstance(assets, dict):
                                portfolios[user_id] = assets
                        except (KeyError, ValueError, TypeError) as e:
                            print(f"⚠️ Invalid portfolio row: {e}")
                            continue
                    print(f"✅ Loaded {len(portfolios)} portfolios from Supabase")
                    return portfolios
                else:
                    error_text = await response.text()
                    print(f"⚠️ Supabase load portfolios: HTTP {response.status}")
                    print(f"   Response: {error_text[:200]}")
                    return {}
        except Exception as e:
            print(f"⚠️ Supabase load portfolios error: {e}")
            return {}

    async def save_portfolio(self, user_id: int, assets: Dict[str, float]):
        """Сохранить портфель в Supabase (async)"""
        if not self.enabled:
            return

        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/portfolios"
            data = {
                "user_id": user_id,
                "assets": assets,  # отправляем dict напрямую в JSONB
                "updated_at": datetime.utcnow().isoformat()
            }

            headers = {**self.headers, "Prefer": "resolution=merge-duplicates"}
            async with session.post(url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status in [200, 201, 204]:
                    # успех, молча
                    return
                else:
                    error_text = await response.text()
                    print(f"⚠️ Supabase save portfolio: HTTP {response.status}")
                    print(f"   Response: {error_text[:200]}")
        except Exception as e:
            print(f"⚠️ Supabase save portfolio error: {e}")

    async def load_trades(self) -> Dict[int, List[Dict[str, Any]]]:
        """Загрузить сделки из Supabase"""
        if not self.enabled:
            return {}

        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/trades?select=*&order=created_at.desc"
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    trades: Dict[int, List[Dict[str, Any]]] = {}
                    for row in data:
                        try:
                            user_id = int(row['user_id'])
                            if user_id not in trades:
                                trades[user_id] = []
                            trades[user_id].append({
                                'id': row['id'],
                                'symbol': row['symbol'],
                                'amount': float(row['amount']),
                                'entry_price': float(row['entry_price']),
                                'target_profit_pct': float(row['target_profit_pct']),
                                'notified': bool(row.get('notified', False)),
                                'timestamp': row.get('created_at', datetime.utcnow().isoformat())
                            })
                        except (KeyError, ValueError, TypeError) as e:
                            print(f"⚠️ Invalid trade row: {e}")
                            continue

                    total_trades = sum(len(t) for t in trades.values())
                    print(f"✅ Loaded {total_trades} trades from Supabase")
                    return trades
                else:
                    error_text = await response.text()
                    print(f"⚠️ Supabase load trades: HTTP {response.status}")
                    print(f"   Response: {error_text[:200]}")
                    return {}
        except Exception as e:
            print(f"⚠️ Supabase load trades error: {e}")
            return {}

    async def add_trade(self, user_id: int, symbol: str, amount: float,
                        entry_price: float, target_profit_pct: float) -> bool:
        """Добавить сделку в Supabase"""
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
                "target_profit_pct": target_profit_pct
                # created_at и notified проставит сама БД по дефолту
            }

            async with session.post(url, headers=self.headers, json=data, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status in [200, 201, 204]:
                    return True
                else:
                    error_text = await response.text()
                    print(f"⚠️ Supabase add trade: HTTP {response.status}")
                    print(f"   Response: {error_text[:200]}")
                    return False
        except Exception as e:
            print(f"⚠️ Supabase add trade error: {e}")
            return False

    async def update_trade_notified(self, trade_id: int):
        """Обновить статус сделки на notified=True"""
        if not self.enabled:
            return

        try:
            session = await self._get_session()
            url = f"{self.url}/rest/v1/trades?id=eq.{trade_id}"
            data = {"notified": True}

            async with session.patch(url, headers=self.headers, json=data, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status in [200, 204]:
                    return
                else:
                    error_text = await response.text()
                    print(f"⚠️ Supabase update trade: HTTP {response.status}")
                    print(f"   Response: {error_text[:200]}")
        except Exception as e:
            print(f"⚠️ Supabase update trade error: {e}")


# ======================
# === ENV VARIABLES ====
# ======================

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
LUNARCRUSH_API_KEY = os.getenv("LUNARCRUSH_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise RuntimeError("⚠ BOT_TOKEN is not set in environment!")
if not CHAT_ID:
    print("⚠ CHAT_ID не установлен - автоматические уведомления будут отключены")

# Инициализация Supabase storage
supabase_storage = SupabaseStorage(SUPABASE_URL, SUPABASE_KEY)

# =======================
# === PATHS / STORAGE ===
# =======================

def get_data_directory() -> Path:
    """Определить безопасную директорию для данных с проверкой прав записи"""
    possible_dirs = [
        Path("/home/claude/bot_data"),
        Path("/opt/render/project/src/bot_data"),
        Path("./bot_data"),
        Path(tempfile.gettempdir()) / "bot_data"
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

# ====================
# === CONFIG / API ===
# ====================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# Доступные тикеры (стоки/ETF)
AVAILABLE_TICKERS = {
    "VWCE.DE": {"name": "VWCE", "type": "stock"},
    "4GLD.DE": {"name": "4GLD (Gold ETC)", "type": "stock"},
    "DE000A2T5DZ1.SG": {"name": "X IE Physical Gold ETC", "type": "stock"},
    "SPY": {"name": "S&P 500 (SPY)", "type": "stock"},
}

# Крипто-активы
CRYPTO_IDS = {
    "BTC": {"binance": "BTCUSDT", "coingecko": "bitcoin", "paprika": "btc-bitcoin", "name": "Bitcoin"},
    "ETH": {"binance": "ETHUSDT", "coingecko": "ethereum", "paprika": "eth-ethereum", "name": "Ethereum"},
    "SOL": {"binance": "SOLUSDT", "coingecko": "solana", "paprika": "sol-solana", "name": "Solana"},
    "AVAX": {"binance": "AVAXUSDT", "coingecko": "avalanche-2", "paprika": "avax-avalanche", "name": "Avalanche"},
    "DOGE": {"binance": "DOGEUSDT", "coingecko": "dogecoin", "paprika": "doge-dogecoin", "name": "Dogecoin"},
    "LINK": {"binance": "LINKUSDT", "coingecko": "chainlink", "paprika": "link-chainlink", "name": "Chainlink"},
}

# Пороги оповещений
THRESHOLDS = {
    "stocks": 1.0,   # % изменения за интервал проверки
    "crypto": 4.0,   # % изменения за интервал проверки
}

# =====================
# === PRICE CACHE  ====
# =====================

class PriceCache:
    """Кеш с TTL, персистентностью и защитой от мусора"""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.cache: Dict[str, Dict] = {}
        self.stats = {"api_calls": 0, "cache_hits": 0}
        self.load()

    def load(self):
        """Загрузить кеш с диска с валидацией"""
        if not CACHE_FILE.exists():
            return

        try:
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)

            if not isinstance(data, dict):
                print("⚠️ Invalid cache format, resetting")
                return

            now = datetime.now().timestamp()
            valid_entries = 0

            for k, v in data.items():
                if not isinstance(v, dict) or 'timestamp' not in v or 'data' not in v:
                    continue
                try:
                    ts = float(v['timestamp'])
                    if now - ts < self.ttl * 2:
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
        """Атомарная запись кеша"""
        try:
            temp_file = CACHE_FILE.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(self.cache, f, indent=2)
            shutil.move(str(temp_file), str(CACHE_FILE))
        except Exception as e:
            print(f"⚠️ Cache save error: {e}")
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass

    def get(self, key: str) -> Optional[Dict]:
        """Получить годные данные из кеша (с TTL)"""
        if key in self.cache:
            entry = self.cache[key]
            if not isinstance(entry, dict) or 'timestamp' not in entry:
                del self.cache[key]
                return None
            try:
                age = datetime.now().timestamp() - float(entry['timestamp'])
                if age < self.ttl:
                    self.stats["cache_hits"] += 1
                    return entry.get('data')
            except (ValueError, TypeError):
                del self.cache[key]
                return None
        return None

    def set(self, key: str, data: Dict):
        """Сохранить объект в кеш"""
        self.cache[key] = {
            'data': data,
            'timestamp': datetime.now().timestamp()
        }
        self.stats["api_calls"] += 1
        # авто-подсохранение
        if len(self.cache) % 10 == 0:
            self.save()

    def get_for_alert(self, key: str) -> Optional[float]:
        """Получить last price для алертов (без TTL-проверки)"""
        if key in self.cache:
            data = self.cache[key].get('data', {})
            price = data.get('price')
            if price is not None:
                try:
                    return float(price)
                except (ValueError, TypeError):
                    return None
        return None

    def set_for_alert(self, key: str, price: float):
        """Сохранить last price для алертов"""
        try:
            price = float(price)
            if math.isnan(price) or math.isinf(price):
                print(f"⚠️ Invalid price value for {key}: {price}")
                return
        except (ValueError, TypeError):
            print(f"⚠️ Cannot convert price to float for {key}: {price}")
            return

        if key not in self.cache:
            self.cache[key] = {'data': {}, 'timestamp': datetime.now().timestamp()}
        self.cache[key]['data']['price'] = price
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

# ==========================
# === RUNTIME STRUCTURES ===
# ==========================

user_portfolios: Dict[int, Dict[str, float]] = {}
user_trades: Dict[int, List[Dict[str, Any]]] = {}
user_profiles: Dict[int, str] = {}

def fetch_initial_data_from_supabase() -> Tuple[Dict[int, Dict[str, float]], Dict[int, List[Dict[str, Any]]]]:
    """
    Безопасно стянуть портфели и сделки из Supabase в момент старта.
    Важно: не трогаем глобальный event loop, создаём временный.
    """
    if not supabase_storage.enabled:
        return {}, {}

    tmp_loop = asyncio.new_event_loop()
    try:
        portfolios = tmp_loop.run_until_complete(supabase_storage.load_portfolios())
        trades = tmp_loop.run_until_complete(supabase_storage.load_trades())
    except Exception as e:
        print(f"⚠️ Supabase load error: {e}")
        portfolios = {}
        trades = {}
    finally:
        tmp_loop.close()

    return portfolios or {}, trades or {}

def load_data():
    """
    Загрузка данных при старте бота.
    Приоритет:
    1. Supabase (если доступен и содержит данные)
    2. Локальные файлы (fallback)
    """
    global user_portfolios, user_trades

    # 1. Supabase
    supabase_portfolios, supabase_trades = fetch_initial_data_from_supabase()

    if supabase_portfolios:
        user_portfolios = supabase_portfolios
        print(f"✅ Loaded {len(user_portfolios)} portfolios from Supabase")

    if supabase_trades:
        user_trades = supabase_trades
        total = sum(len(t) for t in user_trades.values())
        print(f"✅ Loaded {total} trades from Supabase")

    # 2. Fallback: локальные портфели
    if (not user_portfolios) and PORTFOLIO_FILE.exists():
        try:
            with open(PORTFOLIO_FILE, 'r') as f:
                data = json.load(f)

            if isinstance(data, dict):
                restored: Dict[int, Dict[str, float]] = {}
                for k, v in data.items():
                    try:
                        uid = int(k)
                        if isinstance(v, dict):
                            restored[uid] = v
                    except (ValueError, TypeError):
                        continue
                user_portfolios = restored
                print(f"✅ Loaded {len(user_portfolios)} portfolios from local file")
            else:
                print("⚠️ Invalid portfolios format in local file, skipping")

        except json.JSONDecodeError as e:
            print(f"⚠️ Portfolios JSON corrupted: {e}, skipping")
        except Exception as e:
            print(f"⚠️ Portfolio load error: {e}")

    # 3. Fallback: локальные сделки
    if (not user_trades) and TRADES_FILE.exists():
        try:
            with open(TRADES_FILE, 'r') as f:
                data = json.load(f)

            if isinstance(data, dict):
                restored_trades: Dict[int, List[Dict[str, Any]]] = {}
                for k, v in data.items():
                    try:
                        uid = int(k)
                        if isinstance(v, list):
                            restored_trades[uid] = v
                    except (ValueError, TypeError):
                        continue

                user_trades = restored_trades
                print(f"✅ Loaded {len(user_trades)} trade lists from local file")
            else:
                print("⚠️ Invalid trades format in local file, skipping")

        except json.JSONDecodeError as e:
            print(f"⚠️ Trades JSON corrupted: {e}, skipping")
        except Exception as e:
            print(f"⚠️ Trades load error: {e}")

def save_portfolios():
    """Атомарно сохранить ВСЕ портфели локально"""
    try:
        temp_file = PORTFOLIO_FILE.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(user_portfolios, f, indent=2)
        shutil.move(str(temp_file), str(PORTFOLIO_FILE))
    except Exception as e:
        print(f"⚠️ Portfolio save error: {e}")
        try:
            temp_file.unlink(missing_ok=True)
        except Exception:
            pass

def save_portfolio(user_id: int, portfolio: Dict[str, float]):
    """Сохранить портфель ГИБРИДНО: локально + Supabase"""
    user_portfolios[user_id] = portfolio

    # Локально (синхронно)
    save_portfolios()

    # В Supabase — асинхронно
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(supabase_storage.save_portfolio(user_id, portfolio))
    except Exception as e:
        print(f"⚠️ Supabase async save error: {e}")

def save_trades():
    """Атомарно сохранить ВСЕ сделки локально"""
    try:
        temp_file = TRADES_FILE.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(user_trades, f, indent=2)
        shutil.move(str(temp_file), str(TRADES_FILE))
    except Exception as e:
        print(f"⚠️ Trades save error: {e}")
        try:
            temp_file.unlink(missing_ok=True)
        except Exception:
            pass

# Загрузка при старте процесса
load_data()

# ===========================
# === INVESTOR PROFILES  ====
# ===========================

INVESTOR_TYPES = {
    "long": {
        "name": "Долгосрочный инвестор",
        "emoji": "🏔️",
        "desc": "Покупаю на страхе, держу годами"
    },
    "swing": {
        "name": "Свинг-трейдер",
        "emoji": "🌊",
        "desc": "Ловлю волны, держу дни-недели"
    },
    "day": {
        "name": "Дневной трейдер",
        "emoji": "⚡",
        "desc": "Быстрые сделки внутри дня"
    },
}

# Conversation states
SELECT_CRYPTO, ENTER_AMOUNT, ENTER_PRICE, ENTER_TARGET = range(4)
SELECT_ASSET_TYPE, SELECT_ASSET, ENTER_ASSET_AMOUNT = range(4, 7)

def get_main_menu():
    """Главное меню в виде клавиатуры"""
    keyboard = [
        [KeyboardButton("💼 Мой портфель"), KeyboardButton("💹 Все цены")],
        [KeyboardButton("🎯 Мои сделки"), KeyboardButton("📊 Рыночные сигналы")],
        [KeyboardButton("📰 События недели"), KeyboardButton("🔮 Прогнозы")],
        [KeyboardButton("➕ Добавить актив"), KeyboardButton("🆕 Новая сделка")],
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# =============================
# === HTTP / PRICE FETCHERS ===
# =============================

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
    """Получить цену акции/ETF с Yahoo Finance c валидацией"""
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

async def get_crypto_price_raw(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    """Сырой запрос цены на крипту без кеша, с fallback"""
    crypto_info = CRYPTO_IDS.get(symbol)
    if not crypto_info:
        return None

    # 1. Binance
    try:
        binance_symbol = crypto_info["binance"]
        url = "https://api.binance.com/api/v3/ticker/24hr"
        params = {"symbol": binance_symbol}

        print(f"🔍 Trying Binance for {symbol}...")

        async with session.get(url, params=params, timeout=TIMEOUT) as response:
            print(f"   Binance response status: {response.status}")

            if response.status != 200:
                if response.status == 429:
                    print(f"⚠️ Binance rate limit for {symbol} (1200/min exceeded)")
                elif response.status == 403:
                    print(f"⚠️ Binance blocked for {symbol} (geo-block or firewall)")
                elif response.status == 418:
                    print(f"⚠️ Binance IP ban for {symbol}")
                else:
                    print(f"⚠️ Binance HTTP {response.status} for {symbol}")
            else:
                data = await response.json()
                price = float(data.get("lastPrice", 0))
                change_24h = float(data.get("priceChangePercent", 0))

                if price > 0 and not math.isnan(price) and not math.isinf(price):
                    print(f"✅ {symbol} from Binance: ${price:,.2f} ({change_24h:+.2f}%)")
                    return {
                        "usd": price,
                        "change_24h": change_24h if not math.isnan(change_24h) else None,
                        "source": "Binance"
                    }
                else:
                    print(f"⚠️ Binance returned invalid price for {symbol}: {price}")
    except asyncio.TimeoutError:
        print(f"⚠️ Binance timeout for {symbol} (>{TIMEOUT.total}s)")
    except aiohttp.ClientError as e:
        print(f"⚠️ Binance connection error for {symbol}: {e}")
    except Exception as e:
        print(f"⚠️ Binance failed for {symbol}: {type(e).__name__}: {e}")

    # 2. CoinPaprika
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
                            "change_24h": float(change_24h) if change_24h and not math.isnan(float(change_24h)) else None,
                            "source": "CoinPaprika"
                        }
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"⚠️ CoinPaprika failed for {symbol}: {e}")

    # 3. CoinGecko
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
                try:
                    price = float(price)
                    if price > 0 and not math.isnan(price):
                        print(f"✅ {symbol} from CoinGecko: ${price:,.2f}")
                        return {
                            "usd": price,
                            "change_24h": float(change_24h) if change_24h and not math.isnan(float(change_24h)) else None,
                            "source": "CoinGecko"
                        }
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"⚠️ CoinGecko failed for {symbol}: {e}")

    print(f"❌ All sources failed for {symbol}")
    return None

async def get_crypto_price(session: aiohttp.ClientSession, symbol: str, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """Получить цену крипты с кешированием"""
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
    """Индекс страха и жадности (крипторынок)"""
    cache_key = "fear_greed"

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

# ===========================
# === PORTFOLIO / TRADING ===
# ===========================

def get_user_portfolio(user_id: int) -> Dict[str, float]:
    """Получить портфель пользователя, с дефолтами для новых"""
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
    """Собрать список активных тикеров по всем юзерам (для алертов)"""
    active_assets: Dict[str, List[int]] = {}

    # Портфели
    for user_id, portfolio in user_portfolios.items():
        for ticker, quantity in portfolio.items():
            try:
                if float(quantity) > 0:
                    if ticker not in active_assets:
                        active_assets[ticker] = []
                    if user_id not in active_assets[ticker]:
                        active_assets[ticker].append(user_id)
            except (ValueError, TypeError):
                continue

    # Сделки
    for user_id, trades in user_trades.items():
        for trade in trades:
            symbol = trade.get('symbol')
            if symbol:
                if symbol not in active_assets:
                    active_assets[symbol] = []
                if user_id not in active_assets[symbol]:
                    active_assets[symbol].append(user_id)

    return active_assets

def get_user_trades(user_id: int) -> List[Dict[str, Any]]:
    """Все сделки юзера"""
    if user_id not in user_trades:
        user_trades[user_id] = []
    return user_trades[user_id]

def add_trade(user_id: int, symbol: str, amount: float, entry_price: float, target_profit_pct: float):
    """Добавить новую сделку (локально + Supabase async)"""
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
    save_trades()  # локально (атомарно)
    print(f"✅ Added trade for user {user_id}: {symbol} x{amount} @ ${entry_price}")

    # В Supabase — не блокируя поток
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(supabase_storage.add_trade(
            user_id, symbol, amount, entry_price, target_profit_pct
        ))
    except Exception as e:
        print(f"⚠️ Supabase async add trade error: {e}")

# ==========================
# === SIGNALS / ANALYTIC ===
# ==========================

async def get_market_signal(session: aiohttp.ClientSession, symbol: str, investor_type: str) -> Dict[str, Any]:
    """Получить сигнал BUY/HOLD/SELL по активу с учётом профиля инвестора"""
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

# ======================================
# === ALERTS: PRICE + TRADE PROFIT   ===
# ======================================

async def check_all_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Единая проверка алертов (цены и достижение целей по сделкам)"""
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

                    price, currency, _ = price_data
                    cache_key = f"alert_stock_{asset}"
                    old_price = price_cache.get_for_alert(cache_key)

                    if old_price and old_price > 0:
                        try:
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
                        except (ValueError, ZeroDivisionError) as e:
                            print(f"  ⚠️ {asset}: Calculation error - {e}")
                    else:
                        print(f"  {asset}: First check, storing price {price:.2f}")

                    price_cache.set_for_alert(cache_key, price)

                # Крипта
                elif asset in CRYPTO_IDS:
                    crypto_data = await get_crypto_price(session, asset, use_cache=False)
                    if not crypto_data:
                        print(f"  ⚠️ {asset}: No crypto data available")
                        continue

                    current_price = crypto_data["usd"]
                    cache_key = f"alert_crypto_{asset}"

                    # Price alert
                    old_price = price_cache.get_for_alert(cache_key)
                    if old_price and old_price > 0:
                        try:
                            change_pct = ((current_price - old_price) / old_price) * 100
                            print(f"  {asset}: ${old_price:,.2f} -> ${current_price:,.2f} ({change_pct:+.2f}%)")

                            if abs(change_pct) >= THRESHOLDS["crypto"]:
                                emoji = "🚀" if change_pct > 0 else "⚠️"
                                price_alerts.append(
                                    f"{emoji} <b>{asset}</b>: {change_pct:+.2f}%\n"
                                    f"Цена: ${current_price:,.2f}"
                                )
                                print(f"  🚨 PRICE ALERT! {asset} changed by {change_pct:+.2f}%")
                        except (ValueError, ZeroDivisionError) as e:
                            print(f"  ⚠️ {asset}: Calculation error - {e}")
                    else:
                        print(f"  {asset}: First check, storing price ${current_price:,.2f}")

                    price_cache.set_for_alert(cache_key, current_price)

                    # Trade profit alerts (по юзерам у этого актива)
                    for user_id in user_ids:
                        trades = get_user_trades(user_id)

                        for trade in trades:
                            if trade.get("symbol") != asset or trade.get("notified", False):
                                continue
                            try:
                                entry_price = float(trade["entry_price"])
                                target = float(trade["target_profit_pct"])
                                amount = float(trade["amount"])

                                if entry_price <= 0:
                                    continue

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

                                    # пометить сделку, чтобы не спамить повторно
                                    trade["notified"] = True
                                    print(f"  🚨 PROFIT ALERT for user {user_id}: {asset} +{profit_pct:.2f}%!")

                            except (ValueError, TypeError, KeyError, ZeroDivisionError) as e:
                                print(f"  ⚠️ Trade processing error for {asset}: {e}")
                                continue

                await asyncio.sleep(0.2)

            # Сохраняем факт, что трейды помечены notified
            if trade_alerts:
                save_trades()

            # Сохраняем кеш
            price_cache.save()

            # Отправляем price alerts в общий канал (CHAT_ID)
            if price_alerts:
                message = "🔔 <b>Ценовые алерты!</b>\n\n" + "\n\n".join(price_alerts)
                await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
                print(f"📤 Sent {len(price_alerts)} price alerts")

            # Отправляем trade alerts каждому юзеру персонально
            total_trade_alerts = sum(len(alerts) for alerts in trade_alerts.values())
            for user_id, alerts in trade_alerts.items():
                for alert in alerts:
                    try:
                        await context.bot.send_message(chat_id=str(user_id), text=alert, parse_mode='HTML')
                    except Exception as e:
                        print(f"⚠️ Failed to send alert to user {user_id}: {e}")

            if total_trade_alerts:
                print(f"📤 Sent {total_trade_alerts} trade alerts to {len(trade_alerts)} users")

            cache_stats = price_cache.get_stats()
            print(f"📊 Cache stats: {cache_stats}")

            print(
                f"✅ Alerts check complete. Active assets: {len(active_assets)}, "
                f"Price alerts: {len(price_alerts)}, Trade alerts: {total_trade_alerts}"
            )

            price_cache.reset_stats()

    except Exception as e:
        print(f"❌ check_all_alerts error: {e}")
        traceback.print_exc()

# =======================
# === BOT COMMANDS  ====
# =======================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in user_profiles:
        user_profiles[user_id] = "long"

    await update.message.reply_text(
        "👋 <b>Оптимизированный Trading Bot v5-FIXED</b>\n\n"
        "<b>🆕 ИСПРАВЛЕНИЯ:</b>\n"
        "• ✅ Защита от division by zero\n"
        "• ✅ Валидация JSON данных\n"
        "• ✅ Атомарная запись файлов\n"
        "• ✅ Безопасность API ключей\n"
        "• ✅ Graceful shutdown\n\n"
        "<b>⚡ ОПТИМИЗАЦИИ:</b>\n"
        "• Проверка только активных позиций\n"
        "• Персистентное хранение (Supabase + локально)\n"
        "• Умное кеширование (TTL 5 мин)\n"
        "• Приоритет Binance API\n"
        "• Снижение внешних запросов\n\n"
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
    """Показать портфель юзера с текущими оценками стоимости"""
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
        lines: List[str] = ["💼 <b>Ваш портфель:</b>\n"]
        total_value_usd = 0

        async with aiohttp.ClientSession() as session:
            # Акции / ETF
            stock_items = [(k, v) for k, v in portfolio.items() if k in AVAILABLE_TICKERS]
            if stock_items and any(v > 0 for (_, v) in stock_items):
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

                        # грубая конверсия в USD если EUR
                        if cur == "USD":
                            total_value_usd += value
                        elif cur == "EUR":
                            total_value_usd += value * 1.1  # упрощённый курс

                    await asyncio.sleep(0.3)

                lines.append("</pre>")

            # Крипта
            crypto_items = [(k, v) for k, v in portfolio.items() if k in CRYPTO_IDS]
            if crypto_items and any(v > 0 for (_, v) in crypto_items):
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
    """Показать текущие цены всех отслеживаемых тикеров"""
    try:
        # корректное локальное время Риги
        now = datetime.now(ZoneInfo("Europe/Riga"))
        timestamp = now.strftime("%H:%M:%S %d.%m.%Y")

        lines: List[str] = [
            f"💹 <b>Все цены</b>\n",
            f"🕐 Данные: <b>{timestamp}</b> (Рига)\n"
        ]

        async with aiohttp.ClientSession() as session:
            # Акции / ETF
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

            # Крипта
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

                        if chg is not None and not math.isnan(chg):
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
    """Показать активные сделки пользователя"""
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

        lines: List[str] = ["🎯 <b>Ваши сделки:</b>\n"]

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
                initial_value = total_value - total_profit
                if initial_value > 0:
                    total_profit_pct = (total_profit / initial_value) * 100
                    lines.append("━━━━━━━━━━━━━━━━")
                    lines.append(f"💰 <b>Общая стоимость: ${total_value:,.2f}</b>")
                    lines.append(f"📊 <b>Общая прибыль: {total_profit_pct:+.2f}% (${total_profit:+,.2f})</b>")

        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    except Exception as e:
        print(f"❌ my_trades error: {e}")
        traceback.print_exc()
        await update.message.reply_text("⚠ Ошибка при получении данных")

async def cmd_market_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адаптированные под профиль сигналы"""
    user_id = update.effective_user.id
    investor_type = user_profiles.get(user_id, "long")
    type_info = INVESTOR_TYPES[investor_type]

    await update.message.reply_text(f"🔄 Анализирую рынок для {type_info['emoji']} {type_info['name']}...")

    try:
        lines: List[str] = [
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
    """Профиль инвестора (long / swing / day)"""
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
    """Обработка нажатия на кнопку смены профиля"""
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
    """События недели (пока статично, как демо)"""
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
    """Прогнозы - сейчас больше как заглушка"""
    await update.message.reply_text(
        "🔮 <b>Прогнозы</b>\n\n"
        "Используйте 📊 <b>Рыночные сигналы</b> для персонализированных рекомендаций!",
        parse_mode='HTML'
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстро добавить актив по команде /add TICKER КОЛ-ВО"""
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

# ================================
# === CONVERSATION: ADD ASSET  ===
# ================================

async def cmd_add_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало сценария добавления актива с кнопками"""
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

    except Exception:
        await update.message.reply_text(
            "❌ Введите число\nНапример: <code>10</code> или <code>0.5</code>",
            parse_mode='HTML'
        )
        return ENTER_ASSET_AMOUNT

async def add_asset_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

# ==================================
# === CONVERSATION: NEW TRADE    ===
# ==================================

async def cmd_new_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать создание новой сделки с выбором монеты"""
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
    except Exception:
        await update.message.reply_text("❌ Введите число")
        return ENTER_AMOUNT

async def trade_enter_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Вариант с callback-кнопкой "price_continue"
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

    # Вариант с ручным вводом цены
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
    except Exception:
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
    except Exception:
        await update.message.reply_text("❌ Введите число")
        return ENTER_TARGET

async def trade_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

# ===================
# === HELP / MISC ===
# ===================

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Помощь - Fixed Bot v5</b>\n\n"
        "<b>✅ ИСПРАВЛЕНИЯ:</b>\n"
        "• Защита от ошибок деления на ноль\n"
        "• Валидация JSON данных\n"
        "• Атомарная запись файлов\n"
        "• Безопасность API ключей\n\n"
        "<b>⚡ ОПТИМИЗАЦИИ:</b>\n"
        "• Только активные позиции\n"
        "• Снижение запросов к внешним API\n"
        "• Персистентное хранение (локально + Supabase)\n"
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

# ===============================
# === HEALTH CHECK HTTP SERVER ==
# ===============================

async def health_check(request):
    """Health check endpoint для Render"""
    return web.Response(text="OK", status=200)

async def start_health_server():
    """Запустить HTTP сервер для health checks"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)

    port = int(os.getenv('PORT', 10000))

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    print(f"✅ Health check server running on port {port}")
    return runner

# ===========
# === MAIN ==
# ===========

def main():
    print("=" * 60)
    print("🚀 Starting FIXED Trading Bot v5")
    print("=" * 60)
    print(f"Python version: {sys.version}")
    print(f"Telegram bot version: {telegram.__version__}")
    print("=" * 60)
    print("✅ Fixed Issues:")
    print("  • Division by zero protection")
    print("  • JSON validation")
    print("  • Atomic file writes")
    print("  • Removed hardcoded credentials")
    print("  • Graceful shutdown")
    print("=" * 60)
    print("⚡ Optimizations:")
    print("  • Only active assets checked")
    print("  • Smart caching (TTL 5min)")
    print("  • Persistent storage (Supabase + local)")
    print("  • Binance priority")
    print("  • Fewer external API calls")
    print("=" * 60)

    if not TOKEN:
        print("❌ FATAL: BOT_TOKEN not set!")
        sys.exit(1)

    print(f"✅ BOT_TOKEN: {TOKEN[:10]}...")
    print(f"✅ CHAT_ID: {CHAT_ID if CHAT_ID else 'Not set (alerts disabled)'}")
    print(f"✅ DATA_DIR: {DATA_DIR}")

    print("🔧 Setting up signal handlers...")
    import signal  # импорт тут же, чтобы не тянуть в глобальную область

    print("🔧 Building Telegram Application...")
    try:
        app = Application.builder().token(TOKEN).build()
        print("✅ Application built successfully")
    except Exception as e:
        print(f"❌ FATAL: Failed to build application: {e}")
        sys.exit(1)

    print("🔧 Registering handlers...")

    # Conversation handlers: Новая сделка
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

    # Conversation handlers: Добавление актива
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

    print("✅ All handlers registered")

    # Alerts job
    job_queue = app.job_queue
    if job_queue and CHAT_ID:
        print("🔧 Setting up alerts job...")
        job_queue.run_repeating(check_all_alerts, interval=600, first=60)
        print("✅ UNIFIED alerts (price + trade): ENABLED")
        print("   First check in 60 seconds, then every 10 minutes")
    else:
        if not CHAT_ID:
            print("⚠️  Alerts DISABLED (CHAT_ID not set)")
        else:
            print("⚠️  Alerts DISABLED (job_queue not available)")

    print("=" * 60)
    print("🔄 Starting bot polling...")
    print("=" * 60)

    async def run_bot_with_health():
        """Асинхронный раннер: Telegram bot + health server + graceful shutdown"""
        health_runner = await start_health_server()

        # локальный shutdown event
        shutdown_event = asyncio.Event()

        def signal_handler_inner(sig, frame):
            """Обработчик сигналов для graceful shutdown"""
            print(f"\n⚠️  Received signal {sig}, initiating shutdown...")
            asyncio.get_event_loop().call_soon_threadsafe(shutdown_event.set)

        # регистрируем обработчики сигналов
        signal.signal(signal.SIGINT, signal_handler_inner)
        signal.signal(signal.SIGTERM, signal_handler_inner)

        try:
            async with app:
                await app.start()
                await app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES
                )
                print("✅ Bot polling started successfully")
                print("Press Ctrl+C to stop gracefully...")

                # ждём сигнала на остановку
                await shutdown_event.wait()

        finally:
            print("🛑 Stopping bot...")

            # Останавливаем updater
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                    print("  ✅ Updater stopped")
            except Exception as e:
                print(f"  ⚠️ Error stopping updater: {e}")

            # Останавливаем приложение
            try:
                if app.running:
                    await app.stop()
                    print("  ✅ Application stopped")
            except Exception as e:
                print(f"  ⚠️ Error stopping application: {e}")

            # HTTP health server
            print("🛑 Stopping health server...")
            try:
                await health_runner.cleanup()
                print("  ✅ Health server stopped")
            except Exception as e:
                print(f"  ⚠️ Error stopping health server: {e}")

            # финальное сохранение данных
            print("💾 Saving final state...")
            try:
                price_cache.save()
                save_portfolios()
                save_trades()
                print("  ✅ Data saved")
            except Exception as e:
                print(f"  ⚠️ Error saving data: {e}")

            # закрыть сессию Supabase
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
