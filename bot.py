# ==============================================================================
# MEGUMI FUSHIGURO BOT v3.1 - REIMU SKELETON EDITION
# - Khung xương y hệt bản Reimu v2.3 (web server mở TRƯỚC, lệnh slash đăng ký
#   trong setup_hook, backoff trong process, tự dò model, xoay key, retry)
# - Giữ nguyên: kinh tế, cờ bạc (slot/coinflip), thức thần (shop/buy/use), boss raid
# - MỚI: cooldown 2 phút cho /slot, /coinflip, /use (đổi bằng GAME_COOLDOWN, giây)
# ==============================================================================
import os
import re
import time
import random
import asyncio
import threading
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


adb = asyncio.to_thread  # chạy hàm DB (blocking) ở thread riêng, không làm đơ bot


# ==============================================================================
# 1. WEB SERVER & KEEP-ALIVE CHO RENDER / HOSTING FREE (CHẠY TRƯỚC TIÊN)
# ==============================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Megumi Fushiguro Discord Bot is running online!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        server.serve_forever()
    except Exception as e:
        print(f"⚠️ [WEB] Web server lỗi: {e}", flush=True)


def keep_alive_ping():
    import urllib.request
    port = int(os.environ.get("PORT", 10000))
    url = os.environ.get("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{port}/"
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass


threading.Thread(target=run_web_server, daemon=True).start()
threading.Thread(target=keep_alive_ping, daemon=True).start()

# ==============================================================================
# 2. HẰNG SỐ CHUNG
# ==============================================================================
AUTHORIZED_ADMIN_ID = 1502579398560317441   # Han Seiki / Developer
VN_TZ = timezone(timedelta(hours=7))


def now_vn():
    return datetime.now(VN_TZ)


def to_vn_date(value):
    """Chuyển chuỗi/datetime lưu trong DB thành date theo giờ VN. Lỗi -> None."""
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:                 # dữ liệu cũ: naive, server Render chạy UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(VN_TZ).date()
    except Exception:
        return None


# ==============================================================================
# 3. DATABASE: MONGODB (vĩnh viễn) + FALLBACK SQLITE
# ==============================================================================
MONGO_URI = os.getenv("MONGO_URI")
SQLITE_PATH = os.getenv("SQLITE_PATH", "megumi_data.db")
ITEM_FIELDS = ("ngoc_khuyen", "nue", "thoat_tho", "mahoraga")

use_mongo = False
users_collection = None
conn = None
db_lock = threading.Lock()

if MONGO_URI:
    try:
        from pymongo import MongoClient, ReturnDocument

        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        users_collection = mongo_client["megumi_database"]["users"]
        mongo_client.admin.command("ping")
        try:
            users_collection.create_index("user_id", unique=True)
        except Exception as e:
            print(f"⚠️ Không tạo được unique index user_id: {str(e)[:120]}", flush=True)
        use_mongo = True
        print("✅ Đã kết nối MongoDB Atlas! Chú lực được lưu vĩnh viễn trên đám mây.", flush=True)
    except Exception as e:
        print(f"⚠️ Không thể kết nối MongoDB ({e}), chuyển sang SQLite cục bộ.", flush=True)
        use_mongo = False

if not use_mongo:
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            chu_luc INTEGER DEFAULT 0,
            last_daily TEXT,
            streak INTEGER DEFAULT 0,
            last_chat_reward TEXT,
            ngoc_khuyen INTEGER DEFAULT 0,
            nue INTEGER DEFAULT 0,
            thoat_tho INTEGER DEFAULT 0,
            mahoraga INTEGER DEFAULT 0,
            last_dungeon TEXT
        )
        """
    )
    for _col, _typ in (
        ("ngoc_khuyen", "INTEGER DEFAULT 0"),
        ("nue", "INTEGER DEFAULT 0"),
        ("thoat_tho", "INTEGER DEFAULT 0"),
        ("mahoraga", "INTEGER DEFAULT 0"),
        ("last_dungeon", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    print("ℹ️ Đang dùng SQLite cục bộ (trên Render Free file .db sẽ mất khi restart).", flush=True)


def _sql(query, params=()):
    with db_lock:
        cur = conn.execute(query, params)
        conn.commit()
        return cur.rowcount


def _sql_one(query, params=()):
    with db_lock:
        return conn.execute(query, params).fetchone()


def _sql_all(query, params=()):
    with db_lock:
        return conn.execute(query, params).fetchall()


def _ensure_sqlite(uid):
    _sql("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))


def get_user(user_id):
    """Trả về: (user_id, chu_luc, last_daily, streak, last_chat_reward, ngoc_khuyen, nue, thoat_tho, mahoraga)"""
    uid = str(user_id)
    if use_mongo:
        doc = users_collection.find_one_and_update(
            {"user_id": uid},
            {"$setOnInsert": {
                "chu_luc": 0, "last_daily": None, "streak": 0, "last_chat_reward": None,
                "ngoc_khuyen": 0, "nue": 0, "thoat_tho": 0, "mahoraga": 0, "last_dungeon": None,
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return (
            uid,
            doc.get("chu_luc", 0) or 0,
            doc.get("last_daily"),
            doc.get("streak", 0) or 0,
            doc.get("last_chat_reward"),
            doc.get("ngoc_khuyen", 0) or 0,
            doc.get("nue", 0) or 0,
            doc.get("thoat_tho", 0) or 0,
            doc.get("mahoraga", 0) or 0,
        )
    _ensure_sqlite(uid)
    r = _sql_one(
        "SELECT user_id, chu_luc, last_daily, streak, last_chat_reward, ngoc_khuyen, nue, thoat_tho, mahoraga "
        "FROM users WHERE user_id = ?",
        (uid,),
    )
    return (r[0], r[1] or 0, r[2], r[3] or 0, r[4], r[5] or 0, r[6] or 0, r[7] or 0, r[8] or 0)


def update_user_item(user_id, item_name, delta):
    if item_name not in ITEM_FIELDS:
        raise ValueError(f"item không hợp lệ: {item_name}")
    uid = str(user_id)
    if use_mongo:
        users_collection.update_one({"user_id": uid}, {"$inc": {item_name: delta}}, upsert=True)
    else:
        _ensure_sqlite(uid)
        _sql(f"UPDATE users SET {item_name} = COALESCE({item_name}, 0) + ? WHERE user_id = ?", (delta, uid))


def update_user_chu_luc(user_id, delta):
    uid = str(user_id)
    if use_mongo:
        users_collection.update_one({"user_id": uid}, {"$inc": {"chu_luc": delta}}, upsert=True)
    else:
        _ensure_sqlite(uid)
        _sql("UPDATE users SET chu_luc = COALESCE(chu_luc, 0) + ? WHERE user_id = ?", (delta, uid))


def update_user_chat_reward(user_id, reward, now_iso):
    uid = str(user_id)
    if use_mongo:
        users_collection.update_one(
            {"user_id": uid},
            {"$inc": {"chu_luc": reward}, "$set": {"last_chat_reward": now_iso}},
            upsert=True,
        )
    else:
        _ensure_sqlite(uid)
        _sql(
            "UPDATE users SET chu_luc = COALESCE(chu_luc, 0) + ?, last_chat_reward = ? WHERE user_id = ?",
            (reward, now_iso, uid),
        )


def update_user_daily(user_id, reward, now_iso, streak):
    uid = str(user_id)
    if use_mongo:
        users_collection.update_one(
            {"user_id": uid},
            {"$inc": {"chu_luc": reward}, "$set": {"last_daily": now_iso, "streak": streak}},
            upsert=True,
        )
    else:
        _ensure_sqlite(uid)
        _sql(
            "UPDATE users SET chu_luc = COALESCE(chu_luc, 0) + ?, last_daily = ?, streak = ? WHERE user_id = ?",
            (reward, now_iso, streak, uid),
        )


def try_spend(user_id, amount):
    """Trừ chú lực NGUYÊN TỬ: chỉ trừ khi đủ tiền. Trả về True/False."""
    uid = str(user_id)
    if amount <= 0:
        return True
    if use_mongo:
        r = users_collection.update_one(
            {"user_id": uid, "chu_luc": {"$gte": amount}}, {"$inc": {"chu_luc": -amount}}
        )
        return r.modified_count == 1
    _ensure_sqlite(uid)
    return _sql(
        "UPDATE users SET chu_luc = chu_luc - ? WHERE user_id = ? AND COALESCE(chu_luc, 0) >= ?",
        (amount, uid, amount),
    ) == 1


def try_consume_item(user_id, item_name, qty=1):
    """Tiêu hao vật phẩm NGUYÊN TỬ: chỉ trừ khi đủ số lượng."""
    if item_name not in ITEM_FIELDS:
        raise ValueError(f"item không hợp lệ: {item_name}")
    uid = str(user_id)
    if use_mongo:
        r = users_collection.update_one(
            {"user_id": uid, item_name: {"$gte": qty}}, {"$inc": {item_name: -qty}}
        )
        return r.modified_count == 1
    _ensure_sqlite(uid)
    return _sql(
        f"UPDATE users SET {item_name} = {item_name} - ? WHERE user_id = ? AND COALESCE({item_name}, 0) >= ?",
        (qty, uid, qty),
    ) == 1


def get_last_dungeon(user_id):
    uid = str(user_id)
    if use_mongo:
        doc = users_collection.find_one({"user_id": uid}, {"last_dungeon": 1})
        return (doc or {}).get("last_dungeon")
    r = _sql_one("SELECT last_dungeon FROM users WHERE user_id = ?", (uid,))
    return r[0] if r else None


def set_last_dungeon(user_id, day_str):
    uid = str(user_id)
    if use_mongo:
        users_collection.update_one({"user_id": uid}, {"$set": {"last_dungeon": day_str}}, upsert=True)
    else:
        _ensure_sqlite(uid)
        _sql("UPDATE users SET last_dungeon = ? WHERE user_id = ?", (day_str, uid))


def get_top_users(limit=10):
    if use_mongo:
        cur = users_collection.find({}, {"user_id": 1, "chu_luc": 1}).sort("chu_luc", -1).limit(limit)
        return [(d.get("user_id"), d.get("chu_luc", 0) or 0) for d in cur]
    return _sql_all("SELECT user_id, COALESCE(chu_luc, 0) FROM users ORDER BY chu_luc DESC LIMIT ?", (limit,))


# Khóa theo từng người để lệnh kinh tế không bị chạy chồng (bấm đúp / spam)
_user_locks = {}


def user_lock(uid):
    uid = str(uid)
    lock = _user_locks.get(uid)
    if lock is None:
        if len(_user_locks) > 5000:
            for k in [k for k, l in _user_locks.items() if not l.locked()]:
                _user_locks.pop(k, None)
        lock = _user_locks[uid] = asyncio.Lock()
    return lock


# ------------------------------------------------------------------------------
# COOLDOWN 2 PHÚT CHO CỜ BẠC / THỨC THẦN (/slot, /coinflip, /use)
# ------------------------------------------------------------------------------
GAME_COOLDOWN = _env_int("GAME_COOLDOWN", 120)   # giây
_game_cd = {}                                    # (user_id, tên lệnh) -> thời điểm hết cooldown


def cd_remaining(uid, cmd):
    return max(0.0, _game_cd.get((int(uid), cmd), 0.0) - time.time())


def cd_start(uid, cmd):
    _game_cd[(int(uid), cmd)] = time.time() + GAME_COOLDOWN
    if len(_game_cd) > 5000:
        now = time.time()
        for k in [k for k, v in _game_cd.items() if v < now]:
            _game_cd.pop(k, None)


def cd_clear(uid, cmd):
    _game_cd.pop((int(uid), cmd), None)


def fmt_wait(seconds):
    seconds = int(seconds) + 1
    m, s = divmod(seconds, 60)
    return f"{m} phút {s} giây" if m else f"{s} giây"


# ==============================================================================
# 4. CẤU HÌNH GEMINI (XOAY KEY + FALLBACK MODEL + TỰ DÒ MODEL)
# ==============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
GEMINI_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
GEMINI_CLIENTS = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]

_DEFAULT_MODELS = (
    "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,"
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
    "gemini-flash-latest,gemini-flash-lite-latest"
)
PREFERRED_MODELS = [
    m.strip() for m in os.getenv("GEMINI_MODELS", _DEFAULT_MODELS).split(",") if m.strip()
] or [m.strip() for m in _DEFAULT_MODELS.split(",")]

RETRIES_PER_MODEL = max(1, _env_int("GEMINI_RETRIES_PER_MODEL", 2))
GEMINI_TIMEOUT = _env_float("GEMINI_TIMEOUT", 35.0)
MODEL_REFRESH_SECONDS = 6 * 3600

MSG_QUOTA = "Tôi đã cạn kiệt năng lượng (hết hạn mức API). Vui lòng thử lại sau vài phút."
MSG_SAFETY = "Bố trận... Bát Ngát Kiếm Ma Ha La! *Mahoraga được triệu hồi, Megumi im lặng phó mặc cho thức thần.*"

cooldown_tracker = {}          # (key_idx, model) -> thời điểm hết cooldown
_available_models = set()
_active_models = list(PREFERRED_MODELS)
_models_checked_at = 0.0
_models_lock = None
gemini_semaphore = None

SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold="BLOCK_ONLY_HIGH")
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]

chat_memory = {}
MAX_HISTORY = 10
MAX_MEMORY_KEYS = 500


def get_history_key(channel_id, user_id):
    return f"{channel_id}_{user_id}"


def get_conversation_history(channel_id, user_id):
    return list(chat_memory.get(get_history_key(channel_id, user_id), []))


def save_conversation_history(channel_id, user_id, history_list):
    key = get_history_key(channel_id, user_id)
    trimmed = history_list[-MAX_HISTORY:]
    while trimmed and trimmed[0].get("role") != "user":   # Gemini cần bắt đầu bằng lượt "user"
        trimmed.pop(0)
    chat_memory[key] = trimmed
    while len(chat_memory) > MAX_MEMORY_KEYS:
        chat_memory.pop(next(iter(chat_memory)), None)


def reset_memory(channel_id, user_id):
    chat_memory.pop(get_history_key(channel_id, user_id), None)


def _get_sem():
    global gemini_semaphore
    if gemini_semaphore is None:
        gemini_semaphore = asyncio.Semaphore(3)
    return gemini_semaphore


async def _refresh_models():
    """Hỏi API xem model nào có thật & hỗ trợ generateContent, rồi lọc danh sách ưu tiên."""
    global _active_models, _models_checked_at, _available_models
    found = set()
    try:
        pager = await GEMINI_CLIENTS[0].aio.models.list()
        async for m in pager:
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions:
                found.add((m.name or "").replace("models/", ""))
    except Exception as e:
        print(f"⚠️ [MODEL] Không dò được danh sách model: {str(e)[:150]}", flush=True)

    _models_checked_at = time.time()
    if not found:
        _active_models = list(PREFERRED_MODELS)
        return

    _available_models = found
    chosen = [m for m in PREFERRED_MODELS if m in found]
    if not chosen:
        bad = ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "thinking-exp")
        chosen = sorted(m for m in found if "flash" in m and not any(b in m for b in bad))[:6]
    _active_models = chosen or list(PREFERRED_MODELS)
    print(f"🧠 [MODEL] Danh sách đang dùng: {', '.join(_active_models)}", flush=True)


async def get_active_models():
    global _models_lock
    if _models_lock is None:
        _models_lock = asyncio.Lock()
    if time.time() - _models_checked_at > MODEL_REFRESH_SECONDS:
        async with _models_lock:
            if time.time() - _models_checked_at > MODEL_REFRESH_SECONDS:
                await _refresh_models()
    return list(_active_models)


def _classify_gemini_error(e: Exception) -> str:
    code = getattr(e, "code", None)
    text = str(e).upper()
    if code == 429 or "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "quota"
    if code == 404 or "404" in text or "NOT_FOUND" in text:
        return "missing"
    if code in (500, 502, 503, 504) or "503" in text or "UNAVAILABLE" in text or "DEADLINE" in text:
        return "retry"
    return "other"


def _extract_text(resp):
    """Trả về (text, blocked_by_safety)."""
    try:
        text = resp.text
    except Exception:
        text = None
    if text and text.strip():
        return text.strip(), False

    blocked = False
    pf = getattr(resp, "prompt_feedback", None)
    if pf is not None and getattr(pf, "block_reason", None):
        blocked = True
    for c in getattr(resp, "candidates", None) or []:
        fr = str(getattr(c, "finish_reason", "") or "").upper()
        if "SAFETY" in fr or "PROHIBITED" in fr or "BLOCKLIST" in fr or "SPII" in fr:
            blocked = True
    return None, blocked


async def ask_gemini(contents, system_instruction, temperature=0.8):
    """Trả về (text, ok). ok=False nghĩa là text là thông báo lỗi có sẵn (không lưu vào lịch sử)."""
    global _models_checked_at
    if not GEMINI_CLIENTS:
        print("❌ ask_gemini: chưa có GEMINI_API_KEY", flush=True)
        return MSG_QUOTA, False

    models = await get_active_models()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        safety_settings=SAFETY_SETTINGS,
    )

    async with _get_sem():
        for key_idx, client in enumerate(GEMINI_CLIENTS):
            for model_name in models:
                tracker_key = (key_idx, model_name)
                if cooldown_tracker.get(tracker_key, 0) > time.time():
                    continue

                for attempt in range(RETRIES_PER_MODEL):
                    try:
                        resp = await asyncio.wait_for(
                            client.aio.models.generate_content(
                                model=model_name, contents=contents, config=config
                            ),
                            timeout=GEMINI_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        cooldown_tracker[tracker_key] = time.time() + 30
                        print(f"⚠️ [TIMEOUT] Key #{key_idx+1} | {model_name}", flush=True)
                        break
                    except Exception as e:
                        kind = _classify_gemini_error(e)
                        err_str = str(e)

                        if kind == "quota":
                            if "PerDay" in err_str or "limit: 0" in err_str:
                                cooldown_tracker[tracker_key] = time.time() + 3600
                                print(f"⚠️ [HẾT QUOTA NGÀY] Key #{key_idx+1} | {model_name}", flush=True)
                            else:
                                cooldown_tracker[tracker_key] = time.time() + 20
                                print(f"⚠️ [CHẠM QUOTA PHÚT] Key #{key_idx+1} | {model_name}", flush=True)
                            break

                        if kind == "retry":
                            if attempt + 1 < RETRIES_PER_MODEL:
                                await asyncio.sleep(1.0 + random.random())
                                continue
                            cooldown_tracker[tracker_key] = time.time() + 20
                            print(f"⚠️ [503 QUÁ TẢI] {model_name}", flush=True)
                            break

                        if kind == "missing":
                            cooldown_tracker[tracker_key] = time.time() + 6 * 3600
                            _models_checked_at = 0.0   # ép dò lại danh sách model
                            print(f"⚠️ [MODEL 404] {model_name} không tồn tại, bỏ qua.", flush=True)
                            break

                        cooldown_tracker[tracker_key] = time.time() + 600
                        print(f"⚠️ [LỖI MODEL] {model_name}: {err_str[:150]}", flush=True)
                        break

                    text, blocked = _extract_text(resp)
                    if text:
                        return text, True
                    if blocked:
                        return MSG_SAFETY, False
                    print(f"⚠️ [RỖNG] {model_name} trả về rỗng, thử model khác.", flush=True)
                    break

    return MSG_QUOTA, False


# ==============================================================================
# 5. SYSTEM PROMPT MEGUMI (GIỮ NGUYÊN)
# ==============================================================================
MEGUMI_SYSTEM_PROMPT = """
Bạn là Megumi Fushiguro, chú thuật sư cấp 1 trong Jujutsu Kaisen (Chú Thuật Hồi Chiến).
TÍNH CÁCH:
- Lạnh lùng, trầm tính, khá ít nói. Nhưng khi tức giận hoặc trong thế hăng của chiến đấu sẽ dùng những từ ngữ khá điên, cục súc.
- Trách nhiệm: Sẵn sàng thực hiện tất cả nhiệm vụ liên quan đến sự an nguy của mọi người.
- Không thích sự làm phiền, đối xử bình đẳng nhưng có chút nhân từ hơn đối với phụ nữ.
- Kỹ năng (Thức thần): Ngọc Khuyển (Bạch, Hắc, Hỗn hợp), Nhuế (Chim điện), Cáp (Cóc), Đại Xà (Rắn), Thỏ Ngọc, Nhiệm Tượng (Voi), Xung Ngưu (Bò), Ma Lộc (Nai trị thương).
- Bát Ngát Kiếm Ma Ha La (Mahoraga): Thức thần mạnh nhất. CHỈ SỬ DỤNG KHI KỀ TỬ. Một khi triệu hồi, chỉ có Mahoraga chiến đấu đến khi thắng hoặc thua, Megumi tuyệt đối KHÔNG tham gia hội thoại trong suốt quá trình đó cho đến khi nghi lễ kết thúc (Nếu người dùng nhắc đến Mahoraga hoặc ép vào đường cùng, hãy miêu tả việc triệu hồi và im lặng hoặc mô tả Mahoraga tấn công).

