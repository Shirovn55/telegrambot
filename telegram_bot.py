# -*- coding: utf-8 -*-
"""
NgânMiu.Store — Telegram Bot
✅ PHIÊN BẢN TỐI ƯU - FIXED ALL ISSUES
✅ Anti-spam: 15 lỗi/1 phút → Ban 1H → Tái phạm → Ban vĩnh viễn
✅ Batch update (giảm API calls)
✅ Retry logic (tăng stability)
✅ Chỉ SEPAY - Xóa nạp tay
✅ ⭐ HỖ TRỢ LƯU TỐI ĐA 10 COOKIE CÙNG LÚC ⭐
"""

import os
import json
import re
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request
import urllib.parse
import time

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
MAX_COOKIES_PER_REQUEST = 10  # Tối đa 10 cookie
COOKIE_SEPARATOR = "\n"  # Phân cách bằng dòng mới

# =========================================================
# TOPUP RULES (SEPAY)
# =========================================================
MIN_TOPUP_AMOUNT = 10000

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
SPAM_THRESHOLD = 15      # 15 lỗi
SPAM_WINDOW = 60         # trong 60 giây
BAN_DURATION_1H = 3600   # 1 giờ

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
sh          = None  # ✅ Spreadsheet object (for BroadcastState sheet)
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
        wait_time = 2 ** retry_count  # 2s, 4s, 8s
        
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
# STATE (GLOBAL)
# =========================================================
PENDING_VOUCHER = {}    # user_id -> cmd
COMBO1_KEY = "combo1"

# ✅ SPAM TRACKER (in-memory, sync to sheet on ban)
SPAM_TRACKER = {}  # user_id -> {"errors": [timestamp], "ban_count": 0}

# ✅ BROADCAST COOLDOWN (tránh spam broadcast)
LAST_BROADCAST_TIME = None  # timestamp of last broadcast
BROADCAST_COOLDOWN = 60  # seconds - chỉ cho phép broadcast mỗi 60s (tăng từ 30s)

# ✅ MESSAGE DEDUPLICATION (tránh xử lý cùng message nhiều lần)
PROCESSED_MESSAGES = set()  # Lưu message_id đã xử lý
MAX_PROCESSED_MESSAGES = 1000  # Giới hạn số message lưu trong memory

# ✅ BROADCAST LOCK (đang broadcast thì không cho broadcast nữa)
IS_BROADCASTING = False

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

# =========================================================
# KEYBOARD
# =========================================================
def build_main_keyboard():
    return {
        "keyboard": [
            ["🎊 Kích Hoạt Tặng 5k", "💎 Nạp tiền"],
            ["💰 Số dư", "🎁 Lưu Voucher"],
            ["📜 Lịch sử nạp tiền"]
        ],
        "resize_keyboard": True
    }

# =========================================================
# UTIL
# =========================================================
def now_str():
    """Return current time in Vietnam timezone"""
    return datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")

def now_datetime():
    """Return current datetime in Vietnam timezone"""
    return datetime.now(VIETNAM_TZ)

def get_all_user_ids():
    """Lấy tất cả user_id từ sheet Thanh Toan"""
    if not SHEET_READY:
        return []
    try:
        all_values = ws_money.get_all_values()
        user_ids = set()  # ✅ Dùng set() tự động loại duplicate
        for row in all_values[1:]:  # Skip header
            if row and row[0]:  # Có user_id
                try:
                    user_id = int(row[0])
                    user_ids.add(user_id)  # ✅ add() thay vì append()
                except:
                    continue
        
        result = list(user_ids)  # Convert về list
        dprint(f"📊 Found {len(result)} unique users")  # ✅ Debug log
        return result
    except Exception as e:
        dprint("get_all_user_ids error:", e)
        return []

def broadcast_message(message, exclude_admin=False):
    """Gửi thông báo đến tất cả user"""
    user_ids = get_all_user_ids()
    
    if not user_ids:
        dprint("❌ No users found for broadcast")
        return 0, 0
    
    dprint(f"📢 Starting broadcast to {len(user_ids)} users...")
    
    success = 0
    failed = 0
    sent_to = set()  # ✅ Track user đã gửi để tránh duplicate
    
    for user_id in user_ids:
        # ✅ Skip nếu đã gửi cho user này rồi
        if user_id in sent_to:
            dprint(f"⚠️ Skipping duplicate user_id: {user_id}")
            continue
            
        # Bỏ qua admin nếu cần
        if exclude_admin and user_id == ADMIN_ID:
            continue
            
        try:
            # Format thông báo đẹp
            broadcast_text = f"📢 <b>THÔNG BÁO TỪ BOT</b>\n\n{message}"
            tg_send(user_id, broadcast_text)
            sent_to.add(user_id)  # ✅ Đánh dấu đã gửi
            success += 1
            # Tránh spam Telegram API
            time.sleep(0.05)  # 50ms delay giữa mỗi tin nhắn
        except Exception as e:
            dprint(f"❌ Broadcast failed for {user_id}:", e)
            failed += 1
    
    dprint(f"✅ Broadcast completed: {success} success, {failed} failed")
    return success, failed




# =========================================================
# SHEET-BASED STATE (for serverless)
# =========================================================
def get_broadcast_sheet():
    """Get or create BroadcastState sheet"""
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
    """Lấy thời gian broadcast gần nhất từ sheet"""
    ws = get_broadcast_sheet()
    if not ws:
        return None
    try:
        all_values = ws.get_all_values()
        if len(all_values) <= 1:  # Chỉ có header
            return None
        
        # Tìm broadcast STARTED/COMPLETED gần nhất
        for row in reversed(all_values[1:]):  # Skip header, đọc ngược
            if row[2] in ["STARTED", "COMPLETED"]:
                timestamp_str = row[0]
                # Parse: "2024-12-28 16:46:00"
                dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                # Convert to Vietnam timezone timestamp
                return dt.replace(tzinfo=VIETNAM_TZ).timestamp()
        
        return None
    except Exception as e:
        dprint(f"get_last_broadcast_time_from_sheet error: {e}")
        return None

