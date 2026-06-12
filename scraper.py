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

# Sport key corretto per tennis tavolo su The Odds API
TT_SPORT = "tabletennis"


def _now_it() -> datetime:
    return datetime.now(IT_TZ)


def _iso_to_it(iso: str) -> str:
    """Converte ISO8601 UTC → ora italiana formattata dd/mm HH:MM."""
    try:
        iso = iso.replace("Z", "+00:00")
        dt  = datetime.fromisoformat(iso).astimezone(IT_TZ)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return _now_it().strftime("%d/%m %H:%M")


class SignalScraper:

    async def fetch_matches(self) -> list[dict]:
        if not ODDS_KEY:
            logger.warning("ODDS_API_KEY non impostata — uso fallback interno")
            return self._sort(self.get_fallback_matches())

        matches = await self._fetch_odds_api()
        if matches:
            logger.info(f"The Odds API: {len(matches)} partite con quote reali")
            return self._sort(matches)

        logger.warning("The Odds API: nessuna partita disponibile — uso fallback")
        return self._sort(self.get_fallback_matches())

    # ── The Odds API ──────────────────────────────────────────────────────────
    async def _fetch_odds_api(self) -> list[dict]:
        matches = []
        # Regioni EU + UK per avere book italiani/europei (Betfair, Bet365, Unibet ecc.)
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
                    remaining = resp.headers.get("x-requests-remaining", "?")
                    used      = resp.headers.get("x-requests-used", "?")
                    logger.info(f"Odds API — usate:{used} rimaste:{remaining}")

                    if resp.status == 200:
                        events = await resp.json()
                        logger.info(f"Odds API: {len(events)} eventi ricevuti")
                        for ev in events:
                            parsed = self._parse_event(ev)
                            if parsed:
                                matches.append(parsed)
                    elif resp.status == 401:
                        logger.error("Odds API: chiave non valida (401)")
                    elif resp.status == 422:
                        logger.warning("Odds API: sport non disponibile oggi (422)")
                    else:
                        txt = await resp.text()
                        logger.warning(f"Odds API status {resp.status}: {txt[:120]}")
            except Exception as e:
                logger.error(f"Odds API errore connessione: {e}")

        return matches

    def _parse_event(self, ev: dict) -> dict | None:
        try:
            commence = ev.get("commence_time", "")
            kickoff  = _iso_to_it(commence)
            sport    = ev.get("sport_title", "Table Tennis")

            # Estrai le MIGLIORI quote disponibili fra tutti i bookmaker
            best_h2h: dict[str, float] = {}
            over_odds, under_odds, totals_line = None, None, 3.5
            bookmakers_used = []

            for bm in ev.get("bookmakers", []):
                bookmakers_used.append(bm.get("key", ""))
                for market in bm.get("markets", []):
                    if market["key"] == "h2h":
                        for o in market.get("outcomes", []):
                            name  = o["name"]
                            price = float(o["price"])
                            if name not in best_h2h or price > best_h2h[name]:
                                best_h2h[name] = price

                    elif market["key"] == "totals":
                        for o in market.get("outcomes", []):
                            price = float(o["price"])
                            point = float(o.get("point", 3.5))
                            if o["name"] == "Over":
                                if over_odds is None or price > over_odds:
                                    over_odds  = price
                                    totals_line = point
                            elif o["name"] == "Under":
                                if under_odds is None or price > under_odds:
                                    under_odds = price

            if len(best_h2h) < 2:
                return None

            players = list(best_h2h.keys())
            p1, p2  = players[0], players[1]

            return {
                "name":         f"{p1} vs {p2}",
                "player1":      p1,
                "player2":      p2,
                "kickoff":      kickoff,
                "tournament":   sport,
                "status":       "scheduled",
                "odds_home":    round(best_h2h[p1], 3),
                "odds_away":    round(best_h2h[p2], 3),
                "over_odds":    round(over_odds,  3) if over_odds  else None,
                "under_odds":   round(under_odds, 3) if under_odds else None,
                "totals_line":  totals_line,
                "bookmakers":   bookmakers_used[:5],   # solo per debug/log
                "source":       "odds_api",
            }
        except Exception as e:
            logger.debug(f"Parse event error: {e}")
            return None

    # ── Fallback interno ──────────────────────────────────────────────────────
    def get_fallback_matches(self) -> list[dict]:
        """
        Usato solo se The Odds API non è configurata o non risponde.
        Quote stimate — NON usare per piazzare scommesse reali.
        """
        players = [
            ("Fan Zhendong",       "Wang Chuqin"),
            ("Ma Long",            "Truls Moregard"),
            ("Lin Gaoyuan",        "Felix Lebrun"),
            ("Timo Boll",          "Patrick Franziska"),
            ("Tomokazu Harimoto",  "Hugo Calderano"),
            ("Quadri Aruna",       "Benedikt Duda"),
            ("Wong Chun Ting",     "Mattias Falck"),
            ("Dimitrij Ovtcharov", "Simon Gauzy"),
        ]
        tournaments = [
            "WTT Champions", "WTT Star Contender",
            "WTT Grand Smash", "World Championships",
        ]
        now_it = _now_it()
        random.shuffle(players)
        matches = []
        for p1, p2 in players[:6]:
            kickoff_dt = now_it + timedelta(hours=random.randint(1, 20))
            o1 = round(random.uniform(1.50, 2.10), 2)
            o2 = round(random.uniform(1.80, 3.20), 2)
            matches.append({
                "name":        f"{p1} vs {p2}",
                "player1":     p1,
                "player2":     p2,
                "kickoff":     kickoff_dt.strftime("%d/%m %H:%M"),
                "tournament":  random.choice(tournaments),
                "status":      "scheduled",
                "odds_home":   o1,
                "odds_away":   o2,
                "over_odds":   round(random.uniform(1.65, 2.05), 2),
                "under_odds":  round(random.uniform(1.60, 1.95), 2),
                "totals_line": 3.5,
                "bookmakers":  [],
                "source":      "fallback",
            })
        return matches

    # ── Utility ───────────────────────────────────────────────────────────────
    def _sort(self, matches: list[dict]) -> list[dict]:
        """Deduplica e ordina per kickoff crescente (più vicino prima)."""
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
