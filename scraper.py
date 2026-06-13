import aiohttp
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger  = logging.getLogger(__name__)
IT_TZ   = ZoneInfo("Europe/Rome")

ODDS_KEY  = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"
TT_SPORT  = "tabletennis"

# ── Tornei ammessi (quelli visibili sul book dell'utente) ──────────────────────
# Matching parziale: se il nome torneo dell'API CONTIENE una di queste stringhe → OK
ALLOWED_TOURNAMENTS = [
    # Virtuali/simulati — massima copertura sui book italiani
    "pro league",
    "setka cup",
    "masters",
    "tt-cup",
    "tt cup",
    "liga pro",
    "russia",
    "ukraine",
    "czech",
    "virtual",
    # Reali WTT
    "wtt",
    "world",
    "contender",
    "champions",
    "grand smash",
    "star contender",
    # Europei
    "european",
    "bundesliga",
]

def _tournament_ok(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in ALLOWED_TOURNAMENTS)

def _now_it() -> datetime:
    return datetime.now(IT_TZ)

def _iso_to_it(iso: str) -> str:
    try:
        iso = iso.replace("Z", "+00:00")
        dt  = datetime.fromisoformat(iso).astimezone(IT_TZ)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return _now_it().strftime("%d/%m %H:%M")


class SignalScraper:

    async def fetch_matches(self) -> list[dict]:
        if not ODDS_KEY:
            logger.warning("ODDS_API_KEY non impostata — uso fallback")
            return self._sort(self.get_fallback_matches())

        matches = await self._fetch_odds_api()
        if matches:
            logger.info(f"The Odds API: {len(matches)} partite nei tornei ammessi")
            return self._sort(matches)

        logger.warning("Nessuna partita trovata nei tornei ammessi oggi")
        return []

    # ── The Odds API: partite + quote ─────────────────────────────────────────
    async def _fetch_odds_api(self) -> list[dict]:
        matches = []
        url = (
            f"{ODDS_BASE}/sports/{TT_SPORT}/odds/"
            f"?apiKey={ODDS_KEY}"
            f"&regions=eu,uk"
            f"&markets=h2h,totals"
            f"&oddsFormat=decimal"
            f"&dateFormat=iso"
        )
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    rem  = resp.headers.get("x-requests-remaining", "?")
                    used = resp.headers.get("x-requests-used", "?")
                    logger.info(f"Odds API — usate:{used} rimaste:{rem}")

                    if resp.status == 200:
                        events = await resp.json()
                        logger.info(f"Odds API: {len(events)} eventi totali ricevuti")
                        for ev in events:
                            parsed = self._parse_event(ev)
                            if parsed:
                                t = parsed.get("tournament", "")
                                if _tournament_ok(t):
                                    matches.append(parsed)
                                else:
                                    logger.debug(f"Torneo escluso: {t}")
                        logger.info(f"Dopo filtro tornei: {len(matches)} partite")
                    else:
                        txt = await resp.text()
                        logger.warning(f"Odds API status {resp.status}: {txt[:120]}")
            except Exception as e:
                logger.error(f"Odds API errore: {e}")
        return matches

    # ── The Odds API: risultati partite completate ────────────────────────────
    async def fetch_scores(self) -> list[dict]:
        """
        Recupera i risultati delle partite completate nelle ultime 24h.
        Ritorna lista di dict con: home, away, home_score, away_score, completed
        """
        if not ODDS_KEY:
            return []
        results = []
        url = (
            f"{ODDS_BASE}/sports/{TT_SPORT}/scores/"
            f"?apiKey={ODDS_KEY}&daysFrom=1&dateFormat=iso"
        )
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        events = await resp.json()
                        for ev in events:
                            if not ev.get("completed"):
                                continue
                            scores = ev.get("scores") or []
                            if len(scores) < 2:
                                continue
                            # scores è lista di {name, score}
                            score_map = {s["name"]: int(s["score"]) for s in scores}
                            home = ev.get("home_team", "")
                            away = ev.get("away_team", "")
                            if home in score_map and away in score_map:
                                results.append({
                                    "event_id":   ev.get("id", ""),
                                    "home":       home,
                                    "away":       away,
                                    "home_score": score_map[home],
                                    "away_score": score_map[away],
                                    "winner":     home if score_map[home] > score_map[away] else away,
                                })
            except Exception as e:
                logger.warning(f"Scores API errore: {e}")
        logger.info(f"Scores API: {len(results)} partite completate")
        return results

    def _parse_event(self, ev: dict) -> dict | None:
        try:
            home = ev.get("home_team", "")
            away = ev.get("away_team", "")
            if not home or not away:
                return None

            kickoff = _iso_to_it(ev.get("commence_time", ""))
            sport   = ev.get("sport_title", "Table Tennis")

            best_h2h: dict[str, float] = {}
            over_odds, under_odds, totals_line = None, None, 3.5

            for bm in ev.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market["key"] == "h2h":
                        for o in market.get("outcomes", []):
                            p = float(o["price"])
                            if o["name"] not in best_h2h or p > best_h2h[o["name"]]:
                                best_h2h[o["name"]] = p
                    elif market["key"] == "totals":
                        for o in market.get("outcomes", []):
                            p     = float(o["price"])
                            point = float(o.get("point", 3.5))
                            if o["name"] == "Over" and (over_odds is None or p > over_odds):
                                over_odds, totals_line = p, point
                            elif o["name"] == "Under" and (under_odds is None or p > under_odds):
                                under_odds = p

            if home not in best_h2h or away not in best_h2h:
                return None

            return {
                "event_id":    ev.get("id", ""),
                "name":        f"{home} vs {away}",
                "player1":     home,
                "player2":     away,
                "kickoff":     kickoff,
                "tournament":  sport,
                "status":      "scheduled",
                "odds_home":   round(best_h2h[home], 3),
                "odds_away":   round(best_h2h[away], 3),
                "over_odds":   round(over_odds,  3) if over_odds  else None,
                "under_odds":  round(under_odds, 3) if under_odds else None,
                "totals_line": totals_line,
                "source":      "odds_api",
            }
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    # ── Fallback ───────────────────────────────────────────────────────────────
    def get_fallback_matches(self) -> list[dict]:
        players = [
            ("Fan Zhendong", "Wang Chuqin"), ("Ma Long", "Truls Moregard"),
            ("Lin Gaoyuan", "Felix Lebrun"), ("Timo Boll", "Patrick Franziska"),
            ("Tomokazu Harimoto", "Hugo Calderano"), ("Quadri Aruna", "Benedikt Duda"),
        ]
        tournaments = ["WTT Contender Zagreb", "Pro League", "Setka Cup", "Masters"]
        now_it = _now_it()
        random.shuffle(players)
        matches = []
        for p1, p2 in players[:5]:
            kickoff_dt = now_it + timedelta(hours=random.randint(1, 12))
            matches.append({
                "event_id":    "",
                "name":        f"{p1} vs {p2}",
                "player1":     p1,
                "player2":     p2,
                "kickoff":     kickoff_dt.strftime("%d/%m %H:%M"),
                "tournament":  random.choice(tournaments),
                "status":      "scheduled",
                "odds_home":   round(random.uniform(1.55, 2.10), 2),
                "odds_away":   round(random.uniform(1.80, 3.00), 2),
                "over_odds":   round(random.uniform(1.65, 2.00), 2),
                "under_odds":  round(random.uniform(1.60, 1.90), 2),
                "totals_line": 3.5,
                "source":      "fallback",
            })
        return matches

    def _sort(self, matches: list[dict]) -> list[dict]:
        seen, unique = set(), []
        for m in matches:
            if m["name"] not in seen:
                seen.add(m["name"])
                unique.append(m)
        def key(m):
            try:
                return datetime.strptime(m["kickoff"], "%d/%m %H:%M")
            except Exception:
                return datetime.max
        unique.sort(key=key)
        return unique
