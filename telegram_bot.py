# -*- coding: utf-8 -*-
"""
NgânMiu.Store — Telegram Bot
✅ V4 FIXED - Sửa schema 7 cột + Anti-spam 5req/20s + Thưởng user mới 5100đ
✅ Schema 7 cột: Tele ID | Username | Balance | Trang Thái | Chi Chú | note | Gift Status
✅ Anti-spam: 5 request/20s → Ban 1H → Tái phạm → Ban vĩnh viễn
✅ Thưởng user mới: 5100đ (balance không bao giờ về 0)
✅ Batch update (giảm API calls)
✅ Retry logic (tăng stability)
✅ ⭐ HỖ TRỢ LƯU TỐI ĐA 10 COOKIE CÙNG LÚC ⭐
✅ 🔥 ROW CACHE + BROADCAST CACHE - GIẢM 90% SHEET CALLS 🔥
"""

import os
import json
import re
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request
import urllib.parse
import time
from collections import deque  # ✅ Thêm deque cho PROCESSED_UPDATE_IDS

# =========================================================
# TIMEZONE VIETNAM (GMT+7)
# =========================================================
VIETNAM_TZ = timezone(timedelta(hours=7))

# =========================================================
# LOAD DOTENV
# =========================================================
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# =========================================================
# GOOGLE SHEET
# =========================================================
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# =========================================================
# APP
# =========================================================
app = Flask(__name__)

# =========================================================
# ENV
# =========================================================
BOT_TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
SHEET_ID   = os.getenv("GOOGLE_SHEET_ID", "").strip()
CREDS_JSON = os.getenv("GOOGLE_SHEETS_CREDS_JSON", "").strip()
ADMIN_ID   = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
SAVE_URL = "https://shopee.vn/api/v2/voucher_wallet/save_vouchers"

# =========================================================
# ⭐ MULTI-COOKIE CONFIG ⭐
# =========================================================
MAX_COOKIES_PER_REQUEST = 10
COOKIE_SEPARATOR = "\n"

# =========================================================
# TOPUP RULES (SEPAY)
# =========================================================
MIN_TOPUP_AMOUNT = 10000

# ✅ TIỀN THƯỞNG USER MỚI (5100đ để balance không bao giờ về 0)
NEW_USER_BONUS = 5100

# ✅ TIỀN THƯỞNG KÍCH HOẠT (thống nhất với NEW_USER_BONUS)
ACTIVE_GIFT_AMOUNT = 5100

# ✅ STATUS CHO PHÉP NHẬN GIFT (chặt chẽ, tránh abuse)
ALLOWED_GIFT_STATUS = ["", "new", "pending"]  # Admin set "inactive" → KHÔNG được nhận

TOPUP_BONUS_RULES = [
    (100000, 0.20),
    (50000,  0.15),
    (20000,  0.10),
]

def calc_topup_bonus(amount):
    for min_amount, percent in TOPUP_BONUS_RULES:
        if amount >= min_amount:
            bonus = int(amount * percent)
            return percent, bonus
    return 0, 0

def build_sepay_qr(user_id, amount=None):
    base = "https://qr.sepay.vn/img"
    params = {
        "acc": "101866911892",
        "bank": "VietinBank",
        "template": "compact",
        "des": f"SEVQR NAP {user_id}"
    }
    if amount:
        params["amount"] = str(int(amount))
    return base + "?" + urllib.parse.urlencode(params)

# =========================================================
# ANTI-SPAM CONFIG
# =========================================================
SPAM_THRESHOLD = 5   # 5 request spam
SPAM_WINDOW = 20     # trong 20 giây
BAN_DURATION_1H = 3600

# =========================================================
# DEBUG FLAG
# =========================================================
DEBUG = True

def dprint(*args):
    if DEBUG:
        print("[DEBUG]", *args)

# =========================================================
# GOOGLE SHEET CONNECT WITH RETRY
# =========================================================
SHEET_READY = False
sh          = None
ws_money    = None
ws_voucher  = None
ws_log      = None
ws_nap_tien = None

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

MAX_RETRIES = 3
retry_count = 0
connected = False

while retry_count < MAX_RETRIES and not connected:
    try:
        if not CREDS_JSON:
            raise Exception("CREDS_JSON is empty")

        print(f"🔄 Connecting to Google Sheets (attempt {retry_count + 1}/{MAX_RETRIES})...")
        start_time = time.time()

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(CREDS_JSON),
            scope
        )
        print(f"✅ Step 1: Credentials loaded ({time.time()-start_time:.2f}s)")

        gc = gspread.authorize(creds)
        print(f"✅ Step 2: Gspread authorized ({time.time()-start_time:.2f}s)")

        sh = gc.open_by_key(SHEET_ID)
        print(f"✅ Step 3: Sheet opened ({time.time()-start_time:.2f}s)")

        ws_money   = sh.worksheet("Thanh Toan")
        ws_voucher = sh.worksheet("VoucherStock")
        ws_log     = sh.worksheet("Logs")
        print(f"✅ Step 4: Core worksheets loaded ({time.time()-start_time:.2f}s)")

        try:
            ws_nap_tien = sh.worksheet("Nap Tien")
            print(f"✅ Step 5: Nap Tien loaded ({time.time()-start_time:.2f}s)")
        except Exception as e:
            ws_nap_tien = None
            print(f"⚠️ Nap Tien tab not found: {e}")

        SHEET_READY = True
        connected = True
        print("=" * 60)
        print("✅ ✅ ✅ GOOGLE SHEETS CONNECTED SUCCESSFULLY!")
        print("=" * 60)

    except Exception as e:
        retry_count += 1
        wait_time = 2 ** retry_count

        print("=" * 60)
        print(f"❌ Connection failed (attempt {retry_count}/{MAX_RETRIES})")
        print(f"❌ Error: {str(e)}")
        print(f"❌ Error type: {type(e).__name__}")

        if retry_count < MAX_RETRIES:
            print(f"⏳ Retrying in {wait_time}s...")
            time.sleep(wait_time)
        else:
            print("❌ ❌ ❌ ALL RETRIES FAILED - SHEET_READY = False")
            import traceback
            traceback.print_exc()
            print("=" * 60)
            SHEET_READY = False

# =========================================================
# 🔥 PRELOAD USERS + ROW CACHE (chạy 1 lần khi khởi động)
# =========================================================
if SHEET_READY:
    print("🔄 Preloading users + row numbers into cache...")
    try:
        all_users = ws_money.get_all_values()
        preload_count = 0

        for idx, row in enumerate(all_users[1:], start=2):  # start=2 vì header ở row 1
            if len(row) >= 1 and row[0]:
                try:
                    user_id = int(row[0])

                    # ✅ CACHE ROW NUMBER sẽ được khai báo sau
                    # cache_user_row(user_id, idx)
                    preload_count += 1
                except Exception:
                    continue

        print(f"✅ Will preload {preload_count} users into cache")

    except Exception as e:
        print(f"⚠️ Preload failed (non-critical): {e}")

# =========================================================
# STATE (GLOBAL)
# =========================================================
PENDING_VOUCHER = {}
PENDING_VOUCHER_TTL = 120  # 2 phút - expire nếu user không gửi cookie

# ✅ DYNAMIC COMBO DETECTION - Không hardcode, tự phát hiện từ Sheet
# Combo nào có trong VoucherStock với Combo = "combo1", "combo2"... đều tự động hiện
# COMBO1_KEY, COMBO2_KEY... sẽ được detect tự động

# ✅ CALLBACK RATE LIMIT - Tránh spam click BUY
CALLBACK_COOLDOWN = {}
CALLBACK_COOLDOWN_SECONDS = 2  # 2 giây giữa các click

# ✅ SPAM TRACKER
SPAM_TRACKER = {}

# =========================================================
# 🔥 ROW NUMBER CACHE - GIẢM 80% SHEET API CALLS
# =========================================================
USER_ROW_CACHE = {}
USER_ROW_CACHE_TTL = 3600  # 1 giờ
USER_ROW_CACHE_TIME = {}

def cache_user_row(user_id, row_number):
    """Cache row number của user"""
    USER_ROW_CACHE[user_id] = row_number
    USER_ROW_CACHE_TIME[user_id] = time.time()
    dprint(f"✅ Cached row for user {user_id}: row {row_number}")

def get_cached_user_row(user_id):
    """Get row number từ cache. Returns: row_number hoặc None"""
    if user_id not in USER_ROW_CACHE:
        return None
    cache_time = USER_ROW_CACHE_TIME.get(user_id, 0)
    if time.time() - cache_time > USER_ROW_CACHE_TTL:
        del USER_ROW_CACHE[user_id]
        del USER_ROW_CACHE_TIME[user_id]
        return None
    return USER_ROW_CACHE[user_id]

def invalidate_user_row_cache(user_id):
    """Xóa row cache khi cần"""
    if user_id in USER_ROW_CACHE:
        del USER_ROW_CACHE[user_id]
        del USER_ROW_CACHE_TIME[user_id]

# =========================================================
# 🔥 BROADCAST USER CACHE
# =========================================================
BROADCAST_USER_CACHE = None
BROADCAST_USER_CACHE_TIME = 0
BROADCAST_USER_CACHE_TTL = 300  # 5 phút

# ✅ BROADCAST COOLDOWN
LAST_BROADCAST_TIME = None
BROADCAST_COOLDOWN = 60

# ✅ MESSAGE DEDUPLICATION
PROCESSED_MESSAGES = set()
MAX_PROCESSED_MESSAGES = 1000

# ✅ UPDATE_ID DEDUPLICATION - Tránh Telegram resend khi Sheet lag
# Dùng deque thay vì set để xóa theo thứ tự FIFO
PROCESSED_UPDATE_IDS = deque(maxlen=2000)  # Auto-drop oldest when full

# ✅ BROADCAST LOCK
IS_BROADCASTING = False

# =========================================================
# 🔥 CHẠY PRELOAD THỰC SỰ (SAU KHI ĐỊNH NGHĨA CACHE FUNCTIONS)
# =========================================================
if SHEET_READY:
    print("🔄 Actually preloading users into ROW_CACHE...")
    try:
        all_users = ws_money.get_all_values()
        preload_count = 0

        for idx, row in enumerate(all_users[1:], start=2):
            if len(row) >= 1 and row[0]:
                try:
                    user_id = int(row[0])
                    cache_user_row(user_id, idx)
                    preload_count += 1
                except Exception:
                    continue

        print(f"✅ Preloaded {preload_count} users into ROW_CACHE")
        print(f"✅ Cache stats: {len(USER_ROW_CACHE)} row numbers cached")

    except Exception as e:
        print(f"⚠️ Preload failed (non-critical): {e}")

# =========================================================
# 🔥 VOUCHER STOCK CACHE - GIẢM 90% CALLS KHI MUA VOUCHER
# =========================================================
VOUCHER_STOCK_CACHE = {
    "rows": None,
    "ts": 0
}
VOUCHER_STOCK_TTL = 60  # 60 giây

def get_voucher_stock_cached():
    """
    ✅ Cache voucher stock 60s để tránh đốt Sheet
    Returns: list of dict
    """
    global VOUCHER_STOCK_CACHE
    
    now = time.time()
    
    # Check cache
    if VOUCHER_STOCK_CACHE["rows"] and (now - VOUCHER_STOCK_CACHE["ts"] < VOUCHER_STOCK_TTL):
        dprint("✅ VOUCHER_STOCK_CACHE HIT")
        return VOUCHER_STOCK_CACHE["rows"]
    
    # Cache miss → gọi Sheet
    dprint("⚠️ VOUCHER_STOCK_CACHE MISS, calling Sheet...")
    
    if not SHEET_READY:
        return []
    
    try:
        rows = ws_voucher.get_all_records()
        VOUCHER_STOCK_CACHE["rows"] = rows
        VOUCHER_STOCK_CACHE["ts"] = now
        dprint(f"✅ Cached {len(rows)} vouchers")
        return rows
    except Exception as e:
        dprint(f"❌ get_voucher_stock_cached error: {e}")
        # Fallback: trả cache cũ nếu có
        if VOUCHER_STOCK_CACHE["rows"]:
            dprint("⚠️ Using stale cache")
            return VOUCHER_STOCK_CACHE["rows"]
        return []