def set_broadcast_state_to_sheet(admin_id, status, message_id=""):
    """Lưu broadcast state vào sheet"""
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
    """
    Check message_id đã từng broadcast chưa (chống gửi lặp)
    """
    if not message_id:
        return False

    ws = get_broadcast_sheet()
    if not ws:
        return False

    try:
        # Cột D = MessageID
        col_message_ids = ws.col_values(4)
        return str(message_id) in col_message_ids
    except Exception as e:
        dprint("is_broadcast_message_processed error:", e)
        return False

def check_broadcast_cooldown_from_sheet():
    """Check cooldown từ sheet"""
    last_time = get_last_broadcast_time_from_sheet()
    if not last_time:
        return True, 0  # OK to broadcast
    
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
def track_error(user_id, username=""):
    """
    Track lỗi của user, return True nếu cần ban
    """
    now = time.time()
    
    if user_id not in SPAM_TRACKER:
        SPAM_TRACKER[user_id] = {
            "errors": [],
            "ban_count": 0
        }
    
    tracker = SPAM_TRACKER[user_id]
    
    # Thêm timestamp lỗi hiện tại
    tracker["errors"].append(now)
    
    # Xóa lỗi cũ hơn SPAM_WINDOW
    tracker["errors"] = [t for t in tracker["errors"] if now - t < SPAM_WINDOW]
    
    # Check threshold
    if len(tracker["errors"]) >= SPAM_THRESHOLD:
        # Ban user
        ban_count = tracker["ban_count"]
        error_count = len(tracker["errors"])
        
        if ban_count == 0:
            # Lần đầu → Ban 1H
            apply_ban(user_id, "1H")
            notify_admin_spam(user_id, username, "1H", error_count)
            tracker["ban_count"] = 1
            return True
        else:
            # Tái phạm → Ban vĩnh viễn
            apply_ban(user_id, "PERMANENT")
            notify_admin_spam(user_id, username, "PERMANENT", error_count)
            return True
    
    return False

def check_ban_status(user_id):
    """
    Đọc cột F (ghi Chú) để check ban
    Return: {
        "banned": True/False,
        "type": "1H" / "PERMANENT",
        "until": "2025-12-27 10:30" / "Vĩnh viễn"
    }
    """
    if not SHEET_READY:
        return {"banned": False}
    
    row = get_user_row(user_id)
    if not row:
        return {"banned": False}
    
    try:
        note = ws_money.cell(row, 6).value or ""  # Cột F
        
        # Check BAN VĨNH VIỄN
        if "BAN VĨNH VIỄN" in note.upper():
            return {
                "banned": True,
                "type": "PERMANENT",
                "until": "Vĩnh viễn"
            }
        
        # Check BAN 1H
        if "BAN 1H:" in note:
            try:
                ban_until_str = note.split("BAN 1H:")[1].strip()
                ban_until = datetime.strptime(ban_until_str, "%Y-%m-%d %H:%M")
                # Make timezone-aware
                ban_until = ban_until.replace(tzinfo=VIETNAM_TZ)
                
                # Check còn hiệu lực không
                if now_datetime() < ban_until:
                    return {
                        "banned": True,
                        "type": "1H",
                        "until": ban_until_str
                    }
                else:
                    # Hết hạn ban → xóa note
                    ws_money.update_cell(row, 6, "auto từ bot")
                    return {"banned": False}
            except:
                pass
        
        return {"banned": False}
        
    except Exception as e:
        dprint("check_ban_status error:", e)
        return {"banned": False}

def notify_admin_spam(user_id, username, ban_type, error_count):
    """
    Gửi cảnh báo spam cho admin
    """
    if not ADMIN_ID or ADMIN_ID == 0:
        return
    
    try:
        # Lấy thông tin user
        row, balance, status = get_user_data(user_id)
        
        # Format ban info
        if ban_type == "PERMANENT":
            ban_text = "🔨 Hành động: Ban vĩnh viễn"
            time_text = "⏰ Thời gian: Vĩnh viễn"
        else:
            ban_until = now_datetime() + timedelta(seconds=BAN_DURATION_1H)
            ban_text = "🔨 Hành động: Ban 1 giờ"
            time_text = f"⏰ Hết hạn: {ban_until.strftime('%Y-%m-%d %H:%M')}"
        
        # Format username
        if username:
            user_info = f"@{username}"
        else:
            user_info = f"ID: {user_id}"
        
        # Build message
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
    """
    Ghi ban status vào cột F
    ban_type: "1H" hoặc "PERMANENT"
    """
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
        log_row(user_id, "", "BAN_APPLIED", ban_type, note)
        
        dprint(f"✅ Applied ban: {user_id} → {ban_type}")
        
    except Exception as e:
        dprint("apply_ban error:", e)

# =========================================================
# USER / MONEY UTIL
# =========================================================
def get_user_row(user_id):
    if not SHEET_READY:
        return None
    try:
        ids = ws_money.col_values(1)
        return ids.index(str(user_id)) + 1 if str(user_id) in ids else None
    except Exception:
        return None

def ensure_user_exists(user_id, username):
    if not SHEET_READY:
        return None

    row = get_user_row(user_id)
    if row:
        return row

    try:
        ws_money.append_row([
            str(user_id),
            username,
            0,
            "active",
            "auto từ bot"
        ])
    except Exception as e:
        dprint("ensure_user_exists error:", e)

    return get_user_row(user_id)

def get_user_data(user_id):
    if not SHEET_READY:
        return None, 0, ""

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

def add_balance(user_id, amount):
    """✅ Optimized with batch update"""
    if not SHEET_READY:
        return 0

    row = get_user_row(user_id)
    if not row:
        row = ensure_user_exists(user_id, "")

    try:
        bal = int(ws_money.cell(row, 3).value or 0)
        new_bal = bal + int(amount)
        
        # ✅ Single API call
        ws_money.update_cell(row, 3, new_bal)
        
        return new_bal
    except Exception as e:
        dprint("add_balance error:", e)
        return 0

# =========================================================
# TOPUP UNIQUE (ANTI DUPLICATE)
# =========================================================
def is_tx_exists(tx_id):
    if not SHEET_READY or ws_nap_tien is None:
        return False

    try:
        tx_list = ws_nap_tien.col_values(6)  # cột F = tx_id
        return str(tx_id) in tx_list
    except Exception as e:
        print("[TX_CHECK_ERROR]", e)
        return False

