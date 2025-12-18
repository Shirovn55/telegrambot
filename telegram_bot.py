# -*- coding: utf-8 -*-
"""
NgânMiu.Store — Telegram Bot (Voucher + Topup QR + Admin duyệt)
PHIÊN BẢN FULL FIX – PART 1
CORE + ENV + GOOGLE SHEET + TELEGRAM UTIL
"""

import os
import json
import re
import requests
from datetime import datetime
from flask import Flask, request

# =========================================================
# LOAD DOTENV (LOCAL SAFE)
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

QR_URL   = "https://img.vietqr.io/image/TPB-0819555000-compact.png"
SAVE_URL = "https://shopee.vn/api/v2/voucher_wallet/save_vouchers"

# =========================================================
# DEBUG FLAG
# =========================================================
DEBUG = True

def dprint(*args):
    if DEBUG:
        print("[DEBUG]", *args)

# =========================================================
# GOOGLE SHEET CONNECT
# =========================================================
SHEET_READY = False

ws_money   = None
ws_voucher = None
ws_log     = None

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

try:
    if not CREDS_JSON:
        raise Exception("CREDS_JSON is empty")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(CREDS_JSON), scope
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    ws_money   = sh.worksheet("Thanh Toan")
    ws_voucher = sh.worksheet("VoucherStock")
    ws_log     = sh.worksheet("Logs")

    SHEET_READY = True
    print("✅ Google Sheet connected")

except Exception as e:
    print("❌ Google Sheet ERROR:", e)
    SHEET_READY = False

# =========================================================
# STATE (GLOBAL)
# =========================================================
PENDING_VOUCHER = {}         # user_id -> cmd
PENDING_TOPUP   = {}         # user_id -> bill info
WAIT_TOPUP_AMOUNT = {}       # admin_id -> waiting amount
SEEN_BILL_UNIQUE_IDS = set()

COMBO1_KEY = "combo1"

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
        requests.post(
            f"{BASE_URL}/sendMessage",
            data=payload,
            timeout=15
        )
    except Exception as e:
        dprint("tg_send error:", e)

def tg_hide(chat_id, text):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"remove_keyboard": True})
    }
    try:
        requests.post(
            f"{BASE_URL}/sendMessage",
            data=payload,
            timeout=15
        )
    except Exception as e:
        dprint("tg_hide error:", e)

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
        requests.post(
            f"{BASE_URL}/sendPhoto",
            data=payload,
            timeout=20
        )
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
        requests.post(
            f"{BASE_URL}/answerCallbackQuery",
            data=payload,
            timeout=10
        )
    except Exception as e:
        dprint("tg_answer_callback error:", e)

# =========================================================
# KEYBOARD
# =========================================================
def build_main_keyboard():
    return {
        "keyboard": [
            ["📩 Gửi ID kích hoạt", "💳 Nạp tiền"],
            ["💰 Số dư", "🎟️Lưu Voucher"],
            ["📜 Lịch sử nạp tiền"]
        ],
        "resize_keyboard": True
    }

def build_topup_admin_kb(user_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ DUYỆT", "callback_data": f"TOPUP_OK:{user_id}"},
            {"text": "❌ TỪ CHỐI", "callback_data": f"TOPUP_NO:{user_id}"}
        ]]
    }

# =========================================================
# FILE / LOG UTIL
# =========================================================
def get_file_url(file_id):
    try:
        info = requests.get(
            f"{BASE_URL}/getFile",
            params={"file_id": file_id},
            timeout=10
        ).json()
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}"
    except Exception as e:
        dprint("get_file_url error:", e)
        return None

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_row(user_id, username, action, value="", note=""):
    if not SHEET_READY:
        return
    try:
        ws_log.append_row([
            now_str(),
            str(user_id),
            username,
            action,
            value,
            note
        ])
    except Exception as e:
        dprint("log_row error:", e)

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
            "pending",
            "auto from bot"
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
        username = data[1] if len(data) > 1 else ""
        return row, balance, status
    except Exception:
        return row, 0, ""

def add_balance(user_id, amount):
    if not SHEET_READY:
        return 0

    row = get_user_row(user_id)
    if not row:
        row = ensure_user_exists(user_id, "")

    try:
        bal = int(ws_money.cell(row, 3).value or 0)
        new_bal = bal + int(amount)
        ws_money.update_cell(row, 3, new_bal)
        return new_bal
    except Exception as e:
        dprint("add_balance error:", e)
        return 0
