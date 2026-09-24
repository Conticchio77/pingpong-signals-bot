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
               Sport key: scoperto dinamicamente da /sports/ (solo tornei
               attivi, es. "tennis_wta_guadalajara_open") — nessuna chiave
               aggregata "tennis"/"tennis_atp"/"tennis_wta" (non esiste)

Variabili Railway da aggiungere:
  ODDSPAPI_KEY  = <la tua chiave da oddspapi.io>
  ODDS_API_KEY  = <la tua chiave esistente da the-odds-api.com>

Ogni partita ha il campo  sport_label = "🏓 Ping Pong" | "🎾 Tennis"
usato da bot.py per differenziare i segnali nella UI.
"""

import aiohttp
import asyncio
import json
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
        self._tennis_oddspapi_id: int | None = None  # cache ID OddsPapi per tennis (fallback)
        self._odds_tennis_keys: list[str] | None = None  # cache sport_key torneo tennis attivi (The Odds API)
        self.db = db   # se presente, cache persistente su DB (evita 429 da troppe /sports)
        # Stato quota, aggiornato ad ogni chiamata reale alle API — usato da bot.py
        # per avvisare l'admin quando i segnali si fermano per quota esaurita.
        self.tennis_quota_ok   = True
        self.pingpong_quota_ok = True
        # FIX: throttle tra chiamate OddsPapi. Nel log del 24/09 quasi TUTTE
        # le fixture venivano scartate per "nessuna quota reale" — non perché
        # mancassero davvero le quote, ma perché il bot sparava le richieste
        # /fixtures + N x /odds una via l'altra senza pausa, e OddsPapi
        # rispondeva 429 "rate limited" a quasi tutte (persino la seconda
        # /fixtures nello stesso scan). Ora si aspetta un minimo tra due
        # chiamate consecutive a OddsPapi, qualunque sia l'endpoint.
        self._oddspapi_min_interval = 0.8  # secondi
        self._last_oddspapi_call    = 0.0
        self._oddspapi_lock         = asyncio.Lock()
        self._oddspapi_schema_logged = False  # dump struttura /odds solo una volta

    async def _throttle_oddspapi(self):
        """Aspetta il tempo minimo dall'ultima chiamata OddsPapi prima di procedere."""
        async with self._oddspapi_lock:
            now  = asyncio.get_event_loop().time()
            wait = self._oddspapi_min_interval - (now - self._last_oddspapi_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_oddspapi_call = asyncio.get_event_loop().time()

    # ── Entry point principale ─────────────────────────────────────────────────
    async def fetch_matches(self, sport: str = "both") -> list[dict]:
        """Restituisce partite. sport: "both" | "tennis" | "tabletennis" —
        limita le chiamate API al solo sport richiesto, per non consumare
        inutilmente la quota OddsPapi (250 richieste/mese) durante gli scan
        automatici tennis-only (il ping pong ha il suo job dedicato)."""
        results = []
        want_pingpong = sport in ("both", "tabletennis")
        want_tennis   = sport in ("both", "tennis")

        # 1. Ping Pong via OddsPapi
        if want_pingpong and ODDSPAPI_KEY:
            tt = await self._fetch_oddspapi_tt()
            logger.info(f"OddsPapi Ping Pong: {len(tt)} partite")
            results.extend(tt)
        elif want_pingpong:
            logger.warning("ODDSPAPI_KEY non impostata — ping pong saltato")

        # 2. Tennis via The Odds API (fonte primaria: 500 crediti/mese, h2h+totals)
        tennis_matches = []
        if want_tennis and ODDS_KEY:
            tennis_matches = await self._fetch_odds_api_tennis()
            logger.info(f"The Odds API Tennis: {len(tennis_matches)} partite")

        # 2b. Fallback tennis via OddsPapi — riusa la stessa ODDSPAPI_KEY già
        # attiva per il ping pong (nessuna nuova chiave da configurare). Si
        # attiva SOLO quando The Odds API non ha restituito nulla di reale:
        # o perché la quota (500 crediti/mese) è esaurita, o perché
        # ODDS_API_KEY non è affatto configurata. Così la quota OddsPapi
        # condivisa (250 richieste/mese) resta protetta per il ping pong e
        # viene toccata dal tennis solo quando serve davvero.
        if want_tennis and not tennis_matches and ODDSPAPI_KEY:
            if not ODDS_KEY:
                logger.info("ODDS_API_KEY non impostata — uso OddsPapi come fonte tennis primaria")
            else:
                logger.warning("The Odds API tennis a quota esaurita/non disponibile — provo fallback OddsPapi")
            tennis_matches = await self._fetch_oddspapi_tennis()
            logger.info(f"OddsPapi Tennis (fallback): {len(tennis_matches)} partite")
        elif want_tennis and not tennis_matches and not ODDS_KEY:
            logger.warning("Nessuna API tennis configurata (ODDS_API_KEY/ODDSPAPI_KEY) — tennis saltato")

        results.extend(tennis_matches)

        # Fallback demo se nessuna API ha restituito nulla
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
            await self._throttle_oddspapi()
            async with session.get(
                f"{ODDSPAPI_BASE}/sports",
                params={"apiKey": ODDSPAPI_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    logger.warning(f"OddsPapi /sports status {r.status}")
                    if r.status == 429:
                        self.pingpong_quota_ok = False
                    return None
                self.pingpong_quota_ok = True
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
                await self._throttle_oddspapi()
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
                        if r.status == 429:
                            self.pingpong_quota_ok = False
                        return []
                    self.pingpong_quota_ok = True
                    fixtures = await r.json()
                    # fixtures può essere lista diretta o {"data": [...]}
                    if isinstance(fixtures, dict):
                        fixtures = fixtures.get("data") or fixtures.get("fixtures") or []
                    logger.info(f"OddsPapi: {len(fixtures)} fixture ricevute")

                    # Limita a 4 fixture più vicine nel tempo per evitare rate limit
                    # (ogni fixture = 1 chiamata API per le quote — piano gratuito 250/mese)
                    def _sort_key(f):
                        s = f.get("startDate") or f.get("startTime") or ""
                        try:
                            return datetime.fromisoformat(s.replace("Z", "+00:00"))
                        except Exception:
                            return datetime.max.replace(tzinfo=IT_TZ)
                    # FIX: filtra SOLO partite future (kickoff >= adesso). Prima si
                    # ordinavano le fixture dell'intera giornata (00:00-22:00) e si
                    # prendevano le prime N per orario — ma il ping pong (Setka Cup,
                    # Liga Pro, ecc.) gioca 24/7, quindi allo scan delle 07:00 le
                    # "prime N" erano quasi sempre partite già giocate stanotte tra
                    # mezzanotte e le 7. Risultato: nessun segnale ping pong è mai
                    # stato generato. Ora prendiamo le prime N future, qualsiasi ora.
                    now = _now_it()
                    future_fixtures = [f for f in fixtures if _sort_key(f) >= now]
                    fixtures = sorted(future_fixtures, key=_sort_key)[:4]  # max 4 per risparmiare quota OddsPapi
                    logger.info(f"OddsPapi: limitate a {len(fixtures)} fixture future (evita rate limit)")
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
                    await self._throttle_oddspapi()
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
                            # FIX: prima qui non si logava nulla se lo status era 200
                            # ma l'estrazione non trovava comunque quote — quindi ogni
                            # "fixture scartata" era una scatola nera (rate limit? corpo
                            # vuoto? struttura diversa da quella attesa?). Ora logghiamo
                            # cosa è arrivato per capire la vera causa.
                            if odds_home is None:
                                bm_odds_dict = data.get("bookmakerOdds") or {}
                                n_bm = len(bm_odds_dict)
                                if not self._oddspapi_schema_logged and bm_odds_dict:
                                    # Prende UN bookmaker a caso e ne mostra la struttura
                                    # grezza (solo la prima volta, per non intasare i
                                    # log): ci serve capire come sono fatti gli
                                    # "outcomes" per correggere l'estrazione — finora
                                    # abbiamo solo ipotizzato la forma e ci sbagliavamo.
                                    first_slug = next(iter(bm_odds_dict))
                                    sample = {first_slug: bm_odds_dict[first_slug]}
                                    logger.warning(
                                        f"OddsPapi /odds — struttura reale (fixtureId={fid}): "
                                        f"chiavi top-level: {list(data.keys())} | "
                                        f"campione 1 bookmaker: {json.dumps(sample)[:1500]}"
                                    )
                                    self._oddspapi_schema_logged = True
                                logger.warning(
                                    f"OddsPapi /odds 200 ma nessuna quota estratta per "
                                    f"{p1} vs {p2} (fixtureId={fid}): {n_bm} bookmaker"
                                )
                        else:
                            body = await r.text()
                            logger.warning(
                                f"OddsPapi /odds status {r.status} per {p1} vs {p2} "
                                f"(fixtureId={fid}): {body[:200]}"
                            )
                except Exception as e:
                    logger.warning(f"OddsPapi /odds eccezione fixture {fid} ({p1} vs {p2}): {e}")

            # FIX: prima qui si inventavano quote con random.uniform() quando
            # OddsPapi non restituiva quote reali per la fixture — un "segnale"
            # calcolato contro numeri casuali non è una partita vera, per quanto
            # l'evento in sé lo fosse. Ora, senza quote reali, scartiamo la
            # fixture: niente segnale è meglio di un segnale fasullo.
            if odds_home is None or odds_away is None:
                logger.info(f"OddsPapi: nessuna quota reale per {p1} vs {p2} — fixture scartata")
                return None
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

    # ══════════════════════════════════════════════════════════════════════════
    # ── OddsPapi: Tennis (fallback quando The Odds API è a quota esaurita) ─────
    # ══════════════════════════════════════════════════════════════════════════

    async def _get_tennis_oddspapi_sport_id(self, session: aiohttp.ClientSession) -> int | None:
        """Scopre l'ID OddsPapi per il tennis (distinto dal ping pong: 'tennis'
        è contenuto in 'table tennis', quindi va escluso esplicitamente).
        Cache in-memory + persistente su DB come per il ping pong."""
        if self._tennis_oddspapi_id:
            return self._tennis_oddspapi_id

        if self.db is not None:
            cached = self.db.get_setting("tennis_oddspapi_sport_id")
            if cached:
                self._tennis_oddspapi_id = int(cached)
                logger.info(f"OddsPapi: tennis sportId={cached} (da cache DB)")
                return self._tennis_oddspapi_id

        try:
            await self._throttle_oddspapi()
            async with session.get(
                f"{ODDSPAPI_BASE}/sports",
                params={"apiKey": ODDSPAPI_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    logger.warning(f"OddsPapi /sports status {r.status} (ricerca tennis)")
                    if r.status == 429:
                        self.pingpong_quota_ok = False
                    return None
                sports = await r.json()
                for s in sports:
                    name = (s.get("name") or s.get("slug") or "").lower()
                    if "tennis" in name and "table" not in name:
                        self._tennis_oddspapi_id = s.get("sportId") or s.get("id")
                        logger.info(f"OddsPapi: tennis sportId={self._tennis_oddspapi_id} ({s.get('name')})")
                        if self.db is not None and self._tennis_oddspapi_id:
                            self.db.set_setting("tennis_oddspapi_sport_id", str(self._tennis_oddspapi_id))
                        return self._tennis_oddspapi_id
                names = [s.get("name", "?") for s in sports]
                logger.warning(f"OddsPapi: tennis non trovato. Sport disponibili: {names}")
        except Exception as e:
            logger.error(f"OddsPapi /sports errore (ricerca tennis): {e}")
        return None

    async def _fetch_oddspapi_tennis(self) -> list[dict]:
        """Fallback tennis: stessa logica del ping pong via OddsPapi, ma
        limitata a 3 fixture per scan (invece di 4) per lasciare più margine
        alla quota condivisa 250/mese, dato che si attiva solo quando serve."""
        matches = []
        async with aiohttp.ClientSession() as session:
            sport_id = await self._get_tennis_oddspapi_sport_id(session)
            if sport_id is None:
                logger.warning("OddsPapi: sport ID tennis non trovato — fallback tennis non disponibile")
                return []

            today    = _now_it().strftime("%Y-%m-%d")
            tomorrow = (_now_it() + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                await self._throttle_oddspapi()
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
                    logger.info(f"OddsPapi fixtures tennis — richieste rimaste: {rem}")
                    if r.status != 200:
                        txt = await r.text()
                        logger.warning(f"OddsPapi fixtures tennis status {r.status}: {txt[:120]}")
                        if r.status == 429:
                            self.pingpong_quota_ok = False
                        return []
                    fixtures = await r.json()
                    if isinstance(fixtures, dict):
                        fixtures = fixtures.get("data") or fixtures.get("fixtures") or []
                    logger.info(f"OddsPapi tennis: {len(fixtures)} fixture ricevute")

                    def _sort_key(f):
                        s = f.get("startDate") or f.get("startTime") or ""
                        try:
                            return datetime.fromisoformat(s.replace("Z", "+00:00"))
                        except Exception:
                            return datetime.max.replace(tzinfo=IT_TZ)

                    # FIX: stesso bug del ping pong — filtra SOLO partite future
                    # (kickoff >= adesso) prima di prendere le prime 3. Prima si
                    # ordinava l'intera finestra "oggi→domani" e si prendevano le
                    # più vicine per orario, che spesso erano già passate (es. un
                    # torneo sudamericano con kickoff alle 02:00 IT, già finito
                    # quando lo scan gira nel pomeriggio) — venivano scartate a
                    # valle dall'analyzer (kickoff nel passato) sprecando la
                    # richiesta e restituendo 0 segnali.
                    now = _now_it()
                    future_fixtures = [f for f in fixtures if _sort_key(f) >= now]
                    fixtures = sorted(future_fixtures, key=_sort_key)[:3]  # max 3: risparmia quota condivisa
                    logger.info(f"OddsPapi tennis: limitate a {len(fixtures)} fixture future (fallback, risparmio quota)")
            except Exception as e:
                logger.error(f"OddsPapi fixtures tennis errore: {e}")
                return []

            for fix in fixtures:
                parsed = await self._parse_oddspapi_fixture(session, fix)
                if parsed:
                    # _parse_oddspapi_fixture marca tutto come ping pong: qui
                    # correggiamo sport/label/source per il tennis.
                    parsed["sport"]       = "tennis"
                    parsed["sport_label"] = "🎾 Tennis"
                    if parsed.get("source") == "oddspapi":
                        parsed["source"] = "oddspapi_tennis"
                    elif parsed.get("source") == "oddspapi_noodds":
                        parsed["source"] = "oddspapi_tennis_noodds"
                    matches.append(parsed)

        return matches

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
        """Recupera partite di tennis da The Odds API.

        RISPARMIO QUOTA: non esistono chiavi aggregate "tennis"/"tennis_atp"/
        "tennis_wta" — The Odds API espone solo chiavi per singolo torneo
        (es. "tennis_wta_guadalajara_open"), come conferma /sports/. Prima
        interrogavamo 3 chiavi fisse a indovinare (spesso inesistenti, ma
        comunque a pagamento se valide), bruciando fino a 6 crediti a scan
        anche senza trovare nulla. Ora chiediamo prima /sports/ (gratis, non
        consuma crediti) per sapere quali tornei di tennis sono REALMENTE
        attivi in questo momento, e interroghiamo /odds/ solo per quelli.
        Manteniamo markets=h2h,totals: vogliamo sia il vincente (h2h) sia
        l'over (totals), quindi il costo resta 2 crediti per torneo attivo.
        """
        matches = []

        async with aiohttp.ClientSession() as session:
            tennis_keys = await self._get_active_tennis_sport_keys(session)
            if not tennis_keys:
                logger.info("Odds tennis: nessun torneo attivo al momento — salto la chiamata /odds/")
                return []

            for sport_key in tennis_keys:
                url = (
                    f"{ODDS_BASE}/sports/{sport_key}/odds/"
                    f"?apiKey={ODDS_KEY}"
                    f"&regions=eu"
                    f"&markets=h2h,totals"
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
                            self.tennis_quota_ok = True
                            events = await resp.json()
                            logger.info(
                                f"The Odds API ({sport_key}): {len(events)} eventi"
                            )
                            for ev in events:
                                parsed = self._parse_odds_api_tennis(ev)
                                if parsed:
                                    matches.append(parsed)
                        elif resp.status == 404:
                            logger.info(f"Sport key '{sport_key}' non trovato, provo il prossimo")
                        else:
                            txt = await resp.text()
                            logger.warning(
                                f"The Odds API ({sport_key}) status {resp.status}: {txt[:120]}"
                            )
                            if resp.status == 401:
                                self.tennis_quota_ok = False
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
            over_odds = under_odds = totals_line = None

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
                    elif market["key"] == "totals":
                        for o in market.get("outcomes", []):
                            p   = float(o["price"])
                            pt  = o.get("point")
                            if o["name"].lower() == "over":
                                if over_odds is None or p > over_odds:
                                    over_odds   = p
                                    totals_line = pt
                            elif o["name"].lower() == "under":
                                if under_odds is None or p > under_odds:
                                    under_odds = p

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
                "over_odds":      round(over_odds,  3) if over_odds  else None,
                "under_odds":     round(under_odds, 3) if under_odds else None,
                "totals_line":    totals_line,
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

    async def _get_active_tennis_sport_keys(self, session: aiohttp.ClientSession) -> list[str]:
        """The Odds API: l'aggregatore 'tennis' funziona per /odds/ ma NON per /scores/
        (risponde 'Unknown sport'). /scores/ richiede il sport_key del singolo torneo
        attivo (es. tennis_atp_wimbledon). Li scopriamo da /sports/ (attivi = in season)."""
        if self._odds_tennis_keys is not None:
            return self._odds_tennis_keys
        try:
            async with session.get(
                f"{ODDS_BASE}/sports/",
                params={"apiKey": ODDS_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    logger.warning(f"The Odds API /sports status {r.status}")
                    return []
                all_sports = await r.json()
                keys = [
                    s["key"] for s in all_sports
                    if s.get("key", "").startswith("tennis") and s.get("active")
                ]
                logger.info(f"The Odds API: torneo tennis attivi trovati: {keys}")
                self._odds_tennis_keys = keys
                return keys
        except Exception as e:
            logger.error(f"The Odds API /sports errore: {e}")
            return []

    async def fetch_scores(self, sport: str = "both") -> list[dict]:
        """
        Risultati delle partite completate nelle ultime 24h.
        Copre sia tennis (The Odds API) sia ping pong (OddsPapi).
        Formato: [{home, away, winner, sport}, ...]

        sport: "both" | "tennis" | "tabletennis" — limita le chiamate API
        al solo sport richiesto. Usato per evitare di consumare la quota
        OddsPapi (250 req/mese) ad ogni controllo automatico dei risultati
        tennis (che gira ogni 30 min): il ping pong ha una schedulazione
        propria, molto più rada, separata in bot.py.
        """
        results = []
        want_tennis    = sport in ("both", "tennis")
        want_pingpong  = sport in ("both", "tabletennis")

        # ── Tennis via The Odds API ──────────────────────────────────────────
        # OTTIMIZZAZIONE QUOTA: prima di chiamare /scores/ per torneo, controlliamo
        # se abbiamo davvero segnali tennis pendenti. Senza questo controllo, ogni
        # esecuzione (ogni 30-60 min) chiamava /scores/ per OGNI torneo ATP/WTA
        # attivo (anche 8-10 in contemporanea in periodo di tour), bruciando fino
        # a 480 richieste/giorno per controllare risultati di tornei dove magari
        # non avevamo nemmeno un segnale aperto.
        pending_tournaments = set()
        if self.db and want_tennis:
            try:
                for sig in self.db.get_signals_for_auto_result():
                    if sig.get("sport") == "tennis" and sig.get("tournament"):
                        pending_tournaments.add(sig["tournament"].strip().lower())
            except Exception as e:
                logger.warning(f"Scores tennis: errore lettura segnali pendenti: {e}")

        if want_tennis and ODDS_KEY and not pending_tournaments:
            logger.info("Scores tennis: nessun segnale tennis pendente — salto il controllo (risparmio quota)")
            want_tennis = False

        if want_tennis and ODDS_KEY:
            async with aiohttp.ClientSession() as session:
                # Non usiamo la cache in-memory per i scores: potrebbe essere vuota
                # se il bot è appena ripartito. Forziamo un refetch diretto.
                saved_cache = self._odds_tennis_keys
                self._odds_tennis_keys = None
                all_tennis_keys = await self._get_active_tennis_sport_keys(session)
                if not all_tennis_keys:
                    self._odds_tennis_keys = saved_cache
                    logger.warning("Scores tennis: nessun torneo attivo trovato")

                # Filtra: controlla solo i tornei per cui abbiamo segnali pendenti,
                # invece di TUTTI i tornei attivi (risparmio quota drastico).
                # Normalizziamo (no spazi/underscore/prefisso "tennis_") perché il
                # titolo segnale ("WTA Washington Open") e la sport key
                # ("tennis_wta_washington_open") hanno formati diversi.
                def _norm(s: str) -> str:
                    return s.lower().replace("tennis_", "").replace("_", "").replace(" ", "")
                pending_norm = {_norm(t) for t in pending_tournaments}
                tennis_keys = [
                    k for k in all_tennis_keys
                    if any(pn in _norm(k) or _norm(k) in pn for pn in pending_norm)
                ]
                if all_tennis_keys and not tennis_keys:
                    logger.info(
                        f"Scores tennis: nessun torneo attivo corrisponde ai segnali pendenti "
                        f"({pending_tournaments}) tra quelli disponibili {all_tennis_keys}"
                    )
                for sport_key in tennis_keys:
                    url = (
                        f"{ODDS_BASE}/sports/{sport_key}/scores/"
                        f"?apiKey={ODDS_KEY}&daysFrom=1&dateFormat=iso"
                    )
                    try:
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=15)
                        ) as resp:
                            logger.info(f"Scores tennis ({sport_key}): HTTP {resp.status}")
                            if resp.status == 200:
                                events = await resp.json()
                                completed_count = sum(1 for e in events if e.get("completed"))
                                logger.info(f"Scores tennis ({sport_key}): {len(events)} eventi, {completed_count} completati")
                                for ev in events:
                                    if not ev.get("completed"):
                                        continue
                                    scores_list = ev.get("scores") or []
                                    if len(scores_list) < 2:
                                        continue
                                    home = ev.get("home_team", "").strip()
                                    away = ev.get("away_team", "").strip()
                                    score_map = {s["name"].strip(): int(s["score"]) for s in scores_list}
                                    score_map_lower = {k.lower(): v for k, v in score_map.items()}
                                    h_score = score_map.get(home) if home in score_map else score_map_lower.get(home.lower())
                                    a_score = score_map.get(away) if away in score_map else score_map_lower.get(away.lower())
                                    if h_score is not None and a_score is not None:
                                        results.append({
                                            "home":   home,
                                            "away":   away,
                                            "winner": home if h_score > a_score else away,
                                            "sport":  "tennis",
                                        })
                                    else:
                                        logger.debug(f"Scores tennis: nomi non in score_map {home} vs {away}. Keys: {list(score_map.keys())[:4]}")
                            else:
                                txt = await resp.text()
                                logger.warning(f"Scores tennis ({sport_key}) status {resp.status}: {txt[:200]}")
                    except Exception as e:
                        logger.warning(f"Scores tennis errore ({sport_key}): {e}")

        # ── Ping Pong via OddsPapi (/v4/fixtures?statusId=2 + /v4/settlements) ──
        # NOTA: l'endpoint /v4/results NON esiste su OddsPapi (mai esistito nella
        # loro API pubblica — da qui il 404 di prima). Il modo corretto per sapere
        # chi ha vinto una fixture è:
        #   1) /v4/fixtures?statusId=2 → elenco fixture concluse (statusId:
        #      0=da iniziare, 1=live, 2=finita, 3=annullata)
        #   2) /v4/settlements?fixtureId=... → per ciascuna fixture conclusa
        #      restituisce WIN/LOSE per ogni outcome del mercato "101"
        #      (Match Winner): outcome 101 = participant1, 102 = participant2.
        # Ogni fixture finita controllata costa 1 richiesta aggiuntiva di quota.
        #
        # OTTIMIZZAZIONE QUOTA (come per il tennis sopra): prima controlliamo se
        # abbiamo davvero segnali ping pong pendenti. Senza questo filtro, ogni
        # esecuzione (2 volte al giorno) controllava fino a 15 fixture concluse
        # a prescindere da quanti segnali fossero effettivamente aperti — cioè
        # fino a 1 (fixtures) + 15 (settlements) = 16 richieste per controllo,
        # 32/giorno, ~1000/mese: molto oltre le 250 richieste/mese disponibili,
        # ed è la causa più probabile per cui la quota si esaurisce sempre a
        # metà mese. Ora controlliamo solo le fixture che corrispondono a un
        # giocatore con un segnale ping pong pendente.
        pending_pp_players = set()
        if self.db and want_pingpong:
            try:
                for sig in self.db.get_signals_for_auto_result():
                    if sig.get("sport") == "tabletennis":
                        pending_pp_players.add(sig.get("player1", "").strip().lower())
                        pending_pp_players.add(sig.get("player2", "").strip().lower())
                pending_pp_players.discard("")
            except Exception as e:
                logger.warning(f"Scores ping pong: errore lettura segnali pendenti: {e}")

        if want_pingpong and ODDSPAPI_KEY and not pending_pp_players:
            logger.info("Scores ping pong: nessun segnale ping pong pendente — salto il controllo (risparmio quota)")
            want_pingpong = False

        if want_pingpong and ODDSPAPI_KEY:
            try:
                async with aiohttp.ClientSession() as session:
                    sport_id = await self._get_tt_sport_id(session)
                    if sport_id:
                        from_utc = (_now_it() - timedelta(hours=36)).astimezone(ZoneInfo("UTC")) \
                            .strftime("%Y-%m-%dT%H:%M:%SZ")
                        to_utc = _now_it().astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
                        await self._throttle_oddspapi()
                        async with session.get(
                            f"{ODDSPAPI_BASE}/fixtures",
                            params={
                                "apiKey":   ODDSPAPI_KEY,
                                "sportId":  sport_id,
                                "from":     from_utc,
                                "to":       to_utc,
                                "statusId": 2,  # solo fixture concluse
                            },
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            logger.info(f"OddsPapi fixtures concluse ping pong: HTTP {resp.status}")
                            if resp.status == 200:
                                fixtures = await resp.json()
                                if isinstance(fixtures, dict):
                                    fixtures = fixtures.get("data") or fixtures.get("fixtures") or []
                                logger.info(f"OddsPapi: {len(fixtures)} fixture concluse trovate")

                                # Tiene solo le fixture i cui giocatori hanno un
                                # segnale pendente (risparmio quota — vedi sopra),
                                # con un tetto di sicurezza extra a 6 richieste
                                # /settlements anche nel caso limite.
                                def _matches_pending(fix: dict) -> bool:
                                    h = (fix.get("participant1Name") or "").strip().lower()
                                    a = (fix.get("participant2Name") or "").strip().lower()
                                    return h in pending_pp_players or a in pending_pp_players
                                fixtures = [f for f in fixtures if _matches_pending(f)][:6]
                                logger.info(f"OddsPapi: {len(fixtures)} fixture da verificare (match con segnali pendenti)")

                                for fix in fixtures:
                                    home = fix.get("participant1Name", "")
                                    away = fix.get("participant2Name", "")
                                    fid  = fix.get("fixtureId", "")
                                    if not home or not away or not fid:
                                        continue
                                    try:
                                        await self._throttle_oddspapi()
                                        async with session.get(
                                            f"{ODDSPAPI_BASE}/settlements",
                                            params={"apiKey": ODDSPAPI_KEY, "fixtureId": fid},
                                            timeout=aiohttp.ClientTimeout(total=10),
                                        ) as sresp:
                                            if sresp.status != 200:
                                                logger.debug(f"OddsPapi settlements status {sresp.status} per {fid}")
                                                continue
                                            sdata = await sresp.json()
                                            market   = (sdata.get("markets") or {}).get("101", {})
                                            outcomes = market.get("outcomes", {})
                                            r1 = outcomes.get("101", {}).get("players", {}).get("0", {}).get("result")
                                            r2 = outcomes.get("102", {}).get("players", {}).get("0", {}).get("result")
                                            if r1 == "WIN":
                                                winner = home
                                            elif r2 == "WIN":
                                                winner = away
                                            else:
                                                continue  # esito non chiaro (push/annullato/mercato assente)
                                            results.append({
                                                "home":   home,
                                                "away":   away,
                                                "winner": winner,
                                                "sport":  "tabletennis",
                                            })
                                    except Exception as e:
                                        logger.debug(f"OddsPapi settlements errore fixture {fid}: {e}")
                            else:
                                txt = await resp.text()
                                logger.warning(f"OddsPapi fixtures concluse status {resp.status}: {txt[:200]}")
            except Exception as e:
                logger.warning(f"OddsPapi scores errore: {e}")

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
