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
import time
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

def pingpong_scan_hours(interval_h: int) -> list:
    """Ore (Europe/Rome) in cui gira lo scan ping pong. Con intervallo 12h
    (2 scan/giorno) NON si usa 07+19: lo scan delle 19:00 copriva la notte, quando
    si dorme. Si usano invece 07:00 e 14:00, così le partite scelte cadono
    tra le 08:15 e le 22:00. Altri intervalli: 07..22 ogni N ore come prima."""
    if interval_h == 12:
        return [7, 14]
    if interval_h <= 0:
        interval_h = 24
    hours, h = [], 7
    while h <= 22:
        hours.append(h)
        h += interval_h
    return hours


def _pingpong_window_end(now: datetime, min_start: datetime, interval_h: int) -> datetime:
    """Fine della finestra di scelta: il prossimo scan utile (almeno 30 min dopo
    min_start), altrimenti le 22:00 — mai oltre, così non si pescano partite notturne."""
    day_end = now.replace(hour=22, minute=0, second=0, microsecond=0)
    for h in pingpong_scan_hours(interval_h):
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t > now and t >= min_start + timedelta(minutes=30):
            return min(t, day_end)
    return day_end


def _pingpong_fixtures_per_scan(pp_interval_h: int) -> int:
    """Quante fixture controllare per scan ping pong, in base a quanti scan
    girano al giorno (stessa formula "scans_day" usata nel picker di bot.py:
    15 // interval + 1, finestra attiva 07-22 inclusiva).

    FIX: prima era un 3 fisso qualunque fosse l'intervallo. Con 1 scan/giorno
    (default 24h) va benissimo (3 fixture/scan ≈ 124 richieste/mese). Ma se
    l'utente passa a 2 scan/giorno (12h, per dividere la copertura mattina/
    sera) 3 fixture/scan porta a 248 richieste/mese — quota OddsPapi (250)
    praticamente esaurita, senza margine per il fallback tennis che la
    condivide. Con 2 scan/giorno scendiamo a 2 fixture/scan (186 req/mese,
    margine sano) — e nel complesso si controllano comunque più partite al
    giorno (4) rispetto all'unico scan da 3."""
    scans_day = 15 // max(pp_interval_h, 1) + 1
    if scans_day <= 1:
        return 3
    if scans_day == 2:
        return 2
    return 1  # 3+ scan/giorno: tetto più stretto per restare in quota


_coverage_sample_logged = 0


def _provider_entries(f: dict) -> dict:
    """Appiattisce f["externalProviders"] (dict, oppure lista di dict) in
    {nome_provider_minuscolo: valore}, tenendo solo i valori non vuoti."""
    prov = f.get("externalProviders")
    flat = {}
    def add(k, v):
        if v not in (None, "", 0, False, [], {}):
            flat[str(k).lower()] = v
    if isinstance(prov, dict):
        for k, v in prov.items():
            add(k, v)
    elif isinstance(prov, list):
        for item in prov:
            if isinstance(item, dict):
                for k, v in item.items():
                    add(k, v)
            elif item:
                add(item, True)
    return flat


def _fixture_coverage_score(f: dict) -> int:
    """Stima GRATIS (dai soli dati di /fixtures, nessuna richiesta extra) di
    quanti bookmaker prezzeranno la partita. Le partite ping pong con molti
    book (23-75 nei log) hanno vincente + over/under valutabili, quelle con 0-10
    no, e ogni /odds consuma quota. Segnali di buona copertura: un provider
    Pinnacle (book sharp: edge vero invece di consenso) e/o almeno 3 provider
    esterni mappati. Ritorna -1 se hasOdds è esplicitamente False, altrimenti
    un punteggio >= 0 (>= 3 = probabilmente ben coperta)."""
    global _coverage_sample_logged
    if _coverage_sample_logged < 3:
        _coverage_sample_logged += 1
        logger.info(
            f"OddsPapi: diagnostica copertura fixture {f.get('fixtureId')}: "
            f"hasOdds={f.get('hasOdds')} externalProviders={str(f.get('externalProviders'))[:300]}"
        )
    if f.get("hasOdds") is False:
        return -1
    flat = _provider_entries(f)
    pinnacle = any("pinnacle" in k for k in flat)
    return len(flat) + (3 if pinnacle else 0)


def _prefer_covered(fixtures: list, n: int) -> list:
    """Scarta le fixture senza quote e, se ce ne sono abbastanza, tiene solo
    quelle probabilmente ben coperte (Pinnacle o >=3 provider). Se non bastano
    per riempire gli n slot, usa tutte quelle con quote (nessuna perdita)."""
    with_odds = [f for f in fixtures if _fixture_coverage_score(f) >= 0]
    good = [f for f in with_odds if _fixture_coverage_score(f) >= 3]
    pool = good if len(good) >= n else with_odds
    logger.info(
        f"OddsPapi: fixture {len(fixtures)} → con quote {len(with_odds)} → "
        f"ben coperte {len(good)} (uso {'solo le coperte' if pool is good else 'tutte con quote'})"
    )
    return pool


def _spread_pick_fixtures(fixtures: list, sort_key, n: int, window_start: datetime, window_end: datetime) -> list:
    """Sceglie fino a n fixture DISTRIBUITE nel tempo tra window_start e
    window_end, invece delle n più vicine nel tempo.

    FIX: prima si prendevano semplicemente le prime N fixture per orario —
    con un solo scan al giorno (ping pong 24/7) questo significava vedere
    solo una fetta di ~30 minuti della giornata (es. le partite delle 08:15
    quando lo scan gira alle 07:00) e ignorare completamente pomeriggio,
    sera e notte, anche se ci sono centinaia di altre partite disponibili.
    Dividendo la finestra in n fette uguali e prendendo una fixture per
    fetta, lo stesso numero di chiamate /odds copre l'intero arco di tempo
    fino al prossimo scan, invece che un unico istante.
    """
    in_window = sorted(
        [f for f in fixtures if window_start <= sort_key(f) <= window_end], key=sort_key
    )
    if not in_window:
        # Nessuna fixture nella finestra target (raro): ripiega sulle più
        # vicine future, meglio di niente.
        return sorted([f for f in fixtures if sort_key(f) >= window_start], key=sort_key)[:n]
    if len(in_window) <= n:
        return in_window

    span   = (window_end - window_start) / n
    picked = []
    used   = set()
    for i in range(n):
        slice_start = window_start + span * i
        slice_end   = window_start + span * (i + 1)
        candidate = next(
            (f for f in in_window if slice_start <= sort_key(f) < slice_end and id(f) not in used),
            None,
        )
        if candidate is None:
            # Fetta vuota (nessuna partita in quell'intervallo): prende la
            # prima fixture disponibile non ancora scelta, per non sprecare lo slot.
            candidate = next((f for f in in_window if id(f) not in used), None)
        if candidate is not None:
            picked.append(candidate)
            used.add(id(candidate))
    return picked