# =========================================================
# VOUCHER UTIL
# =========================================================

def get_voucher(cmd):
    """
    Lấy voucher đơn theo tên mã
    """
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
                return None, "Voucher đã hết"
            return r, None

    return None, "Không tìm thấy voucher"


def save_voucher_and_check(cookie, voucher):
    """
    Gửi request lưu voucher Shopee
    Trả về: (True/False, reason)
    """
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
        r = requests.post(
            SAVE_URL,
            headers=headers,
            json=payload,
            timeout=15
        )

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
    """
    Lấy danh sách voucher theo combo
    """
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
    """
    LOGIC A (ĐÃ FIX):
    - Voucher nào lưu OK => tính tiền voucher đó
    - Voucher lỗi => bỏ qua, không trừ tiền
    - Nếu không có voucher OK nào => FAIL
    """

    vouchers, err = get_vouchers_by_combo(COMBO1_KEY)
    if err:
        return False, err, 0, 0, []

    saved = []      # voucher lưu OK
    failed = []     # (Tên Mã, reason)

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
# VOUCHER LIST TEXT (SHOW CHO USER)
# =========================================================

def build_voucher_list_text():
    """
    Hiển thị danh sách voucher + combo
    """
    if not SHEET_READY:
        return "❌ Hệ thống Sheet đang lỗi"

    try:
        rows = ws_voucher.get_all_records()
    except Exception:
        return "❌ Không đọc được VoucherStock"

    out = ["🎁 <b>Voucher còn:</b>"]

    for r in rows:
        if r.get("Trạng Thái") == "Còn Mã":
            out.append(
                f"- /{r.get('Tên Mã')} | 💰 <b>Giá:</b> {r.get('Giá')} VNĐ"
            )

    combo_items, combo_err = get_vouchers_by_combo(COMBO1_KEY)
    if not combo_err:
        total_combo = 0
        for v in combo_items:
            try:
                total_combo += int(v.get("Giá", 0))
            except Exception:
                pass

        out.append("\n🎁 <b>COMBO1 : Mã 100k/0đ + Freeship Hỏa Tốc</b>")
        out.append(
            f"- /combo1 | 💰 <b>Giá:</b> {total_combo} VNĐ | 🎫 <b>{len(combo_items)}</b> mã"
        )

    out.append(
        "\n📝 <b>HƯỚNG DẪN</b>\n"
        "Cách 1️⃣: <code>/voucher100k &lt;cookie&gt;</code>\n"
        "Cách 2️⃣: Bấm <code>/voucher100k</code> → gửi cookie\n"
        "\n🎁 <b>COMBO1</b>\n"
        "Cách 1️⃣: <code>/combo1 &lt;cookie&gt;</code>\n"
        "Cách 2️⃣: Bấm <code>/combo1</code> → gửi cookie"
    )

    return "\n".join(out)
# =========================================================
# TOPUP HISTORY
# =========================================================

def topup_history_text(user_id):
    if not SHEET_READY:
        return "❌ Hệ thống Sheet đang lỗi"

    try:
        rows = ws_log.get_all_records()
    except Exception:
        return "❌ Không đọc được Logs"

    logs = []
    for r in rows:
        uid = str(r.get("user_id", ""))
        act = str(r.get("action", ""))
        if uid == str(user_id) and act in ("TOPUP", "TOPUP_CMD"):
            logs.append(r)

    logs = logs[-10:]
    if not logs:
        return "📜 <b>Lịch sử nạp tiền</b>\nChưa có giao dịch nào."

    out = ["📜 <b>Lịch sử nạp tiền (10 gần nhất)</b>"]
    for r in logs:
        out.append(
            f"- {r.get('time')} | +{r.get('value')} | {r.get('note')}"
        )
    return "\n".join(out)


# =========================================================
# CALLBACK QUERY HANDLER (ADMIN)
# =========================================================

