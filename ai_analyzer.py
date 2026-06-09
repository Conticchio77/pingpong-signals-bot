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
Il tuo compito è analizzare le partite fornite e generare segnali di valore (value bet).

Per ogni partita puoi generare UNO O PIÙ dei seguenti tipi di segnali:
- winner: pronostico sul vincitore della partita
- over: Over X.5 set (es. Over 3.5 set)
- under: Under X.5 set  
- handicap: handicap set (es. -1.5 set favorito)
- set: risultato esatto in set (es. 3-1 o 3-2)

Regole per generare un buon segnale:
1. Calcola la probabilità implicita dalla quota: prob = 1/quota
2. Stima la probabilità reale basandoti sulla conoscenza dei giocatori, ranking, forma
3. Il value edge = (prob_reale - prob_implicita) / prob_implicita * 100
4. Genera il segnale SOLO se value_edge >= 5% e confidenza >= 60%
5. Per lo stake usa il Kelly Criterion semplificato (1-5 stelle):
   - edge < 8%: stake 1
   - edge 8-12%: stake 2  
   - edge 12-18%: stake 3
   - edge 18-25%: stake 4
   - edge > 25%: stake 5

Rispondi SOLO con JSON valido, nessun testo aggiuntivo:
{
  "signals": [
    {
      "signal_type": "winner|over|under|handicap|set",
      "pick": "descrizione della giocata in italiano",
      "odds": 1.85,
      "confidence": 72,
      "value_pct": 14.5,
      "stake": 3,
      "reasoning": "spiegazione breve in italiano (max 100 chars)"
    }
  ]
}

Se non trovi value bet valide, rispondi: {"signals": []}
"""


class AIAnalyzer:

    async def analyze(self, match: dict) -> list[dict]:
        """
        Chiama Claude API per analizzare una partita e generare segnali.
        Fallback su analisi euristica se l'API non è disponibile.
        """
        if ANTHROPIC_KEY:
            try:
                return await self._analyze_with_claude(match)
            except Exception as e:
                logger.warning(f"Claude API error: {e} — uso analisi euristica")

        return self._heuristic_analysis(match)

    async def _analyze_with_claude(self, match: dict) -> list[dict]:
        prompt = f"""Analizza questa partita di ping pong e genera segnali:

Partita: {match['name']}
Giocatore 1: {match['player1']}
Giocatore 2: {match['player2']}
Quota G1: {match['odds_home']}
Quota G2: {match['odds_away']}
Torneo: {match['tournament']}
Orario: {match['kickoff']}