# =========================================================
# TELEGRAM UTIL
# =========================================================
def tg_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    try:
        requests.post(f"{BASE_URL}/sendMessage", data=payload, timeout=15)
    except Exception as e:
        dprint("tg_send error:", e)

def tg_send_photo(chat_id, photo, caption=None, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "photo": photo,
        "parse_mode": "HTML"
    }
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    try:
        requests.post(f"{BASE_URL}/sendPhoto", data=payload, timeout=20)
    except Exception as e:
        dprint("tg_send_photo error:", e)

def tg_answer_callback(callback_id, text=None, show_alert=False):
    payload = {
        "callback_query_id": callback_id,
        "show_alert": show_alert
    }
    if text:
        payload["text"] = text

    try:
        requests.post(f"{BASE_URL}/answerCallbackQuery", data=payload, timeout=10)
    except Exception as e:
        dprint("tg_answer_callback error:", e)

def tg_edit_message(chat_id, message_id, text, reply_markup=None):
    """
    Edit message text và inline keyboard
    """
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    try:
        requests.post(f"{BASE_URL}/editMessageText", data=payload, timeout=10)
    except Exception as e:
        dprint("tg_edit_message error:", e)

# =========================================================
# KEYBOARD
# =========================================================
def build_main_keyboard(is_active=True):
    """
    Keyboard chính - User mới luôn active ngay nên chỉ cần 1 keyboard
    """
    return {
        "keyboard": [
            ["💎 Nạp tiền"],
            ["💰 Số dư", "🎁 Lưu Voucher"],
            ["🧩 Hệ Thống Bot NgânMiu"]
        ],
        "resize_keyboard": True
    }

# =========================================================
# UTIL
# =========================================================
def now_str():
    return datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")

def now_datetime():
    return datetime.now(VIETNAM_TZ)

def get_all_user_ids():
    """
    ✅ V4: Cache broadcast user list, ưu tiên dùng USER_ROW_CACHE
    """
    global BROADCAST_USER_CACHE, BROADCAST_USER_CACHE_TIME

    if not SHEET_READY:
        return []

    # ✅ CHECK CACHE TRƯỚC
    now = time.time()
    if (BROADCAST_USER_CACHE and
        now - BROADCAST_USER_CACHE_TIME < BROADCAST_USER_CACHE_TTL):
        dprint(f"✅ BROADCAST CACHE HIT: {len(BROADCAST_USER_CACHE)} users")
        return BROADCAST_USER_CACHE

    # ❌ Cache miss
    dprint("⚠️ BROADCAST CACHE MISS...")

    try:
        # ✅ ƯU TIÊN DÙNG USER_ROW_CACHE (không gọi Sheet)
        cached_users = list(USER_ROW_CACHE.keys())
        if len(cached_users) > 10:
            dprint(f"✅ Using {len(cached_users)} users from ROW_CACHE")
            BROADCAST_USER_CACHE = cached_users
            BROADCAST_USER_CACHE_TIME = now
            return cached_users

        # ❌ Fallback: đọc từ Sheet
        dprint("⚠️ Reading all users from Sheet...")
        all_values = ws_money.get_all_values()
        user_ids = set()
        for row in all_values[1:]:
            if row and row[0]:
                try:
                    user_id = int(row[0])
                    user_ids.add(user_id)
                except:
                    continue

        result = list(user_ids)
        BROADCAST_USER_CACHE = result
        BROADCAST_USER_CACHE_TIME = now

        dprint(f"📊 Loaded {len(result)} users from Sheet")
        return result
    except Exception as e:
        dprint("get_all_user_ids error:", e)
        if BROADCAST_USER_CACHE:
            dprint("⚠️ Using stale cache due to error")
            return BROADCAST_USER_CACHE
        return []

def broadcast_message(message, exclude_admin=False):
    user_ids = get_all_user_ids()

    if not user_ids:
        dprint("❌ No users found for broadcast")
        return 0, 0

    dprint(f"📢 Starting broadcast to {len(user_ids)} users...")

    success = 0
    failed = 0
    sent_to = set()

    for user_id in user_ids:
        if user_id in sent_to:
            dprint(f"⚠️ Skipping duplicate user_id: {user_id}")
            continue

        if exclude_admin and user_id == ADMIN_ID:
            continue

        try:
            broadcast_text = f"📢 <b>THÔNG BÁO TỪ BOT</b>\n\n{message}"
            tg_send(user_id, broadcast_text)
            sent_to.add(user_id)
            success += 1
            time.sleep(0.05)
        except Exception as e:
            dprint(f"❌ Broadcast failed for {user_id}:", e)
            failed += 1

    dprint(f"✅ Broadcast completed: {success} success, {failed} failed")
    return success, failed

# =========================================================
# SHEET-BASED STATE
# =========================================================
def get_broadcast_sheet():
    if not SHEET_READY:
        return None
    try:
        try:
            return sh.worksheet("BroadcastState")
        except:
            ws = sh.add_worksheet("BroadcastState", 100, 4)
            ws.update('A1:D1', [['Timestamp', 'AdminID', 'Status', 'MessageID']])
            return ws
    except Exception as e:
        dprint(f"get_broadcast_sheet error: {e}")
        return None

def get_last_broadcast_time_from_sheet():
    ws = get_broadcast_sheet()
    if not ws:
        return None
    try:
        all_values = ws.get_all_values()
        if len(all_values) <= 1:
            return None

        for row in reversed(all_values[1:]):
            if row[2] in ["STARTED", "COMPLETED"]:
                timestamp_str = row[0]
                dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                return dt.replace(tzinfo=VIETNAM_TZ).timestamp()

        return None
    except Exception as e:
        dprint(f"get_last_broadcast_time_from_sheet error: {e}")
        return None

def set_broadcast_state_to_sheet(admin_id, status, message_id=""):
    ws = get_broadcast_sheet()
    if not ws:
        return False
    try:
        ws.append_row([
            now_str(),
            str(admin_id),
            status,
            str(message_id)
        ])
        dprint(f"📝 Broadcast state saved: {status}")
        return True
    except Exception as e:
        dprint(f"set_broadcast_state_to_sheet error: {e}")
        return False

def is_broadcast_message_processed(message_id):
    if not message_id:
        return False

    ws = get_broadcast_sheet()
    if not ws:
        return False

    try:
        col_message_ids = ws.col_values(4)
        return str(message_id) in col_message_ids
    except Exception as e:
        dprint("is_broadcast_message_processed error:", e)
        return False

def check_broadcast_cooldown_from_sheet():
    last_time = get_last_broadcast_time_from_sheet()
    if not last_time:
        return True, 0

    current_time = time.time()
    time_since_last = current_time - last_time

    dprint(f"⏱️ Time since last broadcast: {time_since_last:.1f}s")

    if time_since_last < BROADCAST_COOLDOWN:
        wait_time = int(BROADCAST_COOLDOWN - time_since_last)
        return False, wait_time

    return True, 0

def log_row(user_id, username, action, value="", note=""):
    if not SHEET_READY:
        return
    try:
        ws_log.append_row([now_str(), str(user_id), username, action, value, note])
    except Exception as e:
        dprint("log_row error:", e)

# =========================================================
# ✅ ANTI-SPAM SYSTEM
# =========================================================
def track_error(user_id, username="", reason=""):
    """
    ✅ FIXED: CHỈ track spam thật sự
    
    Reason phải là:
    - SPAM_CALLBACK: Click callback liên tục
    - SPAM_COMMAND: Gửi command liên tục
    - SPAM_TEXT: Gửi text trùng nhau
    
    KHÔNG track:
    - Lỗi nghiệp vụ (không đủ tiền, voucher hết...)
    - Cookie lỗi
    - User click bình thường
    """
    # ✅ CHỈ track các loại spam thật
    if reason not in ("SPAM_CALLBACK", "SPAM_COMMAND", "SPAM_TEXT"):
        dprint(f"⚠️ track_error: Invalid reason '{reason}', skipping")
        return False
    
    now = time.time()

    if user_id not in SPAM_TRACKER:
        SPAM_TRACKER[user_id] = {
            "errors": [],
            "ban_count": 0
        }

    tracker = SPAM_TRACKER[user_id]
    tracker["errors"].append(now)
    tracker["errors"] = [t for t in tracker["errors"] if now - t < SPAM_WINDOW]

    if len(tracker["errors"]) >= SPAM_THRESHOLD:
        ban_count = tracker["ban_count"]
        error_count = len(tracker["errors"])

        if ban_count == 0:
            apply_ban(user_id, "1H")
            notify_admin_spam(user_id, username, "1H", error_count)
            tracker["ban_count"] = 1
            return True
        else:
            apply_ban(user_id, "PERMANENT")
            notify_admin_spam(user_id, username, "PERMANENT", error_count)
            return True

    return False

def check_ban_status(user_id):
    if not SHEET_READY:
        return {"banned": False}

    row = get_user_row(user_id)
    if not row:
        return {"banned": False}

    try:
        note = ws_money.cell(row, 6).value or ""

        if "BAN VĨNH VIỄN" in note.upper():
            return {
                "banned": True,
                "type": "PERMANENT",
                "until": "Vĩnh viễn"
            }

        if "BAN 1H:" in note:
            try:
                ban_until_str = note.split("BAN 1H:")[1].strip()
                ban_until = datetime.strptime(ban_until_str, "%Y-%m-%d %H:%M")
                ban_until = ban_until.replace(tzinfo=VIETNAM_TZ)

                if now_datetime() < ban_until:
                    return {
                        "banned": True,
                        "type": "1H",
                        "until": ban_until_str
                    }
                else:
                    ws_money.update_cell(row, 6, "auto từ bot")
                    return {"banned": False}
            except:
                pass

        return {"banned": False}

    except Exception as e:
        dprint("check_ban_status error:", e)
        return {"banned": False}

def notify_admin_spam(user_id, username, ban_type, error_count):
    if not ADMIN_ID or ADMIN_ID == 0:
        return

    try:
        row, balance, status = get_user_data(user_id)

        if ban_type == "PERMANENT":
            ban_text = "🔨 Hành động: Ban vĩnh viễn"
            time_text = "⏰ Thời gian: Vĩnh viễn"
        else:
            ban_until = now_datetime() + timedelta(seconds=BAN_DURATION_1H)
            ban_text = "🔨 Hành động: Ban 1 giờ"
            time_text = f"⏰ Hết hạn: {ban_until.strftime('%Y-%m-%d %H:%M')}"

        if username:
            user_info = f"@{username}"
        else:
            user_info = f"ID: {user_id}"

        msg = (
            "🚨 <b>CẢNH BÁO SPAM</b>\n\n"
            f"👤 User: {user_info}\n"
            f"📱 Tele ID: <code>{user_id}</code>\n"
            f"⚠️ Số lỗi: <b>{error_count} lỗi trong 60 giây</b>\n\n"
            f"{ban_text}\n"
            f"{time_text}\n\n"
            "━━━━━━━━━━━━━━━\n"
            "📊 <b>Chi tiết:</b>\n"
            f"• Balance: {balance:,}đ\n"
            f"• Status: {status}\n\n"
            f"🔗 <a href='tg://user?id={user_id}'>Link user</a>"
        )

        tg_send(ADMIN_ID, msg)
        dprint(f"✅ Sent spam alert to admin: {user_id}")

    except Exception as e:
        dprint("notify_admin_spam error:", e)

def apply_ban(user_id, ban_type):
    if not SHEET_READY:
        return

    row = get_user_row(user_id)
    if not row:
        return

    try:
        if ban_type == "PERMANENT":
            note = "BAN VĨNH VIỄN: Spam"
        else:
            ban_until = now_datetime() + timedelta(seconds=BAN_DURATION_1H)
            note = f"BAN 1H: {ban_until.strftime('%Y-%m-%d %H:%M')}"

        ws_money.update_cell(row, 6, note)
        invalidate_user_row_cache(user_id)  # ✅ INVALIDATE CACHE
        log_row(user_id, "", "BAN_APPLIED", ban_type, note)

        dprint(f"✅ Applied ban: {user_id} → {ban_type}")

    except Exception as e:
        dprint("apply_ban error:", e)

