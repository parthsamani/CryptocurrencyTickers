import os, asyncio, pandas as pd
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from curl_cffi import requests as cffi_requests
import requests as req

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
    "1d": ("6mo", "1d"), "1W": ("1y", "1wk")
}
ALIAS_MAP = {"XAUUSD":"XAUUSD","GOLD":"XAUUSD","XAU":"XAUUSD","GC=F":"XAUUSD","BTCUSD":"BTC-USD","BTC":"BTC-USD"}
DEFAULT_SYMBOLS = ["XAUUSD", "BTC-USD"]
custom_symbols = set(DEFAULT_SYMBOLS)
user_settings = {"tf": "5m", "pivot": 10, "rr": 2.0}

def normalize_symbol(s):
    s=s.upper().strip()
    return ALIAS_MAP.get(s, s)

def get_real_spot_price(symbol):
    try:
        if "XAU" in symbol:
            r = req.get("https://api.gold-api.com/price/XAU", timeout=5).json()
            price = r.get('price')
            if price: return float(price)
        if "BTC" in symbol:
            r = req.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
            return float(r['price'])
    except: pass
    return None

def fetch_yahoo_data(symbol, range_str, interval):
    yahoo_sym = "GC=F" if "XAU" in symbol else symbol
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
        r = session.get(url, params={"range":range_str,"interval":interval}, timeout=12)
        j = r.json()['chart']['result'][0]
        ts=j['timestamp']; q=j['indicators']['quote'][0]
        df=pd.DataFrame({'Close':q['close'],'High':q['high'],'Low':q['low']}, index=pd.to_datetime(ts, unit='s'))
        df.dropna(inplace=True)
        return df
    except:
        return pd.DataFrame()

def get_analysis(df, real_live):
    if len(df) < 50: return None
    chart_close = float(df['Close'].iloc[-1])
    close = real_live if real_live else chart_close
    ema9 = float(df['Close'].ewm(span=9).mean().iloc[-1])
    ema21 = float(df['Close'].ewm(span=21).mean().iloc[-1])
    ema50 = float(df['Close'].ewm(span=50).mean().iloc[-1])
    sup = float(df['Low'].tail(20).min())
    res = float(df['High'].tail(20).max())
    atr = float((df['High']-df['Low']).tail(14).mean())
    diff = close - chart_close
    sup += diff; res += diff
    last_5_avg = df['Close'].tail(5).mean()
    prev_5_avg = df['Close'].tail(10).head(5).mean()
    is_down = last_5_avg < prev_5_avg
    is_up = last_5_avg > prev_5_avg
    if close > ema9+diff and ema9 > ema21 and ema21 > ema50 and is_up:
        verdict="BUY"; trend="Up Trend"; sl=sup; risk=close-sl
    elif close < ema9+diff and ema9 < ema21 and ema21 < ema50 and is_down:
        verdict="SELL"; trend="Down Trend"; sl=res; risk=sl-close
    else:
        verdict="WAIT"; trend="Sideways"; sl=close-atr if close>ema21+diff else close+atr; risk=abs(close-sl)
    if risk < atr*0.5: risk = atr
    entry=close
    t1=entry+risk if verdict=="BUY" else entry-risk if verdict=="SELL" else entry
    t2=entry+risk*2 if verdict=="BUY" else entry-risk*2 if verdict=="SELL" else entry
    t3=entry+risk*3 if verdict=="BUY" else entry-risk*3 if verdict=="SELL" else entry
    return {"trend":trend,"verdict":verdict,"support":sup,"resistance":res,"entry":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3,"live":close}

def get_signals_for_symbol(symbol):
    rng, interv = TF_MAP.get(user_settings['tf'], ("5d","5m"))
    df = fetch_yahoo_data(symbol, rng, interv)
    if df.empty: return None
    real_price = get_real_spot_price(symbol)
    a = get_analysis(df, real_price)
    if not a: return None
    return {"analysis":a, "symbol":symbol}

def get_dashboard_text():
    return (
        f"🤖 Dashboard\n"
        f"TF:{user_settings['tf']} Pivot:{user_settings['pivot']} Pairs:{len(custom_symbols)}\n"
        f"/scan - scan\n"
        f"/settings - settings dekho\n"
        f"/add XAUUSD - add\n"
        f"/remove XAUUSD - hatao\n"
        f"/list - list\n"
        f"/clear - sab clear\n"
        f"/reset - XAU & BTC reset\n"
        f"/tf 5m - timeframe\n"
        f"/pivot 10 - pivot\n"
        f"/rr 2.0 - RR"
    )

application = Application.builder().token(BOT_TOKEN).build()

