import os, asyncio, pandas as pd, requests as req_lib
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from curl_cffi import requests as cffi_requests
import concurrent.futures

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_LINK = "https://t.me/ParthTraderAlertsLive"

app = Flask(__name__)
session = cffi_requests.Session(impersonate="chrome110")

TF_MAP = {
    "1m": ("1d", "1m"), "3m": ("5d", "2m"), "5m": ("5d", "5m"),
    "15m": ("5d", "15m"), "30m": ("5d", "30m"),
    "1h": ("7d", "60m"), "2h": ("7d", "90m"), "4h": ("1mo", "60m"),
    "6h": ("1mo", "60m"), "8h": ("3mo", "1h"), "12h": ("3mo", "1h"),
    "1d": ("6mo", "1d"), "1W": ("1y", "1wk"), "1M": ("2y", "1mo")
}

ALIAS_MAP = {
    "XAUUSD": "GC=F", "GOLD": "GC=F", "XAU": "GC=F",
    "XAGUSD": "SI=F", "SILVER": "SI=F",
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
    "BTC": "BTC-USD", "ETH": "ETH-USD",
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "USDINR": "USDINR=X", "EURINR": "EURINR=X"
}

DEFAULT_SYMBOLS = [
    "EURUSD=X","GBPUSD=X","USDJPY=X","USDCHF=X","AUDUSD=X","NZDUSD=X","USDCAD=X",
    "EURGBP=X","EURJPY=X","EURCHF=X","EURAUD=X","EURNZD=X","EURCAD=X",
    "GBPJPY=X","GBPCHF=X","GBPAUD=X","GBPNZD=X","GBPCAD=X",
    "AUDJPY=X","AUDNZD=X","AUDCAD=X","AUDCHF=X",
    "NZDJPY=X","NZDCAD=X","NZDCHF=X","CADJPY=X","CADCHF=X","CHFJPY=X",
    "USDINR=X","EURINR=X","GBPINR=X","JPYINR=X",
    "GC=F","SI=F","CL=F",
    "BTC-USD","ETH-USD","SOL-USD","BNB-USD","XRP-USD","ADA-USD","DOGE-USD",
    "AVAX-USD","DOT-USD","LINK-USD","LTC-USD","TRX-USD","MATIC-USD","SHIB-USD",
    "BCH-USD","UNI-USD","XLM-USD","ETC-USD","HBAR-USD","NEAR-USD","APT-USD",
    "FIL-USD","ARB-USD","OP-USD","ATOM-USD","VET-USD","PEPE-USD","BONK-USD","WIF-USD"
]

custom_symbols = set(DEFAULT_SYMBOLS)
user_settings = {"pivot": 20, "tf": "15m", "rr": 2.0}

def get_ist(): return datetime.utcnow() + timedelta(hours=5, minutes=30)

def normalize_symbol(sym):
    sym = sym.upper().strip()
    if sym in ALIAS_MAP: return ALIAS_MAP[sym]
    return sym

def get_binance_live_price(symbol):
    try:
        bin_sym = symbol.replace("-USD","USDT")
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={bin_sym}"
        r = req_lib.get(url, timeout=4).json()
        return float(r['price'])
    except: return None

def fetch_yahoo_data(symbol, range_str, interval):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"range": range_str, "interval": interval, "includePrePost": "false"}
        r = session.get(url, params=params, timeout=15)
        result = r.json()['chart']['result'][0]
        ts = result['timestamp']; q = result['indicators']['quote'][0]
        df = pd.DataFrame({'Open': q['open'], 'Close': q['close'], 'High': q['high'], 'Low': q['low']}, index=pd.to_datetime(ts, unit='s'))
        df.dropna(inplace=True); return df
    except: return pd.DataFrame()

def compute_ema(series, period): return series.ewm(span=period, adjust=False).mean()

def find_pivots(df, length):
    highs, lows = [], []
    if len(df) < length*2+5: return highs, lows
    for i in range(length, len(df)-length):
        if df['High'].iloc[i] == df['High'].iloc[i-length:i+length+1].max():
            highs.append((df.index[i], df['High'].iloc[i], i))
        if df['Low'].iloc[i] == df['Low'].iloc[i-length:i+length+1].min():
            lows.append((df.index[i], df['Low'].iloc[i], i))
    return highs, lows

