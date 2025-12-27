# -*- coding: utf-8 -*-
"""
NgânMiu.Store — Telegram Bot
✅ PHIÊN BẢN TỐI ƯU - FIXED ALL ISSUES
✅ Anti-spam: 15 lỗi/1 phút → Ban 1H → Tái phạm → Ban vĩnh viễn
✅ Batch update (giảm API calls)
✅ Retry logic (tăng stability)
✅ Chỉ SEPAY - Xóa nạp tay
"""

import os
import json
import re
import requests
from datetime import datetime, timedelta
from flask import Flask, request
import urllib.parse
import time

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
            ["🎁 Kích Hoạt Tặng 5k", "💳 Nạp tiền"],
            ["💰 Số dư", "🎟️Lưu Voucher"],
            ["📜 Lịch sử nạp tiền"]
        ],
        "resize_keyboard": True
    }

# =========================================================
# UTIL
# =========================================================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
def track_error(user_id):
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
        
        if ban_count == 0:
            # Lần đầu → Ban 1H
            apply_ban(user_id, "1H")
            tracker["ban_count"] = 1
            return True
        else:
            # Tái phạm → Ban vĩnh viễn
            apply_ban(user_id, "PERMANENT")
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
                
                # Check còn hiệu lực không
                if datetime.now() < ban_until:
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
            ban_until = datetime.now() + timedelta(seconds=BAN_DURATION_1H)
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
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

# =========================================================
# VOUCHER KEYBOARD
# =========================================================
def build_voucher_info_text():
    return (
        "🎁 <b>VOUCHER HIỆN CÓ</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "🟢 <b>Voucher đơn</b>\n"
        "• Mã 100k 0đ — 💰Giá 1.000 VNĐ\n"
        "• Mã 50% Max 200k — 💰Giá 1.000 VNĐ\n"
        "• Freeship Hỏa Tốc — 💰Giá 1.000 VNĐ\n\n"
        "🟣 <b>COMBO</b>\n"
        "• COMBO1: 100k/0đ + Freeship Hỏa Tốc\n"
        "  💰 2.000 VNĐ | 🎫 2 mã\n\n"
        "👇 <b>BẤM NÚT BÊN DƯỚI ĐỂ MUA</b>"
    )

def build_quick_voucher_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "💸 Mã 100k 0đ", "callback_data": "BUY:voucher100k"},
                {"text": "💸 Mã 50% Max 200k", "callback_data": "BUY:voucher50max200"},
            ],
            [
                {"text": "🚀 Freeship Hỏa Tốc", "callback_data": "BUY:voucherHoaToc"},
            ],
            [
                {"text": "🎁 COMBO1 | Mã 100k + Ship HT 🔥", "callback_data": "BUY:combo1"}
            ]
        ]
    }

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
def handle_callback_query(cb):
    cb_id = cb.get("id")
    data = cb.get("data", "")
    from_user = cb.get("from", {})
    user_id = from_user.get("id")

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
        tg_send(user_id, f"👉 Gửi <b>cookie</b> vào đây để lưu <b>{cmd}</b>")
        return

    tg_answer_callback(cb_id, "⚠️ Thao tác không hỗ trợ", True)

