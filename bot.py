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
    "1d": ("6mo", "1d"), "1W": ("1y", "1wk")
}

ALIAS_MAP = {
    "XAUUSD": "GC=F", "GOLD": "GC=F", "XAU": "GC=F",
    "BTCUSD": "BTC-USD", "BTC": "BTC-USD",
    "ETHUSD": "ETH-USD", "ETH": "ETH-USD"
}

# === ONLY XAUUSD & BTC AS YOU SAID ===
DEFAULT_SYMBOLS = ["GC=F", "BTC-USD"]
custom_symbols = set(DEFAULT_SYMBOLS)

user_settings = {"pivot": 10, "tf": "15m", "rr": 2.0, "ChannelW": 5, "loopback": 290, "minstrength": 1}

def get_ist(): return datetime.utcnow() + timedelta(hours=5, minutes=30)
def normalize_symbol(s):
    s = s.upper().strip()
    return ALIAS_MAP.get(s, s)

def fetch_yahoo_data(symbol, range_str, interval):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = session.get(url, params={"range": range_str, "interval": interval}, timeout=12)
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
    plus_dm = df['High'].diff(); minus_dm = -df['Low'].diff()
    plus_dm[plus_dm<0]=0; minus_dm[minus_dm<0]=0
    tr = compute_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / tr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / tr)
    dx = (abs(plus_di-minus_di)/(plus_di+minus_di+1e-9))*100
    adx = dx.ewm(alpha=1/period).mean()
    return adx, plus_di, minus_di

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
    for p in vals:
        lo, hi, strength = p, p, 0
        for q in vals:
            if abs(q-lo)<=cwidth:
                lo=min(lo,q); hi=max(hi,q); strength+=20
        if strength>=user_settings['minstrength']*20: zones.append((lo,hi))
    uniq=[];
    for lo,hi in zones:
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
    bull+= 1 if es>el else 0; bear+= 1 if es<el else 0
    bull+= 1 if close>es else 0; bear+= 1 if close<es else 0
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
    zones = get_sr_zones(df)
    nearestSup=None; nearestRes=None
    for lo,hi in zones:
        if hi < close: nearestSup = hi if nearestSup is None else max(nearestSup, hi)
        if lo > close: nearestRes = lo if nearestRes is None else min(nearestRes, lo)
    if nearestSup is None: nearestSup = close - atr.iloc[-1]
    if nearestRes is None: nearestRes = close + atr.iloc[-1]
    risk = abs(close-nearestSup) if verdict=="BUY" else abs(nearestRes-close) if verdict=="SELL" else atr.iloc[-1]
    entry=close; sl=nearestSup if verdict=="BUY" else nearestRes
    t1=entry+risk if verdict=="BUY" else entry-risk
    t2=entry+risk*2 if verdict=="BUY" else entry-risk*2
    t3=entry+risk*3 if verdict=="BUY" else entry-risk*3
    return {"close":close,"trend":trend,"verdict":verdict,"bullPct":bullPct,"bearPct":bearPct,"neutral":max(0,100-bullPct-bearPct),"support":nearestSup,"resistance":nearestRes,"entry":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3,"ema_short":es,"ema_long":el,"adx":adx_v}

def get_signals_for_symbol(symbol):
    range_str, interval = TF_MAP.get(user_settings['tf'], ("5d","15m"))
    df = fetch_yahoo_data(symbol, range_str, interval)
    if df.empty: return None
    analysis = get_dashboard_analysis(df)
    if not analysis: return None
    return {"analysis": analysis, "symbol": symbol}

application = Application.builder().token(BOT_TOKEN).build()

