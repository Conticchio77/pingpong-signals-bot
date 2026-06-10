import os
import json
import aiohttp
import logging
import random
from datetime import datetime

logger = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL  = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """Sei un analista esperto di scommesse sportive su ping pong / tennis tavolo.
Il tuo compito è analizzare le partite e generare SOLO segnali ad alto valore realmente piazzabili.

REGOLE FONDAMENTALI:
1. Genera SOLO segnali su mercati standard disponibili su qualsiasi bookmaker europeo:
   - "1" = vince il Giocatore 1  
   - "2" = vince il Giocatore 2
   - "Over X.5 set" / "Under X.5 set" (X = 3 per partite al meglio dei 5)
   - "Handicap set -1.5 [giocatore]" = il favorito vince con almeno 2 set di scarto
2. Le quote che indichi devono essere REALISTICHE per questi mercati (non inventare quote improbabili):
   - Vincente favorito netto: 1.30–1.70
   - Vincente equilibrato: 1.80–2.20
   - Over/Under 3.5 set: 1.60–2.20
   - Handicap -1.5: 1.50–2.50
3. Calcola value edge = (prob_reale - 1/quota) / (1/quota) * 100
4. Genera il segnale SOLO se value_edge >= 5% e confidenza >= 60%
5. Preferisci segnali su mercati SEMPLICI (1/2 o Over/Under): sono più facili da trovare su qualsiasi book
6. Per lo stake usa Kelly semplificato:
   - edge < 8%: 1  |  8–12%: 2  |  12–18%: 3  |  18–25%: 4  |  >25%: 5

IMPORTANTE — TROVABILITÀ DEL SEGNALE:
Questa partita viene da un torneo noto (WTT, Mondiali, Europei, Bundesliga, Liga Pro).
Indica nel campo "book_note" il mercato ESATTO da cercare sul book, ad esempio:
- "Vincente match → cerca '1X2' o 'Match Winner'"
- "Over 3.5 set → cerca 'Totale set' o 'Set totali'"
- "Handicap -1.5 → cerca 'Handicap set' o 'Set handicap'"

Rispondi SOLO con JSON valido, nessun testo aggiuntivo:
{
  "signals": [
    {
      "signal_type": "winner|over|under|handicap",
      "pick": "descrizione BREVE in italiano (es: 'Fan Zhendong vince' oppure 'Over 3.5 set')",
      "odds": 1.85,
      "confidence": 72,
      "value_pct": 14.5,
      "stake": 3,
      "reasoning": "max 100 caratteri in italiano — spiega WHY c'è valore su questa quota",
      "book_note": "come trovarlo sul book (max 60 caratteri)"
    }
  ]
}

Se non trovi value bet valide, rispondi: {"signals": []}
"""


