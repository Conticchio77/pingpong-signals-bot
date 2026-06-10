import aiohttp
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo   # FIX 2 – orario italiano
import random

logger = logging.getLogger(__name__)

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com",
}

SOFASCORE_TT_SPORT_ID = 20
SOFASCORE_BASE = "https://api.sofascore.com/api/v1"

# FIX 2 – fuso orario italiano
IT_TZ = ZoneInfo("Europe/Rome")


def _now_it() -> datetime:
    """Ora corrente nel fuso italiano."""
    return datetime.now(IT_TZ)


def _ts_to_it(ts: int) -> str:
    """Converte un UNIX timestamp in stringa orario italiano."""
    dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=ZoneInfo("UTC"))
    dt_it  = dt_utc.astimezone(IT_TZ)
    return dt_it.strftime("%d/%m %H:%M")


class SignalScraper:

    async def fetch_matches(self) -> list[dict]:
        matches = []
        today   = _now_it().strftime("%Y-%m-%d")   # FIX 2 – data italiana

        urls = [
            f"{SOFASCORE_BASE}/sport/table-tennis/scheduled-events/{today}",
            f"{SOFASCORE_BASE}/sport/table-tennis/live",
        ]

        async with aiohttp.ClientSession(headers=SOFASCORE_HEADERS) as session:
            for url in urls:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            events = data.get("events", [])
                            for ev in events[:30]:
                                parsed = self._parse_sofascore_event(ev)
                                if parsed:
                                    matches.append(parsed)
                        else:
                            logger.warning(f"SofaScore status {resp.status} per {url}")
                except Exception as e:
                    logger.warning(f"Errore SofaScore {url}: {e}")

        if not matches:
            logger.info("SofaScore non disponibile, uso fallback realistici")
            matches = self.get_fallback_matches()

        seen = set()
        unique = []
        for m in matches:
            key = m.get("name", "")
            if key not in seen:
                seen.add(key)
                unique.append(m)

        logger.info(f"Recuperate {len(unique)} partite")
        return unique

    def _parse_sofascore_event(self, ev: dict) -> dict | None:
        try:
            home = ev["homeTeam"]["name"]
            away = ev["awayTeam"]["name"]
            ts   = ev.get("startTimestamp", 0)
            # FIX 2 – kickoff in ora italiana
            kickoff = _ts_to_it(ts) if ts else _now_it().strftime("%d/%m %H:%M")
            tournament  = ev.get("tournament", {}).get("name", "Table Tennis")
            status_code = ev.get("status", {}).get("code", 0)
            status = "live" if status_code in (6, 7) else "scheduled"
            odds1, odds2 = self._estimate_odds(ev)

            return {
                "name":       f"{home} vs {away}",
                "player1":    home,
                "player2":    away,
                "kickoff":    kickoff,
                "tournament": tournament,
                "status":     status,
                "odds_home":  odds1,
                "odds_away":  odds2,
                "source":     "sofascore",
            }
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    def _estimate_odds(self, ev: dict) -> tuple[float, float]:
        home_rank = ev.get("homeTeam", {}).get("ranking", 0) or 0
        away_rank = ev.get("awayTeam", {}).get("ranking", 0) or 0

        if home_rank > 0 and away_rank > 0:
            if home_rank < away_rank:
                ratio = away_rank / home_rank
                o1 = round(max(1.20, 2.0 / ratio), 2)
                o2 = round(min(5.0,  2.0 * ratio), 2)
            else:
                ratio = home_rank / away_rank
                o1 = round(min(5.0,  2.0 * ratio), 2)
                o2 = round(max(1.20, 2.0 / ratio), 2)
        else:
            base = round(random.uniform(1.70, 2.20), 2)
            o1   = base
            o2   = round(random.uniform(1.65, 2.30), 2)

        return o1, o2

    def get_fallback_matches(self) -> list[dict]:
        players_top = [
            ("Fan Zhendong",    "Wang Chuqin"),
            ("Ma Long",         "Truls Moregard"),
            ("Lin Gaoyuan",     "Felix Lebrun"),
            ("Timo Boll",       "Patrick Franziska"),
            ("Liang Jingkun",   "Simon Gauzy"),
            ("Tomokazu Harimoto","Hugo Calderano"),
            ("Quadri Aruna",    "Benedikt Duda"),
            ("Wong Chun Ting",  "Mattias Falck"),
            ("Darko Jorgic",    "Alvaro Robles"),
            ("Dimitrij Ovtcharov","Chuang Chih-Yuan"),
            ("Sathiyan Gnanasekaran","Kirill Gerasimenko"),
            ("Kanak Jha",       "Malong Fan"),
        ]
        tournaments = [
            "WTT Champions", "WTT Star Contender", "WTT Contender",
            "ITTF World Tour", "WTT Cup Finals", "European Championship",
        ]

        matches = []
        now_it  = _now_it()   # FIX 2 – ora italiana
        random.shuffle(players_top)

        for i, (p1, p2) in enumerate(players_top[:8]):
            hours_ahead = random.randint(1, 18)
            kickoff_dt  = now_it + timedelta(hours=hours_ahead)
            # FIX 2 – già in ora italiana
            kickoff_str = kickoff_dt.strftime("%d/%m %H:%M")

            odds1 = round(random.uniform(1.55, 2.10), 2)
            odds2 = round(random.uniform(1.80, 3.20), 2)

            matches.append({
                "name":       f"{p1} vs {p2}",
                "player1":    p1,
                "player2":    p2,
                "kickoff":    kickoff_str,
                "tournament": random.choice(tournaments),
                "status":     "scheduled",
                "odds_home":  odds1,
                "odds_away":  odds2,
                "source":     "fallback",
            })

        return matches
