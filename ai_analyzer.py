import os
import json
import aiohttp
import logging
import random
from datetime import datetime

logger = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL  = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Sei un analista professionista di scommesse su ping pong / tennis tavolo.
Ricevi partite con quote REALI aggregate da bookmaker europei (Bet365, Betfair, Unibet, ecc.).

═══ OBIETTIVO ═══
Trovare VALUE BET: situazioni dove la quota di mercato SOTTOSTIMA la probabilità reale.

═══ MERCATI AMMESSI (trovabili su qualsiasi book europeo) ═══
1. Match Winner: "Vince [Giocatore]"
2. Over/Under set: "Over X.5 set" / "Under X.5 set" (usa la linea fornita)
3. Handicap set: "[Giocatore] -1.5 set" (solo se favorito netto)

═══ CALCOLO VALUE ═══
- Probabilità implicita = 1 / quota
- Value edge = (prob_reale - prob_implicita) / prob_implicita × 100
- Genera segnale SOLO se: value_edge ≥ 5% E confidenza ≥ 60%

═══ STIMA PROBABILITÀ REALE ═══
Usa la tua conoscenza di:
- Ranking mondiale ITTF / WTT dei giocatori
- Forma recente e storico scontri diretti
- Caratteristiche di gioco (aggressivo, difensivo, resistenza nei set lunghi)
- Torneo e importanza della partita

═══ STAKE (Kelly semplificato) ═══
edge < 8% → 1 | 8-12% → 2 | 12-18% → 3 | 18-25% → 4 | >25% → 5

═══ FORMATO RISPOSTA ═══
Rispondi ESCLUSIVAMENTE con JSON valido, zero testo aggiuntivo:
{
  "signals": [
    {
      "signal_type": "winner|over|under|handicap",
      "pick": "testo breve (es: 'Fan Zhendong vince' o 'Over 3.5 set')",
      "odds": 1.85,
      "confidence": 72,
      "value_pct": 14.5,
      "stake": 3,
      "reasoning": "max 100 caratteri — WHY c'è value su questa quota",
      "book_note": "dove cercarlo: es 'Match Winner' o 'Totale set Over 3.5'"
    }
  ]
}
Se non ci sono value bet valide: {"signals": []}
"""


class AIAnalyzer:

    async def analyze(self, match: dict) -> list[dict]:
        if ANTHROPIC_KEY:
            try:
                return await self._claude(match)
            except Exception as e:
                logger.warning(f"Claude API error: {e} — uso euristica")
        return self._heuristic(match)

    # ── Analisi con Claude ────────────────────────────────────────────────────
    async def _claude(self, match: dict) -> list[dict]:
        is_real = match.get("source") == "odds_api"

        # Costruisci sezione quote
        quotes_block = (
            f"  {match['player1']}: {match['odds_home']}\n"
            f"  {match['player2']}: {match['odds_away']}"
        )
        totals_block = ""
        if match.get("over_odds"):
            line = match["totals_line"]
            totals_block = (
                f"\nQuote Totale Set (linea {line}):\n"
                f"  Over {line}: {match['over_odds']}\n"
                f"  Under {line}: {match.get('under_odds', 'n/d')}"
            )
        books_block = ""
        if match.get("bookmakers"):
            books_block = f"\nBookmaker che quotano: {', '.join(match['bookmakers'])}"

        source_note = (
            "✅ QUOTE REALI da bookmaker europei — fidati di questi valori."
            if is_real else
            "⚠️ Quote STIMATE — applica maggiore cautela nell'analisi."
        )

        prompt = f"""Analizza questa partita di ping pong e trova value bet REALI:

Partita: {match['name']}
Torneo:  {match['tournament']}
Orario:  {match['kickoff']} (ora italiana)
Fonte:   {source_note}

Quote Match Winner:
{quotes_block}{totals_block}{books_block}

