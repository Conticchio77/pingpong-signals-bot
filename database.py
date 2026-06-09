import sqlite3
import os
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "pingpong.db")


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY,
                username TEXT,
                balance  REAL NOT NULL DEFAULT 100.0
            );

            CREATE TABLE IF NOT EXISTS matches (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                player1  TEXT NOT NULL,
                player2  TEXT NOT NULL,
                odds1    REAL NOT NULL,
                odds2    REAL NOT NULL,
                status   TEXT NOT NULL DEFAULT 'open',   -- open | closed
                winner   INTEGER                          -- 1 | 2 | NULL
            );

            CREATE TABLE IF NOT EXISTS bets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                match_id     INTEGER NOT NULL,
                player_num   INTEGER NOT NULL,  -- 1 | 2
                amount       REAL NOT NULL,
                odds         REAL NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',  -- pending | won | lost
                FOREIGN KEY(user_id)  REFERENCES users(id),
                FOREIGN KEY(match_id) REFERENCES matches(id)
            );
        """)
        self.conn.commit()

    # ── Users ──────────────────────────────────────────────────────────────────
    def upsert_user(self, user_id: int, username: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)",
            (user_id, username)
        )
        self.conn.execute(
            "UPDATE users SET username=? WHERE id=?",
            (username, user_id)
        )
        self.conn.commit()

    def get_balance(self, user_id: int) -> float:
        row = self.conn.execute(
            "SELECT balance FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row is None:
            return 0.0
        return row["balance"]

    def deduct_credits(self, user_id: int, amount: float) -> bool:
        """Returns True if deduction succeeded (enough balance)."""
        row = self.conn.execute(
            "SELECT balance FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row is None or row["balance"] < amount:
            return False
        self.conn.execute(
            "UPDATE users SET balance = balance - ? WHERE id=?",
            (amount, user_id)
        )
        self.conn.commit()
        return True

    def add_credits(self, user_id: int, amount: float):
        self.conn.execute(
            "UPDATE users SET balance = balance + ? WHERE id=?",
            (amount, user_id)
        )
        self.conn.commit()

    def get_leaderboard(self):
        rows = self.conn.execute(
            "SELECT username, balance FROM users ORDER BY balance DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Matches ────────────────────────────────────────────────────────────────
    def add_match(self, p1: str, p2: str, o1: float, o2: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO matches (player1, player2, odds1, odds2) VALUES (?,?,?,?)",
            (p1, p2, o1, o2)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_open_matches(self):
        rows = self.conn.execute(
            "SELECT * FROM matches WHERE status='open'"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_match(self, match_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM matches WHERE id=?", (match_id,)
        ).fetchone()
        return dict(row) if row else None

    def close_match(self, match_id: int, winner: int):
        self.conn.execute(
            "UPDATE matches SET status='closed', winner=? WHERE id=?",
            (winner, match_id)
        )
        self.conn.commit()

    # ── Bets ───────────────────────────────────────────────────────────────────
    def place_bet(self, user_id: int, match_id: int, player_num: int,
                  amount: float, odds: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO bets (user_id, match_id, player_num, amount, odds) VALUES (?,?,?,?,?)",
            (user_id, match_id, player_num, amount, odds)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_bets_for_match(self, match_id: int):
        rows = self.conn.execute(
            "SELECT * FROM bets WHERE match_id=? AND status='pending'",
            (match_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def settle_bet(self, bet_id: int, status: str):
        self.conn.execute(
            "UPDATE bets SET status=? WHERE id=?",
            (status, bet_id)
        )
        self.conn.commit()

    def get_user_bets(self, user_id: int):
        rows = self.conn.execute(
            """
            SELECT b.*, m.player1, m.player2,
                   CASE b.player_num WHEN 1 THEN m.player1 ELSE m.player2 END AS chosen_player
            FROM bets b
            JOIN matches m ON b.match_id = m.id
            WHERE b.user_id = ?
            ORDER BY b.id DESC
            """,
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
