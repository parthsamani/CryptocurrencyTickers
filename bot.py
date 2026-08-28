import os, asyncio, pandas as pd, requests as req_lib, numpy as np
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
    "1d": ("6mo", "1d"), "1W": ("1y", "1wk")
}

ALIAS_MAP = {"XAUUSD":"GC=F","GOLD":"GC=F","XAGUSD":"SI=F","BTCUSD":"BTC-USD","ETHUSD":"ETH-USD","BTC":"BTC-USD","ETH":"ETH-USD"}
DEFAULT_SYMBOLS = ["EURUSD=X","GBPUSD=X","USDJPY=X","XAUUSD","GC=F","BTC-USD","ETH-USD","SOL-USD"]
custom_symbols = set(DEFAULT_SYMBOLS)
# Dashboard Settings from Pine
user_settings = {"pivot": 10, "tf": "15m", "rr": 2.0, "ChannelW": 5, "loopback": 290, "minstrength": 1}

def get_ist(): return datetime.utcnow() + timedelta(hours=5, minutes=30)
def normalize_symbol(s):
    s=s.upper().strip()
    return ALIAS_MAP.get(s, s)

def fetch_yahoo_data(symbol, range_str, interval):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = session.get(url, params={"range": range_str, "interval": interval}, timeout=15)
        result = r.json()['chart']['result'][0]
        ts=result['timestamp']; q=result['indicators']['quote'][0]
        df=pd.DataFrame({'Open':q['open'],'Close':q['close'],'High':q['high'],'Low':q['low']}, index=pd.to_datetime(ts, unit='s'))
        df.dropna(inplace=True); return df
    except: return pd.DataFrame()

