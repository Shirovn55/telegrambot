# -*- coding: utf-8 -*-
"""
NgânMiu.Store — Telegram Bot (Voucher + Topup QR + Admin duyệt)
WEBHOOK VERSION – ĐÃ CẬP NHẬT LOGIC TỪ BẢN LOCAL (BAO GỒM COMBO1)
Sẵn sàng deploy Vercel
"""

import os
import json
import re
import requests
from datetime import datetime
from flask import Flask, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# =========================================================
# APP (CHO WEBHOOK)
# =========================================================
app = Flask(__name__)

# =========================================================
# LOAD ENV + CONST
# =========================================================
# Không cần load_dotenv() ở môi trường Production (Vercel)
BOT_TOKEN   = os.getenv("TELEGRAM_TOKEN")
SHEET_ID    = os.getenv("GOOGLE_SHEET_ID")
CREDS_JSON  = os.getenv("GOOGLE_SHEETS_CREDS_JSON")
ADMIN_ID    = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# QR của bạn (TPBank)
QR_URL   = "https://img.vietqr.io/image/TPB-0819555000-compact.png"
# Shopee save voucher API
SAVE_URL = "https://shopee.vn/api/v2/voucher_wallet/save_vouchers"

# =========================================================
# GOOGLE SHEET
# =========================================================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    json.loads(CREDS_JSON), scope
)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)

ws_money   = sh.worksheet("Thanh Toan")
ws_voucher = sh.worksheet("VoucherStock")
ws_log     = sh.worksheet("Logs")

# =========================================================
# STATE (ĐƯỢC GIỮ NGUYÊN)
# =========================================================
PENDING_VOUCHER = {}         # user_id -> cmd (đang chờ cookie)
PENDING_TOPUP = {}           # user_id -> {"file_unique_id":..., "img_url":..., "username":...}
WAIT_TOPUP_AMOUNT = {}       # admin_id -> {"user_id":..., "file_unique_id":...} (admin đang nhập số tiền)
SEEN_BILL_UNIQUE_IDS = set() # anti-duplicate bill

COMBO1_KEY = "combo1"

# =========================================================
# TELEGRAM UTIL (CẬP NHẬT: Thêm build_*)
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
        requests.post(f"{BASE_URL}/sendMessage", data=payload, timeout=20)
    except:
        pass

def tg_hide(chat_id, text):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"remove_keyboard": True})
    }
    try:
        requests.post(f"{BASE_URL}/sendMessage", data=payload, timeout=20)
    except:
        pass

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
        requests.post(f"{BASE_URL}/sendPhoto", data=payload, timeout=25)
    except:
        pass

def tg_answer_callback(callback_id, text=None, show_alert=False):
    payload = {
        "callback_query_id": callback_id,
        "show_alert": show_alert
    }
    if text:
        payload["text"] = text
    try:
        requests.post(f"{BASE_URL}/answerCallbackQuery", data=payload, timeout=15)
    except:
        pass