# ══════════════════════════════════════════════════════════════════════════════
# Ogni quanto rileggere i tornei tennis attivi da The Odds API /sports/ (gratis)
TENNIS_KEYS_TTL_S = 3 * 3600
# Fallback tennis su OddsPapi (250 richieste/mese, condivise col ping pong): ogni
# uso costa ~4 richieste, quindi al massimo N volte al giorno.
TENNIS_FALLBACK_MAX_PER_DAY = 1

# Risultati: non ha senso pagare /scores prima che la partita possa essere finita,
# né dopo che l'API non copre più quel giorno (daysFrom=1 → ~24h; ping pong 36h).
RESULT_GRACE_MIN  = {"tennis": 90, "tabletennis": 30}
RESULT_MAX_AGE_H  = {"tennis": 30, "tabletennis": 40}

def result_due(sig: dict) -> bool:
    """True se vale la pena cercare il risultato di questo segnale adesso."""
    sport  = sig.get("sport", "tennis")
    ko_str = (sig.get("kickoff") or "").strip()
    if not ko_str:
        return True
    now = _now_it()
    try:
        try:
            ko = datetime.fromisoformat(ko_str)
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=IT_TZ)
        except ValueError:
            ko = datetime.strptime(f"{now.year}/{ko_str}", "%Y/%d/%m %H:%M").replace(tzinfo=IT_TZ)
            if ko - now > timedelta(days=180):    # es. 31/12 letto a gennaio
                ko = ko.replace(year=ko.year - 1)
    except Exception:
        return True
    age = now - ko
    return (
        timedelta(minutes=RESULT_GRACE_MIN.get(sport, 90))
        <= age
        <= timedelta(hours=RESULT_MAX_AGE_H.get(sport, 30))
    )