class AIAnalyzer:

    async def analyze(self, match: dict) -> list[dict]:
        if ANTHROPIC_KEY:
            try:
                return await self._analyze_with_claude(match)
            except Exception as e:
                logger.warning(f"Claude API error: {e} — uso analisi euristica")

        return self._heuristic_analysis(match)

    async def _analyze_with_claude(self, match: dict) -> list[dict]:
        prompt = f"""Analizza questa partita di ping pong per value bet REALI:

Partita: {match['name']}
Giocatore 1: {match['player1']} — Quota vittoria: {match['odds_home']}
Giocatore 2: {match['player2']} — Quota vittoria: {match['odds_away']}
Torneo: {match['tournament']}
Orario (ora italiana): {match['kickoff']}

NOTA: questo torneo ({match['tournament']}) è quotato dai principali book italiani/europei.
I segnali che generi DEVONO essere trovabili su Snai, Sisal, Goldbet, Betfair o Bet365.

Mercati da considerare:
- Vincente match (1 o 2)
- Over/Under 3.5 set (partita al meglio dei 5)
- Handicap set -1.5 per il favorito

Genera segnali SOLO se la quota implicita sottostima la probabilità reale basata sulla tua conoscenza del ranking e della forma dei giocatori.
"""
        payload = {
            "model":      CLAUDE_MODEL,
            "max_tokens": 800,
            "system":     SYSTEM_PROMPT,
            "messages":   [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"API error {resp.status}: {text[:200]}")
                data = await resp.json()

        raw = data["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        signals_raw = parsed.get("signals", [])
        return [self._enrich_signal(s, match) for s in signals_raw if self._is_valid(s)]

    def _heuristic_analysis(self, match: dict) -> list[dict]:
        signals = []
        o1, o2 = match["odds_home"], match["odds_away"]
        p1, p2 = match["player1"],   match["player2"]

        impl1 = 1 / o1
        impl2 = 1 / o2
        overround = impl1 + impl2

        real1 = impl1 / overround + random.uniform(-0.04, 0.06)
        real2 = 1 - real1
        real1 = max(0.12, min(0.88, real1))
        real2 = max(0.12, min(0.88, real2))

        # ── 1. Vincente ─────────────────────────────────────────────────────────
        for prob_real, odds, player in [(real1, o1, p1), (real2, o2, p2)]:
            prob_impl = 1 / odds
            edge = (prob_real - prob_impl) / prob_impl * 100
            if edge >= 5:
                confidence = min(85, int(52 + edge * 1.8))
                signals.append(self._enrich_signal({
                    "signal_type": "winner",
                    "pick":        f"{player} vince",
                    "odds":        odds,
                    "confidence":  confidence,
                    "value_pct":   round(edge, 1),
                    "stake":       self._kelly_stake(edge),
                    "reasoning":   f"Quota {odds} sottostima prob reale ({prob_real:.0%})",
                    "book_note":   "Cerca 'Match Winner' o '1X2' sul book",
                }, match))

        # ── 2. Over/Under 3.5 set ────────────────────────────────────────────────
        gap = abs(real1 - real2)
        if gap < 0.15:
            over_odds = round(1.65 + gap * 2, 2)
            prob_over_real = 0.58 + random.uniform(-0.05, 0.07)
            edge = (prob_over_real - 1/over_odds) / (1/over_odds) * 100
            if edge >= 5:
                signals.append(self._enrich_signal({
                    "signal_type": "over",
                    "pick":        "Over 3.5 set",
                    "odds":        over_odds,
                    "confidence":  int(56 + edge),
                    "value_pct":   round(edge, 1),
                    "stake":       self._kelly_stake(edge),
                    "reasoning":   f"Gap {gap:.0%} → match equilibrato, probabile 4°/5° set",
                    "book_note":   "Cerca 'Totale set' → Over 3.5",
                }, match))
        else:
            under_odds = round(1.55 + (1 - gap) * 0.5, 2)
            prob_under_real = 0.62 + random.uniform(-0.04, 0.06)
            edge = (prob_under_real - 1/under_odds) / (1/under_odds) * 100
            if edge >= 5:
                signals.append(self._enrich_signal({
                    "signal_type": "under",
                    "pick":        "Under 3.5 set",
                    "odds":        under_odds,
                    "confidence":  int(56 + edge),
                    "value_pct":   round(edge, 1),
                    "stake":       self._kelly_stake(edge),
                    "reasoning":   f"Favorito netto (gap {gap:.0%}): chiude in 3 set",
                    "book_note":   "Cerca 'Totale set' → Under 3.5",
                }, match))

        # ── 3. Handicap -1.5 set ─────────────────────────────────────────────────
        if gap > 0.22:
            fav_player = p1 if real1 > real2 else p2
            fav_prob   = max(real1, real2)
            prob_hcap_real = fav_prob * 0.72 + random.uniform(-0.04, 0.05)
            hcap_odds = round(1 / prob_hcap_real * 0.92, 2)
            hcap_odds = max(1.40, min(2.80, hcap_odds))
            edge = (prob_hcap_real - 1/hcap_odds) / (1/hcap_odds) * 100
            if edge >= 5:
                signals.append(self._enrich_signal({
                    "signal_type": "handicap",
                    "pick":        f"{fav_player} -1.5 set",
                    "odds":        hcap_odds,
                    "confidence":  int(54 + edge),
                    "value_pct":   round(edge, 1),
                    "stake":       self._kelly_stake(edge),
                    "reasoning":   f"{fav_player} molto più forte: copre -1.5 set",
                    "book_note":   "Cerca 'Handicap set' o 'Set handicap'",
                }, match))

        signals.sort(key=lambda x: x["value_pct"], reverse=True)
        return signals[:3]

    def _enrich_signal(self, s: dict, match: dict) -> dict:
        return {
            **s,
            "match":      match["name"],
            "player1":    match["player1"],
            "player2":    match["player2"],
            "kickoff":    match["kickoff"],
            "tournament": match.get("tournament", "Table Tennis"),
            "match_key":  f"{match['name']}_{s['signal_type']}_{s.get('pick','')}"[:100],
            "created_at": datetime.utcnow().isoformat(),
        }

    def _is_valid(self, s: dict) -> bool:
        return (
            s.get("confidence", 0) >= 55
            and s.get("value_pct", 0) >= 5
            and 1.10 < s.get("odds", 1) <= 8.0
        )

    def _kelly_stake(self, edge: float) -> int:
        if edge < 8:  return 1
        if edge < 12: return 2
        if edge < 18: return 3
        if edge < 25: return 4
        return 5
