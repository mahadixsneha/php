# -*- coding: utf-8 -*-
"""
PHP Hosting Bot v1.0
Telegram bot for hosting PHP, HTML, ZIP sites
Runs on Render Free Tier
MongoDB Atlas for database
"""

import os, re, csv, time, secrets, zipfile, shutil, io, json
import logging, requests, mimetypes, bson, subprocess, hashlib
from flask import Flask, request, redirect, session, make_response, Response
import telebot
from telebot import types
from threading import Thread
from datetime import datetime, timedelta
from functools import wraps
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================= CONFIG =================
TOKEN            = os.getenv("BOT_TOKEN")
OWNER_ID         = int(os.getenv("OWNER_ID", "7936924851"))
DOMAIN           = os.getenv("DOMAIN", "https://php-verj.onrender.com")
MONGO_URI        = os.getenv("MONGO_URI", "mongodb+srv://phpbot:1EstERAbeipLfUCc@cluster0.pklx1eg.mongodb.net/?appName=Cluster0")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", secrets.token_hex(16))
USE_WEBHOOK      = bool(os.getenv("USE_WEBHOOK", ""))
MAX_FILE_MB      = 25
MAX_FILE_BYTES   = MAX_FILE_MB * 1024 * 1024
FREE_LIMIT       = 3
PREMIUM_LIMIT    = 50
PHP_TIMEOUT      = 10   # seconds
PHP_MEMORY_LIMIT = "32M"
PHP_MAX_EXEC     = "10"

SUPPORTED_EXT = ['php', 'html', 'htm', 'zip', 'jpg', 'jpeg', 'png', 'gif', 'webp', 'mp4', 'mp3', 'pdf', 'css', 'js']
PHP_EXT       = ['php', 'phtml', 'php3', 'php4', 'php5', 'php7']

# ================= PHP SANDBOX INI =================
PHP_SANDBOX_INI = """
[PHP]
disable_functions = exec,system,shell_exec,passthru,popen,proc_open,proc_close,
    curl_exec,curl_multi_exec,parse_ini_file,show_source,
    pcntl_alarm,pcntl_fork,pcntl_waitpid,pcntl_wait,
    pcntl_wifexited,pcntl_wifstopped,pcntl_wifsignaled,
    pcntl_wifcontinued,pcntl_wexitstatus,pcntl_wtermsig,
    pcntl_wstopsig,pcntl_signal,pcntl_signal_get_handler,
    pcntl_signal_dispatch,pcntl_get_last_error,pcntl_strerror,
    pcntl_sigprocmask,pcntl_sigwaitinfo,pcntl_sigtimedwait,
    pcntl_getpriority,pcntl_setpriority,pcntl_async_signals,
    pcntl_unshare,socket_create,socket_connect,fsockopen,pfsockopen
allow_url_fopen  = Off
allow_url_include = Off
memory_limit     = {mem}
max_execution_time = {exec}
display_errors   = On
log_errors       = Off
expose_php       = Off
open_basedir     = {basedir}
""".strip()