def save_topup_to_sheet(user_id, username, amount, loai, tx_id, note=""):
    if not SHEET_READY or ws_nap_tien is None:
        return

    try:
        ws_nap_tien.append_row([
            now_str(),  # Vietnam time
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
    Parse multiple cookies từ text
    Hỗ trợ:
    - Phân cách bằng dòng mới (\n)
    - Tự động trim whitespace
    - Bỏ qua dòng trống
    - Giới hạn MAX_COOKIES_PER_REQUEST
    
    Returns: list of cookies (max 10)
    """
    # Split by newlines
    lines = text.strip().split('\n')
    
    # Clean và filter
    cookies = []
    for line in lines:
        cookie = line.strip()
        if cookie:  # Bỏ qua dòng trống
            cookies.append(cookie)
    
    # Giới hạn số lượng
    if len(cookies) > MAX_COOKIES_PER_REQUEST:
        cookies = cookies[:MAX_COOKIES_PER_REQUEST]
    
    return cookies

# =========================================================
# VOUCHER UTIL
# =========================================================
def get_voucher(cmd):
    if not SHEET_READY:
        return None, "Hệ thống Sheet đang lỗi"

    try:
        rows = ws_voucher.get_all_records()
    except Exception:
        return None, "Không đọc được VoucherStock"

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
    """
    Lưu voucher cho nhiều cookie
    
    Returns:
        success_count: số cookie lưu thành công
        total_count: tổng số cookie
        failed_details: [(cookie_index, reason)]
    """
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
        
        # Delay nhẹ giữa các request
        if idx < len(cookies):
            time.sleep(0.1)
    
    return success_count, len(cookies), failed_details

# =========================================================
# COMBO UTIL
# =========================================================
def get_vouchers_by_combo(combo_key):
    if not SHEET_READY:
        return [], "Hệ thống Sheet đang lỗi"

    try:
        rows = ws_voucher.get_all_records()
    except Exception:
        return [], "Không đọc được VoucherStock"

    items = []
    for r in rows:
        c = str(r.get("Combo", "")).strip().lower()
        if c == combo_key.strip().lower():
            if r.get("Trạng Thái") == "Còn Mã":
                items.append(r)

    if not items:
        return [], "Combo hiện không có mã"

    return items, None

def process_combo1(cookie):
    """Process COMBO1 với 1 cookie"""
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

def process_combo1_multi_cookies(cookies):
    """
    Process COMBO1 với nhiều cookie
    
    Returns:
        success: True/False
        total_price: tổng giá phải trả
        cookies_saved: số cookie lưu thành công
        total_cookies: tổng số cookie
        vouchers_per_cookie: số voucher mỗi cookie
        failed_details: [(cookie_idx, voucher_name, reason)]
    """
    vouchers, err = get_vouchers_by_combo(COMBO1_KEY)
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
            
            # Delay giữa các voucher
            time.sleep(0.1)
        
        if cookie_success:
            cookies_saved += 1
        
        # Delay giữa các cookie
        if cookie_idx < len(cookies):
            time.sleep(0.2)
    
    if cookies_saved == 0:
        return False, "Không lưu được cookie nào", 0, len(cookies), len(vouchers), failed_details
    
    total_price = cookies_saved * price_per_cookie
    
    return True, total_price, cookies_saved, len(cookies), len(vouchers), failed_details


# =========================================================
# ⭐ DYNAMIC VOUCHER KEYBOARD FROM SHEET ⭐
# =========================================================

# Cache để giảm API calls
VOUCHER_KEYBOARD_CACHE = {
    "keyboard": None,
    "info_text": None,
    "last_update": 0
}
KEYBOARD_CACHE_DURATION = 60  # 60 giây

def apply_strikethrough(text):
    """Apply strikethrough Unicode characters"""
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
        '%': '%̶', '+': '+̶', '/': '/̶', ' ': ' ̶', 'đ': 'đ̶', 'á': 'á̶', 'à': 'à̶', 'ả': 'ả̶',
        'ã': 'ã̶', 'ạ': 'ạ̶', 'â': 'â̶', 'ê': 'ê̶', 'í': 'í̶', 'ì': 'ì̶', 'ỉ': 'ỉ̶', 'ĩ': 'ĩ̶',
        'ị': 'ị̶', 'ó': 'ó̶', 'ò': 'ò̶', 'ỏ': 'ỏ̶', 'õ': 'õ̶', 'ọ': 'ọ̶', 'ô': 'ô̶', 'ơ': 'ơ̶',
        'ú': 'ú̶', 'ù': 'ù̶', 'ủ': 'ủ̶', 'ũ': 'ũ̶', 'ụ': 'ụ̶', 'ư': 'ư̶', 'ý': 'ý̶', 'ỳ': 'ỳ̶',
        'ỷ': 'ỷ̶', 'ỹ': 'ỹ̶', 'ỵ': 'ỵ̶', 'ế': 'ế̶', 'ề': 'ề̶', 'ể': 'ể̶', 'ễ': 'ễ̶', 'ệ': 'ệ̶',
        'ố': 'ố̶', 'ồ': 'ồ̶', 'ổ': 'ổ̶', 'ỗ': 'ỗ̶', 'ộ': 'ộ̶', 'ớ': 'ớ̶', 'ờ': 'ờ̶', 'ở': 'ở̶',
        'ỡ': 'ỡ̶', 'ợ': 'ợ̶', 'ứ': 'ứ̶', 'ừ': 'ừ̶', 'ử': 'ử̶', 'ữ': 'ữ̶', 'ự': 'ự̶',
    }
    result = ""
    for char in text:
        result += strikethrough_map.get(char, char)
    return result

def parse_position(pos_str):
    """
    Parse position string: 1A, 1B, 2A, B1, C2, etc.
    Hỗ trợ cả 2 format:
    - Số + Chữ: 1A, 2B, 10C... → (row_num, col_letter)
    - Chữ + Số: A1, B2, C3... → (col_letter_as_row, number_as_col)
    
    Returns: (row_num, col_letter) or None
    """
    if not pos_str or not isinstance(pos_str, str):
        return None
    
    pos_str = pos_str.strip().upper()
    
    import re
    
    # Format 1: Số + Chữ (1A, 2B, 10C...)
    match = re.match(r'^(\d+)([A-Z])$', pos_str)
    if match:
        row_num = int(match.group(1))
        col_letter = match.group(2)
        return (row_num, col_letter)
    
    # Format 2: Chữ + Số (A1, B2, C3...)
    # Convert: A1 → (1, A), B1 → (2, A), C1 → (3, A)
    match = re.match(r'^([A-Z])(\d+)$', pos_str)
    if match:
        letter = match.group(1)
        number = int(match.group(2))
        
        # Map letter to row: A=1, B=2, C=3...
        row_num = ord(letter) - ord('A') + 1
        
        # Map number to column: 1=A, 2=B, 3=C...
        col_letter = chr(ord('A') + number - 1)
        
        return (row_num, col_letter)
    
    return None

def build_voucher_keyboard_from_sheet():
    """
    Build keyboard dynamically from VoucherStock sheet
    Returns: (keyboard_dict, info_text)
    """
    if not SHEET_READY:
        dprint("❌ Sheet not ready, using static keyboard")
        return build_static_voucher_keyboard()
    
    try:
        dprint("📊 Reading VoucherStock sheet...")
        all_rows = ws_voucher.get_all_records()
        dprint(f"📊 Found {len(all_rows)} rows in VoucherStock")
        
        vouchers_by_position = {}
        has_combo = False
        combo_price = 0
        combo_count = 0
        
        info_lines = ["🎊 <b>VOUCHER HIỆN CÓ - HAPPY NEW YEAR 2026!</b> 🎊\n━━━━━━━━━━━━━━━"]
        
        for idx, row in enumerate(all_rows, 1):
            dprint(f"Row {idx}: {row.get('Tên Mã', 'N/A')}")
            
            # Debug: Show all available column names
            if idx == 1:
                dprint(f"  📋 Available columns: {list(row.keys())}")
            
            # ✅ CHECK "Display" COLUMN - Try multiple variations
            display = ""
            for key in ["Display", "Show", "Visible", "Hiển thị", "Hiển Thị", "Hien thi", "Hien Thi"]:
                if key in row:
                    display = str(row[key]).strip().upper()
                    if display:
                        dprint(f"  Found display column: '{key}' = '{display}'")
                        break
            
            dprint(f"  Display value: '{display}'")
            
            # Accept: YES, Y, TRUE, 1
            if display not in ["YES", "Y", "TRUE", "1"]:
                dprint(f"  ⚠️ Skipped (Display != Yes)")
                continue
            
            pos_str = str(row.get("Vị trí", "")).strip()
            if not pos_str:
                pos_str = str(row.get("Vị Trí", "")).strip()
            if not pos_str:
                pos_str = str(row.get("Position", "")).strip()
            dprint(f"  Position: '{pos_str}'")
            
            combo = str(row.get("Combo", "")).strip().lower()
            if combo == "combo1":
                has_combo = True
                try:
                    combo_price += int(row.get("Giá", 0))
                    combo_count += 1
                except:
                    pass
            
            if not pos_str:
                dprint(f"  ⚠️ Skipped (no position)")
                continue
            
            position = parse_position(pos_str)
            if not position:
                dprint(f"  ⚠️ Invalid position format: {pos_str}")
                continue
            
            dprint(f"  ✅ Added at position {position}")
            vouchers_by_position[position] = row
        
        dprint(f"📊 Total vouchers with valid position: {len(vouchers_by_position)}")
        
        if len(vouchers_by_position) == 0:
            dprint("❌ No vouchers found, using static keyboard")
            return build_static_voucher_keyboard()
        
        keyboard_rows = []
        current_row_num = None
        current_row_buttons = []
        
        sorted_positions = sorted(vouchers_by_position.keys())
        dprint(f"📊 Sorted positions: {sorted_positions}")
        
        for position in sorted_positions:
            row_num, col_letter = position
            voucher = vouchers_by_position[position]
            
            if current_row_num != row_num:
                if current_row_buttons:
                    keyboard_rows.append(current_row_buttons)
                    dprint(f"Added row {current_row_num}: {len(current_row_buttons)} buttons")
                current_row_buttons = []
                current_row_num = row_num
            
            ten_hien_thi = str(voucher.get("Tên hiển thị", "")).strip()
            if not ten_hien_thi:
                ten_hien_thi = str(voucher.get("Tên Hiển Thị", "")).strip()
            if not ten_hien_thi:
                ten_hien_thi = str(voucher.get("Display Name", "")).strip()
            if not ten_hien_thi:
                ten_hien_thi = str(voucher.get("DisplayName", "")).strip()
            if not ten_hien_thi:
                ten_hien_thi = str(voucher.get("Tên Mã", "")).strip()
            
            dprint(f"    Display Name: '{ten_hien_thi}'")
            
            trang_thai = str(voucher.get("Trạng Thái", "")).strip()
            if not trang_thai:
                trang_thai = str(voucher.get("Trạng thái", "")).strip()
            
            ten_ma = str(voucher.get("Tên Mã", "")).strip()
            if not ten_ma:
                ten_ma = str(voucher.get("Tên mã", "")).strip()
            
            gia = int(voucher.get("Giá", 0))
            
            is_sold_out = trang_thai != "Còn Mã"
            
            if is_sold_out:
                button_text = f"⚫ {apply_strikethrough(ten_hien_thi)} (Hết)"
                callback_data = f"SOLD_OUT:{ten_ma}"
            else:
                # ✨ Thêm emoji năm mới ngẫu nhiên
                new_year_emojis = ["🎊", "🎉", "✨", "🎁", "🔥", "⭐", "💫"]
                import random
                emoji = random.choice(new_year_emojis)
                button_text = f"{emoji} {ten_hien_thi}"
                callback_data = f"BUY:{ten_ma}"
            
            current_row_buttons.append({
                "text": button_text,
                "callback_data": callback_data
            })
            
            if not is_sold_out:
                info_lines.append(f"• {ten_hien_thi} — 💰Giá {gia:,} VNĐ")
        
        if current_row_buttons:
            keyboard_rows.append(current_row_buttons)
            dprint(f"Added last row {current_row_num}: {len(current_row_buttons)} buttons")
        
        if has_combo:
            keyboard_rows.append([{
                "text": "🎆 COMBO1 | Mã 100k + Ship HT 🎆",
                "callback_data": "BUY:combo1"
            }])
            info_lines.append(f"\n🟣 <b>COMBO ĐẶC BIỆT</b>")
            info_lines.append(f"• COMBO1: 100k/0đ + Freeship Hỏa Tốc")
            info_lines.append(f"  💰 {combo_price:,} VNĐ | 🎫 {combo_count} mã")
        
        info_lines.append("\n⭐ <b>HỖ TRỢ LƯU TỐI ĐA 10 COOKIE</b>")
        info_lines.append("💡 Gửi mỗi cookie 1 dòng")
        info_lines.append("\n👇 <b>BẤM NÚT BÊN DƯỚI ĐỂ MUA</b>")
        
        keyboard = {"inline_keyboard": keyboard_rows}
        info_text = "\n".join(info_lines)
        
        dprint(f"✅ Built keyboard with {len(keyboard_rows)} rows")
        
        return keyboard, info_text
        
    except Exception as e:
        dprint(f"❌ Error building keyboard from sheet: {e}")
        import traceback
        traceback.print_exc()
        return build_static_voucher_keyboard()

def build_static_voucher_keyboard():
    """Fallback static keyboard"""
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
    """Get voucher keyboard with cache (60s)"""
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
    """Get info text (with cache)"""
    _, info_text = get_voucher_keyboard_cached()
    return info_text

def build_quick_voucher_keyboard():
    """Get keyboard (with cache)"""
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
    if not SHEET_READY:
        return False, "❌ Hệ thống đang lỗi."

    row = get_user_row(user_id)

    if not row:
        row = ensure_user_exists(user_id, username)

    data = ws_money.row_values(row)
    status = data[3] if len(data) > 3 else ""

    if status in ("active", "trial_used"):
        return False, "⚠️ ACC đã kích hoạt và nhận khuyến mãi rồi."

    # ✅ Batch update: status + balance cùng lúc
    try:
        current_balance = int(data[2]) if len(data) > 2 else 0
        new_balance = current_balance + 5000
        
        # Single API call
        ws_money.update(f'C{row}:D{row}', [[new_balance, "active"]])
        
        log_row(user_id, username, "ACTIVE_GIFT_5K", "5000", "Kích hoạt + tặng 5k")
        
        return True, new_balance
    except Exception as e:
        dprint("handle_active_gift_5k error:", e)
        return False, "❌ Lỗi khi cập nhật"

# =========================================================
# CALLBACK QUERY HANDLER
# =========================================================

# =========================================================
# TỔNG KẾT KINH DOANH
# =========================================================

# =========================================================
# TỔNG KẾT KINH DOANH - ĐÚNG THEO CẤU TRÚC SHEET
# =========================================================

def parse_date_from_sheet(date_str):
    """Parse date từ sheet (format: 2025-12-31 14:30:45)"""
    try:
        if isinstance(date_str, datetime):
            return date_str
        # Format: "2025-12-31 14:30:45"
        return datetime.strptime(str(date_str).strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            # Backup: "31/12/2025 14:30:45"
            return datetime.strptime(str(date_str).strip(), "%d/%m/%Y %H:%M:%S")
        except Exception:
            return None

def get_today_stats():
    """Lấy thống kê chi tiết từng loại voucher"""
    if not SHEET_READY:
        return None
    
    today = datetime.now(VIETNAM_TZ).date()
    stats = {
        "napten_count": 0,
        "napten_amount": 0,
        "napten_bonus": 0,
        "napten_users": set(),
        "voucher_details": {},  # {"voucher100k": 15, "combo1": 8}
        "total_usage": 0,
        "active_users": set(),
    }
    
    # ===== ĐỌC NAP TIEN =====
    try:
        if ws_nap_tien:
            all_rows = ws_nap_tien.get_all_values()
            for row in all_rows[1:]:  # Skip header
                if len(row) < 7:
                    continue
                try:
                    # Cột A = time
                    row_date = parse_date_from_sheet(row[0])
                    if row_date and row_date.date() == today:
                        user_id = int(row[1])  # Cột B = Tele ID
                        amount = int(row[3]) if row[3] else 0  # Cột D = số tiền
                        note = row[6]  # Cột G = nội dung (+10%=7500)
                        
                        stats["napten_count"] += 1
                        stats["napten_amount"] += amount
                        stats["napten_users"].add(user_id)
                        stats["active_users"].add(user_id)
                        
                        # Parse bonus từ note (+10%=7500)
                        if note and "=" in note:
                            try:
                                stats["napten_bonus"] += int(note.split("=")[1])
                            except:
                                pass
                except:
                    continue
    except Exception as e:
        dprint(f"Error reading Nap Tien: {e}")
    
    # ===== ĐỌC LOGS - ĐẾM TỪNG LOẠI VOUCHER =====
    try:
        if ws_log:
            all_logs = ws_log.get_all_values()
            for row in all_logs[1:]:  # Skip header
                if len(row) < 6:
                    continue
                try:
                    # Cột A = time
                    row_date = parse_date_from_sheet(row[0])
                    if row_date and row_date.date() == today:
                        user_id = int(row[1])  # Cột B = Tele ID
                        action = row[3]  # Cột D = voucher/COMBO1/AUTO_ACTIVE
                        details = row[5]  # Cột F = voucher100k hoặc balance_sau
                        
                        stats["active_users"].add(user_id)
                        
                        # ĐẾM VOUCHER ĐƠN
                        if action == "VOUCHER":
                            # Cột F = tên voucher (voucher100k, voucher50max200, voucherHoaToc)
                            voucher_name = details
                            if voucher_name not in stats["voucher_details"]:
                                stats["voucher_details"][voucher_name] = 0
                            stats["voucher_details"][voucher_name] += 1
                            stats["total_usage"] += 1
                        
                        # ĐẾM COMBO1
                        elif action == "COMBO1":
                            if "COMBO1" not in stats["voucher_details"]:
                                stats["voucher_details"]["COMBO1"] = 0
                            stats["voucher_details"]["COMBO1"] += 1
                            stats["total_usage"] += 1
                except:
                    continue
    except Exception as e:
        dprint(f"Error reading Logs: {e}")
    
    # Convert set to count
    stats["napten_users"] = len(stats["napten_users"])
    stats["active_users"] = len(stats["active_users"])
    
    return stats

def format_tongket_message(stats):
    """Format message tổng kết"""
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
    
    # Hiển thị chi tiết từng voucher
    if stats["voucher_details"]:
        # Sắp xếp theo số lượng
        sorted_vouchers = sorted(
            stats["voucher_details"].items(), 
            key=lambda x: x[1], 
            reverse=True
        )
        
        for voucher_name, count in sorted_vouchers:
            # Format tên đẹp
            if voucher_name == "COMBO1":
                display_name = "🎁 COMBO1"
            elif "100k" in voucher_name.lower():
                display_name = "💸 Mã 100k 0đ"
            elif "50max200" in voucher_name.lower():
                display_name = "💸 Mã 50% Max 200k"
            elif "hoatoc" in voucher_name.lower():
                display_name = "🚀 Freeship Hỏa Tốc"
            else:
                display_name = f"🎫 {voucher_name}"
            
            msg += f"\n• {display_name}: <b>{count}</b> lượt"
        
        msg += f"\n\n<b>━ Tổng: {stats['total_usage']} lượt lưu</b>"
    else:
        msg += "\n<i>Chưa có voucher nào được lưu</i>"
    
    msg += f"\n\n━━━━━━━━━━━━━━━━━━\n👥 <b>USER HOẠT ĐỘNG</b>\n• Tổng: <b>{stats['active_users']}</b> user"
    
    return msg

def handle_tongket_command(chat_id, user_id):
    """Xử lý lệnh /tongket"""
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

def handle_callback_query(cb):
    cb_id = cb.get("id")
    data = cb.get("data", "")
    from_user = cb.get("from", {})
    user_id = from_user.get("id")

    # SOLD_OUT:voucher100k
    if data.startswith("SOLD_OUT:"):
        tg_answer_callback(cb_id, "⚠️ Voucher này tạm hết mã. Vui lòng quay lại sau!", True)
        return

    # BUY:voucher100k | BUY:combo1
    if data.startswith("BUY:"):
        cmd = data.split(":", 1)[1]

        row, balance, status = get_user_data(user_id)
        if not row:
            tg_answer_callback(cb_id, "❌ Bạn chưa có ID", True)
            return

        if status != "active":
            tg_answer_callback(cb_id, "❌ Tài khoản chưa được kích hoạt", True)
            return

        # ✅ Xóa lệnh cũ nếu có
        if user_id in PENDING_VOUCHER:
            old_cmd = PENDING_VOUCHER[user_id]
            dprint(f"Cleared old pending: {old_cmd}")

        PENDING_VOUCHER[user_id] = cmd

        tg_answer_callback(cb_id)
        tg_send(
            user_id,
            f"👉 Gửi <b>cookie</b> vào đây để lưu <b>{cmd}</b>\n\n"
            f"⭐ <b>Hỗ trợ lưu tối đa 10 cookie</b>\n"
            f"💡 Gửi mỗi cookie 1 dòng"
        )
        return

    tg_answer_callback(cb_id, "⚠️ Thao tác không hỗ trợ", True)

# =========================================================
# CORE UPDATE HANDLER
# =========================================================
def handle_update(update):
    dprint("UPDATE:", update)
    
    # ✅ MESSAGE DEDUPLICATION - Tránh xử lý cùng message nhiều lần
    global PROCESSED_MESSAGES
    msg = update.get("message", {})
    message_id = msg.get("message_id")
    
    if message_id:
        # Tạo unique key: chat_id + message_id
        chat_id = msg.get("chat", {}).get("id")
        msg_key = f"{chat_id}_{message_id}"
        
        if msg_key in PROCESSED_MESSAGES:
            dprint(f"⚠️ DUPLICATE MESSAGE DETECTED: {msg_key} - SKIPPING")
            return  # ✅ BỎ QUA message duplicate
        
        # Thêm vào set
        PROCESSED_MESSAGES.add(msg_key)
        
        # Giới hạn memory: nếu quá 1000 messages, xóa cũ
        if len(PROCESSED_MESSAGES) > MAX_PROCESSED_MESSAGES:
            # Convert to list, xóa 100 message cũ nhất
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
        
        return  # ✅ CHẶN HOÀN TOÀN

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
    
    # /update - Force reload keyboard + show menu (Admin only)
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
        
        # Rebuild keyboard cache
        voucher_keyboard, voucher_info = get_voucher_keyboard_cached()
        
        # Send success message with main menu
        tg_send(
            chat_id, 
            "✅ Đã cập nhật keyboard từ Sheet!\n\n"
            "🎊 <b>Menu đã được refresh</b>",
            build_main_keyboard()
        )
        
        # Show voucher keyboard luôn
        tg_send(chat_id, voucher_info, voucher_keyboard)
        return
    
    # ✅ Skip messages không có text (ảnh, sticker, voice...)
    # Chỉ xử lý các message quan trọng không cần text
    if not text:
        # Cho phép qua nếu đang chờ cookie (user có thể gửi nhầm ảnh)
        if user_id not in PENDING_VOUCHER:
            return
    # ===== ADMIN: /thongbao =====
    if text and text.startswith("/thongbao"):
        # 🔒 Chỉ admin mới được dùng
        if user_id != ADMIN_ID:
            tg_send(chat_id, "⛔ Lệnh này chỉ dành cho Admin")
            return

        # ✅ Lấy message_id (dùng để chống gửi lặp)
        message_id = msg.get("message_id", 0)

        # ✅ Tách nội dung
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

        # ❌ CHẶN NẾU MESSAGE_ID ĐÃ TỪNG BROADCAST
        # (chỉ check khi cú pháp hợp lệ)
        if is_broadcast_message_processed(message_id):
            tg_send(
                chat_id,
                "⚠️ <b>Thông báo này đã được gửi trước đó</b>\n"
                "Bot đã tự động bỏ qua để tránh gửi lặp."
            )
            dprint(f"⚠️ DUPLICATE BROADCAST BLOCKED: msg_id={message_id}")
            return

        # ✅ CHECK COOLDOWN từ sheet (serverless-safe)
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
        # 🔒 BROADCAST LOCK (chặn chạy song song)
        global IS_BROADCASTING
        if IS_BROADCASTING:
            tg_send(
                chat_id,
                "⛔ <b>Đang có broadcast khác chạy</b>\n"
                "Vui lòng đợi broadcast trước hoàn tất."
            )
            return

        IS_BROADCASTING = True

        # ✅ LƯU STATE STARTED (để chống retry / resend)
        if not set_broadcast_state_to_sheet(user_id, "STARTED", message_id):
            IS_BROADCASTING = False 
            tg_send(chat_id, "❌ Lỗi khi lưu trạng thái broadcast, vui lòng thử lại")
            return

        dprint(f"📝 Broadcast STARTED | admin={user_id} | msg_id={message_id}")

        # ✅ PHẢN HỒI NGAY
        tg_send(
            chat_id,
            "✅ <b>ĐÃ NHẬN LỆNH BROADCAST</b>\n\n"
            "⏳ Đang gửi thông báo...\n"
            "📊 Kết quả sẽ được trả về sau khi hoàn tất."
        )

        try:
            # 🚀 THỰC HIỆN BROADCAST
            dprint(f"🔔 Broadcasting: {message[:40]}...")
            success, failed = broadcast_message(message, exclude_admin=False)

            # Log
            log_row(user_id, username, "BROADCAST", str(success), message[:50])

            # ✅ LƯU STATE COMPLETED
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
            # 🔓 MỞ KHÓA BROADCAST
            IS_BROADCASTING = False



    # ===== /start =====
    if text == "/start":
        row = ensure_user_exists(user_id, username)
        row, balance, status = get_user_data(user_id)

        if status != "active" or balance == 0:
            # ✅ Batch update
            try:
                new_bal = balance + 5000
                ws_money.update(f'C{row}:D{row}', [[new_bal, "active"]])
                
                log_row(user_id, username, "AUTO_ACTIVE", "5000", "Auto kích hoạt khi /start")

                tg_send(
                    chat_id,
                    f"🎉 <b>KÍCH HOẠT THÀNH CÔNG</b>\n\n"
                    f"🆔 ID: <code>{user_id}</code>\n"
                    f"🎁 +5.000đ\n"
                    f"💰 Số dư: <b>{new_bal:,}đ</b>",
                    build_main_keyboard()
                )
            except Exception as e:
                dprint("/start error:", e)
                # ✅ Track lỗi
                if track_error(user_id, username):
                    tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
        else:
            tg_send(chat_id, "👋 <b>Chào mừng quay lại!</b>", build_main_keyboard())
        return

    # ===== KÍCH HOẠT + TẶNG 5K =====
    if text in ("🎊 Kích Hoạt Tặng 5k", "🎁 Kích Hoạt Tặng 5k"):
        ok, result = handle_active_gift_5k(user_id, username)

        if not ok:
            tg_send(chat_id, result)
            # ✅ Track lỗi
            if track_error(user_id, username):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        tg_send(
            chat_id,
            f"🎉 <b>KÍCH HOẠT THÀNH CÔNG</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"🎁 Khuyến mãi: <b>+5.000đ</b>\n"
            f"💰 Số dư hiện tại: <b>{result:,}đ</b>\n\n"
            f"👉 <b>Bấm nút bên dưới để sử dụng ngay</b>",
            build_main_keyboard()
        )
        return

    # ===== NẠP TIỀN (CHỈ SEPAY) =====
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
        tg_send(
            chat_id,
            f"💰 <b>Số dư:</b> <b>{balance:,}đ</b>\n"
            f"📌 Trạng thái: <b>{status}</b>",
            build_main_keyboard()
        )
        return

    # ===== LỊCH SỬ =====
    if text in ("📜 Lịch sử nạp tiền", "/topup_history"):
        tg_send(chat_id, topup_history_text(user_id))
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
        # ✅ Track lỗi
        if track_error(user_id):
            tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
        return

    # ===== ĐANG CHỜ COOKIE =====
    if user_id in PENDING_VOUCHER and not text.startswith("/"):
        cmd = PENDING_VOUCHER.pop(user_id)
        
        # ⭐ PARSE MULTIPLE COOKIES
        cookies = parse_cookies(text)
        
        if not cookies:
            tg_send(chat_id, "❌ Không tìm thấy cookie hợp lệ")
            return
        
        num_cookies = len(cookies)
        dprint(f"📊 Received {num_cookies} cookies")

        # ----- COMBO1 -----
        if cmd == COMBO1_KEY:
            ok, total_price, cookies_saved, total_cookies, vouchers_per_cookie, failed = process_combo1_multi_cookies(cookies)

            if not ok:
                tg_send(chat_id, f"❌ <b>COMBO1 THẤT BẠI</b>\n{total_price}")
                if track_error(user_id):
                    tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
                return

            if balance < total_price:
                tg_send(chat_id, f"❌ Không đủ số dư\n💰 Cần: {total_price:,}đ")
                if track_error(user_id):
                    tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
                return

            new_bal = balance - total_price
            ws_money.update_cell(row, 3, new_bal)

            log_row(user_id, username, "COMBO1", str(total_price), f"Lưu COMBO1 {cookies_saved}/{total_cookies} thành công")

            if cookies_saved == total_cookies:
                msg_text = f"✅ Lưu COMBO1 <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{new_bal:,}đ</b>"
            else:
                msg_text = f"⚠️ Lưu COMBO1 <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{new_bal:,}đ</b>"

            tg_send(chat_id, msg_text)
            tg_send(chat_id, "👉 <b>Bấm để lưu tiếp nhanh</b>", build_quick_buy_keyboard("combo1"))
            return

        # ----- VOUCHER ĐƠN -----
        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        price = int(v.get("Giá", 0))
        total_price = price * num_cookies
        
        if balance < total_price:
            tg_send(chat_id, f"❌ Không đủ số dư\n💰 Cần: {total_price:,}đ ({price:,}đ × {num_cookies})")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        # ⭐ SAVE TO MULTI COOKIES
        success_count, total_count, failed_details = save_voucher_multi_cookies(cookies, v)
        
        if success_count == 0:
            tg_send(chat_id, "❌ Không lưu được cookie nào\n💸 Không trừ tiền")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        # Calculate actual price
        actual_price = price * success_count
        new_bal = balance - actual_price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "VOUCHER", str(actual_price), f"Lưu {cmd} {success_count}/{total_count} thành công")

        if success_count == total_count:
            msg_text = f"✅ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{new_bal:,}đ</b>"
        else:
            msg_text = f"⚠️ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{new_bal:,}đ</b>"

        tg_send(chat_id, msg_text)
        tg_send(chat_id, "👉 <b>Bấm để lưu tiếp nhanh</b>", build_quick_buy_keyboard(cmd))
        return

    # ===== LỆNH /voucherxxx <cookie> =====
    # Skip nếu không có text (ví dụ: user gửi ảnh, sticker...)
    if not text:
        return
    
    parts = text.split(maxsplit=1)
    if not parts:
        return
    
    cmd = parts[0].replace("/", "")
    cookie_text = parts[1] if len(parts) > 1 else ""

    # ----- COMBO1 -----
    if cmd == COMBO1_KEY:
        if not cookie_text:
            # ✅ Xóa lệnh cũ
            if user_id in PENDING_VOUCHER:
                dprint(f"Cleared old pending: {PENDING_VOUCHER[user_id]}")
            
            PENDING_VOUCHER[user_id] = COMBO1_KEY
            tg_send(
                chat_id,
                "👉 Gửi <b>cookie</b> để lưu combo1\n\n"
                "⭐ <b>Hỗ trợ lưu tối đa 10 cookie</b>\n"
                "💡 Gửi mỗi cookie 1 dòng"
            )
            return

        # ⭐ PARSE MULTIPLE COOKIES
        cookies = parse_cookies(cookie_text)
        
        if not cookies:
            tg_send(chat_id, "❌ Không tìm thấy cookie hợp lệ")
            return
        
        num_cookies = len(cookies)

        ok, total_price, cookies_saved, total_cookies, vouchers_per_cookie, failed = process_combo1_multi_cookies(cookies)

        if not ok:
            tg_send(chat_id, f"❌ COMBO1 THẤT BẠI\n{total_price}")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        if balance < total_price:
            tg_send(chat_id, f"❌ Không đủ số dư\n💰 Cần: {total_price:,}đ")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        new_bal = balance - total_price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "COMBO1", str(total_price), f"Lưu COMBO1 {cookies_saved}/{total_cookies} thành công")

        if cookies_saved == total_cookies:
            msg_text = f"✅ Lưu COMBO1 <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{new_bal:,}đ</b>"
        else:
            msg_text = f"⚠️ Lưu COMBO1 <b>{cookies_saved}/{total_cookies}</b> thành công | -{total_price:,}đ | Còn: <b>{new_bal:,}đ</b>"

        tg_send(chat_id, msg_text, build_main_keyboard())
        return

    # ----- VOUCHER ĐƠN -----
    if cmd.startswith("voucher"):
        if not cookie_text:
            # ✅ Xóa lệnh cũ
            if user_id in PENDING_VOUCHER:
                dprint(f"Cleared old pending: {PENDING_VOUCHER[user_id]}")
            
            PENDING_VOUCHER[user_id] = cmd
            tg_send(
                chat_id,
                f"👉 Gửi <b>cookie</b> để lưu {cmd}\n\n"
                f"⭐ <b>Hỗ trợ lưu tối đa 10 cookie</b>\n"
                f"💡 Gửi mỗi cookie 1 dòng"
            )
            return

        # ⭐ PARSE MULTIPLE COOKIES
        cookies = parse_cookies(cookie_text)
        
        if not cookies:
            tg_send(chat_id, "❌ Không tìm thấy cookie hợp lệ")
            return
        
        num_cookies = len(cookies)

        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        price = int(v.get("Giá", 0))
        total_price = price * num_cookies
        
        if balance < total_price:
            tg_send(chat_id, f"❌ Không đủ số dư\n💰 Cần: {total_price:,}đ")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        # ⭐ SAVE TO MULTI COOKIES
        success_count, total_count, failed_details = save_voucher_multi_cookies(cookies, v)
        
        if success_count == 0:
            tg_send(chat_id, "❌ Không lưu được cookie nào\n💸 Không trừ tiền")
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        actual_price = price * success_count
        new_bal = balance - actual_price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "VOUCHER", str(actual_price), f"Lưu {cmd} {success_count}/{total_count} thành công")

        if success_count == total_count:
            msg_text = f"✅ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{new_bal:,}đ</b>"
        else:
            msg_text = f"⚠️ Lưu <b>{success_count}/{total_count}</b> thành công | -{actual_price:,}đ | Còn: <b>{new_bal:,}đ</b>"

        tg_send(chat_id, msg_text, build_main_keyboard())
        return

    # ===== FALLBACK =====
    tg_send(
        chat_id,
        "❌ <b>Lệnh không hợp lệ</b>\nDùng /start để xem menu.",
        build_main_keyboard()
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
    new_balance = add_balance(user_id, total_add)

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

    msg = (
        "💰 <b>NẠP TIỀN THÀNH CÔNG</b>\n"
        f"➕ Gốc: <b>{amount:,}đ</b>\n"
    )

    if bonus > 0:
        msg += f"🎁 Thưởng: <b>{bonus:,}đ</b>\n"

    msg += f"💼 Số dư: <b>{new_balance:,}đ</b>"

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
    return "Bot is running", 200

# =========================================================
# LOCAL RUNNER
# =========================================================
if __name__ == "__main__":
    print("=" * 60)
    print(" NgânMiu.Store Telegram Bot - MULTI-COOKIE VERSION")
    print("=" * 60)
    print("ADMIN_ID:", ADMIN_ID)
    print("SHEET_READY:", SHEET_READY)
    print("MAX_COOKIES_PER_REQUEST:", MAX_COOKIES_PER_REQUEST)
    print("=" * 60)

    app.run(host="127.0.0.1", port=5000, debug=False)
