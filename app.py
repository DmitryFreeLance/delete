import heapq
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import requests
from requests import Session


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str
    target_group_id: int | None
    poll_timeout_sec: int
    request_timeout_sec: int
    http_retry_attempts: int
    http_retry_backoff_sec: float
    heartbeat_sec: int
    get_updates_limit: int
    max_delete_per_cycle: int
    ignore_bot_messages: bool
    skip_pending_updates_on_start: bool

    @staticmethod
    def from_env() -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required")

        target_group_raw = os.getenv("TARGET_GROUP_ID", "").strip()
        target_group_id = int(target_group_raw) if target_group_raw else None

        return Config(
            bot_token=token,
            db_path=os.getenv("AUTOPUBLIC_DB_PATH", "/autopublic-data/bot.db").strip(),
            target_group_id=target_group_id,
            poll_timeout_sec=env_int("POLL_TIMEOUT_SEC", 2),
            request_timeout_sec=env_int("REQUEST_TIMEOUT_SEC", 10),
            http_retry_attempts=env_int("HTTP_RETRY_ATTEMPTS", 3),
            http_retry_backoff_sec=float(os.getenv("HTTP_RETRY_BACKOFF_SEC", "0.7")),
            heartbeat_sec=env_int("HEARTBEAT_SEC", 20),
            get_updates_limit=env_int("GET_UPDATES_LIMIT", 100),
            max_delete_per_cycle=env_int("MAX_DELETE_PER_CYCLE", 30),
            ignore_bot_messages=env_bool("IGNORE_BOT_MESSAGES", True),
            skip_pending_updates_on_start=env_bool("SKIP_PENDING_UPDATES_ON_START", True),
        )


class DbGateway:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        # Read-only mode protects the production DB from accidental writes.
        uri = f"file:{self.db_path}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=2)

    def get_target_group_id(self) -> int | None:
        sql = "SELECT value FROM settings WHERE key = 'target_group_id' LIMIT 1"
        with self._connect() as conn:
            cur = conn.cursor()
            row = cur.execute(sql).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return int(str(row[0]).strip())
        except Exception:
            return None

    def is_group_moderation_enabled(self) -> bool:
        sql = "SELECT value FROM settings WHERE key = 'group_moderation_enabled' LIMIT 1"
        with self._connect() as conn:
            cur = conn.cursor()
            row = cur.execute(sql).fetchone()
        if not row or row[0] is None:
            return True
        return str(row[0]).strip().lower() == "true"

    def is_admin(self, user_id: int) -> bool:
        sql = "SELECT 1 FROM admins WHERE user_id = ? LIMIT 1"
        with self._connect() as conn:
            cur = conn.cursor()
            row = cur.execute(sql, (user_id,)).fetchone()
        return row is not None

    def has_active_campaign(self, user_id: int) -> bool:
        sql = "SELECT 1 FROM drafts WHERE user_id = ? AND status = 'ACTIVE' LIMIT 1"
        with self._connect() as conn:
            cur = conn.cursor()
            row = cur.execute(sql, (user_id,)).fetchone()
        return row is not None