BASE       = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE, "sites")
INI_DIR    = os.path.join(BASE, "php_ini")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(INI_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ================= MONGODB =================
try:
    _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    _client.server_info()
    mdb = _client.get_default_database() if '/' in MONGO_URI.split('@')[-1] else _client['phpbot']
    logger.info("✅ MongoDB connected!")
except Exception as e:
    logger.error(f"❌ MongoDB: {e}")
    raise SystemExit("MongoDB connect করা যায়নি!")

col_users    = mdb["users"]
col_files    = mdb["files"]
col_premium  = mdb["premium"]
col_settings = mdb["settings"]
col_views    = mdb["site_views"]
col_pay_req  = mdb["payment_requests"]
col_logs     = mdb["bot_logs"]
col_admins   = mdb["admins"]
col_channels = mdb["force_channels"]
col_short    = mdb["short_urls"]
col_pay_meth = mdb["payment_methods"]

col_users.create_index("id", unique=True)
col_files.create_index("code", unique=True)
col_files.create_index("user_id")
col_files.create_index("slug", sparse=True)
col_premium.create_index("user_id", unique=True)
col_settings.create_index("key", unique=True)
col_admins.create_index("id", unique=True)
col_channels.create_index("username", unique=True)
col_short.create_index("code", unique=True)
col_admins.update_one({"id": OWNER_ID}, {"$set": {"id": OWNER_ID}}, upsert=True)

# ================= DB HELPERS =================
def sg(key, default=None):
    r = col_settings.find_one({"key": key})
    return r["value"] if r else default

def ss(key, value):
    col_settings.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

def is_admin(uid):    return bool(col_admins.find_one({"id": uid}))
def is_banned(uid):   return bool(col_settings.find_one({"key": f"ban_{uid}"}))
def is_maintenance(): return col_settings.find_one({"key": "maintenance"}) and col_settings.find_one({"key": "maintenance"}).get("value") == "on"

def is_premium(uid):
    p = col_premium.find_one({"user_id": uid})
    if p:
        try:
            if datetime.fromisoformat(p["expiry"]) > datetime.now(): return True
        except: pass
        col_premium.delete_one({"user_id": uid})
    return False

def get_limit(uid):
    if is_admin(uid): return 9999
    if is_premium(uid):
        p = col_premium.find_one({"user_id": uid}, {"plan": 1})
        plan = p.get("plan") if p else None
        v = sg(f"limit_{plan}") if plan else None
        return int(v) if v else int(sg("premium_limit", PREMIUM_LIMIT))
    return int(sg("free_limit", FREE_LIMIT))

def gen_code():
    while True:
        c = secrets.token_hex(4)
        if not col_files.find_one({"code": c}): return c

def gen_url_code():
    while True:
        c = secrets.token_urlsafe(5)[:6]
        if not col_short.find_one({"code": c}): return c

def log_action(uid, action, detail=""):
    col_logs.insert_one({"user_id": uid, "action": action, "detail": detail[:300],
                         "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

def safe_del(cid, mid):
    try: bot.delete_message(cid, mid)
    except: pass

def fmt_bytes(size):
    for u in ['B','KB','MB','GB']:
        if size < 1024: return f"{size:.1f} {u}"
        size /= 1024
    return f"{size:.1f} TB"

def get_storage():
    total = 0
    for root, dirs, files in os.walk(UPLOAD_DIR):
        for f in files:
            try: total += os.path.getsize(os.path.join(root, f))
            except: pass
    return total

def check_join(uid):
    for ch in col_channels.find():
        try:
            if bot.get_chat_member(f"@{ch['username']}", uid).status in ["left","kicked"]:
                return False
        except: continue
    return True

def get_bot_username():
    try: return bot.get_me().username
    except: return "phpbot"

# ================= PHP HELPERS =================
def check_php():
    """PHP installed কিনা চেক করুন"""
    try:
        r = subprocess.run(['php', '-v'], capture_output=True, timeout=5)
        return r.returncode == 0
    except: return False

def create_sandbox_ini(user_dir):
    """ইউজার-নির্দিষ্ট secure PHP ini তৈরি করুন"""
    ini_path = os.path.join(INI_DIR, f"user_{hashlib.md5(user_dir.encode()).hexdigest()}.ini")
    content = PHP_SANDBOX_INI.format(
        mem=PHP_MEMORY_LIMIT,
        exec=PHP_MAX_EXEC,
        basedir=user_dir
    )
    with open(ini_path, 'w') as f:
        f.write(content)
    return ini_path

def execute_php(php_file, site_dir, query_string="", post_data=b"", method="GET", extra_headers=None):
    """PHP ফাইল সুরক্ষিতভাবে Execute করুন"""
    if not os.path.exists(php_file):
        return b"<h1>404</h1>", 404

    ini_path = create_sandbox_ini(site_dir)
    env = os.environ.copy()
    env.update({
        "REDIRECT_STATUS":   "200",
        "SCRIPT_FILENAME":   php_file,
        "SCRIPT_NAME":       "/" + os.path.basename(php_file),
        "REQUEST_METHOD":    method,
        "QUERY_STRING":      query_string,
        "CONTENT_TYPE":      (extra_headers or {}).get("Content-Type", ""),
        "CONTENT_LENGTH":    str(len(post_data)),
        "SERVER_NAME":       DOMAIN.replace("https://","").replace("http://",""),
        "SERVER_PORT":       "443",
        "HTTPS":             "on",
        "PHP_SELF":          "/" + os.path.basename(php_file),
        "HTTP_HOST":         DOMAIN.replace("https://","").replace("http://",""),
        "DOCUMENT_ROOT":     site_dir,
        "GATEWAY_INTERFACE": "CGI/1.1",
        "SERVER_PROTOCOL":   "HTTP/1.1",
    })
    if extra_headers:
        for k, v in extra_headers.items():
            env[f"HTTP_{k.upper().replace('-','_')}"] = v

    try:
        result = subprocess.run(
            ['php-cgi', '-c', ini_path, php_file],
            input=post_data,
            capture_output=True,
            timeout=PHP_TIMEOUT,
            cwd=site_dir,
            env=env
        )
        output = result.stdout
    except FileNotFoundError:
        # php-cgi না থাকলে php cli ব্যবহার করুন
        try:
            result = subprocess.run(
                ['php', '-c', ini_path, '-f', php_file],
                input=post_data,
                capture_output=True,
                timeout=PHP_TIMEOUT,
                cwd=site_dir,
                env=env
            )
            output = result.stdout
        except Exception as e:
            return f"<h3>❌ PHP Error: {str(e)[:200]}</h3>".encode(), 500
    except subprocess.TimeoutExpired:
        return "<h3>PHP Execution Timeout (10s)</h3>".encode(), 408
    except Exception as e:
        return f"<h3>❌ Error: {str(e)[:200]}</h3>".encode(), 500

    # CGI header parse করুন
    if b"\r\n\r\n" in output:
        header_part, body = output.split(b"\r\n\r\n", 1)
    elif b"\n\n" in output:
        header_part, body = output.split(b"\n\n", 1)
    else:
        body = output
        header_part = b""

    return body, 200

def scan_php_threats(content):
    """PHP ফাইলে বিপজ্জনক কোড চেক করুন"""
    danger = [
        (r'exec\s*\(', '⚠️ exec()'),
        (r'system\s*\(', '⚠️ system()'),
        (r'shell_exec\s*\(', '⚠️ shell_exec()'),
        (r'passthru\s*\(', '⚠️ passthru()'),
        (r'proc_open\s*\(', '⚠️ proc_open()'),
        (r'popen\s*\(', '⚠️ popen()'),
        (r'base64_decode\s*\(.*\beval\b', '🔐 Encoded eval'),
        (r'\beval\s*\(\s*\$', '🔐 Dynamic eval'),
        (r'file_put_contents.*\.php', '📝 PHP লেখার চেষ্টা'),
        (r'move_uploaded_file', '📤 File upload bypass'),
        (r'\$_(GET|POST|REQUEST|COOKIE)\s*\[.*\]\s*\)', '💉 Unfiltered input'),
    ]
    text = content.decode('utf-8', errors='ignore')
    threats = [label for pattern, label in danger if re.search(pattern, text, re.IGNORECASE)]
    return len(threats) == 0, threats

# ================= BANNED CHECK =================
def banned_check(func):
    @wraps(func)
    def wrapper(msg_or_call, *args, **kwargs):
        uid = msg_or_call.from_user.id
        if is_banned(uid): return
        if is_maintenance() and not is_admin(uid):
            text = "🔧 বট মেইনটেন্যান্সে আছে।"
            if hasattr(msg_or_call, 'message'):
                bot.answer_callback_query(msg_or_call.id, text, show_alert=True)
            else:
                bot.reply_to(msg_or_call, text)
            return
        return func(msg_or_call, *args, **kwargs)
    return wrapper

# ================= MAIN MENU =================
def main_menu(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    items = ["📤 ফাইল আপলোড", "📂 আমার সাইট", "👤 একাউন্ট",
             "💎 প্রিমিয়াম", "🔗 Short URL", "❓ সাহায্য"]
    kb.add(*[types.KeyboardButton(i) for i in items])
    return kb

# ================= START =================
@bot.message_handler(commands=["start"])
def start(msg):
    uid = msg.from_user.id
    if is_banned(uid): return
    if is_maintenance() and not is_admin(uid):
        bot.reply_to(msg, "🔧 বট মেইনটেন্যান্সে আছে।"); return

    username = msg.from_user.username or ""
    first    = msg.from_user.first_name or ""

    # Referral
    args = msg.text.split()
    if len(args) > 1 and args[1].isdigit():
        ref_id = int(args[1])
        if ref_id != uid and not col_users.find_one({"id": uid}):
            col_users.update_one({"id": ref_id}, {"$inc": {"invites": 1}})
            inv = (col_users.find_one({"id": ref_id}) or {}).get("invites", 0)
            if inv > 0 and inv % int(sg("ref_required", 3)) == 0:
                expiry = (datetime.now() + timedelta(days=int(sg("ref_days", 7)))).isoformat()
                col_premium.update_one({"user_id": ref_id}, {"$set": {"user_id": ref_id, "plan": "ref", "expiry": expiry}}, upsert=True)
                try: bot.send_message(ref_id, f"🎉 রেফারেল পুরস্কার! {sg('ref_days',7)} দিনের Premium পেয়েছেন!")
                except: pass

    col_users.update_one({"id": uid},
        {"$set": {"username": username, "first_name": first},
         "$setOnInsert": {"id": uid, "joined": datetime.now().strftime("%Y-%m-%d %H:%M"), "invites": 0}},
        upsert=True)

    if not check_join(uid):
        channels = list(col_channels.find())
        kb = types.InlineKeyboardMarkup()
        for ch in channels:
            kb.add(types.InlineKeyboardButton(f"📢 @{ch['username']}", url=f"https://t.me/{ch['username']}"))
        kb.add(types.InlineKeyboardButton("✅ যোগ দিয়েছি", callback_data="verify"))
        bot.send_message(uid, "⚠️ বট ব্যবহার করতে চ্যানেলে যোগ দিন:", reply_markup=kb)
        return

    send_welcome(msg.chat.id, uid)

def send_welcome(chat_id, uid):
    php_ok    = check_php()
    is_prem   = is_premium(uid)
    count     = col_files.count_documents({"user_id": uid})
    limit     = get_limit(uid)
    bar_fill  = int((count / limit) * 10) if limit > 0 else 0
    bar       = "█" * bar_fill + "░" * (10 - bar_fill)
    status    = "💎 Premium" if is_prem else "🆓 Free"
    php_stat  = "✅ চালু" if php_ok else "❌ বন্ধ"
    bu        = get_bot_username()

    text = (
        f"🐘 <b>PHP Hosting Bot</b>\n━━━━━━━━━━━━━━━\n"
        f"├ {status} | 🐘 PHP: {php_stat}\n"
        f"├ 📂 সাইট: <b>{count}/{limit}</b>\n"
        f"└ [{bar}] {count}/{limit}\n\n"
        f"📁 <b>সাপোর্টেড ফরম্যাট:</b>\n"
        f"🐘 PHP  🌐 HTML  📦 ZIP\n"
        f"🖼 ছবি  🎬 ভিডিও  🎵 MP3  📄 PDF\n\n"
        f"🔗 রেফারেল: <code>https://t.me/{bu}?start={uid}</code>"
    )
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📤 আপলোড", callback_data="btn_upload"),
        types.InlineKeyboardButton("📂 আমার সাইট", callback_data="btn_myfiles")
    )
    kb.row(
        types.InlineKeyboardButton("👤 একাউন্ট", callback_data="btn_account"),
        types.InlineKeyboardButton("💎 Premium", callback_data="btn_premium")
    )
    bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    bot.send_message(chat_id, "👇 মেনু থেকে অপশন বেছে নিন:", reply_markup=main_menu(uid))

@bot.callback_query_handler(func=lambda c: c.data == "verify")
def verify_cb(call):
    if check_join(call.from_user.id):
        safe_del(call.message.chat.id, call.message.message_id)
        send_welcome(call.message.chat.id, call.from_user.id)
    else:
        bot.answer_callback_query(call.id, "❌ এখনো যোগ দেননি!", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data in ["btn_upload","btn_myfiles","btn_account","btn_premium"])
@banned_check
def quick_btn(call):
    bot.answer_callback_query(call.id)
    if call.data == "btn_upload":     ask_upload(call.message, call.from_user.id)
    elif call.data == "btn_myfiles":  list_sites(call.message, call.from_user.id)
    elif call.data == "btn_account":  show_account_msg(call.message, call.from_user.id)
    elif call.data == "btn_premium":  show_premium(call.message, call.from_user.id)

# ================= HELP =================
@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda m: m.text == "❓ সাহায্য")
@banned_check
def help_cmd(msg):
    php_ok = check_php()
    bot.send_message(msg.chat.id,
        f"❓ <b>সাহায্য কেন্দ্র</b>\n━━━━━━━━━━━━━━━\n\n"
        f"🐘 PHP Status: {'✅ চালু' if php_ok else '❌ বন্ধ - /admin দেখুন'}\n\n"
        f"📤 <b>আপলোড</b> — PHP, HTML, ZIP, ছবি, ভিডিও\n"
        f"📂 <b>আমার সাইট</b> — সব হোস্টেড সাইট\n"
        f"👤 <b>একাউন্ট</b> — প্রোফাইল ও তথ্য\n"
        f"💎 <b>Premium</b> — বেশি সাইট হোস্ট করুন\n"
        f"🔗 <b>Short URL</b> — যেকোনো লিংক শর্ট করুন\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⌨️ <b>কমান্ড:</b>\n"
        f"/start — শুরু করুন\n"
        f"/myfiles — আমার সাইট\n"
        f"/account — একাউন্ট\n"
        f"/shorturl [URL] — Short URL\n"
        f"/search [keyword] — ফাইল সার্চ\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 সর্বোচ্চ ফাইল সাইজ: <b>{MAX_FILE_MB}MB</b>\n"
        f"🆓 Free: <b>{sg('free_limit', FREE_LIMIT)} সাইট</b>\n"
        f"💎 Premium: <b>{sg('premium_limit', PREMIUM_LIMIT)} সাইট</b>\n\n"
        f"⚠️ <b>PHP সিকিউরিটি:</b>\n"
        f"• exec, system, shell_exec ব্লক\n"
        f"• বাহ্যিক URL access বন্ধ\n"
        f"• Memory limit: {PHP_MEMORY_LIMIT}\n"
        f"• Execution time: {PHP_MAX_EXEC}s"
    )

# ================= UPLOAD =================
@bot.message_handler(func=lambda m: m.text == "📤 ফাইল আপলোড")
@banned_check
def upload_menu(msg):
    ask_upload(msg, msg.from_user.id)

def ask_upload(msg, uid):
    count = col_files.count_documents({"user_id": uid})
    limit = get_limit(uid)
    bar   = "█" * int((count/limit)*10 if limit else 0) + "░" * (10 - int((count/limit)*10 if limit else 0))
    php_ok = check_php()
    bot.send_message(msg.chat.id,
        f"📤 <b>ফাইল আপলোড</b>\n━━━━━━━━━━━━━━━\n"
        f"📊 [{bar}] {count}/{limit}\n"
        f"🐘 PHP: {'✅ চালু' if php_ok else '❌ বন্ধ'}\n━━━━━━━━━━━━━━━\n\n"
        f"📁 <b>সাপোর্টেড ফরম্যাট:</b>\n"
        f"🐘 <b>.php</b> — PHP ওয়েব পেজ\n"
        f"🌐 <b>.html</b> — Static HTML\n"
        f"📦 <b>.zip</b> — পুরো প্রজেক্ট (PHP+CSS+JS)\n"
        f"🖼 <b>ছবি/ভিডিও/MP3/PDF</b>\n\n"
        f"📦 সর্বোচ্চ সাইজ: <b>{MAX_FILE_MB}MB</b>\n\n"
        f"⬇️ এখন ফাইল পাঠান:")

@bot.message_handler(content_types=["document", "photo", "video", "audio"])
@banned_check
def handle_upload(msg):
    uid = msg.from_user.id
    if col_files.count_documents({"user_id": uid}) >= get_limit(uid):
        bot.reply_to(msg, "⚠️ সাইট লিমিট শেষ! Premium নিন।"); return

    if msg.document:
        fid, fname, fsize = msg.document.file_id, msg.document.file_name or "file", msg.document.file_size or 0
    elif msg.photo:
        fid, fname, fsize = msg.photo[-1].file_id, "image.jpg", msg.photo[-1].file_size or 0
    elif msg.video:
        fid, fname, fsize = msg.video.file_id, msg.video.file_name or "video.mp4", msg.video.file_size or 0
    elif msg.audio:
        fid, fname, fsize = msg.audio.file_id, msg.audio.file_name or "audio.mp3", msg.audio.file_size or 0
    else: return

    if fsize > MAX_FILE_BYTES:
        bot.reply_to(msg, f"❌ ফাইল {MAX_FILE_MB}MB এর বেশি!"); return

    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if ext not in SUPPORTED_EXT:
        bot.reply_to(msg, f"❌ সাপোর্টেড নয়! সাপোর্টেড: {', '.join(SUPPORTED_EXT)}"); return

    wait = bot.reply_to(msg, "⏳ <b>১/৩:</b> ফাইল ডাউনলোড হচ্ছে...")
    try:
        info = bot.get_file(fid)
        data = bot.download_file(info.file_path)
    except Exception as e:
        bot.edit_message_text(f"❌ ডাউনলোড সমস্যা: {e}", msg.chat.id, wait.message_id); return

    # PHP threat scan
    if ext in PHP_EXT:
        is_safe, threats = scan_php_threats(data)
        if not is_safe:
            tlist = "\n".join(f"  • {t}" for t in threats)
            try:
                bot.send_message(OWNER_ID,
                    f"🚨 <b>Suspicious PHP!</b>\n👤 User: <code>{uid}</code>\n"
                    f"📄 File: {fname}\n⚠️ Threats:\n{tlist}")
            except: pass
            if sg("block_php_threats", "off") == "on":
                bot.edit_message_text(
                    f"🚨 <b>ফাইল ব্লক!</b>\n⚠️ সন্দেহজনক কোড:\n{tlist}",
                    msg.chat.id, wait.message_id); return

    bot.edit_message_text("⏳ <b>২/৩:</b> সাইট তৈরি হচ্ছে...", msg.chat.id, wait.message_id)

    code = gen_code()
    site_dir = os.path.join(UPLOAD_DIR, str(uid), code)
    os.makedirs(site_dir, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_type = "php" if ext in PHP_EXT else "html" if ext in ['html','htm'] else "zip" if ext == "zip" else "media"
    extra = ""

    if ext in PHP_EXT:
        with open(os.path.join(site_dir, "index.php"), "wb") as f:
            f.write(data)
    elif ext in ['html', 'htm']:
        with open(os.path.join(site_dir, "index.html"), "wb") as f:
            f.write(data)
    elif ext == 'zip':
        zp = os.path.join(site_dir, "upload.zip")
        with open(zp, "wb") as f:
            f.write(data)
        try:
            with zipfile.ZipFile(zp, 'r') as z:
                z.extractall(site_dir)
            os.remove(zp)
            # PHP ফাইল আছে কিনা চেক করুন
            php_files = []
            for root_, _, fls in os.walk(site_dir):
                for fl in fls:
                    if fl.endswith('.php'):
                        php_files.append(os.path.relpath(os.path.join(root_, fl), site_dir))
            php_count = len(php_files)
            extra = f"\n🐘 PHP ফাইল: {php_count}টি"
        except Exception as e:
            bot.edit_message_text(f"❌ ZIP এরর: {e}", msg.chat.id, wait.message_id)
            shutil.rmtree(site_dir, ignore_errors=True); return
        file_type = "zip"
    else:
        with open(os.path.join(site_dir, fname), "wb") as f:
            f.write(data)
        file_type = "media"

    col_files.insert_one({
        "user_id": uid, "code": code, "name": fname, "type": file_type,
        "date": date, "views": 0, "is_public": 1, "password": None,
        "expiry": None, "slug": None
    })

    slug = code
    url  = f"{DOMAIN}/v/{slug}"
    bot.edit_message_text("⏳ <b>৩/৩:</b> লিংক তৈরি হচ্ছে...", msg.chat.id, wait.message_id)

    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🔗 সাইট দেখুন", url=url),
           types.InlineKeyboardButton("📊 Analytics", callback_data=f"stats_{code}"))
    kb.row(types.InlineKeyboardButton("⚙️ সেটিংস", callback_data=f"cfg_{code}"),
           types.InlineKeyboardButton("📱 QR Code", callback_data=f"qr_{code}"))
    kb.row(types.InlineKeyboardButton("💾 Backup", callback_data=f"backup_{code}"),
           types.InlineKeyboardButton("🗑 ডিলিট", callback_data=f"del_{code}"))

    type_icon = "🐘" if file_type == "php" else "📦" if file_type == "zip" else "🌐" if file_type == "html" else "🖼"

    bot.edit_message_text(
        f"✅ <b>সাইট হোস্ট হয়েছে!</b>\n━━━━━━━━━━━━━━━\n"
        f"{type_icon} টাইপ: <b>{file_type.upper()}</b>\n"
        f"📄 নাম: <b>{fname}</b>\n"
        f"🌐 URL: <code>{url}</code>\n"
        f"📅 তারিখ: {date}{extra}\n\n"
        f"🔗 লিংক শেয়ার করুন!",
        msg.chat.id, wait.message_id, reply_markup=kb)
    log_action(uid, "upload", fname)

    # Owner backup notification
    try:
        u_info = col_users.find_one({"id": uid}, {"username":1,"first_name":1})
        uname  = u_info.get("username","") if u_info else ""
        ulink  = f"@{uname}" if uname else f"<a href='tg://user?id={uid}'>{uid}</a>"
        owner_kb = types.InlineKeyboardMarkup()
        owner_kb.row(
            types.InlineKeyboardButton("✅ রাখুন", callback_data=f"owner_keep_{code}"),
            types.InlineKeyboardButton("🗑 ডিলিট", callback_data=f"owner_del_{code}_{uid}")
        )
        owner_kb.add(types.InlineKeyboardButton("🚫 Ban User", callback_data=f"admin_ban_{uid}"))
        if msg.document or msg.photo or msg.video or msg.audio:
            bot.forward_message(OWNER_ID, msg.chat.id, msg.message_id)
        bot.send_message(OWNER_ID,
            f"📦 <b>নতুন আপলোড</b>\n👤 {ulink} (<code>{uid}</code>)\n"
            f"{type_icon} {file_type.upper()} | 📄 {fname}\n🌐 <code>{url}</code>",
            parse_mode="HTML", disable_web_page_preview=True, reply_markup=owner_kb)
    except Exception as e:
        logger.warning(f"Owner notify: {e}")

# ================= OWNER APPROVE/REJECT =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("owner_keep_"))
def owner_keep(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id, "✅ রাখা হয়েছে!", show_alert=True)
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("owner_del_"))
def owner_del(call):
    if not is_admin(call.from_user.id): return
    parts = call.data[len("owner_del_"):].split("_")
    code  = parts[0]
    f = col_files.find_one({"code": code}, {"user_id": 1})
    if f:
        auid = f["user_id"]
        col_files.delete_one({"code": code})
        try: shutil.rmtree(os.path.join(UPLOAD_DIR, str(auid), code))
        except: pass
        try: bot.send_message(auid, "⚠️ আপনার একটি সাইট Admin দ্বারা সরানো হয়েছে।")
        except: pass
    bot.answer_callback_query(call.id, "🗑 ডিলিট হয়েছে!", show_alert=True)
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass

# ================= MY SITES =================
@bot.message_handler(commands=["myfiles"])
@bot.message_handler(func=lambda m: m.text == "📂 আমার সাইট")
@banned_check
def my_sites_cmd(msg):
    list_sites(msg, msg.from_user.id)

def list_sites(msg, uid, page=0):
    PER = 5
    all_f = list(col_files.find({"user_id": uid}).sort("_id", -1))
    total = len(all_f)
    files = all_f[page*PER:(page+1)*PER]
    if not files:
        bot.send_message(msg.chat.id, "📂 কোনো সাইট নেই। প্রথমে আপলোড করুন।"); return
    text = f"📂 <b>আমার সাইট (মোট {total}টি)</b>\n\n"
    kb   = types.InlineKeyboardMarkup()
    for f in files:
        slug = f.get("slug") or f["code"]
        url  = f"{DOMAIN}/v/{slug}"
        icon = "🐘" if f["type"]=="php" else "📦" if f["type"]=="zip" else "🌐" if f["type"]=="html" else "🖼"
        lock = "🔒" if f.get("password") else ""
        text += f"{icon}{lock} <b>{f['name'][:28]}</b>\n"
        text += f"   👁 {f['views']} | 📅 {f['date'][:10]}\n"
        text += f"   🔗 <code>{url}</code>\n\n"
        kb.row(types.InlineKeyboardButton(f"⚙️ {f['name'][:14]}", callback_data=f"cfg_{f['code']}"),
               types.InlineKeyboardButton("🗑", callback_data=f"del_{f['code']}"))
    nav = []
    if page > 0: nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"pg_{page-1}"))
    if (page+1)*PER < total: nav.append(types.InlineKeyboardButton("➡️", callback_data=f"pg_{page+1}"))
    if nav: kb.row(*nav)
    bot.send_message(msg.chat.id, text, reply_markup=kb, disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("pg_"))
