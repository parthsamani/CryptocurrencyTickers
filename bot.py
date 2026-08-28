import os, asyncio, pandas as pd
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from curl_cffi import requests as cffi_requests

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
ALIAS_MAP = {"XAUUSD":"GC=F","GOLD":"GC=F","BTCUSD":"BTC-USD","BTC":"BTC-USD"}

DEFAULT_SYMBOLS = ["GC=F", "BTC-USD"]
custom_symbols = set(DEFAULT_SYMBOLS)
user_settings = {"pivot": 10, "tf": "15m", "rr": 2.0}

def normalize_symbol(s):
    s=s.upper().strip()
    return ALIAS_MAP.get(s, s)

def fetch_yahoo_data(symbol, range_str, interval):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = session.get(url, params={"range":range_str,"interval":interval}, timeout=10)
        j = r.json()['chart']['result'][0]
        ts=j['timestamp']; q=j['indicators']['quote'][0]
        df=pd.DataFrame({'Close':q['close'],'High':q['high'],'Low':q['low']}, index=pd.to_datetime(ts, unit='s'))
        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"Fetch fail {symbol}: {e}")
        return pd.DataFrame()

def get_analysis(df):
    if len(df) < 50: return None
    close = df['Close'].iloc[-1]
    # Simple & Fast EMA + Support/Resist (Pine heavy logic hata diya)
    ema9 = df['Close'].ewm(span=9).mean().iloc[-1]
    ema50 = df['Close'].ewm(span=50).mean().iloc[-1]
    sup = df['Low'].tail(20).min()
    res = df['High'].tail(20).max()
    atr = (df['High']-df['Low']).tail(14).mean()

    # Trend Logic
    if ema9 > ema50 and close > ema9:
        verdict="BUY"; trend="Up Trend"
        sl=sup; risk=close-sl
    elif ema9 < ema50 and close < ema9:
        verdict="SELL"; trend="Down Trend"
        sl=res; risk=sl-close
    else:
        verdict="WAIT"; trend="Sideways"
        sl=close-atr if close>ema9 else close+atr
        risk=abs(close-sl)

    entry=close
    t1=entry+risk if verdict=="BUY" else entry-risk if verdict=="SELL" else entry
    t2=entry+risk*2 if verdict=="BUY" else entry-risk*2 if verdict=="SELL" else entry
    t3=entry+risk*3 if verdict=="BUY" else entry-risk*3 if verdict=="SELL" else entry

    return {"close":close,"trend":trend,"verdict":verdict,"support":sup,"resistance":res,"entry":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3}

def get_signals_for_symbol(symbol):
    rng, interv = TF_MAP.get(user_settings['tf'], ("5d","15m"))
    df = fetch_yahoo_data(symbol, rng, interv)
    if df.empty: return None
    a = get_analysis(df)
    if not a: return None
    return {"analysis":a, "symbol":symbol}

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
    await update.message.reply_text(f"🤖 ParthTraderAlerts Ready\nTF:{user_settings['tf']} Pairs:{len(custom_symbols)}\n/scan /add /remove /list /reset /tf", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_joined(update.effective_user.id):
        await update.message.reply_text(f"Join {CHANNEL_LINK}"); return
    try:
        await update.message.reply_text(f"📊 Scanning {', '.join(list(custom_symbols))} TF:{user_settings['tf']}...")
        for sym in list(custom_symbols):
            r = get_signals_for_symbol(sym)
            if not r:
                await update.message.reply_text(f"❌ {sym} data fail")
                continue
            a=r['analysis']
            name="XAUUSD" if "GC" in r['symbol'] else r['symbol'].replace("=X","").replace("-USD","")
            emoji="🟢" if a['verdict']=="BUY" else "🔴" if a['verdict']=="SELL" else "🟡"
            txt=(
                f"{emoji} {name} | {a['trend']} | {a['verdict']}\n"
                f"Entry: `{a['entry']:.2f}` SL: `{a['sl']:.2f}`\n"
                f"T1: `{a['t1']:.2f}` T2: `{a['t2']:.2f}` T3: `{a['t3']:.2f}`\n"
                f"Sup: `{a['support']:.2f}` Res: `{a['resistance']:.2f}`\n\n"
                f"🌈 @CryptocurrencyTickers_bot\n"
                f"💙 Devloped by ParthTraderAlerts -Thankyou"
            )
            await update.message.reply_text(txt, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    sym=normalize_symbol(context.args[0]); custom_symbols.add(sym)
    await update.message.reply_text(f"Added {sym}")
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    sym=normalize_symbol(context.args[0]); custom_symbols.discard(sym)
    await update.message.reply_text(f"Removed {sym}")
async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_symbols.clear(); custom_symbols.update(DEFAULT_SYMBOLS)
    await update.message.reply_text("Reset to XAUUSD & BTC-USD")
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Pairs: {', '.join(custom_symbols)}")
async def tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0] in TF_MAP:
        user_settings['tf']=context.args[0]
        await update.message.reply_text(f"TF={user_settings['tf']}")
    else: await update.message.reply_text("Use /tf 1m,3m,5m,15m,30m,1h,2h,4h,1d,1W")
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"TF:{user_settings['tf']} Pairs:{len(custom_symbols)}")

async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if q.data=="scan":
        # create fake update object
        update.message = q.message
        await scan_cmd(update, context)
    elif q.data.startswith("tf_"):
        tf=q.data.replace("tf_","")
        user_settings['tf']=tf
        await q.message.reply_text(f"✅ TF {tf} set. /scan karo")

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("scan", scan_cmd))
application.add_handler(CommandHandler("add", add_cmd))
application.add_handler(CommandHandler("remove", remove_cmd))
application.add_handler(CommandHandler("reset", reset_cmd))
application.add_handler(CommandHandler("list", list_cmd))
application.add_handler(CommandHandler("tf", tf_cmd))
application.add_handler(CommandHandler("settings", settings_cmd))
application.add_handler(CallbackQueryHandler(button_cb))

@app.route('/')
def home(): return f"Bot OK TF:{user_settings['tf']}"

if __name__ == "__main__":
    Thread(target=lambda: app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    application.run_polling(drop_pending_updates=True)