def compute_ema(s,p): return s.ewm(span=p, adjust=False).mean()
def compute_atr(df, period=14):
    hl = df['High']-df['Low']; hc = (df['High']-df['Close'].shift()).abs(); lc = (df['Low']-df['Close'].shift()).abs()
    tr = pd.concat([hl,hc,lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def compute_adx(df, period=14):
    # Simplified ADX for Dashboard
    plus_dm = df['High'].diff(); minus_dm = -df['Low'].diff()
    plus_dm[plus_dm<0]=0; minus_dm[minus_dm<0]=0
    tr = compute_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / tr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / tr)
    dx = (abs(plus_di-minus_di)/(plus_di+minus_di+1e-9))*100
    adx = dx.ewm(alpha=1/period).mean()
    return adx, plus_di, minus_di

# === MAIN PINE LOGIC CONVERTED ===
def find_pine_pivots(df, prd):
    ph, pl = [], []
    for i in range(prd, len(df)-prd):
        if df['High'].iloc[i] == df['High'].iloc[i-prd:i+prd+1].max(): ph.append((i, df['High'].iloc[i]))
        if df['Low'].iloc[i] == df['Low'].iloc[i-prd:i+prd+1].min(): pl.append((i, df['Low'].iloc[i]))
    pivots = sorted(ph+pl, key=lambda x: x[0])
    return pivots[-user_settings['loopback']:]

def get_sr_zones(df):
    prd = user_settings['pivot']
    pivots = find_pine_pivots(df, prd)
    if not pivots: return []
    vals = [p[1] for p in pivots]
    highest = df['High'].tail(300).max(); lowest = df['Low'].tail(300).min()
    cwidth = (highest-lowest)*user_settings['ChannelW']/100
    zones=[]
    for i, p in enumerate(vals):
        lo, hi, strength = p, p, 0
        for q in vals:
            if abs(q-lo)<=cwidth:
                lo=min(lo,q); hi=max(hi,q); strength+=20
        if strength>=user_settings['minstrength']*20:
            zones.append((lo,hi,strength))
    # Remove duplicate zones
    uniq=[];
    for lo,hi,_ in zones:
        if not any(abs(lo-u[0])<cwidth for u in uniq): uniq.append((lo,hi))
    return uniq[:6]

def get_dashboard_analysis(df):
    if len(df)<60: return None
    ema_short = compute_ema(df['Close'], 9)
    ema_long = compute_ema(df['Close'], 50)
    adx, plus_di, minus_di = compute_adx(df, 14)
    atr = compute_atr(df, 14)

    close = df['Close'].iloc[-1]
    es = ema_short.iloc[-1]; el = ema_long.iloc[-1]
    adx_v = adx.iloc[-1]; plus_v = plus_di.iloc[-1]; minus_v = minus_di.iloc[-1]

    bull=0; bear=0
    bull+= 1 if es>el else 0
    bear+= 1 if es<el else 0
    bull+= 1 if close>es else 0
    bear+= 1 if close<es else 0
    bull+= 1 if (adx_v>25 and plus_v>minus_v) else 0
    bear+= 1 if (adx_v>25 and minus_v>plus_v) else 0
    bull+= 1 if close>df['Low'].tail(20).min() else 0
    bear+= 1 if close<df['High'].tail(20).max() else 0

    total = bull+bear
    bullPct = round(bull*100/total) if total else 50
    bearPct = round(bear*100/total) if total else 50

    if bullPct>=70: verdict="BUY"; trend="Up Trend"
    elif bearPct>=70: verdict="SELL"; trend="Down Trend"
    else: verdict="WAIT"; trend="Sideways"

    # SR Zones
    zones = get_sr_zones(df)
    nearestSup = None; nearestRes=None
    for lo,hi in zones:
        if hi < close: nearestSup = hi if nearestSup is None else max(nearestSup, hi)
        if lo > close: nearestRes = lo if nearestRes is None else min(nearestRes, lo)

    if nearestSup is None: nearestSup = close - atr.iloc[-1]
    if nearestRes is None: nearestRes = close + atr.iloc[-1]

    risk = abs(close-nearestSup) if verdict=="BUY" else abs(nearestRes-close) if verdict=="SELL" else atr.iloc[-1]
    entry = close
    sl = nearestSup if verdict=="BUY" else nearestRes if verdict=="SELL" else None
    t1 = entry+risk if verdict=="BUY" else entry-risk if verdict=="SELL" else None
    t2 = entry+risk*2 if verdict=="BUY" else entry-risk*2 if verdict=="SELL" else None
    t3 = entry+risk*3 if verdict=="BUY" else entry-risk*3 if verdict=="SELL" else None

    return {
        "close": close, "trend": trend, "verdict": verdict,
        "bullPct": bullPct, "bearPct": bearPct, "neutral": max(0,100-bullPct-bearPct),
        "support": nearestSup, "resistance": nearestRes,
        "entry": entry, "sl": sl, "t1": t1, "t2": t2, "t3": t3,
        "ema_short": es, "ema_long": el, "adx": adx_v
    }

def get_signals_for_symbol(symbol):
    range_str, interval = TF_MAP.get(user_settings['tf'], ("5d","15m"))
    df = fetch_yahoo_data(symbol, range_str, interval)
    if df.empty: return None
    analysis = get_dashboard_analysis(df)
    if not analysis: return None
    # Daily Pivots
    try:
        daily = fetch_yahoo_data(symbol, "5d", "1d")
        if len(daily)>=2:
            prevH=daily['High'].iloc[-2]; prevL=daily['Low'].iloc[-2]; prevC=daily['Close'].iloc[-2]
            pivot = (prevH+prevL+prevC)/3
            r1 = 2*pivot-prevL; s1=2*pivot-prevH
        else: pivot=r1=s1=None
    except: pivot=r1=s1=None

    return {"analysis": analysis, "daily": {"pivot": pivot, "r1": r1, "s1": s1}, "symbol": symbol}

# Telegram
application = Application.builder().token(BOT_TOKEN).build()
async def is_joined(uid):
    if str(uid)==str(ADMIN_ID): return True
    if not CHANNEL_ID: return True
    try:
        m=await application.bot.get_chat_member(int(CHANNEL_ID), uid)
        return m.status not in ['left','kicked']
    except: return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton("🔍 Dashboard Scan", callback_data="scan")],
        [InlineKeyboardButton("1m",callback_data="tf_1m"),InlineKeyboardButton("5m",callback_data="tf_5m"),InlineKeyboardButton("15m",callback_data="tf_15m"),InlineKeyboardButton("1H",callback_data="tf_1h")]]
    await update.message.reply_text(f"🤖 **ParthTraderAlerts Dashboard Bot**\nTF:{user_settings['tf']} Pivot:{user_settings['pivot']} W:{user_settings['ChannelW']}%\n/scan /settings /add XAUUSD", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_joined(update.effective_user.id): await update.message.reply_text(f"Join {CHANNEL_LINK}"); return
    await update.message.reply_text(f"📊 Scanning Dashboard Logic TF:{user_settings['tf']}...")
    def do_scan():
        res=[]
        for sym in list(custom_symbols):
            r=get_signals_for_symbol(sym)
            if r: res.append(r)
        return res
    loop=asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results=await loop.run_in_executor(pool, do_scan)
    for r in results[:15]:
        a=r['analysis']; sym=r['symbol'].replace("=X","").replace("-USD","").replace("=F","")
        emoji="🟢" if a['verdict']=="BUY" else "🔴" if a['verdict']=="SELL" else "🟡"
        txt=f"{emoji} **{sym} | {a['trend']} | {a['verdict']}**\nBull:{a['bullPct']}% Bear:{a['bearPct']}% Neut:{a['neutral']}%\nEntry: `{a['entry']:.2f}` SL: `{a['sl']:.2f}`\nT1: `{a['t1']:.2f}` T2: `{a['t2']:.2f}` T3: `{a['t3']:.2f}`\nSup: `{a['support']:.2f}` Res: `{a['resistance']:.2f}`\nEMA9:{a['ema_short']:.2f} EMA50:{a['ema_long']:.2f} ADX:{a['adx']:.1f}"
        if r['daily']['pivot']: txt+=f"\nDaily P:{r['daily']['pivot']:.2f} R1:{r['daily']['r1']:.2f} S1:{r['daily']['s1']:.2f}"
        await update.message.reply_text(txt, parse_mode="Markdown")
    if not results: await update.message.reply_text("No data")

async def add_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    s=normalize_symbol(context.args[0]); custom_symbols.add(s)
    await update.message.reply_text(f"Added {s}")
async def settings_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⚙️ TF:{user_settings['tf']} Pivot:{user_settings['pivot']} W:{user_settings['ChannelW']}% LB:{user_settings['loopback']}")

async def tf_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0] in TF_MAP:
        user_settings['tf']=context.args[0]
        await update.message.reply_text(f"TF={user_settings['tf']}")

async def pivot_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if context.args: user_settings['pivot']=int(context.args[0]); await update.message.reply_text(f"Pivot={user_settings['pivot']}")

async def button_cb(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if q.data=="scan": await scan_cmd(update, context)
    elif q.data.startswith("tf_"): user_settings['tf']=q.data.replace("tf_",""); await q.edit_message_text(f"TF {user_settings['tf']} set")

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("scan", scan_cmd))
application.add_handler(CommandHandler("add", add_cmd))
application.add_handler(CommandHandler("settings", settings_cmd))
application.add_handler(CommandHandler("tf", tf_cmd))
application.add_handler(CommandHandler("pivot", pivot_cmd))
application.add_handler(CallbackQueryHandler(button_cb))

@app.route('/')
def home(): return "Dashboard Bot OK"

if __name__=="__main__":
    Thread(target=lambda: app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(drop_pending_updates=True)
