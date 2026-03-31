"""
============================================================
  BINANCE FUTURES STRATEGY BOT
  Strategy : Trend Hunter with EMA + RSI + 4H Confirmation
             + Trailing Stop + Cooldown Protection
  Author   : Your Name
  Capital  : $5 USDT
  Mode     : Toggle TEST_MODE below — True = Testnet, False = Live
============================================================

SETUP INSTRUCTIONS
------------------
1. Install dependencies:
      pip install python-binance pandas python-telegram-bot schedule flask python-dotenv

2. Binance Testnet API:
      Go to https://testnet.binancefuture.com
      Login with GitHub → API Management → Create API Key

3. Binance Live API (for live mode):
      binance.com → Profile → API Management → Create API Key
      Enable: "Futures Trading" permission ONLY (no withdrawal!)
      Restrict to your IP address for safety

4. Telegram Bot:
      Message @BotFather on Telegram → /newbot → get TOKEN
      Message @userinfobot → get your CHAT_ID

5. Fill in your .env file with the keys.

6. Run:
      python binance_strategry_bot.py
"""

# ============================================================
#  MODE SWITCH — change this ONE line to go live
# ============================================================
TEST_MODE = True          # True = Testnet (fake money), False = Live ($5 real)

# ============================================================
#  YOUR KEYS — loaded from .env file
# ============================================================
import os
from dotenv import load_dotenv
load_dotenv()

TESTNET_API_KEY    = os.getenv("TESTNET_BINANCE_API_KEY", "")
TESTNET_API_SECRET = os.getenv("TESTNET_BINANCE_API_SECRET", "")

LIVE_API_KEY       = os.getenv("BINANCE_API_KEY", "")
LIVE_API_SECRET    = os.getenv("BINANCE_API_SECRET", "")

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
#  STRATEGY CONFIGURATION
# ============================================================
SYMBOL          = "DOGEUSDT"

TOTAL_CAPITAL   = 5.0
TRADE_CAPITAL   = 4.0
LEVERAGE        = 3
NOTIONAL        = TRADE_CAPITAL * LEVERAGE   # $12 effective

# -- Entry Filters --
EMA_FAST        = 9              # 1h fast EMA
EMA_SLOW        = 21             # 1h slow EMA
EMA_4H_FAST     = 9              # 4h fast EMA (trend alignment)
EMA_4H_SLOW     = 21             # 4h slow EMA
RSI_PERIOD      = 14             # RSI period
RSI_LONG_MIN    = 50             # RSI must be above this for LONG
RSI_SHORT_MAX   = 50             # RSI must be below this for SHORT
HIGH_LOW_BUFFER = 0.005          # 0.5% buffer from 24h high/low (wider catch zone)
VOLUME_MULT     = 1.1            # Volume must be 1.1x average (relaxed for low-vol markets)

# -- Risk Management --
STOP_LOSS_PCT   = 0.015          # 1.5% stop loss from entry
TAKE_PROFIT_PCT = 0.025          # 2.5% take profit from entry
TRAIL_TRIGGER_PCT = 0.010        # Activate trailing stop after +1% profit
TRAIL_PCT       = 0.005          # Trail 0.5% below/above the peak price
MAX_HOLD_HOURS  = 12             # Force close after 12 hours
MAX_DAILY_LOSS  = 1.5            # Pause bot if daily loss > $1.50
COOLDOWN_MINUTES  = 15           # Minutes to wait after a stop loss hit
MAX_DAILY_TRADES  = 6            # Hard cap on trades per day (prevents overtrading)

# -- Execution --
ORDER_TYPE      = "LIMIT"
LIMIT_OFFSET    = 0.0003         # 0.03% inside spread
CHECK_INTERVAL  = 60             # Seconds between ticks

# ============================================================
#  IMPORTS
# ============================================================
import time
import logging
import schedule
import threading
import json
from threading import Lock
from datetime import datetime, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import asyncio
from flask import Flask, jsonify
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

# ============================================================
#  LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("strategy_bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ============================================================
#  GLOBAL STATE
# ============================================================
state_lock = Lock()   # Protects close_position from double-firing across threads

state = {
    # Position
    "position"          : None,      # "LONG" | "SHORT" | None
    "entry_price"       : None,
    "entry_time"        : None,
    "entry_qty"         : None,
    "stop_loss"         : None,
    "take_profit"       : None,
    # Pending LIMIT order (not yet confirmed filled)
    "pending_order_id"  : None,      # Binance order ID awaiting fill
    "pending_side"      : None,      # "LONG" | "SHORT"
    "pending_placed_at" : None,      # datetime when order was placed
    # Trailing stop
    "highest_price"     : None,      # Peak price since LONG entry
    "lowest_price"      : None,      # Trough price since SHORT entry
    "trail_stop"        : None,      # Active trailing stop level
    # P&L
    "daily_pnl"         : 0.0,
    "daily_trades"      : 0,
    "total_pnl"         : 0.0,
    "wins"              : 0,
    "losses"            : 0,
    # Control
    "paused"            : False,
    "last_sl_time"      : None,      # Timestamp of last stop-loss hit (for cooldown)
    # Signal info
    "last_signal"       : "NEUTRAL",
    "last_rsi"          : None,
    "last_ema_fast"     : None,
    "last_ema_slow"     : None,
    "last_signals"      : [],        # Last 10 signals with timestamp + price
    # Trades
    "trade_log"         : [],
}