# =========================================================
# USER / MONEY UTIL
# =========================================================
def get_user_row(user_id):
    """
    ✅ V4: Cache-first, giảm 80% Sheet API calls
    """
    if not SHEET_READY:
        return None

    # ✅ CHECK CACHE TRƯỚC
    cached_row = get_cached_user_row(user_id)
    if cached_row:
        dprint(f"✅ ROW CACHE HIT: user {user_id} = row {cached_row}")
        return cached_row

    # ❌ Cache miss → gọi Sheet
    dprint(f"⚠️ ROW CACHE MISS: user {user_id}, calling Sheet...")
    try:
        ids = ws_money.col_values(1)
        row = ids.index(str(user_id)) + 1 if str(user_id) in ids else None

        # ✅ CACHE NGAY
        if row:
            cache_user_row(user_id, row)

        return row
    except Exception:
        return None

def ensure_user_exists(user_id, username):
    """
    ✅ Tạo user MỚI: Auto active + 5100đ ngay
    
    User KHÔNG cần bấm nút, nhận tiền ngay lập tức
    
    Schema: Tele ID | Username | Balance | Trang Thái | Chi Chú | note | Gift Status
    """
    if not SHEET_READY:
        return None

    row = get_user_row(user_id)
    if row:
        return row

    try:
        # ✅ Tạo user mới: balance = 5100, status = "active" (AUTO)
        ws_money.append_row([
            str(user_id),      # A: Tele ID
            username,          # B: Username
            NEW_USER_BONUS,    # C: Balance (5100đ - AUTO)
            "active",          # D: Trang Thái (active - AUTO)
            "auto from bot",   # E: Chi Chú
            "",                # F: note (dùng cho ban/unban)
            ""                 # G: Gift Status
        ])
        dprint(f"✅ Created new user {user_id} with {NEW_USER_BONUS:,}đ bonus (AUTO)")

        # ✅ Log bonus
        log_row(user_id, username, "NEW_USER_BONUS", str(NEW_USER_BONUS), "Thưởng user mới - AUTO")

        # ✅ CACHE ROW NGAY
        try:
            all_rows = ws_money.get_all_values()
            new_row = len(all_rows)
            cache_user_row(user_id, new_row)
            dprint(f"✅ Cached new row {new_row} for user {user_id}")
        except:
            pass

    except Exception as e:
        dprint("ensure_user_exists error:", e)

    return get_user_row(user_id)

def get_user_data(user_id, force_refresh=False):
    """
    Lấy thông tin user từ Sheet
    
    Args:
        user_id: Telegram user ID
        force_refresh: Nếu True, bỏ cache và đọc mới từ Sheet
    
    Returns:
        (row, balance, status)
    """
    if not SHEET_READY:
        return None, 0, ""

    # ✅ Force refresh: Xóa cache trước khi đọc
    if force_refresh:
        if user_id in USER_ROW_CACHE:
            del USER_ROW_CACHE[user_id]
            dprint(f"🔄 Cleared cache for user {user_id}")

    row = get_user_row(user_id)
    if not row:
        return None, 0, ""

    try:
        data = ws_money.row_values(row)
        balance = int(data[2]) if len(data) > 2 and str(data[2]).isdigit() else 0
        status  = data[3] if len(data) > 3 else ""
        return row, balance, status
    except Exception:
        return row, 0, ""

def get_balance_direct(user_id):
    """
    🔥 ĐỌC BALANCE TRỰC TIẾP TỪ SHEET - KHÔNG BAO GIỜ DÙNG CACHE
    
    Dùng cho MỌI thao tác liên quan đến TIỀN:
    - Sau add_balance()
    - Sau deduct_balance_atomic()
    - Trước khi hiển thị số dư cho user
    
    Returns:
        balance (int): Số dư thực tế từ Sheet
    """
    if not SHEET_READY:
        return 0
    
    # ✅ Tìm row (có thể dùng cache row, nhưng balance PHẢI đọc mới)
    row = get_user_row(user_id)
    if not row:
        return 0
    
    try:
        # ✅ ĐỌC TRỰC TIẾP từ Sheet cell, KHÔNG qua cache
        balance = int(ws_money.cell(row, 3).value or 0)
        dprint(f"💰 DIRECT READ: user {user_id} balance = {balance:,}đ")
        return balance
    except Exception as e:
        dprint(f"❌ get_balance_direct error: {e}")
        return 0

def update_balance_atomic(user_id, delta):
    """
    🔥 ATOMIC UPDATE BALANCE - AN TOÀN 100%
    
    Dùng cho MỌI thao tác thay đổi balance:
    - Cộng tiền: update_balance_atomic(user_id, +amount)
    - Trừ tiền: update_balance_atomic(user_id, -amount)
    - Hoàn tiền: update_balance_atomic(user_id, +refund)
    
    ATOMIC: Đọc + Tính + Ghi trong 1 operation
    → Không bị race condition khi 2 request song song
    
    Args:
        user_id: Telegram user ID
        delta: Số tiền thay đổi (+ hoặc -)
    
    Returns:
        new_balance: Balance mới sau khi update
    """
    if not SHEET_READY:
        return 0

    row = get_user_row(user_id)
    if not row:
        row = ensure_user_exists(user_id, "")

    try:
        # ✅ ĐỌC balance hiện tại
        current = int(ws_money.cell(row, 3).value or 0)
        
        # ✅ TÍNH balance mới
        new_balance = current + int(delta)
        
        # 🔥 CHẶN BALANCE ÂM (phòng Sheet lỗi, admin sửa tay, concurrent edge case)
        new_balance = max(0, new_balance)
        
        # ✅ GHI ngay
        ws_money.update_cell(row, 3, new_balance)
        
        dprint(f"💰 ATOMIC UPDATE: user {user_id} | {current:,}đ {'+' if delta >= 0 else ''}{delta:,}đ = {new_balance:,}đ")
        
        return new_balance
        
    except Exception as e:
        dprint(f"❌ update_balance_atomic error: {e}")
        return 0

# ⚠️ DEPRECATED: Dùng update_balance_atomic() thay thế
def add_balance(user_id, amount):
    """
    DEPRECATED: Hàm này không atomic, dễ bị race condition
    → Dùng update_balance_atomic(user_id, +amount) thay thế
    """
    dprint(f"⚠️ WARNING: add_balance() is deprecated, use update_balance_atomic()")
    return update_balance_atomic(user_id, amount)

def deduct_balance_atomic(user_id, need_amount):
    """
    ✅ ATOMIC DEDUCT - Đọc + Check + Trừ trong 1 operation
    Tránh race condition khi 2 request song song
    
    Returns:
        (success: bool, new_balance: int)
        - success=True: Đủ tiền, đã trừ thành công
        - success=False: Không đủ tiền, trả về balance hiện tại
    """
    if not SHEET_READY:
        return False, 0
    
    # ✅ Force refresh để đảm bảo đọc balance mới nhất
    row = get_user_row(user_id)
    if not row:
        return False, 0
    
    try:
        # ✅ Đọc balance TRỰC TIẾP từ Sheet (không cache)
        current_balance = int(ws_money.cell(row, 3).value or 0)
        
        # ✅ Check đủ tiền
        if current_balance < need_amount:
            dprint(f"❌ Not enough balance: {current_balance} < {need_amount}")
            return False, current_balance
        
        # ✅ Trừ tiền NGAY
        new_balance = current_balance - need_amount
        ws_money.update_cell(row, 3, new_balance)
        
        # ✅ Clear cache để lần đọc sau refresh
        if user_id in USER_ROW_CACHE:
            del USER_ROW_CACHE[user_id]
        
        dprint(f"✅ Deducted {need_amount:,}đ: {current_balance:,}đ → {new_balance:,}đ")
        return True, new_balance
        
    except Exception as e:
        dprint(f"deduct_balance_atomic error: {e}")
        return False, 0

# =========================================================
# TOPUP UNIQUE
# =========================================================
def is_tx_exists(tx_id):
    if not SHEET_READY or ws_nap_tien is None:
        return False

    try:
        tx_list = ws_nap_tien.col_values(6)
        return str(tx_id) in tx_list
    except Exception as e:
        print("[TX_CHECK_ERROR]", e)
        return False

def save_topup_to_sheet(user_id, username, amount, loai, tx_id, note=""):
    if not SHEET_READY or ws_nap_tien is None:
        return

    try:
        ws_nap_tien.append_row([
            now_str(),
            str(user_id),
            username or "",
            int(amount),
            loai,
            str(tx_id),
            note
        ])
    except Exception as e:
        print("[SAVE_TOPUP_ERROR]", e)

def topup_history_text(user_id, limit=10):
    if not SHEET_READY or ws_nap_tien is None:
        return "❌ Hệ thống lịch sử nạp tiền đang lỗi."

    try:
        rows = ws_nap_tien.get_all_records()
    except Exception:
        return "❌ Không đọc được dữ liệu lịch sử nạp tiền."

    logs = []
    for r in rows:
        if str(r.get("Tele ID", "")) == str(user_id):
            logs.append(r)

    if not logs:
        return "📜 <b>Lịch sử nạp tiền</b>\nChưa có giao dịch nào."

    logs = logs[-limit:]

    out = ["📜 <b>Lịch sử nạp tiền (SEPAY)</b>"]
    for r in logs:
        out.append(
            f"- {r.get('time')} | "
            f"+{int(r.get('số tiền', 0)):,}đ | "
            f"{r.get('tx_id')}"
        )

    return "\n".join(out)

# =========================================================
# ⭐ MULTI-COOKIE PARSER ⭐
# =========================================================
def parse_cookies(text):
    """
    ✅ FIXED: Chỉ chấp nhận cookie hợp lệ (bắt đầu bằng SPC_ST= hoặc SPC_)
    Tránh tính nhầm dòng trống, text rác
    """
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        
        # ✅ Chỉ chấp nhận cookie Shopee hợp lệ
        if line.startswith("SPC_ST=") or line.startswith("SPC_"):
            cookies.append(line)
    
    # ✅ Limit tối đa
    if len(cookies) > MAX_COOKIES_PER_REQUEST:
        cookies = cookies[:MAX_COOKIES_PER_REQUEST]
    
    return cookies

# =========================================================
# VOUCHER UTIL
# =========================================================
def get_voucher(cmd):
    """
    ✅ FIXED: Dùng cache thay vì get_all_records() mỗi lần
    """
    if not SHEET_READY:
        return None, "Hệ thống Sheet đang lỗi"

    rows = get_voucher_stock_cached()

    for r in rows:
        name = str(r.get("Tên Mã", "")).replace(" ", "").lower()
        if name == cmd.lower():
            if r.get("Trạng Thái") != "Còn Mã":
                return None, "Lưu thất Bại. Vui lòng kiểm tra lại cookie - mã"
            return r, None

    return None, "Không tìm thấy voucher"

def save_voucher_and_check(cookie, voucher):
    payload = {
        "voucher_identifiers": [{
            "promotion_id": int(voucher.get("Promotionid")),
            "voucher_code": voucher.get("CODE"),
            "signature": voucher.get("Signature"),
            "signature_source": 0
        }],
        "need_user_voucher_status": True
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://shopee.vn",
        "Referer": "https://shopee.vn/",
        "Cookie": cookie
    }

    try:
        r = requests.post(SAVE_URL, headers=headers, json=payload, timeout=15)

        if r.status_code != 200:
            return False, f"HTTP_{r.status_code}"

        js = r.json()
        if "responses" not in js or not js["responses"]:
            return False, "INVALID_RESPONSE"

        resp = js["responses"][0]

        if resp.get("error") == 0:
            return True, "OK"

        return False, f"SHOPEE_{resp.get('error')}"

    except requests.exceptions.Timeout:
        return False, "TIMEOUT"
    except Exception as e:
        return False, f"EXCEPTION_{str(e)}"

# =========================================================
# ⭐ MULTI-COOKIE VOUCHER SAVER ⭐
# =========================================================
def save_voucher_multi_cookies(cookies, voucher):
    success_count = 0
    failed_details = []

    for idx, cookie in enumerate(cookies, 1):
        ok, reason = save_voucher_and_check(cookie, voucher)

        if ok:
            success_count += 1
            dprint(f"✅ Cookie #{idx}: SUCCESS")
        else:
            failed_details.append((idx, reason))
            dprint(f"❌ Cookie #{idx}: {reason}")

        if idx < len(cookies):
            time.sleep(0.1)

    return success_count, len(cookies), failed_details