class DeleteModerationBot:
    VERIFICATION_BADGE_TEXTS = {
        "проверена. реал",
        "проверена. вирт",
    }
    BADGE_LINK_WINDOW_SEC = 15.0
    POLICY_RETENTION_SEC = 120.0

    def __init__(self, config: Config):
        self.config = config
        self.db = DbGateway(config.db_path)
        hosts_raw = os.getenv("TELEGRAM_API_HOSTS", "api.telegram.org").strip()
        hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
        self.base_urls = [f"https://{host}/bot{config.bot_token}" for host in hosts]
        self.base_url_idx = 0
        self.session: Session = requests.Session()
        self.offset: int | None = None
        self.target_group_id: int | None = config.target_group_id
        self.last_group_refresh_monotonic = 0.0
        self.last_moderation_refresh_monotonic = 0.0
        self.last_heartbeat_monotonic = 0.0
        self.group_moderation_enabled = True

        self.pending_heap: list[tuple[float, int, int, int, int]] = []
        self.pending_keys: set[tuple[int, int]] = set()
        self.seq = 0
        self.recent_message_policy: dict[tuple[int, int], tuple[float, bool]] = {}
        self.last_user_message_key_by_chat: dict[int, tuple[int, int]] = {}

    def run(self) -> None:
        logging.info("Delete moderation bot started")

        if self.config.skip_pending_updates_on_start:
            self.skip_pending_updates()

        while True:
            try:
                self.refresh_runtime_settings()
                self.maybe_emit_heartbeat()
                self.process_due_deletions()
                updates = self.get_updates()
                for update in updates:
                    self.handle_update(update)
            except Exception:
                logging.exception("Main loop error")
                time.sleep(1.0)

    def refresh_runtime_settings(self) -> None:
        now = time.monotonic()
        if now - self.last_group_refresh_monotonic >= 3:
            if self.config.target_group_id is None:
                self.target_group_id = self.db.get_target_group_id()
            self.last_group_refresh_monotonic = now

        if now - self.last_moderation_refresh_monotonic >= 3:
            self.group_moderation_enabled = self.db.is_group_moderation_enabled()
            self.last_moderation_refresh_monotonic = now

    def maybe_emit_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self.last_heartbeat_monotonic < self.config.heartbeat_sec:
            return
        pending = len(self.pending_heap)
        logging.info(
            "Heartbeat: group=%s moderation=%s pending_deletes=%s offset=%s host=%s",
            self.target_group_id,
            self.group_moderation_enabled,
            pending,
            self.offset,
            self.base_urls[self.base_url_idx],
        )
        self.last_heartbeat_monotonic = now

    def active_base_url(self) -> str:
        return self.base_urls[self.base_url_idx]

    def rotate_base_url(self) -> None:
        if len(self.base_urls) <= 1:
            return
        self.base_url_idx = (self.base_url_idx + 1) % len(self.base_urls)
        logging.warning("Switched Telegram host to %s", self.base_urls[self.base_url_idx])

    def api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = max(1, self.config.http_retry_attempts)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            url = f"{self.active_base_url()}/{method}"
            try:
                resp = self.session.post(url, json=payload, timeout=self.config.request_timeout_sec)
                return resp.json()
            except requests.RequestException as e:
                last_error = e
                logging.warning("Telegram API request failed (%s/%s): %s", attempt, attempts, e)
                self.rotate_base_url()
                if attempt < attempts:
                    time.sleep(self.config.http_retry_backoff_sec * attempt)

        logging.error("Telegram API unavailable after retries: %s", last_error)
        return {"ok": False, "description": f"network_error: {last_error}"}

    def get_updates(self) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self.config.poll_timeout_sec,
            "limit": self.config.get_updates_limit,
            "allowed_updates": ["message"],
        }
        if self.offset is not None:
            payload["offset"] = self.offset

        data = self.api_call("getUpdates", payload)
        if not data.get("ok"):
            logging.warning("getUpdates failed: %s", data)
            time.sleep(1.0)
            return []

        updates = data.get("result", []) or []
        return updates

    def skip_pending_updates(self) -> None:
        logging.info("Skipping pending updates on start")
        data = self.api_call("getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
        if not data.get("ok"):
            logging.warning("Initial getUpdates failed: %s", data)
            return

        updates = data.get("result", []) or []
        if not updates:
            return

        max_id = max(u.get("update_id", 0) for u in updates)
        self.offset = max_id + 1
        logging.info("Offset moved to %s", self.offset)

    def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self.offset = update_id + 1

        message = update.get("message")
        if not isinstance(message, dict):
            return

        self.handle_group_message(message)

    def handle_group_message(self, message: dict[str, Any]) -> None:
        if not self.group_moderation_enabled:
            return

        if self.target_group_id is None:
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or chat_id != self.target_group_id:
            return

        self.cleanup_recent_message_policy()

        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        if not isinstance(user_id, int):
            return

        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            return

        is_bot_message = bool(from_user.get("is_bot"))
        if self.config.ignore_bot_messages and is_bot_message:
            if self.should_delete_verification_badge(chat_id, message):
                self.enqueue_delete(chat_id, message_id, 0.0, 0)
            return

        is_admin = self.db.is_admin(user_id)
        has_active_campaign = False if is_admin else self.db.has_active_campaign(user_id)
        should_delete_user_message = (not is_admin) and (not has_active_campaign)
        self.remember_user_message_policy(chat_id, message_id, should_delete_user_message)
        if should_delete_user_message:
            self.enqueue_delete(chat_id, message_id, 0.0, 0)

    @classmethod
    def is_verification_badge_message(cls, message: dict[str, Any]) -> bool:
        text = message.get("text")
        if not isinstance(text, str):
            return False
        normalized = " ".join(text.strip().lower().split())
        return normalized in cls.VERIFICATION_BADGE_TEXTS

    def should_delete_verification_badge(self, chat_id: int, message: dict[str, Any]) -> bool:
        if not self.is_verification_badge_message(message):
            return False

        now = time.time()
        reply_to = message.get("reply_to_message") or {}
        reply_from = reply_to.get("from") or {}
        reply_user_id = reply_from.get("id")
        if isinstance(reply_user_id, int):
            is_admin = self.db.is_admin(reply_user_id)
            if is_admin:
                return False
            if self.db.has_active_campaign(reply_user_id):
                return False
            return True

        reply_message_id = reply_to.get("message_id")
        if isinstance(reply_message_id, int):
            key = (chat_id, reply_message_id)
            decision = self.recent_message_policy.get(key)
            if decision is not None:
                seen_ts, should_delete = decision
                if now - seen_ts <= self.BADGE_LINK_WINDOW_SEC:
                    return should_delete

        last_key = self.last_user_message_key_by_chat.get(chat_id)
        if last_key is not None:
            decision = self.recent_message_policy.get(last_key)
            if decision is not None:
                seen_ts, should_delete = decision
                if now - seen_ts <= self.BADGE_LINK_WINDOW_SEC:
                    return should_delete

        # Fallback: if we cannot link the badge to a recent user post, keep old behavior.
        return True

    def remember_user_message_policy(self, chat_id: int, message_id: int, should_delete: bool) -> None:
        key = (chat_id, message_id)
        self.recent_message_policy[key] = (time.time(), should_delete)
        self.last_user_message_key_by_chat[chat_id] = key

    def cleanup_recent_message_policy(self) -> None:
        now = time.time()
        stale_keys = [
            key
            for key, (seen_ts, _) in self.recent_message_policy.items()
            if now - seen_ts > self.POLICY_RETENTION_SEC
        ]
        for key in stale_keys:
            self.recent_message_policy.pop(key, None)

        stale_chats = [
            chat_id
            for chat_id, key in self.last_user_message_key_by_chat.items()
            if key not in self.recent_message_policy
        ]
        for chat_id in stale_chats:
            self.last_user_message_key_by_chat.pop(chat_id, None)

    def enqueue_delete(self, chat_id: int, message_id: int, due_ts: float, attempt: int) -> None:
        key = (chat_id, message_id)
        if key in self.pending_keys:
            return
        self.pending_keys.add(key)
        self.seq += 1
        if due_ts <= 0:
            due_ts = time.time()
        heapq.heappush(self.pending_heap, (due_ts, self.seq, chat_id, message_id, attempt))

    def process_due_deletions(self) -> None:
        processed = 0
        now = time.time()
        while self.pending_heap and processed < self.config.max_delete_per_cycle:
            due_ts, _, chat_id, message_id, attempt = self.pending_heap[0]
            if due_ts > now:
                break
            heapq.heappop(self.pending_heap)
            key = (chat_id, message_id)
            if key not in self.pending_keys:
                continue

            data = self.api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
            if data.get("ok"):
                self.pending_keys.discard(key)
                processed += 1
                continue

            error_code = int(data.get("error_code") or 0)
            if error_code == 429:
                retry_after = self.extract_retry_after(data)
                self.seq += 1
                heapq.heappush(
                    self.pending_heap,
                    (time.time() + retry_after, self.seq, chat_id, message_id, attempt + 1),
                )
                logging.warning("429 on delete %s/%s, retry in %s sec", chat_id, message_id, retry_after)
                continue

            # Non-retryable (e.g. message already deleted/too old) -> drop.
            logging.info("Delete skipped for %s/%s: %s", chat_id, message_id, data)
            self.pending_keys.discard(key)
            processed += 1

    @staticmethod
    def extract_retry_after(response_json: dict[str, Any]) -> int:
        params = response_json.get("parameters") or {}
        retry_after = params.get("retry_after")
        if isinstance(retry_after, int) and retry_after > 0:
            return retry_after

        desc = str(response_json.get("description") or "")
        marker = "retry after "
        idx = desc.lower().find(marker)
        if idx >= 0:
            raw = desc[idx + len(marker):].strip().split(" ")[0]
            try:
                value = int(raw)
                if value > 0:
                    return value
            except Exception:
                pass
        return 3


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> None:
    setup_logging()
    cfg = Config.from_env()
    bot = DeleteModerationBot(cfg)
    bot.run()


if __name__ == "__main__":
    main()