def pages(call):
    bot.answer_callback_query(call.id)
    list_sites(call.message, call.from_user.id, int(call.data[3:]))

# ================= FILE SETTINGS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("cfg_"))
@banned_check
def file_cfg(call):
    code = call.data[4:]
    uid  = call.from_user.id
    f    = col_files.find_one({"code": code, "user_id": uid})
    if not f:
        bot.answer_callback_query(call.id, "❌ পাওয়া যায়নি!", show_alert=True); return
    slug  = f.get("slug") or code
    url   = f"{DOMAIN}/v/{slug}"
    icon  = "🐘" if f["type"]=="php" else "📦" if f["type"]=="zip" else "🌐"
    pub   = "🔐 Private করুন" if f.get("is_public") else "🔓 Public করুন"
    kb    = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🔗 দেখুন", url=url),
           types.InlineKeyboardButton("📊 Analytics", callback_data=f"stats_{code}"))
    kb.row(types.InlineKeyboardButton("📱 QR Code", callback_data=f"qr_{code}"),
           types.InlineKeyboardButton("💾 Backup", callback_data=f"backup_{code}"))
    kb.row(types.InlineKeyboardButton("✏️ Rename", callback_data=f"ren_{code}"),
           types.InlineKeyboardButton("🔗 Custom Slug", callback_data=f"slug_{code}"))
    kb.row(types.InlineKeyboardButton("🔒 Password", callback_data=f"pass_{code}"),
           types.InlineKeyboardButton("⏰ Expiry", callback_data=f"exp_{code}"))
    kb.row(types.InlineKeyboardButton("🔄 আপডেট", callback_data=f"upd_{code}"),
           types.InlineKeyboardButton(pub, callback_data=f"pub_{code}"))
    kb.add(types.InlineKeyboardButton("🗑 ডিলিট", callback_data=f"del_{code}"))
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            f"⚙️ <b>সাইট সেটিংস</b>\n━━━━━━━━━━━━━━━\n"
            f"{icon} নাম: <b>{f['name']}</b>\n🌐 URL: <code>{url}</code>\n"
            f"👁 Views: <b>{f['views']}</b> | 📅 {f['date'][:10]}\n"
            f"🔒 Password: {'আছে' if f.get('password') else 'নেই'}",
            call.message.chat.id, call.message.message_id, reply_markup=kb)
    except:
        bot.send_message(call.message.chat.id, f"⚙️ সাইট সেটিংস: {f['name']}", reply_markup=kb)

# ================= RENAME =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("ren_"))
def rename(call):
    code = call.data[4:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "✏️ নতুন নাম লিখুন:")
    bot.register_next_step_handler(call.message, lambda m: _save_rename(m, code))

def _save_rename(msg, code):
    n = (msg.text or "").strip()
    if not n: bot.reply_to(msg, "❌ নাম দিন।"); return
    col_files.update_one({"code": code, "user_id": msg.from_user.id}, {"$set": {"name": n}})
    bot.reply_to(msg, f"✅ নাম পরিবর্তন: <b>{n}</b>")