def get_signals_for_symbol(symbol):
    range_str, interval = TF_MAP.get(user_settings['tf'], ("5d", "15m"))
    df = fetch_yahoo_data(symbol, range_str, interval)
    if df.empty or len(df) < 80: return None
    live_price = get_binance_live_price(symbol) if "-USD" in symbol else None
    curr_price = live_price if live_price else float(df['Close'].iloc[-1])
    df.iloc[-1, df.columns.get_loc('Close')] = curr_price
    ema_f = compute_ema(df['Close'], 9); ema_s = compute_ema(df['Close'], 50)
    trend = "BULLISH" if ema_f.iloc[-1] > ema_s.iloc[-1] else "BEARISH"
    highs, lows = find_pivots(df, user_settings['pivot'])
    if not highs or not lows: return None
    last_high_price, last_high_idx = float(highs[-1][1]), highs[-1][2]
    last_low_price, last_low_idx = float(lows[-1][1]), lows[-1][2]
    is_buy_ready = (len(df) - last_low_idx) == (user_settings['pivot'] + 1)
    is_sell_ready = (len(df) - last_high_idx) == (user_settings['pivot'] + 1)
    signals = []
    if is_buy_ready and curr_price > last_low_price:
        risk = curr_price - last_low_price
        tp = curr_price + risk * user_settings['rr']
        signals.append({"type": "BUY", "symbol": symbol, "entry": curr_price, "sl": last_low_price, "tp": tp, "trend": trend})
    if is_sell_ready and last_high_price > curr_price:
        risk = last_high_price - curr_price
        tp = curr_price - risk * user_settings['rr']
        signals.append({"type": "SELL", "symbol": symbol, "entry": curr_price, "sl": last_high_price, "tp": tp, "trend": trend})
    overview = {"symbol": symbol, "curr": curr_price, "last_high": last_high_price, "last_low": last_low_price, "trend": trend}
    return {"signals": signals, "overview": overview}

application = Application.builder().token(BOT_TOKEN).build()

async def is_joined(user_id):
    if str(user_id) == str(ADMIN_ID): return True
    if not CHANNEL_ID: return True
    try:
        m = await application.bot.get_chat_member(chat_id=int(CHANNEL_ID), user_id=user_id)
        return m.status not in ['left','kicked','banned']
    except: return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_joined(update.effective_user.id):
        kb = [[InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)]]
        await update.message.reply_text(f"Join: {CHANNEL_LINK}", reply_markup=InlineKeyboardMarkup(kb)); return
    kb = [[InlineKeyboardButton("🔍 Scan All Live", callback_data="scan")],
          [InlineKeyboardButton("1m", callback_data="tf_1m"), InlineKeyboardButton("5m", callback_data="tf_5m"), InlineKeyboardButton("15m", callback_data="tf_15m"), InlineKeyboardButton("30m", callback_data="tf_30m")],
          [InlineKeyboardButton("1H", callback_data="tf_1h"), InlineKeyboardButton("4H", callback_data="tf_4h"), InlineKeyboardButton("1D", callback_data="tf_1d"), InlineKeyboardButton("1W", callback_data="tf_1W")]]
    await update.message.reply_text(f"🤖 **FINAL LIVE BOT v2**\nTF: {user_settings['tf']} | Pivot: {user_settings['pivot']} | RR: 1:{user_settings['rr']} | Pairs: {len(custom_symbols)}\n\n**Commands:**\n/scan /settings /list\n/add XAUUSD or /add BTC-USD\n/remove BTC-USD\n/clear /reset\n/tf 5m /pivot 10 /rr 1.5", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_joined(update.effective_user.id): await update.message.reply_text(f"Join {CHANNEL_LINK}"); return
    await update.message.reply_text(f"🔍 Live Scanning {len(custom_symbols)} pairs (TF:{user_settings['tf']} P:{user_settings['pivot']})...")
    def do_scan():
        sigs, ovs = [], []
        for sym in list(custom_symbols):
            res = get_signals_for_symbol(sym)
            if res: ovs.append(res['overview']); sigs.extend(res['signals'])
        return sigs, ovs
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        signals, overviews = await loop.run_in_executor(pool, do_scan)
    msg = f"📊 **OVERVIEW {user_settings['tf']} P{user_settings['pivot']}** {get_ist().strftime('%H:%M')}\n"
    for ov in overviews[:40]:
        name = ov['symbol'].replace("-USD","").replace("=X","").replace("=F","")
        msg += f"`{name}` {ov['curr']:.4f} L:{ov['last_low']:.4f} H:{ov['last_high']:.4f} {ov['trend'][:4]}\n"
    for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
        await update.message.reply_text(chunk, parse_mode="Markdown")
    if not signals: await update.message.reply_text("⏳ No Next Candle Entry right now.")
    else:
        for s in signals[:10]:
            txt = f"{'🟢' if s['type']=='BUY' else '🔴'} **{s['type']} {s['symbol']} | {user_settings['tf']} P{user_settings['pivot']}**\nEntry: `{s['entry']:.5f}`\nSL: `{s['sl']:.5f}`\nTP 1:{user_settings['rr']}: `{s['tp']:.5f}`\nTrend: {s['trend']}"
            await update.message.reply_text(txt, parse_mode="Markdown")

async def add_symbol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Ex: /add XAUUSD or /add BTC-USD"); return
    sym = normalize_symbol(context.args[0])
    custom_symbols.add(sym)
    await update.message.reply_text(f"✅ Added {sym} (Total: {len(custom_symbols)})")

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Ex: /remove BTC-USD"); return
    sym = normalize_symbol(context.args[0])
    custom_symbols.discard(sym)
    await update.message.reply_text(f"❌ Removed {sym} (Total: {len(custom_symbols)})")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_symbols.clear()
    await update.message.reply_text("🗑️ All cleared. Ab /add se add karo.")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_symbols.clear()
    custom_symbols.update(DEFAULT_SYMBOLS)
    await update.message.reply_text(f"🔄 Reset Done! {len(custom_symbols)} pairs")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ", ".join(list(custom_symbols)[:80])
    await update.message.reply_text(f"📋 Pairs ({len(custom_symbols)}):\n{txt}")

async def tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] not in TF_MAP:
        await update.message.reply_text(f"TF: {', '.join(TF_MAP.keys())}"); return
    user_settings['tf'] = context.args[0]
    await update.message.reply_text(f"✅ TF = {user_settings['tf']}")