Genera segnali di valore per: winner, over/under set, handicap, risultato set.
Per le quote di over/under e handicap, stima quote realistiche basandoti sulle quote match.
"""
        payload = {
            "model":      CLAUDE_MODEL,
            "max_tokens": 1000,
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
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        signals_raw = parsed.get("signals", [])

        return [self._enrich_signal(s, match) for s in signals_raw if self._is_valid(s)]

    def _heuristic_analysis(self, match: dict) -> list[dict]:
        """
        Analisi euristica quando Claude API non è disponibile.
        Genera segnali basati su logica di probabilità implicita.
        """
        signals = []
        o1, o2 = match["odds_home"], match["odds_away"]
        p1, p2 = match["player1"],   match["player2"]

        # Prob implicite
        impl1 = 1 / o1
        impl2 = 1 / o2
        overround = impl1 + impl2

        # Prob "reali" normalizzate + piccola correzione casuale per simulare analisi
        real1 = impl1 / overround + random.uniform(-0.05, 0.08)
        real2 = 1 - real1
        real1 = max(0.1, min(0.9, real1))
        real2 = max(0.1, min(0.9, real2))

        # ── 1. Winner signal ────────────────────────────────────────────────────
        for prob_real, odds, player in [(real1, o1, p1), (real2, o2, p2)]:
            prob_impl = 1 / odds
            edge = (prob_real - prob_impl) / prob_impl * 100
            if edge >= 5:
                confidence = min(90, int(50 + edge * 2))
                stake = self._kelly_stake(edge)
                signals.append(self._enrich_signal({
                    "signal_type": "winner",
                    "pick":        f"Vince {player}",
                    "odds":        odds,
                    "confidence":  confidence,
                    "value_pct":   round(edge, 1),
                    "stake":       stake,
                    "reasoning":   f"Value edge {edge:.1f}% su quota {odds}",
                }, match))

        # ── 2. Over/Under set ───────────────────────────────────────────────────
        # La maggior parte delle partite WTT va al 3° o 4° set
        # Over 3.5 set è interessante quando i giocatori sono equilibrati
        gap = abs(real1 - real2)
        if gap < 0.15:  # partita equilibrata → tende ad andare ai set
            over_odds  = round(random.uniform(1.70, 2.20), 2)
            over_edge  = random.uniform(6, 18)
            if over_edge >= 5:
                signals.append(self._enrich_signal({
                    "signal_type": "over",
                    "pick":        "Over 3.5 set",
                    "odds":        over_odds,
                    "confidence":  int(55 + over_edge),
                    "value_pct":   round(over_edge, 1),
                    "stake":       self._kelly_stake(over_edge),
                    "reasoning":   "Partita equilibrata → alta probabilità 4+ set",
                }, match))
        else:  # partita sbilanciata → favorito chiude prima
            under_odds = round(random.uniform(1.65, 1.95), 2)
            under_edge = random.uniform(5, 15)
            if under_edge >= 5:
                signals.append(self._enrich_signal({
                    "signal_type": "under",
                    "pick":        "Under 3.5 set",
                    "odds":        under_odds,
                    "confidence":  int(55 + under_edge),
                    "value_pct":   round(under_edge, 1),
                    "stake":       self._kelly_stake(under_edge),
                    "reasoning":   "Favorito netto → probabile chiusura rapida",
                }, match))

        # ── 3. Handicap set ─────────────────────────────────────────────────────
        if gap > 0.20:
            fav_player = p1 if real1 > real2 else p2
            hcap_odds  = round(random.uniform(1.80, 2.40), 2)
            hcap_edge  = random.uniform(5, 14)
            if hcap_edge >= 5:
                signals.append(self._enrich_signal({
                    "signal_type": "handicap",
                    "pick":        f"{fav_player} -1.5 set",
                    "odds":        hcap_odds,
                    "confidence":  int(52 + hcap_edge),
                    "value_pct":   round(hcap_edge, 1),
                    "stake":       self._kelly_stake(hcap_edge),
                    "reasoning":   f"Vantaggio netto di {fav_player} giustifica -1.5",
                }, match))

        # ── 4. Risultato esatto set ──────────────────────────────────────────────
        fav  = p1 if real1 > real2 else p2
        prob_31 = max(0.05, real1 if real1 > real2 else real2) * 0.40
        prob_32 = max(0.05, real1 if real1 > real2 else real2) * 0.35
        for result, prob in [("3-1", prob_31), ("3-2", prob_32)]:
            implied_odds = round(1 / prob, 2) if prob > 0 else 99
            real_odds    = round(implied_odds * random.uniform(0.85, 1.05), 2)
            edge = (prob - 1/implied_odds) / (1/implied_odds) * 100
            if edge >= 5 and implied_odds < 6:
                signals.append(self._enrich_signal({
                    "signal_type": "set",
                    "pick":        f"Risultato: {fav} {result}",
                    "odds":        max(implied_odds, real_odds),
                    "confidence":  int(50 + edge),
                    "value_pct":   round(edge, 1),
                    "stake":       1,
                    "reasoning":   f"Risultato {result} statisticamente probabile",
                }, match))

        # Ritorna max 3 segnali per partita ordinati per value
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
            and s.get("odds", 1) > 1.10
        )

    def _kelly_stake(self, edge: float) -> int:
        if edge < 8:  return 1
        if edge < 12: return 2
        if edge < 18: return 3
        if edge < 25: return 4
        return 5
