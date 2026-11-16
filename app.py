# -*- coding: utf-8 -*-
"""
Telegram points shop for Render Web Service.
Required environment variables:
  BOT_TOKEN  - Telegram bot token
  OWNER_ID   - Telegram numeric user ID of the primary owner
Optional:
  ADMIN_IDS  - comma-separated additional admin IDs
  DB_PATH    - SQLite path (default: /var/data/shop.db if available, otherwise shop.db)
"""
import os
import time
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, jsonify
import telebot
from telebot import types

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "/var/data/shop.db" if os.path.isdir("/var/data") else "shop.db")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render Environment Variables.")
if not OWNER_ID:
    raise RuntimeError("Set OWNER_ID to your Telegram numeric user ID.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("points-shop")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)
app = Flask(__name__)
db_lock = threading.RLock()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          points INTEGER NOT NULL DEFAULT 0, referred_by INTEGER,
          joined_at TEXT DEFAULT CURRENT_TIMESTAMP, last_daily TEXT,
          banned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          description TEXT DEFAULT '', price INTEGER NOT NULL CHECK(price>=0),
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS stock(
          id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
          secret TEXT NOT NULL, sold_to INTEGER, sold_at TEXT,
          FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS purchases(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          product_id INTEGER NOT NULL, stock_id INTEGER NOT NULL,
          purchased_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS roles(
          user_id INTEGER PRIMARY KEY, role TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS referrals(
          invited_id INTEGER PRIMARY KEY, inviter_id INTEGER NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        c.execute("INSERT OR IGNORE INTO roles(user_id,role) VALUES(?, 'owner')", (OWNER_ID,))
        for uid in ADMIN_IDS:
            c.execute("INSERT OR IGNORE INTO roles(user_id,role) VALUES(?, 'admin')", (uid,))

def get_role(uid):
    with db_lock, db() as c:
        r = c.execute("SELECT role FROM roles WHERE user_id=?", (uid,)).fetchone()
        return r["role"] if r else ""

def is_staff(uid):
    return get_role(uid) in ("owner", "co_owner", "admin", "moderator")

def can_manage_shop(uid):
    return get_role(uid) in ("owner", "co_owner", "admin")

def touch_user(u):
    with db_lock, db() as c:
        c.execute("""INSERT INTO users(user_id,username,first_name) VALUES(?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
          (u.id, u.username or "", u.first_name or ""))

def user_row(uid):
    with db_lock, db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def is_banned(uid):
    r = user_row(uid)
    return bool(r and r["banned"])

def send_admins(text):
    with db_lock, db() as c:
        ids = [x["user_id"] for x in c.execute("SELECT user_id FROM roles WHERE role IN ('owner','co_owner','admin','moderator')")]
    for uid in set(ids):
        try: bot.send_message(uid, text)
        except Exception: pass

def main_keyboard():
    k = types.ReplyKeyboardMarkup(resize_keyboard=True)
    k.row("🛍 المتجر", "👤 حسابي")
    k.row("🎁 الهدية اليومية", "🔗 الإحالة")
    k.row("🏆 المتصدرين", "📦 مشترياتي")
    k.row("ℹ️ المساعدة")
    return k

def staff_keyboard():
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("➕ إضافة سلعة", callback_data="adm:add_product"),
          types.InlineKeyboardButton("📦 إدارة السلع", callback_data="adm:products"))
    k.add(types.InlineKeyboardButton("➕ إضافة مفاتيح", callback_data="adm:add_stock"),
          types.InlineKeyboardButton("🗑 حذف مفتاح", callback_data="adm:remove_stock"))
    k.add(types.InlineKeyboardButton("👥 المستخدمون", callback_data="adm:users"),
          types.InlineKeyboardButton("📊 الإحصائيات", callback_data="adm:stats"))
    k.add(types.InlineKeyboardButton("➕ نقاط لمستخدم", callback_data="adm:give"),
          types.InlineKeyboardButton("➖ خصم نقاط", callback_data="adm:take"))
    k.add(types.InlineKeyboardButton("🚫 حظر مستخدم", callback_data="adm:ban"),
          types.InlineKeyboardButton("✅ فك حظر", callback_data="adm:unban"))
    if get_role(OWNER_ID) == "owner":
        k.add(types.InlineKeyboardButton("🛡 إدارة الرتب", callback_data="adm:roles"))
    return k

def set_state(uid, state):
    # In-memory admin conversation states; reset on restart.
    STATES[uid] = state
STATES = {}

def ask_next(message, prompt, state):
    set_state(message.from_user.id, state)
    bot.send_message(message.chat.id, prompt, reply_markup=types.ReplyKeyboardRemove())

def safe_int(s, minimum=0):
    try:
        n = int(s.strip())
        return n if n >= minimum else None
    except Exception:
        return None

def stock_count(pid):
    with db_lock, db() as c:
        return c.execute("SELECT COUNT(*) n FROM stock WHERE product_id=? AND sold_to IS NULL", (pid,)).fetchone()["n"]

def notify_low_stock(product_name):
    send_admins(f"⚠️ <b>نفد مخزون السلعة:</b> {html_escape(product_name)}\\nأضف مفاتيح جديدة من لوحة الإدارة.")

def html_escape(s):
    import html
    return html.escape(str(s or ""))

def show_shop(chat_id):
    with db_lock, db() as c:
        rows = c.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    if not rows:
        bot.send_message(chat_id, "🛍 المتجر فارغ حالياً، انتظر إضافة سلع جديدة.", reply_markup=main_keyboard()); return
    k = types.InlineKeyboardMarkup(row_width=1)
    lines = ["<b>🛍 متجر المفاتيح</b>", ""]
    for p in rows:
        count = stock_count(p["id"])
        lines.append(f"🔹 <b>{html_escape(p['name'])}</b> — {p['price']} نقطة\\n📦 المتوفر: {count}")
        if p["description"]: lines.append(f"📝 {html_escape(p['description'])}")
        k.add(types.InlineKeyboardButton(f"شراء {p['name']} • {p['price']} نقطة", callback_data=f"buy:{p['id']}"))
    bot.send_message(chat_id, "\\n".join(lines), reply_markup=k)

def register_user(message):
    u = message.from_user
    with db_lock, db() as c:
        exists = c.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not exists:
            c.execute("INSERT INTO users(user_id,username,first_name) VALUES(?,?,?)", (u.id,u.username or "",u.first_name or ""))
        else:
            c.execute("UPDATE users SET username=?,first_name=? WHERE user_id=?", (u.username or "",u.first_name or "",u.id))

def handle_start(message):
    register_user(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try: inviter = int(parts[1][4:])
        except ValueError: inviter = 0
        if inviter and inviter != message.from_user.id:
            with db_lock, db() as c:
                fresh = c.execute("SELECT referred_by FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
                if fresh and fresh["referred_by"] is None:
                    valid = c.execute("SELECT user_id FROM users WHERE user_id=?", (inviter,)).fetchone()
                    if valid:
                        c.execute("UPDATE users SET referred_by=? WHERE user_id=?", (inviter,message.from_user.id))
                        c.execute("UPDATE users SET points=points+1 WHERE user_id=?", (inviter,))
                        c.execute("UPDATE users SET points=points+1 WHERE user_id=?", (message.from_user.id,))
                        c.execute("INSERT OR IGNORE INTO referrals(invited_id,inviter_id) VALUES(?,?)", (message.from_user.id,inviter))
                        try: bot.send_message(inviter, "🎉 دخل شخص من رابطك! حصلت على نقطة إحالة واحدة.")
                        except Exception: pass
    if is_banned(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 حسابك محظور من استخدام المتجر."); return
    bot.send_message(message.chat.id, f"أهلاً <b>{html_escape(message.from_user.first_name)}</b> في متجر المفاتيح!\\nاختر من الأزرار بالأسفل 👇", reply_markup=main_keyboard())

@bot.message_handler(commands=["start"])
def start_cmd(m): handle_start(m)

@bot.message_handler(commands=["help"])
def help_cmd(m):
    if is_banned(m.from_user.id): return
    bot.send_message(m.chat.id, "الأوامر: /start — /shop — /daily — /ref — /top — /myid\\nالإدارة: /admin (للمخولين فقط).", reply_markup=main_keyboard())

@bot.message_handler(commands=["myid"])
def myid_cmd(m): bot.reply_to(m, f"🆔 ID: <code>{m.from_user.id}</code>")

@bot.message_handler(commands=["shop"])
def shop_cmd(m):
    if not is_banned(m.from_user.id): show_shop(m.chat.id)

@bot.message_handler(commands=["daily"])
def daily_cmd(m): daily(m)

@bot.message_handler(commands=["ref"])
def ref_cmd(m): referral(m)

@bot.message_handler(commands=["top"])
def top_cmd(m): leaderboard(m.chat.id)

@bot.message_handler(commands=["admin"])
def admin_cmd(m):
    if not is_staff(m.from_user.id): return
    bot.send_message(m.chat.id, "🛠 <b>لوحة الإدارة</b>\\nاختر العملية:", reply_markup=staff_keyboard())

@bot.message_handler(commands=["owner"])
def owner_cmd(m):
    if get_role(m.from_user.id) != "owner": return
    bot.send_message(m.chat.id, "👑 أنت المالك الرئيسي. استخدم /admin لإدارة المتجر.")

@bot.message_handler(commands=["coowner"])
def coowner_cmd(m):
    if get_role(m.from_user.id) not in ("owner","co_owner"): return
    bot.send_message(m.chat.id, "👑 رتبة المالك المساعد تتيح إدارة المتجر والرتب عبر لوحة الإدارة.")

@bot.message_handler(commands=["staff"])
def staff_cmd(m):
    if not is_staff(m.from_user.id): return
    bot.send_message(m.chat.id, "🛡 لوحة الموظفين:", reply_markup=staff_keyboard())

@bot.message_handler(commands=["cancel"])
def cancel_cmd(m):
    STATES.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "تم إلغاء العملية.", reply_markup=main_keyboard())

def daily(m):
    if is_banned(m.from_user.id): return
    today = datetime.now(timezone.utc).date().isoformat()
    with db_lock, db() as c:
        row = c.execute("SELECT last_daily FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
        if not row:
            register_user(m)
            row = {"last_daily": None}
        if row["last_daily"] == today:
            bot.send_message(m.chat.id, "⏳ أخذت هديتك اليوم! ارجع بعد تجدد اليوم.", reply_markup=main_keyboard()); return
        c.execute("UPDATE users SET points=points+1,last_daily=? WHERE user_id=?", (today,m.from_user.id))
    bot.send_message(m.chat.id, "🎁 مبروك! حصلت على نقطة يومية واحدة.", reply_markup=main_keyboard())

def referral(m):
    if is_banned(m.from_user.id): return
    me = bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{m.from_user.id}"
    with db_lock, db() as c:
        count = c.execute("SELECT COUNT(*) n FROM referrals WHERE inviter_id=?", (m.from_user.id,)).fetchone()["n"]
    bot.send_message(m.chat.id, f"🔗 <b>رابط إحالتك:</b>\\n<code>{link}</code>\\n👥 عدد إحالاتك: {count}\\n🎁 كل مستخدم جديد يدخل من رابطك يمنحك نقطة ويمنحه نقطة.", reply_markup=main_keyboard())

def leaderboard(chat_id):
    with db_lock, db() as c:
        rows = c.execute("""SELECT u.user_id,u.username,u.first_name,COUNT(r.invited_id) refs
          FROM users u LEFT JOIN referrals r ON r.inviter_id=u.user_id
          GROUP BY u.user_id ORDER BY refs DESC, u.points DESC LIMIT 10""").fetchall()
    lines = ["🏆 <b>أفضل 10 في الإحالات</b>", ""]
    for i,r in enumerate(rows,1):
        name = ("@" + r["username"]) if r["username"] else (r["first_name"] or f"مستخدم {r['user_id']}")
        lines.append(f"{i}. {html_escape(name)} — {r['refs']} إحالة")
    bot.send_message(chat_id, "\\n".join(lines), reply_markup=main_keyboard())

def account(m):
    r = user_row(m.from_user.id)
    bot.send_message(m.chat.id, f"👤 <b>حسابك</b>\\n🆔 <code>{m.from_user.id}</code>\\n⭐ نقاطك: <b>{r['points'] if r else 0}</b>\\n🏷 رتبتك: {get_role(m.from_user.id) or 'مستخدم'}", reply_markup=main_keyboard())

def my_purchases(m):
    with db_lock, db() as c:
        rows = c.execute("""SELECT p.name,s.secret,bu.purchased_at FROM purchases bu
          JOIN products p ON p.id=bu.product_id JOIN stock s ON s.id=bu.stock_id
          WHERE bu.user_id=? ORDER BY bu.id DESC LIMIT 30""", (m.from_user.id,)).fetchall()
    if not rows:
        bot.send_message(m.chat.id, "📦 ما عندك مشتريات حتى الآن.", reply_markup=main_keyboard()); return
    lines=["📦 <b>آخر مشترياتك</b>",""]
    for x in rows: lines.append(f"• {html_escape(x['name'])}\\n<code>{html_escape(x['secret'])}</code>\\n")
    bot.send_message(m.chat.id, "\\n".join(lines), reply_markup=main_keyboard())

@bot.callback_query_handler(func=lambda q: q.data.startswith("buy:"))
def buy_callback(q):
    uid=q.from_user.id
    if is_banned(uid):
        bot.answer_callback_query(q.id,"حسابك محظور",show_alert=True); return
    try: pid=int(q.data.split(":")[1])
    except Exception: bot.answer_callback_query(q.id,"طلب غير صالح"); return
    with db_lock, db() as c:
        c.execute("BEGIN IMMEDIATE")
        p=c.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
        u=c.execute("SELECT points FROM users WHERE user_id=?",(uid,)).fetchone()
        s=c.execute("SELECT * FROM stock WHERE product_id=? AND sold_to IS NULL ORDER BY id LIMIT 1",(pid,)).fetchone()
        if not p:
            c.rollback(); bot.answer_callback_query(q.id,"السلعة غير متاحة",show_alert=True); return
        if not s:
            c.rollback(); bot.answer_callback_query(q.id,"نفد المخزون حالياً",show_alert=True)
            notify_low_stock(p["name"]); return
        if not u or u["points"] < p["price"]:
            c.rollback(); bot.answer_callback_query(q.id,"نقاطك غير كافية",show_alert=True); return
        c.execute("UPDATE users SET points=points-? WHERE user_id=?",(p["price"],uid))
        c.execute("UPDATE stock SET sold_to=?,sold_at=CURRENT_TIMESTAMP WHERE id=? AND sold_to IS NULL",(uid,s["id"]))
        c.execute("INSERT INTO purchases(user_id,product_id,stock_id) VALUES(?,?,?)",(uid,pid,s["id"]))
        remaining=c.execute("SELECT COUNT(*) n FROM stock WHERE product_id=? AND sold_to IS NULL",(pid,)).fetchone()["n"]
        c.commit()
    bot.answer_callback_query(q.id,"تم الشراء بنجاح 🎉")
    bot.send_message(uid,f"✅ <b>تم شراء {html_escape(p['name'])}</b>\\n🔑 مفتاحك الخاص:\\n<code>{html_escape(s['secret'])}</code>\\n⭐ المتبقي من نقاطك: {u['points']-p['price']}", reply_markup=main_keyboard())
    if remaining == 0: notify_low_stock(p["name"])

@bot.callback_query_handler(func=lambda q: q.data.startswith("adm:"))
def admin_callback(q):
    uid=q.from_user.id
    if not is_staff(uid):
        bot.answer_callback_query(q.id); return
    action=q.data.split(":",1)[1]
    bot.answer_callback_query(q.id)
    if action=="add_product" and can_manage_shop(uid):
        ask_next(q.message,"أرسل اسم السلعة:", "product_name")
    elif action=="products":
        with db_lock, db() as c: rows=c.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        if not rows: bot.send_message(uid,"لا توجد سلع."); return
        k=types.InlineKeyboardMarkup()
        for p in rows:
            k.add(types.InlineKeyboardButton(f"{'🟢' if p['active'] else '⚫'} {p['name']} | {p['price']} نقطة",callback_data=f"toggle:{p['id']}"))
        bot.send_message(uid,"اضغط السلعة لتفعيلها أو إيقافها:",reply_markup=k)
    elif action=="add_stock" and can_manage_shop(uid):
        with db_lock, db() as c: rows=c.execute("SELECT id,name FROM products WHERE active=1").fetchall()
        k=types.InlineKeyboardMarkup()
        for p in rows: k.add(types.InlineKeyboardButton(p["name"],callback_data=f"stockfor:{p['id']}"))
        bot.send_message(uid,"اختر السلعة لإضافة المفاتيح:",reply_markup=k)
    elif action=="remove_stock" and can_manage_shop(uid):
        with db_lock, db() as c: rows=c.execute("SELECT id,name FROM products WHERE active=1").fetchall()
        k=types.InlineKeyboardMarkup()
        for p in rows: k.add(types.InlineKeyboardButton(p["name"],callback_data=f"rmstock:{p['id']}"))
        bot.send_message(uid,"اختر السلعة لحذف آخر مفتاح غير مباع:",reply_markup=k)
    elif action in ("give","take"):
        ask_next(q.message,"أرسل ID المستخدم ثم مسافة ثم عدد النقاط. مثال: 123456789 5",action+"_points")
    elif action in ("ban","unban"):
        ask_next(q.message,"أرسل ID المستخدم:",action+"_user")
    elif action=="stats":
        with db_lock, db() as c:
            users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            pts=c.execute("SELECT COALESCE(SUM(points),0) n FROM users").fetchone()["n"]
            sold=c.execute("SELECT COUNT(*) n FROM purchases").fetchone()["n"]
            stock=c.execute("SELECT COUNT(*) n FROM stock WHERE sold_to IS NULL").fetchone()["n"]
        bot.send_message(uid,f"📊 <b>الإحصائيات</b>\\n👥 المستخدمون: {users}\\n⭐ مجموع النقاط: {pts}\\n🛒 المبيعات: {sold}\\n🔑 المفاتيح المتوفرة: {stock}")
    elif action=="users":
        with db_lock, db() as c: rows=c.execute("SELECT user_id,username,points FROM users ORDER BY joined_at DESC LIMIT 20").fetchall()
        bot.send_message(uid,"👥 <b>آخر المستخدمين</b>\\n"+"\\n".join(f"{r['user_id']} | @{r['username']} | {r['points']} نقطة" for r in rows))
    elif action=="roles" and get_role(uid)=="owner":
        ask_next(q.message,"أرسل: ID الرتبة (admin / moderator / co_owner / user). مثال: 123456789 admin", "set_role")

@bot.callback_query_handler(func=lambda q: q.data.startswith(("stockfor:","rmstock:","toggle:")))
def product_admin_callback(q):
    uid=q.from_user.id
    if not can_manage_shop(uid): bot.answer_callback_query(q.id); return
    action,pid=q.data.split(":"); pid=int(pid)
    if action=="stockfor":
        STATES[uid]=f"stock:{pid}"
        bot.answer_callback_query(q.id)
        bot.send_message(uid,"أرسل المفاتيح، مفتاح واحد في كل سطر:")
    elif action=="rmstock":
        with db_lock, db() as c:
            r=c.execute("SELECT id,secret FROM stock WHERE product_id=? AND sold_to IS NULL ORDER BY id DESC LIMIT 1",(pid,)).fetchone()
            if r: c.execute("DELETE FROM stock WHERE id=?",(r["id"],))
        bot.answer_callback_query(q.id,"تم حذف مفتاح" if r else "لا يوجد مخزون",show_alert=True)
    else:
        with db_lock, db() as c:
            c.execute("UPDATE products SET active=1-active WHERE id=?",(pid,))
            r=c.execute("SELECT active FROM products WHERE id=?",(pid,)).fetchone()
        bot.answer_callback_query(q.id,"تم تحديث حالة السلعة")
        bot.send_message(uid,"الحالة: مفعلة" if r and r["active"] else "الحالة: متوقفة")

@bot.message_handler(func=lambda m: m.from_user.id in STATES, content_types=["text"])
def state_handler(m):
    uid=m.from_user.id
    if not is_staff(uid):
        STATES.pop(uid,None); return
    state=STATES.get(uid,"")
    value=(m.text or "").strip()
    if value=="/cancel":
        STATES.pop(uid,None); bot.send_message(m.chat.id,"ألغيت العملية.",reply_markup=main_keyboard()); return
    if state=="product_name":
        if not can_manage_shop(uid): STATES.pop(uid,None); return
        STATES[uid]={"step":"product_desc","name":value}
        bot.send_message(m.chat.id,"أرسل وصف السلعة (أو اكتب - بدون وصف):"); return
    if isinstance(state,dict) and state.get("step")=="product_desc":
        state["desc"]="" if value=="-" else value
        STATES[uid]={"step":"product_price","data":state}
        bot.send_message(m.chat.id,"أرسل سعر السلعة بالنقاط (رقم صحيح):"); return
    if isinstance(state,dict) and state.get("step")=="product_price":
        price=safe_int(value,0)
        if price is None: bot.send_message(m.chat.id,"السعر لازم يكون رقماً صحيحاً. أعد الإرسال:"); return
        d=state["data"]
        with db_lock, db() as c:
            c.execute("INSERT INTO products(name,description,price) VALUES(?,?,?)",(d["name"],d["desc"],price))
            pid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        STATES.pop(uid,None)
        bot.send_message(m.chat.id,f"✅ تمت إضافة السلعة #{pid}. أضف مفاتيحها من لوحة الإدارة.",reply_markup=main_keyboard()); return
    if state.startswith("stock:"):
        pid=int(state.split(":")[1])
        keys=[x.strip() for x in value.splitlines() if x.strip()]
        if not keys: bot.send_message(m.chat.id,"لم أجد مفاتيح. أرسل كل مفتاح في سطر."); return
        with db_lock, db() as c:
            c.executemany("INSERT INTO stock(product_id,secret) VALUES(?,?)",[(pid,x) for x in keys])
        STATES.pop(uid,None)
        bot.send_message(m.chat.id,f"✅ تمت إضافة {len(keys)} مفتاح للمخزون.",reply_markup=main_keyboard()); return
    if state in ("give_points","take_points"):
        parts=value.split()
        if len(parts)!=2 or not parts[0].isdigit() or safe_int(parts[1],1) is None:
            bot.send_message(m.chat.id,"الصيغة غير صحيحة. مثال: 123456789 5"); return
        target,amount=int(parts[0]),int(parts[1])
        with db_lock, db() as c:
            exists=c.execute("SELECT user_id FROM users WHERE user_id=?",(target,)).fetchone()
            if not exists: bot.send_message(m.chat.id,"المستخدم غير مسجل في البوت."); return
            delta=amount if state=="give_points" else -amount
            if delta<0:
                points=c.execute("SELECT points FROM users WHERE user_id=?",(target,)).fetchone()["points"]
                if points<amount: bot.send_message(m.chat.id,"رصيد المستخدم أقل من المبلغ."); return
            c.execute("UPDATE users SET points=points+? WHERE user_id=?",(delta,target))
        STATES.pop(uid,None)
        bot.send_message(m.chat.id,"✅ تم تحديث النقاط.",reply_markup=main_keyboard())
        try: bot.send_message(target,f"⭐ تم تعديل رصيدك بواسطة الإدارة: {'+' if delta>0 else ''}{delta} نقطة.")
        except Exception: pass
        return
    if state in ("ban_user","unban_user"):
        target=safe_int(value,1)
        if target is None: bot.send_message(m.chat.id,"أرسل ID صحيحاً."); return
        with db_lock, db() as c: c.execute("UPDATE users SET banned=? WHERE user_id=?",(1 if state=="ban_user" else 0,target))
        STATES.pop(uid,None); bot.send_message(m.chat.id,"تم تنفيذ العملية.",reply_markup=main_keyboard()); return
    if state=="set_role":
        if get_role(uid)!="owner": STATES.pop(uid,None); return
        parts=value.split()
        if len(parts)!=2 or not parts[0].isdigit() or parts[1] not in ("admin","moderator","co_owner","user"):
            bot.send_message(m.chat.id,"الصيغة: ID admin أو moderator أو co_owner أو user"); return
        target,role=int(parts[0]),parts[1]
        if target==OWNER_ID: bot.send_message(m.chat.id,"لا يمكن تغيير رتبة المالك الرئيسي."); return
        with db_lock, db() as c:
            if role=="user": c.execute("DELETE FROM roles WHERE user_id=?",(target,))
            else: c.execute("INSERT INTO roles(user_id,role) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",(target,role))
        STATES.pop(uid,None); bot.send_message(m.chat.id,f"✅ تم تعيين الرتبة: {role}",reply_markup=main_keyboard()); return

@bot.message_handler(func=lambda m: True, content_types=["text"])
def text_router(m):
    if is_banned(m.from_user.id): return
    register_user(m)
    t=(m.text or "").strip()
    if t=="🛍 المتجر": show_shop(m.chat.id)
    elif t=="👤 حسابي": account(m)
    elif t=="🎁 الهدية اليومية": daily(m)
    elif t=="🔗 الإحالة": referral(m)
    elif t=="🏆 المتصدرين": leaderboard(m.chat.id)
    elif t=="📦 مشترياتي": my_purchases(m)
    elif t=="ℹ️ المساعدة": help_cmd(m)

@app.callback_query_handler(func=lambda q: True)
def unknown_callback(q):
    try: bot.answer_callback_query(q.id)
    except Exception: pass

@app.get("/")
def health():
    return jsonify({"status":"ok","service":"telegram-points-shop","time":int(time.time())}), 200

@app.get("/health")
def health2(): return "OK", 200

def run_bot():
    while True:
        try:
            log.info("Starting Telegram polling")
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
        except Exception:
            log.exception("Polling crashed; retrying in 5 seconds")
            time.sleep(5)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