# ============================================================
#  BINANCE CLIENT SETUP
# ============================================================
def create_client():
    if TEST_MODE:
        log.info("=== TESTNET MODE ACTIVE (fake money) ===")
        c = Client(
            api_key    = TESTNET_API_KEY,
            api_secret = TESTNET_API_SECRET,
            testnet    = True
        )
        c.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
    else:
        log.info("=== LIVE MODE ACTIVE — REAL MONEY ===")
        c = Client(
            api_key    = LIVE_API_KEY,
            api_secret = LIVE_API_SECRET
        )
    return c

client = create_client()

# ============================================================
#  TELEGRAM ALERTS
# ============================================================
async def send_telegram(message: str):
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def notify(message: str):
    """Sync wrapper — safe to call from background threads."""
    try:
        asyncio.get_event_loop().run_until_complete(send_telegram(message))
    except RuntimeError:
        asyncio.run(send_telegram(message))

# ============================================================
#  SIGNAL ENGINE
# ============================================================
def get_24hr_range() -> tuple:
    ticker = client.futures_ticker(symbol=SYMBOL)
    return float(ticker['highPrice']), float(ticker['lowPrice'])

def get_klines(interval="1h", limit=50) -> pd.DataFrame:
    raw = client.futures_klines(symbol=SYMBOL, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        'time','open','high','low','close','volume',
        'close_time','quote_vol','trades','taker_buy_base','taker_buy_quote','ignore'
    ])
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)
    return df

def get_ema(df: pd.DataFrame, period: int) -> float:
    return df['close'].ewm(span=period, adjust=False).mean().iloc[-1]

def get_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder RSI from close prices."""
    delta  = df['close'].diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=period - 1, adjust=False).mean()
    avg_l  = loss.ewm(com=period - 1, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, 1e-10)
    return float(100 - (100 / (1 + rs.iloc[-1])))

def get_4h_trend() -> tuple:
    """Returns (trend_ok_long, trend_ok_short) based on 4h EMA alignment."""
    df        = get_klines(interval="4h", limit=30)
    ema_fast  = get_ema(df, EMA_4H_FAST)
    ema_slow  = get_ema(df, EMA_4H_SLOW)
    return ema_fast > ema_slow, ema_fast < ema_slow

def get_volume_signal(df: pd.DataFrame) -> bool:
    avg_vol     = df['volume'].iloc[:-1].mean()
    current_vol = df['volume'].iloc[-1]
    return current_vol >= (avg_vol * VOLUME_MULT)

def get_current_price() -> float:
    ticker = client.futures_symbol_ticker(symbol=SYMBOL)
    return float(ticker['price'])

def is_cooldown_active() -> bool:
    """True if we're within COOLDOWN_MINUTES of the last stop-loss hit."""
    if state["last_sl_time"] is None:
        return False
    elapsed = datetime.utcnow() - state["last_sl_time"]
    return elapsed < timedelta(minutes=COOLDOWN_MINUTES)

def get_signal() -> str:
    """
    CORE STRATEGY — 5-point confirmation:
      1. Price within HIGH_LOW_BUFFER of 24h high (LONG) or low (SHORT)
      2. 1h EMA fast > slow  (trend on entry timeframe)
      3. 4h EMA fast > slow  (higher-timeframe trend alignment)
      4. RSI > 50 for LONG / RSI < 50 for SHORT
      5. Volume >= VOLUME_MULT x average  (real breakout, not fake)
    """
    try:
        high_24h, low_24h = get_24hr_range()
        price             = get_current_price()
        df                = get_klines(interval="1h", limit=60)
        ema_fast          = get_ema(df, EMA_FAST)
        ema_slow          = get_ema(df, EMA_SLOW)
        rsi               = get_rsi(df, RSI_PERIOD)
        vol_ok            = get_volume_signal(df)
        trend_long, trend_short = get_4h_trend()

        # Persist to state so dashboard can display them
        state["last_rsi"]      = round(rsi, 2)
        state["last_ema_fast"] = round(ema_fast, 5)
        state["last_ema_slow"] = round(ema_slow, 5)

        log.info(
            f"Price={price:.5f} | 24H H={high_24h:.5f} L={low_24h:.5f} "
            f"| EMA{EMA_FAST}={ema_fast:.5f} EMA{EMA_SLOW}={ema_slow:.5f} "
            f"| RSI={rsi:.1f} | Vol={vol_ok} "
            f"| 4H Long={trend_long} Short={trend_short}"
        )

        # LONG: breakout up + 1h uptrend + 4h uptrend + RSI momentum + volume
        if (price >= high_24h * (1 - HIGH_LOW_BUFFER)
                and ema_fast > ema_slow
                and trend_long
                and rsi > RSI_LONG_MIN
                and vol_ok):
            return "LONG"

        # SHORT: breakdown + 1h downtrend + 4h downtrend + RSI momentum + volume
        if (price <= low_24h * (1 + HIGH_LOW_BUFFER)
                and ema_fast < ema_slow
                and trend_short
                and rsi < RSI_SHORT_MAX
                and vol_ok):
            return "SHORT"

        return "NEUTRAL"

    except Exception as e:
        log.error(f"Signal error: {e}")
        return "NEUTRAL"

