import time
import requests
import json
import logging
import concurrent.futures
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, Filters
import threading
import asyncio
from bingx_client import BingxClient

# =====================================================
# ================== CONFIG ===========================
# =====================================================
VOL_PERIOD = 60
USERS_FILE = Path("users.json")
LOG_FILE = Path("bot.log")
TELEGRAM_TOKEN = ""
bot = Bot(token=TELEGRAM_TOKEN)

# =====================================================
# ================== CONFIG ===========================
# =====================================================


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def load_users():
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.error("Corrupted users.json, starting fresh")
            return {}
    return {}

def save_users(users_dict):
    USERS_FILE.write_text(json.dumps(users_dict, indent=4, ensure_ascii=False), encoding="utf-8")

users = load_users()

BINANCE_FAPI_URL = "https://fapi.binance.com"

CHECK_INTERVAL_MIN = 1

OI_4H_THRESHOLD = 10.0
OI_24H_THRESHOLD = 16.0


MIN_OI_USDT = 5_000_00
SIGNAL_COOLDOWN_HOURS = 3
REQUEST_TIMEOUT = 10

# =====================================================
# ================== UTILS ============================
# =====================================================

def pct(now, past):
    return 0.0 if past == 0 else (now - past) / past * 100.0

def send_alert(chat_id, text, parse_mode="HTML"):
    try:
        bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)

    except Exception as e:
        error_text = str(e)
        logger.error(f"Telegram error {chat_id}: {error_text}")

        # 🚫 Пользователь заблокировал бота → удаляем из базы
        if "Forbidden" in error_text and "blocked by the user" in error_text:
            users = load_users()
            chat_id_str = str(chat_id)

            if chat_id_str in users:
                del users[chat_id_str]
                save_users(users)
                logger.info(f"User {chat_id} removed from users.json (bot blocked)")

def binance_get(endpoint, params=None):
    url = BINANCE_FAPI_URL + endpoint
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

# =====================================================
# ================== DATA =============================
# =====================================================

def get_symbols():
    data = binance_get("/fapi/v1/exchangeInfo")
    return [
        s["symbol"]
        for s in data["symbols"]
        if s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
    ]

def get_oi_hist(symbol, limit):
    return binance_get("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": limit})

def get_klines(symbol, limit):
    return binance_get("/fapi/v1/klines", {"symbol": symbol, "interval": "5m", "limit": limit})

# =====================================================
# ================== CORE LOGIC =======================
# =====================================================

def check_symbol(symbol):
    try:
        oi_4h = get_oi_hist(symbol, 48)
        oi_24h = get_oi_hist(symbol, 288)
        if len(oi_24h) < 288:
            print('lenoi error')
            return None

        oi_now = float(oi_4h[-1]["sumOpenInterestValue"])
        oi_4h_ago = float(oi_4h[0]["sumOpenInterestValue"])
        oi_24h_ago = float(oi_24h[0]["sumOpenInterestValue"])
        if oi_now < MIN_OI_USDT:
            print('oi min error')
            return None

        oi_growth_4h = pct(oi_now, oi_4h_ago)
        oi_growth_24h = pct(oi_now, oi_24h_ago)

        klines_4h = get_klines(symbol, 48)
        klines_24h = get_klines(symbol, 288)

        price_now = float(klines_4h[-1][4])
        price_4h_ago = float(klines_4h[0][4])
        price_24h_ago = float(klines_24h[0][4])

        price_growth_4h = pct(price_now, price_4h_ago)
        price_growth_24h = pct(price_now, price_24h_ago)

        

        # Обработка пользователей (синхронно, как в старом коде)
        users = load_users()  # Загружаем свежие данные
        for chat_id_str, user_data in list(users.items()):
            
            price_oi_ratio = user_data.get("price_oi_ratio", 0.5)
            allow_4h = user_data.get("signals_4h_enabled", True)
            allow_24h = user_data.get("signals_24h_enabled", True)
            
            signal_4h = oi_growth_4h >= OI_4H_THRESHOLD and price_growth_4h <= oi_growth_4h * price_oi_ratio and allow_4h
            signal_24h = oi_growth_24h >= OI_24H_THRESHOLD and price_growth_24h <= oi_growth_24h * price_oi_ratio and allow_24h

            if not (signal_4h or signal_24h):
                return None

            period = "4h" if signal_4h else "24h"

            signal_data = {
                "symbol": symbol,
                "period": period,
                "oi_growth_4h": oi_growth_4h,
                "oi_growth_24h": oi_growth_24h,
                "price_growth_4h": price_growth_4h,
                "price_growth_24h": price_growth_24h,
                "price_now": price_now,
                "oi_now": oi_now
            }
        
        
            chat_id = int(chat_id_str)
            send_alert(chat_id, generate_alert_text(signal_data))

            if not user_data.get("trading_enabled", False):
                continue

            last_signals = user_data.get("last_signal_time", {})
            if symbol in last_signals and datetime.utcnow() - datetime.fromisoformat(last_signals[symbol]) < timedelta(hours=SIGNAL_COOLDOWN_HOURS):
                continue

            last_signals[symbol] = datetime.utcnow().isoformat()
            user_data["last_signal_time"] = last_signals
            save_users(users)

            if symbol in user_data.get("blacklist", []):
                continue

            # Открытие сделки в отдельном потоке
            concurrent.futures.ThreadPoolExecutor().submit(open_trade_for_user, chat_id_str, signal_data)

    except Exception as e:
        logger.error(f"Error checking {symbol}: {e}")

