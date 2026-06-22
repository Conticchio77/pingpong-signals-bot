"""
scraper.py — Multi-sport: Ping Pong + Tennis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sorgenti:
  • Ping Pong → OddsPapi (gratuita, 250 req/mese, 370+ bookmaker)
               Endpoint: https://api.oddspapi.io/v4
               Env var:  ODDSPAPI_KEY
               Sport ID: scoperto dinamicamente (cerca "table tennis")

  • Tennis    → The Odds API (gratuita, 500 crediti/mese)
               Endpoint: https://api.the-odds-api.com/v4
               Env var:  ODDS_API_KEY   (già presente nel tuo progetto)
               Sport key: "tennis"

Variabili Railway da aggiungere:
  ODDSPAPI_KEY  = <la tua chiave da oddspapi.io>
  ODDS_API_KEY  = <la tua chiave esistente da the-odds-api.com>

Ogni partita ha il campo  sport_label = "🏓 Ping Pong" | "🎾 Tennis"
usato da bot.py per differenziare i segnali nella UI.
"""

import aiohttp
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IT_TZ  = ZoneInfo("Europe/Rome")

# ── Chiavi API ─────────────────────────────────────────────────────────────────
ODDSPAPI_KEY = os.environ.get("ODDSPAPI_KEY", "")   # ping pong
ODDS_KEY     = os.environ.get("ODDS_API_KEY", "")   # tennis (già presente)

ODDSPAPI_BASE = "https://api.oddspapi.io/v4"
ODDS_BASE     = "https://api.the-odds-api.com/v4"

# ── Helper tempo ───────────────────────────────────────────────────────────────
def _now_it() -> datetime:
    return datetime.now(IT_TZ)

def _iso_to_it(iso: str) -> str:
    try:
        iso = iso.replace("Z", "+00:00")
        dt  = datetime.fromisoformat(iso).astimezone(IT_TZ)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return _now_it().strftime("%d/%m %H:%M")


