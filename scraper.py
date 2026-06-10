import aiohttp
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import random

logger = logging.getLogger(__name__)

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com",
}

SOFASCORE_TT_SPORT_ID = 20
SOFASCORE_BASE = "https://api.sofascore.com/api/v1"

IT_TZ = ZoneInfo("Europe/Rome")

# ── Tornei coperti quasi sempre dai principali book italiani ed europei ──────────
# Fonte: verifica manuale su Snai, Sisal, Goldbet, Betfair, Bet365, William Hill
ALLOWED_TOURNAMENTS = {
    # WTT — serie principale, copertura quasi universale
    "WTT Champions",
    "WTT Star Contender",
    "WTT Contender",
    "WTT Feeder",
    "WTT Cup Finals",
    "WTT Grand Smash",

    # ITTF / WTT World
    "World Championships",
    "ITTF World Championships",
    "ITTF World Tour",
    "World Team Championships",

    # Olimpiadi / Grandi eventi — copertura massima
    "Olympic Games",
    "Olympics",

    # Europei — coperti bene dai book italiani
    "European Championships",
    "European Games",
    "European Top 16",

    # Champions League ping pong (ETTU) — copertura media, incluso per sicurezza
    "ETTU Champions League",
    "Champions League",

    # Bundesliga tedesca — la lega più quotata d'Europa sui book
    "Bundesliga",
    "1. Bundesliga",
    "2. Bundesliga",

    # Liga Pro (Russia/Ucraina) — ottima copertura su Betfair e exchange
    "Liga Pro",
    "Pro League",
}

# Parole chiave parziali — se il nome torneo contiene una di queste viene accettato
ALLOWED_KEYWORDS = [
    "wtt",
    "world",
    "olympic",
    "champions",
    "european",
    "bundesliga",
    "liga pro",
    "pro league",
    "ittf",
    "grand smash",
    "star contender",
    "contender",
]


def _tournament_is_allowed(name: str) -> bool:
    """Ritorna True se il torneo è probabilmente quotato sui principali book."""
    n = name.lower().strip()
    # Check esatto
    if name in ALLOWED_TOURNAMENTS:
        return True
    # Check per keyword parziale
    return any(kw in n for kw in ALLOWED_KEYWORDS)


def _now_it() -> datetime:
    return datetime.now(IT_TZ)


def _ts_to_it(ts: int) -> str:
    dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=ZoneInfo("UTC"))
    dt_it  = dt_utc.astimezone(IT_TZ)
    return dt_it.strftime("%d/%m %H:%M")


class SignalScraper:

    async def fetch_matches(self) -> list[dict]:
        matches = []
        today   = _now_it().strftime("%Y-%m-%d")

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
                            for ev in events[:60]:  # aumentato per compensare il filtro tornei
                                parsed = self._parse_sofascore_event(ev)
                                if parsed:
                                    matches.append(parsed)
                        else:
                            logger.warning(f"SofaScore status {resp.status} per {url}")
                except Exception as e:
                    logger.warning(f"Errore SofaScore {url}: {e}")

        # ── Filtra per tornei trovabili sui book ────────────────────────────────
        allowed = [m for m in matches if _tournament_is_allowed(m.get("tournament", ""))]
        skipped = len(matches) - len(allowed)
        if skipped:
            logger.info(f"Filtrati {skipped} match su tornei non coperti dai book")

        if not allowed:
            logger.info("Nessuna partita su tornei noti — uso fallback")
            allowed = self.get_fallback_matches()

        seen = set()
        unique = []
        for m in allowed:
            key = m.get("name", "")
            if key not in seen:
                seen.add(key)
                unique.append(m)

        logger.info(f"Recuperate {len(unique)} partite su tornei quotati dai book")
        return unique

    def _parse_sofascore_event(self, ev: dict) -> dict | None:
        try:
            home = ev["homeTeam"]["name"]
            away = ev["awayTeam"]["name"]
            ts   = ev.get("startTimestamp", 0)
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
        """
        Fallback con SOLO tornei coperti dai principali book italiani/europei.
        I nomi dei giocatori sono top-ranking mondiali ben noti ai book.
        """
        # Coppie di giocatori top mondiale — tutti presenti sui book maggiori
        players_top = [
            ("Fan Zhendong",        "Wang Chuqin"),
            ("Ma Long",             "Truls Moregard"),
            ("Lin Gaoyuan",         "Felix Lebrun"),
            ("Timo Boll",           "Patrick Franziska"),
            ("Liang Jingkun",       "Simon Gauzy"),
            ("Tomokazu Harimoto",   "Hugo Calderano"),
            ("Quadri Aruna",        "Benedikt Duda"),
            ("Wong Chun Ting",      "Mattias Falck"),
            ("Darko Jorgic",        "Alvaro Robles"),
            ("Dimitrij Ovtcharov",  "Chuang Chih-Yuan"),
        ]

        # SOLO tornei con copertura affidabile sui book italiani/europei
        tournaments_reliable = [
            "WTT Champions",
            "WTT Star Contender",
            "WTT Grand Smash",
            "World Championships",
            "European Championships",
        ]

        matches = []
        now_it  = _now_it()
        random.shuffle(players_top)

        for i, (p1, p2) in enumerate(players_top[:8]):
            hours_ahead = random.randint(1, 18)
            kickoff_dt  = now_it + timedelta(hours=hours_ahead)
            kickoff_str = kickoff_dt.strftime("%d/%m %H:%M")

            odds1 = round(random.uniform(1.55, 2.10), 2)
            odds2 = round(random.uniform(1.80, 3.20), 2)

            matches.append({
                "name":       f"{p1} vs {p2}",
                "player1":    p1,
                "player2":    p2,
                "kickoff":    kickoff_str,
                "tournament": random.choice(tournaments_reliable),
                "status":     "scheduled",
                "odds_home":  odds1,
                "odds_away":  odds2,
                "source":     "fallback",
            })

        return matches