def generate_alert_text(signal):
    return (
        f"<b>${signal['symbol'].replace('USDT', '')}</b>\n"
        f"🚨 <b>OI ALERT</b>\n"
        f"⏱ Период: {signal['period']}\n\n"
        f"OI 4h: {signal['oi_growth_4h']:.1f}%\n"
        f"OI 24h: {signal['oi_growth_24h']:.1f}%\n\n"
        f"Цена 4h: {signal['price_growth_4h']:.1f}%\n"
        f"Цена 24h: {signal['price_growth_24h']:.1f}%\n\n"
        f"Текущая цена: {signal['price_now']:.4f}\n"
        f"OI: {signal['oi_now']/1e6:.1f}M USDT\n\n"
        f"<i>OI растёт быстрее цены → возможное накопление</i>"
    )

def check_volume_filter(symbol, multiplier):
    klines = get_klines(symbol, VOL_PERIOD)
    if len(klines) < VOL_PERIOD:
        return False
    volumes = [float(k[5]) for k in klines[:-1]]
    avg_volume = sum(volumes) / len(volumes)
    return float(klines[-1][5]) >= avg_volume * multiplier

def open_trade_for_user(chat_id_str, signal):
    chat_id = int(chat_id_str)
    user_data = users.get(chat_id_str)
    if not user_data:
        return

    symbol = signal["symbol"]
    price_now = signal["price_now"]

    if symbol in user_data.get("blacklist", []):
        logger.info(f"Skipped {symbol} for {chat_id} — in blacklist")
        return

    try:
        bx = BingxClient(
            user_data["api_key"],
            user_data["api_secret"],
            testnet=user_data.get("testnet", False)
        )

        leverage_responce = bx.set_leverage(symbol, 'LONG', user_data.get("leverage", 10))
        if leverage_responce.get('code') != 0:
            leverage_responce = bx.set_leverage(symbol, 'LONG', user_data.get("leverage", 10), one_way_mode = True)
            
            
        s = symbol.replace('USDT', '-USDT')
        qty = (user_data.get("margin_usdt", 50) * user_data.get("leverage", 10)) / price_now

        stop_price = price_now * (1 - user_data.get("stop_loss_pct", 2.0) / 100)
        precision = bx.count_decimal_places(price_now)
        stop_price = round(stop_price, precision)

        tp_prices = [
            round(price_now * (1 + p / 100), precision)
            for p in user_data.get("take_profit_pcts", [4.0, 6.0])
        ]
        qty = round(qty, 0 if precision < 2 else 1)

        if user_data.get("volume_filter_enabled", False):
            if not check_volume_filter(symbol, user_data.get("volume_multiplier", 2.0)):
                logger.info(f"Volume filter blocked {symbol} for {chat_id}")
                return
            
            
        one_way_mode = False
        
        resp = bx.place_market_order('long', qty, s, stop=stop_price, pos_side_BOTH=False)
        resp = json.loads(resp) if isinstance(resp, str) else resp  # ← ЭТО САМОЕ ВАЖНОЕ!
        if resp.get('code') == 109400:
            one_way_mode = True
            resp = bx.place_market_order('long', qty, s, stop=stop_price, pos_side_BOTH=one_way_mode)  
            
        elif resp.get('code') == 109425:
            print('symbol does not exists')
            return
        elif resp.get('code') != 0:
            raise ValueError(f"Order failed: {resp.get('msg')}")

        # Проверка позиции
        positions = bx.get_positions()
        pos = next((p for p in positions if p['symbol'] == s), None)
        if not pos or abs(float(pos.get('positionAmt', 0))) < qty * 0.9:
            bx.place_market_order('short', qty, s, pos_side_BOTH = one_way_mode, reduceOnly=True)
            raise ValueError("Position not opened properly")

        send_alert(chat_id, f"✅ Order placed on {symbol}. Period: {signal['period']}")

        # Тейк-профиты
        resp_tps = bx.set_multiple_tp(s, qty, bx.get_mark_price(s), 'long', tp_prices, one_way_mode)
        if any(r.get('code') != 0 for r in resp_tps):
            retry_resp = bx.set_multiple_tp(s, qty * 0.99, bx.get_mark_price(s), 'long', tp_prices, one_way_mode)
            if any(r.get('code') != 0 for r in retry_resp):
                r = bx.place_market_order('short', qty, s,pos_side_BOTH = one_way_mode, reduceOnly=True )
                send_alert(chat_id, f"❌ TP placement failed → position closed {symbol}. {retry_resp}. Closing responce {r} ")
                return

        # Проверка ордеров TP
        orders = bx.get_open_orders(s)
        if len([o for o in orders if o['type'] == 'TAKE_PROFIT_MARKET']) != len(tp_prices):
            bx.cancel_existing_orders(s)
            bx.place_market_order('short', qty, s,pos_side_BOTH = one_way_mode, reduceOnly=True)
            send_alert(chat_id, f"❌ Not all TPs set → position closed {symbol}")
            return

        # Trailing
        if user_data.get("trailing_enabled", False):
            act_price = price_now * (1 + user_data.get("trailing_activation_pct", 1.5) / 100)
            trail_rate = round(user_data.get("trailing_rate_pct", 0.5) / 100, 3)
            trail_resp = bx.set_trailing(s, 'long', qty, act_price, trail_rate)
            if trail_resp.get('code') != 0:
                logger.warning(f"Trailing failed for {chat_id} {symbol}: {trail_resp}")

        logger.info(f"Trade successfully opened for {chat_id} — {symbol}")

    except Exception as e:
        logger.error(f"Trade error {chat_id} {symbol}")
        send_alert(chat_id, f"❌ Ошибка открытия сделки {symbol}: {str(e)}, {resp}")
        try:
            bx.place_market_order('short', qty, s, pos_side_BOTH = one_way_mode, reduceOnly=True)
        except:
            pass

