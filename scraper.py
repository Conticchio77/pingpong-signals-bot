"""
scraper.py — Table Tennis signal scraper via SofaScore
=======================================================
Fonte dati: SofaScore API (gratuita, non ufficiale)
Copre: WTT Contender, WTT Star Contender, World Championships,
       European Championships — tutti quotati su Bet365 Italia.

Nessuna API key richiesta.
"""

import aiohttp
import logging
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IT_TZ  = ZoneInfo("Europe/Rome")

SOFA_BASE = "https://api.sofascore.com/api/v1"
SOFA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept":     "application/json, text/plain, */*",
    "Referer":    "https://www.sofascore.com/",
    "Origin":     "https://www.sofascore.com",
}

# ── Tornei WTT reali disponibili su Bet365 Italia ─────────────────────────────
ALLOWED_TOURNAMENTS = [
    "wtt", "world tt", "world table tennis",
    "world championships", "world cup",
    "contender", "star contender", "grand smash",
    "champions", "cup finals",
    "european championships", "europe top 16",
    "olympic", "commonwealth",
]

# ── Note su dove trovare la partita sul book ───────────────────────────────────
BOOK_NOTE_TEMPLATE = "Cerca su Bet365 → Ping Pong → {tournament}"


def _tournament_ok(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in ALLOWED_TOURNAMENTS)

def _now_it() -> datetime:
    return datetime.now(IT_TZ)

def _unix_to_it(ts: int) -> str:
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IT_TZ)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return _now_it().strftime("%d/%m %H:%M")

def _today_and_tomorrow() -> list[str]:
    now = _now_it()
    return [
        (now + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(3)   # oggi + domani + dopodomani
    ]


class SignalScraper:

    # ══ ENTRY POINT ══════════════════════════════════════════════════════════
    async def fetch_matches(self) -> list[dict]:
        matches = await self._fetch_sofascore()
        if matches:
            logger.info(f"SofaScore: {len(matches)} partite WTT trovate")
            return self._sort(matches)

        logger.warning("SofaScore vuoto — uso fallback demo")
        return self._sort(self.get_fallback_matches())

    # ══ SOFASCORE ════════════════════════════════════════════════════════════
    async def _fetch_sofascore(self) -> list[dict]:
        all_events: list[dict] = []

        async with aiohttp.ClientSession(headers=SOFA_HEADERS) as session:
            for date_str in _today_and_tomorrow():
                url = f"{SOFA_BASE}/sport/table-tennis/scheduled-events/{date_str}"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data   = await resp.json()
                            events = data.get("events", [])
                            logger.info(f"SofaScore {date_str}: {len(events)} eventi")
                            all_events.extend(events)
                        else:
                            logger.warning(f"SofaScore {date_str} HTTP {resp.status}")
                except Exception as e:
                    logger.error(f"SofaScore {date_str} errore: {e}")

        # Log tutti i tornei ricevuti (debug)
        tournaments = sorted(set(
            ev.get("tournament", {}).get("name", "?") for ev in all_events
        ))
        logger.info(f"Tornei SofaScore: {tournaments}")

        # Filtra solo tornei WTT quotati su Bet365
        matches = []
        for ev in all_events:
            t_name = ev.get("tournament", {}).get("name", "")
            if not _tournament_ok(t_name):
                logger.debug(f"Torneo escluso: '{t_name}'")
                continue
            parsed = self._parse_sofascore_event(ev)
            if parsed:
                matches.append(parsed)

        logger.info(f"Dopo filtro WTT: {len(matches)} partite")
        return matches

    def _parse_sofascore_event(self, ev: dict) -> dict | None:
        try:
            home = ev.get("homeTeam", {}).get("name", "")
            away = ev.get("awayTeam", {}).get("name", "")
            if not home or not away:
                return None

            ts          = ev.get("startTimestamp", 0)
            kickoff     = _unix_to_it(ts)
            tournament  = ev.get("tournament", {}).get("name", "Table Tennis")
            category    = ev.get("tournament", {}).get("category", {}).get("name", "")
            full_t_name = f"{category} {tournament}".strip() if category else tournament

            # SofaScore non fornisce quote → l'AI le stima
            # Usiamo dati disponibili per dare contesto all'analizzatore
            home_rank = ev.get("homeTeam", {}).get("ranking", None)
            away_rank = ev.get("awayTeam", {}).get("ranking", None)

            # Stima grezza probabilità da ranking (se disponibile)
            odds_home, odds_away = self._estimate_odds_from_ranking(home_rank, away_rank)

            return {
                "event_id":    str(ev.get("id", "")),
                "name":        f"{home} vs {away}",
                "player1":     home,
                "player2":     away,
                "kickoff":     kickoff,
                "tournament":  full_t_name,
                "status":      "scheduled",
                "odds_home":   odds_home,
                "odds_away":   odds_away,
                "over_odds":   None,
                "under_odds":  None,
                "totals_line": 3.5,
                "source":      "sofascore",
                "book_note":   BOOK_NOTE_TEMPLATE.format(tournament=tournament),
            }
        except Exception as e:
            logger.debug(f"Parse SofaScore event error: {e}")
            return None

    def _estimate_odds_from_ranking(
        self, rank1, rank2
    ) -> tuple[float, float]:
        """
        Stima grezza quote moneyline da ranking mondiale.
        Senza ranking → assume partita equilibrata.
        """
        if rank1 and rank2:
            try:
                r1, r2 = int(rank1), int(rank2)
                # Più basso il ranking = più forte il giocatore
                total   = r1 + r2
                prob1   = r2 / total   # prob. home di vincere
                prob2   = r1 / total
                margin  = 0.05         # 5% margine book
                o1 = round(1 / (prob1 + margin / 2), 2)
                o2 = round(1 / (prob2 + margin / 2), 2)
                # Clamp tra 1.30 e 4.00
                o1 = max(1.30, min(4.00, o1))
                o2 = max(1.30, min(4.00, o2))
                return o1, o2
            except Exception:
                pass
        # Partita equilibrata di default
        return round(random.uniform(1.70, 2.10), 2), round(random.uniform(1.70, 2.10), 2)

    # ══ RISULTATI ════════════════════════════════════════════════════════════
    async def fetch_scores(self) -> list[dict]:
        """Partite WTT terminate ieri e oggi."""
        results = []
        now = _now_it()
        dates = [
            (now - timedelta(days=1)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        ]

        async with aiohttp.ClientSession(headers=SOFA_HEADERS) as session:
            for date_str in dates:
                url = f"{SOFA_BASE}/sport/table-tennis/scheduled-events/{date_str}"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        for ev in data.get("events", []):
                            # Solo partite finite
                            status = ev.get("status", {}).get("type", "")
                            if status != "finished":
                                continue

                            home      = ev.get("homeTeam", {}).get("name", "")
                            away      = ev.get("awayTeam", {}).get("name", "")
                            home_score = ev.get("homeScore", {}).get("current", None)
                            away_score = ev.get("awayScore", {}).get("current", None)

                            if not home or not away:
                                continue
                            if home_score is None or away_score is None:
                                continue

                            winner = home if home_score > away_score else away
                            results.append({
                                "event_id":   str(ev.get("id", "")),
                                "home":       home,
                                "away":       away,
                                "home_score": home_score,
                                "away_score": away_score,
                                "winner":     winner,
                            })
                except Exception as e:
                    logger.warning(f"SofaScore scores {date_str}: {e}")

        logger.info(f"SofaScore scores: {len(results)} partite finite")
        return results

    # ══ FALLBACK DEMO ════════════════════════════════════════════════════════
    def get_fallback_matches(self) -> list[dict]:
        """Usato solo se SofaScore non risponde. Dati demo."""
        players = [
            ("Fan Zhendong",      "Wang Chuqin"),
            ("Ma Long",           "Truls Moregard"),
            ("Lin Gaoyuan",       "Felix Lebrun"),
            ("Timo Boll",         "Patrick Franziska"),
            ("Tomokazu Harimoto", "Hugo Calderano"),
        ]
        tournaments = [
            "WTT Contender Tunis",
            "WTT Star Contender Doha",
            "WTT Contender Zagreb",
        ]
        now_it = _now_it()
        random.shuffle(players)
        matches = []
        for p1, p2 in players[:4]:
            t = random.choice(tournaments)
            kickoff_dt = now_it + timedelta(hours=random.randint(2, 18))
            odds_h = round(random.uniform(1.60, 2.20), 2)
            odds_a = round(random.uniform(1.60, 2.20), 2)
            matches.append({
                "event_id":    "",
                "name":        f"{p1} vs {p2}",
                "player1":     p1,
                "player2":     p2,
                "kickoff":     kickoff_dt.strftime("%d/%m %H:%M"),
                "tournament":  t,
                "status":      "scheduled",
                "odds_home":   odds_h,
                "odds_away":   odds_a,
                "over_odds":   None,
                "under_odds":  None,
                "totals_line": 3.5,
                "source":      "fallback",
                "book_note":   f"⚠️ DEMO — Cerca su Bet365 → Ping Pong → {t}",
            })
        return matches

    # ══ UTILS ════════════════════════════════════════════════════════════════
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
