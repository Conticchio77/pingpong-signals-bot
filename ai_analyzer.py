"""
ai_analyzer.py — Analisi con de-vig Pinnacle
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Metodo: Power De-vig su Pinnacle (o miglior sharp disponibile)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Come funziona:
  1. Dalla risposta OddsPapi/Odds API prende le quote di OGNI bookmaker
  2. Identifica Pinnacle (o Singbet/SBOBet) come "sharp book"
  3. Applica Power De-vig su Pinnacle → ottiene probabilità reale senza margine
  4. Confronta probabilità reale con quote dei "soft book" (Bet365, Unibet, ecc.)
  5. Se la quota soft è MAGGIORE del fair value → c'è value reale
  6. Calcola Kelly fraction per lo stake

Campi extra attesi nel dict match (provenienti da scraper.py):
  raw_bookmakers: dict  → {"pinnacle": {"home": 1.85, "away": 2.10}, "bet365": {...}, ...}
  Se assente usa le odds_home/odds_away già mediate come fallback.
"""

import hashlib
import logging
import math
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IT_TZ  = ZoneInfo("Europe/Rome")

# ── Bookmaker classificati per affidabilità ────────────────────────────────────
SHARP_BOOKS = {
    "pinnacle", "pinnaclesports", "pin",
    "singbet", "crown",
    "sbobet", "sbo",
    "betfair_ex", "betfair", "matchbook",
}

SOFT_BOOKS = {
    "bet365", "unibet", "bwin", "betway", "williamhill",
    "william_hill", "1xbet", "betclic", "snai", "lottomatica",
    "sisal", "goldbet", "eurobet", "planetwin365",
}

# ── Soglie value bet ───────────────────────────────────────────────────────────
MIN_VALUE_PCT        = 5.0    # % minimo di edge per generare segnale
MIN_ODDS             = 1.40   # quota minima accettata
MAX_ODDS             = 5.00   # quota massima accettata
MIN_SOFT_BOOKS       = 1      # almeno N soft book devono confermare la quota
MIN_HOURS_BEFORE     = 1.0    # default ore minime al kickoff
MAX_EDGE_NO_SHARP    = 20.0   # default cap edge% senza Pinnacle