# ================= CUSTOM SLUG =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("slug_"))
def set_slug(call):
    code = call.data[5:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🔗 Custom URL লিখুন (শুধু a-z, 0-9, -):\nউদাহরণ: my-portfolio")
    bot.register_next_step_handler(call.message, lambda m: _save_slug(m, code))

def _save_slug(msg, code):
    slug = (msg.text or "").strip().lower().replace(" ","-")
    if not re.match(r'^[a-z0-9\-]+$', slug):
        bot.reply_to(msg, "❌ শুধু a-z, 0-9, - ব্যবহার করুন।"); return
    if col_files.find_one({"slug": slug}):
        bot.reply_to(msg, "❌ এই slug ব্যবহৃত। অন্যটি দিন।"); return
    col_files.update_one({"code": code, "user_id": msg.from_user.id}, {"$set": {"slug": slug}})
    url = f"{DOMAIN}/v/{slug}"
    bot.reply_to(msg, f"✅ Custom URL সেট!\n🔗 <code>{url}</code>")

# ================= PASSWORD =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("pass_"))
def set_pass(call):
    code = call.data[5:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🔒 পাসওয়ার্ড লিখুন (মুছতে 'remove' লিখুন):")
    bot.register_next_step_handler(call.message, lambda m: _save_pass(m, code))

def _save_pass(msg, code):
    pw = (msg.text or "").strip()
    if pw.lower() == "remove":
        col_files.update_one({"code": code, "user_id": msg.from_user.id}, {"$set": {"password": None}})
        bot.reply_to(msg, "✅ পাসওয়ার্ড সরানো হয়েছে!")
    elif pw:
        col_files.update_one({"code": code, "user_id": msg.from_user.id}, {"$set": {"password": pw}})
        bot.reply_to(msg, f"✅ পাসওয়ার্ড সেট: <code>{pw}</code>")
    else:
        bot.reply_to(msg, "❌ পাসওয়ার্ড দিন।")

# ================= EXPIRY =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("exp_"))
def set_expiry(call):
    code = call.data[4:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "⏰ কতদিন পরে ডিলিট হবে?\n(সংখ্যা লিখুন | 'remove' = মুছুন)")
    bot.register_next_step_handler(call.message, lambda m: _save_expiry(m, code))

def _save_expiry(msg, code):
    val = (msg.text or "").strip()
    if val.lower() == "remove":
        col_files.update_one({"code": code, "user_id": msg.from_user.id}, {"$set": {"expiry": None}})
        bot.reply_to(msg, "✅ Expiry সরানো হয়েছে!")
    elif val.isdigit():
        exp = (datetime.now() + timedelta(days=int(val))).isoformat()
        col_files.update_one({"code": code, "user_id": msg.from_user.id}, {"$set": {"expiry": exp}})
        bot.reply_to(msg, f"✅ {val} দিন পরে সাইট ডিলিট হবে।")
    else:
        bot.reply_to(msg, "❌ বৈধ সংখ্যা দিন।")

# ================= PUBLIC/PRIVATE =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("pub_"))
def toggle_pub(call):
    code = call.data[4:]
    f = col_files.find_one({"code": code, "user_id": call.from_user.id}, {"is_public":1})
    if not f: bot.answer_callback_query(call.id, "❌ পাওয়া যায়নি!", show_alert=True); return
    nv = 0 if f.get("is_public") else 1
    col_files.update_one({"code": code}, {"$set": {"is_public": nv}})
    bot.answer_callback_query(call.id, f"✅ {'Public' if nv else 'Private'} করা হয়েছে!", show_alert=True)

# ================= UPDATE =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("upd_"))
def update_site(call):
    code = call.data[4:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🔄 নতুন ফাইল পাঠান:")
    bot.register_next_step_handler(call.message, lambda m: _do_update(m, code))

def _do_update(msg, code):
    if not msg.document: bot.reply_to(msg, "❌ ফাইল পাঠান।"); return
    uid = msg.from_user.id
    f   = col_files.find_one({"code": code, "user_id": uid})
    if not f: bot.reply_to(msg, "❌ পাওয়া যায়নি।"); return
    fname = msg.document.file_name or "file"
    ext   = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if ext not in SUPPORTED_EXT: bot.reply_to(msg, "❌ সাপোর্টেড নয়।"); return
    site_dir = os.path.join(UPLOAD_DIR, str(uid), code)
    shutil.rmtree(site_dir, ignore_errors=True)
    os.makedirs(site_dir, exist_ok=True)
    info = bot.get_file(msg.document.file_id)
    data = bot.download_file(info.file_path)
    if ext in PHP_EXT:
        with open(os.path.join(site_dir, "index.php"), "wb") as fh: fh.write(data)
    elif ext in ['html','htm']:
        with open(os.path.join(site_dir, "index.html"), "wb") as fh: fh.write(data)
    elif ext == 'zip':
        zp = os.path.join(site_dir, "u.zip")
        with open(zp, "wb") as fh: fh.write(data)
        with zipfile.ZipFile(zp, 'r') as z: z.extractall(site_dir)
        os.remove(zp)
    else:
        with open(os.path.join(site_dir, fname), "wb") as fh: fh.write(data)
    ftype = "php" if ext in PHP_EXT else "html" if ext in ['html','htm'] else "zip" if ext=='zip' else "media"
    col_files.update_one({"code": code}, {"$set": {"name": fname, "type": ftype, "date": datetime.now().strftime("%Y-%m-%d %H:%M")}})
    bot.reply_to(msg, "✅ সাইট আপডেট হয়েছে!")

# ================= DELETE =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("del_"))
def delete_site(call):
    code = call.data[4:]
    f = col_files.find_one({"code": code}, {"user_id":1})
    if not f or (f["user_id"] != call.from_user.id and not is_admin(call.from_user.id)):
        bot.answer_callback_query(call.id, "❌ অনুমতি নেই!", show_alert=True); return
    col_files.delete_one({"code": code})
    try: shutil.rmtree(os.path.join(UPLOAD_DIR, str(f["user_id"]), code))
    except: pass
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text("🗑 সাইট ডিলিট হয়েছে!", call.message.chat.id, call.message.message_id)
    except: pass

# ================= QR CODE =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("qr_"))
def send_qr(call):
    import qrcode
    code = call.data[3:]
    f    = col_files.find_one({"code": code}, {"slug":1})
    slug = (f.get("slug") if f else None) or code
    url  = f"{DOMAIN}/v/{slug}"
    qr   = qrcode.make(url)
    buf  = io.BytesIO(); qr.save(buf, format="PNG"); buf.seek(0); buf.name = "qr.png"
    bot.answer_callback_query(call.id)
    bot.send_photo(call.message.chat.id, buf, caption=f"📱 QR Code\n<code>{url}</code>")

# ================= ANALYTICS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("stats_"))
@banned_check
def analytics(call):
    code = call.data[6:]
    f    = col_files.find_one({"code": code, "user_id": call.from_user.id})
    if not f: bot.answer_callback_query(call.id, "❌ পাওয়া যায়নি!", show_alert=True); return
    by_country = list(col_views.aggregate([
        {"$match": {"code": code}},
        {"$group": {"_id": "$country", "c": {"$sum": 1}}},
        {"$sort": {"c": -1}}, {"$limit": 5}
    ]))
    by_day = list(col_views.aggregate([
        {"$match": {"code": code}},
        {"$group": {"_id": {"$substr": ["$date", 0, 10]}, "c": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": 7}
    ]))
    unique = len(col_views.distinct("ip", {"code": code}))
    ctxt   = "".join(f"\n  🌍 {r['_id'] or 'Unknown'}: {r['c']}" for r in by_country)
    dtxt   = "".join(f"\n  📅 {r['_id']}: {r['c']}" for r in by_day)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        f"📊 <b>Analytics</b>\n━━━━━━━━━━━━━━━\n"
        f"📄 <b>{f['name']}</b>\n\n"
        f"👁 মোট Views: <b>{f['views']}</b>\n"
        f"👤 Unique: <b>{unique}</b>\n"
        f"🕐 শেষ Visit: {f.get('last_view','N/A')}\n\n"
        f"🌍 <b>দেশ:</b>{ctxt or ' N/A'}\n\n"
        f"📅 <b>৭ দিনের ট্রেন্ড:</b>{dtxt or ' N/A'}")

# ================= BACKUP =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("backup_"))
@banned_check
def backup(call):
    code = call.data[7:]
    uid  = call.from_user.id
    f    = col_files.find_one({"code": code, "user_id": uid}, {"name":1})
    if not f: bot.answer_callback_query(call.id, "❌ পাওয়া যায়নি!", show_alert=True); return
    site_dir = os.path.join(UPLOAD_DIR, str(uid), code)
    if not os.path.exists(site_dir): bot.answer_callback_query(call.id, "❌ ফাইল নেই!", show_alert=True); return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root_, _, fls in os.walk(site_dir):
            for fl in fls:
                fp = os.path.join(root_, fl)
                zf.write(fp, os.path.relpath(fp, site_dir))
    buf.seek(0); buf.name = f"backup_{code}.zip"
    bot.answer_callback_query(call.id)
    bot.send_document(call.message.chat.id, buf, caption=f"💾 Backup: <b>{f['name']}</b>")

# ================= FILE SEARCH =================
@bot.message_handler(commands=["search"])
@banned_check
def search_files(msg):
    uid  = msg.from_user.id
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(msg, "🔍 ব্যবহার: /search [কীওয়ার্ড]\nউদাহরণ: /search portfolio"); return
    kw      = args[1].strip()
    results = list(col_files.find({"user_id": uid, "name": {"$regex": kw, "$options": "i"}}).limit(10))
    if not results:
        bot.reply_to(msg, f"❌ '<b>{kw}</b>' পাওয়া যায়নি।"); return
    text = f"🔍 <b>'{kw}' ({len(results)}টি):</b>\n\n"
    kb   = types.InlineKeyboardMarkup()
    for fi in results:
        slug = fi.get("slug") or fi["code"]
        icon = "🐘" if fi["type"]=="php" else "📦" if fi["type"]=="zip" else "🌐"
        text += f"{icon} <b>{fi['name'][:30]}</b> | 👁 {fi['views']}\n   <code>{DOMAIN}/v/{slug}</code>\n\n"
        kb.add(types.InlineKeyboardButton(f"⚙️ {fi['name'][:20]}", callback_data=f"cfg_{fi['code']}"))
    bot.reply_to(msg, text, reply_markup=kb, disable_web_page_preview=True)

# ================= SHORT URL =================
@bot.message_handler(func=lambda m: m.text == "🔗 Short URL")
@bot.message_handler(commands=["shorturl"])
@banned_check
def shorturl_cmd(msg):
    uid  = msg.from_user.id
    args = msg.text.split()
    if len(args) > 1 and args[1].startswith("http"):
        _make_short(msg, uid, args[1])
    else:
        bot.send_message(msg.chat.id, "🔗 শর্ট করতে চান এমন URL পাঠান:")
        bot.register_next_step_handler(msg, lambda m: _make_short(m, uid, (m.text or "").strip()))

def _make_short(msg, uid, url):
    if not url or not url.startswith("http"):
        bot.reply_to(msg, "❌ বৈধ URL দিন।"); return
    code  = gen_url_code()
    short = f"{DOMAIN}/s/{code}"
    col_short.insert_one({"code": code, "url": url, "user_id": uid,
                           "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "clicks": 0})
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🔗 Short URL", url=short),
           types.InlineKeyboardButton("🗑 ডিলিট", callback_data=f"dels_{code}"))
    bot.reply_to(msg, f"✅ <b>Short URL!</b>\n🔗 <code>{short}</code>\n📄 {url[:60]}{'...' if len(url)>60 else ''}",
                 reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("dels_"))
def del_short(call):
    col_short.delete_one({"code": call.data[5:], "user_id": call.from_user.id})
    bot.answer_callback_query(call.id, "🗑 ডিলিট হয়েছে!", show_alert=True)
    try: bot.edit_message_text("🗑 Short URL ডিলিট হয়েছে।", call.message.chat.id, call.message.message_id)
    except: pass

# ================= ACCOUNT =================
@bot.message_handler(commands=["account"])
@bot.message_handler(func=lambda m: m.text == "👤 একাউন্ট")
@banned_check
def account(msg):
    show_account_msg(msg, msg.from_user.id)

def show_account_msg(msg, uid):
    is_prem = is_premium(uid)
    count   = col_files.count_documents({"user_id": uid})
    limit   = get_limit(uid)
    u       = col_users.find_one({"id": uid})
    prem    = col_premium.find_one({"user_id": uid})
    views   = col_views.count_documents({"user_id": uid}) if col_views else 0
    bar     = "█" * int((count/limit)*10 if limit else 0) + "░" * (10 - int((count/limit)*10 if limit else 0))
    pline   = ""
    if prem and is_prem:
        try:
            days = (datetime.fromisoformat(prem["expiry"]) - datetime.now()).days
            pline = f"\n├ 📦 Plan: <b>{prem['plan']}</b> | ⏳ <b>{days} দিন</b> বাকি"
        except: pass
    bot.send_message(msg.chat.id,
        f"👤 <b>আমার প্রোফাইল</b>\n━━━━━━━━━━━━━━━\n"
        f"├ 🆔 ID: <code>{uid}</code>\n"
        f"├ 👤 @{u.get('username','N/A') if u and u.get('username') else 'N/A'}\n"
        f"├ 🌟 {'💎 Premium' if is_prem else '🆓 Free'}{pline}\n"
        f"├ 📅 যোগদান: {u.get('joined','N/A')[:10] if u else 'N/A'}\n"
        f"└ 📊 <b>Stats</b>\n"
        f"   ├ 🌐 সাইট: <b>{count}/{limit}</b> [{bar}]\n"
        f"   └ 👫 রেফারেল: <b>{u.get('invites',0) if u else 0}</b> জন")

# ================= PREMIUM =================
@bot.message_handler(func=lambda m: m.text == "💎 প্রিমিয়াম")
@banned_check
def prem_cmd(msg):
    show_premium(msg, msg.from_user.id)

def show_premium(msg, uid):
    methods = list(col_pay_meth.find({"active": 1}))
    pay_txt = "\n\n💳 <b>পেমেন্ট পদ্ধতি:</b>\n"
    for m in methods:
        pay_txt += f"• <b>{m['name']}</b>: <code>{m['number']}</code>\n"
        if m.get("note"): pay_txt += f"  ➜ {m['note']}\n"
    if not methods: pay_txt = "\n\nOwner কে contact করুন।"
    sp = sg("price_silver","১৫০"); gp = sg("price_gold","৩৫০"); lp = sg("price_lifetime","৯৯৯")
    sl = sg("limit_silver","20"); gl = sg("limit_gold","50"); ll = sg("limit_lifetime","200")
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(f"🥈 Silver ৳{sp}", callback_data="plan_silver"),
           types.InlineKeyboardButton(f"🥇 Gold ৳{gp}", callback_data="plan_gold"))
    kb.add(types.InlineKeyboardButton(f"💫 Lifetime ৳{lp}", callback_data="plan_lifetime"))
    kb.add(types.InlineKeyboardButton("👨‍💻 Owner Contact", url=f"tg://user?id={OWNER_ID}"))
    bot.send_message(msg.chat.id,
        f"💎 <b>Premium Plans</b>\n━━━━━━━━━━━━━━━\n"
        f"🥈 <b>Silver</b> — ৩০ দিন | {sl} সাইট | ৳{sp}\n"
        f"🥇 <b>Gold</b> — ৯০ দিন | {gl} সাইট | ৳{gp}\n"
        f"💫 <b>Lifetime</b> — চিরস্থায়ী | {ll} সাইট | ৳{lp}"
        f"{pay_txt}", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan_"))
@banned_check
def plan_select(call):
    plan = call.data[5:]
    prices = {"silver": sg("price_silver","১৫০"), "gold": sg("price_gold","৩৫০"), "lifetime": sg("price_lifetime","৯৯৯")}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        f"💎 <b>{plan.title()} Plan</b>\n\nমূল্য: ৳{prices.get(plan,'N/A')}\n\n"
        f"পেমেন্ট করে TXN ID পাঠান:")
    bot.register_next_step_handler(call.message, lambda m: _pay_request(m, plan))

def _pay_request(msg, plan):
    uid = msg.from_user.id
    col_pay_req.insert_one({"user_id": uid, "plan": plan, "txn": msg.text or "",
                             "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "status": "pending"})
    bot.reply_to(msg, "✅ Payment request পাঠানো হয়েছে। Admin verify করবেন।")
    try:
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton(f"✅ Approve {plan}", callback_data=f"pay_ok_{uid}_{plan}"),
               types.InlineKeyboardButton("❌ Reject", callback_data=f"pay_no_{uid}"))
        bot.send_message(OWNER_ID,
            f"💳 <b>Payment Request</b>\n👤 <code>{uid}</code>\n"
            f"📦 Plan: {plan}\n📝 TXN: {msg.text or ''}", reply_markup=kb)
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_ok_"))
def pay_approve(call):
    if not is_admin(call.from_user.id): return
    _, uid_s, plan = call.data.split("_", 2)[1], call.data.split("_")[2], call.data.split("_")[3]
    uid  = int(call.data.split("_")[2])
    plan = call.data.split("_")[3]
    days = {"silver": 30, "gold": 90, "lifetime": 36500}.get(plan, 30)
    exp  = (datetime.now() + timedelta(days=days)).isoformat()
    col_premium.update_one({"user_id": uid}, {"$set": {"user_id": uid, "plan": plan, "expiry": exp}}, upsert=True)
    bot.answer_callback_query(call.id, "✅ Approved!")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass
    try: bot.send_message(uid, f"🎉 <b>{plan.title()} Premium</b> চালু! ({days} দিন)")
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_no_"))
def pay_reject(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data[7:])
    bot.answer_callback_query(call.id, "❌ Rejected!")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass
    try: bot.send_message(uid, "❌ পেমেন্ট verify হয়নি। সঠিক TXN ID দিয়ে আবার চেষ্টা করুন।")
    except: pass

# ================= ADMIN PANEL =================
@bot.message_handler(commands=["admin"])
def admin_panel(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ আপনি admin নন।"); return
    show_admin(msg.chat.id)

def show_admin(chat_id):
    total_u = col_users.count_documents({})
    total_s = col_files.count_documents({})
    prem_c  = col_premium.count_documents({})
    pending = col_pay_req.count_documents({"status": "pending"})
    php_ok  = check_php()
    storage = fmt_bytes(get_storage())
    tv_agg  = list(col_files.aggregate([{"$group": {"_id": None, "t": {"$sum": "$views"}}}]))
    total_v = tv_agg[0]["t"] if tv_agg else 0

    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("📊 Stats", callback_data="a_stats"),
           types.InlineKeyboardButton("👥 Users", callback_data="a_users"))
    kb.row(types.InlineKeyboardButton("💎 Premium দিন", callback_data="a_prem"),
           types.InlineKeyboardButton("🚫 Ban/Unban", callback_data="a_ban"))
    kb.row(types.InlineKeyboardButton("📢 Broadcast", callback_data="a_broadcast"),
           types.InlineKeyboardButton("🔧 Maintenance", callback_data="a_maint"))
    kb.row(types.InlineKeyboardButton("💳 Payments", callback_data="a_pays"),
           types.InlineKeyboardButton("🐘 PHP Settings", callback_data="a_php"))
    kb.row(types.InlineKeyboardButton("💰 Prices", callback_data="a_prices"),
           types.InlineKeyboardButton("⚙️ Limits", callback_data="a_limits"))
    kb.row(types.InlineKeyboardButton("📁 Channels", callback_data="a_channels"),
           types.InlineKeyboardButton("💾 Storage", callback_data="a_storage"))
    kb.row(types.InlineKeyboardButton("📜 Logs", callback_data="a_logs"),
           types.InlineKeyboardButton("📤 Export CSV", callback_data="a_csv"))
    kb.row(types.InlineKeyboardButton("🔍 User Search", callback_data="a_search"),
           types.InlineKeyboardButton("👤 Add Admin", callback_data="a_addadmin"))
    kb.row(types.InlineKeyboardButton("💳 Pay Methods", callback_data="a_paymeth"),
           types.InlineKeyboardButton("📨 Message User", callback_data="a_msguser"))

    bot.send_message(chat_id,
        f"⚙️ <b>Admin Panel</b>\n━━━━━━━━━━━━━━━\n"
        f"👥 Users: <b>{total_u}</b> | 🌐 Sites: <b>{total_s}</b>\n"
        f"👁 Views: <b>{total_v}</b> | 💎 Premium: <b>{prem_c}</b>\n"
        f"💳 Pending: <b>{pending}</b> | 💾 Storage: <b>{storage}</b>\n"
        f"🐘 PHP: {'✅ চালু' if php_ok else '❌ বন্ধ'}",
        reply_markup=kb)

# ===== ADMIN - PHP SETTINGS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_php")
def admin_php(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    php_ok     = check_php()
    block_thr  = sg("block_php_threats", "off")
    timeout    = sg("php_timeout", PHP_TIMEOUT)
    mem        = sg("php_memory", PHP_MEMORY_LIMIT)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        f"🛡 Threat Block: {'✅ ON' if block_thr=='on' else '❌ OFF'}",
        callback_data="toggle_phpblock"))
    kb.add(types.InlineKeyboardButton("⏱ Timeout সেট", callback_data="set_php_timeout"))
    kb.add(types.InlineKeyboardButton("💾 Memory Limit সেট", callback_data="set_php_mem"))
    bot.send_message(call.message.chat.id,
        f"🐘 <b>PHP Settings</b>\n━━━━━━━━━━━━━━━\n"
        f"🐘 PHP Status: {'✅ ইনস্টল আছে' if php_ok else '❌ ইনস্টল নেই'}\n"
        f"🛡 Threat Auto-Block: {'✅ ON' if block_thr=='on' else '❌ OFF'}\n"
        f"⏱ Timeout: <b>{timeout}s</b>\n"
        f"💾 Memory: <b>{mem}</b>\n\n"
        f"{'✅ PHP চালু আছে!' if php_ok else '⚠️ start.sh দিয়ে PHP install করুন!'}",
        reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "toggle_phpblock")
def toggle_phpblock(call):
    if not is_admin(call.from_user.id): return
    cur = sg("block_php_threats", "off")
    nv  = "off" if cur == "on" else "on"
    ss("block_php_threats", nv)
    bot.answer_callback_query(call.id, f"✅ Threat Block {'চালু' if nv=='on' else 'বন্ধ'}!", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data == "set_php_timeout")
def set_timeout(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"⏱ PHP Timeout (বর্তমান: {sg('php_timeout',PHP_TIMEOUT)}s)\nনতুন সময় (সেকেন্ড) লিখুন:")
    bot.register_next_step_handler(call.message, lambda m: _save_timeout(m))

def _save_timeout(msg):
    if not is_admin(msg.from_user.id): return
    v = (msg.text or "").strip()
    if not v.isdigit(): bot.reply_to(msg, "❌ সংখ্যা দিন।"); return
    ss("php_timeout", v)
    bot.reply_to(msg, f"✅ PHP Timeout: <b>{v}s</b>")

@bot.callback_query_handler(func=lambda c: c.data == "set_php_mem")
def set_mem(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"💾 PHP Memory (বর্তমান: {sg('php_memory',PHP_MEMORY_LIMIT)})\nউদাহরণ: 32M, 64M, 128M:")
    bot.register_next_step_handler(call.message, lambda m: _save_mem(m))

def _save_mem(msg):
    if not is_admin(msg.from_user.id): return
    v = (msg.text or "").strip().upper()
    if not re.match(r'^\d+M$', v): bot.reply_to(msg, "❌ সঠিক ফরম্যাট দিন। যেমন: 64M"); return
    ss("php_memory", v)
    bot.reply_to(msg, f"✅ PHP Memory: <b>{v}</b>")

# ===== ADMIN - STATS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_stats")
def admin_stats(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id, "⏳ লোড হচ্ছে...")
    today  = datetime.now().strftime("%Y-%m-%d")
    nu     = col_users.count_documents({"joined": {"$regex": f"^{today}"}})
    ns     = col_files.count_documents({"date":   {"$regex": f"^{today}"}})
    tv_agg = list(col_files.aggregate([{"$group": {"_id": None, "t": {"$sum": "$views"}}}]))
    tv     = tv_agg[0]["t"] if tv_agg else 0
    top    = list(col_files.find({},{"name":1,"views":1,"code":1,"type":1}).sort("views",-1).limit(5))
    ttxt   = "".join(f"\n  {'🐘' if s.get('type')=='php' else '🌐'} {s['name'][:20]}: {s['views']}" for s in top)
    bot.send_message(call.message.chat.id,
        f"📊 <b>Stats — {today}</b>\n━━━━━━━━━━━━━━━\n"
        f"👥 আজ নতুন: <b>+{nu}</b> (মোট {col_users.count_documents({})})\n"
        f"🌐 আজ নতুন সাইট: <b>+{ns}</b> (মোট {col_files.count_documents({})})\n"
        f"👁 মোট Views: <b>{tv}</b>\n"
        f"💎 Premium: <b>{col_premium.count_documents({})}</b>\n"
        f"💾 Storage: <b>{fmt_bytes(get_storage())}</b>\n\n"
        f"🏆 <b>Top Sites:</b>{ttxt or ' N/A'}")

# ===== ADMIN - USERS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_users")
def admin_users(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    _show_users(call.message.chat.id, 0)

def _show_users(chat_id, page):
    PER   = 20
    total = col_users.count_documents({})
    users = list(col_users.find({},{"id":1,"username":1,"joined":1}).sort("id",-1).skip(page*PER).limit(PER))
    pages = max(1, ((total-1)//PER)+1)
    text  = f"👥 <b>ইউজার (মোট {total}) — পেজ {page+1}/{pages}:</b>\n\n"
    for u in users:
        p   = "💎" if is_premium(u["id"]) else "🆓"
        un  = f"@{u['username']}" if u.get("username") else "N/A"
        text += f"{p} <code>{u['id']}</code> | {un} | {(u.get('joined') or 'N/A')[:10]}\n"
    kb  = types.InlineKeyboardMarkup()
    nav = []
    if page > 0: nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"aupg_{page-1}"))
    if (page+1)*PER < total: nav.append(types.InlineKeyboardButton("➡️", callback_data=f"aupg_{page+1}"))
    if nav: kb.row(*nav)
    bot.send_message(chat_id, text[:4000], reply_markup=kb if nav else None)

@bot.callback_query_handler(func=lambda c: c.data.startswith("aupg_"))
def user_page(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    _show_users(call.message.chat.id, int(call.data[5:]))

# ===== ADMIN - PREMIUM =====
@bot.callback_query_handler(func=lambda c: c.data == "a_prem")
def admin_prem(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "💎 Premium দিন:\nফরম্যাট: USER_ID PLAN DAYS\nউদাহরণ: 123456 gold 30")
    bot.register_next_step_handler(call.message, _give_prem)

def _give_prem(msg):
    if not is_admin(msg.from_user.id): return
    try:
        uid, plan, days = int(msg.text.split()[0]), msg.text.split()[1], int(msg.text.split()[2])
        exp = (datetime.now() + timedelta(days=days)).isoformat()
        col_premium.update_one({"user_id": uid}, {"$set": {"user_id": uid, "plan": plan, "expiry": exp}}, upsert=True)
        bot.reply_to(msg, f"✅ <code>{uid}</code> কে {days}d {plan} Premium দেওয়া হয়েছে!")
        try: bot.send_message(uid, f"🎉 <b>{plan.title()} Premium</b> পেয়েছেন! ({days} দিন)")
        except: pass
    except Exception as e:
        bot.reply_to(msg, f"❌ ভুল ফরম্যাট: {e}")

# ===== ADMIN - BAN =====
@bot.callback_query_handler(func=lambda c: c.data == "a_ban")
def admin_ban(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🚫 Ban/Unban:\nUser ID পাঠান:")
    bot.register_next_step_handler(call.message, _do_ban)

def _do_ban(msg):
    if not is_admin(msg.from_user.id): return
    uid_s = (msg.text or "").strip()
    if not uid_s.isdigit(): bot.reply_to(msg, "❌ বৈধ ID দিন।"); return
    uid = int(uid_s)
    if uid == OWNER_ID: bot.reply_to(msg, "❌ Owner কে ban করা যাবে না!"); return
    key = f"ban_{uid}"
    if col_settings.find_one({"key": key}):
        col_settings.delete_one({"key": key})
        bot.reply_to(msg, f"✅ <code>{uid}</code> Unban!")
        try: bot.send_message(uid, "✅ আপনার ban তুলে নেওয়া হয়েছে।")
        except: pass
    else:
        ss(key, "1")
        bot.reply_to(msg, f"🚫 <code>{uid}</code> Ban!")
        try: bot.send_message(uid, "🚫 আপনাকে Ban করা হয়েছে।")
        except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_ban_"))
def quick_ban(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data[10:])
    if uid == OWNER_ID: bot.answer_callback_query(call.id, "❌ Owner ban করা যাবে না!", show_alert=True); return
    key = f"ban_{uid}"
    if col_settings.find_one({"key": key}):
        col_settings.delete_one({"key": key})
        bot.answer_callback_query(call.id, f"✅ {uid} Unban!", show_alert=True)
    else:
        ss(key, "1")
        bot.answer_callback_query(call.id, f"🚫 {uid} Ban!", show_alert=True)
        try: bot.send_message(uid, "🚫 আপনাকে Ban করা হয়েছে।")
        except: pass

# ===== ADMIN - BROADCAST =====
@bot.callback_query_handler(func=lambda c: c.data == "a_broadcast")
def admin_bc(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("📝 Text", callback_data="bc_text"),
           types.InlineKeyboardButton("🖼 Photo", callback_data="bc_photo"))
    bot.send_message(call.message.chat.id, "📢 Broadcast টাইপ:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("bc_"))
def bc_type(call):
    if not is_admin(call.from_user.id): return
    btype = call.data[3:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, {"text":"📝 মেসেজ লিখুন:","photo":"🖼 Photo পাঠান:"}.get(btype,"পাঠান:"))
    bot.register_next_step_handler(call.message, lambda m: _do_bc(m, btype))

def _do_bc(msg, btype):
    if not is_admin(msg.from_user.id): return
    users = list(col_users.find({},{"id":1}))
    ok = err = 0
    wait = bot.reply_to(msg, f"📢 শুরু... {len(users)} জন")
    for u in users:
        try:
            if btype == "text": bot.send_message(u["id"], msg.text, parse_mode="HTML")
            elif btype == "photo" and msg.photo: bot.send_photo(u["id"], msg.photo[-1].file_id, caption=msg.caption or "")
            ok += 1; time.sleep(0.05)
        except: err += 1
    try: bot.edit_message_text(f"✅ Broadcast! ✅{ok} ❌{err}", msg.chat.id, wait.message_id)
    except: pass

# ===== ADMIN - MAINTENANCE =====
@bot.callback_query_handler(func=lambda c: c.data == "a_maint")
def toggle_maint(call):
    if not is_admin(call.from_user.id): return
    cur = sg("maintenance","off"); nv = "off" if cur=="on" else "on"
    ss("maintenance", nv)
    bot.answer_callback_query(call.id, f"✅ Maintenance {'চালু' if nv=='on' else 'বন্ধ'}!", show_alert=True)

# ===== ADMIN - PAYMENTS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_pays")
def admin_pays(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    pays = list(col_pay_req.find({"status":"pending"}).sort("_id",-1).limit(10))
    if not pays: bot.send_message(call.message.chat.id, "✅ কোনো pending payment নেই।"); return
    for p in pays:
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok_{p['user_id']}_{p['plan']}"),
               types.InlineKeyboardButton("❌ Reject",  callback_data=f"pay_no_{p['user_id']}"))
        bot.send_message(call.message.chat.id,
            f"💳 <b>Payment</b>\n👤 <code>{p['user_id']}</code>\n"
            f"📦 {p['plan']} | 📝 {p.get('txn','')[:100]}", reply_markup=kb)

# ===== ADMIN - PRICES =====
@bot.callback_query_handler(func=lambda c: c.data == "a_prices")
def admin_prices(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    for p in ["silver","gold","lifetime"]:
        kb.add(types.InlineKeyboardButton(f"✏️ {p.title()} ৳{sg(f'price_{p}','N/A')}", callback_data=f"setprice_{p}"))
    bot.send_message(call.message.chat.id, "💰 <b>Plan Prices</b>", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("setprice_"))
def setprice(call):
    if not is_admin(call.from_user.id): return
    plan = call.data[9:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"{plan.title()} এর নতুন মূল্য লিখুন:")
    bot.register_next_step_handler(call.message, lambda m: (ss(f"price_{plan}", m.text.strip()), bot.reply_to(m, f"✅ {plan.title()}: ৳{m.text.strip()}")))

# ===== ADMIN - LIMITS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_limits")
def admin_limits(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    for k,l in [("free","free_limit"),("silver","limit_silver"),("gold","limit_gold"),("lifetime","limit_lifetime"),("premium","premium_limit")]:
        kb.add(types.InlineKeyboardButton(f"✏️ {k.title()}: {sg(l, FREE_LIMIT if k=='free' else PREMIUM_LIMIT)}", callback_data=f"setlimit_{l}"))
    bot.send_message(call.message.chat.id, "⚙️ <b>Upload Limits</b>", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("setlimit_"))
def setlimit(call):
    if not is_admin(call.from_user.id): return
    key = call.data[9:]
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"{key} এর নতুন limit লিখুন:")
    bot.register_next_step_handler(call.message, lambda m: (ss(key, m.text.strip()), bot.reply_to(m, f"✅ {key}: {m.text.strip()}")) if m.text.strip().isdigit() else bot.reply_to(m, "❌ সংখ্যা দিন।"))

# ===== ADMIN - CHANNELS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_channels")
def admin_chs(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    chs  = list(col_channels.find())
    text = "📁 <b>Force Join Channels</b>\n\n" + ("".join(f"• @{c['username']}\n" for c in chs) or "কোনো channel নেই।")
    kb   = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("➕ যোগ", callback_data="ch_add"),
           types.InlineKeyboardButton("🗑 সরান", callback_data="ch_del"))
    bot.send_message(call.message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "ch_add")
def ch_add(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Channel username পাঠান (@ছাড়া):")
    bot.register_next_step_handler(call.message, lambda m: _ch_add_save(m))

def _ch_add_save(msg):
    if not is_admin(msg.from_user.id): return
    un = (msg.text or "").strip().lstrip("@")
    try:
        col_channels.insert_one({"username": un})
        bot.reply_to(msg, f"✅ @{un} যোগ হয়েছে!")
    except DuplicateKeyError:
        bot.reply_to(msg, "⚠️ ইতিমধ্যে আছে!")

@bot.callback_query_handler(func=lambda c: c.data == "ch_del")
def ch_del(call):
    if not is_admin(call.from_user.id): return
    chs = list(col_channels.find())
    if not chs: bot.answer_callback_query(call.id, "কোনো channel নেই!", show_alert=True); return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    for c in chs: kb.add(types.InlineKeyboardButton(f"🗑 @{c['username']}", callback_data=f"chdel_{c['username']}"))
    bot.send_message(call.message.chat.id, "কোনটি সরাতে চান?", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("chdel_"))
def ch_del_do(call):
    if not is_admin(call.from_user.id): return
    col_channels.delete_one({"username": call.data[6:]})
    bot.answer_callback_query(call.id, f"✅ সরানো হয়েছে!", show_alert=True)

# ===== ADMIN - STORAGE =====
@bot.callback_query_handler(func=lambda c: c.data == "a_storage")
def admin_storage(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    total   = get_storage()
    fc      = col_files.count_documents({})
    by_type = list(col_files.aggregate([{"$group": {"_id": "$type", "c": {"$sum": 1}}}]))
    ttxt    = "\n".join(f"  • {r['_id']}: {r['c']}টি" for r in by_type)
    bot.send_message(call.message.chat.id,
        f"💾 <b>Storage Monitor</b>\n\n"
        f"📁 মোট সাইট: {fc}\n"
        f"💽 Total: <b>{fmt_bytes(total)}</b>\n\n"
        f"📊 <b>টাইপ অনুযায়ী:</b>\n{ttxt or 'N/A'}")

# ===== ADMIN - LOGS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_logs")
def admin_logs(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    logs = list(col_logs.find({},{"user_id":1,"action":1,"date":1}).sort("_id",-1).limit(20))
    text = "📋 <b>সর্বশেষ ২০টি Log:</b>\n\n"
    for l in logs:
        text += f"👤 <code>{l['user_id']}</code> | {l['action']} | {l['date'][:16]}\n"
    bot.send_message(call.message.chat.id, text[:4000])

# ===== ADMIN - CSV EXPORT =====
@bot.callback_query_handler(func=lambda c: c.data == "a_csv")
def admin_csv(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id, "⏳ CSV তৈরি হচ্ছে...")
    users = list(col_users.find({},{"id":1,"username":1,"joined":1,"invites":1}))
    buf   = io.StringIO()
    wr    = csv.writer(buf)
    wr.writerow(["ID","Username","Joined","Invites","Premium","Sites"])
    for u in users:
        p  = "Yes" if is_premium(u["id"]) else "No"
        sc = col_files.count_documents({"user_id": u["id"]})
        wr.writerow([u["id"], u.get("username",""), u.get("joined",""), u.get("invites",0), p, sc])
    out = io.BytesIO(buf.getvalue().encode()); out.name = f"users_{datetime.now().strftime('%Y%m%d')}.csv"
    bot.send_document(call.message.chat.id, out, caption="📤 Users CSV")

# ===== ADMIN - SEARCH USER =====
@bot.callback_query_handler(func=lambda c: c.data == "a_search")
def admin_search(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "🔍 User ID বা @username লিখুন:")
    bot.register_next_step_handler(call.message, _search_user)

def _search_user(msg):
    if not is_admin(msg.from_user.id): return
    q = (msg.text or "").strip().lstrip("@")
    u = col_users.find_one({"id": int(q)}) if q.isdigit() else col_users.find_one({"username": q})
    if not u: bot.reply_to(msg, f"❌ '{q}' পাওয়া যায়নি।"); return
    uid = u["id"]
    p   = col_premium.find_one({"user_id": uid})
    pinfo = ""
    if p and is_premium(uid):
        try: pinfo = f"\n💎 {p['plan']} → {p['expiry'][:10]}"
        except: pass
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("💎 Premium দিন", callback_data=f"agp_{uid}"),
           types.InlineKeyboardButton("🚫 Ban/Unban",   callback_data=f"admin_ban_{uid}"))
    kb.add(types.InlineKeyboardButton("💔 Premium সরান", callback_data=f"armp_{uid}"))
    bot.reply_to(msg,
        f"👤 <b>User Info</b>\n🆔 <code>{uid}</code>\n"
        f"@{u.get('username','N/A')} | 📅 {u.get('joined','N/A')[:10]}\n"
        f"🌐 Sites: {col_files.count_documents({'user_id':uid})}\n"
        f"🌟 {'💎 Premium' if is_premium(uid) else '🆓 Free'}{pinfo}",
        reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("agp_"))
def agp(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data[4:])
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"<code>{uid}</code> কে Premium:\nফরম্যাট: PLAN DAYS")
    bot.register_next_step_handler(call.message, lambda m: _agp_save(m, uid))

def _agp_save(msg, uid):
    if not is_admin(msg.from_user.id): return
    try:
        plan, days = msg.text.split()[0], int(msg.text.split()[1])
        exp = (datetime.now() + timedelta(days=days)).isoformat()
        col_premium.update_one({"user_id": uid}, {"$set": {"user_id": uid, "plan": plan, "expiry": exp}}, upsert=True)
        bot.reply_to(msg, f"✅ Done!")
        try: bot.send_message(uid, f"🎉 {plan.title()} Premium! ({days}d)")
        except: pass
    except: bot.reply_to(msg, "❌ ভুল ফরম্যাট।")

@bot.callback_query_handler(func=lambda c: c.data.startswith("armp_"))
def armp(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data[5:])
    col_premium.delete_one({"user_id": uid})
    bot.answer_callback_query(call.id, f"✅ Premium সরানো হয়েছে!", show_alert=True)
    try: bot.send_message(uid, "⚠️ আপনার Premium সরানো হয়েছে।")
    except: pass

# ===== ADMIN - ADD ADMIN =====
@bot.callback_query_handler(func=lambda c: c.data == "a_addadmin")
def add_admin(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    admins = list(col_admins.find())
    text   = "👤 <b>Admins:</b>\n" + "".join(f"• <code>{a['id']}</code>\n" for a in admins)
    kb     = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("➕ যোগ করুন", callback_data="do_addadmin"),
           types.InlineKeyboardButton("🗑 সরান",     callback_data="do_remadmin"))
    bot.send_message(call.message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "do_addadmin")
def do_addadmin(call):
    if call.from_user.id != OWNER_ID: bot.answer_callback_query(call.id, "❌ শুধু Owner!", show_alert=True); return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "নতুন Admin এর User ID পাঠান:")
    bot.register_next_step_handler(call.message, lambda m: _save_addadmin(m))

def _save_addadmin(msg):
    if msg.from_user.id != OWNER_ID: return
    uid_s = (msg.text or "").strip()
    if not uid_s.isdigit(): bot.reply_to(msg, "❌ বৈধ ID দিন।"); return
    uid = int(uid_s)
    col_admins.update_one({"id": uid}, {"$set": {"id": uid}}, upsert=True)
    bot.reply_to(msg, f"✅ <code>{uid}</code> Admin হয়েছেন!")
    try: bot.send_message(uid, "🎉 আপনাকে Admin করা হয়েছে!")
    except: pass

@bot.callback_query_handler(func=lambda c: c.data == "do_remadmin")
def do_remadmin(call):
    if call.from_user.id != OWNER_ID: bot.answer_callback_query(call.id, "❌ শুধু Owner!", show_alert=True); return
    admins = [a for a in col_admins.find() if a["id"] != OWNER_ID]
    if not admins: bot.answer_callback_query(call.id, "অন্য কোনো admin নেই!", show_alert=True); return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    for a in admins: kb.add(types.InlineKeyboardButton(f"🗑 {a['id']}", callback_data=f"remadm_{a['id']}"))
    bot.send_message(call.message.chat.id, "কাকে সরাবেন?", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("remadm_"))
def remadm(call):
    if call.from_user.id != OWNER_ID: return
    uid = int(call.data[7:])
    if uid == OWNER_ID: bot.answer_callback_query(call.id, "❌ Owner সরানো যাবে না!", show_alert=True); return
    col_admins.delete_one({"id": uid})
    bot.answer_callback_query(call.id, f"✅ {uid} সরানো হয়েছে!", show_alert=True)

# ===== ADMIN - PAYMENT METHODS =====
@bot.callback_query_handler(func=lambda c: c.data == "a_paymeth")
def admin_paymeth(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    meths = list(col_pay_meth.find({"active":1}))
    text  = "💳 <b>Payment Methods</b>\n\n"
    for m in meths: text += f"• <b>{m['name']}</b>: <code>{m['number']}</code>\n  {m.get('note','')}\n\n"
    if not meths: text += "কোনো method নেই।"
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("➕ যোগ", callback_data="pm_add"),
           types.InlineKeyboardButton("🗑 সরান", callback_data="pm_del"))
    bot.send_message(call.message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "pm_add")
def pm_add(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "ফরম্যাট: নাম | নম্বর | নির্দেশনা\nউদাহরণ: <code>বিকাশ | 01XXXXXXXXX | Send Money করুন</code>")
    bot.register_next_step_handler(call.message, _pm_add_save)

def _pm_add_save(msg):
    if not is_admin(msg.from_user.id): return
    try:
        parts = [p.strip() for p in (msg.text or "").split("|")]
        col_pay_meth.insert_one({"name": parts[0], "number": parts[1],
                                  "note": parts[2] if len(parts)>2 else "", "active": 1})
        bot.reply_to(msg, f"✅ {parts[0]} যোগ হয়েছে!")
    except Exception as e:
        bot.reply_to(msg, f"❌ সমস্যা: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "pm_del")
def pm_del(call):
    if not is_admin(call.from_user.id): return
    meths = list(col_pay_meth.find({"active":1}))
    if not meths: bot.answer_callback_query(call.id, "কোনো method নেই!", show_alert=True); return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    for m in meths: kb.add(types.InlineKeyboardButton(f"🗑 {m['name']}", callback_data=f"pmdel_{str(m['_id'])}"))
    bot.send_message(call.message.chat.id, "কোনটি সরাবেন?", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("pmdel_"))
def pmdel_do(call):
    if not is_admin(call.from_user.id): return
    try:
        col_pay_meth.update_one({"_id": bson.ObjectId(call.data[6:])}, {"$set": {"active":0}})
        bot.answer_callback_query(call.id, "✅ সরানো হয়েছে!", show_alert=True)
    except: bot.answer_callback_query(call.id, "❌ সমস্যা!", show_alert=True)

# ===== ADMIN - MESSAGE USER =====
@bot.callback_query_handler(func=lambda c: c.data == "a_msguser")
def admin_msguser(call):
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📨 Target User ID লিখুন:")
    bot.register_next_step_handler(call.message, _msguser_id)

def _msguser_id(msg):
    if not is_admin(msg.from_user.id): return
    uid_s = (msg.text or "").strip()
    if not uid_s.isdigit(): bot.reply_to(msg, "❌ বৈধ ID দিন।"); return
    bot.reply_to(msg, f"📨 <code>{uid_s}</code> কে message লিখুন:")
    bot.register_next_step_handler(msg, lambda m: _msguser_send(m, int(uid_s)))

def _msguser_send(msg, uid):
    if not is_admin(msg.from_user.id): return
    try:
        bot.send_message(uid, f"📨 <b>Admin Message:</b>\n\n{msg.text}")
        bot.reply_to(msg, f"✅ পাঠানো হয়েছে!")
    except Exception as e:
        bot.reply_to(msg, f"❌ পাঠানো যায়নি: {e}")

# ================= BACKGROUND TASKS =================
def expiry_checker():
    while True:
        try:
            for f in col_files.find({"expiry": {"$ne": None}}):
                try:
                    if datetime.fromisoformat(f["expiry"]) < datetime.now():
                        col_files.delete_one({"code": f["code"]})
                        try: shutil.rmtree(os.path.join(UPLOAD_DIR, str(f["user_id"]), f["code"]))
                        except: pass
                except: pass
            for p in col_premium.find():
                try:
                    diff = datetime.fromisoformat(p["expiry"]) - datetime.now()
                    if timedelta(days=0) < diff < timedelta(days=3):
                        try: bot.send_message(p["user_id"], f"⚠️ Premium {diff.days+1} দিন পরে শেষ হবে!")
                        except: pass
                except: pass
        except Exception as e:
            logger.error(f"Expiry checker: {e}")
        time.sleep(3600)

# ================= FLASK ROUTES =================
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))

@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return resp

# ===== HOME =====
@app.route('/')
def home():
    bu  = get_bot_username()
    php = check_php()
    tu  = col_users.count_documents({})
    ts  = col_files.count_documents({})
    php_s = col_files.count_documents({"type":"php"})
    tv_agg = list(col_files.aggregate([{"$group":{"_id":None,"t":{"$sum":"$views"}}}]))
    tv  = tv_agg[0]["t"] if tv_agg else 0
    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🐘 PHP Hosting Bot</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0f0f1a;color:#fff;font-family:'Segoe UI',sans-serif}}
header{{background:linear-gradient(135deg,#1a1a3e,#0f0f1a);padding:80px 24px;text-align:center;border-bottom:1px solid #222}}
h1{{font-size:40px;margin-bottom:8px;background:linear-gradient(135deg,#7c3aed,#2563eb);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.sub{{color:#888;font-size:18px;margin-bottom:32px}}.cta{{display:inline-block;background:#7c3aed;color:#fff;padding:14px 36px;border-radius:10px;text-decoration:none;font-size:16px;font-weight:600}}
.stats{{display:flex;justify-content:center;gap:40px;padding:48px 24px;background:#111120;flex-wrap:wrap}}
.stat .num{{font-size:36px;font-weight:bold;color:#7c3aed}}.stat .label{{color:#888;font-size:14px;margin-top:4px}}
.features{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;padding:60px 24px;max-width:1000px;margin:0 auto}}
.feature{{background:#1a1a2e;padding:28px;border-radius:12px;border:1px solid #2a2a4e}}
.feature .icon{{font-size:36px;margin-bottom:12px}}.feature h3{{margin-bottom:8px}}.feature p{{color:#666;font-size:14px;line-height:1.6}}
.php-badge{{display:inline-block;background:{'#166534' if php else '#7f1d1d'};color:{'#86efac' if php else '#fca5a5'};padding:4px 12px;border-radius:20px;font-size:13px;margin-bottom:16px}}
footer{{text-align:center;padding:40px;color:#444;border-top:1px solid #1a1a2e}}</style></head>
<body><header><div style="font-size:64px;margin-bottom:16px">🐘</div>
<h1>PHP Hosting Bot</h1>
<div class="php-badge">🐘 PHP {'✅ Active' if php else '❌ Not Installed'}</div>
<p class="sub">Telegram-এ PHP, HTML, ZIP সাইট হোস্ট করুন!</p>
<a href="https://t.me/{bu}" class="cta">🚀 Bot শুরু করুন</a></header>
<div class="stats">
<div class="stat"><div class="num">{tu:,}</div><div class="label">👥 ইউজার</div></div>
<div class="stat"><div class="num">{ts:,}</div><div class="label">🌐 সাইট</div></div>
<div class="stat"><div class="num">{php_s:,}</div><div class="label">🐘 PHP সাইট</div></div>
<div class="stat"><div class="num">{tv:,}</div><div class="label">👁 Views</div></div>
</div>
<div class="features">
<div class="feature"><div class="icon">🐘</div><h3>PHP Execution</h3><p>PHP ফাইল সরাসরি execute হবে। Sandbox সুরক্ষায় নিরাপদ।</p></div>
<div class="feature"><div class="icon">📦</div><h3>ZIP Project</h3><p>পুরো PHP প্রজেক্ট ZIP করে আপলোড করুন।</p></div>
<div class="feature"><div class="icon">🔒</div><h3>Sandboxed</h3><p>exec, system, shell_exec সব ব্লক। নিরাপদ execution।</p></div>
<div class="feature"><div class="icon">🌐</div><h3>Multi-format</h3><p>PHP, HTML, ZIP, ছবি, ভিডিও সব ধরনের ফাইল।</p></div>
<div class="feature"><div class="icon">📊</div><h3>Analytics</h3><p>Views, Unique visitor, দেশভিত্তিক তথ্য।</p></div>
<div class="feature"><div class="icon">💎</div><h3>Premium</h3><p>Silver, Gold, Lifetime প্ল্যানে বেশি সাইট হোস্ট করুন।</p></div>
</div>
<footer>© 2025 PHP Hosting Bot | <a href="https://t.me/{bu}" style="color:#7c3aed">@{bu}</a></footer>
</body></html>"""

# ===== SHORT URL REDIRECT =====
@app.route('/s/<code>')
def short_redirect(code):
    r = col_short.find_one({"code": code}, {"url":1})
    if not r: return "❌ Not Found", 404
    col_short.update_one({"code": code}, {"$inc": {"clicks": 1}})
    return redirect(r["url"], 302)

# ===== PASSWORD AUTH =====
@app.route('/v/<slug>/auth', methods=['POST'])
def auth_site(slug):
    f = col_files.find_one({"$or": [{"slug": slug}, {"code": slug}]}, {"code":1,"password":1})
    if not f: return "Not Found", 404
    if request.form.get('pw','') == f["password"]:
        session[f'auth_{f["code"]}'] = True
        return redirect(f'/v/{slug}')
    return _pass_page(slug, error=True)

def _pass_page(slug, error=False):
    err = '<p style="color:#e05252;margin-bottom:12px">❌ ভুল পাসওয়ার্ড!</p>' if error else ""
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🔒 Password Required</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0f0f1a;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:24px}}
.box{{max-width:340px;width:100%}}h1{{font-size:22px;margin-bottom:8px}}.sub{{color:#888;margin-bottom:24px}}
input{{width:100%;background:#1a1a2e;border:1px solid #2a2a4e;color:#fff;padding:12px;border-radius:8px;font-size:16px;margin-bottom:12px;outline:none}}
input:focus{{border-color:#7c3aed}}button{{width:100%;background:#7c3aed;color:#fff;border:none;padding:12px;border-radius:8px;font-size:16px;cursor:pointer}}</style></head>
<body><div class="box"><div style="font-size:48px;margin-bottom:12px">🔒</div>
<h1>সুরক্ষিত সাইট</h1><p class="sub">পাসওয়ার্ড দিন</p>
{err}<form method="POST" action="/v/{slug}/auth">
<input type="password" name="pw" placeholder="Password" autofocus required>
<button>প্রবেশ করুন →</button></form></div></body></html>"""

# ===== MAIN SITE SERVER =====
@app.route('/v/<slug>')
@app.route('/v/<slug>/<path:subpath>')
def serve_site(slug, subpath=""):
    ip  = request.remote_addr
    ua  = request.headers.get('User-Agent','')
    f   = col_files.find_one({"$or": [{"slug": slug}, {"code": slug}]})
    if not f: return "❌ Site Not Found", 404

    # Expiry check
    if f.get("expiry"):
        try:
            if datetime.fromisoformat(f["expiry"]) < datetime.now():
                col_files.delete_one({"code": f["code"]})
                return "❌ সাইটের মেয়াদ শেষ", 404
        except: pass

    # Password check
    if f.get("password"):
        if not session.get(f'auth_{f["code"]}'):
            return _pass_page(slug)

    site_dir = os.path.join(UPLOAD_DIR, str(f["user_id"]), f["code"])
    if not os.path.exists(site_dir): return "❌ Site files not found", 404

    # Determine file to serve
    if subpath:
        target = os.path.realpath(os.path.join(site_dir, subpath))
    else:
        # Auto-detect index file
        target = None
        for idx in ["index.php", "index.html", "index.htm"]:
            candidate = os.path.join(site_dir, idx)
            if os.path.exists(candidate):
                target = candidate; break
        if not target:
            # directory listing
            items = os.listdir(site_dir)
            rows  = "".join(
                f'<tr><td>{"📁" if os.path.isdir(os.path.join(site_dir,n)) else "📄"}</td>'
                f'<td><a href="/v/{slug}/{n}">{n}</a></td>'
                f'<td>{fmt_bytes(os.path.getsize(os.path.join(site_dir,n))) if os.path.isfile(os.path.join(site_dir,n)) else "-"}</td></tr>'
                for n in sorted(items))
            return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>📂 {slug}</title>
<style>body{{background:#0f0f1a;color:#fff;font-family:sans-serif;padding:24px}}h1{{color:#7c3aed;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse}}td{{padding:8px 12px;border-bottom:1px solid #222}}a{{color:#a78bfa}}</style></head>
<body><h1>📂 /{slug}</h1><table>{rows}</table></body></html>"""

    if not target or not os.path.exists(target):
        return "❌ File not found", 404

    # Security: path traversal check
    if not os.path.realpath(target).startswith(os.path.realpath(site_dir)):
        return "❌ Forbidden", 403

    # View count
    col_files.update_one({"code": f["code"]}, {"$inc": {"views": 1},
        "$set": {"last_view": datetime.now().strftime("%Y-%m-%d %H:%M")}})
    col_views.insert_one({"code": f["code"], "user_id": f["user_id"],
        "ip": ip, "country": request.headers.get("CF-IPCountry","Unknown"),
        "ua": ua[:200], "date": datetime.now().strftime("%Y-%m-%d %H:%M")})

    ext = target.rsplit('.', 1)[-1].lower() if '.' in target else ''

    # PHP execution
    if ext in PHP_EXT:
        if not check_php():
            return """<h2 style='font-family:sans-serif;color:#e55'>🐘 PHP Not Installed</h2>
            <p>Server-এ PHP ইনস্টল নেই। Admin কে জানান।</p>""", 503
        qs = request.query_string.decode('utf-8', errors='ignore')
        pd = request.get_data()
        headers = dict(request.headers)
        body, status = execute_php(target, site_dir, qs, pd, request.method, headers)
        return Response(body, status=status, content_type='text/html; charset=utf-8')

    # Static files
    import flask
    directory = os.path.dirname(target)
    filename  = os.path.basename(target)
    mime, _   = mimetypes.guess_type(filename)
    resp      = make_response(flask.send_from_directory(directory, filename))
    if mime: resp.headers['Content-Type'] = mime
    return resp

# ===== ADMIN WEB PANEL =====
@app.route('/admin')
def admin_web():
    auth = request.args.get('key','')
    doc  = col_settings.find_one({"key":"admin_web_key"})
    if not doc:
        k = secrets.token_hex(16); ss("admin_web_key", k)
        return f"Key set. /admin?key={k}", 200
    if auth != doc['value']: return "❌ Unauthorized", 403
    tu = col_users.count_documents({}); ts = col_files.count_documents({})
    php_s = col_files.count_documents({"type":"php"})
    tv_agg = list(col_files.aggregate([{"$group":{"_id":None,"t":{"$sum":"$views"}}}]))
    tv = tv_agg[0]["t"] if tv_agg else 0
    prem = col_premium.count_documents({}); php_ok = check_php()
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚙️ Admin</title><style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0f0f1a;color:#fff;font-family:'Segoe UI',sans-serif;padding:24px}}
h1{{font-size:22px;color:#7c3aed;margin-bottom:24px}}.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:16px;margin-bottom:32px}}
.card{{background:#1a1a2e;border-radius:10px;padding:20px;text-align:center;border:1px solid #2a2a4e}}
.card .num{{font-size:30px;font-weight:bold;color:#7c3aed}}.card .lbl{{color:#888;font-size:12px;margin-top:4px}}</style></head>
<body><h1>⚙️ Admin Dashboard</h1><div class="cards">
<div class="card"><div class="num">{tu}</div><div class="lbl">👥 Users</div></div>
<div class="card"><div class="num">{ts}</div><div class="lbl">🌐 Sites</div></div>
<div class="card"><div class="num">{php_s}</div><div class="lbl">🐘 PHP Sites</div></div>
<div class="card"><div class="num">{tv}</div><div class="lbl">👁 Views</div></div>
<div class="card"><div class="num">{prem}</div><div class="lbl">💎 Premium</div></div>
<div class="card"><div class="num">{'✅' if php_ok else '❌'}</div><div class="lbl">PHP Status</div></div>
</div></body></html>"""

# ===== WEBHOOK =====
@app.route(f'/webhook/{WEBHOOK_SECRET}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode())])
        return '', 200
    return 'Bad request', 400

@app.errorhandler(404)
def not_found(e):
    return "<h2 style='font-family:sans-serif'>❌ 404 — পাওয়া যায়নি</h2>", 404

# ================= FLASK SERVER =================
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

# ================= MAIN =================
if __name__ == "__main__":
    Thread(target=run_flask,      daemon=True).start()
    Thread(target=expiry_checker, daemon=True).start()

    if USE_WEBHOOK and WEBHOOK_URL:
        wurl = f"{WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}"
        bot.remove_webhook(); time.sleep(1)
        bot.set_webhook(url=wurl)
        logger.info(f"✅ Webhook: {wurl}")
        import signal; signal.pause()
    else:
        logger.info("✅ Bot polling...")
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