def handle_callback_query(cb):
    cb_id = cb.get("id")
    admin_id = cb["from"]["id"]
    data = cb.get("data", "")

    if admin_id != ADMIN_ID:
        tg_answer_callback(cb_id, "Bạn không có quyền", True)
        return

    # ===== ADMIN DUYỆT NẠP =====
    if data.startswith("TOPUP_OK:"):
        uid = int(data.split(":")[1])

        info = PENDING_TOPUP.get(uid)
        if not info:
            tg_answer_callback(cb_id, "Yêu cầu không tồn tại", True)
            return

        WAIT_TOPUP_AMOUNT[ADMIN_ID] = {
            "user_id": uid,
            "file_unique_id": info.get("file_unique_id", "")
        }

        tg_answer_callback(cb_id, "Nhập số tiền để cộng", False)
        tg_send(
            ADMIN_ID,
            f"💰 Nhập số tiền cộng cho <code>{uid}</code>\nVD: <b>50000</b>"
        )
        return

    # ===== ADMIN TỪ CHỐI =====
    if data.startswith("TOPUP_NO:"):
        uid = int(data.split(":")[1])

        PENDING_TOPUP.pop(uid, None)
        tg_answer_callback(cb_id, "Đã từ chối", False)

        tg_send(
            uid,
            "❌ <b>Nạp tiền bị từ chối</b>\nVui lòng liên hệ admin."
        )
        log_row(uid, "", "TOPUP_REJECT", "", "Admin reject")
        return

    tg_answer_callback(cb_id, "Action không hỗ trợ", True)


# =========================================================
# NHẬN BILL (PHOTO / DOCUMENT)
# =========================================================

def handle_bill_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username", "")

    file_id = None
    file_unique_id = None

    if "photo" in msg:
        p = msg["photo"][-1]
        file_id = p.get("file_id")
        file_unique_id = p.get("file_unique_id")

    elif "document" in msg:
        doc = msg["document"]
        if doc.get("mime_type", "").startswith("image/"):
            file_id = doc.get("file_id")
            file_unique_id = doc.get("file_unique_id")

    if not file_id:
        return False

    if file_unique_id and file_unique_id in SEEN_BILL_UNIQUE_IDS:
        tg_send(
            chat_id,
            "⚠️ Bill này đã được xử lý trước đó."
        )
        return True

    ensure_user_exists(user_id, username)

    img_url = get_file_url(file_id)
    if not img_url:
        tg_send(chat_id, "❌ Không lấy được ảnh bill.")
        return True

    PENDING_TOPUP[user_id] = {
        "file_unique_id": file_unique_id,
        "img_url": img_url,
        "username": username
    }

    kb = build_topup_admin_kb(user_id)

    tg_send_photo(
        ADMIN_ID,
        img_url,
        caption=(
            "💳 <b>YÊU CẦU NẠP TIỀN</b>\n"
            f"👤 User: <code>{user_id}</code>\n"
            f"@{username}\n\n"
            "👉 Bấm <b>DUYỆT</b> để nhập số tiền."
        ),
        reply_markup=kb
    )

    tg_send(
        chat_id,
        "✅ Đã gửi bill cho admin duyệt."
    )
    log_row(user_id, username, "TOPUP_REQ", "", "Send bill")

    return True


# =========================================================
# ADMIN NHẬP SỐ TIỀN DUYỆT BILL
# =========================================================

def handle_admin_amount_input(admin_id, text):
    if admin_id not in WAIT_TOPUP_AMOUNT:
        return False

    try:
        amount = int(text)
    except ValueError:
        tg_send(admin_id, "❌ Số tiền không hợp lệ (vd: 50000)")
        return True

    pack = WAIT_TOPUP_AMOUNT.pop(admin_id)
    uid = int(pack["user_id"])
    fu  = pack.get("file_unique_id")

    if fu and fu in SEEN_BILL_UNIQUE_IDS:
        tg_send(admin_id, "⚠️ Bill này đã xử lý rồi.")
        return True

    ensure_user_exists(uid, "")
    new_bal = add_balance(uid, amount)

    if fu:
        SEEN_BILL_UNIQUE_IDS.add(fu)

    PENDING_TOPUP.pop(uid, None)

    log_row(uid, "", "TOPUP", str(amount), "Admin approve bill")

    tg_send(
        admin_id,
        f"✅ Đã cộng <b>{amount}</b> cho <code>{uid}</code>\n"
        f"Số dư mới: <b>{new_bal}</b>"
    )
    tg_send(
        uid,
        f"✅ <b>Nạp tiền thành công</b>\n"
        f"💰 +{amount}\n"
        f"💼 Số dư: <b>{new_bal}</b>"
    )

    return True


# =========================================================
# ADMIN COMMAND: +50000 123456
# =========================================================

