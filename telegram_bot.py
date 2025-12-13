# -*- coding: utf-8 -*-
import os, json, requests
from datetime import datetime
from flask import Flask, request, jsonify
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= ENV =================
BOT_TOKEN   = os.getenv("TELEGRAM_TOKEN")
SHEET_ID   = os.getenv("GOOGLE_SHEET_ID")
CREDS_JSON = os.getenv("GOOGLE_SHEETS_CREDS_JSON")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ================= APP =================
app = Flask(__name__)

# ================= GOOGLE SHEET =================
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

# ================= STATE =================
PENDING_VOUCHER = {}

# ================= TELEGRAM =================
def tg_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    requests.post(f"{BASE_URL}/sendMessage", data=payload, timeout=10)

def tg_hide(chat_id, text):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"remove_keyboard": True})
    }
    requests.post(f"{BASE_URL}/sendMessage", data=payload, timeout=10)

# ================= USER =================
def get_user_row(user_id):
    ids = ws_money.col_values(1)
    return ids.index(str(user_id)) + 1 if str(user_id) in ids else None

# ================= VOUCHER =================
def get_voucher(cmd):
    rows = ws_voucher.get_all_records()
    for r in rows:
        if r["Tên Mã"].replace(" ", "").lower() == cmd.lower():
            if r["Trạng Thái"] != "Còn Mã":
                return None, "Voucher đã hết"
            return r, None
    return None, "Không tìm thấy voucher"

# ================= SHOPEE API =================
SAVE_URL = "https://shopee.vn/api/v2/voucher_wallet/save_vouchers"

def save_voucher_and_check(cookie, voucher):
    """
    True  -> lưu MỚI thành công (có collect_time)
    False -> lưu trùng / không đủ điều kiện / lỗi
    """
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
        "Origin": "https://telegrambot-one-rho.vercel.app",
        "Referer": "https://telegrambot-one-rho.vercel.app/",
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

# ================= ROUTES =================
@app.route("/", methods=["GET"])
def home():
    return "🤖 Telegram Bot is running (Webhook FULL LOGIC)"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data or "message" not in data:
        return jsonify(ok=True)

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    username = msg["from"].get("username", "")
    text = msg.get("text", "").strip()

    # ===== START =====
    if text == "/start":
        kb = {
            "keyboard": [
                ["📩 Gửi ID kích hoạt"],
                ["/balance", "/voucherlist"]
            ],
            "resize_keyboard": True
        }
        tg_send(chat_id, "👋 Chào bạn!\nBấm nút dưới để gửi ID kích hoạt.", kb)
        return jsonify(ok=True)

    # ===== SEND ID =====
    if text == "📩 Gửi ID kích hoạt":
        if get_user_row(user_id):
            tg_send(chat_id, f"🆔 ID của bạn: <b>{user_id}</b>\n⏳ Chờ admin kích hoạt.")
        else:
            ws_money.append_row([str(user_id), username, 0, "pending", "auto từ bot"])
            tg_send(
                chat_id,
                f"📩 Đã gửi ID!\n🆔 ID: <b>{user_id}</b>\n"
                "Vui lòng nhắn tin ADMIN để nạp tiền."
            )
        return jsonify(ok=True)

    # ===== CHECK ACTIVE =====
    row = get_user_row(user_id)
    if not row:
        tg_send(chat_id, "❌ Chưa kích hoạt")
        return jsonify(ok=True)

    data_row = ws_money.row_values(row)
    balance = int(data_row[2])
    status  = data_row[3]

    if status != "active":
        tg_send(chat_id, "❌ Tài khoản chưa được kích hoạt")
        return jsonify(ok=True)

    # ===== BALANCE =====
    if text == "/balance":
        tg_send(chat_id, f"💰 Số dư: <b>{balance}</b>")
        return jsonify(ok=True)

    # ===== LIST =====
    if text == "/voucherlist":
        rows = ws_voucher.get_all_records()
        out = ["📦 <b>Voucher còn:</b>"]
        for r in rows:
            if r["Trạng Thái"] == "Còn Mã":
                out.append(f"- /{r['Tên Mã']} | {r['Giá']}")
        out.append(
            "\n📝 <b>HƯỚNG DẪN</b>\n"
            "💰 <b>Giá:</b> 1000đ / 1 lượt lưu\n"
            "Cách 1️⃣: <code>/voucherxxx &lt;cookie&gt;</code>\n"
            "Cách 2️⃣: Bấm <code>/voucherxxx</code> → gửi cookie"
        )
        tg_send(chat_id, "\n".join(out))
        return jsonify(ok=True)

    # ===== CÁCH 2: ĐANG CHỜ COOKIE =====
    if user_id in PENDING_VOUCHER and not text.startswith("/"):
        cmd = PENDING_VOUCHER.pop(user_id)
        cookie = text.strip()

        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            return jsonify(ok=True)

        price = int(v["Giá"])
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            return jsonify(ok=True)

        ok, reason = save_voucher_and_check(cookie, v)

        if not ok:
            tg_send(chat_id, "❌ <b>Lưu mã thất bại</b>\n💸 Không trừ tiền")
            ws_log.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(user_id), username, cmd, "FAIL", reason
            ])
            return jsonify(ok=True)

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)
        ws_log.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            str(user_id), username, cmd, price, new_bal
        ])

        tg_hide(
            chat_id,
            "✅ <b>Thành công!</b>\n"
            f"💸 Đã trừ: <b>{price}</b>\n"
            f"💰 Số dư còn lại: <b>{new_bal}</b>"
        )
        return jsonify(ok=True)

    # ===== CÁCH 1: GÕ /voucher + cookie =====
    parts = text.split(maxsplit=1)
    cmd = parts[0].replace("/", "")
    cookie = parts[1] if len(parts) > 1 else ""

    if cmd.startswith("voucher"):
        if not cookie:
            PENDING_VOUCHER[user_id] = cmd
            tg_send(chat_id, f"👉 Gửi <b>cookie</b> để lưu mã:\n<b>{cmd}</b>")
            return jsonify(ok=True)

        v, err = get_voucher(cmd)
        if err:
            tg_send(chat_id, f"❌ {err}")
            return jsonify(ok=True)

        price = int(v["Giá"])
        if balance < price:
            tg_send(chat_id, "❌ Không đủ số dư")
            return jsonify(ok=True)

        ok, reason = save_voucher_and_check(cookie, v)

        if not ok:
            tg_send(chat_id, "❌ <b>Lưu mã thất bại</b>\n💸 Không trừ tiền")
            ws_log.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(user_id), username, cmd, "FAIL", reason
            ])
            return jsonify(ok=True)

        new_bal = balance - price
        ws_money.update_cell(row, 3, new_bal)
        ws_log.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            str(user_id), username, cmd, price, new_bal
        ])

        tg_hide(
            chat_id,
            "✅ <b>Thành công!</b>\n"
            f"💸 Đã trừ: <b>{price}</b>\n"
            f"💰 Số dư còn lại: <b>{new_bal}</b>"
        )

    return jsonify(ok=True)
