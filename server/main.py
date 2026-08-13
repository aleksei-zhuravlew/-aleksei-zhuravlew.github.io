import base64
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "/data/game.db")
BOT_ID = os.environ.get("TELEGRAM_BOT_ID", "").strip()
TELEGRAM_PUBLIC_KEY = bytes.fromhex(
    "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"
)
PUZZLEBOT_TOKEN = os.environ.get("PUZZLEBOT_API_TOKEN", "").strip()
PUZZLEBOT_COMMAND = os.environ.get("PUZZLEBOT_COMMAND", "/plus_game_day").strip()
CAMPAIGN_ID = os.environ.get("CAMPAIGN_ID", "release-catcher-2026").strip()
AUTH_MAX_AGE = int(os.environ.get("TELEGRAM_AUTH_MAX_AGE", "86400"))
REWARD_MIN_SECONDS = int(os.environ.get("REWARD_MIN_SECONDS", "5"))
ALLOWED_GENRES = {"indie", "pop", "rock", "underground", "electronic", "folk"}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            username TEXT,
            score INTEGER NOT NULL DEFAULT 0,
            caught_genres TEXT NOT NULL DEFAULT '[]',
            misses INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            reward_unlocked INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS leaderboard (
            user_id INTEGER PRIMARY KEY,
            player_name TEXT NOT NULL,
            username TEXT,
            best_score INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rewards (
            campaign_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            reward_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            session_id TEXT NOT NULL,
            issued_at INTEGER,
            updated_at INTEGER NOT NULL,
            last_error TEXT,
            PRIMARY KEY (campaign_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_leaderboard_score
            ON leaderboard(best_score DESC, updated_at ASC);
        """)


def verify_init_data(raw):
    if not BOT_ID or not BOT_ID.isdigit():
        raise ApiError(503, "telegram_not_configured", "Telegram bot ID is not configured")
    if not raw:
        raise ApiError(401, "telegram_auth_required", "Open the game from Telegram")

    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    values = dict(pairs)
    values.pop("hash", None)
    received_signature = values.pop("signature", "")
    if not received_signature:
        raise ApiError(401, "telegram_signature_missing", "Telegram signature is missing")

    data_check_string = (
        f"{BOT_ID}:WebAppData\n"
        + "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    )
    try:
        padding = "=" * (-len(received_signature) % 4)
        signature = base64.urlsafe_b64decode(received_signature + padding)
        public_key = Ed25519PublicKey.from_public_bytes(TELEGRAM_PUBLIC_KEY)
        public_key.verify(signature, data_check_string.encode("utf-8"))
    except (InvalidSignature, ValueError, TypeError):
        raise ApiError(401, "telegram_signature_invalid", "Telegram signature is invalid")

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        raise ApiError(401, "telegram_auth_date_invalid", "Telegram auth date is invalid")
    now = int(time.time())
    if auth_date <= 0 or abs(now - auth_date) > AUTH_MAX_AGE:
        raise ApiError(401, "telegram_auth_expired", "Reopen the game from Telegram")

    try:
        user = json.loads(values.get("user", "{}"))
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ApiError(401, "telegram_user_invalid", "Telegram user is missing")

    username = str(user.get("username") or "").strip()[:64] or None
    first_name = str(user.get("first_name") or "").strip()[:64]
    player_name = f"@{username}" if username else (first_name or f"Игрок {user_id}")
    return {"id": user_id, "username": username, "player_name": player_name}

def validate_progress(payload, session):
    try:
        score = int(payload.get("score", 0))
        misses = int(payload.get("misses", 0))
    except (TypeError, ValueError):
        raise ApiError(400, "progress_invalid", "Score or misses are invalid")

    genres = payload.get("caught_genres", [])
    if not isinstance(genres, list):
        raise ApiError(400, "genres_invalid", "Caught genres must be a list")
    genres = list(dict.fromkeys(str(item) for item in genres))
    if not set(genres).issubset(ALLOWED_GENRES):
        raise ApiError(400, "genres_invalid", "Unknown genre")
    if score < 0 or score > 10_000_000 or misses < 0 or misses > 3:
        raise ApiError(400, "progress_invalid", "Progress is out of range")
    if score < session["score"] or misses < session["misses"]:
        raise ApiError(409, "progress_rewind", "Progress cannot go backwards")

    unique_count = len(genres)
    remainder = score - unique_count * 100
    if remainder < 0 or remainder % 25 != 0:
        raise ApiError(400, "score_invalid", "Score does not match caught releases")
    caught_items = unique_count + remainder // 25

    elapsed = max(0, int(time.time()) - session["started_at"])
    max_items = max(1, int((elapsed + 1.5) / 0.48))
    if caught_items > max_items + 2:
        raise ApiError(400, "score_too_fast", "Score grew faster than the game allows")

    reward_unlocked = (
        unique_count == len(ALLOWED_GENRES)
        and misses < 3
        and elapsed >= REWARD_MIN_SECONDS
    )
    return score, misses, genres, reward_unlocked


def get_session(db, session_id, user_id):
    row = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()
    if not row:
        raise ApiError(404, "session_not_found", "Game session was not found")
    return row


def apply_progress(db, payload, user):
    session_id = str(payload.get("session_id") or "")
    session = get_session(db, session_id, user["id"])
    score, misses, genres, reward_unlocked = validate_progress(payload, session)
    now = int(time.time())
    db.execute(
        """UPDATE sessions
           SET score = ?, misses = ?, caught_genres = ?, reward_unlocked = ?,
               updated_at = ?, status = ?
           WHERE id = ?""",
        (
            score,
            misses,
            json.dumps(genres, ensure_ascii=False),
            1 if reward_unlocked or session["reward_unlocked"] else 0,
            now,
            "finished" if misses >= 3 else "active",
            session_id,
        ),
    )
    db.execute(
        """INSERT INTO leaderboard(user_id, player_name, username, best_score, updated_at)
           VALUES(?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             player_name = excluded.player_name,
             username = excluded.username,
             best_score = MAX(leaderboard.best_score, excluded.best_score),
             updated_at = CASE
               WHEN excluded.best_score > leaderboard.best_score
               THEN excluded.updated_at ELSE leaderboard.updated_at END""",
        (user["id"], user["player_name"], user["username"], score, now),
    )
    return {
        "session_id": session_id,
        "score": score,
        "reward_unlocked": bool(reward_unlocked or session["reward_unlocked"]),
    }


def puzzlebot_issue_day(user_id):
    if not PUZZLEBOT_TOKEN:
        raise ApiError(503, "puzzlebot_not_configured", "PuzzleBot token is not configured")
    query = urllib.parse.urlencode(
        {"token": PUZZLEBOT_TOKEN, "method": "sendCommand"}
    )
    url = f"https://api.puzzlebot.top/?{query}"
    body = json.dumps(
        {"tg_chat_id": user_id, "command_name": PUZZLEBOT_COMMAND}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(64_000)
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"PuzzleBot returned HTTP {response.status}")
            if raw:
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, dict) and parsed.get("success") is False:
                    raise RuntimeError("PuzzleBot rejected the command")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as error:
        raise ApiError(502, "puzzlebot_failed", f"PuzzleBot request failed: {error}")


class Handler(BaseHTTPRequestHandler):
    server_version = "ReleaseCatcherAPI/1.0"

    def log_message(self, format_string, *args):
        print(f'{self.address_string()} - {format_string % args}', flush=True)

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "body_invalid", "Invalid content length")
        if length <= 0 or length > 32_768:
            raise ApiError(400, "body_invalid", "Request body is invalid")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "json_invalid", "Request body must be JSON")

    def authenticate(self):
        return verify_init_data(self.headers.get("X-Telegram-Init-Data", ""))

    def route(self):
        path = urllib.parse.urlsplit(self.path).path
        if self.command == "GET" and path == "/api/health":
            return 200, {
                "ok": True,
                "telegram_configured": bool(BOT_ID and BOT_ID.isdigit()),
                "puzzlebot_configured": bool(PUZZLEBOT_TOKEN),
            }

        if self.command == "GET" and path == "/api/leaderboard":
            with connect() as db:
                rows = db.execute(
                    """SELECT player_name AS name, best_score AS score
                       FROM leaderboard
                       ORDER BY best_score DESC, updated_at ASC LIMIT 10"""
                ).fetchall()
            return 200, {"entries": [dict(row) for row in rows]}

        if self.command == "POST" and path == "/api/session/start":
            user = self.authenticate()
            session_id = uuid.uuid4().hex
            now = int(time.time())
            with connect() as db:
                db.execute(
                    """INSERT INTO sessions
                       (id, user_id, player_name, username, started_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        user["id"],
                        user["player_name"],
                        user["username"],
                        now,
                        now,
                    ),
                )
            return 201, {"session_id": session_id, "player": user["player_name"]}

        if self.command == "POST" and path == "/api/session/progress":
            user = self.authenticate()
            payload = self.read_json()
            with connect() as db:
                result = apply_progress(db, payload, user)
            return 200, result

        if self.command == "POST" and path == "/api/reward/claim":
            user = self.authenticate()
            payload = self.read_json()
            with connect() as db:
                progress = apply_progress(db, payload, user)

            reward_id = f"{CAMPAIGN_ID}:{user['id']}"
            now = int(time.time())
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                session = get_session(db, progress["session_id"], user["id"])
                if not session["reward_unlocked"]:
                    raise ApiError(409, "reward_locked", "Collect all six genres first")

                reward = db.execute(
                    "SELECT * FROM rewards WHERE campaign_id = ? AND user_id = ?",
                    (CAMPAIGN_ID, user["id"]),
                ).fetchone()
                if reward and reward["status"] == "issued":
                    db.commit()
                    return 200, {
                        "success": True,
                        "already_issued": True,
                        "reward_id": reward["reward_id"],
                    }
                if reward and reward["status"] == "processing" and now - reward["updated_at"] < 60:
                    db.commit()
                    raise ApiError(409, "reward_processing", "Reward is already processing")

                db.execute(
                    """INSERT INTO rewards
                       (campaign_id, user_id, reward_id, status, session_id, updated_at)
                       VALUES (?, ?, ?, 'processing', ?, ?)
                       ON CONFLICT(campaign_id, user_id) DO UPDATE SET
                         status = 'processing', session_id = excluded.session_id,
                         updated_at = excluded.updated_at, last_error = NULL""",
                    (CAMPAIGN_ID, user["id"], reward_id, progress["session_id"], now),
                )
                db.commit()

            try:
                puzzlebot_issue_day(user["id"])
            except ApiError as error:
                with connect() as db:
                    db.execute(
                        """UPDATE rewards SET status = 'failed', updated_at = ?, last_error = ?
                           WHERE campaign_id = ? AND user_id = ?""",
                        (int(time.time()), error.code, CAMPAIGN_ID, user["id"]),
                    )
                raise

            with connect() as db:
                db.execute(
                    """UPDATE rewards
                       SET status = 'issued', issued_at = ?, updated_at = ?, last_error = NULL
                       WHERE campaign_id = ? AND user_id = ?""",
                    (int(time.time()), int(time.time()), CAMPAIGN_ID, user["id"]),
                )
            return 200, {
                "success": True,
                "already_issued": False,
                "reward_id": reward_id,
            }

        raise ApiError(404, "not_found", "Endpoint not found")

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_request(self):
        try:
            status, payload = self.route()
            self.send_json(status, payload)
        except ApiError as error:
            self.send_json(
                error.status,
                {"success": False, "error": error.code, "message": error.message},
            )
        except Exception as error:
            print(f"Unhandled error: {error!r}", flush=True)
            self.send_json(
                500,
                {"success": False, "error": "internal_error", "message": "Internal server error"},
            )


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Release Catcher API listening on :{PORT}", flush=True)
    server.serve_forever()