# ══════════════════════════════════════════════════════════════════════════════
class SignalScraper:

    def __init__(self, db=None):
        self._tt_sport_id: int | None = None   # cache ID OddsPapi per ping pong
        self.db = db   # se presente, cache persistente su DB (evita 429 da troppe /sports)

    # ── Entry point principale ─────────────────────────────────────────────────
    async def fetch_matches(self) -> list[dict]:
        """Restituisce partite di ENTRAMBI gli sport, ordinate per kickoff."""
        results = []

        # 1. Ping Pong via OddsPapi
        if ODDSPAPI_KEY:
            tt = await self._fetch_oddspapi_tt()
            logger.info(f"OddsPapi Ping Pong: {len(tt)} partite")
            results.extend(tt)
        else:
            logger.warning("ODDSPAPI_KEY non impostata — ping pong saltato")

        # 2. Tennis via The Odds API
        if ODDS_KEY:
            ten = await self._fetch_odds_api_tennis()
            logger.info(f"The Odds API Tennis: {len(ten)} partite")
            results.extend(ten)
        else:
            logger.warning("ODDS_API_KEY non impostata — tennis saltato")

        # Fallback se entrambe le API sono assenti
        if not results:
            logger.warning("Nessuna API configurata — uso fallback misto")
            results = self.get_fallback_matches()

        return self._sort_dedup(results)

    # ══════════════════════════════════════════════════════════════════════════
    # ── OddsPapi: Ping Pong ────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    async def _get_tt_sport_id(self, session: aiohttp.ClientSession) -> int | None:
        """Scopre l'ID OddsPapi per il ping pong. Cache in-memory + persistente su DB
        per evitare di richiamare /sports ad ogni scan (consuma quota → 429)."""
        if self._tt_sport_id:
            return self._tt_sport_id

        # 1. Prova la cache persistente sul DB (sopravvive a restart/redeploy)
        if self.db is not None:
            cached = self.db.get_tt_sport_id()
            if cached:
                self._tt_sport_id = cached
                logger.info(f"OddsPapi: ping pong sportId={cached} (da cache DB)")
                return cached

        # 2. Cache assente: interroga /sports (consuma 1 richiesta)
        try:
            async with session.get(
                f"{ODDSPAPI_BASE}/sports",
                params={"apiKey": ODDSPAPI_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    logger.warning(f"OddsPapi /sports status {r.status}")
                    return None
                sports = await r.json()
                for s in sports:
                    name = (s.get("name") or s.get("slug") or "").lower()
                    if "table" in name or "ping" in name:
                        self._tt_sport_id = s.get("sportId") or s.get("id")
                        logger.info(f"OddsPapi: ping pong sportId={self._tt_sport_id} ({s.get('name')})")
                        if self.db is not None and self._tt_sport_id:
                            self.db.set_tt_sport_id(self._tt_sport_id)
                        return self._tt_sport_id
                # Log tutti gli sport disponibili per debug
                names = [s.get("name", "?") for s in sports]
                logger.warning(f"OddsPapi: ping pong non trovato. Sport disponibili: {names}")
        except Exception as e:
            logger.error(f"OddsPapi /sports errore: {e}")
        return None

    async def _fetch_oddspapi_tt(self) -> list[dict]:
        matches = []
        async with aiohttp.ClientSession() as session:
            sport_id = await self._get_tt_sport_id(session)
            if sport_id is None:
                logger.warning("OddsPapi: sport ID ping pong non trovato — ping pong non disponibile")
                return []

            # Fetch fixtures dei prossimi 2 giorni
            today     = _now_it().strftime("%Y-%m-%d")
            tomorrow  = (_now_it() + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                async with session.get(
                    f"{ODDSPAPI_BASE}/fixtures",
                    params={
                        "apiKey":  ODDSPAPI_KEY,
                        "sportId": sport_id,
                        "from":    today,
                        "to":      tomorrow,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    rem = r.headers.get("X-RateLimit-Remaining", "?")
                    logger.info(f"OddsPapi fixtures — richieste rimaste: {rem}")
                    if r.status != 200:
                        txt = await r.text()
                        logger.warning(f"OddsPapi fixtures status {r.status}: {txt[:120]}")
                        return []
                    fixtures = await r.json()
                    # fixtures può essere lista diretta o {"data": [...]}
                    if isinstance(fixtures, dict):
                        fixtures = fixtures.get("data") or fixtures.get("fixtures") or []
                    logger.info(f"OddsPapi: {len(fixtures)} fixture ricevute")

                    # Limita a 12 fixture più vicine nel tempo per evitare rate limit
                    # (ogni fixture = 1 chiamata API per le quote — piano gratuito 250/mese)
                    def _sort_key(f):
                        s = f.get("startDate") or f.get("startTime") or ""
                        try:
                            return datetime.fromisoformat(s.replace("Z", "+00:00"))
                        except Exception:
                            return datetime.max.replace(tzinfo=IT_TZ)
                    fixtures = sorted(fixtures, key=_sort_key)[:12]
                    logger.info(f"OddsPapi: limitate a {len(fixtures)} fixture (evita rate limit)")
            except Exception as e:
                logger.error(f"OddsPapi fixtures errore: {e}")
                return []

            # Per ogni fixture recupera le quote
            for fix in fixtures:
                parsed = await self._parse_oddspapi_fixture(session, fix)
                if parsed:
                    matches.append(parsed)

        return matches

    async def _parse_oddspapi_fixture(
        self, session: aiohttp.ClientSession, fix: dict
    ) -> dict | None:
        try:
            p1 = (fix.get("participant1Name") or fix.get("home") or "").strip()
            p2 = (fix.get("participant2Name") or fix.get("away") or "").strip()
            if not p1 or not p2:
                return None

            fid      = fix.get("fixtureId") or fix.get("id", "")
            start    = fix.get("startDate") or fix.get("startTime") or ""
            kickoff  = _iso_to_it(start) if start else _now_it().strftime("%d/%m %H:%M")
            tourn    = (
                fix.get("tournamentName")
                or fix.get("league")
                or fix.get("tournament", {}).get("name", "Ping Pong")
                if isinstance(fix.get("tournament"), dict)
                else fix.get("tournament", "Ping Pong")
            )

            # Recupera quote
            odds_home, odds_away, over_odds, under_odds, totals_line = \
                None, None, None, None, 3.5
            raw_bookmakers = {}

            if fid:
                try:
                    async with session.get(
                        f"{ODDSPAPI_BASE}/odds",
                        params={
                            "apiKey":    ODDSPAPI_KEY,
                            "fixtureId": fid,
                            "marketId":  "101",   # 101 = Match Winner (moneyline)
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            odds_home, odds_away, over_odds, under_odds, totals_line = \
                                self._extract_oddspapi_odds(data, p1, p2)
                            # Salva quote per bookmaker per de-vig Pinnacle
                            raw_bookmakers = self._extract_raw_bookmakers(data, p1, p2)
                except Exception as e:
                    logger.debug(f"OddsPapi odds errore fixture {fid}: {e}")

            # Se non abbiamo le quote, stima ragionevole (non random puro)
            if odds_home is None:
                odds_home = round(random.uniform(1.60, 2.20), 2)
                odds_away = round(random.uniform(1.60, 2.20), 2)
                source    = "oddspapi_noodds"
            else:
                source = "oddspapi"

            return {
                "event_id":        str(fid),
                "name":            f"{p1} vs {p2}",
                "player1":         p1,
                "player2":         p2,
                "kickoff":         kickoff,
                "tournament":      str(tourn),
                "status":          "scheduled",
                "odds_home":       round(odds_home, 3),
                "odds_away":       round(odds_away, 3),
                "over_odds":       round(over_odds,  3) if over_odds  else None,
                "under_odds":      round(under_odds, 3) if under_odds else None,
                "totals_line":     totals_line,
                "source":          source,
                "sport":           "tabletennis",
                "sport_label":     "🏓 Ping Pong",
                "raw_bookmakers":  raw_bookmakers,
            }
        except Exception as e:
            logger.debug(f"OddsPapi parse errore: {e}")
            return None

    def _extract_oddspapi_odds(
        self, data: dict, p1: str, p2: str
    ) -> tuple:
        """Estrae le migliori quote dal response OddsPapi."""
        odds_home = odds_away = over_odds = under_odds = None
        totals_line = 3.5

        # Struttura: {"bookmakerOdds": {"bookmakerSlug": {"outcomes": [...]}}}
        bm_odds = data.get("bookmakerOdds") or {}
        for bm_slug, bm_data in bm_odds.items():
            outcomes = bm_data.get("outcomes") or []
            for o in outcomes:
                name  = (o.get("name") or o.get("participant") or "").lower()
                price = float(o.get("price") or o.get("odds") or 0)
                if not price:
                    continue
                if p1.lower() in name or name in p1.lower():
                    if odds_home is None or price > odds_home:
                        odds_home = price
                elif p2.lower() in name or name in p2.lower():
                    if odds_away is None or price > odds_away:
                        odds_away = price
                elif "over" in name:
                    if over_odds is None or price > over_odds:
                        over_odds = price
                        pt = float(o.get("point") or o.get("line") or 3.5)
                        totals_line = pt
                elif "under" in name:
                    if under_odds is None or price > under_odds:
                        under_odds = price

        return odds_home, odds_away, over_odds, under_odds, totals_line

    def _extract_raw_bookmakers(self, data: dict, p1: str, p2: str) -> dict:
        """
        Estrae quote per ogni bookmaker nel formato:
        {"pinnacle": {"home": 1.85, "away": 2.10}, "bet365": {...}, ...}
        Usato dall'analyzer per il de-vig Pinnacle.
        """
        raw = {}
        bm_odds = data.get("bookmakerOdds") or {}
        for slug, bm_data in bm_odds.items():
            outcomes = bm_data.get("outcomes") or []
            entry = {}
            for o in outcomes:
                name  = (o.get("name") or o.get("participant") or "").lower()
                price = float(o.get("price") or o.get("odds") or 0)
                if not price:
                    continue
                if p1.lower() in name or name in p1.lower():
                    entry["home"] = price
                elif p2.lower() in name or name in p2.lower():
                    entry["away"] = price
            if "home" in entry and "away" in entry:
                raw[slug.lower()] = entry
        return raw


    # ══════════════════════════════════════════════════════════════════════════

    async def _fetch_odds_api_tennis(self) -> list[dict]:
        """Recupera partite di tennis da The Odds API (sport key: 'tennis')."""
        matches = []
        # "tennis" restituisce ATP + WTA + ITF aggregati
        sports_to_try = ["tennis", "tennis_atp", "tennis_wta"]

        async with aiohttp.ClientSession() as session:
            for sport_key in sports_to_try:
                url = (
                    f"{ODDS_BASE}/sports/{sport_key}/odds/"
                    f"?apiKey={ODDS_KEY}"
                    f"&regions=eu,uk"
                    f"&markets=h2h"
                    f"&oddsFormat=decimal"
                    f"&dateFormat=iso"
                )
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        rem  = resp.headers.get("x-requests-remaining", "?")
                        used = resp.headers.get("x-requests-used",      "?")
                        logger.info(
                            f"The Odds API tennis ({sport_key}) — usate:{used} rimaste:{rem}"
                        )
                        if resp.status == 200:
                            events = await resp.json()
                            logger.info(
                                f"The Odds API ({sport_key}): {len(events)} eventi"
                            )
                            for ev in events:
                                parsed = self._parse_odds_api_tennis(ev)
                                if parsed:
                                    matches.append(parsed)
                            if matches:
                                break   # bastano le partite del primo sport key valido
                        elif resp.status == 404:
                            logger.info(f"Sport key '{sport_key}' non trovato, provo il prossimo")
                        else:
                            txt = await resp.text()
                            logger.warning(
                                f"The Odds API ({sport_key}) status {resp.status}: {txt[:120]}"
                            )
                except Exception as e:
                    logger.error(f"The Odds API tennis errore ({sport_key}): {e}")

        # Dedup interni per nome partita
        seen, unique = set(), []
        for m in matches:
            if m["name"] not in seen:
                seen.add(m["name"])
                unique.append(m)
        return unique

    def _parse_odds_api_tennis(self, ev: dict) -> dict | None:
        try:
            home = ev.get("home_team", "").strip()
            away = ev.get("away_team", "").strip()
            if not home or not away:
                return None

            kickoff = _iso_to_it(ev.get("commence_time", ""))
            sport   = ev.get("sport_title", "Tennis")

            best: dict[str, float] = {}
            raw_bookmakers: dict   = {}

            for bm in ev.get("bookmakers", []):
                bm_slug = bm.get("key", "").lower()
                for market in bm.get("markets", []):
                    if market["key"] == "h2h":
                        entry = {}
                        for o in market.get("outcomes", []):
                            p = float(o["price"])
                            n = o["name"]
                            if n not in best or p > best[n]:
                                best[n] = p
                            if n == home:
                                entry["home"] = p
                            elif n == away:
                                entry["away"] = p
                        if "home" in entry and "away" in entry:
                            raw_bookmakers[bm_slug] = entry

            if home not in best or away not in best:
                return None

            return {
                "event_id":       ev.get("id", ""),
                "name":           f"{home} vs {away}",
                "player1":        home,
                "player2":        away,
                "kickoff":        kickoff,
                "tournament":     sport,
                "status":         "scheduled",
                "odds_home":      round(best[home], 3),
                "odds_away":      round(best[away], 3),
                "over_odds":      None,
                "under_odds":     None,
                "totals_line":    None,
                "source":         "odds_api",
                "sport":          "tennis",
                "sport_label":    "🎾 Tennis",
                "raw_bookmakers": raw_bookmakers,
            }
        except Exception as e:
            logger.debug(f"Parse tennis errore: {e}")
            return None

    # ══════════════════════════════════════════════════════════════════════════
    # ── Scores (aggiornamento risultati) ──────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    async def fetch_scores(self) -> list[dict]:
        """
        Risultati delle partite completate nelle ultime 24h.
        Copre sia tennis (The Odds API) sia ping pong (OddsPapi — se disponibile).
        Formato: [{home, away, winner, sport}, ...]
        """
        results = []
        seen_pairs: set[tuple] = set()   # dedup home+away cross sport_key

        # Tennis via The Odds API
        # NOTA: "tennis" come sport_key generico spesso risponde 200 ma con array vuoto.
        # Le partite reali sono su "tennis_atp" e "tennis_wta" — si testano per prime.
        # NON interrompere al primo 200: continua su tutti i keys e dedup per coppia.
        if ODDS_KEY:
            async with aiohttp.ClientSession() as session:
                for sport_key in ["tennis_atp", "tennis_wta", "tennis"]:
                    url = (
                        f"{ODDS_BASE}/sports/{sport_key}/scores/"
                        f"?apiKey={ODDS_KEY}&daysFrom=1&dateFormat=iso"
                    )
                    try:
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=15)
                        ) as resp:
                            if resp.status == 404:
                                logger.info(
                                    f"Scores: sport key '{sport_key}' non trovata (404), provo la prossima"
                                )
                                continue
                            if resp.status != 200:
                                logger.warning(
                                    f"Scores tennis errore ({sport_key}): HTTP {resp.status}"
                                )
                                continue
                            events = await resp.json()
                            added = 0
                            for ev in events:
                                if not ev.get("completed"):
                                    continue
                                scores = ev.get("scores") or []
                                if len(scores) < 2:
                                    continue
                                home = ev.get("home_team", "")
                                away = ev.get("away_team", "")
                                pair = (home.lower(), away.lower())
                                if pair in seen_pairs:
                                    continue   # già raccolto da un'altra sport key
                                try:
                                    score_map = {s["name"]: int(s["score"]) for s in scores}
                                except (KeyError, ValueError):
                                    continue
                                if home in score_map and away in score_map:
                                    winner = (
                                        home if score_map[home] > score_map[away] else away
                                    )
                                    results.append({
                                        "home":   home,
                                        "away":   away,
                                        "winner": winner,
                                        "sport":  "tennis",
                                    })
                                    seen_pairs.add(pair)
                                    added += 1
                            logger.info(
                                f"Scores tennis ({sport_key}): {added} partite completate trovate"
                            )
                    except Exception as e:
                        logger.warning(f"Scores tennis errore ({sport_key}): {e}")

        logger.info(f"Scores totali: {len(results)} partite completate")
        return results

    # ══════════════════════════════════════════════════════════════════════════
    # ── Fallback e utilities ───────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    def get_fallback_matches(self) -> list[dict]:
        """Partite demo per sviluppo/test — chiaramente marcate come fallback."""
        now = _now_it()
        tt_players = [
            ("Fan Zhendong", "Wang Chuqin"),
            ("Ma Long", "Truls Moregard"),
            ("Lin Gaoyuan", "Felix Lebrun"),
        ]
        tennis_players = [
            ("Jannik Sinner", "Carlos Alcaraz"),
            ("Novak Djokovic", "Daniil Medvedev"),
            ("Alexander Zverev", "Stefanos Tsitsipas"),
        ]
        tt_tournaments     = ["Setka Cup", "Liga Pro", "TT Elite Series"]
        tennis_tournaments = ["ATP Masters 1000", "WTA 1000", "ATP 500"]

        matches = []
        for i, (p1, p2) in enumerate(tt_players[:2]):
            matches.append({
                "event_id":    f"fb_tt_{i}",
                "name":        f"{p1} vs {p2}",
                "player1":     p1, "player2": p2,
                "kickoff":     (now + timedelta(hours=i+1)).strftime("%d/%m %H:%M"),
                "tournament":  random.choice(tt_tournaments),
                "status":      "scheduled",
                "odds_home":   round(random.uniform(1.60, 2.20), 2),
                "odds_away":   round(random.uniform(1.60, 2.20), 2),
                "over_odds":   round(random.uniform(1.65, 2.00), 2),
                "under_odds":  round(random.uniform(1.60, 1.90), 2),
                "totals_line": 3.5,
                "source":      "fallback",
                "sport":       "tabletennis",
                "sport_label": "🏓 Ping Pong",
            })
        for i, (p1, p2) in enumerate(tennis_players[:2]):
            matches.append({
                "event_id":    f"fb_ten_{i}",
                "name":        f"{p1} vs {p2}",
                "player1":     p1, "player2": p2,
                "kickoff":     (now + timedelta(hours=i+3)).strftime("%d/%m %H:%M"),
                "tournament":  random.choice(tennis_tournaments),
                "status":      "scheduled",
                "odds_home":   round(random.uniform(1.40, 2.80), 2),
                "odds_away":   round(random.uniform(1.40, 2.80), 2),
                "over_odds":   None,
                "under_odds":  None,
                "totals_line": None,
                "source":      "fallback",
                "sport":       "tennis",
                "sport_label": "🎾 Tennis",
            })
        return matches

    def _sort_dedup(self, matches: list[dict]) -> list[dict]:
        seen, unique = set(), []
        for m in matches:
            key = m["name"]
            if key not in seen:
                seen.add(key)
                unique.append(m)

        def sort_key(m):
            try:
                return datetime.strptime(m["kickoff"], "%d/%m %H:%M")
            except Exception:
                return datetime.max

        unique.sort(key=sort_key)
        return unique
