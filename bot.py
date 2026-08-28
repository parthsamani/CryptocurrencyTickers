import os, asyncio, pandas as pd, json, time
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from curl_cffi import requests as cffi_requests
import requests as req
import pytz

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
# DTC Trader Group ID - isko env me daal do, nahi to username se kaam chalega
GROUP_ID = os.getenv("GROUP_ID", "@DTC_Trader")
GROUP_LINK = "https://t.me/DTC_Trader"

app = Flask(__name__)
session = cffi_requests.Session(impersonate="chrome110")
IST = pytz.timezone('Asia/Kolkata')

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

# --- SECURITY STORAGE ---
USERS_FILE = "users.json"
ALLOWED_FILE = "allowed.json"

def load_json(file, default):
    try:
        if os.path.exists(file):
            with open(file, 'r') as f: return json.load(f)
    except: pass
    return default

def save_json(file, data):
    try:
        with open(file, 'w') as f: json.dump(data, f)
    except: pass

users_data = load_json(USERS_FILE, {}) # {user_id: {name, username, first_seen, last_seen, left_at}}
allowed_users = set(load_json(ALLOWED_FILE, [])) # manual allowed ids

def normalize_symbol(s):
    s=s.upper().strip()
    return ALIAS_MAP.get(s, s)

def get_real_spot_price(symbol):
    try:
        if "XAU" in symbol:
            r = req.get("https://api.gold-api.com/price/XAU", timeout=5).json()
            price = r.get('price')
            if price: return float(price)
        if "BTC" in symbol or "ETH" in symbol or "SOL" in symbol or "-USD" in symbol:
            # Binance for crypto
            bin_sym = symbol.replace("-","")
            r = req.get(f"https://api.binance.com/api/v3/ticker/price?symbol={bin_sym}", timeout=5).json()
            if 'price' in r: return float(r['price'])
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
        f"🤖 Dashboard @CryptocurrencyTickers_bot\n"
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

def get_full_guide():
    return (
        f"{get_dashboard_text()}\n\n"
        f"📖 User Guide:\n"
        f"1️⃣ /tf 5m - Timeframe (1m 3m 5m 15m 30m 1h 2h 4h 1d 1W)\n"
        f" Scalping: 1m-5m | Intraday: 15m-1h\n\n"
        f"2️⃣ SCAN NOW - Live BUY/SELL\n\n"
        f"3️⃣ /add SYMBOL - Ex: /add ETH-USD /add EURUSD /add SOL-USD\n\n"
        f"4️⃣ /remove SYMBOL - Hatao\n\n"
        f"5️⃣ /pivot 10 - Low=zyada signal, High=quality signal\n\n"
        f"6️⃣ /rr 2.0 - 1.5 safe, 2.0+ high profit\n\n"
        f"7️⃣ /list /clear /reset /settings\n\n"
        f"👇 Timeframe Select Karo:"
    )

application = Application.builder().token(BOT_TOKEN).build()

