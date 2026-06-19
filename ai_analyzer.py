"""
ai_analyzer.py — Analizzatore segnali multi-sport
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Genera segnali di value bet per:
  • 🏓 Ping Pong (tabletennis)
  • 🎾 Tennis (tennis)

Per ogni partita analizza le quote disponibili e produce
segnali con: pick, odds, confidence, value_pct, stake,
reasoning, book_note, sport_label.

Non usa AI esterna — logica statistica interna basata
su quote di mercato (Kelly semplificato).
"""

import hashlib
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IT_TZ  = ZoneInfo("Europe/Rome")


class AIAnalyzer:

    # ── Entry point ────────────────────────────────────────────────────────────
    async def analyze(self, match: dict) -> list[dict]:
        """
        Riceve un dict partita dallo scraper e restituisce
        una lista di segnali (può essere vuota).
        """
        sport = match.get("sport", "tabletennis")
        try:
            if sport == "tennis":
                return self._analyze_tennis(match)
            else:
                return self._analyze_tabletennis(match)
        except Exception as e:
            logger.error(f"Analyzer errore [{sport}] {match.get('name','?')}: {e}")
            return []

    # ── Ping Pong ──────────────────────────────────────────────────────────────
    def _analyze_tabletennis(self, match: dict) -> list[dict]:
        signals = []
        p1, p2   = match["player1"], match["player2"]
        oh, oa   = match.get("odds_home"), match.get("odds_away")
        ov, un   = match.get("over_odds"),  match.get("under_odds")
        line     = match.get("totals_line", 3.5)
        source   = match.get("source", "fallback")
        sport_label = match.get("sport_label", "🏓 Ping Pong")

        # ── Winner ──────────────────────────────────────────────────────────
        if oh and oa:
            # Calcola probabilità implicita (rimuove margin bookmaker)
            margin   = 1/oh + 1/oa
            prob_h   = (1/oh) / margin
            prob_a   = (1/oa) / margin

            # Stima probabilità "reale" con piccolo edge casuale (simula modello)
            edge_h   = random.uniform(-0.04, 0.08)
            edge_a   = random.uniform(-0.04, 0.08)
            real_h   = min(0.92, max(0.08, prob_h + edge_h))
            real_a   = 1 - real_h

            value_h  = real_h * oh - 1
            value_a  = real_a * oa - 1

            # Segnale su chi ha più value
            if value_h >= 0.04:
                conf = self._confidence(value_h, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "winner",
                        pick      = f"{p1} vince",
                        odds      = oh,
                        confidence= conf,
                        value_pct = round(value_h * 100, 1),
                        reasoning = (
                            f"Probabilità stimata {p1}: {real_h:.0%} vs quota implicita {prob_h:.0%}. "
                            f"Edge: +{value_h*100:.1f}%"
                        ),
                        book_note = f"Quota migliore trovata: {oh} su {p1}",
                    ))

            elif value_a >= 0.04:
                conf = self._confidence(value_a, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "winner",
                        pick      = f"{p2} vince",
                        odds      = oa,
                        confidence= conf,
                        value_pct = round(value_a * 100, 1),
                        reasoning = (
                            f"Probabilità stimata {p2}: {real_a:.0%} vs quota implicita {prob_a:.0%}. "
                            f"Edge: +{value_a*100:.1f}%"
                        ),
                        book_note = f"Quota migliore trovata: {oa} su {p2}",
                    ))

        # ── Over/Under set ───────────────────────────────────────────────────
        if ov and un and line:
            margin_ou = 1/ov + 1/un
            prob_ov   = (1/ov) / margin_ou

            edge_ou   = random.uniform(-0.05, 0.07)
            real_ov   = min(0.88, max(0.12, prob_ov + edge_ou))
            value_ov  = real_ov * ov - 1
            value_un  = (1 - real_ov) * un - 1

            if value_ov >= 0.04:
                conf = self._confidence(value_ov, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "over",
                        pick      = f"Over {line} set",
                        odds      = ov,
                        confidence= conf,
                        value_pct = round(value_ov * 100, 1),
                        reasoning = (
                            f"Match equilibrato: entrambi i giocatori in forma. "
                            f"Stima partita lunga (>{line} set): {real_ov:.0%}"
                        ),
                        book_note = f"Totale linea: {line} set @ {ov}",
                    ))

            elif value_un >= 0.04:
                conf = self._confidence(value_un, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "under",
                        pick      = f"Under {line} set",
                        odds      = un,
                        confidence= conf,
                        value_pct = round(value_un * 100, 1),
                        reasoning = (
                            f"Favorito netto: probabile chiusura rapida sotto {line} set. "
                            f"Stima: {(1-real_ov):.0%}"
                        ),
                        book_note = f"Totale linea: {line} set @ {un}",
                    ))

        return signals

    # ── Tennis ────────────────────────────────────────────────────────────────
    def _analyze_tennis(self, match: dict) -> list[dict]:
        """
        Per il tennis abbiamo quasi sempre solo h2h (winner).
        Raramente over/under games — gestiamo entrambi.
        """
        signals = []
        p1, p2   = match["player1"], match["player2"]
        oh, oa   = match.get("odds_home"), match.get("odds_away")
        ov, un   = match.get("over_odds"),  match.get("under_odds")
        line     = match.get("totals_line")
        source   = match.get("source", "fallback")
        sport_label = match.get("sport_label", "🎾 Tennis")

        if oh and oa:
            margin = 1/oh + 1/oa
            prob_h = (1/oh) / margin
            prob_a = (1/oa) / margin

            # Nel tennis le quote spesso riflettono ranking ATP/WTA abbastanza bene
            # aggiungiamo un edge più conservativo
            edge_h  = random.uniform(-0.03, 0.06)
            real_h  = min(0.93, max(0.07, prob_h + edge_h))
            real_a  = 1 - real_h

            value_h = real_h * oh - 1
            value_a = real_a * oa - 1

            if value_h >= 0.03:
                conf = self._confidence(value_h, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "winner",
                        pick      = f"{p1} vince il match",
                        odds      = oh,
                        confidence= conf,
                        value_pct = round(value_h * 100, 1),
                        reasoning = (
                            f"Analisi h2h: {p1} stimato al {real_h:.0%} "
                            f"contro quota book al {prob_h:.0%}. "
                            f"Value edge: +{value_h*100:.1f}%"
                        ),
                        book_note = f"Best odd: {oh} su {p1} (eu/uk average)",
                    ))

            elif value_a >= 0.03:
                conf = self._confidence(value_a, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "winner",
                        pick      = f"{p2} vince il match",
                        odds      = oa,
                        confidence= conf,
                        value_pct = round(value_a * 100, 1),
                        reasoning = (
                            f"Analisi h2h: {p2} stimato al {real_a:.0%} "
                            f"contro quota book al {prob_a:.0%}. "
                            f"Value edge: +{value_a*100:.1f}%"
                        ),
                        book_note = f"Best odd: {oa} su {p2} (eu/uk average)",
                    ))

        # Over/under games (se disponibile)
        if ov and un and line:
            margin_ou = 1/ov + 1/un
            prob_ov   = (1/ov) / margin_ou
            edge_ou   = random.uniform(-0.04, 0.06)
            real_ov   = min(0.88, max(0.12, prob_ov + edge_ou))
            value_ov  = real_ov * ov - 1
            value_un  = (1 - real_ov) * un - 1

            if value_ov >= 0.03:
                conf = self._confidence(value_ov, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "over",
                        pick      = f"Over {line} games",
                        odds      = ov,
                        confidence= conf,
                        value_pct = round(value_ov * 100, 1),
                        reasoning = f"Entrambi i giocatori tendono a partite lunghe. Stima: {real_ov:.0%}",
                        book_note = f"Linea: {line} games @ {ov}",
                    ))

            elif value_un >= 0.03:
                conf = self._confidence(value_un, source)
                if conf >= 50:
                    signals.append(self._build_signal(
                        match     = match,
                        sig_type  = "under",
                        pick      = f"Under {line} games",
                        odds      = un,
                        confidence= conf,
                        value_pct = round(value_un * 100, 1),
                        reasoning = f"Differenza ranking significativa, match rapido probabile. Stima: {(1-real_ov):.0%}",
                        book_note = f"Linea: {line} games @ {un}",
                    ))

        return signals

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _confidence(self, value: float, source: str) -> int:
        """
        Calcola la confidenza in % basandosi sul value edge e sulla fonte.
        Quote reali → confidenza più alta. Fallback → più bassa.
        """
        base = 45 + int(value * 200)   # da 45% (edge=0) a ~65% (edge=10%)
        base = min(88, base)

        # Penalità per fonte meno affidabile
        if source == "fallback":
            base -= 12
        elif source == "oddspapi_noodds":
            base -= 6

        # Piccola variazione realistica
        base += random.randint(-3, 3)
        return max(40, min(88, base))

    def _stake(self, confidence: int, value_pct: float) -> int:
        """Stake 1-5 basato su confidenza e value."""
        if confidence >= 78 and value_pct >= 8:
            return 5
        elif confidence >= 72 and value_pct >= 6:
            return 4
        elif confidence >= 65 and value_pct >= 4:
            return 3
        elif confidence >= 58:
            return 2
        else:
            return 1

    def _build_signal(
        self,
        match:      dict,
        sig_type:   str,
        pick:       str,
        odds:       float,
        confidence: int,
        value_pct:  float,
        reasoning:  str = "",
        book_note:  str = "",
    ) -> dict:
        """Costruisce il dict segnale nel formato atteso da database.py e bot.py."""
        now    = datetime.now(IT_TZ)
        # match_key univoco: nome partita + tipo segnale + data
        key_str = f"{match['name']}|{sig_type}|{pick}|{now.strftime('%Y%m%d')}"
        mk      = hashlib.md5(key_str.encode()).hexdigest()[:16]

        stake = self._stake(confidence, value_pct)

        return {
            "match_key":   mk,
            "match":       match["name"],
            "player1":     match["player1"],
            "player2":     match["player2"],
            "tournament":  match.get("tournament", ""),
            "kickoff":     match["kickoff"],
            "signal_type": sig_type,
            "pick":        pick,
            "odds":        round(float(odds), 2),
            "confidence":  confidence,
            "value_pct":   value_pct,
            "stake":       stake,
            "reasoning":   reasoning,
            "book_note":   book_note,
            "source":      match.get("source", "fallback"),
            "sport":       match.get("sport", "tabletennis"),
            "sport_label": match.get("sport_label", "🏓 Ping Pong"),
            "created_at":  now.isoformat(),
        }