async def is_joined(uid):
    if str(uid)==str(ADMIN_ID): return True
    if not CHANNEL_ID: return True
    try:
        m=await application.bot.get_chat_member(int(CHANNEL_ID), uid)
        return m.status not in ['left','kicked']
    except: return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_joined(update.effective_user.id):
        kb=[[InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)]]
        await update.message.reply_text(f"Join {CHANNEL_LINK}", reply_markup=InlineKeyboardMarkup(kb)); return
    kb=[[InlineKeyboardButton("🔍 Scan", callback_data="scan")],
        [InlineKeyboardButton("1m",callback_data="tf_1m"),InlineKeyboardButton("5m",callback_data="tf_5m"),InlineKeyboardButton("15m",callback_data="tf_15m"),InlineKeyboardButton("1H",callback_data="tf_1h")]]
    await update.message.reply_text(f"🤖 **Parth Dashboard vFinal**\nTF:{user_settings['tf']} Pivot:{user_settings['pivot']} Pairs:{len(custom_symbols)}\n\n**Commands:**\n/scan - scan\n/settings - settings dekho\n/add XAUUSD - add\n/remove XAUUSD - hatao\n/list - list\n/clear - sab clear\n/reset - XAU & BTC reset\n/tf 5m - timeframe\n/pivot 10 - pivot\n/rr 2.0 - RR", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_joined(update.effective_user.id): await update.message.reply_text(f"Join {CHANNEL_LINK}"); return
    await update.message.reply_text(f"📊 Scanning {len(custom_symbols)} pairs TF:{user_settings['tf']} P:{user_settings['pivot']}...")
    results=[]
    for sym in list(custom_symbols):
        try:
            r=get_signals_for_symbol(sym)
            if r: results.append(r)
        except: continue
    if not results: await update.message.reply_text("❌ Data fail, 30 sec baad /scan karo"); return
    for r in results:
        a=r['analysis']; name="XAUUSD" if "GC" in r['symbol'] else r['symbol'].replace("=X","").replace("-USD","")
        emoji="🟢" if a['verdict']=="BUY" else "🔴" if a['verdict']=="SELL" else "🟡"
        txt=f"{emoji} **{name} | {a['trend']} | {a['verdict']}**\nBull:{a['bullPct']}% Bear:{a['bearPct']}% Neut:{a['neutral']}%\nEntry: `{a['entry']:.2f}` SL: `{a['sl']:.2f}`\nT1: `{a['t1']:.2f}` T2: `{a['t2']:.2f}` T3: `{a['t3']:.2f}`\nSup: `{a['support']:.2f}` Res: `{a['resistance']:.2f}`\nEMA9:{a['ema_short']:.2f} EMA50:{a['ema_long']:.2f} ADX:{a['adx']:.1f}"
        await update.message.reply_text(txt, parse_mode="Markdown")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Use: /add XAUUSD"); return
    sym=normalize_symbol(context.args[0]); custom_symbols.add(sym)
    await update.message.reply_text(f"✅ Added {sym} Total:{len(custom_symbols)}")

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Use: /remove XAUUSD"); return
    sym=normalize_symbol(context.args[0]); custom_symbols.discard(sym)
    await update.message.reply_text(f"❌ Removed {sym} Total:{len(custom_symbols)}")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_symbols.clear(); await update.message.reply_text("🗑️ Cleared all. /add se add karo")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom_symbols.clear(); custom_symbols.update(DEFAULT_SYMBOLS)
    await update.message.reply_text(f"🔄 Reset to XAUUSD & BTC-USD Done!")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📋 Pairs ({len(custom_symbols)}): {', '.join(custom_symbols)}")

async def tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] not in TF_MAP:
        await update.message.reply_text(f"Use: /tf 1m,5m,15m,1h,1d"); return
    user_settings['tf']=context.args[0]
    await update.message.reply_text(f"✅ TF = {user_settings['tf']}")

async def pivot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text(f"Pivot: {user_settings['pivot']} Use /pivot 10"); return
    user_settings['pivot']=int(context.args[0]); await update.message.reply_text(f"✅ Pivot = {user_settings['pivot']}")

async def rr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text(f"RR: {user_settings['rr']}"); return
    user_settings['rr']=float(context.args[0]); await update.message.reply_text(f"✅ RR = 1:{user_settings['rr']}")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⚙️ **Settings**\nTF: {user_settings['tf']}\nPivot: {user_settings['pivot']}\nRR: 1:{user_settings['rr']}\nWidth: {user_settings['ChannelW']}%\nPairs: {len(custom_symbols)}", parse_mode="Markdown")

async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if q.data=="scan": await scan_cmd(update, context)
    elif q.data.startswith("tf_"): user_settings['tf']=q.data.replace("tf_",""); await q.edit_message_text(f"TF {user_settings['tf']} set")

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("scan", scan_cmd))
application.add_handler(CommandHandler("overview", scan_cmd))
application.add_handler(CommandHandler("add", add_cmd))
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
def home(): return f"OK TF:{user_settings['tf']} P:{user_settings['pivot']} Pairs:{len(custom_symbols)}"
if __name__ == "__main__":
    Thread(target=lambda: app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(drop_pending_updates=True)