# --- SECURITY LOGIC ---
async def check_access(update: Update):
    user = update.effective_user
    uid = str(user.id)
    now_ist = datetime.now(IST)
    now_ts = time.time()

    # save user details
    if uid not in users_data:
        users_data[uid] = {
            "id": uid,
            "name": user.full_name,
            "username": f"@{user.username}" if user.username else "NoUsername",
            "first_seen": now_ist.strftime("%d-%m-%Y %I:%M %p IST"),
            "last_seen": now_ist.strftime("%d-%m-%Y %I:%M %p IST"),
        }
    users_data[uid]["last_seen"] = now_ist.strftime("%d-%m-%Y %I:%M %p IST")
    users_data[uid]["name"] = user.full_name
    save_json(USERS_FILE, users_data)

    # Owner bypass
    if str(user.id) == str(ADMIN_ID):
        return True
    # Manual allowed bypass
    if int(user.id) in allowed_users or uid in allowed_users:
        return True

    # Check group membership
    try:
        member = await application.bot.get_chat_member(GROUP_ID, user.id)
        status = member.status
        if status in ['left', 'kicked', 'banned']:
            # left logic
            if "left_at" not in users_data[uid]:
                users_data[uid]["left_at"] = now_ts
                save_json(USERS_FILE, users_data)
            left_at = users_data[uid].get("left_at", now_ts)
            hours_left = 24 - (now_ts - left_at)/3600
            if hours_left <= 0:
                # 24h over, block
                kb = [[InlineKeyboardButton("🔗 Join DTC Trader Group", url=GROUP_LINK)]]
                await update.message.reply_text(
                    f"⛔ You left group so please Join group and use this bot\n\n"
                    f"24 hours ho gaye group left kiye, ab access cancel.\n"
                    f"Join karo fir /start karo\n\n{ GROUP_LINK }",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return False
            else:
                kb = [[InlineKeyboardButton("🔗 Join DTC Trader Group", url=GROUP_LINK)]]
                await update.message.reply_text(
                    f"⚠️ You left group so please Join group and use this bot\n\n"
                    f"Group: {GROUP_LINK}\n"
                    f"Access {int(hours_left)} hours me cancel ho jayega. Wapas join karo.\n",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                return False
        else:
            # joined, clear left_at
            if "left_at" in users_data[uid]:
                del users_data[uid]["left_at"]
                save_json(USERS_FILE, users_data)
            return True
    except Exception as e:
        print(f"Membership check fail: {e}")
        # If bot not admin, allow but warn admin
        return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    kb=[
        [InlineKeyboardButton("1m",callback_data="tf_1m"),InlineKeyboardButton("3m",callback_data="tf_3m"),InlineKeyboardButton("5m",callback_data="tf_5m")],
        [InlineKeyboardButton("15m",callback_data="tf_15m"),InlineKeyboardButton("30m",callback_data="tf_30m"),InlineKeyboardButton("1H",callback_data="tf_1h")],
        [InlineKeyboardButton("2H",callback_data="tf_2h"),InlineKeyboardButton("4H",callback_data="tf_4h"),InlineKeyboardButton("1D",callback_data="tf_1d")],
        [InlineKeyboardButton("1W",callback_data="tf_1W")],
        [InlineKeyboardButton("🔍 SCAN NOW", callback_data="scan")]
    ]
    await update.message.reply_text(
        f"✅ Join successfully so bot started without any issue\n\n{get_full_guide()}",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(get_dashboard_text())

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(f"Scanning {', '.join(list(custom_symbols))} TF:{user_settings['tf']}...")
    for sym in list(custom_symbols):
        r = get_signals_for_symbol(sym)
        if not r:
            await update.message.reply_text(f"{sym} data fail"); continue
        a=r['analysis']
        real_name = r['symbol']
        if "XAU" in real_name or real_name == "GC=F":
            name = "XAUUSD"
        else:
            name = real_name
        emoji="🟢" if a['verdict']=="BUY" else "🔴" if a['verdict']=="SELL" else "🟡"
        txt=(
            f"{emoji} {name} | {a['trend']} | {a['verdict']}\n"
            f"Live: {a['live']:.2f} | TF: {user_settings['tf']}\n"
            f"Entry: {a['entry']:.2f} SL: {a['sl']:.2f}\n"
            f"T1: {a['t1']:.2f} T2: {a['t2']:.2f} T3: {a['t3']:.2f}\n"
            f"Sup: {a['support']:.2f} Res: {a['resistance']:.2f}\n\n"
            f"🌈 Devloped by ParthTraderAlerts\n"
            f"💙 —Daily Trading Control —Thankyou."
        )
        await update.message.reply_text(txt)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(get_dashboard_text() + f"\n\nPairs: {', '.join(custom_symbols)}")

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    if not context.args:
        await update.message.reply_text("Use: /add ETH-USD"); return
    sym=normalize_symbol(context.args[0]); custom_symbols.add(sym)
    await update.message.reply_text(f"Added {sym}\n{get_dashboard_text()}")

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    if not context.args: return
    sym=normalize_symbol(context.args[0]); custom_symbols.discard(sym)
    await update.message.reply_text(f"Removed {sym}")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(f"Pairs: {', '.join(custom_symbols)}")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    custom_symbols.clear()
    await update.message.reply_text("Sab clear")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    custom_symbols.clear(); custom_symbols.update(DEFAULT_SYMBOLS)
    await update.message.reply_text(f"Reset Done\n{get_dashboard_text()}")

async def tf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    if context.args and context.args[0] in TF_MAP:
        user_settings['tf']=context.args[0]
        kb = [[InlineKeyboardButton("🔍 /scan - One Click Scan", callback_data="scan")]]
        await update.message.reply_text(f"Timeframe set [{user_settings['tf']}] ab scan Karo /scan", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text("Use: /tf 1m 3m 5m 15m 30m 1h 2h 4h 1d 1W")

async def pivot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    if context.args:
        user_settings['pivot']=int(context.args[0])
        await update.message.reply_text(f"Pivot {user_settings['pivot']} set")
    else:
        await update.message.reply_text(f"Pivot: {user_settings['pivot']}")

async def rr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    if context.args:
        user_settings['rr']=float(context.args[0])
        await update.message.reply_text(f"RR {user_settings['rr']} set")
    else:
        await update.message.reply_text(f"RR: {user_settings['rr']}")

# --- ADMIN ONLY COMMANDS ---
async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id)!= str(ADMIN_ID):
        await update.message.reply_text("Owner only"); return
    if not context.args:
        await update.message.reply_text("Use: /allow USER_ID"); return
    uid = int(context.args[0])
    allowed_users.add(uid)
    save_json(ALLOWED_FILE, list(allowed_users))
    await update.message.reply_text(f"Access granted to {uid}")

async def deny_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id)!= str(ADMIN_ID):
        await update.message.reply_text("Owner only"); return
    if not context.args:
        await update.message.reply_text("Use: /deny USER_ID"); return
    uid = int(context.args[0])
    allowed_users.discard(uid)
    save_json(ALLOWED_FILE, list(allowed_users))
    await update.message.reply_text(f"Access removed {uid}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id)!= str(ADMIN_ID):
        await update.message.reply_text("Owner only - Admin only"); return
    if not users_data:
        await update.message.reply_text("No users yet"); return
    msg = f"👥 Users List - IST Time\nTotal: {len(users_data)}\n\n"
    for uid, info in list(users_data.items())[-30:]: # last 30
        left_info = ""
        if "left_at" in info:
            left_time = datetime.fromtimestamp(info["left_at"], IST).strftime("%d-%m %I:%M %p")
            left_info = f" | Left: {left_time}"
        msg += f"ID:{info.get('id')} {info.get('name')} {info.get('username')}\n First: {info.get('first_seen')}\n Last: {info.get('last_seen')}{left_info}\n\n"
    # Telegram limit 4096
    if len(msg) > 4000:
        msg = msg[:4000]
    await update.message.reply_text(msg)

async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    # check access for button too
    dummy_update = update
    dummy_update.message = q.message
    # quick access check without sending duplicate msg
    user = q.from_user
    if str(user.id)!= str(ADMIN_ID) and int(user.id) not in allowed_users:
        try:
            member = await application.bot.get_chat_member(GROUP_ID, user.id)
            if member.status in ['left','kicked']:
                await q.message.reply_text(f"You left group so please Join group and use this bot\n{GROUP_LINK}")
                return
        except: pass

    if q.data=="scan":
        await scan_cmd(dummy_update, context)
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
application.add_handler(CommandHandler("allow", allow_cmd))
application.add_handler(CommandHandler("deny", deny_cmd))
application.add_handler(CommandHandler("users", users_cmd))
application.add_handler(CallbackQueryHandler(button_cb))

@app.route('/')
def home(): return "Bot Secure OK - DTC Trader"

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    Thread(target=lambda: app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    application.run_polling(drop_pending_updates=True)