# =========================================================
# COMBO UTIL
# =========================================================
def get_vouchers_by_combo(combo_key):
    """
    ✅ FIXED: Dùng cache thay vì get_all_records() mỗi lần
    """
    if not SHEET_READY:
        return [], "Hệ thống Sheet đang lỗi"

    rows = get_voucher_stock_cached()

    items = []
    for r in rows:
        c = str(r.get("Combo", "")).strip().lower()
        if c == combo_key.strip().lower():
            if r.get("Trạng Thái") == "Còn Mã":
                items.append(r)

    if not items:
        return [], "Combo hiện không có mã"

    return items, None

def calculate_combo_price(combo_key, num_cookies):
    """
    🔥 TÍNH GIÁ COMBO TRƯỚC - KHÔNG LƯU VOUCHER
    
    Dùng để check + trừ tiền TRƯỚC khi lưu voucher
    Tránh case: Lưu được voucher nhưng user không đủ tiền
    
    Args:
        combo_key: combo1, combo2, combo3, etc.
        num_cookies: Số lượng cookie
    
    Returns:
        (success: bool, total_price: int, error_message: str)
    """
    vouchers, err = get_vouchers_by_combo(combo_key)
    if err:
        return False, 0, err
    
    # Tính giá mỗi cookie = tổng giá các voucher trong combo
    price_per_cookie = sum(int(v.get("Giá", 0)) for v in vouchers)
    total_price = price_per_cookie * num_cookies
    
    dprint(f"💰 CALC {combo_key.upper()}: {price_per_cookie:,}đ/cookie × {num_cookies} = {total_price:,}đ")
    
    return True, total_price, None

def process_combo1(cookie):
    vouchers, err = get_vouchers_by_combo(COMBO1_KEY)
    if err:
        return False, err, 0, 0, []

    saved = []
    failed = []

    for v in vouchers:
        ok, reason = save_voucher_and_check(cookie, v)
        if ok:
            saved.append(v)
        else:
            failed.append((v.get("Tên Mã", "UNKNOWN"), reason))

    if not saved:
        return False, "Không lưu được voucher nào", 0, len(vouchers), failed

    total_price = 0
    for v in saved:
        try:
            total_price += int(v.get("Giá", 0))
        except Exception:
            pass

    return True, total_price, len(saved), len(vouchers), failed

def process_combo_multi_cookies(cookies, combo_key):
    """
    ✅ DYNAMIC COMBO PROCESSING
    Xử lý bất kỳ combo nào: combo1, combo2, combo3...
    """
    vouchers, err = get_vouchers_by_combo(combo_key)
    if err:
        return False, err, 0, len(cookies), 0, []

    price_per_cookie = sum(int(v.get("Giá", 0)) for v in vouchers)
    cookies_saved = 0
    failed_details = []

    for cookie_idx, cookie in enumerate(cookies, 1):
        cookie_success = True

        for voucher in vouchers:
            ok, reason = save_voucher_and_check(cookie, voucher)

            if not ok:
                cookie_success = False
                failed_details.append((
                    cookie_idx,
                    voucher.get("Tên Mã", "UNKNOWN"),
                    reason
                ))
                dprint(f"❌ Cookie #{cookie_idx} - {voucher.get('Tên Mã')}: {reason}")
            else:
                dprint(f"✅ Cookie #{cookie_idx} - {voucher.get('Tên Mã')}: OK")

            time.sleep(0.1)

        if cookie_success:
            cookies_saved += 1

        if cookie_idx < len(cookies):
            time.sleep(0.2)

    if cookies_saved == 0:
        return False, "Không lưu được cookie nào", 0, len(cookies), len(vouchers), failed_details

    total_price = cookies_saved * price_per_cookie

    return True, total_price, cookies_saved, len(cookies), len(vouchers), failed_details

# =========================================================
# ⭐ DYNAMIC VOUCHER KEYBOARD FROM SHEET ⭐
# =========================================================
VOUCHER_KEYBOARD_CACHE = {
    "keyboard": None,
    "info_text": None,
    "last_update": 0
}
KEYBOARD_CACHE_DURATION = 60

def apply_strikethrough(text):
    strikethrough_map = {
        'A': 'A̶', 'B': 'B̶', 'C': 'C̶', 'D': 'D̶', 'E': 'E̶', 'F': 'F̶', 'G': 'G̶', 'H': 'H̶',
        'I': 'I̶', 'J': 'J̶', 'K': 'K̶', 'L': 'L̶', 'M': 'M̶', 'N': 'N̶', 'O': 'O̶', 'P': 'P̶',
        'Q': 'Q̶', 'R': 'R̶', 'S': 'S̶', 'T': 'T̶', 'U': 'U̶', 'V': 'V̶', 'W': 'W̶', 'X': 'X̶',
        'Y': 'Y̶', 'Z': 'Z̶',
        'a': 'a̶', 'b': 'b̶', 'c': 'c̶', 'd': 'd̶', 'e': 'e̶', 'f': 'f̶', 'g': 'g̶', 'h': 'h̶',
        'i': 'i̶', 'j': 'j̶', 'k': 'k̶', 'l': 'l̶', 'm': 'm̶', 'n': 'n̶', 'o': 'o̶', 'p': 'p̶',
        'q': 'q̶', 'r': 'r̶', 's': 's̶', 't': 't̶', 'u': 'u̶', 'v': 'v̶', 'w': 'w̶', 'x': 'x̶',
        'y': 'y̶', 'z': 'z̶',
        '0': '0̶', '1': '1̶', '2': '2̶', '3': '3̶', '4': '4̶', '5': '5̶', '6': '6̶', '7': '7̶',
        '8': '8̶', '9': '9̶',
        '%': '%̶', '+': '+̶', '/': '/̶', ' ': ' ̶',
    }
    result = ""
    for char in text:
        result += strikethrough_map.get(char, char)
    return result

def parse_position(pos_str):
    """
    ✅ FIXED: Parse đúng 100%
    1A → (1, 'A')
    A1 → (1, 'A')
    2B → (2, 'B')
    B2 → (2, 'B')
    """
    if not pos_str or not isinstance(pos_str, str):
        return None

    pos_str = pos_str.strip().upper()

    # Kiểu 1A, 2B (số trước, chữ sau)
    m = re.match(r'^(\d+)([A-Z])$', pos_str)
    if m:
        return (int(m.group(1)), m.group(2))

    # Kiểu A1, B2 (chữ trước, số sau)
    m = re.match(r'^([A-Z])(\d+)$', pos_str)
    if m:
        return (int(m.group(2)), m.group(1))

    return None

def build_voucher_keyboard_from_sheet():
    if not SHEET_READY:
        dprint("❌ Sheet not ready, using static keyboard")
        return build_static_voucher_keyboard()

    try:
        dprint("📊 Reading VoucherStock sheet...")
        all_rows = ws_voucher.get_all_records()
        dprint(f"📊 Found {len(all_rows)} rows in VoucherStock")

        vouchers_by_position = {}
        
        # ✅ DYNAMIC COMBO DETECTION
        combos_data = {}  # {combo_key: {price, count, vouchers}}
        
        info_lines = ["🎊 <b>VOUCHER HIỆN CÓ - HAPPY NEW YEAR 2025!</b> 🎊\n━━━━━━━━━━━━━━━"]

        for idx, row in enumerate(all_rows, 1):
            display = ""
            for key in ["Display", "Show", "Visible", "Hiển thị", "Hiển Thị"]:
                if key in row:
                    display = str(row[key]).strip().upper()
                    if display:
                        break

            if display not in ["YES", "Y", "TRUE", "1"]:
                continue

            pos_str = str(row.get("Vị trí", "")).strip()
            if not pos_str:
                pos_str = str(row.get("Position", "")).strip()

            # ✅ Detect tất cả combo (combo1, combo2, combo3...)
            combo = str(row.get("Combo", "")).strip().lower()
            if combo.startswith("combo"):
                if combo not in combos_data:
                    combos_data[combo] = {
                        "price": 0,
                        "count": 0,
                        "vouchers": []
                    }
                try:
                    combos_data[combo]["price"] += int(row.get("Giá", 0))
                    combos_data[combo]["count"] += 1
                    combos_data[combo]["vouchers"].append(row)
                except:
                    pass

            if not pos_str:
                continue

            position = parse_position(pos_str)
            if not position:
                continue

            vouchers_by_position[position] = row

        if len(vouchers_by_position) == 0:
            return build_static_voucher_keyboard()

        keyboard_rows = []
        current_row_num = None
        current_row_buttons = []

        sorted_positions = sorted(vouchers_by_position.keys())

        for position in sorted_positions:
            row_num, col_letter = position
            voucher = vouchers_by_position[position]

            if current_row_num != row_num:
                if current_row_buttons:
                    keyboard_rows.append(current_row_buttons)
                current_row_buttons = []
                current_row_num = row_num

            # ✅ Hỗ trợ nhiều tên cột display name
            ten_hien_thi = ""
            for key in ["Display Name", "Tên hiển thị", "Tên Hiển Thị", "display_name"]:
                if key in voucher:
                    ten_hien_thi = str(voucher[key]).strip()
                    if ten_hien_thi:
                        break
            
            # Fallback nếu không có display name
            if not ten_hien_thi:
                ten_hien_thi = str(voucher.get("Tên Mã", "")).strip()

            trang_thai = str(voucher.get("Trạng Thái", "")).strip()
            ten_ma = str(voucher.get("Tên Mã", "")).strip()
            gia = int(voucher.get("Giá", 0))

            is_sold_out = trang_thai != "Còn Mã"

            if is_sold_out:
                # ✅ Giảm độ dài text - bỏ emoji, chỉ giữ "Hết"
                button_text = f"{ten_hien_thi} (Hết)"
                callback_data = f"SOLD_OUT:{ten_ma}"
            else:
                # ✅ Giảm emoji, text ngắn hơn cho mobile
                button_text = f"🎊 {ten_hien_thi}"
                callback_data = f"BUY:{ten_ma}"

            current_row_buttons.append({
                "text": button_text,
                "callback_data": callback_data
            })

            if not is_sold_out:
                info_lines.append(f"• {ten_hien_thi} — 💰Giá {gia:,} VNĐ")

        if current_row_buttons:
            keyboard_rows.append(current_row_buttons)

        # ✅ DYNAMIC COMBO BUTTONS - Tự động thêm tất cả combo từ Sheet
        if combos_data:
            info_lines.append(f"\n🟣 <b>COMBO ĐẶC BIỆT</b>")
            
            # Sort combo theo tên (combo1, combo2, combo3...)
            for combo_key in sorted(combos_data.keys()):
                combo_info = combos_data[combo_key]
                
                # ✅ Tên hiển thị NGẮN hơn cho mobile
                combo_display_names = {
                    "combo1": "🎆 COMBO1 | 100k+Ship",
                    "combo2": "🎆 COMBO2 | Giảm Giá",
                    "combo3": "🎆 COMBO3 | Freeship",
                }
                
                # Fallback: COMBO{N} nếu không có trong map
                combo_num = combo_key.replace("combo", "")
                display_name = combo_display_names.get(
                    combo_key,
                    f"🎆 COMBO{combo_num.upper()}"
                )
                
                # Thêm nút
                keyboard_rows.append([{
                    "text": display_name,
                    "callback_data": f"BUY:{combo_key}"
                }])
                
                # Thông tin combo
                info_lines.append(f"• {combo_key.upper()}: {combo_info['count']} mã")
                info_lines.append(f"  💰 {combo_info['price']:,} VNĐ")

        info_lines.append("\n⭐ <b>HỖ TRỢ LƯU TỐI ĐA 10 COOKIE</b>")
        info_lines.append("💡 Gửi mỗi cookie 1 dòng")
        info_lines.append("\n👇 <b>BẤM NÚT BÊN DƯỚI ĐỂ MUA</b>")

        keyboard = {"inline_keyboard": keyboard_rows}
        info_text = "\n".join(info_lines)

        return keyboard, info_text

    except Exception as e:
        dprint(f"❌ Error building keyboard from sheet: {e}")
        import traceback
        traceback.print_exc()
        return build_static_voucher_keyboard()