def handle_admin_add_balance(user_id, text):
    if user_id != ADMIN_ID:
        return False

    if not text.startswith("+"):
        return False

    m = re.match(r"^\+(\d+)\s+(\d+)$", text)
    if not m:
        tg_send(
            user_id,
            "❌ Sai cú pháp\nDùng: <code>+50000 123456</code>"
        )
        return True

    amount = int(m.group(1))
    uid    = int(m.group(2))

    ensure_user_exists(uid, "")
    new_bal = add_balance(uid, amount)

    log_row(uid, "", "TOPUP_CMD", str(amount), "Admin cmd")

    tg_send(
        user_id,
        f"✅ Đã cộng <b>{amount}</b> cho <code>{uid}</code>\n"
        f"Số dư mới: <b>{new_bal}</b>"
    )
    tg_send(
        uid,
        f"✅ <b>Nạp tiền thành công</b>\n"
        f"💰 +{amount}\n"
        f"💼 Số dư: <b>{new_bal}</b>"
    )

    return True
# =========================================================
# CORE UPDATE HANDLER (FULL FIX)
# =========================================================

def handle_update(update):
    dprint("UPDATE:", update)

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

    # ===== 1. BILL (ẢNH) =====
    if handle_bill_message(msg):
        return

    # ===== 2. ADMIN +50000 UID =====
    if handle_admin_add_balance(user_id, text):
        return

    # ===== 3. ADMIN NHẬP TIỀN DUYỆT BILL =====
    if handle_admin_amount_input(user_id, text):
        return

    # ===== /start =====
    if text == "/start":
        ensure_user_exists(user_id, username)
        tg_send(
            chat_id,
            "👋 <b>Chào bạn!</b>\nChọn chức năng bên dưới 👇",
            build_main_keyboard()
        )
        return

    # ===== MENU: GỬI ID =====
    if text == "📩 Gửi ID kích hoạt":
        row = get_user_row(user_id)
        if row:
            tg_send(
                chat_id,
                f"🆔 ID của bạn: <b>{user_id}</b>\n"
                "⏳ Chờ admin kích hoạt."
            )
        else:
            ensure_user_exists(user_id, username)
            tg_send(
                chat_id,
                f"📩 Đã gửi ID!\n🆔 <b>{user_id}</b>\n"
                "Vui lòng liên hệ admin để nạp tiền."
            )
        return

    # ===== MENU: NẠP TIỀN =====
    if text == "💳 Nạp tiền":
        ensure_user_exists(user_id, username)
        tg_send_photo(
            chat_id,
            QR_URL,
            caption=(
                "💳 <b>NẠP TIỀN</b>\n\n"
                "📌 <b>NỘI DUNG CK</b>\n"
                f"<code>NAP {user_id}</code>\n\n"
                "📸 Gửi <b>ẢNH BILL</b> sau khi chuyển."
            )
        )
        return

    # ===== LẤY USER DATA =====
    row, balance, status = get_user_data(user_id)
    if not row:
        tg_send(chat_id, "❌ Bạn chưa có ID. Bấm 📩 Gửi ID kích hoạt.")
        return

    # ===== MENU: SỐ DƯ =====
    if text in ("💰 Số dư", "/balance"):
        tg_send(
            chat_id,
            f"💰 <b>Số dư:</b> <b>{balance}</b>\n"
            f"📌 Trạng thái: <b>{status}</b>"
        )
        return

    # ===== MENU: LỊCH SỬ =====
    if text in ("📜 Lịch sử nạp tiền", "/topup_history"):
        tg_send(chat_id, topup_history_text(user_id))
        return

    # ===== MENU: XEM VOUCHER (KHÔNG CHẶN ACTIVE) =====
    if text in ("🎟️Lưu Voucher", "Voucher", "🎟️ Voucher"):
        tg_send(chat_id, build_voucher_list_text())
        return

    # =====================================================
    # ===== CHẶN LƯU NẾU CHƯA ACTIVE =====
    # =====================================================
    if status != "active" and (
        text.startswith("/voucher")
        or text.startswith("/combo")
        or user_id in PENDING_VOUCHER
    ):
        tg_send(chat_id, "❌ Tài khoản chưa được kích hoạt.")
        return

    # =====================================================
    # ===== CÁCH 2: ĐANG CHỜ COOKIE =====
    # =====================================================
    if user_id in PENDING_VOUCHER and not text.startswith("/"):
        cmd = PENDING_VOUCHER.pop(user_id)
        cookie = text.strip()

        # ----- COMBO1 -----
        if cmd == COMBO1_KEY:
            ok, total_price, n_saved, n_total, failed = process_combo1(cookie)

            if not ok:
                tg_send(chat_id, f"❌ <b>COMBO1 THẤT BẠI</b>\n{total_price}")
                return

            if balance < total_price:
                tg_send(chat_id, "❌ Không đủ số dư")
                return

            new_bal = balance - total_price
            ws_money.update_cell(row, 3, new_bal)

            log_row(
                user_id,
                username,
                "COMBO1",
                str(total_price),
                f"{n_saved}/{n_total}"
            )

            msg = (
                "✅ <b>COMBO1 THÀNH CÔNG</b>\n"
                f"🎫 Lưu: <b>{n_saved}/{n_total}</b>\n"
                f"💸 Trừ: <b>{total_price}</b>\n"
                f"💰 Còn: <b>{new_bal}</b>"
            )

            if failed:
                msg += "\n\n⚠️ Voucher lỗi:\n"
                for name, reason in failed:
                    msg += f"- {name}: {reason}\n"

            tg_send(chat_id, msg, build_main_keyboard())
            return

        # ----- VOUCHER ĐƠN -----
        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            return

        price = int(v.get("Giá", 0))
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            return

        ok, reason = save_voucher_and_check(cookie, v)
        if not ok:
            tg_send(chat_id, "❌ Lưu mã thất bại\n💸 Không trừ tiền")
            return

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "VOUCHER", str(price), cmd)

        tg_send(
            chat_id,
            f"✅ <b>Thành công</b>\n"
            f"💸 -{price}\n"
            f"💰 Còn: <b>{new_bal}</b>",
            build_main_keyboard()
        )
        return

    # =====================================================
    # ===== CÁCH 1: /voucherxxx <cookie> | /combo1 <cookie>
    # =====================================================
    parts = text.split(maxsplit=1)
    cmd = parts[0].replace("/", "")
    cookie = parts[1] if len(parts) > 1 else ""

    # ----- COMBO1 -----
    if cmd == COMBO1_KEY:
        if not cookie:
            PENDING_VOUCHER[user_id] = COMBO1_KEY
            tg_send(chat_id, "👉 Gửi <b>cookie</b> để lưu combo1")
            return

        ok, total_price, n_saved, n_total, failed = process_combo1(cookie)

        if not ok:
            tg_send(chat_id, f"❌ COMBO1 THẤT BẠI\n{total_price}")
            return

        if balance < total_price:
            tg_send(chat_id, "❌ Không đủ số dư")
            return

        new_bal = balance - total_price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "COMBO1", str(total_price), f"{n_saved}/{n_total}")

        tg_send(
            chat_id,
            f"✅ <b>COMBO1 OK</b>\n"
            f"🎫 {n_saved}/{n_total}\n"
            f"💸 {total_price}\n"
            f"💰 {new_bal}",
            build_main_keyboard()
        )
        return

    # ----- VOUCHER ĐƠN -----
    if cmd.startswith("voucher"):
        if not cookie:
            PENDING_VOUCHER[user_id] = cmd
            tg_send(chat_id, f"👉 Gửi <b>cookie</b> để lưu {cmd}")
            return

        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            return

        price = int(v.get("Giá", 0))
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            return

        ok, reason = save_voucher_and_check(cookie, v)
        if not ok:
            tg_send(chat_id, "❌ Lưu mã thất bại\n💸 Không trừ tiền")
            return

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)

        log_row(user_id, username, "VOUCHER", str(price), cmd)

        tg_send(
            chat_id,
            f"✅ <b>Thành công</b>\n"
            f"💸 -{price}\n"
            f"💰 Còn: <b>{new_bal}</b>",
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
# WEBHOOK ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    if not SHEET_READY:
        return "Bot running, Sheet ERROR", 500
    return "Bot is running", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    handle_update(update)
    return "ok"


# =========================================================
# LOCAL RUNNER
# =========================================================
if __name__ == "__main__":
    print("====================================")
    print(" NgânMiu.Store Telegram Bot (FULL)")
    print("====================================")
    print("ADMIN_ID:", ADMIN_ID)
    print("SHEET_READY:", SHEET_READY)

    app.run(host="127.0.0.1", port=5000, debug=False)