# ============================================================
#  POSITION SIZING
# ============================================================
def calculate_quantity(price: float) -> float:
    qty = NOTIONAL / price
    return round(qty, 0)

# ============================================================
#  EXECUTOR
# ============================================================
def set_leverage():
    try:
        client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        log.info(f"Leverage set to {LEVERAGE}x")
    except BinanceAPIException as e:
        log.warning(f"Leverage set warning: {e}")

def place_entry_order(signal: str) -> dict | None:
    price = get_current_price()
    qty   = calculate_quantity(price)

    if signal == "LONG":
        side        = "BUY"
        limit_price = round(price * (1 + LIMIT_OFFSET), 4)
        sl          = round(price * (1 - STOP_LOSS_PCT), 5)
        tp          = round(price * (1 + TAKE_PROFIT_PCT), 5)
    else:
        side        = "SELL"
        limit_price = round(price * (1 - LIMIT_OFFSET), 4)
        sl          = round(price * (1 + STOP_LOSS_PCT), 5)
        tp          = round(price * (1 - TAKE_PROFIT_PCT), 5)

    try:
        order = client.futures_create_order(
            symbol      = SYMBOL,
            side        = side,
            type        = "LIMIT",
            timeInForce = "GTC",
            quantity    = qty,
            price       = limit_price
        )
        order_id = order["orderId"]
        log.info(f"Entry order placed: {side} {qty} {SYMBOL} @ {limit_price} | id={order_id}")

        # Store as PENDING — position is only active once we confirm it filled
        state["pending_order_id"]  = order_id
        state["pending_side"]      = signal
        state["pending_placed_at"] = datetime.utcnow()
        state["entry_price"]       = price
        state["entry_qty"]         = qty
        state["stop_loss"]         = sl
        state["take_profit"]       = tp
        state["highest_price"]     = price
        state["lowest_price"]      = price
        state["trail_stop"]        = None

        # Record signal history
        state["last_signals"].insert(0, {
            "time"   : datetime.utcnow().strftime("%H:%M:%S"),
            "signal" : signal,
            "price"  : price,
            "rsi"    : state["last_rsi"],
        })
        state["last_signals"] = state["last_signals"][:10]

        mode_tag = "TESTNET" if TEST_MODE else "LIVE"
        notify(
            f"*{mode_tag} — Entry Order Placed ({signal})*\n"
            f"Symbol   : `{SYMBOL}`\n"
            f"Qty      : `{qty}` DOGE\n"
            f"Limit    : `{limit_price:.5f}`\n"
            f"RSI      : `{state['last_rsi']}`\n"
            f"SL       : `{sl:.5f}` ({STOP_LOSS_PCT*100}%)\n"
            f"TP       : `{tp:.5f}` ({TAKE_PROFIT_PCT*100}%)\n"
            f"Trail at : `+{TRAIL_TRIGGER_PCT*100}%` profit\n"
            f"Lev      : `{LEVERAGE}x`"
        )
        return order

    except BinanceAPIException as e:
        log.error(f"Order placement failed: {e}")
        notify(f"*Order FAILED*: {e}")
        return None

def close_position(reason: str = "manual"):
    # Claim the position atomically — prevents double-close from two threads
    with state_lock:
        if not state["position"]:
            return
        position_snap = state["position"]
        entry_snap    = state["entry_price"]
        qty           = state["entry_qty"]
        state["position"] = None   # Mark closed immediately so no other thread retries

    close_side = "SELL" if position_snap == "LONG" else "BUY"

    try:
        client.futures_create_order(
            symbol     = SYMBOL,
            side       = close_side,
            type       = "MARKET",
            quantity   = qty,
            reduceOnly = True
        )
        exit_price = get_current_price()

        # Correct PnL: qty already incorporates leverage (qty = NOTIONAL/price)
        if position_snap == "LONG":
            pnl = (exit_price - entry_snap) * qty
        else:
            pnl = (entry_snap - exit_price) * qty

        state["daily_pnl"] += pnl
        state["total_pnl"] += pnl
        if pnl > 0:
            state["wins"] += 1
        else:
            state["losses"] += 1

        trade_record = {
            "time"   : datetime.utcnow().isoformat(),
            "side"   : position_snap,
            "entry"  : entry_snap,
            "exit"   : exit_price,
            "qty"    : qty,
            "pnl"    : round(pnl, 4),
            "reason" : reason,
        }
        state["trade_log"].append(trade_record)

        mode_tag = "TESTNET" if TEST_MODE else "LIVE"
        notify(
            f"*{mode_tag} — Position Closed*\n"
            f"Side     : `{position_snap}`\n"
            f"Entry    : `{entry_snap:.5f}`\n"
            f"Exit     : `{exit_price:.5f}`\n"
            f"PnL      : `${pnl:+.4f}`\n"
            f"Reason   : `{reason}`\n"
            f"Daily PnL: `${state['daily_pnl']:+.4f}`"
        )

        log.info(f"Position closed | Reason: {reason} | PnL: ${pnl:+.4f}")

        # Clear remaining position state (position was already set None above)
        state["entry_price"]   = None
        state["entry_time"]    = None
        state["entry_qty"]     = None
        state["stop_loss"]     = None
        state["take_profit"]   = None
        state["highest_price"] = None
        state["lowest_price"]  = None
        state["trail_stop"]    = None

    except BinanceAPIException as e:
        log.error(f"Close order failed: {e}")
        notify(f"*CLOSE FAILED* — manual action needed!\n{e}")