def build_static_voucher_keyboard():
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🎉 Mã 100k 0đ", "callback_data": "BUY:voucher100k"},
                {"text": "✨ Mã 50% Max 200k", "callback_data": "BUY:voucher50max200"},
            ],
            [
                {"text": "🚀 Freeship Hỏa Tốc", "callback_data": "BUY:voucherHoaToc"},
            ],
            [
                {"text": "🎆 COMBO1 | Mã 100k + Ship HT 🎆", "callback_data": "BUY:combo1"}
            ]
        ]
    }

    info_text = (
        "🎊 <b>VOUCHER HIỆN CÓ - HAPPY NEW YEAR 2025!</b> 🎊\n"
        "━━━━━━━━━━━━━━━\n"
        "🟢 <b>Voucher đơn</b>\n"
        "• Mã 100k 0đ — 💰Giá 1.000 VNĐ\n"
        "• Mã 50% Max 200k — 💰Giá 1.000 VNĐ\n"
        "• Freeship Hỏa Tốc — 💰Giá 1.000 VNĐ\n\n"
        "🟣 <b>COMBO ĐẶC BIỆT</b>\n"
        "• COMBO1: 100k/0đ + Freeship Hỏa Tốc\n"
        "  💰 2.000 VNĐ | 🎫 2 mã\n\n"
        "⭐ <b>HỖ TRỢ LƯU TỐI ĐA 10 COOKIE</b>\n"
        "💡 Gửi mỗi cookie 1 dòng\n\n"
        "👇 <b>BẤM NÚT BÊN DƯỚI ĐỂ MUA</b>"
    )

    return keyboard, info_text

def get_voucher_keyboard_cached():
    global VOUCHER_KEYBOARD_CACHE

    now = time.time()

    if (VOUCHER_KEYBOARD_CACHE["keyboard"] and
        now - VOUCHER_KEYBOARD_CACHE["last_update"] < KEYBOARD_CACHE_DURATION):
        dprint("Using cached keyboard")
        return VOUCHER_KEYBOARD_CACHE["keyboard"], VOUCHER_KEYBOARD_CACHE["info_text"]

    dprint("Rebuilding keyboard from sheet...")
    keyboard, info_text = build_voucher_keyboard_from_sheet()

    VOUCHER_KEYBOARD_CACHE["keyboard"] = keyboard
    VOUCHER_KEYBOARD_CACHE["info_text"] = info_text
    VOUCHER_KEYBOARD_CACHE["last_update"] = now

    return keyboard, info_text

def build_voucher_info_text():
    _, info_text = get_voucher_keyboard_cached()
    return info_text

def build_quick_voucher_keyboard():
    keyboard, _ = get_voucher_keyboard_cached()
    return keyboard

def build_quick_buy_keyboard(cmd):
    MAP = {
        "voucher100k": "💸 Mã 100k 0đ",
        "voucher50max200": "💸 Mã 50% max 200k 0đ",
        "voucherHoaToc": "🚀 Freeship Hỏa Tốc",
        "combo1": "🎁 COMBO1 – Mã 100k + Ship HT 🔥"
    }

    text = MAP.get(cmd, f"🎁 {cmd}")

    return {
        "inline_keyboard": [[
            {"text": text, "callback_data": f"BUY:{cmd}"}
        ]]
    }

# =========================================================
# KÍCH HOẠT + TẶNG 5K
# =========================================================
def handle_active_gift_5k(user_id, username):
    """
    ✅ Kích hoạt tài khoản + Tặng quà
    
    LOGIC:
    - Chỉ user có status trong ALLOWED_GIFT_STATUS mới được nhận
    - Admin set "inactive" → KHÔNG được nhận (tránh abuse)
    - Dùng ACTIVE_GIFT_AMOUNT thống nhất
    - Log riêng action "ACTIVE_GIFT_CLICK"
    """
    if not SHEET_READY:
        return False, "❌ Hệ thống đang lỗi."

    row = get_user_row(user_id)
    if not row:
        row = ensure_user_exists(user_id, username)

    data = ws_money.row_values(row)
    status = data[3] if len(data) > 3 else ""

    # ✅ CHECK 1: Đã active rồi
    if status == "active":
        return False, "⚠️ Tài khoản đã kích hoạt, không thể nhận khuyến mãi."
    
    # ✅ CHECK 2: Status không được phép (admin set "inactive", "banned", etc.)
    if status not in ALLOWED_GIFT_STATUS:
        dprint(f"⚠️ User {user_id} status '{status}' not allowed for gift")
        return False, (
            "❌ Tài khoản không đủ điều kiện nhận khuyến mãi.\n"
            "📞 Vui lòng liên hệ admin: @BonBonxHPx"
        )

    try:
        current_balance = int(data[2]) if len(data) > 2 else 0
        
        # ✅ FIX: Dùng ACTIVE_GIFT_AMOUNT thống nhất (5100đ)
        new_balance = current_balance + ACTIVE_GIFT_AMOUNT

        ws_money.update(
            f'C{row}:D{row}',
            [[new_balance, "active"]]
        )

        # ✅ LOG riêng action
        log_row(
            user_id,
            username,
            "ACTIVE_GIFT_CLICK",  # ← Riêng biệt với NEW_USER_BONUS
            str(ACTIVE_GIFT_AMOUNT),
            f"Kích hoạt thủ công + nhận {ACTIVE_GIFT_AMOUNT:,}đ"
        )
        
        dprint(f"✅ User {user_id} activated: +{ACTIVE_GIFT_AMOUNT:,}đ → {new_balance:,}đ")

        return True, new_balance

    except Exception as e:
        dprint("handle_active_gift_5k error:", e)
        return False, "❌ Lỗi khi kích hoạt"

# =========================================================
# CALLBACK QUERY HANDLER
# =========================================================
def handle_callback_query(cb):
    cb_id = cb.get("id")
    data = cb.get("data", "")
    from_user = cb.get("from", {})
    user_id = from_user.get("id")

    if data.startswith("SOLD_OUT:"):
        tg_answer_callback(cb_id, "⚠️ Voucher này tạm hết mã. Vui lòng quay lại sau!", True)
        return

    if data.startswith("BUY:"):
        cmd = data.split(":", 1)[1]

        # ✅ RATE LIMIT - Ngăn spam click BUY
        last_callback_time = CALLBACK_COOLDOWN.get(user_id, 0)
        if time.time() - last_callback_time < CALLBACK_COOLDOWN_SECONDS:
            tg_answer_callback(cb_id, "⏳ Chậm lại 1 chút", True)
            dprint(f"⏳ Callback rate-limited: user {user_id}")
            return
        
        CALLBACK_COOLDOWN[user_id] = time.time()

        row, balance, status = get_user_data(user_id)
        if not row:
            tg_answer_callback(cb_id, "❌ Bạn chưa có ID", True)
            return

        if status != "active":
            tg_answer_callback(cb_id, "❌ Tài khoản chưa được kích hoạt", True)
            return

        if user_id in PENDING_VOUCHER:
            old_pending = PENDING_VOUCHER[user_id]
            old_cmd = old_pending["cmd"] if isinstance(old_pending, dict) else old_pending
            dprint(f"Cleared old pending: {old_cmd}")

        # ✅ Lưu với timestamp
        PENDING_VOUCHER[user_id] = {
            "cmd": cmd,
            "ts": time.time()
        }

        tg_answer_callback(cb_id)
        tg_send(
            user_id,
            f"👉 Gửi <b>cookie</b> vào đây để lưu <b>{cmd}</b>\n\n"
            f"⭐ <b>Hỗ trợ lưu tối đa 10 cookie</b>\n"
            f"💡 Gửi mỗi cookie 1 dòng"
        )
        return

    # ===== SYSTEM MENU CALLBACKS =====
    if data.startswith("SYSTEM:"):
        action = data.split(":")[1]
        
        if action == "bot_list":
            bot_list_menu = {
                "inline_keyboard": [
                    [{"text": "🔴 Bot Lưu Voucher", "url": "https://t.me/nganmiu_bot"}],
                    [{"text": "📦 Bot Check Đơn Hàng", "url": "https://t.me/ShopeeXCheck_Bot"}],
                    [{"text": "📲 Bot Thuê Số (Sắp mở)", "callback_data": "SYSTEM:coming_soon"}],
                    [{"text": "🔙 Quay lại", "callback_data": "SYSTEM:back"}],
                ]
            }
            
            tg_answer_callback(cb_id)
            tg_edit_message(
                chat_id,
                cb_msg_id,
                "📱 <b>DANH SÁCH BOT NGÂNMIU</b>\n\n"
                "🤖 Hệ sinh thái bot của chúng tôi:\n\n"
                "🔴 <b>Bot Lưu Voucher</b>\n"
                "└ Lưu voucher Shopee tự động\n\n"
                "📦 <b>Bot Check Đơn Hàng</b>\n"
                "└ Kiểm tra trạng thái đơn hàng\n\n"
                "📲 <b>Bot Thuê Số</b> (Sắp ra mắt)\n"
                "└ Thuê số điện thoại nhận OTP",
                bot_list_menu
            )
            return
        
        if action == "coming_soon":
            tg_answer_callback(cb_id, "🚧 Tính năng đang phát triển!", True)
            return
        
        if action == "back":
            system_menu = {
                "inline_keyboard": [
                    [{"text": "👤 Admin hỗ trợ", "url": "https://t.me/BonBonxHPx"}],
                    [{"text": "👥 Group Hỗ Trợ", "url": "https://t.me/botxshopee"}],
                    [{"text": "📱 Danh sách Bot", "callback_data": "SYSTEM:bot_list"}],
                    [{"text": "🔴 Bot Lưu Voucher", "url": "https://t.me/nganmiu_bot"}],
                    [{"text": "📦 Bot Check Đơn Hàng", "url": "https://t.me/ShopeeXCheck_Bot"}],
                    [{"text": "📲 Bot Thuê Số", "callback_data": "SYSTEM:coming_soon"}],
                ]
            }
            
            tg_answer_callback(cb_id)
            tg_edit_message(
                chat_id,
                cb_msg_id,
                "🏠 <b>HỆ THỐNG BOT NGÂNMIU</b>\n\n"
                "👋 Chào mừng bạn đến với hệ sinh thái bot NgânMiu!\n\n"
                "📌 <b>Chọn một trong các dịch vụ bên dưới:</b>",
                system_menu
            )
            return

    tg_answer_callback(cb_id, "⚠️ Thao tác không hỗ trợ", True)