async def pivot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current Pivot: {user_settings['pivot']}\nUse: /pivot 10"); return
    try:
        val = int(context.args[0])
        user_settings['pivot'] = val
        await update.message.reply_text(f"✅ Pivot = {val}")
    except: await update.message.reply_text("Ex: /pivot 15")

async def rr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current RR: {user_settings['rr']}"); return
    try:
        val = float(context.args[0])
        user_settings['rr'] = val
        await update.message.reply_text(f"✅ RR = 1:{val}")
    except: await update.message.reply_text("Ex: /rr 1.5")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⚙️ **Settings**\nTF: {user_settings['tf']}\nPivot: {user_settings['pivot']}\nRR: 1:{user_settings['rr']}\nPairs: {len(custom_symbols)}", parse_mode="Markdown")

async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "scan": await scan_cmd(update, context)
    elif q.data.startswith("tf_"): user_settings['tf'] = q.data.replace("tf_",""); await q.edit_message_text(f"TF {user_settings['tf']} set", reply_markup=q.message.reply_markup)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("scan", scan_cmd))
application.add_handler(CommandHandler("overview", scan_cmd))
application.add_handler(CommandHandler("add", add_symbol_cmd))
application.add_handler(CommandHandler("remove", remove_cmd))
application.add_handler(CommandHandler("clear", clear_cmd))
application.add_handler(CommandHandler("reset", reset_cmd))
application.add_handler(CommandHandler("list", list_cmd))
application.add_handler(CommandHandler("tf", tf_cmd))
application.add_handler(CommandHandler("pivot", pivot_cmd))
application.add_handler(CommandHandler("rr", rr_cmd))
application.add_handler(CommandHandler("settings", settings_cmd))
application.add_handler(CallbackQueryHandler(button_cb))

@app.route('/')
def home(): return f"LIVE Bot OK | TF {user_settings['tf']} P{user_settings['pivot']} | {len(custom_symbols)} pairs"

if __name__ == "__main__":
    Thread(target=lambda: app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(drop_pending_updates=True)
