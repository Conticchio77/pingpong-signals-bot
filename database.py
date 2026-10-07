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
            ("sent_to_vip",     "INTEGER DEFAULT 0"),   # inviato al gruppo VIP
            ("sent_to_free",    "INTEGER DEFAULT 0"),   # inviato al gruppo FREE
            ("result_reminded", "INTEGER DEFAULT 0"),   # promemoria "risultato mancante" già mandato
        ]:
            try:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
                self.conn.commit()
            except Exception:
                pass  # colonna già esistente

        # Bilanci separati per sport: balance_history non aveva una colonna
        # sport. La aggiungiamo e la ripopoliamo dal segnale collegato
        # (sig_id → signals.sport) per le righe già esistenti.
        try:
            self.conn.execute("ALTER TABLE balance_history ADD COLUMN sport TEXT")
            self.conn.commit()
            self.conn.execute("""
                UPDATE balance_history
                SET sport = (SELECT sport FROM signals WHERE signals.id = balance_history.sig_id)
                WHERE sport IS NULL
            """)
            self.conn.commit()
        except Exception:
            pass  # colonna già esistente

        defaults = {
            "scan_interval":    "3",
            "auto_send":        "0",
            "min_confidence":   "55",
            # Soglia separata per il ping pong: senza Pinnacle/sharp su
            # OddsPapi, la confidenza dei segnali consenso è tappata a ~70
            # (vedi _confidence in ai_analyzer.py) — con la stessa soglia
            # del tennis il ping pong non genera quasi mai nulla.
            "min_confidence_pp": "60",
            "last_scan":        "mai",
            "sport_filter":     "both",
            "unit_value":       "10",   # legacy (€), non più usato per i calcoli dopo il passaggio a %
            "stake_pct":        "0.5",  # % di bankroll puntata su ogni segnale (flat, non scalata su 1-5)
            # Destinazione invii, separata per sport: "vip" / "free" / "both"
            "send_dest_tennis":   "vip",
            "send_dest_pingpong": "vip",
            "tt_sport_id":      "",
            "min_hours_before": "1.0",   # ore minime al kickoff
            "max_edge_no_sharp":"20.0",  # cap edge% senza Pinnacle
            "min_value_pct":    "3.0",   # % minimo di edge per generare un segnale (era fisso al 5.0)
            "pingpong_scan_interval": "12",  # ore tra uno scan ping pong e il successivo (12 = 2 scan/giorno, 07:00 e 19:00)
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

    def mark_signal_sent(self, sig_id: int, vip: bool, free: bool):
        """Segna il segnale come inviato e ricorda A QUALE gruppo (VIP/FREE).
        Non declassa uno stato won/lost già assegnato."""
        self.conn.execute(
            """UPDATE signals
               SET status = CASE WHEN status IN ('won','lost') THEN status ELSE 'sent' END,
                   sent_to_vip  = MAX(COALESCE(sent_to_vip, 0),  ?),
                   sent_to_free = MAX(COALESCE(sent_to_free, 0), ?)
               WHERE id = ?""",
            (1 if vip else 0, 1 if free else 0, sig_id)
        )
        self.conn.commit()

    def mark_result_reminded(self, sig_ids: list):
        if not sig_ids:
            return
        marks = ",".join("?" * len(sig_ids))
        self.conn.execute(f"UPDATE signals SET result_reminded=1 WHERE id IN ({marks})", list(sig_ids))
        self.conn.commit()

    def get_stats(self) -> dict:
        total     = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        sent      = self.conn.execute("SELECT COUNT(*) FROM signals WHERE status='sent'").fetchone()[0]
        pending   = self.conn.execute("SELECT COUNT(*) FROM signals WHERE status='pending'").fetchone()[0]
        discarded = self.conn.execute("SELECT COUNT(*) FROM signals WHERE status='discarded'").fetchone()[0]
        won       = self.conn.execute("SELECT COUNT(*) FROM signals WHERE result='won'").fetchone()[0]
        lost      = self.conn.execute("SELECT COUNT(*) FROM signals WHERE result='lost'").fetchone()[0]
        sent_to_vip  = self.conn.execute("SELECT COALESCE(SUM(sent_to_vip),0)  FROM signals").fetchone()[0]
        sent_to_free = self.conn.execute("SELECT COALESCE(SUM(sent_to_free),0) FROM signals").fetchone()[0]

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
                      SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) AS lost,
                      COALESCE(SUM(sent_to_vip),0)  AS sent_vip,
                      COALESCE(SUM(sent_to_free),0) AS sent_free
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
                "sent_vip":    r["sent_vip"] or 0,
                "sent_free":   r["sent_free"] or 0,
            }

        return {
            "total":     total,
            "sent_vip":  sent,            # legacy: segnali ancora in stato 'sent'
            "sent_to_vip":  sent_to_vip,   # inviati al gruppo VIP (cumulativo)
            "sent_to_free": sent_to_free,  # inviati al gruppo FREE (cumulativo)
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
        """Azzera tutti i risultati (vinto/perso) senza cancellare i segnali.
        FIX: prima non svuotava balance_history — dopo il passaggio da stake
        in € a stake in % di bankroll, i vecchi importi in € sarebbero
        rimasti mescolati nella stessa tabella con i nuovi valori in %,
        corrompendo bilancio/ROI. Il reset ora pulisce anche quello."""
        self.conn.execute("UPDATE signals SET result=NULL WHERE result IN ('won','lost')")
        self.conn.execute("UPDATE signals SET status='seen' WHERE status IN ('won','lost')")
        self.conn.execute("DELETE FROM balance_history")
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
            "min_confidence_pp": int(raw.get("min_confidence_pp", 60)),
            "stake_pct":        float(raw.get("stake_pct", 0.5)),
            "send_dest_tennis":   raw.get("send_dest_tennis", "vip"),
            "send_dest_pingpong": raw.get("send_dest_pingpong", "vip"),
            "last_scan":        raw.get("last_scan", "mai"),
            "sport_filter":     raw.get("sport_filter", "both"),
            "unit_value":       float(raw.get("unit_value", 10)),
            "min_hours_before": float(raw.get("min_hours_before", 1.0)),
            "max_edge_no_sharp":float(raw.get("max_edge_no_sharp", 20.0)),
            "min_value_pct":    float(raw.get("min_value_pct", 3.0)),
            "pingpong_scan_interval": int(raw.get("pingpong_scan_interval", 24)),
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

    def increment_api_calls(self, api_name: str, by: int = 1) -> int:
        """Incrementa e ritorna il contatore mensile di chiamate reali per
        un'API esterna (es. "oddspapi"), con reset automatico ad ogni nuovo
        mese solare. Tracciato da noi indipendentemente da cosa riporta (o
        non riporta) l'header di rate-limit dell'API — serve per il bottone
        "Quota" nel pannello admin."""
        period_key = f"{api_name}_calls_period"
        count_key  = f"{api_name}_calls_count"
        cur_period = datetime.now().strftime("%Y-%m")
        stored_period = self.get_setting(period_key, "")
        if stored_period != cur_period:
            count = 0
            self.set_setting(period_key, cur_period)
        else:
            count = int(self.get_setting(count_key, "0") or 0)
        count += by
        self.set_setting(count_key, count)
        return count

    def get_api_calls_this_month(self, api_name: str) -> int:
        period_key = f"{api_name}_calls_period"
        count_key  = f"{api_name}_calls_count"
        if self.get_setting(period_key, "") != datetime.now().strftime("%Y-%m"):
            return 0
        return int(self.get_setting(count_key, "0") or 0)

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
    # FIX: passaggio da stake fisso in € (1-5 unità × unit_value) a stake
    # flat in % di bankroll (stake_pct, uguale per ogni segnale indipendente
    # dal rating 1-5, che resta solo un'indicazione di confidenza sul
    # segnale). "profit" e "balance" ora sono in punti percentuali di
    # bankroll, non più €. Aggiunta anche la colonna sport per poter
    # calcolare bilanci separati per sport oltre al totale.
    def record_balance_entry(self, sig: dict, result: str, stake_pct: float = None):
        """Registra un'entry nel bilancio (in % di bankroll) dopo un risultato."""
        if stake_pct is None:
            stake_pct = self.get_settings().get("stake_pct", 0.5)
        stake  = sig.get("stake", 1)  # rating 1-5, solo informativo, non incide più sul calcolo
        odds   = sig.get("odds", 1.0)
        sport  = sig.get("sport", "tabletennis")
        if result == "won":
            profit = round((odds - 1) * stake_pct, 4)
        else:
            profit = round(-stake_pct, 4)

        # Balance "globale" (tutti gli sport) — mantenuto per compatibilità
        # col grafico storico complessivo.
        prev = self.conn.execute(
            "SELECT COALESCE(SUM(profit), 0) FROM balance_history"
        ).fetchone()[0]
        balance = round(float(prev) + profit, 4)

        self.conn.execute(
            """INSERT INTO balance_history
               (sig_id, match, pick, odds, stake, result, profit, balance, sport, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                sig.get("id"), sig.get("match",""), sig.get("pick",""),
                odds, stake, result, profit, balance, sport,
                datetime.utcnow().isoformat()
            )
        )
        self.conn.commit()

    def get_balance_history(self, limit: int = 50, sport: str | None = None) -> list[dict]:
        """Risultati per il grafico bilancio. sport=None → tutti gli sport insieme."""
        if sport:
            rows = self.conn.execute(
                "SELECT * FROM balance_history WHERE sport=? ORDER BY id ASC", (sport,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM balance_history ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_balance_stats(self, stake_pct: float = None, sport: str | None = None) -> dict:
        """Statistiche bilancio in % di bankroll. sport=None → totale su
        entrambi gli sport; sport="tennis"/"tabletennis" → solo quello."""
        if stake_pct is None:
            stake_pct = self.get_settings().get("stake_pct", 0.5)
        rows = self.get_balance_history(sport=sport)
        if not rows:
            return {
                "total_bets": 0, "won": 0, "lost": 0,
                "winrate": 0, "profit_pct": 0.0, "roi": 0.0,
                "best_win_pct": 0.0, "worst_loss_pct": 0.0,
                "current_balance_pct": 0.0, "stake_pct": stake_pct,
            }
        won   = sum(1 for r in rows if r["result"] == "won")
        lost  = sum(1 for r in rows if r["result"] == "lost")
        total = won + lost
        profit_pct = sum(r["profit"] for r in rows)
        total_staked_pct = total * stake_pct  # stake flat: n° giocate × stake_pct
        roi = round(profit_pct / total_staked_pct * 100, 1) if total_staked_pct else 0
        return {
            "total_bets":           total,
            "won":                  won,
            "lost":                 lost,
            "winrate":              round(won / total * 100, 1) if total else 0,
            "profit_pct":           round(profit_pct, 2),
            "roi":                  roi,
            "best_win_pct":         round(max((r["profit"] for r in rows if r["result"]=="won"), default=0), 2),
            "worst_loss_pct":       round(min((r["profit"] for r in rows if r["result"]=="lost"), default=0), 2),
            # Somma cumulativa ricalcolata sul sottoinsieme filtrato (non la
            # colonna "balance", che è sempre la cumulata GLOBALE).
            "current_balance_pct":  round(sum(r["profit"] for r in rows), 2),
            "stake_pct":            stake_pct,
        }

    def purge_all_signals(self) -> int:
        """Cancella TUTTI i segnali, statistiche e bilancio."""
        self.conn.execute("DELETE FROM signals")
        self.conn.execute("DELETE FROM balance_history")
        cur = self.conn.execute("SELECT changes()")
        self.conn.commit()
        return cur.fetchone()[0]