def build_main_keyboard():
    return {
        "keyboard": [
            ["📩 Gửi ID kích hoạt", "💳 Nạp tiền"],
            ["/balance", "/voucherlist", "/topup_history"]
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

def get_file_url(file_id):
    # Dùng API getFile để lấy file_path, sau đó build URL
    info = requests.get(f"{BASE_URL}/getFile", params={"file_id": file_id}, timeout=20).json()
    file_path = info["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

# =========================================================
# LOG UTIL (ĐƯỢC GIỮ NGUYÊN)
# =========================================================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_row(user_id, username, action, value="", note=""):
    try:
        ws_log.append_row([now_str(), str(user_id), username, action, value, note])
    except:
        pass

# =========================================================
# USER / MONEY UTIL (ĐƯỢC GIỮ NGUYÊN)
# =========================================================
def get_user_row(user_id):
    ids = ws_money.col_values(1)
    return ids.index(str(user_id)) + 1 if str(user_id) in ids else None

def ensure_user_exists(user_id, username):
    row = get_user_row(user_id)
    if row:
        return row
    try:
        ws_money.append_row([str(user_id), username, 0, "pending", "auto từ bot"])
    except:
        pass
    return get_user_row(user_id)

def get_user_data(user_id):
    row = get_user_row(user_id)
    if not row:
        return None, None, None
    data = ws_money.row_values(row)
    balance = int(data[2]) if len(data) > 2 and str(data[2]).isdigit() else 0
    status  = data[3] if len(data) > 3 else ""
    username = data[1] if len(data) > 1 else ""
    return row, balance, status

def add_balance(user_id, amount):
    row = get_user_row(user_id)
    if not row:
        row = ensure_user_exists(user_id, "")
    bal = int(ws_money.cell(row, 3).value or 0)
    new_bal = bal + int(amount)
    ws_money.update_cell(row, 3, new_bal)
    return new_bal

# =========================================================
# VOUCHER UTIL (ĐƯỢC GIỮ NGUYÊN)
# =========================================================
def get_voucher(cmd):
    rows = ws_voucher.get_all_records()
    for r in rows:
        name = str(r.get("Tên Mã", "")).replace(" ", "").lower()
        if name == cmd.lower():
            if r.get("Trạng Thái") != "Còn Mã":
                return None, "Voucher đã hết"
            return r, None
    return None, "Không tìm thấy voucher"

def save_voucher_and_check(cookie, voucher):
    payload = {
        "voucher_identifiers": [{
            "promotion_id": int(voucher["Promotionid"]),
            "voucher_code": voucher["CODE"],
            "signature": voucher["Signature"],
            "signature_source": 0
        }],
        "need_user_voucher_status": True
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://autopee.vercel.app",
        "Referer": "https://autopee.vercel.app/",
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
        if resp.get("error") != 0:
            return False, f"SHOPEE_{resp.get('error')}"

        voucher_data = resp.get("data", {}).get("voucher", {})
        if voucher_data.get("collect_time"):
            return True, "OK"

        return False, "NOT_COLLECTED"

    except requests.exceptions.Timeout:
        return False, "TIMEOUT"
    except Exception as e:
        return False, f"EXCEPTION_{e}"

# =========================================================
# COMBO UTIL (ĐÃ CẬP NHẬT TỪ telegram_bot.py)
# =========================================================
def get_vouchers_by_combo(combo_key):
    """
    Lấy các voucher theo cột Combo trong sheet VoucherStock.
    Chỉ lấy Trạng Thái == 'Còn Mã'
    """
    try:
        rows = ws_voucher.get_all_records()
    except:
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
    Lưu toàn bộ voucher có Combo=combo1.
    Return: (True, total_price, n) hoặc (False, reason, n_saved)
    """
    vouchers, err = get_vouchers_by_combo(COMBO1_KEY)
    if err:
        return False, err, 0

    # tổng giá
    total_price = 0
    for v in vouchers:
        try:
            total_price += int(v.get("Giá", 0))
        except:
            pass

    n_saved = 0
    for v in vouchers:
        ok, reason = save_voucher_and_check(cookie, v)
        if not ok:
            # fail giữa chừng -> báo lỗi, KHÔNG trừ tiền (caller sẽ xử lý)
            return False, f"Lỗi lưu {v.get('Tên Mã')} ({reason})", n_saved
        n_saved += 1

    return True, total_price, n_saved

# =========================================================
# TOPUP HISTORY (ĐƯỢC GIỮ NGUYÊN)
# =========================================================
def topup_history_text(user_id):
    try:
        rows = ws_log.get_all_records()
    except:
        return "❌ Không đọc được Logs"

    logs = []
    for r in rows:
        uid = str(r.get("user_id", ""))
        act = str(r.get("action", ""))
        if uid == str(user_id) and (act == "TOPUP" or act == "TOPUP_CMD"):
            logs.append(r)

    logs = logs[-10:]
    if not logs:
        return "📜 <b>Lịch sử nạp tiền</b>\nChưa có giao dịch nào."

    out = ["📜 <b>Lịch sử nạp tiền (10 gần nhất)</b>"]
    for r in logs:
        t = r.get("time", "")
        v = r.get("value", "")
        note = r.get("note", "")
        out.append(f"- {t} | +{v} | {note}")
    return "\n".join(out)

# =========================================================
# CORE HANDLER — HỢP NHẤT LOGIC TỪ telegram_bot.py
# =========================================================
def handle_update(upd):

    # =================================================
    #                 CALLBACK QUERY (ADMIN)
    # =================================================
    if "callback_query" in upd:
        cb = upd["callback_query"]
        cb_id = cb.get("id")
        admin_id = cb["from"]["id"]
        data = cb.get("data", "")

        # chỉ admin xử lý
        if admin_id != ADMIN_ID:
            tg_answer_callback(cb_id, "Bạn không có quyền.", True)
            return

        # TOPUP_OK:<uid>
        if data.startswith("TOPUP_OK:"):
            uid = int(data.split(":")[1])
            info = PENDING_TOPUP.get(uid)
            if not info:
                tg_answer_callback(cb_id, "Yêu cầu không tồn tại / đã xử lý.", True)
                return

            WAIT_TOPUP_AMOUNT[ADMIN_ID] = {
                "user_id": uid,
                "file_unique_id": info.get("file_unique_id", "")
            }
            tg_answer_callback(cb_id, "OK, nhập số tiền để cộng.", False)
            tg_send(
                ADMIN_ID,
                f"💰 Nhập số tiền cộng cho <code>{uid}</code>\nVí dụ: <b>50000</b>"
            )
            return

        # TOPUP_NO:<uid>
        if data.startswith("TOPUP_NO:"):
            uid = int(data.split(":")[1])
            PENDING_TOPUP.pop(uid, None)
            tg_answer_callback(cb_id, "Đã từ chối.", False)

            tg_send(
                uid,
                "❌ <b>Nạp tiền bị từ chối</b>\nVui lòng liên hệ admin để kiểm tra."
            )
            log_row(uid, "", "TOPUP_REJECT", "", "Admin reject")
            return

        tg_answer_callback(cb_id, "Không hỗ trợ action này.", True)
        return

    # =================================================
    #                      MESSAGE
    # =================================================
    msg = upd.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username", "")
    text = (msg.get("text") or "").strip()
    
    # Lấy file_id và file_unique_id cho cả photo và document (ảnh)
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
            
    # =================================================
    #           NHẬN ẢNH BILL (PHOTO / DOCUMENT)
    # =================================================
    if file_id:
        if file_unique_id and file_unique_id in SEEN_BILL_UNIQUE_IDS:
            tg_send(
                chat_id,
                "⚠️ Bill này đã gửi/đã xử lý trước đó. Nếu cần, liên hệ admin."
            )
            return

        ensure_user_exists(user_id, username)

        try:
            # Dùng hàm get_file_url đã định nghĩa
            img_url = get_file_url(file_id)
        except:
            tg_send(chat_id, "❌ Không lấy được ảnh bill, thử gửi lại.")
            return

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
                "👉 Bấm <b>DUYỆT</b> để nhập số tiền cộng."
            ),
            reply_markup=kb
        )

        tg_send(
            chat_id,
            "✅ Đã gửi bill cho admin duyệt. Vui lòng chờ."
        )
        log_row(user_id, username, "TOPUP_REQ", "", "Send bill to admin")
        return

    # =================================================
    #          ADMIN: +50000 1999478799
    # =================================================
    if user_id == ADMIN_ID and text.startswith("+"):
        m = re.match(r"^\+(\d+)\s+(\d+)$", text)
        if m:
            amt = int(m.group(1))
            uid = int(m.group(2))

            ensure_user_exists(uid, "")
            new_bal = add_balance(uid, amt)

            log_row(uid, "", "TOPUP_CMD", str(amt), "Admin cmd")
            tg_send(
                ADMIN_ID,
                f"✅ Đã cộng <b>{amt}</b> cho <code>{uid}</code>\n"
                f"Số dư mới: <b>{new_bal}</b>"
            )
            tg_send(
                uid,
                f"✅ <b>Nạp tiền thành công</b>\n"
                f"💰 +{amt}\n"
                f"💼 Số dư: <b>{new_bal}</b>"
            )
        else:
            tg_send(
                ADMIN_ID,
                "❌ Sai cú pháp. Dùng: <code>+50000 1999478799</code>"
            )
        return

    # =================================================
    #     ADMIN: đang chờ nhập số tiền duyệt bill
    # =================================================
    if user_id == ADMIN_ID and user_id in WAIT_TOPUP_AMOUNT:
        try:
            amt = int(text)
            pack = WAIT_TOPUP_AMOUNT.pop(user_id)
            uid = int(pack["user_id"])
            fu = pack.get("file_unique_id", "")

            if fu and fu in SEEN_BILL_UNIQUE_IDS:
                tg_send(ADMIN_ID, "⚠️ Bill này đã xử lý trước đó.")
                return

            ensure_user_exists(uid, "")
            new_bal = add_balance(uid, amt)

            if fu:
                SEEN_BILL_UNIQUE_IDS.add(fu)

            PENDING_TOPUP.pop(uid, None)

            log_row(uid, "", "TOPUP", str(amt), "Admin approve bill")
            tg_send(
                ADMIN_ID,
                f"✅ Duyệt nạp tiền OK\n"
                f"User: <code>{uid}</code>\n"
                f"+{amt}\n"
                f"Số dư: <b>{new_bal}</b>"
            )
            tg_send(
                uid,
                f"✅ <b>Nạp tiền thành công</b>\n"
                f"💰 +{amt}\n"
                f"💼 Số dư: <b>{new_bal}</b>"
            )
        except:
            tg_send(ADMIN_ID, "❌ Số tiền không hợp lệ. Nhập lại (vd: 50000).")
        return

    # =================================================
    #                    /start
    # =================================================
    if text == "/start":
        tg_send(
            chat_id,
            "👋 Chào bạn!\nChọn chức năng bên dưới 👇",
            build_main_keyboard()
        )
        return

    # =================================================
    #            GỬI ID KÍCH HOẠT
    # =================================================
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
                f"📩 Đã gửi ID!\n🆔 ID: <b>{user_id}</b>\n"
                "Vui lòng nhắn admin để nạp tiền."
            )
        return

    # =================================================
    #                    NẠP TIỀN
    # =================================================
    if text == "💳 Nạp tiền":
        ensure_user_exists(user_id, username)
        tg_send_photo(
            chat_id,
            QR_URL,
            caption=(
                "💳 <b>NẠP TIỀN</b>\n\n"
                "✅ Quét QR để chuyển khoản\n"
                "📌 <b>NỘI DUNG CHUYỂN KHOẢN (BẮT BUỘC)</b>\n"
                f"<code>NAP {user_id}</code>\n\n"
                "📸 Chuyển xong, gửi <b>ẢNH BILL</b> vào đây để admin duyệt."
            )
        )
        return

    # =================================================
    #        TỪ ĐÂY: CÁC LỆNH CẦN ACTIVE
    # =================================================
    row, balance, status = get_user_data(user_id)
    if not row:
        tg_send(chat_id, "❌ Bạn chưa có ID. Bấm 📩 Gửi ID kích hoạt.")
        return

    if status != "active":
        if text == "/topup_history":
            tg_send(chat_id, topup_history_text(user_id))
            return

        if text == "/balance":
            tg_send(
                chat_id,
                f"💰 Số dư: <b>{balance}</b>\n(Chưa active)"
            )
            return

        tg_send(chat_id, "❌ Tài khoản chưa được kích hoạt")
        return

    # =================================================
    #                    /balance
    # =================================================
    if text == "/balance":
        tg_send(chat_id, f"💰 Số dư: <b>{balance}</b>")
        return

    # =================================================
    #                    /topup_history
    # =================================================
    if text == "/topup_history":
        tg_send(chat_id, topup_history_text(user_id))
        return

    # =================================================
    #                    /voucherlist (CẬP NHẬT COMBO1)
    # =================================================
    if text == "/voucherlist":
        rows = ws_voucher.get_all_records()
        out = ["📦 <b>Voucher còn:</b>"]
        for r in rows:
            if r.get("Trạng Thái") == "Còn Mã":
                out.append(f"- /{r.get('Tên Mã')} | {r.get('Giá')}")

        combo_items, combo_err = get_vouchers_by_combo(COMBO1_KEY)
        if not combo_err:
            total_combo = sum(int(v.get("Giá", 0)) for v in combo_items)
            out.append("\n🎁 <b>COMBO:</b>")
            out.append(f"- /combo1 | {total_combo} | {len(combo_items)} mã")
            
        out.append(
            "\n📝 <b>HƯỚNG DẪN</b>\n"
            "Cách 1️⃣: <code>/voucherxxx &lt;cookie&gt;</code>\n"
            "Cách 2️⃣: Bấm <code>/voucherxxx</code> → gửi cookie\n"
            "\n🎁 <b>COMBO1</b>\n"
            "Cách 1️⃣: <code>/combo1 &lt;cookie&gt;</code>\n"
            "Cách 2️⃣: Bấm <code>/combo1</code> → gửi cookie"
        )

        tg_send(chat_id, "\n".join(out))
        return

    # =================================================
    #   CÁCH 2: bấm /voucherxxx hoặc /combo1 rồi gửi cookie (ĐÃ CẬP NHẬT COMBO1)
    # =================================================
    if user_id in PENDING_VOUCHER and not text.startswith("/"):
        cmd = PENDING_VOUCHER.pop(user_id)
        cookie = text.strip()

        # Xử lý Combo1
        if cmd == COMBO1_KEY:
            combo_items, combo_err = get_vouchers_by_combo(COMBO1_KEY)
            if combo_err:
                tg_send(chat_id, f"❌ {combo_err}")
                return

            total_price = sum(int(v.get("Giá", 0)) for v in combo_items)

            if balance < total_price:
                tg_send(chat_id, "❌ Không đủ số dư cho combo1")
                return

            ok, reason_or_total, n_saved = process_combo1(cookie)
            if not ok:
                tg_send(chat_id, f"❌ <b>Combo1 thất bại</b>\n{reason_or_total}\n💸 Không trừ tiền")
                log_row(user_id, username, "COMBO_FAIL", "combo1", str(reason_or_total))
                return

            new_bal = balance - total_price
            ws_money.update_cell(row, 3, new_bal)
            log_row(user_id, username, "COMBO", str(total_price), f"combo1 -> {new_bal}")

            tg_hide(
                chat_id,
                "✅ <b>COMBO1 THÀNH CÔNG!</b>\n"
                f"🎁 Đã lưu: <b>{n_saved}</b> mã\n"
                f"💸 Đã trừ: <b>{total_price}</b>\n"
                f"💰 Số dư còn lại: <b>{new_bal}</b>"
            )
            return

        # Xử lý Voucher đơn
        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            return

        price = int(v["Giá"])
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            return

        ok, reason = save_voucher_and_check(cookie, v)
        if not ok:
            tg_send(chat_id, "❌ Lưu mã thất bại\n💸 Không trừ tiền")
            log_row(user_id, username, "FAIL", cmd, reason)
            return

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)
        log_row(user_id, username, "VOUCHER", str(price), f"{cmd} -> {new_bal}")

        tg_hide(
            chat_id,
            f"✅ <b>Thành công!</b>\n"
            f"💸 Đã trừ: <b>{price}</b>\n"
            f"💰 Số dư còn lại: <b>{new_bal}</b>"
        )
        return

    # =================================================
    #   CÁCH 1: gõ /voucherxxx <cookie> hoặc /combo1 <cookie> (ĐÃ CẬP NHẬT COMBO1)
    # =================================================
    parts = text.split(maxsplit=1)
    cmd = parts[0].replace("/", "")
    cookie = parts[1] if len(parts) > 1 else ""

    # Xử lý Combo1
    if cmd == COMBO1_KEY:
        if not cookie:
            PENDING_VOUCHER[user_id] = COMBO1_KEY
            tg_send(chat_id, "👉 Gửi <b>cookie</b> để lưu <b>combo1</b>")
            return

        combo_items, combo_err = get_vouchers_by_combo(COMBO1_KEY)
        if combo_err:
            tg_send(chat_id, f"❌ {combo_err}")
            return

        total_price = sum(int(v.get("Giá", 0)) for v in combo_items)

        if balance < total_price:
            tg_send(chat_id, "❌ Không đủ số dư cho combo1")
            return

        ok, reason_or_total, n_saved = process_combo1(cookie)
        if not ok:
            tg_send(chat_id, f"❌ <b>Combo1 thất bại</b>\n{reason_or_total}\n💸 Không trừ tiền")
            log_row(user_id, username, "COMBO_FAIL", "combo1", str(reason_or_total))
            return

        new_bal = balance - total_price
        ws_money.update_cell(row, 3, new_bal)
        log_row(user_id, username, "COMBO", str(total_price), f"combo1 -> {new_bal}")

        tg_hide(
            chat_id,
            "✅ <b>COMBO1 THÀNH CÔNG!</b>\n"
            f"🎁 Đã lưu: <b>{n_saved}</b> mã\n"
            f"💸 Đã trừ: <b>{total_price}</b>\n"
            f"💰 Số dư còn lại: <b>{new_bal}</b>"
        )
        return

    # Xử lý Voucher đơn
    if cmd.startswith("voucher"):
        if not cookie:
            PENDING_VOUCHER[user_id] = cmd
            tg_send(chat_id, f"👉 Gửi <b>cookie</b> để lưu mã:\n<b>{cmd}</b>")
            return

        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            return

        price = int(v["Giá"])
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            return

        ok, reason = save_voucher_and_check(cookie, v)
        if not ok:
            tg_send(chat_id, "❌ Lưu mã thất bại\n💸 Không trừ tiền")
            log_row(user_id, username, "FAIL", cmd, reason)
            return

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)
        log_row(user_id, username, "VOUCHER", str(price), f"{cmd} -> {new_bal}")

        tg_hide(
            chat_id,
            f"✅ <b>Thành công!</b>\n"
            f"💸 Đã trừ: <b>{price}</b>\n"
            f"💰 Số dư còn lại: <b>{new_bal}</b>"
        )
        return

    # =================================================
    # FALLBACK
    # =================================================
    tg_send(
        chat_id,
        "❌ <b>Lệnh không hợp lệ</b>\n"
        "Dùng /start để xem menu."
    )


# =========================================================
# WEBHOOK ROUTE (GIỮ NGUYÊN)
# =========================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    handle_update(update)
    return "ok"