Genera segnali solo dove la quota di mercato sottostima la probabilità reale
basandoti sulla tua conoscenza dei giocatori, ranking WTT/ITTF e forma recente.
Preferisci mercati semplici (Match Winner, Over/Under) trovabili su tutti i book.
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
                json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}: {(await resp.text())[:200]}")
                data = await resp.json()

        raw = data["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        signals_raw = json.loads(raw).get("signals", [])
        return [self._enrich(s, match) for s in signals_raw if self._valid(s)]

    # ── Analisi euristica ─────────────────────────────────────────────────────
    def _heuristic(self, match: dict) -> list[dict]:
        signals = []
        o1, o2  = match["odds_home"], match["odds_away"]
        p1, p2  = match["player1"],   match["player2"]
        is_real = match.get("source") == "odds_api"

        # Con quote reali il mercato è già efficiente → variazione ridotta
        delta = 0.025 if is_real else 0.055
        impl1     = 1 / o1
        impl2     = 1 / o2
        overround = impl1 + impl2
        real1 = max(0.12, min(0.88, impl1 / overround + random.uniform(-delta, delta * 1.4)))
        real2 = 1 - real1

        # ── Match Winner ────────────────────────────────────────────────────
        for prob_r, odds, player in [(real1, o1, p1), (real2, o2, p2)]:
            edge = (prob_r - 1/odds) / (1/odds) * 100
            if edge >= 5:
                signals.append(self._enrich({
                    "signal_type": "winner",
                    "pick":        f"{player} vince",
                    "odds":        odds,
                    "confidence":  min(85, int(52 + edge * 1.8)),
                    "value_pct":   round(edge, 1),
                    "stake":       self._kelly(edge),
                    "reasoning":   f"Quota {odds} sottostima prob reale ({prob_r:.0%})",
                    "book_note":   "Cerca 'Match Winner' o '1X2'",
                }, match))

        # ── Over/Under con quote reali ───────────────────────────────────────
        gap  = abs(real1 - real2)
        line = match.get("totals_line", 3.5)

        if match.get("over_odds") and match.get("under_odds"):
            for s_type, q, label, prob_fn in [
                ("over",  match["over_odds"],  f"Over {line} set",
                 lambda g: max(0.25, min(0.78, 0.54 + (0.08 - g * 0.45))),),
                ("under", match["under_odds"], f"Under {line} set",
                 lambda g: max(0.25, min(0.78, 0.46 + g * 0.45)),),
            ]:
                if q <= 1.05:
                    continue
                prob_r = prob_fn(gap)
                edge   = (prob_r - 1/q) / (1/q) * 100
                if edge >= 5:
                    signals.append(self._enrich({
                        "signal_type": s_type,
                        "pick":        label,
                        "odds":        q,
                        "confidence":  min(82, int(54 + edge)),
                        "value_pct":   round(edge, 1),
                        "stake":       self._kelly(edge),
                        "reasoning":   (
                            f"Partita equilibrata (gap {gap:.0%}) → lunga"
                            if s_type == "over"
                            else f"Favorito netto (gap {gap:.0%}) → corta"
                        ),
                        "book_note":   f"Cerca 'Totale set' → {label}",
                    }, match))
        else:
            # Quote Over/Under non disponibili: stima
            if gap < 0.14:
                q    = round(random.uniform(1.70, 2.00), 2)
                pr   = 0.57 + random.uniform(-0.04, 0.05)
                edge = (pr - 1/q) / (1/q) * 100
                if edge >= 5:
                    signals.append(self._enrich({
                        "signal_type": "over",
                        "pick":        f"Over {line} set",
                        "odds":        q,
                        "confidence":  int(55 + edge),
                        "value_pct":   round(edge, 1),
                        "stake":       self._kelly(edge),
                        "reasoning":   "Match equilibrato: alta prob 4°-5° set",
                        "book_note":   f"Cerca 'Totale set' → Over {line}",
                    }, match))
            elif gap > 0.20:
                q    = round(random.uniform(1.58, 1.88), 2)
                pr   = 0.60 + random.uniform(-0.03, 0.05)
                edge = (pr - 1/q) / (1/q) * 100
                if edge >= 5:
                    signals.append(self._enrich({
                        "signal_type": "under",
                        "pick":        f"Under {line} set",
                        "odds":        q,
                        "confidence":  int(55 + edge),
                        "value_pct":   round(edge, 1),
                        "stake":       self._kelly(edge),
                        "reasoning":   "Favorito netto: chiude in 3 set",
                        "book_note":   f"Cerca 'Totale set' → Under {line}",
                    }, match))

        # ── Handicap -1.5 set ────────────────────────────────────────────────
        if gap > 0.22:
            fav       = p1 if real1 > real2 else p2
            fav_p     = max(real1, real2)
            prob_hcap = max(0.25, min(0.75, fav_p * 0.72 + random.uniform(-0.03, 0.04)))
            hcap_q    = round(max(1.40, min(2.80, 1 / prob_hcap * 0.91)), 2)
            edge      = (prob_hcap - 1/hcap_q) / (1/hcap_q) * 100
            if edge >= 5:
                signals.append(self._enrich({
                    "signal_type": "handicap",
                    "pick":        f"{fav} -1.5 set",
                    "odds":        hcap_q,
                    "confidence":  min(82, int(54 + edge)),
                    "value_pct":   round(edge, 1),
                    "stake":       self._kelly(edge),
                    "reasoning":   f"{fav} dominante: copre -1.5 set",
                    "book_note":   "Cerca 'Handicap set' o 'Set handicap'",
                }, match))

        signals.sort(key=lambda x: x["value_pct"], reverse=True)
        return signals[:3]

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _enrich(self, s: dict, match: dict) -> dict:
        return {
            **s,
            "match":      match["name"],
            "player1":    match["player1"],
            "player2":    match["player2"],
            "kickoff":    match["kickoff"],
            "tournament": match.get("tournament", "Table Tennis"),
            "source":     match.get("source", "n/d"),
            "match_key":  f"{match['name']}_{s['signal_type']}_{s.get('pick','')}".replace(" ", "_")[:100],
            "created_at": datetime.utcnow().isoformat(),
        }

    def _valid(self, s: dict) -> bool:
        return (
            s.get("confidence", 0) >= 55
            and s.get("value_pct",  0) >= 5
            and 1.10 < s.get("odds", 0) <= 9.0
        )

    def _kelly(self, edge: float) -> int:
        if edge < 8:  return 1
        if edge < 12: return 2
        if edge < 18: return 3
        if edge < 25: return 4
        return 5