class AIAnalyzer:

    async def analyze(self, match: dict, settings: dict = None) -> list[dict]:
        sport = match.get("sport", "tabletennis")
        try:
            return self._analyze(match, sport, settings or {})
        except Exception as e:
            logger.error(f"Analyzer errore [{sport}] {match.get('name','?')}: {e}")
            return []

    # ── Core ───────────────────────────────────────────────────────────────────
    def _analyze(self, match: dict, sport: str, settings: dict) -> list[dict]:
        signals   = []
        raw_bm    = match.get("raw_bookmakers", {})   # da scraper arricchito
        has_sharp = bool(raw_bm)

        # Legge limiti da settings (con fallback alle costanti)
        min_hours    = float(settings.get("min_hours_before", MIN_HOURS_BEFORE))
        max_edge_cap = float(settings.get("max_edge_no_sharp", MAX_EDGE_NO_SHARP)) / 100

        # ── Filtro anticipo kickoff ──────────────────────────────────────────
        kickoff_str = match.get("kickoff", "")
        if kickoff_str:
            try:
                try:
                    ko = datetime.fromisoformat(kickoff_str)
                    if ko.tzinfo is None:
                        ko = ko.replace(tzinfo=IT_TZ)
                except ValueError:
                    year = datetime.now(IT_TZ).year
                    ko = datetime.strptime(f"{year}/{kickoff_str}", "%Y/%d/%m %H:%M")
                    ko = ko.replace(tzinfo=IT_TZ)
                now_it = datetime.now(IT_TZ)
                hours_to_ko = (ko - now_it).total_seconds() / 3600
                if hours_to_ko < min_hours:
                    logger.info(
                        f"Segnale scartato (kickoff tra {hours_to_ko:.1f}h < min {min_hours}h): "
                        f"{match.get('name','?')} — {kickoff_str}"
                    )
                    return []
            except Exception as e:
                logger.warning(f"Impossibile parsare kickoff '{kickoff_str}': {e}")

        # ── Stima probabilità reale ──────────────────────────────────────────
        if has_sharp:
            fair_home, fair_away = self._devi_power(raw_bm, match)
        else:
            # Fallback: de-vig semplice sulle quote mediate già disponibili
            oh = match.get("odds_home")
            oa = match.get("odds_away")
            if not oh or not oa:
                return []
            fair_home, fair_away = self._devi_simple(oh, oa)

        if fair_home is None or fair_away is None:
            return []

        p1 = match["player1"]
        p2 = match["player2"]

        # ── Winner ──────────────────────────────────────────────────────────
        # Cerca la quota migliore nei soft book (o usa odds_home/away)
        best_h, best_h_book = self._best_soft_odd(raw_bm, p1, match.get("odds_home"))
        best_a, best_a_book = self._best_soft_odd(raw_bm, p2, match.get("odds_away"))

        if best_h and MIN_ODDS <= best_h <= MAX_ODDS:
            value_h = fair_home * best_h - 1
            if not has_sharp:
                value_h = min(value_h, max_edge_cap)
            if value_h >= MIN_VALUE_PCT / 100:
                conf = self._confidence(value_h, has_sharp, match.get("source",""))
                signals.append(self._build(
                    match     = match,
                    sig_type  = "winner",
                    pick      = f"{p1} vince",
                    odds      = best_h,
                    fair_prob = fair_home,
                    value_pct = round(value_h * 100, 2),
                    confidence= conf,
                    reasoning = self._reasoning_winner(p1, fair_home, best_h, best_h_book, has_sharp),
                    book_note = f"Quota trovata su: {best_h_book or 'media mercato'}",
                ))

        if best_a and MIN_ODDS <= best_a <= MAX_ODDS:
            value_a = fair_away * best_a - 1
            if not has_sharp:
                value_a = min(value_a, max_edge_cap)
            if value_a >= MIN_VALUE_PCT / 100:
                conf = self._confidence(value_a, has_sharp, match.get("source",""))
                signals.append(self._build(
                    match     = match,
                    sig_type  = "winner",
                    pick      = f"{p2} vince",
                    odds      = best_a,
                    fair_prob = fair_away,
                    value_pct = round(value_a * 100, 2),
                    confidence= conf,
                    reasoning = self._reasoning_winner(p2, fair_away, best_a, best_a_book, has_sharp),
                    book_note = f"Quota trovata su: {best_a_book or 'media mercato'}",
                ))

        # ── Over/Under ───────────────────────────────────────────────────────
        ov   = match.get("over_odds")
        un   = match.get("under_odds")
        line = match.get("totals_line")
        if ov and un and line and MIN_ODDS <= ov <= MAX_ODDS and MIN_ODDS <= un <= MAX_ODDS:
            fair_ov, fair_un = self._devi_simple(ov, un)
            if fair_ov and fair_un:
                value_ov = fair_ov * ov - 1
                value_un = fair_un * un - 1
                if not has_sharp:
                    value_ov = min(value_ov, max_edge_cap)
                    value_un = min(value_un, max_edge_cap)

                unit = "set" if sport == "tabletennis" else "games"

                if value_ov >= MIN_VALUE_PCT / 100:
                    conf = self._confidence(value_ov, has_sharp, match.get("source",""))
                    signals.append(self._build(
                        match     = match,
                        sig_type  = "over",
                        pick      = f"Over {line} {unit}",
                        odds      = ov,
                        fair_prob = fair_ov,
                        value_pct = round(value_ov * 100, 2),
                        confidence= conf,
                        reasoning = (
                            f"De-vig Over {line} {unit}: probabilità fair {fair_ov:.1%} "
                            f"vs quota {ov} (value +{value_ov*100:.1f}%)"
                        ),
                        book_note = f"Linea: {line} {unit}",
                    ))

                elif value_un >= MIN_VALUE_PCT / 100:
                    conf = self._confidence(value_un, has_sharp, match.get("source",""))
                    signals.append(self._build(
                        match     = match,
                        sig_type  = "under",
                        pick      = f"Under {line} {unit}",
                        odds      = un,
                        fair_prob = fair_un,
                        value_pct = round(value_un * 100, 2),
                        confidence= conf,
                        reasoning = (
                            f"De-vig Under {line} {unit}: probabilità fair {fair_un:.1%} "
                            f"vs quota {un} (value +{value_un*100:.1f}%)"
                        ),
                        book_note = f"Linea: {line} {unit}",
                    ))

        # Ordina per value decrescente, max 2 segnali per partita
        signals.sort(key=lambda x: x["value_pct"], reverse=True)
        return signals[:2]

    # ── De-vig Power (metodo professionale) ───────────────────────────────────
    def _devi_power(self, raw_bm: dict, match: dict) -> tuple:
        """
        Power De-vig su Pinnacle.
        Trova k tale che p1^k + p2^k = 1 (rimuove il margine non linearmente).
        Molto più accurato del de-vig additivo semplice.
        """
        # Cerca sharp book in ordine di priorità
        sharp_odds = None
        for book_name, odds in raw_bm.items():
            if any(s in book_name.lower() for s in SHARP_BOOKS):
                h = odds.get("home") or odds.get(match.get("player1",""), {})
                a = odds.get("away") or odds.get(match.get("player2",""), {})
                if h and a and h > 1.01 and a > 1.01:
                    sharp_odds = (float(h), float(a))
                    logger.info(f"Sharp book trovato: {book_name} → {h}/{a}")
                    break

        if not sharp_odds:
            # Nessun sharp book → de-vig semplice sulle quote migliori disponibili
            oh = match.get("odds_home")
            oa = match.get("odds_away")
            if oh and oa:
                return self._devi_simple(oh, oa)
            return None, None

        oh, oa = sharp_odds
        p_raw_h = 1 / oh
        p_raw_a = 1 / oa

        # Risolvi k con Newton-Raphson: p_h^k + p_a^k = 1
        k = self._solve_power_k(p_raw_h, p_raw_a)

        fair_h = p_raw_h ** k / (p_raw_h ** k + p_raw_a ** k)
        fair_a = 1 - fair_h

        logger.debug(f"Power de-vig k={k:.4f}: fair_h={fair_h:.4f} fair_a={fair_a:.4f}")
        return round(fair_h, 4), round(fair_a, 4)

    def _solve_power_k(self, p1: float, p2: float, iterations: int = 20) -> float:
        """Newton-Raphson per trovare k in p1^k + p2^k = 1."""
        k = 1.0
        for _ in range(iterations):
            f  = p1**k + p2**k - 1
            df = p1**k * math.log(p1) + p2**k * math.log(p2)
            if abs(df) < 1e-10:
                break
            k -= f / df
            k  = max(0.5, min(2.0, k))   # clamp sicurezza
        return k

    def _devi_simple(self, oh: float, oa: float) -> tuple:
        """De-vig additivo semplice (fallback se no sharp book)."""
        if not oh or not oa or oh <= 1 or oa <= 1:
            return None, None
        margin = 1/oh + 1/oa
        return round((1/oh) / margin, 4), round((1/oa) / margin, 4)

    # ── Miglior quota soft book ────────────────────────────────────────────────
    def _best_soft_odd(self, raw_bm: dict, player: str, fallback: float) -> tuple:
        """
        Cerca la quota migliore per un giocatore tra i soft book.
        Ritorna (quota, nome_book).
        """
        if not raw_bm:
            return fallback, None

        best_price = 0.0
        best_book  = None

        for book_name, odds in raw_bm.items():
            # Salta sharp book per la ricerca della quota "da giocare"
            if any(s in book_name.lower() for s in SHARP_BOOKS):
                continue
            # Cerca la quota per il giocatore nel dict del bookmaker
            price = None
            for key, val in odds.items():
                if isinstance(val, (int, float)) and player.lower() in key.lower():
                    price = float(val)
                    break
                # Struttura alternativa: {"home": x, "away": y}
                if key in ("home", "away") and isinstance(val, (int, float)):
                    if key == "home" and "home" in odds:
                        price = float(val)
                        break

            if price and price > best_price:
                best_price = price
                best_book  = book_name

        # Se non trovato tra soft, usa il fallback (media mercato)
        if not best_price and fallback:
            return float(fallback), "media mercato"

        return (round(best_price, 3), best_book) if best_price else (fallback, None)

    # ── Confidenza ────────────────────────────────────────────────────────────
    def _confidence(self, value: float, has_sharp: bool, source: str) -> int:
        """
        Confidenza basata su:
        - Entità del value edge
        - Se abbiamo usato Pinnacle (più affidabile) o de-vig semplice
        - Fonte dati (API reale vs fallback)

        NOTA: senza sharp book il de-vig semplice non è affidabile →
        confidenza cappata a 65 e penalità più alta.
        """
        # Base: da 50% (edge=2.5%) a 82% (edge=15%+)
        base = 50 + int(min(value * 200, 32))

        if has_sharp:
            base += 8   # dati Pinnacle reali → bonus affidabilità
        else:
            base -= 12  # de-vig semplice → penalità forte
            base = min(base, 65)  # mai sopra 65% senza sharp book

        # Penalità per fonte meno affidabile
        if source == "fallback":
            base -= 15
        elif source == "oddspapi_noodds":
            base -= 8

        # Piccola variazione (±2%) per evitare confidenze sempre identiche
        base += random.randint(-2, 2)
        return max(45, min(90, base))

    # ── Stake Kelly semplificato ───────────────────────────────────────────────
    def _stake(self, fair_prob: float, odds: float, confidence: int) -> int:
        """
        Kelly fraction: f = (p*b - q) / b  dove b = odds-1, p=fair_prob, q=1-p
        Usiamo Kelly/4 (quarter Kelly) per sicurezza, mappato su scala 1-5.
        """
        b = odds - 1
        q = 1 - fair_prob
        kelly = (fair_prob * b - q) / b if b > 0 else 0
        kelly = max(0, kelly)
        quarter_kelly = kelly / 4

        # Mappa su 1-5 (ogni 2% di Kelly = 1 punto stake)
        stake = max(1, min(5, int(quarter_kelly * 200) + 1))

        # Confidenza bassa → taglia stake
        if confidence < 58:
            stake = max(1, stake - 1)

        return stake

    # ── Reasoning leggibile ───────────────────────────────────────────────────
    def _reasoning_winner(
        self, player: str, fair_prob: float,
        best_odd: float, book: str, has_sharp: bool
    ) -> str:
        method = "de-vig Pinnacle" if has_sharp else "de-vig mercato"
        fair_odd = round(1 / fair_prob, 2) if fair_prob > 0 else "?"
        note = "" if has_sharp else " ⚠️ Nessun sharp book — edge stimato."
        return (
            f"Probabilità fair ({method}): {fair_prob:.1%} → quota fair {fair_odd}. "
            f"Quota disponibile: {best_odd} ({book or 'media'}) — "
            f"edge reale: +{(fair_prob * best_odd - 1)*100:.1f}%{note}"
        )

    # ── Build segnale ─────────────────────────────────────────────────────────
    def _build(
        self,
        match:      dict,
        sig_type:   str,
        pick:       str,
        odds:       float,
        fair_prob:  float,
        value_pct:  float,
        confidence: int,
        reasoning:  str = "",
        book_note:  str = "",
    ) -> dict:
        now     = datetime.now(IT_TZ)
        # match_key: usa event_id se disponibile (stabile tra scan), altrimenti nome+data.
        # sig_type distingue winner da over/under sulla stessa partita.
        # NON includiamo il pick nel key: due scan dello stesso evento devono
        # produrre la stessa chiave e il secondo viene ignorato da signal_exists().
        event_id = match.get("event_id") or match["name"]
        key_str = f"{event_id}|{sig_type}|{now.strftime('%Y%m%d')}"
        mk      = hashlib.md5(key_str.encode()).hexdigest()[:16]
        stake   = self._stake(fair_prob, odds, confidence)

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
