import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "signals.db")


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key    TEXT UNIQUE,
                match        TEXT,
                player1      TEXT,
                player2      TEXT,
                tournament   TEXT,
                kickoff      TEXT,
                signal_type  TEXT,
                pick         TEXT,
                odds         REAL,
                confidence   INTEGER,
                value_pct    REAL,
                stake        INTEGER,
                reasoning    TEXT,
                book_note    TEXT,
                source       TEXT DEFAULT 'n/d',
                sport        TEXT DEFAULT 'tabletennis',
                sport_label  TEXT DEFAULT '🏓 Ping Pong',
                status       TEXT DEFAULT 'pending',
                result       TEXT,
                created_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # Aggiungi colonne mancanti se il DB esiste già (upgrade sicuro)
        for col, definition in [
            ("book_note",   "TEXT"),
            ("source",      "TEXT DEFAULT 'n/d'"),
            ("sport",       "TEXT DEFAULT 'tabletennis'"),
            ("sport_label", "TEXT DEFAULT '🏓 Ping Pong'"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
                self.conn.commit()
            except Exception:
                pass  # colonna già esistente

        defaults = {
            "scan_interval":  "1",
            "auto_send":      "0",
            "min_confidence": "55",
            "last_scan":      "mai",
            "sport_filter":   "both",   # "both" | "tabletennis" | "tennis"
        }
        for k, v in defaults.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        self.conn.commit()

    # ── Signals ────────────────────────────────────────────────────────────────
    def save_signal(self, s: dict) -> int:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO signals
               (match_key, match, player1, player2, tournament, kickoff,
                signal_type, pick, odds, confidence, value_pct, stake,
                reasoning, book_note, source, sport, sport_label, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s["match_key"], s["match"], s["player1"], s["player2"],
                s.get("tournament", ""), s["kickoff"],
                s["signal_type"], s["pick"], s["odds"],
                s["confidence"], s["value_pct"], s["stake"],
                s.get("reasoning", ""), s.get("book_note", ""),
                s.get("source", "n/d"),
                s.get("sport", "tabletennis"),
                s.get("sport_label", "🏓 Ping Pong"),
                s.get("created_at", datetime.utcnow().isoformat()),
            )
        )
        self.conn.commit()
        return cur.lastrowid

    def signal_exists(self, match_key: str) -> bool:
        row = self.conn.execute(
            "SELECT id FROM signals WHERE match_key=?", (match_key,)
        ).fetchone()
        return row is not None

    def get_signal(self, sig_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM signals WHERE id=?", (sig_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_pending_signals(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE status='pending' ORDER BY value_pct DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_signal_status(self, sig_id: int, status: str, result: str = None):
        self.conn.execute(
            "UPDATE signals SET status=?, result=? WHERE id=?",
            (status, result, sig_id)
        )
        self.conn.commit()

    def get_stats(self) -> dict:
        total     = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        sent      = self.conn.execute("SELECT COUNT(*) FROM signals WHERE status='sent'").fetchone()[0]
        pending   = self.conn.execute("SELECT COUNT(*) FROM signals WHERE status='pending'").fetchone()[0]
        discarded = self.conn.execute("SELECT COUNT(*) FROM signals WHERE status='discarded'").fetchone()[0]
        won       = self.conn.execute("SELECT COUNT(*) FROM signals WHERE result='won'").fetchone()[0]
        lost      = self.conn.execute("SELECT COUNT(*) FROM signals WHERE result='lost'").fetchone()[0]

        total_results = won + lost
        winrate = round(won / total_results * 100, 1) if total_results > 0 else 0

        roi_rows         = self.conn.execute("SELECT odds, stake FROM signals WHERE result='won'").fetchall()
        total_won_profit = sum((r["odds"] - 1) * r["stake"] for r in roi_rows)
        total_stake_all  = self.conn.execute(
            "SELECT COALESCE(SUM(stake),0) FROM signals WHERE result IN ('won','lost')"
        ).fetchone()[0]
        roi = round(total_won_profit / total_stake_all * 100, 1) if total_stake_all > 0 else 0

        return {
            "total":     total,
            "sent_vip":  sent,
            "pending":   pending,
            "discarded": discarded,
            "won":       won,
            "lost":      lost,
            "winrate":   winrate,
            "roi":       roi,
            "last_scan": self.get_settings()["last_scan"],
        }

    def purge_old_signals(self) -> int:
        """Cancella segnali già risolti (vinti/persi/scartati). Ritorna il numero eliminato."""
        cur = self.conn.execute(
            "DELETE FROM signals WHERE status IN ('won', 'lost', 'discarded')"
        )
        self.conn.commit()
        return cur.rowcount

    def reset_results(self):
        """Azzera tutti i risultati (vinto/perso) senza cancellare i segnali."""
        self.conn.execute("UPDATE signals SET result=NULL WHERE result IN ('won','lost')")
        self.conn.execute("UPDATE signals SET status='seen' WHERE status IN ('won','lost')")
        self.conn.commit()

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        """Ultimi N segnali ordinati per kickoff crescente (più vicino prima)."""
        rows = self.conn.execute(
            "SELECT * FROM signals ORDER BY kickoff ASC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Settings ───────────────────────────────────────────────────────────────
    def get_settings(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        raw  = {r["key"]: r["value"] for r in rows}
        return {
            "scan_interval":  int(raw.get("scan_interval", 1)),
            "auto_send":      raw.get("auto_send", "0") == "1",
            "min_confidence": int(raw.get("min_confidence", 60)),
            "last_scan":      raw.get("last_scan", "mai"),
            "sport_filter":   raw.get("sport_filter", "both"),
        }

    def set_setting(self, key: str, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )
        self.conn.commit()

    def toggle_setting(self, key: str):
        current = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        new_val = "0" if (current and current["value"] == "1") else "1"
        self.set_setting(key, new_val)

    def get_signals_for_auto_result(self) -> list[dict]:
        """Segnali pendenti/visti che potrebbero avere un risultato da aggiornare."""
        rows = self.conn.execute(
            """SELECT * FROM signals
               WHERE status IN ('pending','seen','sent')
               AND result IS NULL""",
        ).fetchall()
        return [dict(r) for r in rows]

    def auto_update_result(self, sig_id: int, result: str) -> bool:
        """Aggiorna automaticamente il risultato. Ritorna True se aggiornato."""
        self.conn.execute(
            "UPDATE signals SET status=?, result=? WHERE id=? AND result IS NULL",
            (result, result, sig_id)
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT changes()"
        ).fetchone()[0] > 0