# ============================================================
#  ORDER FILL VERIFICATION
# ============================================================
def check_pending_order():
    """
    Called every tick when a LIMIT entry order is pending.
    - If filled → activate position tracking.
    - If still open after 2 minutes → cancel it (price moved away).
    """
    if not state["pending_order_id"]:
        return

    try:
        order = client.futures_get_order(symbol=SYMBOL, orderId=state["pending_order_id"])
        status = order.get("status", "")

        if status == "FILLED":
            filled_price = float(order.get("avgPrice", state["entry_price"]))
            log.info(f"Entry order filled @ {filled_price:.5f}")
            state["position"]         = state["pending_side"]
            state["entry_price"]      = filled_price
            state["entry_time"]       = datetime.utcnow()
            state["daily_trades"]    += 1
            state["pending_order_id"] = None
            state["pending_side"]     = None
            state["pending_placed_at"]= None
            mode_tag = "TESTNET" if TEST_MODE else "LIVE"
            notify(
                f"*{mode_tag} — Entry FILLED*\n"
                f"Side   : `{state['position']}`\n"
                f"Filled : `{filled_price:.5f}`\n"
                f"SL     : `{state['stop_loss']:.5f}`\n"
                f"TP     : `{state['take_profit']:.5f}`"
            )

        elif status in ("CANCELED", "REJECTED", "EXPIRED"):
            log.warning(f"Entry order {status} — resetting.")
            state["pending_order_id"]  = None
            state["pending_side"]      = None
            state["pending_placed_at"] = None
            state["entry_price"]       = None
            state["entry_qty"]         = None
            state["stop_loss"]         = None
            state["take_profit"]       = None

        else:
            # Still NEW or PARTIALLY_FILLED — check if it has been too long
            age = datetime.utcnow() - state["pending_placed_at"]
            if age > timedelta(minutes=2):
                log.warning(f"Entry order unfilled after 2 min — cancelling id={state['pending_order_id']}")
                try:
                    client.futures_cancel_order(symbol=SYMBOL, orderId=state["pending_order_id"])
                except BinanceAPIException as cancel_err:
                    log.error(f"Cancel failed: {cancel_err}")
                state["pending_order_id"]  = None
                state["pending_side"]      = None
                state["pending_placed_at"] = None
                state["entry_price"]       = None
                state["entry_qty"]         = None
                state["stop_loss"]         = None
                state["take_profit"]       = None
                notify("*Entry order cancelled* — price moved before fill.")

    except BinanceAPIException as e:
        log.error(f"check_pending_order error: {e}")

# ============================================================
#  STARTUP POSITION SYNC
# ============================================================
def sync_position_from_binance():
    """
    On startup, check Binance for any existing open position.
    Prevents orphaning a position from a previous crash.
    """
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            side       = "LONG" if amt > 0 else "SHORT"
            entry_price = float(p.get("entryPrice", 0))
            qty        = abs(amt)
            state["position"]      = side
            state["entry_price"]   = entry_price
            state["entry_time"]    = datetime.utcnow()
            state["entry_qty"]     = qty
            state["highest_price"] = entry_price
            state["lowest_price"]  = entry_price
            state["stop_loss"]     = round(
                entry_price * (1 - STOP_LOSS_PCT) if side == "LONG"
                else entry_price * (1 + STOP_LOSS_PCT), 5
            )
            state["take_profit"]   = round(
                entry_price * (1 + TAKE_PROFIT_PCT) if side == "LONG"
                else entry_price * (1 - TAKE_PROFIT_PCT), 5
            )
            log.warning(f"Recovered existing {side} position @ {entry_price} qty={qty}")
            notify(
                f"*Position recovered on restart*\n"
                f"Side  : `{side}`\n"
                f"Entry : `{entry_price:.5f}`\n"
                f"SL    : `{state['stop_loss']:.5f}`\n"
                f"TP    : `{state['take_profit']:.5f}`"
            )
            break
    except Exception as e:
        log.error(f"sync_position_from_binance error: {e}")