# =====================================================
# ================== TELEGRAM HANDLERS ================
# =====================================================

(
    API_KEY, API_SECRET, TESTNET,PRICE_OI_RATIO_STATE,  LEVERAGE, MARGIN, STOP_LOSS, TP_LIST,
    TRAILING_ACTIVATION, TRAILING_RATE, VOLUME_MULTIPLIER, 
) = range(11)

WELCOME_MESSAGE = """
Добро пожаловать в OI Alert Bot v2!
Автор: @Perpetual_god
Канал автора по трейдингу: @crypto_maniacdt
Курс по алготрейдингу + бонусы для рефералов: https://t.me/crypto_maniacdt/428 

Формат настроек:
- API Key/Secret: строки из BingX
- Leverage: целое (например 10)
- Типы сигналов 4h/24h:  вкл/выкл
- PRICE -> OI: число (адекватные значения менее 0.7)
- Margin USDT: число (например 50)
- SL %: число (например 2.0)
- TP %: через запятую (например 4,6,8)
- Trailing Activation %: число
- Trailing Rate %: число

Фильтры:
- Volume Filter: вкл/выкл + multiplier
- Blacklist: /blacklist_add SYMBOL

Число-коэффициент PRICE->OI отвечает за максимальное возможное отношение роста цены к росту OI. В виде формулы: входим если рост_ОИ*коэф > рост_цены
Период сигналов овечает за количество свечей, по которому мы смотрим отношение роста OI к росту цены.
4h сигналы лучшие для трейдинга, но также и по 24ч винрейт удовлетворительный.

ВАЖНО: софт идеально работает с HEDGE MODE, но в v2 добавлена поддержка и one-way mode.

Команды:
/start /settings /stats /help /stop
/blacklist_add /blacklist_remove /blacklist_show
"""