class SignalScraper:

    def __init__(self, db=None):
        self._tt_sport_id: int | None = None   # cache ID OddsPapi per ping pong
        self._tennis_oddspapi_id: int | None = None  # cache ID OddsPapi per tennis (fallback)
        self._odds_tennis_keys: list[str] | None = None  # cache sport_key torneo tennis attivi (The Odds API)
        self._odds_tennis_keys_ts: float = 0.0           # quando è stata riempita (time.monotonic)
        self._fb_date: str = ""                          # fallback OddsPapi tennis: giorno e contatore
        self._fb_count: int = 0
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
        self._all_markets_cache     = None  # lista completa /markets, scaricata una volta

    def _save_quota_snapshot(self, key_prefix: str, **fields):
        """Salva su DB l'ultima lettura nota della quota di un'API esterna
        (header di rate-limit, se presente), così il pannello admin può
        mostrarla senza dover fare una chiamata live apposta (che
        consumerebbe quota a sua volta). Ignora i valori mancanti ("?"/None)."""
        if self.db is None:
            return
        saved_any = False
        for name, val in fields.items():
            if val is None or val == "?":
                continue
            try:
                self.db.set_setting(f"{key_prefix}_{name}", str(val))
                saved_any = True
            except Exception as e:
                logger.debug(f"Impossibile salvare quota {key_prefix}_{name}: {e}")
        if saved_any:
            try:
                self.db.set_setting(f"{key_prefix}_updated_at", _now_it().strftime("%Y-%m-%d %H:%M"))
            except Exception:
                pass

    async def _throttle_oddspapi(self):
        """Aspetta il tempo minimo dall'ultima chiamata OddsPapi prima di procedere."""
        async with self._oddspapi_lock:
            now  = asyncio.get_event_loop().time()
            wait = self._oddspapi_min_interval - (now - self._last_oddspapi_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_oddspapi_call = asyncio.get_event_loop().time()

    async def _get_all_markets(self, session: aiohttp.ClientSession) -> list | None:
        """Scarica l'intera lista mercati di OddsPapi (~579 in tutto) UNA volta
        sola e la cache in memoria per il resto della vita del processo.

        FIX: /v4/markets NON accetta un filtro sportId (a differenza di v5,
        di cui avevo letto la documentazione per errore) — restituisce sempre
        la lista globale, ignorando qualsiasi parametro sportId passato.
        Prima filtravamo lato server (credendolo filtrato) e prendevamo il
        primo mercato "moneyline 2-way" della lista intera: per questo tennis
        e ping pong risultavano avere lo STESSO marketId, appartenente in
        realtà a un altro sport. Ora scarichiamo tutto una sola volta e
        filtriamo noi per sportId in _get_winner_market."""
        if self._all_markets_cache is not None:
            return self._all_markets_cache
        await self._throttle_oddspapi()
        try:
            async with session.get(
                f"{ODDSPAPI_BASE}/markets",
                params={"apiKey": ODDSPAPI_KEY, "language": "en"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    logger.warning(f"OddsPapi /markets status {r.status}")
                    if r.status == 429:
                        self.pingpong_quota_ok = False
                    return None
                markets = await r.json()
        except Exception as e:
            logger.error(f"OddsPapi /markets errore: {e}")
            return None
        logger.info(f"OddsPapi: /markets scaricati e cachati — {len(markets)} mercati totali")
        self._all_markets_cache = markets
        return markets

    async def _get_winner_market(self, session: aiohttp.ClientSession, sport_id: int) -> dict | None:
        """Scopre il mercato "vincente 2-way" (moneyline) REALE per questo sport,
        filtrando client-side la lista completa di /markets (vedi _get_all_markets)."""
        cache_attr = f"_winner_market_{sport_id}"
        cached = getattr(self, cache_attr, None)
        if cached:
            return cached

        # FIX: chiave versionata "_v2" — la vecchia chiave senza suffisso può
        # contenere valori sbagliati salvati dalle esecuzioni precedenti al fix
        # del filtro sportId (es. marketId=10728 riusato per qualunque sport).
        # Con un nome nuovo la cache riparte pulita invece di fidarsi di dati
        # scritti da codice difettoso.
        setting_key = f"oddspapi_winner_market_v2_{sport_id}"
        if self.db is not None:
            raw = self.db.get_setting(setting_key)
            if raw:
                try:
                    parsed = json.loads(raw)
                    setattr(self, cache_attr, parsed)
                    logger.info(
                        f"OddsPapi: mercato vincente sportId={sport_id} da cache DB → "
                        f"marketId={parsed.get('market_id')}"
                    )
                    return parsed
                except Exception:
                    pass

        markets = await self._get_all_markets(session)
        if not markets:
            return None

        candidates = [
            m for m in markets
            if m.get("sportId") == sport_id
            and m.get("marketType") == "moneyline"
            and m.get("marketLength") == 2
            and not m.get("playerProp")
            and (m.get("handicap") or 0) == 0
        ]
        if not candidates:
            sample = [
                (m.get("marketId"), m.get("marketType"), m.get("period"))
                for m in markets if m.get("sportId") == sport_id
            ][:15]
            logger.warning(
                f"OddsPapi: nessun mercato moneyline 2-way trovato per sportId={sport_id}. "
                f"Mercati di questo sport (campione): {sample}"
            )
            return None

        # Se più candidati (periodi diversi), preferisci la partita intera
        preferred_periods = ("match", "fulltime", "result", "game", "regulation")
        chosen = next(
            (c for p in preferred_periods for c in candidates if c.get("period") == p),
            candidates[0],
        )
        outcomes = chosen.get("outcomes") or []
        if len(outcomes) != 2:
            logger.warning(
                f"OddsPapi: mercato moneyline sportId={sport_id} ha {len(outcomes)} "
                f"outcome (attesi 2): {chosen}"
            )
            return None

        result = {
            "market_id":  chosen["marketId"],
            "outcome_p1": outcomes[0]["outcomeId"],
            "outcome_p2": outcomes[1]["outcomeId"],
        }
        logger.info(
            f"OddsPapi: mercato vincente sportId={sport_id} → "
            f"{chosen.get('marketName')} (marketId={result['market_id']}, "
            f"outcomes={result['outcome_p1']}/{result['outcome_p2']})"
        )
        setattr(self, cache_attr, result)
        if self.db is not None:
            try:
                self.db.set_setting(setting_key, json.dumps(result))
            except Exception as e:
                logger.debug(f"OddsPapi: impossibile cachare winner market su DB: {e}")
        return result

    async def _get_totals_market(self, session: aiohttp.ClientSession, sport_id: int) -> dict | None:
        """Scopre il mercato Over/Under (totals) per questo sport — stessa logica
        di _get_winner_market, sulla stessa lista /markets già scaricata e cachata.
        Tra le varie linee (handicap) disponibili prende quella "di mezzo" come
        linea principale — le altre sono linee alternative, non ci servono."""
        cache_attr = f"_totals_market_{sport_id}"
        cached = getattr(self, cache_attr, None)
        if cached:
            return cached

        setting_key = f"oddspapi_totals_market_v2_{sport_id}"
        if self.db is not None:
            raw = self.db.get_setting(setting_key)
            if raw:
                try:
                    parsed = json.loads(raw)
                    setattr(self, cache_attr, parsed)
                    logger.info(
                        f"OddsPapi: mercato totals sportId={sport_id} da cache DB → "
                        f"marketId={parsed.get('market_id')}, linea={parsed.get('line')}"
                    )
                    return parsed
                except Exception:
                    pass

        markets = await self._get_all_markets(session)
        if not markets:
            return None

        candidates = [
            m for m in markets
            if m.get("sportId") == sport_id
            and str(m.get("marketType") or "").startswith("totals")  # "totals", "totals-points", "totals-sets", ecc.
            and m.get("marketLength") == 2
            and not m.get("playerProp")
            and (m.get("handicap") or 0) > 0   # >0: è una vera linea over/under
        ]
        if not candidates:
            # Diagnostica: mostra TUTTI i marketType/period presenti per questo
            # sport, per capire se "totals" ha un nome diverso qui (es. "total",
            # "over_under") o se il filtro handicap/marketLength è troppo stretto.
            sport_markets = [m for m in markets if m.get("sportId") == sport_id]
            sample = [
                (m.get("marketId"), m.get("marketType"), m.get("marketLength"),
                 m.get("handicap"), m.get("period"), m.get("marketName"))
                for m in sport_markets
            ][:20]
            logger.warning(
                f"OddsPapi: nessun mercato totals (over/under) trovato per "
                f"sportId={sport_id} tra {len(sport_markets)} mercati di questo "
                f"sport. Campione (marketId, type, length, handicap, period, name): {sample}"
            )
            return None

        candidates.sort(key=lambda m: m.get("handicap") or 0)
        chosen = candidates[len(candidates) // 2]  # linea "di mezzo" come principale

        outcomes = chosen.get("outcomes") or []
        over_id = under_id = None
        for o in outcomes:
            name = (o.get("outcomeName") or "").strip().lower()
            if name == "over":
                over_id = o.get("outcomeId")
            elif name == "under":
                under_id = o.get("outcomeId")
        if over_id is None or under_id is None:
            logger.warning(
                f"OddsPapi: mercato totals sportId={sport_id} senza outcome "
                f"Over/Under riconoscibili: {chosen}"
            )
            return None

        result = {
            "market_id":   chosen["marketId"],
            "outcome_over": over_id,
            "outcome_under": under_id,
            "line":        chosen.get("handicap"),
        }
        logger.info(
            f"OddsPapi: mercato totals sportId={sport_id} → "
            f"{chosen.get('marketName')} linea {result['line']} "
            f"(marketId={result['market_id']}, outcomes={over_id}/{under_id})"
        )
        setattr(self, cache_attr, result)
        if self.db is not None:
            try:
                self.db.set_setting(setting_key, json.dumps(result))
            except Exception as e:
                logger.debug(f"OddsPapi: impossibile cachare totals market su DB: {e}")
        return result

    async def _get_totals_markets_all(self, session: aiohttp.ClientSession, sport_id: int) -> list:
        """Come _get_totals_market, ma ritorna TUTTE le linee totals candidate
        per questo sport invece di sceglierne una sola ("di mezzo") in anticipo.

        FIX: _get_totals_market sceglieva UNA linea fissa (es. 65.0 punti per
        il ping pong) e la cercava identica su ogni fixture — ma linee diverse
        (formato/durata diversi) sono quotate da bookmaker diversi a seconda
        della partita, quindi quella fissa non combaciava quasi mai (confermato
        nei log: "0 linee totals disponibili" su ogni fixture ping pong,
        nonostante 19-72 book sul mercato vincente della stessa fixture). Qui
        invece si prova OGNI linea candidata per fixture — _extract_oddspapi_totals
        già supporta più linee contemporaneamente (dict {linea: {book: ...}}),
        e _totals_reference in ai_analyzer.py sceglie da sola quella con più
        copertura book per quella specifica partita."""
        cache_attr = f"_totals_markets_all_{sport_id}"
        cached = getattr(self, cache_attr, None)
        if cached is not None:
            return cached

        setting_key = f"oddspapi_totals_markets_all_v1_{sport_id}"
        if self.db is not None:
            raw = self.db.get_setting(setting_key)
            if raw:
                try:
                    parsed = json.loads(raw)
                    setattr(self, cache_attr, parsed)
                    logger.info(
                        f"OddsPapi: {len(parsed)} linee totals candidate sportId={sport_id} "
                        f"da cache DB → linee {[m.get('line') for m in parsed]}"
                    )
                    return parsed
                except Exception:
                    pass

        markets = await self._get_all_markets(session)
        if not markets:
            return []

        candidates = [
            m for m in markets
            if m.get("sportId") == sport_id
            and str(m.get("marketType") or "").startswith("totals")
            and m.get("marketLength") == 2
            and not m.get("playerProp")
            and (m.get("handicap") or 0) > 0
        ]
        result = []
        for c in candidates:
            outcomes = c.get("outcomes") or []
            over_id = under_id = None
            for o in outcomes:
                name = (o.get("outcomeName") or "").strip().lower()
                if name == "over":
                    over_id = o.get("outcomeId")
                elif name == "under":
                    under_id = o.get("outcomeId")
            if over_id is None or under_id is None:
                continue
            result.append({
                "market_id":    c["marketId"],
                "outcome_over": over_id,
                "outcome_under": under_id,
                "line":         c.get("handicap"),
            })

        setattr(self, cache_attr, result)
        logger.info(
            f"OddsPapi: {len(result)} linee totals candidate trovate per "
            f"sportId={sport_id} → linee {[m['line'] for m in result]}"
        )
        if self.db is not None:
            try:
                self.db.set_setting(setting_key, json.dumps(result))
            except Exception as e:
                logger.debug(f"OddsPapi: impossibile cachare totals markets (tutte) su DB: {e}")
        return result

    # ── Entry point principale ─────────────────────────────────────────────────
    def _tennis_fallback_allowed(self) -> bool:
        today = _now_it().strftime("%Y-%m-%d")
        date, count = self._fb_date, self._fb_count
        if self.db is not None:
            try:
                date  = self.db.get_setting("oddspapi_tennis_fb_date", "") or ""
                count = int(self.db.get_setting("oddspapi_tennis_fb_count", "0") or 0)
            except Exception:
                pass
        if date != today:
            count = 0
        return count < TENNIS_FALLBACK_MAX_PER_DAY

    def _tennis_fallback_register(self):
        today = _now_it().strftime("%Y-%m-%d")
        if self._fb_date != today:
            self._fb_date, self._fb_count = today, 0
        self._fb_count += 1
        if self.db is not None:
            try:
                prev = int(self.db.get_setting("oddspapi_tennis_fb_count", "0") or 0) \
                    if self.db.get_setting("oddspapi_tennis_fb_date", "") == today else 0
                self.db.set_setting("oddspapi_tennis_fb_date", today)
                self.db.set_setting("oddspapi_tennis_fb_count", prev + 1)
            except Exception:
                pass

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
        if want_tennis and not tennis_matches and ODDSPAPI_KEY and not self._tennis_fallback_allowed():
            logger.info(
                f"Tennis: nessuna partita da The Odds API; fallback OddsPapi già usato oggi "
                f"(tetto {TENNIS_FALLBACK_MAX_PER_DAY}/giorno, quota condivisa col ping pong) — salto"
            )
        elif want_tennis and not tennis_matches and ODDSPAPI_KEY:
            self._tennis_fallback_register()
            if not ODDS_KEY:
                logger.info("ODDS_API_KEY non impostata — uso OddsPapi come fonte tennis primaria")
            else:
                logger.warning("The Odds API tennis: nessuna partita (quota esaurita, nessun torneo attivo o lista tornei vecchia) — provo fallback OddsPapi")
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

            winner_market = await self._get_winner_market(session, sport_id)
            if winner_market is None:
                logger.warning("OddsPapi: mercato vincente ping pong non trovato — ping pong non disponibile")
                return []
            totals_market = await self._get_totals_market(session, sport_id)
            totals_markets_all = await self._get_totals_markets_all(session, sport_id)

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
                    self._save_quota_snapshot("quota_oddspapi", remaining=rem)
                    if self.db is not None:
                        self.db.increment_api_calls("oddspapi")
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
                    #
                    # FIX 2: "future" da solo non basta — il ping pong gioca una
                    # partita ogni 5 minuti H24, quindi le fixture "più vicine nel
                    # tempo" hanno quasi sempre il kickoff tra pochi minuti.
                    # L'analyzer scarta comunque tutto sotto min_hours_before (1h di
                    # default), quindi prima si sprecava la chiamata /odds (quota!)
                    # su partite che sarebbero state buttate via subito dopo. Ora
                    # chiediamo fixture che partono tra almeno 75 minuti (1h + un
                    # margine di sicurezza sul tempo che lo scan impiega) — così le
                    # quote recuperate hanno davvero una chance di diventare un segnale.
                    #
                    # FIX 3: invece delle 3 più vicine (tutte ammassate in mezz'ora),
                    # le distribuiamo lungo tutta la finestra fino al prossimo scan
                    # (pingpong_scan_interval ore) — copertura sull'intera giornata
                    # invece che su un'unica fetta oraria, a parità di richieste.
                    now = _now_it()
                    min_start = now + timedelta(minutes=75)
                    pp_interval_h = 24
                    if self.db is not None:
                        try:
                            pp_interval_h = self.db.get_settings().get("pingpong_scan_interval", 24)
                        except Exception:
                            pass
                    window_end = _pingpong_window_end(now, min_start, pp_interval_h)

                    # Metodo principale: UNA richiesta con le quote di tutte le partite dei
                    # tornei migliori (vedi _fetch_pp_by_tournaments). Se non va, si ripiega
                    # sul vecchio metodo per-partita qui sotto.
                    if os.environ.get("PP_USE_TOURNAMENT_ODDS", "1") == "1":
                        try:
                            tm = await self._fetch_pp_by_tournaments(
                                session, fixtures, winner_market, totals_market,
                                totals_markets_all, min_start, window_end,
                            )
                        except Exception as e:
                            logger.warning(f"OddsPapi by-tournaments errore: {e}")
                            tm = None
                        if tm:
                            return tm
                        logger.info("OddsPapi by-tournaments: nessun risultato utile — ripiego sul metodo per-partita")

                    n_fixtures = _pingpong_fixtures_per_scan(pp_interval_h)
                    fixtures = _spread_pick_fixtures(_prefer_covered(fixtures, n_fixtures), _sort_key, n_fixtures, min_start, window_end)
                    logger.info(
                        f"OddsPapi: {len(fixtures)} fixture distribuite tra "
                        f"{min_start.strftime('%H:%M')} e {window_end.strftime('%H:%M')} "
                        f"({n_fixtures}/scan in base a pingpong_scan_interval={pp_interval_h}h, evita rate limit)"
                    )
            except Exception as e:
                logger.error(f"OddsPapi fixtures errore: {e}")
                return []

            # Per ogni fixture recupera le quote
            for fix in fixtures:
                parsed = await self._parse_oddspapi_fixture(
                    session, fix, winner_market, totals_market,
                    totals_markets_all=totals_markets_all,
                )
                if parsed:
                    matches.append(parsed)

        return matches

    async def _fetch_pp_by_tournaments(
        self, session: aiohttp.ClientSession, fixtures: list, winner_market: dict,
        totals_market: dict | None, totals_markets_all: list | None,
        min_start: datetime, window_end: datetime,
    ) -> list[dict] | None:
        """Ping pong: /v4/odds-by-tournaments restituisce le quote di TUTTE le partite
        dei tornei richiesti, ma per UN SOLO bookmaker a chiamata (parametro
        "bookmaker", obbligatorio: con 0 o più di uno risponde 400). Quindi: 1 richiesta
        per bookmaker, e ogni richiesta copre decine di partite invece di una.
        Si usano pochi bookmaker: il primo è lo sharp di riferimento (sbobet), gli altri
        sono i soft su cui cercare valore (bwin). Costo per scan: 1 (elenco partite)
        + 1 per bookmaker (default 2) = 3 richieste, come il vecchio metodo che però
        controllava solo 2 partite.
        Ritorna None se qualcosa non va (il chiamante ripiega sul vecchio metodo).
        Variabili d'ambiente opzionali su Railway:
          PP_USE_TOURNAMENT_ODDS=0        → disattiva (torna al metodo per-partita)
          PP_BOOKMAKERS=sbobet,bwin       → bookmaker da interrogare (il primo = sharp)
          PP_MAX_BOOK_REQUESTS=3          → massimo bookmaker (= richieste) per scan
          PP_TOURNAMENTS_PER_REQUEST=3    → quanti tornei per richiesta
          PP_MAX_FIXTURES=40              → massimo partite analizzate per scan
        """
        if getattr(self, "_pp_tourn_disabled", False):
            return None

        def _start(f):
            s_ = f.get("startDate") or f.get("startTime") or ""
            try:
                dt = datetime.fromisoformat(str(s_).replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=IT_TZ)
            except Exception:
                return None

        by_t: dict = {}
        for f in fixtures:
            tid, st = f.get("tournamentId"), _start(f)
            if tid is None or st is None or _fixture_coverage_score(f) < 3:
                continue
            if min_start <= st <= window_end:
                by_t.setdefault(tid, []).append(f)
        if not by_t:
            logger.info("OddsPapi by-tournaments: nessun torneo con partite ben coperte nella finestra")
            return None

        n_t = max(1, int(os.environ.get("PP_TOURNAMENTS_PER_REQUEST", "3")))
        top = sorted(by_t.items(), key=lambda kv: len(kv[1]), reverse=True)[:n_t]
        tids = [str(k) for k, _ in top]
        n_cand = sum(len(v) for _, v in top)

        books = [b.strip() for b in os.environ.get("PP_BOOKMAKERS", "sbobet,bwin").split(",") if b.strip()]
        books = books[:max(1, int(os.environ.get("PP_MAX_BOOK_REQUESTS", "3")))]

        base = {str(f.get("fixtureId")): f for f in fixtures}
        combined: dict = {}      # fixtureId -> {"fx": campi della fixture, "bm": {slug: payload bookmaker}}
        per_book: list = []
        total_bytes = 0
        t0 = time.time()
        for i, slug in enumerate(books):
            await self._throttle_oddspapi()
            async with session.get(
                f"{ODDSPAPI_BASE}/odds-by-tournaments",
                params={"apiKey": ODDSPAPI_KEY, "tournamentIds": ",".join(tids), "bookmaker": slug},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                self._save_quota_snapshot("quota_oddspapi", remaining=r.headers.get("X-RateLimit-Remaining", "?"))
                if self.db is not None:
                    self.db.increment_api_calls("oddspapi")
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"OddsPapi odds-by-tournaments (bookmaker={slug}) status {r.status}: {body[:200]}")
                    if r.status in (400, 401, 403, 404, 422):
                        # errore di configurazione: inutile ritentare (e sprecare quota) a ogni scan
                        self._pp_tourn_disabled = True
                        logger.warning("OddsPapi by-tournaments disattivato fino al riavvio del bot")
                        return None
                    if i == 0:
                        return None
                    continue
                raw = await r.read()
            total_bytes += len(raw)
            data = json.loads(raw)
            del raw
            if isinstance(data, dict):
                data = data.get("data") or data.get("fixtures") or []
            if not isinstance(data, list):
                logger.warning("OddsPapi odds-by-tournaments: formato risposta inatteso")
                return None
            n_with = 0
            for fx in data:
                if not isinstance(fx, dict) or fx.get("hasOdds") is False:
                    continue
                fid = str(fx.get("fixtureId"))
                entry = combined.setdefault(fid, {"fx": {}, "bm": {}})
                entry["fx"].update({k: v for k, v in fx.items() if k != "bookmakerOdds" and v not in (None, "")})
                for sl, payload in (fx.get("bookmakerOdds") or {}).items():
                    entry["bm"][sl] = payload
                    n_with += 1
            per_book.append(f"{slug}:{n_with}")

        in_window = []
        for fid, entry in combined.items():
            merged = {**base.get(fid, {}), **entry["fx"], "bookmakerOdds": entry["bm"]}
            st = _start(merged)
            if st is not None and min_start <= st <= window_end and len(entry["bm"]) >= 2:
                in_window.append((st, merged))     # servono sharp + almeno un soft
        in_window.sort(key=lambda x: x[0])
        cap = max(1, int(os.environ.get("PP_MAX_FIXTURES", "40")))
        in_window = in_window[:cap]

        matches = []
        for _, merged in in_window:
            m = await self._parse_oddspapi_fixture(
                session, merged, winner_market, totals_market,
                totals_markets_all=totals_markets_all, odds_data=merged, quiet=True,
            )
            if m:
                matches.append(m)

        with_totals = sum(1 for m in matches if m.get("raw_totals"))
        logger.info(
            f"OddsPapi by-tournaments: tornei {tids} ({n_cand} partite candidate), bookmaker {per_book} → "
            f"{len(combined)} fixture ricevute, {len(in_window)} con almeno 2 book in finestra, "
            f"{len(matches)} con quote ({with_totals} con over/under), "
            f"{total_bytes / 1024:.0f} KB in {time.time() - t0:.1f}s"
        )
        return matches or None

    async def _parse_oddspapi_fixture(
        self, session: aiohttp.ClientSession, fix: dict, winner_market: dict,
        totals_market: dict | None = None, default_sport_label: str = "Ping Pong",
        totals_markets_all: list | None = None,
        odds_data: dict | None = None, quiet: bool = False,
    ) -> dict | None:
        """odds_data: payload quote già in mano (es. da /odds-by-tournaments) —
        se presente NON si fa la chiamata /odds per-partita (zero richieste).
        quiet: il log "book coverage" per partita va a DEBUG (scan con tante partite)."""
        try:
            p1 = (fix.get("participant1Name") or fix.get("home") or "").strip()
            p2 = (fix.get("participant2Name") or fix.get("away") or "").strip()
            if not p1 or not p2:
                return None

            fid      = fix.get("fixtureId") or fix.get("id", "")
            start    = fix.get("startDate") or fix.get("startTime") or ""
            kickoff  = _iso_to_it(start) if start else _now_it().strftime("%d/%m %H:%M")
            tourn = fix.get("tournamentName")
            if not tourn and isinstance(fix.get("tournament"), dict):
                # FIX: era scritto come "A or B or C if isinstance(...) else D" — in
                # Python il ternario ha PRECEDENZA PIÙ BASSA di "or", quindi l'intera
                # espressione "A or B or C" veniva valutata solo SE fix["tournament"]
                # era un dict. Ma il campo reale di OddsPapi è "tournamentName" piatto
                # (non annidato in "tournament"), quindi quella condizione era sempre
                # falsa e si finiva dritti nell'else col default "Ping Pong" fisso —
                # anche per le partite di tennis. Ora leggiamo "tournamentName" per
                # primo, sempre, col fallback annidato/label solo come extra sicurezza.
                tourn = fix.get("tournament", {}).get("name")
            if not tourn:
                tourn = fix.get("league") or default_sport_label

            # Recupera quote
            odds_home, odds_away, over_odds, under_odds, totals_line = \
                None, None, None, None, 3.5
            raw_bookmakers = {}
            raw_totals: dict = {}

            data = odds_data
            if data is None and fid:
                try:
                    await self._throttle_oddspapi()
                    async with session.get(
                        f"{ODDSPAPI_BASE}/odds",
                        params={
                            "apiKey":    ODDSPAPI_KEY,
                            "fixtureId": fid,
                            # Nota: /v4/odds non filtra realmente per marketId (come
                            # /v4/markets non filtra per sportId) — la risposta
                            # contiene comunque tutti i mercati del fixture. Lo
                            # passiamo lo stesso per chiarezza/compatibilità futura,
                            # ma l'estrazione qui sotto lavora sul payload completo.
                            "marketId":  str(winner_market["market_id"]),
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        self._save_quota_snapshot(
                            "quota_oddspapi", remaining=r.headers.get("X-RateLimit-Remaining", "?")
                        )
                        if self.db is not None:
                            self.db.increment_api_calls("oddspapi")
                        if r.status == 200:
                            data = await r.json()
                        else:
                            body = await r.text()
                            logger.warning(
                                f"OddsPapi /odds status {r.status} per {p1} vs {p2} "
                                f"(fixtureId={fid}): {body[:200]}"
                            )
                except Exception as e:
                    logger.warning(f"OddsPapi /odds eccezione fixture {fid} ({p1} vs {p2}): {e}")

            if data is not None:
                try:
                    odds_home, odds_away, over_odds, under_odds, totals_line = \
                        self._extract_oddspapi_odds(data, winner_market, totals_market)
                    # Salva quote per bookmaker per de-vig Pinnacle
                    raw_bookmakers = self._extract_raw_bookmakers(data, winner_market)
                    raw_totals = self._extract_oddspapi_totals(
                        data, totals_markets_all or totals_market
                    )
                    # DIAGNOSTICA: ai_analyzer.py serve un book sharp oppure il consenso
                    # di almeno MIN_REF_BOOKS=3 book sulla stessa quota/linea per
                    # generare un segnale (vincente o over/under). Logghiamo quanti book
                    # arrivano davvero per fixture.
                    n_tot_books = max((len(b) for b in raw_totals.values()), default=0)
                    (logger.debug if quiet else logger.info)(
                        f"OddsPapi [{default_sport_label}] book coverage: {p1} vs {p2} — "
                        f"{len(raw_bookmakers)} book su mercato vincente {list(raw_bookmakers.keys())}, "
                        f"{len(raw_totals)} linee totals disponibili, max {n_tot_books} book "
                        f"sulla stessa linea (serve >=3 senza sharp per generare un segnale)"
                    )
                    if odds_home is None:
                        n_bm = len(data.get("bookmakerOdds") or {})
                        (logger.debug if quiet else logger.info)(
                            f"OddsPapi: nessun bookmaker ha quotato il mercato "
                            f"vincente per {p1} vs {p2} (fixtureId={fid}, "
                            f"{n_bm} bookmaker nel payload ma nessuno su "
                            f"marketId={winner_market['market_id']})"
                        )
                except Exception as e:
                    logger.warning(f"OddsPapi estrazione quote fixture {fid} ({p1} vs {p2}): {e}")

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
                "raw_totals":      raw_totals,
                "source":          source,
                "sport":           "tabletennis",
                "sport_label":     "🏓 Ping Pong",
                "raw_bookmakers":  raw_bookmakers,
            }
        except Exception as e:
            logger.debug(f"OddsPapi parse errore: {e}")
            return None

    def _extract_oddspapi_totals(self, data: dict, totals_markets: list | dict | None) -> dict:
        """{linea: {book: {"over": x, "under": y}}} dal payload /odds di OddsPapi.
        Stessa chiamata già pagata per il vincente: nessuna richiesta in più.

        FIX: prima accettava UNA sola linea (dict) — ora accetta anche una
        LISTA di linee candidate (vedi _get_totals_markets_all) e le prova
        tutte sulla fixture corrente, invece di scartare in blocco se la
        fixture non offre esattamente quell'unica linea pre-scelta. Accetta
        ancora il vecchio formato a dict singolo per compatibilità."""
        if not totals_markets:
            return {}
        markets_list = [totals_markets] if isinstance(totals_markets, dict) else totals_markets
        bm_odds = data.get("bookmakerOdds") or {}
        out: dict = {}
        for tm in markets_list:
            tot_id = str(tm["market_id"])
            oc_o, oc_u = str(tm["outcome_over"]), str(tm["outcome_under"])
            line = tm.get("line")
            if not line:
                continue
            for slug, bm_data in bm_odds.items():
                mkt = (bm_data.get("markets") or {}).get(tot_id)
                if not mkt:
                    continue
                try:
                    ov = float(((mkt.get("outcomes") or {}).get(oc_o) or {}).get("players", {}).get("0", {}).get("price"))
                    un = float(((mkt.get("outcomes") or {}).get(oc_u) or {}).get("players", {}).get("0", {}).get("price"))
                except (TypeError, ValueError):
                    continue
                if ov and un:
                    out.setdefault(line, {})[slug.lower()] = {"over": ov, "under": un}
        return out

    def _extract_oddspapi_odds(
        self, data: dict, winner_market: dict, totals_market: dict | None = None
    ) -> tuple:
        """Estrae le migliori quote dal response OddsPapi.

        FIX: la struttura reale (vedi docs.oddspapi.io) NON è la lista piatta
        {"bookmakerOdds": {slug: {"outcomes": [{"name":.., "price":..}]}}} che
        si ipotizzava — è annidata per bookmaker → "markets" (keyed
        dall'ID scoperto in _get_winner_market) → "outcomes" (keyed per
        outcome_p1/outcome_p2) → "players" → "0" → "price". Niente "name" nel
        singolo outcome: l'associazione a p1/p2 è data dall'ID outcome, non dal
        nome del giocatore (che qui è quasi sempre null).
        """
        odds_home = odds_away = over_odds = under_odds = None
        totals_line = (totals_market or {}).get("line") or 3.5

        mkt_id = str(winner_market["market_id"])
        oc_p1  = str(winner_market["outcome_p1"])
        oc_p2  = str(winner_market["outcome_p2"])

        tot_mkt_id  = str(totals_market["market_id"])    if totals_market else None
        oc_over     = str(totals_market["outcome_over"]) if totals_market else None
        oc_under    = str(totals_market["outcome_under"]) if totals_market else None

        bm_odds = data.get("bookmakerOdds") or {}
        for bm_slug, bm_data in bm_odds.items():
            markets = bm_data.get("markets") or {}

            def _price(mkt: dict | None, oc_id):
                if not mkt or not oc_id:
                    return None
                try:
                    return float(
                        (mkt.get("outcomes") or {}).get(oc_id, {}).get("players", {}).get("0", {}).get("price")
                    )
                except (TypeError, ValueError):
                    return None

            winner_mkt = markets.get(mkt_id)
            price1 = _price(winner_mkt, oc_p1)
            price2 = _price(winner_mkt, oc_p2)
            if price1 and (odds_home is None or price1 > odds_home):
                odds_home = price1
            if price2 and (odds_away is None or price2 > odds_away):
                odds_away = price2

            if tot_mkt_id:
                totals_mkt = markets.get(tot_mkt_id)
                p_over  = _price(totals_mkt, oc_over)
                p_under = _price(totals_mkt, oc_under)
                if p_over and (over_odds is None or p_over > over_odds):
                    over_odds = p_over
                if p_under and (under_odds is None or p_under > under_odds):
                    under_odds = p_under

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

            winner_market = await self._get_winner_market(session, sport_id)
            if winner_market is None:
                logger.warning("OddsPapi: mercato vincente tennis non trovato — fallback tennis non disponibile")
                return []
            totals_market = await self._get_totals_market(session, sport_id)
            totals_markets_all = await self._get_totals_markets_all(session, sport_id)

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
                    self._save_quota_snapshot("quota_oddspapi", remaining=rem)
                    if self.db is not None:
                        self.db.increment_api_calls("oddspapi")
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
                    #
                    # FIX 2: stesso margine di sicurezza del ping pong — richiediamo
                    # kickoff tra almeno 75 minuti, non semplicemente "nel futuro",
                    # perché l'analyzer scarta comunque tutto sotto 1h (min_hours_before).
                    #
                    # FIX 3: stessa distribuzione oraria del ping pong, invece delle
                    # 3 più vicine ammassate insieme.
                    now = _now_it()
                    min_start = now + timedelta(minutes=75)
                    tennis_interval_h = 3
                    if self.db is not None:
                        try:
                            tennis_interval_h = self.db.get_settings().get("scan_interval", 3)
                        except Exception:
                            pass
                    window_end = now + timedelta(hours=tennis_interval_h)
                    fixtures = _spread_pick_fixtures(_prefer_covered(fixtures, 3), _sort_key, 3, min_start, window_end)
                    logger.info(
                        f"OddsPapi tennis: {len(fixtures)} fixture distribuite tra "
                        f"{min_start.strftime('%H:%M')} e {window_end.strftime('%H:%M')} (fallback, risparmio quota)"
                    )
            except Exception as e:
                logger.error(f"OddsPapi fixtures tennis errore: {e}")
                return []

            for fix in fixtures:
                parsed = await self._parse_oddspapi_fixture(
                    session, fix, winner_market, totals_market, default_sport_label="Tennis",
                    totals_markets_all=totals_markets_all,
                )
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

    def _extract_raw_bookmakers(self, data: dict, winner_market: dict) -> dict:
        """
        Estrae quote per ogni bookmaker nel formato:
        {"pinnacle": {"home": 1.85, "away": 2.10}, "bet365": {...}, ...}
        Usato dall'analyzer per il de-vig Pinnacle.
        FIX: stessa struttura reale annidata di _extract_oddspapi_odds.
        """
        raw = {}
        mkt_id = str(winner_market["market_id"])
        oc_p1  = str(winner_market["outcome_p1"])
        oc_p2  = str(winner_market["outcome_p2"])

        bm_odds = data.get("bookmakerOdds") or {}
        for slug, bm_data in bm_odds.items():
            market = (bm_data.get("markets") or {}).get(mkt_id)
            if not market:
                continue
            outcomes = market.get("outcomes") or {}
            try:
                price1 = float(outcomes.get(oc_p1, {}).get("players", {}).get("0", {}).get("price"))
                price2 = float(outcomes.get(oc_p2, {}).get("players", {}).get("0", {}).get("price"))
            except (TypeError, ValueError):
                continue
            if price1 and price2:
                raw[slug.lower()] = {"home": price1, "away": price2}
        return raw


    # ══════════════════════════════════════════════════════════════════════════

    async def _tournament_has_upcoming(
        self, session: aiohttp.ClientSession, sport_key: str, min_start: datetime
    ) -> bool | None:
        """Usa /events (GRATIS, non consuma crediti) per sapere se il torneo ha
        almeno una partita che parte dopo min_start. Se no, inutile pagare /odds
        (2 crediti: h2h+totals). None = non so (errore) → si procede come prima."""
        try:
            async with session.get(
                f"{ODDS_BASE}/sports/{sport_key}/events",
                params={"apiKey": ODDS_KEY, "dateFormat": "iso"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return None
                events = await r.json()
        except Exception as e:
            logger.debug(f"/events {sport_key} errore: {e}")
            return None
        for ev in events or []:
            try:
                ct = datetime.fromisoformat(str(ev.get("commence_time", "")).replace("Z", "+00:00"))
            except ValueError:
                return True   # data illeggibile: meglio non perdere partite
            if ct >= min_start:
                return True
        return False

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

            min_hours = 1.0
            if self.db is not None:
                try:
                    min_hours = float(self.db.get_settings().get("min_hours_before", 1.0))
                except Exception:
                    pass
            min_start = _now_it() + timedelta(hours=min_hours)
            n_skipped = 0

            for sport_key in tennis_keys:
                if await self._tournament_has_upcoming(session, sport_key, min_start) is False:
                    n_skipped += 1
                    logger.info(
                        f"The Odds API ({sport_key}): nessuna partita utile (/events gratis) — "
                        f"salto /odds, risparmio 2 crediti"
                    )
                    continue
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
                        self._save_quota_snapshot("quota_oddsapi_tennis", used=used, remaining=rem)
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
            raw_totals: dict = {}   # {linea: {book: {"over": x, "under": y}}}

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
                        per_line: dict = {}
                        for o in market.get("outcomes", []):
                            nm_, pt_ = (o.get("name") or "").lower(), o.get("point")
                            if pt_ is not None and nm_ in ("over", "under"):
                                per_line.setdefault(pt_, {})[nm_] = float(o["price"])
                        for pt_, d_ in per_line.items():
                            if "over" in d_ and "under" in d_:
                                raw_totals.setdefault(pt_, {})[bm_slug] = d_
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
                "raw_totals":     raw_totals,
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
        # FIX: la cache non scadeva mai. Se il bot restava acceso con la lista di
        # un torneo ormai finito (es. tennis_wta_singapore_open), gli scan
        # interrogavano solo quello → 0 eventi → fallback su OddsPapi (quota
        # condivisa col ping pong), con The Odds API ancora a 500 crediti. La
        # lista arriva da /sports/ (non consuma crediti), quindi la rinfreschiamo
        # ogni TENNIS_KEYS_TTL_S secondi. Una lista vuota non è mai "fresca".
        if (
            self._odds_tennis_keys
            and (time.monotonic() - self._odds_tennis_keys_ts) < TENNIS_KEYS_TTL_S
        ):
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
                self._odds_tennis_keys_ts = time.monotonic()
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
                    if sig.get("sport") == "tennis" and sig.get("tournament") and result_due(sig):
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
                saved_ts    = self._odds_tennis_keys_ts
                self._odds_tennis_keys = None
                all_tennis_keys = await self._get_active_tennis_sport_keys(session)
                if not all_tennis_keys:
                    self._odds_tennis_keys = saved_cache
                    self._odds_tennis_keys_ts = saved_ts
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
                    if sig.get("sport") == "tabletennis" and result_due(sig):
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