# ============================================================
#  RISK MANAGER
# ============================================================
def check_risk_exits():
    if not state["position"]:
        return

    price = get_current_price()
    pos   = state["position"]

    # --- Update trailing stop ---
    if pos == "LONG":
        if price > (state["highest_price"] or price):
            state["highest_price"] = price
        profit_pct = (price - state["entry_price"]) / state["entry_price"]
        if profit_pct >= TRAIL_TRIGGER_PCT:
            new_trail = state["highest_price"] * (1 - TRAIL_PCT)
            if state["trail_stop"] is None or new_trail > state["trail_stop"]:
                state["trail_stop"] = round(new_trail, 5)
                log.info(f"Trailing stop updated → {state['trail_stop']:.5f}")

    elif pos == "SHORT":
        if price < (state["lowest_price"] or price):
            state["lowest_price"] = price
        profit_pct = (state["entry_price"] - price) / state["entry_price"]
        if profit_pct >= TRAIL_TRIGGER_PCT:
            new_trail = state["lowest_price"] * (1 + TRAIL_PCT)
            if state["trail_stop"] is None or new_trail < state["trail_stop"]:
                state["trail_stop"] = round(new_trail, 5)
                log.info(f"Trailing stop updated → {state['trail_stop']:.5f}")

    # --- Trailing stop exit ---
    if state["trail_stop"] is not None:
        if pos == "LONG" and price <= state["trail_stop"]:
            close_position("trail_stop")
            return
        if pos == "SHORT" and price >= state["trail_stop"]:
            close_position("trail_stop")
            return

    # --- Fixed stop loss ---
    if pos == "LONG" and price <= state["stop_loss"]:
        state["last_sl_time"] = datetime.utcnow()
        close_position("stop_loss")
        return
    if pos == "SHORT" and price >= state["stop_loss"]:
        state["last_sl_time"] = datetime.utcnow()
        close_position("stop_loss")
        return

    # --- Take profit ---
    if pos == "LONG" and price >= state["take_profit"]:
        close_position("take_profit")
        return
    if pos == "SHORT" and price <= state["take_profit"]:
        close_position("take_profit")
        return

    # --- Max hold time ---
    if state["entry_time"]:
        held = datetime.utcnow() - state["entry_time"]
        if held > timedelta(hours=MAX_HOLD_HOURS):
            close_position(f"max_hold_{MAX_HOLD_HOURS}h")
            return

    # --- Daily loss limit ---
    if state["daily_pnl"] <= -MAX_DAILY_LOSS:
        close_position("daily_loss_limit")
        state["paused"] = True
        notify(f"*Daily loss limit hit* (${MAX_DAILY_LOSS}). Bot paused for today.")
        log.warning("Daily loss limit reached. Bot paused.")

# ============================================================
#  MAIN STRATEGY LOOP
# ============================================================
def strategy_tick():
    if state["paused"]:
        log.info("Bot is paused. Send /resume to continue.")
        return

    log.info("--- Tick ---")

    # Check pending LIMIT order first (fill verification / stale cancel)
    if state["pending_order_id"]:
        check_pending_order()
        return  # Wait for fill result before doing anything else

    if state["position"]:
        check_risk_exits()
        return

    # Daily trade cap
    if state["daily_trades"] >= MAX_DAILY_TRADES:
        log.info(f"Daily trade cap reached ({MAX_DAILY_TRADES}). No new entries today.")
        return

    # Cooldown gate after stop loss
    if is_cooldown_active():
        remaining = COOLDOWN_MINUTES - int(
            (datetime.utcnow() - state["last_sl_time"]).total_seconds() / 60
        )
        log.info(f"Cooldown active — {remaining}m remaining. Skipping entry.")
        return

    signal = get_signal()
    state["last_signal"] = signal
    log.info(f"Signal: {signal}")

    if signal in ("LONG", "SHORT"):
        set_leverage()
        place_entry_order(signal)

# ============================================================
#  DAILY RESET
# ============================================================
def daily_reset():
    state["daily_pnl"]    = 0.0
    state["daily_trades"] = 0
    if state["paused"]:
        state["paused"] = False
        notify("*Daily reset* — bot unpaused for new trading day.")
    log.info("Daily stats reset.")