def start(update: Update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id not in users:
        users[chat_id] = {
            "trading_enabled": False,
            "testnet": False,
            "api_key": "", "api_secret": "",
            "leverage": 10, "margin_usdt": 50,
            "signals_4h_enabled": True,
            "signals_24h_enabled": True,
            "price_oi_ratio": 0.5,
            "stop_loss_pct": 2.0, "take_profit_pcts": [4.0, 6.0],
            "trailing_enabled": False,
            "trailing_activation_pct": 1.5, "trailing_rate_pct": 0.5,
            "volume_filter_enabled": False, "volume_multiplier": 2.0,
            "blacklist": [], "last_signal_time": {}
        }
        save_users(users)

    update.message.reply_text(WELCOME_MESSAGE)
    return show_settings_menu(update, context)

def help_command(update: Update, context):
    update.message.reply_text(WELCOME_MESSAGE)

def stop(update: Update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id in users:
        del users[chat_id]
        save_users(users)
    update.message.reply_text("Подписка отключена")
    return ConversationHandler.END

def settings(update: Update, context):
    return show_settings_menu(update, context)

def show_settings_menu(update: Update, context):
    chat_id = str(update.effective_chat.id if update.message else update.callback_query.message.chat_id)
    user = users.get(chat_id, {})

    keyboard = [
        [InlineKeyboardButton(f"⚙️ Торговля: {'✅' if user.get('trading_enabled') else '❌'}", callback_data='toggle_trading')],
        [InlineKeyboardButton(f"🔑 API Key: {'✅' if user.get('api_key') else '❌'}", callback_data='set_api_key')],
        [InlineKeyboardButton(f"🔒 API Secret: {'✅' if user.get('api_secret') else '❌'}", callback_data='set_api_secret')],
        [InlineKeyboardButton(f"🌐 Сеть: {'Testnet' if user.get('testnet') else 'Real'}", callback_data='toggle_testnet')],
        [InlineKeyboardButton(f"📈 Плечо: {user.get('leverage', 10)}x", callback_data='set_leverage')],
        [InlineKeyboardButton(f"💰 Маржа: {user.get('margin_usdt', 50)} USDT", callback_data='set_margin')],
        [InlineKeyboardButton(
            f"⏱ 4H сигналы: {'✅' if user.get('signals_4h_enabled', True) else '❌'}",
            callback_data='toggle_4h'
        )],
        [InlineKeyboardButton(
            f"⏱ 24H сигналы: {'✅' if user.get('signals_24h_enabled', True) else '❌'}",
            callback_data='toggle_24h'
        )],
        [InlineKeyboardButton(
            f"📐 PRICE→OI: {user.get('price_oi_ratio', 0.5)}",
            callback_data='set_price_oi_ratio'
        )],
        [InlineKeyboardButton(f"🛑 SL: {user.get('stop_loss_pct', 2.0)}%", callback_data='set_sl')],
        [InlineKeyboardButton(f"🎯 TP: {','.join(map(str, user.get('take_profit_pcts', [4,6])))}%", callback_data='set_tp_list')],
        [InlineKeyboardButton(f"📉 Трейлинг: {'✅' if user.get('trailing_enabled') else '❌'}", callback_data='toggle_trailing')],
        [InlineKeyboardButton(f"🚀 Активация трейлинга: {user.get('trailing_activation_pct', 1.5)}%", callback_data='set_trail_act')],
        [InlineKeyboardButton(f"📊 Price Rate: {user.get('trailing_rate_pct', 0.5)}%", callback_data='set_trail_rate')],
        [InlineKeyboardButton(f"🔍 Volume filter: {'✅' if user.get('volume_filter_enabled') else '❌'}", callback_data='toggle_volume_filter')],
        [InlineKeyboardButton(f"📶 Volume x{user.get('volume_multiplier', 2.0)}", callback_data='set_volume_multiplier')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "<b>⚙️ Настройки торгового бота</b>"

    if update.callback_query:
        try:
            update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise
    else:
        update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

    return ConversationHandler.END

def blacklist_show(update: Update, context):
    chat_id = str(update.effective_chat.id)
    blacklist = users.get(chat_id, {}).get("blacklist", [])
    if not blacklist:
        update.message.reply_text("Чёрный список пуст")
        return
    text = "<b>Чёрный список:</b>\n\n" + "\n".join(f"• {s}" for s in sorted(blacklist))
    update.message.reply_text(text, parse_mode="HTML")

def button_handler(update: Update, context):
    query = update.callback_query
    query.answer()
    chat_id = str(query.message.chat_id)
    data = query.data

    if data == 'toggle_trading':
        users[chat_id]['trading_enabled'] = not users[chat_id].get('trading_enabled', False)
    elif data == 'toggle_testnet':
        users[chat_id]['testnet'] = not users[chat_id].get('testnet', False)
    elif data == 'toggle_trailing':
        users[chat_id]['trailing_enabled'] = not users[chat_id].get('trailing_enabled', False)
    elif data == 'toggle_volume_filter':
        users[chat_id]['volume_filter_enabled'] = not users[chat_id].get('volume_filter_enabled', False)
    elif data.startswith('set_'):
        context.user_data['setting'] = data
        field = data.replace('set_', '').replace('_', ' ').title()
        query.edit_message_text(f"Введите новое значение для <b>{field}</b>:", parse_mode="HTML")
        return get_state(data)
    elif data == 'toggle_4h':
        users[chat_id]['signals_4h_enabled'] = not users[chat_id].get('signals_4h_enabled', True)

    elif data == 'toggle_24h':
        users[chat_id]['signals_24h_enabled'] = not users[chat_id].get('signals_24h_enabled', True)

    elif data == 'set_price_oi_ratio':
        context.user_data['setting'] = 'set_price_oi_ratio'
        query.edit_message_text(
            "Введите коэффициент PRICE → OI (например 0.5):"
        )
        return PRICE_OI_RATIO_STATE

    save_users(users)
    return show_settings_menu(update, context)

def get_state(data: str) -> int:
    mapping = {
        'set_api_key': API_KEY,
        'set_api_secret': API_SECRET,
        'set_leverage': LEVERAGE,
        'set_margin': MARGIN,
        'set_sl': STOP_LOSS,
        'set_tp_list': TP_LIST,
        'set_trail_act': TRAILING_ACTIVATION,
        'set_trail_rate': TRAILING_RATE,
        'set_volume_multiplier': VOLUME_MULTIPLIER,
        
    }
    return mapping.get(data, ConversationHandler.END)

def set_value(update: Update, context, key: str, type_func):
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()

    try:
        value = type_func(text)
        users[chat_id][key] = value
        save_users(users)
        update.message.reply_text(f"{key.replace('_', ' ').title()} установлен: {value}")
    except ValueError:
        update.message.reply_text("Неверный формат. Попробуйте снова.")
        return get_state(context.user_data.get('setting', ''))

    return show_settings_menu(update, context)

def set_api_key(update: Update, context):
    return set_value(update, context, 'api_key', str)

def set_api_secret(update: Update, context):
    return set_value(update, context, 'api_secret', str)

def set_leverage(update: Update, context):
    return set_value(update, context, 'leverage', int)

def set_margin(update: Update, context):
    return set_value(update, context, 'margin_usdt', float)

def set_sl(update: Update, context):
    return set_value(update, context, 'stop_loss_pct', float)

def set_trail_act(update: Update, context):
    return set_value(update, context, 'trailing_activation_pct', float)

def set_trail_rate(update: Update, context):
    return set_value(update, context, 'trailing_rate_pct', float)

def set_volume_multiplier(update: Update, context):
    return set_value(update, context, 'volume_multiplier', float)

def set_tp_list(update: Update, context):
    chat_id = str(update.effective_chat.id)
    try:
        tp_list = [float(x) for x in update.message.text.replace(' ', '').split(',')]
        if not tp_list or any(x <= 0 for x in tp_list):
            raise ValueError
        users[chat_id]['take_profit_pcts'] = tp_list
        save_users(users)
        update.message.reply_text(f"Take Profits: {tp_list}%")
    except:
        update.message.reply_text("Формат: 4,6,8")
        return TP_LIST
    return show_settings_menu(update, context)

def blacklist_add(update: Update, context):
    if not context.args:
        update.message.reply_text("Использование: /blacklist_add BTCUSDT")
        return
    symbol = context.args[0].upper()
    chat_id = str(update.effective_chat.id)
    users.setdefault(chat_id, {})["blacklist"] = users[chat_id].get("blacklist", [])
    if symbol not in users[chat_id]["blacklist"]:
        users[chat_id]["blacklist"].append(symbol)
        save_users(users)
    update.message.reply_text(f"{symbol} добавлен в чёрный список")

def blacklist_remove(update: Update, context):
    if not context.args:
        update.message.reply_text("Использование: /blacklist_remove BTCUSDT")
        return
    symbol = context.args[0].upper()
    chat_id = str(update.effective_chat.id)
    if symbol in users.get(chat_id, {}).get("blacklist", []):
        users[chat_id]["blacklist"].remove(symbol)
        save_users(users)
    update.message.reply_text(f"{symbol} удалён из чёрного списка")

def stats(update: Update, context):
    chat_id = str(update.effective_chat.id)
    user_data = users.get(chat_id)
    if not user_data or not user_data.get("api_key"):
        update.message.reply_text("API не настроен")
        return

    try:
        bx = BingxClient(user_data["api_key"], user_data["api_secret"], user_data.get("testnet", False))
        positions = bx.get_positions()
        open_pos = [p for p in positions if abs(float(p.get('positionAmt', 0))) > 0]
        unrealized = sum(float(p.get('unrealizedProfit', 0)) for p in open_pos)

        trades = bx.get_trades_history(days=1)
        closed = [t for t in trades if t.get("incomeType") == "REALIZED_PNL"]
        realized_24h = sum(float(t.get('income', 0)) for t in closed)

        text = (
            f"Статистика:\n\n"
            f"Открытых позиций: {len(open_pos)}\n"
            f"Нереализованный PnL: {unrealized:.2f} USDT\n\n"
            f"За 24ч:\n"
            f"Закрыто сделок: {len(closed)}\n"
            f"Реализованный PnL: {realized_24h:.2f} USDT"
        )
        update.message.reply_text(text)
    except Exception as e:
        logger.error(f"Stats error {chat_id}: {e}")
        update.message.reply_text(f"Ошибка: {e}")

# =====================================================
# ================== MAIN LOOP ========================
# =====================================================
def set_price_oi_ratio(update: Update, context):
    chat_id = str(update.effective_chat.id)
    try:
        value = float(update.message.text)
        if not (0 < value <= 2):
            raise ValueError
        users[chat_id]['price_oi_ratio'] = value
        save_users(users)
        update.message.reply_text(f"PRICE → OI коэффициент установлен: {value}")
    except:
        update.message.reply_text("Введите число, например 0.5")
        return PRICE_OI_RATIO_STATE

    return show_settings_menu(update, context)


def telegram_bot():
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('settings', settings),
            CommandHandler('help', help_command),
            CallbackQueryHandler(button_handler)
        ],
        states={
            API_KEY: [MessageHandler(Filters.text & ~Filters.command, set_api_key)],
            API_SECRET: [MessageHandler(Filters.text & ~Filters.command, set_api_secret)],
            LEVERAGE: [MessageHandler(Filters.text & ~Filters.command, set_leverage)],
            MARGIN: [MessageHandler(Filters.text & ~Filters.command, set_margin)],
            STOP_LOSS: [MessageHandler(Filters.text & ~Filters.command, set_sl)],
            TP_LIST: [MessageHandler(Filters.text & ~Filters.command, set_tp_list)],
            TRAILING_ACTIVATION: [MessageHandler(Filters.text & ~Filters.command, set_trail_act)],
            TRAILING_RATE: [MessageHandler(Filters.text & ~Filters.command, set_trail_rate)],
            VOLUME_MULTIPLIER: [MessageHandler(Filters.text & ~Filters.command, set_volume_multiplier)],
            PRICE_OI_RATIO_STATE: [MessageHandler(Filters.text & ~Filters.command, set_price_oi_ratio)],
        },
        fallbacks=[],
        per_message=False,  
    )

    dp.add_handler(CommandHandler("blacklist_add", blacklist_add))
    dp.add_handler(CommandHandler("blacklist_show", blacklist_show))
    dp.add_handler(CommandHandler("blacklist_remove", blacklist_remove))
    dp.add_handler(CommandHandler("stats", stats))
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("stop", stop))

    # Установка списка команд для / в Telegram
    commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("settings", "Настройки"),
        BotCommand("stats", "Статистика позиций"),
        BotCommand("help", "Помощь"),
        BotCommand("stop", "Отписаться"),
        BotCommand("blacklist_add", "Добавить в blacklist"),
        BotCommand("blacklist_remove", "Удалить из blacklist"),
        BotCommand("blacklist_show", "Показать blacklist"),
    ]
    bot.set_my_commands(commands)

    updater.start_polling()

# Запуск Telegram-бота в отдельном потоке
threading.Thread(target=telegram_bot, daemon=True).start()

def main():
    symbols = get_symbols()
    logger.info(f"Loaded {len(symbols)} perpetual symbols")

    while True:
        start_time = time.time()
        logger.info("Scan started")

        signals = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(check_symbol, s): s for s in symbols}
            for future in concurrent.futures.as_completed(futures):
                signal = future.result()
                if signal:
                    signals.append(signal)

        elapsed = time.time() - start_time
        sleep_time = max(60, CHECK_INTERVAL_MIN * 60 - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()