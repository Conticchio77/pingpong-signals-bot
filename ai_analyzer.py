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
import os
import math
import random
import statistics
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

# ── Bookmaker "giocabili" (dove l'utente può davvero puntare) ──────────────────
# La quota da giocare viene cercata SOLO tra questi book; gli sharp restano il
# riferimento per la probabilità fair. Match per sottostringa sul nome/slug.
#   Tennis (The Odds API, slug "onexbet"; via OddsPapi sarebbe "1xbet"):
#     variabile Railway TENNIS_SOFT_BOOKS, default "onexbet,1xbet". Vuota = tutti.
#   Ping pong: i book arrivano già filtrati dallo scraper (PP_BOOKMAKERS); se vuoi
#     un filtro in più usa PP_SOFT_BOOKS (default vuoto = nessun filtro).
def _soft_whitelist(sport: str) -> list:
    if sport == "tennis":
        raw = os.environ.get("TENNIS_SOFT_BOOKS", "onexbet,1xbet")
    else:
        raw = os.environ.get("PP_SOFT_BOOKS", "")
    return [b.strip().lower() for b in raw.split(",") if b.strip()]

def _soft_allowed(book_name: str, whitelist: list) -> bool:
    if not whitelist:
        return True
    name = (book_name or "").lower()
    return any(w in name for w in whitelist)