# ============================================================
#  TELEGRAM BOT COMMANDS
# ============================================================
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode  = "TESTNET" if TEST_MODE else "LIVE"
    pos   = state["position"] or "None"
    price = get_current_price()

    unrealized = 0.0
    if state["position"] and state["entry_price"]:
        qty = state["entry_qty"] or 0
        if state["position"] == "LONG":
            unrealized = (price - state["entry_price"]) * qty / LEVERAGE
        else:
            unrealized = (state["entry_price"] - price) * qty / LEVERAGE

    total_trades = state["wins"] + state["losses"]
    win_rate = (state["wins"] / total_trades * 100) if total_trades > 0 else 0.0

    trail_info = f"`{state['trail_stop']:.5f}`" if state["trail_stop"] else "`inactive`"

    msg = (
        f"*Bot Status — {mode}*\n"
        f"Symbol       : `{SYMBOL}`\n"
        f"Price        : `{price:.5f}`\n"
        f"Position     : `{pos}`\n"
        f"Entry price  : `{state['entry_price'] or 'N/A'}`\n"
        f"Stop loss    : `{state['stop_loss'] or 'N/A'}`\n"
        f"Take profit  : `{state['take_profit'] or 'N/A'}`\n"
        f"Trail stop   : {trail_info}\n"
        f"Unrealized   : `${unrealized:+.4f}`\n"
        f"RSI          : `{state['last_rsi'] or 'N/A'}`\n"
        f"Last signal  : `{state['last_signal']}`\n"
        f"Daily PnL    : `${state['daily_pnl']:+.4f}`\n"
        f"Total PnL    : `${state['total_pnl']:+.4f}`\n"
        f"Win rate     : `{win_rate:.1f}%` ({state['wins']}W / {state['losses']}L)\n"
        f"Daily trades : `{state['daily_trades']}`\n"
        f"Paused       : `{state['paused']}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["position"]:
        close_position("manual_telegram")
        await update.message.reply_text("Position closed manually.")
    else:
        await update.message.reply_text("No open position to close.")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    await update.message.reply_text("Bot paused. Send /resume to continue.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    await update.message.reply_text("Bot resumed. Trading is now active.")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    trades = state["trade_log"]
    if not trades:
        await update.message.reply_text("No completed trades yet.")
        return

    total    = sum(t["pnl"] for t in trades)
    win_rate = (state["wins"] / (state["wins"] + state["losses"]) * 100
                if (state["wins"] + state["losses"]) > 0 else 0.0)

    lines = [
        f"*Trade Report*\n",
        f"Total trades : `{len(trades)}`",
        f"Win / Loss   : `{state['wins']}W / {state['losses']}L`",
        f"Win rate     : `{win_rate:.1f}%`",
        f"Total PnL    : `${total:+.4f}`",
        f"\n*Last 5 trades:*"
    ]
    for t in trades[-5:]:
        lines.append(
            f"`{t['side']} | Entry:{t['entry']:.4f} "
            f"Exit:{t['exit']:.4f} PnL:${t['pnl']:+.4f} [{t['reason']}]`"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "*Available Commands*\n"
        "/status  — current position + P&L\n"
        "/close   — emergency close position\n"
        "/pause   — stop taking new trades\n"
        "/resume  — resume trading\n"
        "/report  — trade history + win rate\n"
        "/help    — this message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ============================================================
#  WEB DASHBOARD
# ============================================================
flask_app = Flask(__name__)

@flask_app.route("/api/state")
def api_state():
    """JSON endpoint — polled by dashboard JS every 2 seconds."""
    price = 0.0
    try:
        price = get_current_price()
    except Exception:
        pass

    unrealized = 0.0
    if state["position"] and state["entry_price"]:
        qty = state["entry_qty"] or 0
        if state["position"] == "LONG":
            unrealized = (price - state["entry_price"]) * qty / LEVERAGE
        else:
            unrealized = (state["entry_price"] - price) * qty / LEVERAGE

    hold_time = ""
    if state["entry_time"]:
        delta = datetime.utcnow() - state["entry_time"]
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        hold_time = f"{h}h {m}m"

    total_t  = state["wins"] + state["losses"]
    win_rate = round(state["wins"] / total_t * 100, 1) if total_t > 0 else 0.0

    cooldown_remaining = 0
    if is_cooldown_active():
        cooldown_remaining = COOLDOWN_MINUTES - int(
            (datetime.utcnow() - state["last_sl_time"]).total_seconds() / 60
        )

    return jsonify({
        "mode"          : "TESTNET" if TEST_MODE else "LIVE",
        "symbol"        : SYMBOL,
        "price"         : price,
        "position"      : state["position"] or "None",
        "entry_price"   : state["entry_price"],
        "stop_loss"     : state["stop_loss"],
        "take_profit"   : state["take_profit"],
        "trail_stop"    : state["trail_stop"],
        "unrealized"    : round(unrealized, 4),
        "daily_pnl"     : round(state["daily_pnl"], 4),
        "total_pnl"     : round(state["total_pnl"], 4),
        "daily_trades"  : state["daily_trades"],
        "wins"          : state["wins"],
        "losses"        : state["losses"],
        "win_rate"      : win_rate,
        "paused"        : state["paused"],
        "last_signal"   : state["last_signal"],
        "last_rsi"      : state["last_rsi"],
        "ema_fast"      : state["last_ema_fast"],
        "ema_slow"      : state["last_ema_slow"],
        "hold_time"     : hold_time,
        "cooldown_min"  : cooldown_remaining,
        "leverage"      : LEVERAGE,
        "last_signals"  : state["last_signals"],
        "trade_log"     : state["trade_log"][-20:],
        "server_time"   : datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    })

@flask_app.route("/")
def dashboard():
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Doge Bot Dashboard</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:20px}
    h2{color:#8b949e;font-size:.8rem;text-transform:uppercase;margin:20px 0 10px}
    #header{display:flex;align-items:center;gap:12px;margin-bottom:20px}
    #header h1{color:#58a6ff;font-size:1.4rem}
    .badge{display:inline-block;padding:2px 10px;border-radius:4px;font-size:.8rem;font-weight:700}
    .badge.running{background:#2ecc7122;color:#2ecc71;border:1px solid #2ecc71}
    .badge.paused{background:#e74c3c22;color:#e74c3c;border:1px solid #e74c3c}
    .badge.cooldown{background:#f1c40f22;color:#f1c40f;border:1px solid #f1c40f}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:20px}
    .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
    .card .lbl{font-size:.7rem;color:#8b949e;text-transform:uppercase;margin-bottom:5px}
    .card .val{font-size:1.15rem;font-weight:700}
    .green{color:#2ecc71}.red{color:#e74c3c}.blue{color:#58a6ff}
    .yellow{color:#f1c40f}.orange{color:#e67e22}.white{color:#c9d1d9}
    table{width:100%;border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden;margin-bottom:20px}
    th{background:#21262d;color:#8b949e;font-size:.72rem;padding:9px 11px;text-align:left}
    td{padding:7px 11px;font-size:.82rem;border-top:1px solid #21262d}
    tr:hover{background:#1c2129}
    #ticker{font-size:.7rem;color:#8b949e;margin-top:8px}
    .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#2ecc71;margin-right:5px;animation:pulse 2s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  </style>
</head>
<body>
  <div id="header">
    <h1>&#9889; Doge Bot</h1>
    <span id="mode-badge" class="badge running">LOADING</span>
    <span id="status-badge" class="badge running">...</span>
  </div>

  <div class="grid">
    <div class="card"><div class="lbl">Symbol</div><div class="val blue" id="c-symbol">—</div></div>
    <div class="card"><div class="lbl">Current Price</div><div class="val white" id="c-price">—</div></div>
    <div class="card"><div class="lbl">Position</div><div class="val white" id="c-position">—</div></div>
    <div class="card"><div class="lbl">Entry Price</div><div class="val white" id="c-entry">—</div></div>
    <div class="card"><div class="lbl">Stop Loss</div><div class="val red" id="c-sl">—</div></div>
    <div class="card"><div class="lbl">Take Profit</div><div class="val green" id="c-tp">—</div></div>
    <div class="card"><div class="lbl">Trail Stop</div><div class="val orange" id="c-trail">—</div></div>
    <div class="card"><div class="lbl">Hold Time</div><div class="val white" id="c-hold">—</div></div>
    <div class="card"><div class="lbl">Unrealized P&amp;L</div><div class="val white" id="c-unrealized">—</div></div>
    <div class="card"><div class="lbl">Daily P&amp;L</div><div class="val white" id="c-dpnl">—</div></div>
    <div class="card"><div class="lbl">Total P&amp;L</div><div class="val white" id="c-tpnl">—</div></div>
    <div class="card"><div class="lbl">RSI (14)</div><div class="val white" id="c-rsi">—</div></div>
    <div class="card"><div class="lbl">EMA Fast (1h)</div><div class="val white" id="c-ema-fast">—</div></div>
    <div class="card"><div class="lbl">EMA Slow (1h)</div><div class="val white" id="c-ema-slow">—</div></div>
    <div class="card"><div class="lbl">Last Signal</div><div class="val white" id="c-signal">—</div></div>
    <div class="card"><div class="lbl">Daily Trades</div><div class="val yellow" id="c-dtrades">—</div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val white" id="c-winrate">—</div></div>
    <div class="card"><div class="lbl">Win / Loss</div><div class="val white" id="c-wl">—</div></div>
    <div class="card"><div class="lbl">Leverage</div><div class="val white" id="c-lev">—</div></div>
    <div class="card"><div class="lbl">Cooldown</div><div class="val white" id="c-cooldown">—</div></div>
  </div>

  <h2>Signal History</h2>
  <table>
    <thead><tr><th>Time</th><th>Signal</th><th>Price</th><th>RSI</th></tr></thead>
    <tbody id="signal-body"><tr><td colspan="4" style="text-align:center;color:#8b949e;padding:14px">No signals yet</td></tr></tbody>
  </table>

  <h2>Trade History (last 20)</h2>
  <table>
    <thead><tr><th>Time (UTC)</th><th>Side</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&amp;L</th><th>Reason</th></tr></thead>
    <tbody id="trade-body"><tr><td colspan="7" style="text-align:center;color:#8b949e;padding:14px">No trades yet</td></tr></tbody>
  </table>

  <div id="ticker"><span class="dot"></span>Live — updates every 2s &nbsp;|&nbsp; <span id="c-time">—</span> UTC</div>

<script>
function fmt(v, decimals=5){ return v != null ? Number(v).toFixed(decimals) : 'N/A'; }
function pnlClass(v){ return v >= 0 ? 'green' : 'red'; }
function sigClass(s){ return s==='LONG'?'green':s==='SHORT'?'red':'white'; }

function update(){
  fetch('/api/state').then(r=>r.json()).then(d=>{
    // Header badges
    var modeBadge = document.getElementById('mode-badge');
    modeBadge.textContent = d.mode;
    modeBadge.className = 'badge running';

    var sBadge = document.getElementById('status-badge');
    if(d.paused){ sBadge.textContent='PAUSED'; sBadge.className='badge paused'; }
    else if(d.cooldown_min > 0){ sBadge.textContent='COOLDOWN '+d.cooldown_min+'m'; sBadge.className='badge cooldown'; }
    else{ sBadge.textContent='RUNNING'; sBadge.className='badge running'; }

    // Cards
    document.getElementById('c-symbol').textContent = d.symbol;
    document.getElementById('c-price').textContent  = fmt(d.price);

    var posEl = document.getElementById('c-position');
    posEl.textContent = d.position;
    posEl.className = 'val ' + sigClass(d.position);

    document.getElementById('c-entry').textContent = d.entry_price ? fmt(d.entry_price) : 'N/A';
    document.getElementById('c-sl').textContent    = d.stop_loss   ? fmt(d.stop_loss)   : 'N/A';
    document.getElementById('c-tp').textContent    = d.take_profit ? fmt(d.take_profit) : 'N/A';
    document.getElementById('c-trail').textContent = d.trail_stop  ? fmt(d.trail_stop)  : 'inactive';
    document.getElementById('c-hold').textContent  = d.hold_time || 'N/A';

    var uEl = document.getElementById('c-unrealized');
    uEl.textContent = '$'+fmt(d.unrealized,4);
    uEl.className = 'val ' + pnlClass(d.unrealized);

    var dEl = document.getElementById('c-dpnl');
    dEl.textContent = '$'+fmt(d.daily_pnl,4);
    dEl.className = 'val ' + pnlClass(d.daily_pnl);

    var tEl = document.getElementById('c-tpnl');
    tEl.textContent = '$'+fmt(d.total_pnl,4);
    tEl.className = 'val ' + pnlClass(d.total_pnl);

    var rsiEl = document.getElementById('c-rsi');
    var rsiVal = d.last_rsi ? Number(d.last_rsi).toFixed(1) : 'N/A';
    rsiEl.textContent = rsiVal;
    rsiEl.className = 'val ' + (d.last_rsi > 55 ? 'green' : d.last_rsi < 45 ? 'red' : 'yellow');

    document.getElementById('c-ema-fast').textContent = d.ema_fast ? fmt(d.ema_fast) : 'N/A';
    document.getElementById('c-ema-slow').textContent = d.ema_slow ? fmt(d.ema_slow) : 'N/A';

    var sigEl = document.getElementById('c-signal');
    sigEl.textContent = d.last_signal;
    sigEl.className = 'val ' + sigClass(d.last_signal);

    document.getElementById('c-dtrades').textContent = d.daily_trades;

    var wrEl = document.getElementById('c-winrate');
    wrEl.textContent = d.win_rate + '%';
    wrEl.className = 'val ' + (d.win_rate >= 50 ? 'green' : 'red');

    document.getElementById('c-wl').textContent = d.wins + 'W / ' + d.losses + 'L';
    document.getElementById('c-lev').textContent = d.leverage + 'x';
    document.getElementById('c-cooldown').textContent = d.cooldown_min > 0 ? d.cooldown_min+'m left' : 'None';
    document.getElementById('c-time').textContent = d.server_time;

    // Signal history
    var sb = document.getElementById('signal-body');
    if(d.last_signals && d.last_signals.length > 0){
      sb.innerHTML = d.last_signals.map(s =>
        '<tr><td>'+s.time+'</td>'
        +'<td style="color:'+(s.signal==='LONG'?'#2ecc71':s.signal==='SHORT'?'#e74c3c':'#8b949e')+'">'+s.signal+'</td>'
        +'<td>'+Number(s.price).toFixed(5)+'</td>'
        +'<td>'+(s.rsi ? Number(s.rsi).toFixed(1) : 'N/A')+'</td></tr>'
      ).join('');
    }

    // Trade history
    var tb = document.getElementById('trade-body');
    if(d.trade_log && d.trade_log.length > 0){
      tb.innerHTML = [...d.trade_log].reverse().map(t =>
        '<tr><td>'+t.time.slice(0,19)+'</td>'
        +'<td>'+t.side+'</td>'
        +'<td>'+Number(t.entry).toFixed(5)+'</td>'
        +'<td>'+Number(t.exit).toFixed(5)+'</td>'
        +'<td>'+t.qty+'</td>'
        +'<td style="color:'+(t.pnl>0?'#2ecc71':'#e74c3c')+'">${'+Number(t.pnl).toFixed(4)+'}</td>'
        +'<td>'+t.reason+'</td></tr>'
      ).join('');
    }
  }).catch(()=>{});
}

update();
setInterval(update, 2000);
</script>
</body>
</html>"""

def run_dashboard():
    import logging as _log
    _log.getLogger("werkzeug").setLevel(_log.ERROR)
    flask_app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

# ============================================================
#  SCHEDULER + MAIN ENTRY POINT
# ============================================================
def run_schedule():
    schedule.every(CHECK_INTERVAL).seconds.do(strategy_tick)
    schedule.every().day.at("00:00").do(daily_reset)
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    log.info("========================================")
    log.info(f"  BOT STARTING — {'TESTNET' if TEST_MODE else 'LIVE'} MODE")
    log.info(f"  Symbol   : {SYMBOL}")
    log.info(f"  Capital  : ${TOTAL_CAPITAL}")
    log.info(f"  Leverage : {LEVERAGE}x")
    log.info(f"  Notional : ${NOTIONAL}")
    log.info(f"  Strategy : EMA + RSI + 4H Trend + Trailing Stop")
    log.info("========================================")

    try:
        bal = client.futures_account_balance()
        usdt_bal = next((b for b in bal if b['asset'] == 'USDT'), None)
        if usdt_bal:
            log.info(f"Futures USDT balance: {usdt_bal['balance']}")

        # Recover any open position from a previous crash
        sync_position_from_binance()
        notify(
            f"*Bot Started — {'TESTNET' if TEST_MODE else 'LIVE'}*\n"
            f"Symbol: `{SYMBOL}` | Capital: `${TOTAL_CAPITAL}` | Lev: `{LEVERAGE}x`\n"
            f"Strategy: EMA + RSI + 4H trend + Trailing Stop\n"
            f"Trading is *active* automatically.\n"
            f"Dashboard: `http://localhost:8080`\n"
            f"Send /help for commands."
        )
    except Exception as e:
        log.error(f"API connection failed: {e}")
        raise

    dash_thread = threading.Thread(target=run_dashboard, daemon=True)
    dash_thread.start()
    log.info("Web dashboard started at http://localhost:8080")

    thread = threading.Thread(target=run_schedule, daemon=True)
    thread.start()
    log.info("Strategy loop started — trading active.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("close",  cmd_close))
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("help",   cmd_help))

    log.info("Telegram bot listening...")
    app.run_polling()

if __name__ == "__main__":
    main()