# =========================================================
# CORE UPDATE HANDLER
# =========================================================
def handle_update(update):
    dprint("UPDATE:", update)

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
                if track_error(user_id):
                    tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
        else:
            tg_send(chat_id, "👋 <b>Chào mừng quay lại!</b>", build_main_keyboard())
        return

    # ===== KÍCH HOẠT + TẶNG 5K =====
    if text == "🎁 Kích Hoạt Tặng 5k":
        ok, result = handle_active_gift_5k(user_id, username)

        if not ok:
            tg_send(chat_id, result)
            # ✅ Track lỗi
            if track_error(user_id):
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
    if text == "💳 Nạp tiền":
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
    if text in ("🎟️Lưu Voucher", "Voucher", "🎟️ Voucher"):
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
        cookie = text.strip()

        # ----- COMBO1 -----
        if cmd == COMBO1_KEY:
            ok, total_price, n_saved, n_total, failed = process_combo1(cookie)

            if not ok:
                tg_send(chat_id, f"❌ <b>COMBO1 THẤT BẠI</b>\n{total_price}")
                # ✅ Track lỗi
                if track_error(user_id):
                    tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
                return

            if balance < total_price:
                tg_send(chat_id, "❌ Không đủ số dư")
                # ✅ Track lỗi
                if track_error(user_id):
                    tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
                return

            new_bal = balance - total_price
            ws_money.update_cell(row, 3, new_bal)

            log_row(user_id, username, "COMBO1", str(total_price), f"{n_saved}/{n_total}")

            msg_text = (
                "✅ <b>COMBO1 THÀNH CÔNG</b>\n"
                f"🎫 Lưu: <b>{n_saved}/{n_total}</b>\n"
                f"💸 Trừ: <b>{total_price:,}đ</b>\n"
                f"💰 Còn: <b>{new_bal:,}đ</b>"
            )

            if failed:
                msg_text += "\n\n⚠️ Voucher lỗi:\n"
                for name, reason in failed:
                    msg_text += f"- {name}: {reason}\n"

            tg_send(chat_id, msg_text)
            tg_send(chat_id, "👉 <b>Bấm để lưu tiếp nhanh</b>", build_quick_buy_keyboard("combo1"))
            return

        # ----- VOUCHER ĐƠN -----
        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        price = int(v.get("Giá", 0))
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        ok, reason = save_voucher_and_check(cookie, v)
        if not ok:
            tg_send(chat_id, "❌ Lưu mã thất bại\n💸 Không trừ tiền")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "VOUCHER", str(price), cmd)

        tg_send(
            chat_id,
            f"✅ <b>Thành công</b>\n"
            f"💸 -{price:,}đ\n"
            f"💰 Còn: <b>{new_bal:,}đ</b>"
        )
        tg_send(chat_id, "👉 <b>Bấm để lưu tiếp nhanh</b>", build_quick_buy_keyboard(cmd))
        return

    # ===== LỆNH /voucherxxx <cookie> =====
    parts = text.split(maxsplit=1)
    cmd = parts[0].replace("/", "")
    cookie = parts[1] if len(parts) > 1 else ""

    # ----- COMBO1 -----
    if cmd == COMBO1_KEY:
        if not cookie:
            # ✅ Xóa lệnh cũ
            if user_id in PENDING_VOUCHER:
                dprint(f"Cleared old pending: {PENDING_VOUCHER[user_id]}")
            
            PENDING_VOUCHER[user_id] = COMBO1_KEY
            tg_send(chat_id, "👉 Gửi <b>cookie</b> để lưu combo1")
            return

        ok, total_price, n_saved, n_total, failed = process_combo1(cookie)

        if not ok:
            tg_send(chat_id, f"❌ COMBO1 THẤT BẠI\n{total_price}")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        if balance < total_price:
            tg_send(chat_id, "❌ Không đủ số dư")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        new_bal = balance - total_price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "COMBO1", str(total_price), f"{n_saved}/{n_total}")

        tg_send(
            chat_id,
            f"✅ <b>COMBO1 OK</b>\n"
            f"🎫 {n_saved}/{n_total}\n"
            f"💸 {total_price:,}đ\n"
            f"💰 {new_bal:,}đ",
            build_main_keyboard()
        )
        return

    # ----- VOUCHER ĐƠN -----
    if cmd.startswith("voucher"):
        if not cookie:
            # ✅ Xóa lệnh cũ
            if user_id in PENDING_VOUCHER:
                dprint(f"Cleared old pending: {PENDING_VOUCHER[user_id]}")
            
            PENDING_VOUCHER[user_id] = cmd
            tg_send(chat_id, f"👉 Gửi <b>cookie</b> để lưu {cmd}")
            return

        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        price = int(v.get("Giá", 0))
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        ok, reason = save_voucher_and_check(cookie, v)
        if not ok:
            tg_send(chat_id, "❌ Lưu mã thất bại\n💸 Không trừ tiền")
            # ✅ Track lỗi
            if track_error(user_id):
                tg_send(chat_id, "⛔ Tài khoản bị khóa do spam. Liên hệ @BonBonxHPx")
            return

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "VOUCHER", str(price), cmd)

        tg_send(
            chat_id,
            f"✅ <b>Thành công</b>\n"
            f"💸 -{price:,}đ\n"
            f"💰 Còn: <b>{new_bal:,}đ</b>",
            build_main_keyboard()
        )
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
    print(" NgânMiu.Store Telegram Bot - OPTIMIZED VERSION")
    print("=" * 60)
    print("ADMIN_ID:", ADMIN_ID)
    print("SHEET_READY:", SHEET_READY)
    print("=" * 60)

    app.run(host="127.0.0.1", port=5000, debug=False)