# ── Soglie value bet ───────────────────────────────────────────────────────────
MIN_VALUE_PCT        = 3.0    # % minimo di edge per generare segnale (default — override da settings["min_value_pct"])
MIN_ODDS             = 1.40   # quota minima accettata
MAX_ODDS             = 5.00   # quota massima accettata
MIN_SOFT_BOOKS       = 1      # almeno N soft book devono confermare la quota
MIN_HOURS_BEFORE     = 1.0    # default ore minime al kickoff
SAME_DAY_ONLY        = False  # default: NON limitare al giorno solare (vedi MAX_HOURS_AHEAD)
MAX_HOURS_AHEAD      = 18.0   # scarta partite oltre N ore da adesso (copre lo scan serale per i match asiatici del mattino dopo)
MAX_EDGE_NO_SHARP    = 20.0   # default cap edge% senza Pinnacle
MIN_REF_BOOKS        = 3      # book minimi per un riferimento di consenso (senza sharp)


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
        raw_bm    = match.get("raw_bookmakers") or {}   # da scraper arricchito

        # FIX: blocco esplicito, indipendente da qualunque altra logica a monte
        # (in bot.py) — un match con source "fallback" (dati demo/random, usati
        # solo se TUTTE le API sono giù) non deve MAI produrre un segnale reale.
        source = match.get("source", "")
        if source == "fallback":
            logger.info(f"Match scartato (fonte demo, non una partita vera): {match.get('name','?')}")
            return []

        # Legge limiti da settings (con fallback alle costanti)
        min_hours    = float(settings.get("min_hours_before", MIN_HOURS_BEFORE))
        same_day_only = bool(settings.get("same_day_only", SAME_DAY_ONLY))
        max_hours_ahead = float(settings.get("max_hours_ahead", MAX_HOURS_AHEAD))
        min_value    = float(settings.get("min_value_pct", MIN_VALUE_PCT)) / 100
        # Cap edge senza Pinnacle: più basso per ping pong (de-vig meno affidabile senza sharp)
        if sport == "tabletennis":
            max_edge_cap = 15.0 / 100   # ping pong: max 15% (OddsPapi non ha mai Pinnacle)
        else:
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
                # FIX: prima non c'era nessun tetto massimo — solo il minimo
                # di 1h sopra. Una partita trovata oggi ma in programma tra 2-3
                # giorni passava comunque il filtro, generando segnali su match
                # troppo lontani nel tempo (quote non ancora definitive, lega
                # che potrebbe rinviare, ecc.). Richiesto esplicitamente: solo
                # partite dello stesso giorno solare (confronto per data, non
                # per ore, quindi niente segnali anche solo per un match delle
                # 00:30 di domani trovato in uno scan serale).
                if hours_to_ko > max_hours_ahead:
                    logger.info(
                        f"Segnale scartato (kickoff tra {hours_to_ko:.1f}h > max {max_hours_ahead:.0f}h): "
                        f"{match.get('name','?')} — {kickoff_str}"
                    )
                    return []
                if same_day_only and ko.date() != now_it.date():
                    logger.info(
                        f"Segnale scartato (kickoff {ko.date()} non è oggi {now_it.date()}): "
                        f"{match.get('name','?')} — {kickoff_str}"
                    )
                    return []
            except Exception as e:
                logger.warning(f"Impossibile parsare kickoff '{kickoff_str}': {e}")

        # ── Stima probabilità reale ──────────────────────────────────────────
        # Riferimento: 1) book sharp (de-vig power); 2) altrimenti CONSENSO =
        # mediana delle probabilità dei book, ognuno de-viggato per conto suo.
        # Il vecchio fallback (de-vig sulle quote MIGLIORI) confrontava la quota
        # migliore con se stessa: value ≤ 0 salvo arbitraggio, quindi non usciva
        # mai un segnale.
        fair_home, fair_away, ref_kind, n_ref = self._reference_probs(raw_bm)
        if fair_home is None:
            logger.info(
                f"Nessun riferimento affidabile ({n_ref} book con quote, servono "
                f"{MIN_REF_BOOKS} o uno sharp): {match.get('name','?')} — winner saltato"
            )
            ref_kind = None
        has_sharp = ref_kind == "sharp"
        source    = match.get("source", "")

        def _edge_ok(value: float, label: str) -> bool:
            # Senza sharp un edge sopra il tetto è quasi sempre una quota
            # vecchia/anomala di un singolo book: meglio scartarlo che mostrarlo
            # "tagliato" al tetto come se fosse un segnale buono.
            if not has_sharp and value > max_edge_cap:
                logger.info(
                    f"Edge {value:.1%} sopra il tetto {max_edge_cap:.0%} senza sharp "
                    f"(quota sospetta): {match.get('name','?')} — {label}"
                )
                return False
            return True

        p1 = match["player1"]
        p2 = match["player2"]

        # ── Winner ──────────────────────────────────────────────────────────
        if ref_kind:
            soft_wl = _soft_whitelist(sport)
            best_h, best_h_book = self._best_soft_odd(raw_bm, "home", match.get("odds_home"), soft_wl)
            best_a, best_a_book = self._best_soft_odd(raw_bm, "away", match.get("odds_away"), soft_wl)

            for player, fair, best, book in (
                (p1, fair_home, best_h, best_h_book),
                (p2, fair_away, best_a, best_a_book),
            ):
                # DIAGNOSTICA: prima questi due scarti erano silenziosi (solo
                # `continue`) — con 72 book di copertura ma zero segnali, non
                # si riusciva a capire se la causa fosse "quota fuori range"
                # (es. 1.15 per un favorito schiacciante, comune nel ping
                # pong) o "edge sotto soglia". Ora entrambi i casi sono loggati.
                if not best and soft_wl:
                    logger.info(
                        f"Winner scartato (nessuna quota su {'/'.join(soft_wl)}): "
                        f"{match.get('name','?')} — {player}"
                    )
                    continue
                if not best or not (MIN_ODDS <= best <= MAX_ODDS):
                    logger.info(
                        f"Winner scartato (quota {best} fuori range {MIN_ODDS}-{MAX_ODDS}): "
                        f"{match.get('name','?')} — {player}"
                    )
                    continue
                value = fair * best - 1
                if value < min_value:
                    logger.info(
                        f"Winner scartato (edge {value:.1%} < min {min_value:.1%}): "
                        f"{match.get('name','?')} — {player} @ {best}"
                    )
                    continue
                if not _edge_ok(value, f"{player} vince"):
                    continue
                signals.append(self._build(
                    match     = match,
                    sig_type  = "winner",
                    pick      = f"{player} vince",
                    odds      = best,
                    fair_prob = fair,
                    value_pct = round(value * 100, 2),
                    confidence= self._confidence(value, ref_kind, source),
                    reasoning = self._reasoning_winner(player, fair, best, book, ref_kind, n_ref),
                    book_note = f"Quota trovata su: {book or 'media mercato'}",
                ))

        # ── Over/Under ───────────────────────────────────────────────────────
        # Servono le quote PER BOOK sulla stessa linea (raw_totals). Prima si
        # prendevano over e under migliori tra tutti i book, anche su linee
        # diverse, e li si de-viggava insieme: falsi positivi.
        tot = self._totals_reference(match.get("raw_totals") or {}, _soft_whitelist(sport))
        if tot:
            unit = "set" if sport == "tabletennis" else "games"
            line = tot["line"]
            cand = []
            for side, fair, best, book in (
                ("over",  tot["fair_over"],  tot["best_over"],  tot["over_book"]),
                ("under", tot["fair_under"], tot["best_under"], tot["under_book"]),
            ):
                if not best or not (MIN_ODDS <= best <= MAX_ODDS):
                    logger.info(
                        f"Totals scartato (quota {best} fuori range {MIN_ODDS}-{MAX_ODDS}): "
                        f"{match.get('name','?')} — {side} {line}"
                    )
                    continue
                value = fair * best - 1
                if value >= min_value:
                    cand.append((value, side, fair, best, book))
                else:
                    logger.info(
                        f"Totals scartato (edge {value:.1%} < min {min_value:.1%}): "
                        f"{match.get('name','?')} — {side} {line} @ {best}"
                    )
            if cand:
                value, side, fair, best, book = max(cand)
                tot_sharp = tot["kind"] == "sharp"
                if tot_sharp or value <= max_edge_cap:
                    method = "book sharp" if tot_sharp else f"consenso di {tot['n_books']} book (mediana)"
                    signals.append(self._build(
                        match     = match,
                        sig_type  = side,
                        pick      = f"{side.capitalize()} {line} {unit}",
                        odds      = best,
                        fair_prob = fair,
                        value_pct = round(value * 100, 2),
                        confidence= self._confidence(value, tot["kind"], source),
                        reasoning = (
                            f"{side.capitalize()} {line} {unit}: probabilità fair {fair:.1%} "
                            f"({method}) vs quota {best} (value +{value*100:.1f}%)"
                        ),
                        book_note = f"Linea: {line} {unit} | quota su: {book}",
                    ))
                else:
                    logger.info(
                        f"Edge {value:.1%} sopra il tetto {max_edge_cap:.0%} senza sharp "
                        f"(quota sospetta): {match.get('name','?')} — {side} {line}"
                    )

        # Ordina per value decrescente, max 2 segnali per partita
        signals.sort(key=lambda x: x["value_pct"], reverse=True)
        return signals[:2]

    # ── Riferimento di probabilità ─────────────────────────────────────────────
    @staticmethod
    def _is_sharp(book_name: str) -> bool:
        name = (book_name or "").lower()
        return any(s in name for s in SHARP_BOOKS)

    @staticmethod
    def _valid_pair(a, b) -> bool:
        return (
            isinstance(a, (int, float)) and isinstance(b, (int, float))
            and a > 1.01 and b > 1.01
        )

    def _power_fair(self, oa: float, ob: float) -> tuple:
        """Probabilità fair di un mercato a 2 esiti con power de-vig."""
        pa, pb = 1 / oa, 1 / ob
        k = self._solve_power_k(pa, pb)
        fa = pa ** k / (pa ** k + pb ** k)
        return fa, 1 - fa

    def _reference_probs(self, raw_bm: dict) -> tuple:
        """(fair_home, fair_away, kind, n_book). kind: 'sharp' | 'consensus' | None."""
        for book, odds in raw_bm.items():
            if self._is_sharp(book):
                h, a = odds.get("home"), odds.get("away")
                if self._valid_pair(h, a):
                    logger.info(f"Sharp book trovato: {book} → {h}/{a}")
                    fh, fa = self._power_fair(float(h), float(a))
                    return round(fh, 4), round(fa, 4), "sharp", 1
        fairs = [
            self._power_fair(float(o["home"]), float(o["away"]))[0]
            for o in raw_bm.values()
            if self._valid_pair(o.get("home"), o.get("away"))
        ]
        if len(fairs) >= MIN_REF_BOOKS:
            fh = statistics.median(fairs)
            return round(fh, 4), round(1 - fh, 4), "consensus", len(fairs)
        return None, None, None, len(fairs)

    def _totals_reference(self, raw_totals: dict, whitelist: list | None = None) -> dict | None:
        """raw_totals = {linea: {book: {"over": x, "under": y}}}. Sceglie la linea
        quotata da più book su entrambi i lati e ne ricava la probabilità fair."""
        best_line, entries = None, {}
        for line, books in raw_totals.items():
            valid = {b: o for b, o in books.items() if self._valid_pair(o.get("over"), o.get("under"))}
            # con una whitelist la linea deve essere quotata da almeno un book giocabile
            if whitelist and not any(
                _soft_allowed(b, whitelist) and not self._is_sharp(b) for b in valid
            ):
                continue
            if len(valid) > len(entries) or (
                valid and len(valid) == len(entries) and best_line is not None and line < best_line
            ):
                best_line, entries = line, valid
        if not entries:
            return None

        sharp = [b for b in entries if self._is_sharp(b)]
        if sharp:
            o = entries[sharp[0]]
            fair_ov, fair_un = self._power_fair(float(o["over"]), float(o["under"]))
            kind, playable = "sharp", {b: v for b, v in entries.items() if b not in sharp}
        elif len(entries) >= MIN_REF_BOOKS:
            fair_ov = statistics.median(
                self._power_fair(float(o["over"]), float(o["under"]))[0] for o in entries.values()
            )
            fair_un = 1 - fair_ov
            kind, playable = "consensus", entries
        else:
            logger.info(f"Over/Under: linea {best_line} con solo {len(entries)} book (servono {MIN_REF_BOOKS} o uno sharp) — saltato")
            return None
        if whitelist:
            playable = {b: v for b, v in playable.items() if _soft_allowed(b, whitelist)}
        if not playable:
            return None

        over_book  = max(playable, key=lambda b: playable[b]["over"])
        under_book = max(playable, key=lambda b: playable[b]["under"])
        return {
            "line": best_line, "kind": kind, "n_books": len(entries),
            "fair_over": round(fair_ov, 4), "fair_under": round(fair_un, 4),
            "best_over": round(float(playable[over_book]["over"]), 3), "over_book": over_book,
            "best_under": round(float(playable[under_book]["under"]), 3), "under_book": under_book,
        }

    # ── De-vig Power (metodo professionale) ───────────────────────────────────
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
    def _best_soft_odd(self, raw_bm: dict, side: str, fallback: float, whitelist: list | None = None) -> tuple:
        """
        Cerca la quota migliore per un lato (side = "home" o "away") tra i soft book.
        Ritorna (quota, nome_book).

        NOTA: prende `side` esplicito (non il nome giocatore) perché raw_bookmakers
        usa sempre chiavi generiche {"home": x, "away": y} (vedi scraper.py, entry["home"]/
        entry["away"]). Il vecchio codice cercava il nome giocatore dentro le chiavi
        "home"/"away" (che non lo contengono mai) e poi aveva un fallback che leggeva
        SEMPRE "home" a prescindere dal lato cercato — quindi il lato "away" riceveva
        sempre la quota home per errore.
        """
        if not raw_bm:
            return fallback, None

        best_price = 0.0
        best_book  = None

        for book_name, odds in raw_bm.items():
            # Salta sharp book per la ricerca della quota "da giocare"
            if any(s in book_name.lower() for s in SHARP_BOOKS):
                continue
            if not _soft_allowed(book_name, whitelist or []):
                continue
            val = odds.get(side)
            if isinstance(val, (int, float)) and val > best_price:
                best_price = float(val)
                best_book  = book_name

        # Con una whitelist niente "media mercato": se il book scelto non quota, nessun segnale
        if not best_price and whitelist:
            return None, None

        # Se non trovato tra soft, usa il fallback (media mercato)
        if not best_price and fallback:
            return float(fallback), "media mercato"

        return (round(best_price, 3), best_book) if best_price else (fallback, None)

    # ── Confidenza ────────────────────────────────────────────────────────────
    def _confidence(self, value: float, ref_kind: str, source: str) -> int:
        """
        Confidenza basata su:
        - Entità del value edge
        - Riferimento usato: book sharp (più affidabile) o consenso dei book
        - Fonte dati (API reale vs fallback)

        Consenso (mediana di ≥3 book) = riferimento più debole di uno sharp:
        penalità -6 e tetto 70, quindi serve un edge un po' più alto (~5.5%+
        con min_confidence 55) rispetto ai segnali con sharp (3%).
        """
        # Base: da 50% (edge=2.5%) a 82% (edge=15%+)
        base = 50 + int(min(value * 200, 32))

        if ref_kind == "sharp":
            base += 8   # dati sharp reali → bonus affidabilità
        else:
            base -= 6
            base = min(base, 70)

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
        best_odd: float, book: str, ref_kind: str, n_ref: int = 0
    ) -> str:
        if ref_kind == "sharp":
            method, note = "de-vig book sharp", ""
        else:
            method = f"consenso di {n_ref} book (mediana)"
            note = " ⚠️ Nessuno sharp book — riferimento = consenso."
        fair_odd = round(1 / fair_prob, 2) if fair_prob > 0 else "?"
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
        event_id = match.get("event_id") or ""
        if not event_id:
            # Fallback: nome normalizzato + data. Meno stabile ma meglio di niente.
            event_id = f"{match['name'].lower().replace(' ', '_')}_{now.strftime('%Y%m%d')}"
            logger.warning(f"match_key: event_id mancante per '{match['name']}' — uso nome come chiave")
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
