import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


TEST_HANDOFF_TEXT = "测试转人工"
MAX_ATTEMPTS = 6


class Store:
    """Persist a single account's cursor, callbacks and handoffs. / 持久化单个账号的游标、回调和转接。"""

    def __init__(self, path: Path, corp_id: str, account: str):
        self.path = path
        self.account = account
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    corp_id TEXT NOT NULL,
                    account TEXT NOT NULL,
                    cursor TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS callbacks (
                    id INTEGER PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    token TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    send_time INTEGER NOT NULL,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    state TEXT NOT NULL,
                    servicer_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                );
            """)
            db.execute("INSERT OR IGNORE INTO sync_state (id, corp_id, account) VALUES (1, ?, ?)",
                       (corp_id, account))
            scope = db.execute("SELECT corp_id, account FROM sync_state WHERE id = 1").fetchone()
            if tuple(scope) != (corp_id, account):
                raise ValueError("Use a different WECOM_DB_PATH for a different WeCom account")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue_callback(self, token: str) -> None:
        """Durably record a signed callback before acknowledging it. / 回调先持久化，再向企微确认。"""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO callbacks (token_hash, token) VALUES (?, ?)", (digest, token))

    def work(self) -> tuple[sqlite3.Row | None, sqlite3.Row | None, str]:
        """Read one due callback and transfer for the single worker. / 单 Worker 读取到期回调及转接。"""
        now = time.time()
        with self._connect() as db:
            callback = db.execute("SELECT * FROM callbacks WHERE state='pending' AND next_attempt<=? "
                                  "ORDER BY id LIMIT 1", (now,)).fetchone()
            handoff = db.execute("SELECT * FROM messages WHERE state='pending' AND next_attempt<=? "
                                 "ORDER BY rowid LIMIT 1", (now,)).fetchone()
            cursor = db.execute("SELECT cursor FROM sync_state WHERE id=1").fetchone()[0]
        return callback, handoff, cursor

    def save_page(self, callback_id: int, cursor: str, more: bool, items: list[dict]) -> None:
        """Commit messages and cursor together so a failed page can be replayed.
        消息、游标和回调进度一起提交，失败后可以重放当前页。
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in items:
                if item.get("msgtype") == "event":
                    self._save_session_event(db, item)
                if item.get("msgtype") == "text" and item.get("origin") == 3:
                    self._save_text(db, item)
            db.execute("UPDATE sync_state SET cursor=? WHERE id=1", (cursor,))
            if not more:
                db.execute("UPDATE callbacks SET state='done', token=NULL, last_error=NULL WHERE id=?", (callback_id,))

    def _save_text(self, db: sqlite3.Connection, item: dict) -> None:
        # 只记录测试账号客户文本，数据库不落原文。 / Record customer text metadata only for the test account.
        message_id, sender, text = (item.get(key) for key in ("msgid", "external_userid", "text"))
        if item.get("open_kfid") != self.account or not all(isinstance(x, str) and x for x in (message_id, sender)):
            return
        content = text.get("content") if isinstance(text, dict) else None
        if not isinstance(content, str):
            return
        state = "pending" if content.strip() == TEST_HANDOFF_TEXT else "received"
        send_time = item.get("send_time")
        db.execute("INSERT OR IGNORE INTO messages (message_id, sender_id, send_time, state) VALUES (?, ?, ?, ?)",
                   (message_id, sender, send_time if type(send_time) is int else 0, state))

    def _save_session_event(self, db: sqlite3.Connection, item: dict) -> None:
        # 以企微事件确认人工接起或结束。 / Use provider events to confirm pickup or closure.
        event = item.get("event")
        if (not isinstance(event, dict) or event.get("event_type") != "session_status_change"
                or event.get("open_kfid") != self.account or not event.get("external_userid")):
            return
        change = event.get("change_type")
        if change not in (1, 2, 3, 4):
            return
        state = "closed" if change == 3 else "accepted"
        sent = item.get("send_time")
        db.execute("""UPDATE messages SET state=?, servicer_id=COALESCE(?, servicer_id), last_error=NULL
                    WHERE sender_id=? AND state IN ('pending','queued','accepted') AND (? IS NULL OR send_time<=?)""",
                   (state, event.get("new_servicer_userid"), event["external_userid"], sent, sent))

    def finish_handoff(self, message_id: str, state: str, servicer: str | None) -> None:
        """Write the confirmed provider outcome for a pending request. / 保存企微确认的转接结果。"""
        with self._connect() as db:
            db.execute("UPDATE messages SET state=?, servicer_id=?, last_error=NULL "
                       "WHERE message_id=? AND state='pending'", (state, servicer, message_id))

    def retry(self, row: sqlite3.Row, error: str, *, callback: bool) -> int:
        """Reschedule with bounded backoff and keep the final failure visible. / 退避重试并保留最终错误。"""
        attempts = row["attempts"] + 1
        state = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        values = (attempts, time.time() + min(60, 2 ** (attempts - 1)), state, error)
        with self._connect() as db:
            if callback:
                db.execute("UPDATE callbacks SET attempts=?, next_attempt=?, state=?, last_error=?, "
                           "token=CASE WHEN ?='failed' THEN NULL ELSE token END WHERE id=?",
                           (*values, state, row["id"]))
            else:
                db.execute("UPDATE messages SET attempts=?, next_attempt=?, state=?, last_error=? WHERE message_id=?",
                           (*values, row["message_id"]))
        return attempts