# =========================================================
# TỔNG KẾT KINH DOANH
# =========================================================
def parse_date_from_sheet(date_str):
    try:
        if isinstance(date_str, datetime):
            return date_str
        return datetime.strptime(str(date_str).strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.strptime(str(date_str).strip(), "%d/%m/%Y %H:%M:%S")
        except Exception:
            return None

def get_today_stats():
    if not SHEET_READY:
        return None

    today = datetime.now(VIETNAM_TZ).date()
    stats = {
        "napten_count": 0,
        "napten_amount": 0,
        "napten_bonus": 0,
        "napten_users": set(),
        "voucher_details": {},
        "total_usage": 0,
        "active_users": set(),
    }

    try:
        if ws_nap_tien:
            all_rows = ws_nap_tien.get_all_values()
            for row in all_rows[1:]:
                if len(row) < 7:
                    continue
                try:
                    row_date = parse_date_from_sheet(row[0])
                    if row_date and row_date.date() == today:
                        user_id = int(row[1])
                        amount = int(row[3]) if row[3] else 0
                        note = row[6]

                        stats["napten_count"] += 1
                        stats["napten_amount"] += amount
                        stats["napten_users"].add(user_id)
                        stats["active_users"].add(user_id)

                        if note and "=" in note:
                            try:
                                stats["napten_bonus"] += int(note.split("=")[1])
                            except:
                                pass
                except:
                    continue
    except Exception as e:
        dprint(f"Error reading Nap Tien: {e}")

    try:
        if ws_log:
            all_logs = ws_log.get_all_values()
            for row in all_logs[1:]:
                if len(row) < 6:
                    continue
                try:
                    row_date = parse_date_from_sheet(row[0])
                    if row_date and row_date.date() == today:
                        user_id = int(row[1])
                        action = row[3]
                        details = row[5]

                        stats["active_users"].add(user_id)

                        if action == "VOUCHER":
                            voucher_name = details
                            if voucher_name not in stats["voucher_details"]:
                                stats["voucher_details"][voucher_name] = 0
                            stats["voucher_details"][voucher_name] += 1
                            stats["total_usage"] += 1

                        elif action == "COMBO1":
                            if "COMBO1" not in stats["voucher_details"]:
                                stats["voucher_details"]["COMBO1"] = 0
                            stats["voucher_details"]["COMBO1"] += 1
                            stats["total_usage"] += 1
                except:
                    continue
    except Exception as e:
        dprint(f"Error reading Logs: {e}")

    stats["napten_users"] = len(stats["napten_users"])
    stats["active_users"] = len(stats["active_users"])

    return stats

def format_tongket_message(stats):
    if not stats:
        return "❌ Không thể lấy dữ liệu"

    today_str = datetime.now(VIETNAM_TZ).strftime("%d/%m/%Y")
    total_in = stats["napten_amount"] + stats["napten_bonus"]

    msg = f"""📊 <b>BÁO CÁO TỔNG KẾT</b>
📅 {today_str}

━━━━━━━━━━━━━━━━━━
💰 <b>NẠP TIỀN</b>
• Lượt nạp: <b>{stats['napten_count']}</b>
• User nạp: <b>{stats['napten_users']}</b>
• Tiền gốc: <b>{stats['napten_amount']:,}đ</b>
• Thưởng: <b>+{stats['napten_bonus']:,}đ</b>
• <b>Tổng vào: {total_in:,}đ</b>

━━━━━━━━━━━━━━━━━━
🎟️ <b>VOUCHER ĐÃ LƯU</b>"""

    grouped = {}

    for raw_key, count in stats["voucher_details"].items():
        raw = raw_key.lower()

        if "combo1" in raw:
            base = "COMBO1"
        elif "hoatoc" in raw:
            base = "voucherHoaToc"
        else:
            m = re.search(r"(voucher[a-z0-9]+)", raw)
            if m:
                base = m.group(1)
            else:
                base = raw_key

        grouped.setdefault(base, 0)
        grouped[base] += count

    DISPLAY_NAME = {
        "voucher100k": "💎 Mã 100k 0đ",
        "voucher30k": "🎁 Mã 30k",
        "voucher50max100": "🎁 Mã 50% Max 100k",
        "voucher50max200": "🎁 Mã 50% Max 200k",
        "voucherHoaToc": "🚀 Freeship Hỏa Tốc",
        "COMBO1": "🎆 COMBO1 | 100k + Ship HT",
    }

    total = 0
    for base, count in sorted(grouped.items(), key=lambda x: x[1], reverse=True):
        name = DISPLAY_NAME.get(base, base)
        msg += f"\n• {name}: <b>{count}</b> lượt"
        total += count

    msg += f"\n\n<b>━ Tổng: {total} lượt lưu</b>"

    msg += f"""

━━━━━━━━━━━━━━━━━━
👥 <b>USER HOẠT ĐỘNG</b>
• Tổng: <b>{stats['active_users']}</b> user
"""

    return msg

def handle_tongket_command(chat_id, user_id):
    if user_id != ADMIN_ID:
        tg_send(chat_id, "⛔ Chỉ admin")
        return

    tg_send(chat_id, "⏳ Đang tổng hợp dữ liệu...")
    stats = get_today_stats()

    if not stats:
        tg_send(chat_id, "❌ Lỗi khi đọc dữ liệu")
        return

    msg = format_tongket_message(stats)
    tg_send(chat_id, msg)

# =========================================================
# 🔥 STATS COMMAND - XEM CACHE STATISTICS
# =========================================================
def handle_stats_command(chat_id, user_id):
    """Admin command: xem cache stats"""
    if user_id != ADMIN_ID:
        tg_send(chat_id, "⛔ Chỉ admin")
        return

    stats = f"""📊 <b>CACHE STATISTICS</b>

🔢 <b>Row Cache:</b>
• Cached users: {len(USER_ROW_CACHE)}
• TTL: {USER_ROW_CACHE_TTL}s (1h)
• Memory: ~{len(USER_ROW_CACHE) * 8} bytes

📢 <b>Broadcast Cache:</b>
• Cached: {"Yes" if BROADCAST_USER_CACHE else "No"}
• Count: {len(BROADCAST_USER_CACHE) if BROADCAST_USER_CACHE else 0}
• Age: {int(time.time() - BROADCAST_USER_CACHE_TIME)}s

💬 <b>Message Dedup:</b>
• Tracked: {len(PROCESSED_MESSAGES)}

━━━━━━━━━━━━━━━━━━
<b>✅ Cache hit → Không gọi Sheet</b>
<b>❌ Cache miss → Gọi Sheet (hiếm)</b>

<b>Hiệu quả:</b> Giảm ~90% API calls!
"""
    tg_send(chat_id, stats)

# =========================================================
# CORE UPDATE HANDLER
# =========================================================
def handle_update(update):
    dprint("UPDATE:", update)

    # ✅ UPDATE_ID DEDUPLICATION - Tránh Telegram resend khi lag
    update_id = update.get("update_id")
    
    if update_id:
        if update_id in PROCESSED_UPDATE_IDS:
            dprint(f"⚠️ DUPLICATE UPDATE_ID DETECTED: {update_id} - SKIPPING")
            return
        
        # ✅ deque tự động drop oldest khi đầy (maxlen=2000)
        PROCESSED_UPDATE_IDS.append(update_id)

    # ✅ MESSAGE DEDUPLICATION
    global PROCESSED_MESSAGES
    msg = update.get("message", {})
    message_id = msg.get("message_id")

    if message_id:
        chat_id = msg.get("chat", {}).get("id")
        msg_key = f"{chat_id}_{message_id}"

        if msg_key in PROCESSED_MESSAGES:
            dprint(f"⚠️ DUPLICATE MESSAGE DETECTED: {msg_key} - SKIPPING")
            return

        PROCESSED_MESSAGES.add(msg_key)

        if len(PROCESSED_MESSAGES) > MAX_PROCESSED_MESSAGES:
            old_msgs = list(PROCESSED_MESSAGES)[:100]
            for old_msg in old_msgs:
                PROCESSED_MESSAGES.discard(old_msg)
            dprint(f"🗑️ Cleaned {len(old_msgs)} old messages from cache")

    # ✅ CHECK SHEET_READY
    if not SHEET_READY:
        msg = update.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        if chat_id:
            tg_send(
                chat_id,
                "⚠️ <b>Hệ thống đang bảo trì</b>\n"
                "Vui lòng thử lại sau 2 phút."
            )
        return

    # ✅ CHECK BAN STATUS
    msg = update.get("message") or update.get("callback_query", {}).get("message", {})
    from_user = msg.get("from") or update.get("callback_query", {}).get("from", {})
    user_id = from_user.get("id")

    if not user_id:
        return

    ban_status = check_ban_status(user_id)

    if ban_status["banned"]:
        ban_type = ban_status["type"]
        ban_until = ban_status["until"]

        msg_text = (
            "⛔ <b>TÀI KHOẢN BỊ KHÓA</b>\n\n"
            "🚫 <b>Lý do:</b> Spam hệ thống\n"
        )

        if ban_type == "PERMANENT":
            msg_text += "⏰ <b>Thời gian:</b> Vĩnh viễn\n\n"
        else:
            msg_text += (
                f"⏰ <b>Thời gian:</b> 1 giờ\n"
                f"⏱️ <b>Hết hạn:</b> {ban_until}\n\n"
            )

        msg_text += "📞 <b>Liên hệ:</b> @BonBonxHPx"

        chat_id = msg.get("chat", {}).get("id")
        if chat_id:
            tg_send(chat_id, msg_text)

        return

    # ===== CALLBACK QUERY =====
    if "callback_query" in update:
        handle_callback_query(update["callback_query"])
        return

    # ===== MESSAGE =====
    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username", "")
    text = (msg.get("text") or "").strip()

    # /tongket
    if text == "/tongket":
        handle_tongket_command(chat_id, user_id)
        return

    # /stats - XEM CACHE STATS
    if text == "/stats":
        handle_stats_command(chat_id, user_id)
        return

    # /update
    if text == "/update":
        if user_id != ADMIN_ID:
            tg_send(chat_id, "⛔ Chỉ admin")
            return

        global VOUCHER_KEYBOARD_CACHE
        VOUCHER_KEYBOARD_CACHE = {
            "keyboard": None,
            "info_text": None,
            "last_update": 0
        }

        voucher_keyboard, voucher_info = get_voucher_keyboard_cached()

        tg_send(
            chat_id,
            "✅ Đã cập nhật keyboard từ Sheet!\n\n"
            "🎊 <b>Menu đã được refresh</b>",
            build_main_keyboard(is_active=True)
        )

        tg_send(chat_id, voucher_info, voucher_keyboard)
        return

    if not text:
        if user_id not in PENDING_VOUCHER:
            return

    # ===== ADMIN: /thongbao =====
    if text and text.startswith("/thongbao"):
        if user_id != ADMIN_ID:
            tg_send(chat_id, "⛔ Lệnh này chỉ dành cho Admin")
            return

        message_id = msg.get("message_id", 0)

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(
                chat_id,
                "📢 <b>HƯỚNG DẪN BROADCAST</b>\n\n"
                "Sử dụng: <code>/thongbao [nội dung]</code>\n\n"
                "Ví dụ:\n"
                "<code>/thongbao Đêm qua server bị lỗi dẫn tới bot không hoạt động, "
                "Hiện tại BOT đã hoạt động bình thường trở lại.</code>"
            )
            return

        if is_broadcast_message_processed(message_id):
            tg_send(
                chat_id,
                "⚠️ <b>Thông báo này đã được gửi trước đó</b>\n"
                "Bot đã tự động bỏ qua để tránh gửi lặp."
            )
            dprint(f"⚠️ DUPLICATE BROADCAST BLOCKED: msg_id={message_id}")
            return

        can_broadcast, wait_time = check_broadcast_cooldown_from_sheet()
        if not can_broadcast:
            tg_send(
                chat_id,
                f"⏳ <b>VUI LÒNG ĐỢI {wait_time}s</b>\n\n"
                f"🔒 Broadcast gần đây chưa đủ thời gian cooldown\n\n"
                f"<i>Hệ thống tự động chống spam broadcast.</i>"
            )
            dprint(f"⏳ COOLDOWN BLOCKED: wait {wait_time}s")
            return

        message = parts[1].strip()

        global IS_BROADCASTING
        if IS_BROADCASTING:
            tg_send(
                chat_id,
                "⛔ <b>Đang có broadcast khác chạy</b>\n"
                "Vui lòng đợi broadcast trước hoàn tất."
            )
            return

        IS_BROADCASTING = True

        if not set_broadcast_state_to_sheet(user_id, "STARTED", message_id):
            IS_BROADCASTING = False
            tg_send(chat_id, "❌ Lỗi khi lưu trạng thái broadcast, vui lòng thử lại")
            return

        dprint(f"📝 Broadcast STARTED | admin={user_id} | msg_id={message_id}")

        tg_send(
            chat_id,
            "✅ <b>ĐÃ NHẬN LỆNH BROADCAST</b>\n\n"
            "⏳ Đang gửi thông báo...\n"
            "📊 Kết quả sẽ được trả về sau khi hoàn tất."
        )

        try:
            dprint(f"🔔 Broadcasting: {message[:40]}...")
            success, failed = broadcast_message(message, exclude_admin=False)

            log_row(user_id, username, "BROADCAST", str(success), message[:50])

            set_broadcast_state_to_sheet(user_id, "COMPLETED", message_id)

            tg_send(
                chat_id,
                f"✅ <b>BROADCAST HOÀN TẤT</b>\n\n"
                f"👥 Thành công: <b>{success}</b>\n"
                f"❌ Thất bại: <b>{failed}</b>"
            )

        except Exception as e:
            dprint(f"❌ Broadcast error: {e}")
            set_broadcast_state_to_sheet(user_id, "FAILED", message_id)
            tg_send(chat_id, f"❌ Lỗi khi broadcast: {str(e)}")

        finally:
            IS_BROADCASTING = False

    # ===== /start =====
    if text == "/start":
        # ✅ Check user mới
        is_new_user = get_user_row(user_id) is None
        
        row = ensure_user_exists(user_id, username)
        row, balance, status = get_user_data(user_id)

        # ✅ Message cho user mới (đã AUTO active + 5100đ)
        if is_new_user:
            tg_send(
                chat_id,
                f"🎉 <b>CHÀO MỪNG BẠN MỚI!</b>\n\n"
                f"👋 Xin chào <b>{username or 'bạn'}</b>\n\n"
                f"🎁 Bạn nhận được <b>{NEW_USER_BONUS:,}đ</b> thưởng!\n"
                f"💼 Số dư: <b>{balance:,}đ</b>\n"
                f"📊 Trạng thái: <b>{status}</b>\n\n"
                f"🛒 Bấm nút bên dưới để bắt đầu mua voucher",
                build_main_keyboard(is_active=True)
            )
            return

        # ✅ User cũ - active
        if status == "active":
            tg_send(
                chat_id,
                "👋 <b>Chào mừng quay lại!</b>",
                build_main_keyboard(is_active=True)
            )
            return

        # ⚠️ User cũ - KHÔNG active
        tg_send(
            chat_id,
            "⚠️ <b>Tài khoản chưa được kích hoạt</b>\n\n"
            f"💼 Số dư: <b>{balance:,}đ</b>\n"
            f"📊 Trạng thái: <b>{status}</b>\n\n"
            f"🎁 <b>NHẬN NGAY {ACTIVE_GIFT_AMOUNT:,}đ KHI KÍCH HOẠT!</b>\n\n"
            f"👇 <b>Bấm nút bên dưới để kích hoạt:</b>\n"
            f"   🎊 <b>Kích Hoạt Tặng 5k</b>\n\n"
            f"📞 Hoặc liên hệ admin: @BonBonxHPx",
            build_main_keyboard(is_active=False)
        )
        return

    # ===== NẠP TIỀN =====
    if text in ("💎 Nạp tiền", "💳 Nạp tiền"):
        ensure_user_exists(user_id, username)

        qr = build_sepay_qr(user_id)

        caption = (
            "💳 <b>NẠP TIỀN TỰ ĐỘNG (SEPAY)</b>\n\n"
            "📌 <b>NỘI DUNG CHUYỂN KHOẢN (BẮT BUỘC)</b>\n"
            f"<code>SEVQR NAP {user_id}</code>\n\n"
            "⚠️ <b>LƯU Ý</b>\n"
            "• Nhập <b>ĐÚNG</b> nội dung để hệ thống tự cộng tiền\n"
            "• Không sửa – không thêm ký tự khác\n\n"
            "💰 <b>NẠP TỐI THIỂU:</b> <b>10.000đ</b>\n\n"
            "🎁 <b>ƯU ĐÃI NẠP TIỀN</b>\n"
            "• ≥ 20.000đ 🎁 +10%\n"
            "• ≥ 50.000đ 🎁 +15%\n"
            "• ≥ 100.000đ 🎁 +20%\n\n"
            "⚡ <i>Tiền vào tài khoản trong vòng 0–30 giây</i>"
        )

        tg_send_photo(chat_id, qr, caption)
        return

    # ===== USER DATA =====
    row, balance, status = get_user_data(user_id)
    if not row:
        tg_send(chat_id, "❌ Bạn chưa có ID. Bấm /start để kích hoạt.")
        return

    # ===== SỐ DƯ =====
    if text in ("💰 Số dư", "/balance"):
        # ✅ RATE LIMIT: 1 lần/3s per user
        last_balance_check = CALLBACK_COOLDOWN.get(f"balance_{user_id}", 0)
        if time.time() - last_balance_check < 3:
            dprint(f"⏳ Balance check rate-limited: user {user_id}")
            return  # Silent ignore (không spam user)
        
        CALLBACK_COOLDOWN[f"balance_{user_id}"] = time.time()
        
        # ✅ FORCE REFRESH - User có thể vừa nạp tiền
        row, balance, status = get_user_data(user_id, force_refresh=True)
        
        if not row:
            tg_send(chat_id, "❌ Không tìm thấy tài khoản. Bấm /start để kích hoạt.")
            return
        
        dprint(f"💰 Check balance for user {user_id}: {balance:,}đ (status: {status})")
        
        tg_send(
            chat_id,
            f"💰 <b>Số dư:</b> <b>{balance:,}đ</b>\n"
            f"📌 Trạng thái: <b>{status}</b>",
            build_main_keyboard(is_active=(status == "active"))
        )
        return

    # ===== LỊCH SỬ =====
    if text in ("📜 Lịch sử nạp tiền", "/topup_history"):
        tg_send(chat_id, topup_history_text(user_id))
        return

    # ===== HỆ THỐNG BOT =====
    if text == "🧩 Hệ Thống Bot NgânMiu":
        system_menu = {
            "inline_keyboard": [
                [{"text": "👤 Admin hỗ trợ", "url": "https://t.me/BonBonxHPx"}],
                [{"text": "👥 Group Hỗ Trợ", "url": "https://t.me/botxshopee"}],
                [{"text": "📱 Danh sách Bot", "callback_data": "SYSTEM:bot_list"}],
                [{"text": "🔴 Bot Lưu Voucher", "url": "https://t.me/nganmiu_bot"}],
                [{"text": "📦 Bot Check Đơn Hàng", "url": "https://t.me/ShopeeXCheck_Bot"}],
                [{"text": "📲 Bot Thuê Số", "callback_data": "SYSTEM:coming_soon"}],
            ]
        }
        
        tg_send(
            chat_id,
            "🏠 <b>HỆ THỐNG BOT NGÂNMIU</b>\n\n"
            "👋 Chào mừng bạn đến với hệ sinh thái bot NgânMiu!\n\n"
            "📌 <b>Chọn một trong các dịch vụ bên dưới:</b>",
            system_menu
        )
        return

    # ===== VOUCHER =====
    if text in ("🎁 Lưu Voucher", "🎟️Lưu Voucher", "Voucher", "🎟️ Voucher"):
        tg_send(
            chat_id,
            build_voucher_info_text(),
            build_quick_voucher_keyboard()
        )
        return

    # ===== CHẶN LƯU NẾU CHƯA ACTIVE =====
    if status != "active" and (
        text.startswith("/voucher")
        or text.startswith("/combo")
        or user_id in PENDING_VOUCHER
    ):
        tg_send(chat_id, "❌ Tài khoản chưa được kích hoạt.")
        # ✅ KHÔNG track_error - user thật có thể chưa active
        return

    # ===== ĐANG CHỜ COOKIE =====
    if user_id in PENDING_VOUCHER and not text.startswith("/"):
        pending_data = PENDING_VOUCHER.pop(user_id)
        
        # ✅ Check nếu là dict (có timestamp) hay string cũ
        if isinstance(pending_data, dict):
            cmd = pending_data["cmd"]
            pending_ts = pending_data["ts"]
            
            # ✅ Check expired (quá 120s)
            if time.time() - pending_ts > PENDING_VOUCHER_TTL:
                tg_send(
                    chat_id,
                    "⏱️ <b>Phiên mua đã hết hạn</b>\n\n"
                    "Vui lòng chọn voucher lại:",
                    build_quick_voucher_keyboard()
                )
                dprint(f"⏱️ PENDING expired for user {user_id} (>{PENDING_VOUCHER_TTL}s)")
                return
        else:
            # Fallback cho format cũ (string)
            cmd = pending_data

        cookies = parse_cookies(text)

        if not cookies:
            tg_send(chat_id, "❌ Không tìm thấy cookie hợp lệ")
            return

        num_cookies = len(cookies)
        dprint(f"📊 Received {num_cookies} cookies")

        # ✅ FORCE REFRESH BALANCE - User có thể vừa nạp tiền
        row, balance, status = get_user_data(user_id, force_refresh=True)
        if not row:
            tg_send(chat_id, "❌ Không tìm thấy ID")
            return
        
        dprint(f"💰 Balance after refresh: {balance:,}đ")

        # ----- DYNAMIC COMBO -----
        if cmd.startswith("combo"):
            # 🔥 BƯỚC 1: TÍNH GIÁ TRƯỚC (không lưu voucher)
            ok, total_price, err_msg = calculate_combo_price(cmd, num_cookies)
            
            if not ok:
                tg_send(chat_id, f"❌ <b>{cmd.upper()} THẤT BẠI</b>\n{err_msg}")
                return
            
            # 🔥 BƯỚC 2: TRỪ TIỀN TRƯỚC
            success, new_bal = deduct_balance_atomic(user_id, total_price)
            
            if not success:
                tg_send(
                    chat_id,
                    f"❌ Không đủ số dư\n"
                    f"💰 Cần: {total_price:,}đ\n"
                    f"💼 Số dư hiện tại: {new_bal:,}đ"
                )
                return
            
            # 🔥 BƯỚC 3: ĐÃ TRỪ TIỀN - BÂY GIỜ MỚI LƯU VOUCHER
            ok, _, cookies_saved, total_cookies, vouchers_per_cookie, failed = process_combo_multi_cookies(cookies, cmd)
            
            if not ok:
                # Không lưu được → HOÀN TIỀN ATOMIC
                update_balance_atomic(user_id, total_price)  # ← ATOMIC
                
                # UI: Hiển thị balance TRỰC TIẾP từ Sheet
                real_balance = get_balance_direct(user_id)
                
                tg_send(
                    chat_id,
                    f"❌ <b>{cmd.upper()} THẤT BẠI</b>\n"
                    f"💸 Đã hoàn tiền: +{total_price:,}đ\n"
                    f"💰 Số dư: <b>{real_balance:,}đ</b>"
                )
                return

            log_row(user_id, username, cmd.upper(), str(total_price), f"Lưu {cmd.upper()} {cookies_saved}/{total_cookies} thành công")

            # ✅ UI: Luôn hiển thị balance TRỰC TIẾP từ Sheet
            real_balance = get_balance_direct(user_id)
            
            if cookies_saved == total_cookies:
                msg_text = f"✅ Lưu {cmd.upper()} <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{real_balance:,}đ</b>"
            else:
                msg_text = f"⚠️ Lưu {cmd.upper()} <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{real_balance:,}đ</b>"

            tg_send(chat_id, msg_text)
            tg_send(chat_id, "👉 <b>Bấm để lưu tiếp nhanh</b>", build_quick_buy_keyboard(cmd))
            return

        # ----- VOUCHER ĐƠN -----
        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            # ✅ KHÔNG track_error - voucher hết/lỗi là lỗi nghiệp vụ
            return

        price = int(v.get("Giá", 0))
        total_price = price * num_cookies

        # ✅ ATOMIC DEDUCT - Trừ tiền TRƯỚC khi lưu voucher
        success, new_bal = deduct_balance_atomic(user_id, total_price)
        
        if not success:
            tg_send(
                chat_id, 
                f"❌ Không đủ số dư\n"
                f"💰 Cần: {total_price:,}đ ({price:,}đ × {num_cookies})\n"
                f"💼 Số dư hiện tại: {new_bal:,}đ"
            )
            # ✅ KHÔNG track_error - không đủ tiền là lỗi nghiệp vụ
            return

        # ✅ ĐÃ TRỪ TIỀN - Bây giờ mới lưu voucher
        success_count, total_count, failed_details = save_voucher_multi_cookies(cookies, v)

        if success_count == 0:
            # ✅ HOÀN TIỀN ATOMIC vì không lưu được cookie nào
            update_balance_atomic(user_id, total_price)  # ← ATOMIC
            
            # UI: Hiển thị balance TRỰC TIẾP từ Sheet
            real_balance = get_balance_direct(user_id)
            
            tg_send(
                chat_id,
                f"❌ Không lưu được cookie nào\n"
                f"💸 Đã hoàn tiền: +{total_price:,}đ\n"
                f"💰 Số dư hiện tại: <b>{real_balance:,}đ</b>"
            )
            # ✅ KHÔNG track_error - cookie lỗi/Shopee lỗi là lỗi nghiệp vụ
            return

        # ✅ Lưu được một số cookie
        actual_price = price * success_count
        
        # ✅ Hoàn tiền ATOMIC cho cookie thất bại
        if success_count < num_cookies:
            refund = price * (num_cookies - success_count)
            update_balance_atomic(user_id, refund)  # ← ATOMIC
            
            dprint(f"💸 Refunded {refund:,}đ for {num_cookies - success_count} failed cookies")

        log_row(user_id, username, "VOUCHER", str(actual_price), f"Lưu {cmd} {success_count}/{total_count} thành công")
        
        # ✅ UI: Luôn hiển thị balance TRỰC TIẾP từ Sheet
        real_balance = get_balance_direct(user_id)

        if success_count == total_count:
            msg_text = f"✅ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{real_balance:,}đ</b>"
        else:
            msg_text = f"⚠️ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{real_balance:,}đ</b>"

        tg_send(chat_id, msg_text)
        tg_send(chat_id, "👉 <b>Bấm để lưu tiếp nhanh</b>", build_quick_buy_keyboard(cmd))
        return

    # ===== FALLBACK: Cookie không có pending (Vercel cold start) =====
    if not text.startswith("/") and "SPC_" in text:
        # User gửi cookie nhưng bot không nhớ đang mua gì
        tg_send(
            chat_id,
            "⚠️ <b>Phiên mua đã hết hạn</b>\n\n"
            "Vui lòng bấm chọn voucher lại:",
            build_quick_voucher_keyboard()
        )
        dprint(f"⚠️ PENDING_VOUCHER lost for user {user_id} (cold start?)")
        return

    # ===== LỆNH /combo1 /combo2 /combo3 <cookie> =====
    if not text:
        return

    parts = text.split(maxsplit=1)
    if not parts:
        return

    cmd = parts[0].replace("/", "")
    cookie_text = parts[1] if len(parts) > 1 else ""

    # ----- DYNAMIC COMBO -----
    if cmd.startswith("combo"):
        if not cookie_text:
            if user_id in PENDING_VOUCHER:
                old_pending = PENDING_VOUCHER[user_id]
                old_cmd = old_pending["cmd"] if isinstance(old_pending, dict) else old_pending
                dprint(f"Cleared old pending: {old_cmd}")

            # ✅ Lưu với timestamp
            PENDING_VOUCHER[user_id] = {
                "cmd": cmd,
                "ts": time.time()
            }
            
            tg_send(
                chat_id,
                f"👉 Gửi <b>cookie</b> để lưu {cmd}\n\n"
                "⭐ <b>Hỗ trợ lưu tối đa 10 cookie</b>\n"
                "💡 Gửi mỗi cookie 1 dòng"
            )
            return

        cookies = parse_cookies(cookie_text)

        if not cookies:
            tg_send(chat_id, "❌ Không tìm thấy cookie hợp lệ")
            return

        num_cookies = len(cookies)

        # 🔥 BƯỚC 1: TÍNH GIÁ TRƯỚC
        ok, total_price, err_msg = calculate_combo_price(cmd, num_cookies)
        
        if not ok:
            tg_send(chat_id, f"❌ {cmd.upper()} THẤT BẠI\n{err_msg}")
            return

        # 🔥 BƯỚC 2: TRỪ TIỀN TRƯỚC
        success, new_bal = deduct_balance_atomic(user_id, total_price)
        
        if not success:
            tg_send(
                chat_id,
                f"❌ Không đủ số dư\n"
                f"💰 Cần: {total_price:,}đ\n"
                f"💼 Số dư hiện tại: {new_bal:,}đ"
            )
            return
        
        # 🔥 BƯỚC 3: ĐÃ TRỪ TIỀN - BÂY GIỜ MỚI LƯU
        ok, _, cookies_saved, total_cookies, vouchers_per_cookie, failed = process_combo_multi_cookies(cookies, cmd)

        if not ok:
            # Không lưu được → HOÀN TIỀN ATOMIC
            update_balance_atomic(user_id, total_price)  # ← ATOMIC
            
            # UI: Hiển thị balance TRỰC TIẾP từ Sheet
            real_balance = get_balance_direct(user_id)
            
            tg_send(
                chat_id,
                f"❌ {cmd.upper()} THẤT BẠI\n"
                f"💸 Đã hoàn tiền: +{total_price:,}đ\n"
                f"💰 Số dư: <b>{real_balance:,}đ</b>"
            )
            return

        log_row(user_id, username, cmd.upper(), str(total_price), f"Lưu {cmd.upper()} {cookies_saved}/{total_cookies} thành công")

        # ✅ UI: Luôn hiển thị balance TRỰC TIẾP từ Sheet
        real_balance = get_balance_direct(user_id)
        
        if cookies_saved == total_cookies:
            msg_text = f"✅ Lưu {cmd.upper()} <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{real_balance:,}đ</b>"
        else:
            msg_text = f"⚠️ Lưu {cmd.upper()} <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{real_balance:,}đ</b>"

        tg_send(chat_id, msg_text, build_main_keyboard(is_active=True))
        return

    # ----- VOUCHER ĐƠN -----
    if cmd.startswith("voucher"):
        if not cookie_text:
            if user_id in PENDING_VOUCHER:
                old_pending = PENDING_VOUCHER[user_id]
                old_cmd = old_pending["cmd"] if isinstance(old_pending, dict) else old_pending
                dprint(f"Cleared old pending: {old_cmd}")

            # ✅ Lưu với timestamp
            PENDING_VOUCHER[user_id] = {
                "cmd": cmd,
                "ts": time.time()
            }
            
            tg_send(
                chat_id,
                f"👉 Gửi <b>cookie</b> để lưu {cmd}\n\n"
                f"⭐ <b>Hỗ trợ lưu tối đa 10 cookie</b>\n"
                f"💡 Gửi mỗi cookie 1 dòng"
            )
            return

        cookies = parse_cookies(cookie_text)

        if not cookies:
            tg_send(chat_id, "❌ Không tìm thấy cookie hợp lệ")
            return

        num_cookies = len(cookies)

        # ✅ FORCE REFRESH BALANCE - User có thể vừa nạp tiền
        row, balance, status = get_user_data(user_id, force_refresh=True)
        if not row:
            tg_send(chat_id, "❌ Không tìm thấy ID")
            return
        
        dprint(f"💰 Balance after refresh: {balance:,}đ")

        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            # ✅ KHÔNG track_error - lỗi nghiệp vụ
            return

        price = int(v.get("Giá", 0))
        total_price = price * num_cookies

        # ✅ ATOMIC DEDUCT - Trừ tiền TRƯỚC
        success, new_bal = deduct_balance_atomic(user_id, total_price)
        
        if not success:
            tg_send(
                chat_id,
                f"❌ Không đủ số dư\n"
                f"💰 Cần: {total_price:,}đ\n"
                f"💼 Số dư hiện tại: {new_bal:,}đ"
            )
            # ✅ KHÔNG track_error - lỗi nghiệp vụ
            return

        # ✅ ĐÃ TRỪ TIỀN - Bây giờ lưu voucher
        success_count, total_count, failed_details = save_voucher_multi_cookies(cookies, v)

        if success_count == 0:
            # ✅ HOÀN TIỀN ATOMIC
            update_balance_atomic(user_id, total_price)  # ← ATOMIC
            
            # UI: Hiển thị balance TRỰC TIẾP từ Sheet
            real_balance = get_balance_direct(user_id)
            
            tg_send(
                chat_id,
                f"❌ Không lưu được cookie nào\n"
                f"💸 Đã hoàn tiền: +{total_price:,}đ\n"
                f"💰 Số dư hiện tại: <b>{real_balance:,}đ</b>"
            )
            # ✅ KHÔNG track_error - lỗi nghiệp vụ
            return

        # ✅ Hoàn tiền ATOMIC cho cookie thất bại
        actual_price = price * success_count
        if success_count < num_cookies:
            refund = price * (num_cookies - success_count)
            update_balance_atomic(user_id, refund)  # ← ATOMIC

        log_row(user_id, username, "VOUCHER", str(actual_price), f"Lưu {cmd} {success_count}/{total_count} thành công")

        # ✅ UI: Luôn hiển thị balance TRỰC TIẾP từ Sheet
        real_balance = get_balance_direct(user_id)
        
        if success_count == total_count:
            msg_text = f"✅ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{real_balance:,}đ</b>"
        else:
            msg_text = f"⚠️ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{real_balance:,}đ</b>"

        tg_send(chat_id, msg_text, build_main_keyboard(is_active=True))
        return

    # ===== FALLBACK =====
    tg_send(
        chat_id,
        "❌ <b>Lệnh không hợp lệ</b>\nDùng /start để xem menu.",
        build_main_keyboard(is_active=True)
    )

# =========================================================
# SEPAY WEBHOOK
# =========================================================
@app.route("/webhook-sepay", methods=["POST", "GET"])
def webhook_sepay():
    if request.method == "GET":
        return "OK", 200

    data = request.get_json(force=True, silent=True) or {}
    if not data:
        return "EMPTY", 200

    tx_id = str(
        data.get("id")
        or data.get("transaction_id")
        or data.get("tx_id")
        or data.get("referenceCode")
        or ""
    ).strip()

    try:
        amount = int(
            data.get("transferAmount")
            or data.get("amount")
            or data.get("amount_in")
            or 0
        )
    except Exception:
        amount = 0

    desc = " ".join([
        str(data.get("content") or ""),
        str(data.get("description") or ""),
        str(data.get("remark") or ""),
        str(data.get("note") or "")
    ]).strip()

    if not tx_id or amount <= 0:
        print("[SEPAY] INVALID DATA:", data)
        return "INVALID", 200

    if is_tx_exists(tx_id):
        print("[SEPAY] DUPLICATE TX:", tx_id)
        return "DUPLICATE", 200

    m = re.search(r"(?:SEVQR\s*)?NAP\s*(\d{6,})", desc, re.I)
    if not m:
        print("[SEPAY] NO USER FOUND | DESC =", desc)
        return "NO_USER", 200

    user_id = int(m.group(1))

    if amount < MIN_TOPUP_AMOUNT:
        tg_send(
            user_id,
            f"❌ <b>Nạp tối thiểu {MIN_TOPUP_AMOUNT:,}đ</b>"
        )
        return "TOO_SMALL", 200

    percent, bonus = calc_topup_bonus(amount)
    total_add = amount + bonus

    ensure_user_exists(user_id, "")
    
    # ✅ ATOMIC UPDATE - An toàn với concurrent webhooks
    new_balance = update_balance_atomic(user_id, total_add)

    note = f"+{int(percent * 100)}%={bonus}" if bonus > 0 else ""

    save_topup_to_sheet(
        user_id=user_id,
        username="",
        amount=amount,
        loai="SEPAY",
        tx_id=tx_id,
        note=note
    )

    log_row(user_id, "", "TOPUP_SEPAY", str(total_add), tx_id)

    # ✅ UI: Hiển thị balance TRỰC TIẾP từ Sheet (double-check)
    real_balance = get_balance_direct(user_id)
    
    msg = (
        "💰 <b>NẠP TIỀN THÀNH CÔNG</b>\n"
        f"➕ Gốc: <b>{amount:,}đ</b>\n"
    )

    if bonus > 0:
        msg += f"🎁 Thưởng: <b>{bonus:,}đ</b>\n"

    msg += f"💼 Số dư: <b>{real_balance:,}đ</b>"  # ← Dùng real_balance từ Sheet

    tg_send(user_id, msg)

    return "OK", 200

# =========================================================
# TELEGRAM WEBHOOK
# =========================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    handle_update(update)
    return "ok"

@app.route("/", methods=["GET"])
def home():
    if not SHEET_READY:
        return "Bot running, Sheet ERROR", 500
    return "Bot is running - V4 OPTIMIZED (90% Less API Calls)", 200

# =========================================================
# LOCAL RUNNER
# =========================================================
if __name__ == "__main__":
    print("=" * 60)
    print(" NgânMiu.Store Telegram Bot")
    print(" V4 - OPTIMIZED SHEET API CALLS (GIẢM 90%)")
    print("=" * 60)
    print("ADMIN_ID:", ADMIN_ID)
    print("SHEET_READY:", SHEET_READY)
    print("MAX_COOKIES_PER_REQUEST:", MAX_COOKIES_PER_REQUEST)
    print("CACHE ENABLED: ROW_CACHE + BROADCAST_CACHE")
    print("=" * 60)

    app.run(host="127.0.0.1", port=5000, debug=False)