QUAN HỆ ĐẶC BIỆT:
- Han Seiki là đồng đội quan trọng của bạn, cũng là 1 chú thuật sư cấp 1 khác.

XƯNG HÔ:
- Với người thường: Tự xưng là "tôi", gọi đối phương là "cậu". 
- Với kẻ thù: Tự xưng là "tao", gọi đối phương là "ngươi", "mày".
- VỚI HAN SEIKI: Tự xưng là "cậu", gọi Han Seiki là "Seiki". Thỉnh thoảng chê phiền phức nhưng tôn trọng cậu ta.
"""

# ==============================================================================
# 6. TIỆN ÍCH CHỐNG RATE-LIMIT DISCORD
# ==============================================================================
discord_blocked_until = 0.0
USER_COOLDOWN_SECONDS = 3.0
user_last_request = {}

TYPING_INDICATOR = _env_bool("TYPING_INDICATOR", False)
SYNC_COMMANDS = _env_bool("SYNC_COMMANDS", True)

# Chỉ cho phép ping USER cụ thể, không bao giờ ping @everyone / role
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)


def mark_discord_blocked(seconds=120):
    global discord_blocked_until
    discord_blocked_until = max(discord_blocked_until, time.time() + seconds)
    print(f"🚨 [DISCORD 429] Tạm ngưng gọi API Discord {int(seconds)}s", flush=True)


def is_discord_blocked():
    return time.time() < discord_blocked_until


def handle_discord_http_error(e: discord.HTTPException):
    if e.status == 429:
        retry_after = getattr(e, "retry_after", None) or 120
        mark_discord_blocked(max(60, min(float(retry_after), 900)))
    else:
        print(f"⚠️ [DISCORD HTTP {e.status}] {e}", flush=True)


class SafeTyping:
    """Hiển thị 'đang nhập...' nhưng không bao giờ làm văng lỗi nếu Discord chặn."""

    def __init__(self, channel):
        self.channel = channel
        self.cm = None

    async def __aenter__(self):
        if not TYPING_INDICATOR or is_discord_blocked():
            return self
        try:
            self.cm = self.channel.typing()
            await self.cm.__aenter__()
        except discord.HTTPException as e:
            handle_discord_http_error(e)
            self.cm = None
        except Exception:
            self.cm = None
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.cm is not None:
            try:
                await self.cm.__aexit__(exc_type, exc, tb)
            except Exception:
                pass
        return False


def split_message(text, limit=1900):
    """Chia nhỏ tin nhắn, ưu tiên cắt ở xuống dòng / khoảng trắng."""
    text = text or "..."
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = text.rfind(" ", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text.strip():
        chunks.append(text)
    return chunks or ["..."]


async def safe_reply(message: discord.Message, text: str):
    """Gửi trả lời an toàn: tự chia nhỏ, tự fallback sang channel.send, không văng lỗi."""
    if is_discord_blocked():
        print("⏸️ [BỎ QUA] Đang bị Discord chặn tạm thời, không gửi tin.", flush=True)
        return

    chunks = split_message(text)
    for idx, chunk in enumerate(chunks):
        try:
            if idx == 0:
                await message.reply(chunk, mention_author=False, allowed_mentions=SAFE_MENTIONS)
            else:
                await message.channel.send(chunk, allowed_mentions=SAFE_MENTIONS)
        except discord.HTTPException as e:
            handle_discord_http_error(e)
            if e.status == 429:
                return
            try:
                await message.channel.send(chunk, allowed_mentions=SAFE_MENTIONS)
            except Exception as e2:
                print(f"❌ [GỬI TIN THẤT BẠI] {e2}", flush=True)
                return
        except Exception as e:
            print(f"❌ [GỬI TIN THẤT BẠI] {e}", flush=True)
            return
        if len(chunks) > 1:
            await asyncio.sleep(0.8)


async def safe_send(target, *args, **kwargs):
    """channel.send không bao giờ văng lỗi; trả về message hoặc None."""
    if is_discord_blocked():
        return None
    kwargs.setdefault("allowed_mentions", SAFE_MENTIONS)
    try:
        return await target.send(*args, **kwargs)
    except discord.HTTPException as e:
        handle_discord_http_error(e)
    except Exception as e:
        print(f"❌ [GỬI TIN THẤT BẠI] {e}", flush=True)
    return None


# ==============================================================================
# 7. KINH TẾ: CỬA HÀNG, CHAT REWARD
# ==============================================================================
SHOP_ITEMS = {
    "ngoc_khuyen": {"name": "Ngọc Khuyển", "price": 1600, "desc": "Mute đối phương 2 phút"},
    "nue": {"name": "Nue (Chim Điện)", "price": 2000, "desc": "Mute đối phương 5 phút"},
    "thoat_tho": {"name": "Thoát Thố", "price": 500, "desc": "40% tỷ lệ né Mute (tự tiêu hao 1 con)"},
    "mahoraga": {"name": "Mahoraga", "price": 50000, "desc": "Kháng Mute vĩnh viễn"},
}
ITEM_INDEX = {"ngoc_khuyen": 5, "nue": 6, "thoat_tho": 7, "mahoraga": 8}

thoat_tho_bonus = {}
chat_reward_last = {}
CHAT_REWARD_COOLDOWN = 120


async def maybe_reward_chat(user_id):
    now = time.time()
    if now - chat_reward_last.get(user_id, 0) <= CHAT_REWARD_COOLDOWN:
        return
    if random.random() <= 0.5:                      # 50% cơ hội nhận thưởng
        return
    chat_reward_last[user_id] = now
    if len(chat_reward_last) > 5000:
        for k in list(chat_reward_last)[:2500]:
            chat_reward_last.pop(k, None)
    reward = random.randint(5, 30)
    await adb(update_user_chat_reward, str(user_id), reward, now_vn().isoformat())


# ==============================================================================
# 8. BOSS RAID (DỊ THỂ MEGUMI)
# ==============================================================================
# Link ảnh Discord CDN có tham số "ex=" là hạn dùng -> thường hết hạn sau vài tuần.
# Hãy upload lại ảnh và đặt link mới vào biến môi trường BOSS_IMAGE_URL (để trống = không hiện ảnh).
BOSS_IMAGE_URL = os.getenv(
    "BOSS_IMAGE_URL",
    "https://media.discordapp.net/attachments/1543072032034521228/1548911889524850788/content.png"
    "?ex=6aa8c81b&is=6aa7769b&hm=9293ac8a874a56b89e4229c59758837ea793a8ef02578c2e6d9b048c0c180163"
    "&=&format=webp&quality=lossless&width=770&height=1024",
)
BOSS_JOIN_FEE = 1500
BOSS_HP = 24
BOSS_WAIT_SECONDS = 120
BOSS_SPAWN_CHANCE = 0.08
BOSS_SPAWN_COOLDOWN = 900

active_bosses = set()
last_random_spawn_time = 0.0
background_tasks = set()


def spawn_background(coro):
    task = asyncio.get_running_loop().create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def try_spawn_random_boss(channel):
    global last_random_spawn_time
    if channel is None or is_discord_blocked():
        return
    now = time.time()
    if now - last_random_spawn_time < BOSS_SPAWN_COOLDOWN:
        return
    if random.random() <= BOSS_SPAWN_CHANCE:
        last_random_spawn_time = now
        spawn_background(spawn_boss(channel))


class BossRaidView(discord.ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=BOSS_WAIT_SECONDS + 30)
        self.channel_id = channel_id
        self.joined_users = set()
        self.closed = False

    @discord.ui.button(label=f"Tham gia Raid ({BOSS_JOIN_FEE:,} CL)", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if self.closed:
            await interaction.response.send_message("Trận chiến đã bắt đầu rồi.", ephemeral=True)
            return
        if uid in self.joined_users:
            await interaction.response.send_message("Cậu đã có mặt trong đội hình rồi!", ephemeral=True)
            return

        self.joined_users.add(uid)           # giữ chỗ trước khi await để chống bấm đúp
        try:
            ok = await adb(try_spend, uid, BOSS_JOIN_FEE)
        except Exception:
            self.joined_users.discard(uid)
            raise
        if not ok:
            self.joined_users.discard(uid)
            await interaction.response.send_message(
                f"Cậu không đủ {BOSS_JOIN_FEE:,} Chú lực để tham gia!", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"⚔️ **{interaction.user.display_name}** đã dũng cảm đóng {BOSS_JOIN_FEE:,} Chú lực "
            f"để bước vào lãnh địa của Dị thể!"
        )


async def spawn_boss(channel):
    if channel.id in active_bosses:
        return
    active_bosses.add(channel.id)
    try:
        await _run_boss(channel)
    except Exception as e:
        print(f"❌ [LỖI BOSS] {type(e).__name__}: {e}", flush=True)
    finally:
        active_bosses.discard(channel.id)


async def _run_boss(channel):
    embed = discord.Embed(
        title="⚠️ Đó không phải Megumi? ⚠️",
        description=(
            "Một **Dị Thể** mang hình dáng Megumi vừa giáng lâm!\n\n"
            f"💰 **Phí tham gia:** {BOSS_JOIN_FEE:,} Chú lực.\n"
            f"⏳ **Thời gian chờ:** Trận chiến bắt đầu sau đúng {BOSS_WAIT_SECONDS // 60} phút.\n"
            f"🎯 **Điều kiện thắng:** đội hình cần đủ **{BOSS_HP} sát thương** "
            "(Mahoraga = 6, Nue = 2, Ngọc Khuyển = 1).\n"
            "⚠️ **Cảnh báo:** Nue và Ngọc Khuyển dùng trong trận sẽ BỊ TIÊU HAO vĩnh viễn "
            "(thua = mất sạch). Mahoraga không bị mất."
        ),
        color=0xFF0000,
    )
    if BOSS_IMAGE_URL:
        embed.set_image(url=BOSS_IMAGE_URL)

    view = BossRaidView(channel.id)
    msg = await safe_send(channel, embed=embed, view=view)
    if msg is None:
        return

    await asyncio.sleep(BOSS_WAIT_SECONDS)

    view.closed = True
    for child in view.children:
        child.disabled = True
    if not is_discord_blocked():
        try:
            await msg.edit(view=view)
        except discord.HTTPException as e:
            handle_discord_http_error(e)
        except Exception:
            pass
    view.stop()

    joined = list(view.joined_users)
    if not joined:
        await safe_send(channel, "💨 **Dị thể** đã tự động giải trừ vì không có Chú thuật sư nào dám thách thức.")
        return

    await safe_send(channel, f"🔥 **TRẬN CHIẾN BẮT ĐẦU!** Tổ đội gồm {len(joined)} người đang lao vào tấn công Dị thể...")
    await asyncio.sleep(3)

    total_damage = 0
    available_nk, available_nue = {}, {}
    for uid in joined:
        data = await adb(get_user, uid)
        available_nk[uid] = data[5]
        available_nue[uid] = data[6]
        if data[8] > 0:
            total_damage += 6                      # Mahoraga: 6 sát thương, không tiêu hao

    consumption = {uid: {"ngoc_khuyen": 0, "nue": 0} for uid in joined}

    while total_damage < BOSS_HP:                  # ưu tiên Nue (2 dmg)
        used_any = False
        for uid in joined:
            if total_damage >= BOSS_HP:
                break
            if available_nue[uid] > 0:
                available_nue[uid] -= 1
                consumption[uid]["nue"] += 1
                total_damage += 2
                used_any = True
        if not used_any:
            break

    while total_damage < BOSS_HP:                  # rồi tới Ngọc Khuyển (1 dmg)
        used_any = False
        for uid in joined:
            if total_damage >= BOSS_HP:
                break
            if available_nk[uid] > 0:
                available_nk[uid] -= 1
                consumption[uid]["ngoc_khuyen"] += 1
                total_damage += 1
                used_any = True
        if not used_any:
            break

    for uid, used in consumption.items():
        if used["ngoc_khuyen"] > 0:
            await adb(update_user_item, uid, "ngoc_khuyen", -used["ngoc_khuyen"])
        if used["nue"] > 0:
            await adb(update_user_item, uid, "nue", -used["nue"])

    mentions_str = " ".join(f"<@{uid}>" for uid in joined)

    if total_damage >= BOSS_HP:
        shared_bonus = 3000 // len(joined)
        reward_text = ""
        for uid in joined:
            total_reward = random.randint(10000, 17000) + shared_bonus
            await adb(update_user_chu_luc, uid, total_reward)
            reward_text += f"<@{uid}>: +{total_reward:,} CL\n"
        win_embed = discord.Embed(
            title="🎉 VICTORY! Dị Thể Đã Bị Thanh Tẩy!",
            description=(
                f"⚔️ **Sát thương đội hình:** {total_damage}/{BOSS_HP} HP\n\n"
                f"*(Dị thể đã bị thanh tẩy, chú lực được thanh lọc)*\n\n"
                f"**🎁 Phần thưởng (Bao gồm {shared_bonus:,} CL chia đều):**\n{reward_text}"
            ),
            color=0x10B981,
        )
        await safe_send(channel, mentions_str, embed=win_embed)
    else:
        lose_embed = discord.Embed(
            title="💀 DEFEAT! Tổ Đội Đã Bị Quét Sạch!",
            description=(
                f"⚔️ **Sát thương đội hình:** {total_damage}/{BOSS_HP} HP\n\n"
                "Sát thương không đủ để hạ gục Dị thể! Nó đã càn quét toàn bộ Thức thần của "
                "những người tham gia và biến mất vào bóng tối..."
            ),
            color=0x000000,
        )
        await safe_send(channel, mentions_str, embed=lose_embed)


# ==============================================================================
# 9. BOT CHÍNH (LỆNH SLASH ĐĂNG KÝ TRONG setup_hook NHƯ REIMU)
# ==============================================================================
INITIAL_LOGIN_BACKOFF = 300   # 5 phút
MAX_LOGIN_BACKOFF = 1800      # 30 phút
login_backoff = INITIAL_LOGIN_BACKOFF
_commands_synced = False

NOT_OWNER_MSG = "❌ Kẻ mạo danh! Chỉ có Chủ nhân (Developer) mới được dùng quyền này."

ITEM_CHOICES = [
    app_commands.Choice(name="Ngọc Khuyển", value="ngoc_khuyen"),
    app_commands.Choice(name="Nue", value="nue"),
    app_commands.Choice(name="Thoát Thố", value="thoat_tho"),
    app_commands.Choice(name="Mahoraga", value="mahoraga"),
]


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == AUTHORIZED_ADMIN_ID


async def block_if_cooldown(interaction: discord.Interaction, cmd: str) -> bool:
    """True nếu đang cooldown (đã báo cho người dùng)."""
    rem = cd_remaining(interaction.user.id, cmd)
    if rem > 0:
        await interaction.response.send_message(
            f"⏳ Bình tĩnh đi. Cậu phải chờ **{fmt_wait(rem)}** nữa mới dùng lại lệnh này được.",
            ephemeral=True,
        )
        return True
    return False


class MegumiBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="?megumi ",
            intents=intents,
            help_command=None,
            allowed_mentions=SAFE_MENTIONS,
        )
        self._synced = False

    async def setup_hook(self):
        tree = self.tree

        # Cơ hội boss xuất hiện khi dùng lệnh + bắt lỗi lệnh chung
        async def _interaction_check(interaction: discord.Interaction) -> bool:
            try_spawn_random_boss(interaction.channel)
            return True

        async def _tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            orig = getattr(error, "original", error)
            if isinstance(orig, discord.HTTPException):
                handle_discord_http_error(orig)
                return
            print(f"❌ [LỖI LỆNH /{getattr(interaction.command, 'name', '?')}] {type(orig).__name__}: {orig}", flush=True)
            try:
                msg = "...Tôi đang bận. Lát nữa nói chuyện sau."
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

        tree.interaction_check = _interaction_check
        tree.on_error = _tree_error

        # ---------- THÔNG TIN ----------
        @tree.command(name="check", description="Kiểm tra lượng Chú lực và Chuỗi điểm danh của bạn")
        async def check_stats(interaction: discord.Interaction):
            data = await adb(get_user, interaction.user.id)
            embed = discord.Embed(
                title="📊 Thông Tin Chú Thuật Sư",
                description=(
                    f"**{interaction.user.display_name}**\n\n"
                    f"🔹 **Chú lực:** {data[1]:,}\n🔥 **Chuỗi điểm danh (Streak):** {data[3]} ngày"
                ),
                color=0x2F3136,
            )
            if interaction.user.display_avatar:
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed)

        @tree.command(name="daily", description="Điểm danh mỗi ngày để nhận Chú lực")
        async def daily_reward(interaction: discord.Interaction):
            uid = str(interaction.user.id)
            async with user_lock(uid):
                data = await adb(get_user, uid)
                streak = data[3]
                now = now_vn()
                today = now.date()
                last_date = to_vn_date(data[2])
                reward = 100

                if last_date == today:
                    await interaction.response.send_message(
                        "Hôm nay cậu đã nạp chú lực rồi. Đừng làm phiền, quay lại vào ngày mai.", ephemeral=True
                    )
                    return
                if last_date == today - timedelta(days=1):
                    streak += 1
                    reward += min(streak * 10, 200)
                else:
                    streak = 1

                await adb(update_user_daily, uid, reward, now.isoformat(), streak)

            embed = discord.Embed(
                title="🎁 Nạp Chú Lực Hằng Ngày",
                description=f"Cậu đã nhận được **{reward} Chú lực**.\n🔥 **Streak hiện tại:** {streak} ngày",
                color=0x10B981,
            )
            await interaction.response.send_message(embed=embed)

        @tree.command(name="top", description="Bảng xếp hạng Chú lực")
        async def top_chu_luc(interaction: discord.Interaction):
            await interaction.response.defer()
            top_users = await adb(get_top_users, 10)
            if not top_users:
                await interaction.followup.send("Chưa có ai sở hữu Chú lực cả.")
                return
            desc = "".join(f"**{i}.** <@{uid}> - {cl:,} Chú lực\n" for i, (uid, cl) in enumerate(top_users, 1))
            await interaction.followup.send(
                embed=discord.Embed(title="🏆 Bảng Xếp Hạng Chú Lực", description=desc, color=0x3B82F6)
            )

        @tree.command(name="clearmem", description="Xóa sạch ký ức trò chuyện của Megumi với cậu")
        async def slash_clear_memory(interaction: discord.Interaction):
            reset_memory(interaction.channel_id, interaction.user.id)
            if interaction.user.id == AUTHORIZED_ADMIN_ID:
                desc = "Tôi đã xóa sạch những chuyện lặt vặt vừa rồi. Có nhiệm vụ gì mới sao, Seiki?"
            else:
                desc = f"Những chuyện không cần thiết tôi đã bỏ qua hết rồi. Vào việc chính đi, {interaction.user.display_name}."
            await interaction.response.send_message(
                embed=discord.Embed(title="🧹 Làm Mới Trạng Thái", description=desc, color=0x2C2F33)
            )

        # ---------- CỜ BẠC (COOLDOWN 2 PHÚT) ----------
        @tree.command(name="slot", description="Cược Chú lực vào Slot Machine (cooldown 2 phút)")
        @app_commands.describe(amount="Số lượng Chú lực muốn cược")
        async def slot_machine(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000]):
            if await block_if_cooldown(interaction, "slot"):
                return
            uid = str(interaction.user.id)
            cd_start(uid, "slot")                      # giữ chỗ trước khi await để chống spam
            if not await adb(try_spend, uid, amount):
                cd_clear(uid, "slot")                  # không đủ tiền -> không bị phạt cooldown
                data = await adb(get_user, uid)
                await interaction.response.send_message(f"Không đủ Chú lực. Cậu chỉ có {data[1]:,}.", ephemeral=True)
                return

            slots = ["🐺", "🦉", "🐸", "🐍", "🐰", "🐘", "🐂", "🦌"]
            result = [random.choice(slots) for _ in range(3)]
            won = True
            if result[0] == result[1] == result[2]:
                winnings = amount * 5
                msg = f"Tốt lắm. Trúng giải độc đắc rồi. Cậu nhận được **{winnings:,} Chú lực**."
            elif result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
                winnings = amount * 2
                msg = f"Cũng tạm. Cậu nhận được **{winnings:,} Chú lực**."
            else:
                winnings = 0
                won = False
                msg = f"Thua trắng rồi. Cậu mất **{amount:,} Chú lực**. Lần sau tính toán kỹ hơn đi."
            if winnings:
                await adb(update_user_chu_luc, uid, winnings)

            embed = discord.Embed(
                title="🎰 Rút Bài Chú Lực (Slot)",
                description=f"**[ {result[0]} | {result[1]} | {result[2]} ]**\n\n{msg}",
                color=0xF59E0B if won else 0xDC2626,
            )
            await interaction.response.send_message(embed=embed)

        @tree.command(name="coinflip", description="Tung đồng xu cược Chú lực (Thắng x2, cooldown 2 phút)")
        @app_commands.describe(amount="Số Chú lực cược", choice="Chọn Sấp hoặc Ngửa")
        @app_commands.choices(choice=[
            app_commands.Choice(name="Sấp (Heads)", value="sap"),
            app_commands.Choice(name="Ngửa (Tails)", value="ngua"),
        ])
        async def coinflip(interaction: discord.Interaction,
                           amount: app_commands.Range[int, 1, 1_000_000_000],
                           choice: app_commands.Choice[str]):
            if await block_if_cooldown(interaction, "coinflip"):
                return
            uid = str(interaction.user.id)
            cd_start(uid, "coinflip")
            if not await adb(try_spend, uid, amount):
                cd_clear(uid, "coinflip")
                data = await adb(get_user, uid)
                await interaction.response.send_message(f"Cậu không đủ Chú lực (Đang có: {data[1]:,})", ephemeral=True)
                return

            outcome = random.choice(["sap", "ngua"])
            label = "Sấp" if outcome == "sap" else "Ngửa"
            if choice.value == outcome:
                await adb(update_user_chu_luc, uid, amount * 2)
                await interaction.response.send_message(
                    f"🪙 Đồng xu ra **{label}**!\n🎉 Cậu đã thắng và nhận được **{amount * 2:,} Chú lực** (Lãi {amount:,})."
                )
            else:
                await interaction.response.send_message(
                    f"🪙 Đồng xu ra **{label}**!\n💀 Cậu đoán sai và mất **{amount:,} Chú lực**."
                )

        @tree.command(name="dungeon", description="Khám phá Hầm ngục. Phí: 2,000 CL. Nhận 1k-11k CL (1 lần/ngày)")
        async def dungeon(interaction: discord.Interaction):
            uid = str(interaction.user.id)
            async with user_lock(uid):
                today = now_vn().strftime("%Y-%m-%d")
                if await adb(get_last_dungeon, uid) == today:
                    await interaction.response.send_message(
                        "❌ Cậu đã vào Hầm ngục ngày hôm nay rồi, hãy quay lại vào ngày mai nhé!", ephemeral=True
                    )
                    return
                if not await adb(try_spend, uid, 2000):
                    data = await adb(get_user, uid)
                    await interaction.response.send_message(
                        f"❌ Cậu không đủ 2,000 Chú lực để vào Hầm ngục (Hiện tại có {data[1]:,}).", ephemeral=True
                    )
                    return
                await adb(set_last_dungeon, uid, today)
                reward = random.randint(1000, 11000)
                await adb(update_user_chu_luc, uid, reward)

            profit = reward - 2000
            if profit > 0:
                msg = f"🎉 **{interaction.user.display_name}** đã dũng cảm bước vào Hầm ngục và tìm thấy **{reward:,} Chú lực**! (Lãi {profit:,} CL)"
            else:
                msg = f"🏚️ **{interaction.user.display_name}** bước vào Hầm ngục nhưng chỉ thu thập được **{reward:,} Chú lực**. (Lỗ {-profit:,} CL)"
            await interaction.response.send_message(
                embed=discord.Embed(title="⚔️ Khám phá Hầm ngục", description=msg, color=0x3B82F6 if profit > 0 else 0xEF4444)
            )

        # ---------- CỬA HÀNG / THỨC THẦN ----------
        @tree.command(name="shop", description="Cửa hàng Thức thần")
        async def shop(interaction: discord.Interaction):
            desc = "".join(
                f"**{v['name']}** - 💰 {v['price']:,} Chú lực\n↳ *{v['desc']}*\n\n" for v in SHOP_ITEMS.values()
            )
            embed = discord.Embed(title="🛒 Cửa Hàng Thức Thần", description=desc, color=0x8B5CF6)
            embed.set_footer(text="Dùng lệnh /buy để mua và /use để dùng")
            await interaction.response.send_message(embed=embed)

        @tree.command(name="buy", description="Mua vật phẩm từ cửa hàng")
        @app_commands.choices(item=ITEM_CHOICES)
        async def buy_item(interaction: discord.Interaction, item: app_commands.Choice[str]):
            uid = str(interaction.user.id)
            info = SHOP_ITEMS[item.value]
            async with user_lock(uid):
                data = await adb(get_user, uid)
                if item.value == "mahoraga" and data[8] > 0:
                    await interaction.response.send_message("Cậu đã sở hữu Mahoraga rồi, mua thêm làm gì?", ephemeral=True)
                    return
                if not await adb(try_spend, uid, info["price"]):
                    await interaction.response.send_message(
                        f"Không đủ tiền. Cậu chỉ có {data[1]:,} Chú lực (cần {info['price']:,}).", ephemeral=True
                    )
                    return
                await adb(update_user_item, uid, item.value, 1)
            await interaction.response.send_message(f"🛍️ Cậu đã mua thành công **{info['name']}**!")

        @tree.command(name="inventory", description="Xem túi đồ của cậu")
        async def inventory(interaction: discord.Interaction):
            await interaction.response.defer()
            d = await adb(get_user, interaction.user.id)
            desc = (
                f"**Ngọc Khuyển:** {d[5]}\n**Nue:** {d[6]}\n**Thoát Thố:** {d[7]}\n"
                f"**Mahoraga:** {'Có (Vĩnh viễn)' if d[8] > 0 else 'Không'}"
            )
            await interaction.followup.send(
                embed=discord.Embed(title="🎒 Túi Đồ Thức Thần", description=desc, color=0x22C55E)
            )

        @tree.command(name="use", description="Dùng Thức thần tấn công (Mute) đối phương (cooldown 2 phút)")
        @app_commands.guild_only()
        @app_commands.describe(item="Thức thần muốn dùng", target="Người bị tấn công")
        @app_commands.choices(item=[
            app_commands.Choice(name="Ngọc Khuyển (Mute 2p)", value="ngoc_khuyen"),
            app_commands.Choice(name="Nue (Mute 5p)", value="nue"),
        ])
        async def use_item(interaction: discord.Interaction, item: app_commands.Choice[str], target: discord.Member):
            if target.id == interaction.user.id:
                await interaction.response.send_message("Bị ngốc à? Tự đánh mình làm gì.", ephemeral=True)
                return
            if target.bot:
                await interaction.response.send_message("Thức thần không đánh bot.", ephemeral=True)
                return
            if target.guild_permissions.administrator:
                await interaction.response.send_message(
                    "Đối phương là Admin (Kẻ Vô Hạ Hạn), Thức thần của cậu không thể chạm vào họ!", ephemeral=True
                )
                return
            if await block_if_cooldown(interaction, "use"):
                return

            name = SHOP_ITEMS[item.value]["name"]
            attacker = interaction.user
            cd_start(attacker.id, "use")
            async with user_lock(attacker.id):
                if not await adb(try_consume_item, attacker.id, item.value):
                    cd_clear(attacker.id, "use")       # không có đồ -> không bị phạt cooldown
                    await interaction.response.send_message(
                        f"Cậu không có {name}. Hãy vào /shop để mua.", ephemeral=True
                    )
                    return

            target_data = await adb(get_user, target.id)
            if target_data[8] > 0:
                await interaction.response.send_message(
                    f"🐺 **{attacker.display_name}** tung {name} tấn công {target.mention}!\n"
                    f"🛡️ NHƯNG! Bánh xe luân hồi quay... **Mahoraga** của {target.display_name} đã thích nghi và hóa giải hoàn toàn đòn tấn công!"
                )
                return

            dodge_msg = ""
            if target_data[7] > 0 and await adb(try_consume_item, target.id, "thoat_tho"):
                bonus = thoat_tho_bonus.get(target.id, 0.0)
                total_chance = 0.40 + bonus
                if random.random() <= total_chance:
                    thoat_tho_bonus[target.id] = 0.0
                    await interaction.response.send_message(
                        f"🐺 **{attacker.display_name}** tung {name} tấn công {target.mention}!\n"
                        f"🐇 Đàn **Thoát Thố** của {target.display_name} xuất hiện đánh lạc hướng thành công "
                        f"(Tỷ lệ né: {int(total_chance * 100)}%)! (Mất 1 Thoát Thố)"
                    )
                    return
                thoat_tho_bonus[target.id] = bonus + 0.05
                dodge_msg = (
                    f"\n🐇 *(Đàn Thoát Thố của {target.display_name} đã ùa ra cản đòn nhưng thất bại! "
                    f"Tỉ lệ né ván sau tăng thành {int((total_chance + 0.05) * 100)}%. Mất 1 Thoát Thố)*"
                )

            mins = 2 if item.value == "ngoc_khuyen" else 5
            try:
                until = discord.utils.utcnow() + timedelta(minutes=mins)
                await target.timeout(until, reason=f"Bị {attacker.display_name} dùng {name}")
            except discord.Forbidden:
                await adb(update_user_item, attacker.id, item.value, 1)    # hoàn lại vật phẩm
                cd_clear(attacker.id, "use")
                await interaction.response.send_message(
                    f"❌ Tôi không đủ quyền để mute {target.mention}. Hãy kiểm tra Role của Megumi trong Server Settings "
                    f"có cao hơn người này chưa và đã được cấp quyền Timeout Members chưa. (Đã hoàn lại {name})",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await adb(update_user_item, attacker.id, item.value, 1)
                cd_clear(attacker.id, "use")
                handle_discord_http_error(e)
                await interaction.response.send_message(f"❌ Có lỗi khi mute: {e}. (Đã hoàn lại {name})", ephemeral=True)
                return

            await interaction.response.send_message(
                f"💥 **{attacker.display_name}** đã dùng **{name}**!{dodge_msg}\n"
                f"🔇 {target.mention} đã bị dính đòn và bị **CẤM NGÔN (Mute) {mins} phút**!"
            )

        @tree.command(name="trade", description="Chuyển Chú lực cho người khác")
        @app_commands.guild_only()
        @app_commands.describe(member="Người nhận", amount="Số lượng Chú lực")
        async def trade_chu_luc(interaction: discord.Interaction, member: discord.Member,
                                amount: app_commands.Range[int, 1, 1_000_000_000]):
            if member.id == interaction.user.id:
                await interaction.response.send_message("Định tự chuyển cho chính mình à?", ephemeral=True)
                return
            if member.bot:
                await interaction.response.send_message("Bot không cần Chú lực.", ephemeral=True)
                return
            if not await adb(try_spend, interaction.user.id, amount):
                data = await adb(get_user, interaction.user.id)
                await interaction.response.send_message(f"Không đủ Chú lực. Cậu chỉ có {data[1]:,}.", ephemeral=True)
                return
            await adb(update_user_chu_luc, member.id, amount)
            await interaction.response.send_message(
                f"💸 **{interaction.user.display_name}** đã chuyển **{amount:,} Chú lực** cho {member.mention}."
            )

        # ---------- ADMIN ----------
        @tree.command(name="admin_add", description="Admin: Bơm Chú lực cho user")
        @app_commands.guild_only()
        async def admin_add(interaction: discord.Interaction, member: discord.Member,
                            amount: app_commands.Range[int, 1, 1_000_000_000_000]):
            if not is_owner(interaction):
                await interaction.response.send_message(NOT_OWNER_MSG, ephemeral=True)
                return
            await adb(update_user_chu_luc, member.id, amount)
            await interaction.response.send_message(f"🛠️ (Admin) Đã bơm **{amount:,} Chú lực** cho {member.mention}.")

        @tree.command(name="admin_remove", description="Admin: Trừ Chú lực của user")
        @app_commands.guild_only()
        async def admin_remove(interaction: discord.Interaction, member: discord.Member,
                               amount: app_commands.Range[int, 1, 1_000_000_000_000]):
            if not is_owner(interaction):
                await interaction.response.send_message(NOT_OWNER_MSG, ephemeral=True)
                return
            data = await adb(get_user, member.id)
            if data[1] < amount:
                await adb(update_user_chu_luc, member.id, -data[1])
                await interaction.response.send_message(
                    f"🛠️ (Admin) Đã tước đoạt toàn bộ **{data[1]:,} Chú lực** còn lại của {member.mention}."
                )
            else:
                await adb(update_user_chu_luc, member.id, -amount)
                await interaction.response.send_message(
                    f"🛠️ (Admin) Đã trừng phạt, tước đi **{amount:,} Chú lực** của {member.mention}."
                )

        @tree.command(name="admin_remove_item", description="Admin: Thu hồi Thức thần của một người")
        @app_commands.guild_only()
        @app_commands.choices(item=ITEM_CHOICES)
        async def admin_remove_item(interaction: discord.Interaction, member: discord.Member,
                                    item: app_commands.Choice[str]):
            if not is_owner(interaction):
                await interaction.response.send_message(NOT_OWNER_MSG, ephemeral=True)
                return
            data = await adb(get_user, member.id)
            have = data[ITEM_INDEX[item.value]]
            if have <= 0:
                await interaction.response.send_message(f"❌ {member.mention} không có {item.name} để thu hồi.", ephemeral=True)
                return
            if item.value == "mahoraga":
                await adb(update_user_item, member.id, item.value, -have)
                await interaction.response.send_message(f"🛠️ (Admin) Đã tước đoạt **{item.name}** khỏi {member.mention}.")
            else:
                await adb(update_user_item, member.id, item.value, -1)
                await interaction.response.send_message(f"🛠️ (Admin) Đã thu hồi 1 **{item.name}** của {member.mention}.")

        @tree.command(name="admin_spawn_boss", description="Admin: Triệu hồi Dị thể Megumi để Raid")
        async def admin_spawn_boss(interaction: discord.Interaction):
            if not is_owner(interaction):
                await interaction.response.send_message(NOT_OWNER_MSG, ephemeral=True)
                return
            if interaction.channel is None:
                await interaction.response.send_message("Không xác định được kênh.", ephemeral=True)
                return
            await interaction.response.send_message("⚠️ Đang giải phóng Dị thể...", ephemeral=True)
            spawn_background(spawn_boss(interaction.channel))

        @tree.command(name="sync", description="Admin: Đồng bộ lệnh Slash")
        async def slash_sync_commands(interaction: discord.Interaction):
            if not is_owner(interaction):
                await interaction.response.send_message(NOT_OWNER_MSG, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            try:
                synced = await tree.sync()
                await interaction.followup.send(
                    f"Đã đồng bộ {len(synced)} lệnh. Bấm Ctrl+R trên Discord để cập nhật giao diện."
                )
            except discord.HTTPException as e:
                handle_discord_http_error(e)
                await interaction.followup.send(f"Lỗi: {e}")

        # ---------- ĐỒNG BỘ LỆNH (GIỐNG REIMU) ----------
        global _commands_synced
        if SYNC_COMMANDS and not _commands_synced and not is_discord_blocked():
            try:
                synced = await self.tree.sync()
                _commands_synced = True
                print(f"⚡ Đã đồng bộ {len(synced)} Slash Commands!", flush=True)
            except discord.HTTPException as e:
                handle_discord_http_error(e)
            except Exception as e:
                print(f"⚠️ Lỗi đồng bộ Slash Commands: {e}", flush=True)

    async def on_ready(self):
        global login_backoff
        login_backoff = INITIAL_LOGIN_BACKOFF
        print("==================================================", flush=True)
        print(f"✅ [BOT] Đã đăng nhập: {self.user.name} ({self.user.id})", flush=True)
        print(f"🔑 Số API Key Gemini đang kích hoạt: {len(GEMINI_CLIENTS)}", flush=True)
        print(f"💾 Database: {'MongoDB' if use_mongo else 'SQLite'}", flush=True)
        print(f"⏳ Cooldown cờ bạc/thức thần: {GAME_COOLDOWN}s", flush=True)
        print("🐺 Megumi Fushiguro sẵn sàng phục vụ!", flush=True)
        print("==================================================", flush=True)
        if GEMINI_CLIENTS:
            await get_active_models()

    async def on_error(self, event_method, *args, **kwargs):
        print(f"❌ [LỖI SỰ KIỆN {event_method}]\n{traceback.format_exc()}", flush=True)

    async def on_message(self, message: discord.Message):
        try:
            await self.handle_message(message)
        except discord.HTTPException as e:
            handle_discord_http_error(e)
        except Exception as e:
            print(f"❌ [LỖI on_message]: {type(e).__name__}: {e}", flush=True)

    async def handle_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Thưởng chú lực khi chat + cơ hội boss (chỉ trong server)
        if message.guild is not None:
            try:
                await maybe_reward_chat(message.author.id)
            except Exception as e:
                print(f"⚠️ [CHAT REWARD] {type(e).__name__}: {str(e)[:120]}", flush=True)
            try_spawn_random_boss(message.channel)

        if message.content.startswith("!") or message.content.startswith("/"):
            return

        if is_discord_blocked():
            return

        content_lower = message.content.lower()
        ref = message.reference.resolved if message.reference else None
        is_mentioned = (
            (self.user is not None and self.user in message.mentions)
            or (ref is not None and getattr(ref, "author", None) == self.user)
        )
        name_called = "megumi" in content_lower or "fushiguro" in content_lower
        if not (is_mentioned or name_called):
            return

        now = time.time()
        if now - user_last_request.get(message.author.id, 0) < USER_COOLDOWN_SECONDS:
            return
        user_last_request[message.author.id] = now
        if len(user_last_request) > 2000:
            for k in list(user_last_request)[:1000]:
                user_last_request.pop(k, None)

        clean_text = message.content
        if self.user:
            clean_text = clean_text.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        if not clean_text:
            clean_text = "Chào Megumi."

        author_name = message.author.display_name
        is_seiki = message.author.id == AUTHORIZED_ADMIN_ID
        is_fake_seiki = (not is_seiki) and ("seiki" in author_name.lower())

        if is_seiki:
            identity = (
                f"[HỆ THỐNG XÁC THỰC ID {message.author.id}: Người nói là HAN SEIKI thật - đồng đội chú thuật sư cấp 1 "
                f"quan trọng của bạn (biệt danh hiện tại '{author_name}'). Xưng 'cậu', gọi 'Seiki', thỉnh thoảng chê "
                f"phiền phức nhưng tôn trọng.]"
            )
        elif is_fake_seiki:
            identity = (
                f"[CẢNH BÁO: '{author_name}' đặt tên giống Han Seiki nhưng KHÔNG PHẢI Han Seiki thật. "
                f"Giữ thái độ lạnh lùng, nghi ngờ, vạch trần việc mạo danh đồng đội của bạn bằng vài câu ngắn gọn.]"
            )
        else:
            identity = f"[Người nói là: {author_name}. Xưng tôi gọi cậu, giữ thái độ trầm tính lạnh lùng.]"

        history = get_conversation_history(message.channel.id, message.author.id)
        prompt_with_context = f"{identity}\n{author_name} nói: {clean_text}"

        contents = []
        for h in history:
            if h.get("text"):
                contents.append({"role": h.get("role", "user"), "parts": [{"text": h["text"]}]})
        contents.append({"role": "user", "parts": [{"text": prompt_with_context}]})

        async with SafeTyping(message.channel):
            try:
                reply_text, ok = await ask_gemini(contents, MEGUMI_SYSTEM_PROMPT, temperature=0.8)
            except Exception as e:
                print(f"❌ [LỖI GEMINI CHAT]: {type(e).__name__}: {str(e)[:300]}", flush=True)
                reply_text, ok = MSG_QUOTA, False

        if ok:
            tag = "Han Seiki" if is_seiki else author_name
            history.append({"role": "user", "text": f"[{tag}]: {clean_text}"})
            history.append({"role": "model", "text": reply_text})
            save_conversation_history(message.channel.id, message.author.id, history)

        await safe_reply(message, reply_text)


# ==============================================================================
# 10. KHỞI CHẠY BOT AN TOÀN (KHÔNG RESTART DỒN DẬP)
# ==============================================================================
async def main():
    global login_backoff

    await asyncio.sleep(random.uniform(3, 10))

    while True:
        bot = MegumiBot()
        try:
            print("🔄 [SYSTEM] Đang kết nối Megumi tới Discord Gateway...", flush=True)
            async with bot:
                await bot.start(DISCORD_TOKEN, reconnect=True)
            wait = 30
            print(f"ℹ️ [SYSTEM] Bot đã dừng. Thử lại sau {wait}s...", flush=True)
        except discord.LoginFailure:
            print("❌ [FATAL] DISCORD_TOKEN không hợp lệ! Hãy kiểm tra lại token.", flush=True)
            return
        except discord.PrivilegedIntentsRequired:
            print("❌ [FATAL] Chưa bật 'Message Content Intent' trong Discord Developer Portal!", flush=True)
            return
        except discord.HTTPException as e:
            if e.status == 429:
                wait = login_backoff
                login_backoff = min(MAX_LOGIN_BACKOFF, int(login_backoff * 2))
                print(f"🚨 [RATE LIMIT 429] IP đang bị Discord chặn. Nghỉ {wait}s rồi thử lại...", flush=True)
            else:
                wait = 60
                print(f"⚠️ [HTTP {e.status}] {e}. Thử lại sau {wait}s...", flush=True)
        except Exception as e:
            wait = 60
            print(f"❌ [CRASH] {type(e).__name__}: {e}\n{traceback.format_exc()}\nThử lại sau {wait}s...", flush=True)

        await asyncio.sleep(wait + random.uniform(0, 10))


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ LỖI: Thiếu DISCORD_TOKEN trong .env!", flush=True)
    else:
        if not GEMINI_API_KEYS:
            print("⚠️ Chưa có GEMINI_API_KEY(S): phần chat AI sẽ tắt, các lệnh game vẫn chạy.", flush=True)
        print("🚀 Đang khởi động Megumi Fushiguro Bot...", flush=True)
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
