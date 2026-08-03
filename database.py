import sqlite3
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "signals.db")


class Database:
    def __init__(self):
        is_persistent = os.path.isabs(DB_PATH) and DB_PATH != "signals.db"
        logger.info(
            f"💾 DB in uso: {DB_PATH} "
            f"({'persistente su volume' if is_persistent else '⚠️ ATTENZIONE: path relativo, probabilmente NON persistente tra i redeploy'})"
        )
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

            CREATE TABLE IF NOT EXISTS balance_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sig_id     INTEGER,
                match      TEXT,
                pick       TEXT,
                odds       REAL,
                stake      INTEGER,
                result     TEXT,
                profit     REAL,
                balance    REAL,
                created_at TEXT
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
            "scan_interval":    "3",
            "auto_send":        "0",
            "min_confidence":   "55",
            "last_scan":        "mai",
            "sport_filter":     "both",
            "unit_value":       "10",
            "tt_sport_id":      "",
            "min_hours_before": "1.0",   # ore minime al kickoff
            "max_edge_no_sharp":"20.0",  # cap edge% senza Pinnacle
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

        # ── Breakdown per sport (ping pong vs tennis) ────────────────────────
        by_sport = {}
        sport_rows = self.conn.execute(
            """SELECT sport, sport_label,
                      COUNT(*) AS total,
                      SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                      SUM(CASE WHEN result='won'  THEN 1 ELSE 0 END) AS won,
                      SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) AS lost
               FROM signals GROUP BY sport"""
        ).fetchall()
        for r in sport_rows:
            s_won, s_lost = r["won"] or 0, r["lost"] or 0
            s_total_res = s_won + s_lost
            by_sport[r["sport"] or "n/d"] = {
                "sport_label": r["sport_label"] or r["sport"] or "n/d",
                "total":       r["total"] or 0,
                "pending":     r["pending"] or 0,
                "won":         s_won,
                "lost":        s_lost,
                "winrate":     round(s_won / s_total_res * 100, 1) if s_total_res > 0 else 0,
            }

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
            "by_sport":  by_sport,
        }

    def purge_old_signals(self) -> int:
        """Cancella segnali già risolti (vinti/persi/scartati). Ritorna il numero eliminato."""
        cur = self.conn.execute(
            "DELETE FROM signals WHERE status IN ('won', 'lost', 'discarded')"
        )
        self.conn.commit()
        return cur.rowcount

    def purge_all_signals(self) -> int:
        """Cancella TUTTI i segnali e azzera le statistiche."""
        cur = self.conn.execute("DELETE FROM signals")
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
            "scan_interval":    int(raw.get("scan_interval", 1)),
            "auto_send":        raw.get("auto_send", "0") == "1",
            "min_confidence":   int(raw.get("min_confidence", 60)),
            "last_scan":        raw.get("last_scan", "mai"),
            "sport_filter":     raw.get("sport_filter", "both"),
            "unit_value":       float(raw.get("unit_value", 10)),
            "min_hours_before": float(raw.get("min_hours_before", 1.0)),
            "max_edge_no_sharp":float(raw.get("max_edge_no_sharp", 20.0)),
        }

    def get_setting(self, key: str, default=None):
        """Legge una singola chiave arbitraria dalla tabella settings (non solo quelle note)."""
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

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

    # ── Cache sport ID OddsPapi (evita richieste /sports ripetute) ──────────────
    def get_tt_sport_id(self) -> Optional[int]:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key='tt_sport_id'"
        ).fetchone()
        if row and row["value"]:
            try:
                return int(row["value"])
            except (TypeError, ValueError):
                return None
        return None

    def set_tt_sport_id(self, sport_id: int):
        self.set_setting("tt_sport_id", str(sport_id))

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

    # ── Balance History ────────────────────────────────────────────────────────
    def record_balance_entry(self, sig: dict, result: str, unit_value: float = None):
        """Registra un'entry nel bilancio dopo un risultato."""
        if unit_value is None:
            unit_value = self.get_settings().get("unit_value", 10.0)
        stake  = sig.get("stake", 1)
        odds   = sig.get("odds", 1.0)
        if result == "won":
            profit = round((odds - 1) * stake * unit_value, 2)
        else:
            profit = round(-stake * unit_value, 2)

        # Calcola balance corrente = somma di tutti i profit precedenti + questo
        prev = self.conn.execute(
            "SELECT COALESCE(SUM(profit), 0) FROM balance_history"
        ).fetchone()[0]
        balance = round(float(prev) + profit, 2)

        self.conn.execute(
            """INSERT INTO balance_history
               (sig_id, match, pick, odds, stake, result, profit, balance, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                sig.get("id"), sig.get("match",""), sig.get("pick",""),
                odds, stake, result, profit, balance,
                datetime.utcnow().isoformat()
            )
        )
        self.conn.commit()

    def get_balance_history(self, limit: int = 50) -> list[dict]:
        """Ultimi N risultati per il grafico bilancio."""
        rows = self.conn.execute(
            "SELECT * FROM balance_history ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_balance_stats(self, unit_value: float = None) -> dict:
        """Statistiche complete per il pannello bilancio."""
        if unit_value is None:
            unit_value = self.get_settings().get("unit_value", 10.0)
        rows = self.get_balance_history()
        if not rows:
            return {
                "total_bets": 0, "won": 0, "lost": 0,
                "winrate": 0, "profit": 0.0, "roi": 0.0,
                "best_win": 0.0, "worst_loss": 0.0,
                "current_balance": 0.0, "unit_value": unit_value,
            }
        won   = sum(1 for r in rows if r["result"] == "won")
        lost  = sum(1 for r in rows if r["result"] == "lost")
        total = won + lost
        profit = sum(r["profit"] for r in rows)
        total_staked = sum(r["stake"] * unit_value for r in rows)
        roi = round(profit / total_staked * 100, 1) if total_staked else 0
        return {
            "total_bets":       total,
            "won":              won,
            "lost":             lost,
            "winrate":          round(won / total * 100, 1) if total else 0,
            "profit":           round(profit, 2),
            "roi":              roi,
            "best_win":         round(max((r["profit"] for r in rows if r["result"]=="won"), default=0), 2),
            "worst_loss":       round(min((r["profit"] for r in rows if r["result"]=="lost"), default=0), 2),
            "current_balance":  round(rows[-1]["balance"] if rows else 0, 2),
            "unit_value":       unit_value,
        }

    def purge_all_signals(self) -> int:
        """Cancella TUTTI i segnali, statistiche e bilancio."""
        self.conn.execute("DELETE FROM signals")
        self.conn.execute("DELETE FROM balance_history")
        cur = self.conn.execute("SELECT changes()")
        self.conn.commit()
        return cur.fetchone()[0]
