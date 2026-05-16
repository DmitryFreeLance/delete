import heapq
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import requests


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
    def __init__(self, config: Config):
        self.config = config
        self.db = DbGateway(config.db_path)
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"
        self.offset: int | None = None
        self.target_group_id: int | None = config.target_group_id
        self.last_group_refresh_monotonic = 0.0
        self.last_moderation_refresh_monotonic = 0.0
        self.group_moderation_enabled = True

        self.pending_heap: list[tuple[float, int, int, int, int]] = []
        self.pending_keys: set[tuple[int, int]] = set()
        self.seq = 0

    def run(self) -> None:
        logging.info("Delete moderation bot started")

        if self.config.skip_pending_updates_on_start:
            self.skip_pending_updates()

        while True:
            try:
                self.refresh_runtime_settings()
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

    def api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{method}"
        resp = requests.post(url, json=payload, timeout=self.config.request_timeout_sec)
        return resp.json()

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

        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        if not isinstance(user_id, int):
            return

        if self.config.ignore_bot_messages and bool(from_user.get("is_bot")):
            return

        if self.db.is_admin(user_id):
            return

        if self.db.has_active_campaign(user_id):
            return

        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            return

        self.enqueue_delete(chat_id, message_id, 0.0, 0)

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