async def is_joined(uid):
    if str(uid)==str(ADMIN_ID): return True
    if not CHANNEL_ID: return True
    try:
        m=await application.bot.get_chat_member(int(CHANNEL_ID), uid)
        return m.status not in ['left','kicked']
    except: return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb=[
        [InlineKeyboardButton("1m",callback_data="tf_1m"),InlineKeyboardButton("3m",callback_data="tf_3m"),InlineKeyboardButton("5m",callback_data="tf_5m")],
        [InlineKeyboardButton("15m",callback_data="tf_15m"),InlineKeyboardButton("30m",callback_data="tf_30m"),InlineKeyboardButton("1H",callback_data="tf_1h")],
        [InlineKeyboardButton("2H",callback_data="tf_2h"),InlineKeyboardButton("4H",callback_data="tf_4h"),InlineKeyboardButton("1D",callback_data="tf_1d")],
        [InlineKeyboardButton("1W",callback_data="tf_1W")],
        [InlineKeyboardButton("🔍 SCAN NOW", callback_data="scan")]
    ]
    guide_text = (
        f"{get_dashboard_text()}\n\n"
        f"📖 User Guide:\n"
        f"1️⃣ Timeframe select karo\n"
        f"2️⃣ SCAN NOW dabao\n\n"
        f"👇 Timeframe Select Karo:"
    )
    await update.message.reply_text(guide_text, reply_markup=InlineKeyboardMarkup(kb))

async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_dashboard_text())

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_joined(update.effective_user.id):
        await update.message.reply_text(f"Join {CHANNEL_LINK}"); return
    await update.message.reply_text(f"Scanning {', '.join(list(custom_symbols))} TF:{user_settings['tf']}...")
    for sym in list(custom_symbols):
        r = get_signals_for_symbol(sym)
        if not r:
            await update.message.reply_text(f"{sym} data fail"); continue
        a=r['analysis']
        name="XAUUSD" if "XAU" in r['symbol'] else "BTC-USD"
        emoji="🟢" if a['verdict']=="BUY" else "🔴" if a['verdict']=="SELL" else "🟡"
        txt=(
            f"{emoji} {name} | {a['trend']} | {a['verdict']}\n"
            f"Live: {a['live']:.2f} | TF: {user_settings['tf']}\n"
            f"Entry: {a['entry']:.2f} SL: {a['sl']:.2f}\n"
            f"T1: {a['t1']:.2f} T2: {a['t2']:.2f} T3: {a['t3']:.2f}\n"
            f"Sup: {a['support']:.2f} Res: {a['resistance']:.2f}\n\n"
            f"🌈 @CryptocurrencyTickers_bot\n"
            f"💙 Devloped by ParthTraderAlerts -Thankyou"
        )
        await update.message.reply_text(txt)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_dashboard_text() + f"\n\nPairs: {', '.join(custom_symbols)}")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /add XAUUSD"); return
    sym=normalize_symbol(context.args[0]); custom_symbols.add(sym)
    await update.message.reply_text(f"Added {sym} | {get_dashboard_text()}")

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /remove XAUUSD"); return
    sym=normalize_symbol(context.args[0]); custom_symbols.discard(sym)
    await update.message.reply_text(f"Removed {sym}")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Pairs: {', '.join(custom_symbols)}")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_symbols.clear()
    await update.message.reply_text("Sab clear ho gaya /reset se wapas la sakte ho")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_symbols.clear(); custom_symbols.update(DEFAULT_SYMBOLS)
    await update.message.reply_text(f"Reset Done XAU & BTC\n{get_dashboard_text()}")

async def tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0] in TF_MAP:
        user_settings['tf']=context.args[0]
        kb = [[InlineKeyboardButton("🔍 /scan - One Click Scan", callback_data="scan")]]
        await update.message.reply_text(f"Timeframe set [{user_settings['tf']}] ab scan Karo /scan", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text("Use: /tf 1m 3m 5m 15m 30m 1h 2h 4h 1d 1W")

async def pivot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            user_settings['pivot']=int(context.args[0])
            await update.message.reply_text(f"Pivot set {user_settings['pivot']}\n{get_dashboard_text()}")
        except:
            await update.message.reply_text("Use: /pivot 10")
    else:
        await update.message.reply_text(f"Pivot: {user_settings['pivot']}")

async def rr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            user_settings['rr']=float(context.args[0])
            await update.message.reply_text(f"RR set {user_settings['rr']}\n{get_dashboard_text()}")
        except:
            await update.message.reply_text("Use: /rr 2.0")
    else:
        await update.message.reply_text(f"RR: {user_settings['rr']}")

async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if q.data=="scan":
        update.message = q.message
        await scan_cmd(update, context)
    elif q.data.startswith("tf_"):
        tf=q.data.replace("tf_","")
        user_settings['tf']=tf
        kb = [[InlineKeyboardButton("🔍 /scan - One Click Scan", callback_data="scan")]]
        await q.message.reply_text(f"Timeframe set [{tf}] ab scan Karo /scan", reply_markup=InlineKeyboardMarkup(kb))

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("dashboard", dashboard_cmd))
application.add_handler(CommandHandler("scan", scan_cmd))
application.add_handler(CommandHandler("settings", settings_cmd))
application.add_handler(CommandHandler("add", add_cmd))
application.add_handler(CommandHandler("remove", remove_cmd))
application.add_handler(CommandHandler("list", list_cmd))
application.add_handler(CommandHandler("clear", clear_cmd))
application.add_handler(CommandHandler("reset", reset_cmd))
application.add_handler(CommandHandler("tf", tf_cmd))
application.add_handler(CommandHandler("pivot", pivot_cmd))
application.add_handler(CommandHandler("rr", rr_cmd))
application.add_handler(CallbackQueryHandler(button_cb))

@app.route('/')
def home(): return "Bot OK"

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    Thread(target=lambda: app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    application.run_polling(drop_pending_updates=True)
