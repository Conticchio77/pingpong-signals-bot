import os
import io
import logging
import asyncio
import time
import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scraper import SignalScraper, result_due, pingpong_scan_hours
from ai_analyzer import AIAnalyzer
from database import Database

import httpx

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
# httpx a INFO logga ogni URL, token Telegram incluso (e un getUpdates ogni 10s
# riempie i log di Railway) — a WARNING restano solo errori veri.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ROME         = ZoneInfo("Europe/Rome")
TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "858001417"))
VIP_GROUP_ID  = int(os.environ.get("VIP_GROUP_ID",  "-1004272035660"))
FREE_GROUP_ID = int(os.environ.get("FREE_GROUP_ID", "-1002520876408"))
ODDS_KEY     = os.environ.get("ODDS_API_KEY", "")

# ── Relay verso il bot "Segnali dal Futuro" (inoltro privato filtrato per utente) ──
# Telegram non consegna ai bot i messaggi scritti da altri bot: il post nel gruppo VIP
# resta solo un contenitore/archivio visivo. L'inoltro privato agli utenti passa da qui.
RELAY_URL = os.environ.get("RELAY_URL", "https://node-red-production-9694.up.railway.app/relay-segnale-esterno")
RELAY_KEY = os.environ.get("RELAY_SECRET_KEY", "xUp_WW4mUaGCIZNoWkSWXx3c6BpNDaZy")

async def relay_to_private(text: str, category: str = "ping_signal"):
    """Manda il segnale all'endpoint del bot VIP, che lo inoltra in privato agli utenti idonei."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                RELAY_URL,
                headers={"X-Relay-Key": RELAY_KEY},
                json={"category": category, "text": text, "parse_mode": "Markdown"},
            )
            if resp.status_code != 200:
                logger.warning(f"Relay privato fallito ({resp.status_code}): {resp.text[:200]}")
            else:
                logger.info(f"Relay privato ok: {resp.json()}")
    except Exception as e:
        logger.warning(f"Relay privato: errore di connessione: {e}")

def _dest_label(sport: str) -> str:
    settings = db.get_settings()
    key = "send_dest_tennis" if sport == "tennis" else "send_dest_pingpong"
    return {"vip": "VIP", "free": "Free", "both": "VIP+Free"}.get(settings.get(key, "vip"), "VIP")

def _sport_short(sport: str) -> str:
    return "tennis" if sport == "tennis" else "pingpong"

def _dest_setting(kind: str, short: str) -> str:
    """Destinazione (vip/free/both/none) per un tipo di messaggio e uno sport.
    kind: "send" (segnali), "result" (vinto/perso), "stats" (statistiche
    mensili). Per result/stats, se non è mai stata scelta, segue la
    destinazione dei segnali di quello sport."""
    base = db.get_settings().get(f"send_dest_{short}", "vip")
    if kind == "send":
        return base
    return db.get_setting(f"{kind}_dest_{short}") or base

_DEST_NAMES = {"vip": "📤 VIP", "free": "🆓 Free", "both": "📤🆓 Entrambi", "none": "🚫 Nessuno"}

def _dest_group_ids(sport: str, kind: str = "send") -> list[int]:
    """Gruppi a cui inviare, in base alla destinazione scelta per quello
    sport e tipo di messaggio (segnali / risultati / statistiche)."""
    dest = _dest_setting(kind, _sport_short(sport))
    if dest == "none":
        return []
    if dest == "free":
        return [FREE_GROUP_ID]
    if dest == "both":
        return [VIP_GROUP_ID, FREE_GROUP_ID]
    return [VIP_GROUP_ID]

def _group_name(gid: int) -> str:
    return "VIP" if gid == VIP_GROUP_ID else ("Free" if gid == FREE_GROUP_ID else str(gid))

async def send_to_groups(bot, sport: str, kind: str, text: str, reply_markup=None) -> list[int]:
    """Invia text ai gruppi scelti per (sport, kind). Ritorna gli ID dove è
    arrivato davvero (gli errori, es. bot non nel gruppo, sono solo loggati)."""
    sent = []
    for gid in _dest_group_ids(sport, kind):
        try:
            await bot.send_message(chat_id=gid, text=text, parse_mode="Markdown", reply_markup=reply_markup)
            sent.append(gid)
        except Exception as e:
            logger.warning(f"Invio ({kind}) al gruppo {gid} fallito: {e}")
    return sent

def _sent_note(sport: str, kind: str, sent: list[int]) -> str:
    """Riga di conferma per l'admin su dove è arrivato il messaggio."""
    wanted = _dest_group_ids(sport, kind)
    if not wanted:
        return "📨 Gruppi: nessuno (destinazione disattivata)"
    names = ", ".join(_group_name(g) for g in sent) or "—"
    failed = [g for g in wanted if g not in sent]
    extra = f" ⚠️ non riuscito: {', '.join(_group_name(g) for g in failed)}" if failed else ""
    return f"📨 Inviato a: {names}{extra}"

def _result_group_text(sig: dict, result: str, bal: dict) -> str:
    emoji = "✅" if result == "won" else "❌"
    sport_label = sig.get("sport_label", "🏓")
    bal_str = f"{bal['current_balance_pct']:+.2f}% bankroll" if bal["total_bets"] > 0 else "n/d"
    return (
        f"{emoji} *Risultato*\n"
        f"{'━' * 20}\n"
        f"{sport_label} {sig['match']}\n"
        f"🎯 {sig['pick']} @ {sig['odds']}\n"
        f"📌 Stake: {sig['stake']}/5\n\n"
        f"{emoji} *{'VINTO! 🎉' if result == 'won' else 'Perso.'}*\n\n"
        f"💰 Bilancio {sport_label}: *{bal_str}* | ROI: *{bal['roi']:+.1f}%*"
    )

async def send_signal_to_groups(bot, sig: dict, text: str, dest: str | None = None) -> list[int]:
    """Invia il segnale ai gruppi. dest: "vip" | "free" | "both"; se None usa la
    destinazione impostata per lo sport. Il relay privato (DM agli iscritti VIP)
    parte solo se tra le destinazioni c'è il VIP. Ritorna gli ID dei gruppi in
    cui il messaggio è arrivato davvero (vuoto = invio fallito ovunque)."""
    if dest is None:
        dest = _dest_setting("send", _sport_short(sig.get("sport", "tabletennis")))
    gids = {"free": [FREE_GROUP_ID], "both": [VIP_GROUP_ID, FREE_GROUP_ID]}.get(dest, [VIP_GROUP_ID])
    sent = []
    for gid in gids:
        try:
            await bot.send_message(chat_id=gid, text=text, parse_mode="Markdown")
            sent.append(gid)
        except Exception as e:
            logger.warning(f"Invio segnale al gruppo {gid} fallito: {e}")
    if VIP_GROUP_ID in gids:
        await relay_to_private(text)
    return sent

def _send_rows(s: dict) -> list:
    """Bottoni di invio manuale: VIP / FREE / ENTRAMBI (o solo il gruppo
    mancante se il segnale è già stato inviato a uno dei due)."""
    sid = s["id"]
    vip, free = bool(s.get("sent_to_vip")), bool(s.get("sent_to_free"))
    if s.get("status") in ("won", "lost"):
        return []
    if vip and free:
        return []
    if vip:
        return [[InlineKeyboardButton("🆓 Invia anche a FREE", callback_data=f"sendto_free_{sid}")]]
    if free:
        return [[InlineKeyboardButton("📤 Invia anche a VIP", callback_data=f"sendto_vip_{sid}")]]
    if s.get("status") == "sent":      # inviato prima del tracciamento per gruppo: non so dove
        return []
    return [
        [InlineKeyboardButton("📤 VIP", callback_data=f"sendto_vip_{sid}"),
         InlineKeyboardButton("🆓 FREE", callback_data=f"sendto_free_{sid}"),
         InlineKeyboardButton("📤🆓 ENTRAMBI", callback_data=f"sendto_both_{sid}")],
        [InlineKeyboardButton("🗑 Scarta", callback_data=f"discard_{sid}")],
    ]

db       = Database()
scraper  = SignalScraper(db=db)
analyzer = AIAnalyzer()

# ── Tastiera persistente (sempre visibile in basso) ──────────────────────────────
PERSISTENT_KB = ReplyKeyboardMarkup(
    [
        ["🔍 Scan",  "📋 Segnali"],
        ["📊 Stats", "⚙️ Impostazioni"],
        ["📡 Quota", "🏠 Home"],
    ],
    resize_keyboard=True,
)

# ── Helpers ─────────────────────────────────────────────────────────────────────
def signal_text(s: dict, for_vip: bool = False) -> str:
    icons  = {"winner": "🏆", "over": "📈", "under": "📉", "handicap": "⚖️", "set": "🎯"}
    icon   = icons.get(s["signal_type"], "🎯")
    stars  = "⭐" * min(5, max(1, round(s["confidence"] / 20)))
    vsign  = f"+{s['value_pct']:.1f}%" if s["value_pct"] > 0 else f"{s['value_pct']:.1f}%"

    # Determina sport e intestazione
    sport_label = s.get("sport_label", "🏓 Ping Pong")
    sport_name  = "PING PONG" if "Ping" in sport_label else "TENNIS"
    default_tourn = "Ping Pong" if "Ping" in sport_label else "Tennis"

    # Fonte dati
    src_map = {
        "odds_api":              "📡 The Odds API",
        "oddspapi":              "📡 OddsPapi",
        "oddspapi_noodds":       "📡 OddsPapi (fixture only)",
        "oddspapi_tennis":       "📡 OddsPapi (fallback tennis)",
        "oddspapi_tennis_noodds":"📡 OddsPapi (fallback tennis, fixture only)",
        "fallback":              "⚠️ Quote stimate",
    }
    src = src_map.get(s.get("source", ""), "⚠️ Quote stimate")

    text = (
        f"{sport_label} *SEGNALE {sport_name}*\n"
        f"{'━' * 22}\n"
        f"{icon} *{s['match']}*\n"
        f"🎯 Giocata: *{s['pick']}*\n"
        f"💰 Quota: *{s['odds']}*\n"
        f"📊 Confidenza: *{s['confidence']}%* {stars}\n"
        f"💡 Value edge: *{vsign}*\n"
        f"📌 Stake: *{s['stake']}/5* | 💹 Puntata: *{db.get_settings().get('stake_pct', 0.5)}% bankroll*\n"
        f"⏰ Inizio: *{s['kickoff']}*\n"
        f"🌍 Torneo: {s.get('tournament', default_tourn)}\n"
        f"🔗 Fonte: {src}\n"
    )
    if not for_vip:
        reasoning = s.get("reasoning", "")
        book_note = s.get("book_note", "")
        if reasoning:
            text += f"\n📝 _{reasoning}_"
        if book_note:
            text += f"\n🔎 _{book_note}_"
    return text

def vip_signal_text(s: dict) -> str:
    return signal_text(s, for_vip=True)

def now_it_str() -> str:
    return datetime.datetime.now(ROME).strftime("%d/%m %H:%M")

def _row_month(r: dict) -> str:
    """Mese (YYYYMM, fuso Roma) di una riga di balance_history (created_at è UTC)."""
    try:
        dt = datetime.datetime.fromisoformat(str(r.get("created_at", "")))
        return dt.replace(tzinfo=datetime.timezone.utc).astimezone(ROME).strftime("%Y%m")
    except Exception:
        return ""

def genera_grafico_bilancio(sport: str | None = None, month: str | None = None) -> io.BytesIO | None:
    """Genera un grafico PNG del bilancio (in % di bankroll) nel tempo.
    sport=None → totale su entrambi gli sport. Ritorna BytesIO o None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        history = db.get_balance_history(sport=sport)
        if month:
            history = [r for r in history if _row_month(r) == month]
        if len(history) < 2:
            return None
        # Bilancio cumulato ricalcolato sul sottoinsieme (sport) selezionato,
        # non la colonna "balance" salvata (quella è sempre la cumulata globale).
        running = 0.0
        cum_balances = []
        for r in history:
            running += r["profit"]
            cum_balances.append(running)

        balances = [0.0] + cum_balances
        labels   = ["Start"] + [f"#{i+1}" for i in range(len(history))]
        colors   = ["#2ecc71" if b >= 0 else "#e74c3c" for b in balances[1:]]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), facecolor="#1a1a2e")
        fig.suptitle("📊 Bilancio Segnali" + (f" — {month[4:]}/{month[:4]}" if month else ""), color="white", fontsize=16, fontweight="bold", y=0.98)

        # ── Grafico linea bilancio ─────────────────────────────────────────
        ax1.set_facecolor("#16213e")
        ax1.plot(range(len(balances)), balances, color="#3498db", linewidth=2.5, zorder=3)
        ax1.fill_between(
            range(len(balances)), balances, 0,
            where=[b >= 0 for b in balances],
            alpha=0.3, color="#2ecc71", label="Profitto"
        )
        ax1.fill_between(
            range(len(balances)), balances, 0,
            where=[b < 0 for b in balances],
            alpha=0.3, color="#e74c3c", label="Perdita"
        )
        ax1.axhline(0, color="white", linewidth=0.8, linestyle="--", alpha=0.5)
        ax1.set_ylabel("Bilancio (% bankroll)", color="white")
        ax1.tick_params(colors="white")
        ax1.spines[:].set_color("#444")
        ax1.set_xlim(0, len(balances) - 1)
        ax1.grid(axis="y", color="#333", linestyle="--", alpha=0.5)

        # Annotazione ultimo valore
        last_val = balances[-1]
        color_last = "#2ecc71" if last_val >= 0 else "#e74c3c"
        ax1.annotate(
            f"{last_val:+.2f}%",
            xy=(len(balances)-1, last_val),
            color=color_last, fontsize=12, fontweight="bold",
            xytext=(-40, 10), textcoords="offset points"
        )

        # ── Grafico barre singole scommesse ───────────────────────────────
        ax2.set_facecolor("#16213e")
        profits = [r["profit"] for r in history]
        bar_colors = ["#2ecc71" if p >= 0 else "#e74c3c" for p in profits]
        ax2.bar(range(len(profits)), profits, color=bar_colors, alpha=0.85, width=0.7)
        ax2.axhline(0, color="white", linewidth=0.8, linestyle="--", alpha=0.5)
        ax2.set_ylabel("Profitto per bet (% bankroll)", color="white")
        ax2.set_xlabel("Numero scommessa", color="white")
        ax2.tick_params(colors="white")
        ax2.spines[:].set_color("#444")
        ax2.grid(axis="y", color="#333", linestyle="--", alpha=0.5)

        won_patch  = mpatches.Patch(color="#2ecc71", label="Vinto")
        lost_patch = mpatches.Patch(color="#e74c3c", label="Perso")
        ax2.legend(handles=[won_patch, lost_patch], facecolor="#1a1a2e", labelcolor="white")

        plt.tight_layout(rect=[0, 0, 1, 0.96])

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#1a1a2e")
        plt.close(fig)
        buf.seek(0)
        return buf

    except ImportError:
        logger.warning("matplotlib non installato — grafico non disponibile")
        return None
    except Exception as e:
        logger.error(f"Errore grafico bilancio: {e}")
        return None

def admin_panel_text() -> str:
    s     = db.get_settings()
    stats = db.get_stats()

    oddspapi_key = os.environ.get("ODDSPAPI_KEY", "")
    src_parts = []
    if oddspapi_key:
        src_parts.append("📡 OddsPapi (🏓)")
    else:
        src_parts.append("⚠️ OddsPapi non configurata")
    if ODDS_KEY:
        src_parts.append("📡 The Odds API (🎾)")
    else:
        src_parts.append("⚠️ The Odds API non configurata")
    src_tag = " | ".join(src_parts)

    sport_filter = s.get("sport_filter", "both")
    sf_label = {"both": "🏓🎾 Entrambi", "tabletennis": "🏓 Solo Ping Pong", "tennis": "🎾 Solo Tennis"}.get(sport_filter, "🏓🎾 Entrambi")

    # ── Righe con la suddivisione per sport ──────────────────────────────────
    by_sport = stats.get("by_sport", {})
    sport_meta = {
        "tabletennis": "🏓 Ping Pong",
        "tennis":      "🎾 Tennis",
    }
    sport_lines = []
    seen_keys = set()
    for key, default_label in sport_meta.items():
        d = by_sport.get(key, {})
        label = d.get("sport_label") or default_label
        sport_lines.append(
            f"{label}: *{d.get('total', 0)}* tot | ⏳ {d.get('pending', 0)} | "
            f"✅ {d.get('won', 0)}V ❌ {d.get('lost', 0)}P | Win% {d.get('winrate', 0)}%"
        )
        seen_keys.add(key)
    # Eventuali sport extra non previsti sopra (mostrati solo se hanno segnali)
    for key, d in by_sport.items():
        if key not in seen_keys and d.get("total", 0) > 0:
            sport_lines.append(
                f"{d['sport_label']}: *{d['total']}* tot | ⏳ {d['pending']} | "
                f"✅ {d['won']}V ❌ {d['lost']}P | Win% {d['winrate']}%"
            )
    sport_breakdown = ("\n" + "\n".join(sport_lines) + "\n") if sport_lines else ""

    return (
        f"🏓🎾 *Signals Bot — Admin Panel*\n"
        f"{'━' * 26}\n"
        f"🕐 Ora: {now_it_str()}\n"
        f"🔗 {src_tag}\n\n"
        f"📨 Segnali tot: *{stats['total']}* | ⏳ Pendenti: *{stats['pending']}*\n"
        f"✅ Vinti: *{stats['won']}* | ❌ Persi: *{stats['lost']}* | 🏆 Win%: *{stats['winrate']}%*\n"
        f"{sport_breakdown}"
        f"🔄 Ultimo scan: *{stats['last_scan']}*\n\n"
        f"⚙️ Scan ogni *{s['scan_interval']}h* | "
        f"Confidenza min: 🎾*{s['min_confidence']}%* 🏓*{s['min_confidence_pp']}%* | "
        f"Auto-VIP: *{'✅' if s['auto_send'] else '❌'}* | "
        f"Sport: *{sf_label}*"
    )

def admin_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Cerca segnali ora", callback_data="admin_scan"),
         InlineKeyboardButton("📋 Segnali",           callback_data="admin_list")],
        [InlineKeyboardButton("📊 Statistiche",       callback_data="admin_stats"),
         InlineKeyboardButton("⚙️ Impostazioni",      callback_data="admin_settings")],
        [InlineKeyboardButton("📡 Quota API",         callback_data="admin_quota")],
    ])

async def _live_oddsapi_quota():
    """The Odds API: /v4/sports è GRATUITO (non consuma crediti) e restituisce
    negli header x-requests-used / x-requests-remaining del TUO account.
    Ritorna (used, remaining) come int, oppure None se non disponibile."""
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.the-odds-api.com/v4/sports/", params={"apiKey": key})
        used = r.headers.get("x-requests-used")
        rem  = r.headers.get("x-requests-remaining")
        if used is None and rem is None:
            return None
        return (int(float(used)) if used is not None else None,
                int(float(rem))  if rem  is not None else None)
    except Exception as e:
        logger.warning(f"Quota live The Odds API fallita: {e}")
        return None


def _find_key(obj, names):
    """Cerca ricorsivamente la prima chiave (case-insensitive) tra `names` in dict/list annidati."""
    names = {n.lower() for n in names}
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in names and isinstance(v, (int, float, str)) and v != "":
                    return v
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(x for x in cur if isinstance(x, (dict, list)))
    return None


async def _live_oddspapi_quota():
    """OddsPapi: legge l'uso reale dell'account (stessi numeri della dashboard,
    es. 85/250). Prova più endpoint e più nomi di campo, e logga la risposta
    grezza se non riesce a interpretarla, così il formato reale è visibile nei log.
    Ritorna dict con count, limit, valid_until (o None)."""
    key = os.environ.get("ODDSPAPI_KEY", "")
    if not key:
        logger.warning("Quota live OddsPapi: ODDSPAPI_KEY non impostata")
        return None
    count_names = {"request_count", "requests_count", "requests_used", "request_used", "used", "usage", "count"}
    limit_names = {"request_limit", "requests_limit", "limit", "quota", "max_requests"}
    until_names = {"valid_until", "validuntil", "expires_at"}
    for path in ("/v4/account", "/v4/account/usage", "/v4/usage"):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"https://api.oddspapi.io{path}", params={"apiKey": key})
            if r.status_code != 200:
                logger.warning(f"Quota live OddsPapi {path}: HTTP {r.status_code} — {r.text[:200]}")
                continue
            data = r.json()
            sub = data
            if isinstance(data, dict) and isinstance(data.get("subscriptions"), list) and data["subscriptions"]:
                subs = data["subscriptions"]
                cur_id = data.get("current_subscription_id")
                sub = next((x for x in subs if x.get("subscription_id") == cur_id), None) \
                      or next((x for x in subs if x.get("is_active")), None) \
                      or subs[0]
            cnt = _find_key(sub, count_names)
            lim = _find_key(sub, limit_names)
            if lim is None:
                logger.warning(f"Quota live OddsPapi {path}: formato non riconosciuto — {str(data)[:400]}")
                continue
            try:
                cnt = int(float(cnt)) if cnt is not None else None
                lim = int(float(lim))
            except (TypeError, ValueError):
                logger.warning(f"Quota live OddsPapi {path}: valori non numerici — {str(data)[:400]}")
                continue
            until = _find_key(sub, until_names)
            db.set_setting("quota_oddspapi_count", cnt if cnt is not None else "")
            db.set_setting("quota_oddspapi_limit", lim)
            db.set_setting("quota_oddspapi_updated_at", datetime.datetime.now(ROME).strftime("%d/%m %H:%M"))
            return {"count": cnt, "limit": lim, "valid_until": until}
        except Exception as e:
            logger.warning(f"Quota live OddsPapi {path} fallita: {e}")
    return None


async def send_quota(fn):
    """Quota API LIVE dai tuoi account: legge i dati reali con le tue chiavi
    (ODDS_API_KEY / ODDSPAPI_KEY) usando endpoint gratuiti che non consumano
    crediti. Se la lettura live fallisce, ripiega sull'ultimo valore salvato."""
    get = db.get_setting
    now = datetime.datetime.now(ROME).strftime("%d/%m %H:%M")

    live_t, live_p = await asyncio.gather(_live_oddsapi_quota(), _live_oddspapi_quota())

    # 🎾 Tennis — The Odds API
    if live_t is not None:
        used, rem = live_t
        tot = (used + rem) if (used is not None and rem is not None) else None
        tennis_block = (
            f"🎾 *Tennis* (The Odds API) — 🟢 live\n"
            f"   Usate: *{used if used is not None else '?'}* / Totale: *{tot if tot is not None else '?'}*\n"
            f"   Rimaste: *{rem if rem is not None else '?'}*\n"
            f"   Aggiornato: {now}"
        )
    else:
        t_used = get("quota_oddsapi_tennis_used")
        t_rem  = get("quota_oddsapi_tennis_remaining")
        t_upd  = get("quota_oddsapi_tennis_updated_at")
        if t_used is not None or t_rem is not None:
            t_used_i = int(t_used) if t_used and t_used.isdigit() else None
            t_rem_i  = int(t_rem)  if t_rem  and t_rem.isdigit()  else None
            t_tot = f"{t_used_i + t_rem_i}" if (t_used_i is not None and t_rem_i is not None) else "?"
            tennis_block = (
                f"🎾 *Tennis* (The Odds API) — ⚪ ultimo dato salvato\n"
                f"   Usate: *{t_used or '?'}* / Totale: *{t_tot}*\n"
                f"   Rimaste: *{t_rem or '?'}*\n"
                f"   Ultimo aggiornamento: {t_upd or '—'}"
            )
        else:
            tennis_block = "🎾 *Tennis* (The Odds API)\n   Nessun dato (chiave ODDS_API_KEY mancante o API non raggiungibile)."

    # 🏓 Ping Pong — OddsPapi
    pp_ours = db.get_api_calls_this_month("oddspapi")
    if live_p is not None and live_p.get("limit") is not None:
        cnt, lim = live_p.get("count"), live_p["limit"]
        rem = (lim - cnt) if isinstance(cnt, (int, float)) else None
        vu  = (live_p.get("valid_until") or "")[:10]
        pp_block = (
            f"🏓 *Ping Pong* (OddsPapi) — 🟢 live\n"
            f"   Usate: *{cnt if cnt is not None else '?'}* / Totale: *{lim}*\n"
            f"   Rimaste: *{rem if rem is not None else '?'}*\n"
            + (f"   Valido fino al: {vu}\n" if vu else "")
            + f"   Aggiornato: {now}"
        )
    else:
        pp_upd = get("quota_oddspapi_updated_at")
        s_cnt, s_lim = get("quota_oddspapi_count"), get("quota_oddspapi_limit")
        saved = f"   Ultimo dato salvato: *{s_cnt}* / *{s_lim}*\n" if (s_cnt and s_lim) else ""
        pp_block = (
            f"🏓 *Ping Pong* (OddsPapi) — ⚪ lettura live non riuscita\n"
            f"{saved}"
            f"   Chiamate fatte dal bot questo mese: *{pp_ours}*\n"
            f"   Ultimo aggiornamento: {pp_upd or '—'}"
        )

    await fn(
        f"📡 *Quota API — dai tuoi account*\n{'━' * 26}\n\n{tennis_block}\n\n{pp_block}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Aggiorna", callback_data="admin_quota")],
            [InlineKeyboardButton("📡 The Odds API", url="https://the-odds-api.com/account/"),
             InlineKeyboardButton("📡 OddsPapi",     url="https://oddspapi.io/us/account")],
            [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
        ])
    )

# ── /start e /menu ──────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🏓 Bot riservato.")
        return
    await update.message.reply_text(
        "🏓 Usa i tasti qui sotto per navigare:",
        reply_markup=PERSISTENT_KB
    )
    await update.message.reply_text(
        admin_panel_text(),
        parse_mode="Markdown",
        reply_markup=admin_panel_kb()
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias /menu — riporta sempre al pannello principale.
    FIX: la tastiera persistente in basso (Scan/Segnali/Stats/Impostazioni/
    Home) viene agganciata da Telegram SOLO quando un messaggio la include
    esplicitamente in reply_markup — prima questo accadeva solo in /start,
    quindi se la tastiera spariva dal client (es. Telegram Desktop dopo un
    riavvio) l'unico modo per farla ricomparire era rifare /start. Ora anche
    /menu la riattacca."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "🏓 Usa i tasti qui sotto per navigare:",
        reply_markup=PERSISTENT_KB
    )
    await update.message.reply_text(
        admin_panel_text(),
        parse_mode="Markdown",
        reply_markup=admin_panel_kb()
    )

# ── Tastiera persistente → gestisce tasti fissi in basso ─────────────────────────
async def kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    txt = update.message.text

    if txt == "🔍 Scan":
        msg = await update.message.reply_text("🔍 Scansione in corso...")
        count = await run_signal_scan(context.application, manual=True)
        if count == -1:
            await msg.edit_text("⏰ Scan disattivato fuori orario — attivo solo tra le 07:00 e le 22:00.")
        else:
            await msg.edit_text(f"✅ Scan completato! Nuovi segnali: *{count}*", parse_mode="Markdown")
        await update.message.reply_text(admin_panel_text(), parse_mode="Markdown", reply_markup=admin_panel_kb())

    elif txt == "📋 Segnali":
        await send_signals_list(update.message.reply_text)

    elif txt == "📊 Stats":
        await send_stats(update.message.reply_text)

    elif txt == "⚙️ Impostazioni":
        await send_settings(update.message.reply_text)

    elif txt == "📡 Quota":
        await send_quota(update.message.reply_text)

    elif txt == "🏠 Home":
        await update.message.reply_text(
            admin_panel_text(), parse_mode="Markdown", reply_markup=admin_panel_kb()
        )

# ── Funzioni pannello ────────────────────────────────────────────────────────────
async def send_signals_list(fn):
    signals = db.get_recent_signals(20)
    if not signals:
        await fn("📭 Nessun segnale ancora.")
        return
    status_icon = {
        "pending":   "🆕",   # nuovo, non ancora aperto
        "seen":      "👁",    # aperto ma senza risultato
        "sent":      "📤",   # inviato al VIP
        "discarded": "🗑",   # scartato
        "won":       "✅",   # vinto
        "lost":      "❌",   # perso
    }
    kb = []
    for s in signals:
        si = status_icon.get(s["status"], "•")
        sport_label = s.get("sport_label") or ""
        # Prende solo l'emoji iniziale dello sport (es. "🏓" da "🏓 Ping Pong")
        sport_icon = sport_label.split(" ")[0] if sport_label else ("🏓" if s.get("sport") == "tabletennis" else "🎾")
        label = f"{si} {sport_icon} {s['kickoff']} | {s['match'][:16]} @{s['odds']}"
        kb.append([InlineKeyboardButton(label, callback_data=f"view_signal_{s['id']}")])
    kb.append([InlineKeyboardButton("🗑 Cancella vecchi segnali", callback_data="confirm_purge")])
    await fn(
        "📋 *Segnali — dal più vicino:*\n\n"
        "🆕 Nuovo  👁 Visto  📤 Inviato VIP\n"
        "✅ Vinto  ❌ Perso  🗑 Scartato",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

def _fmt_balance_block(title: str, bal: dict) -> str:
    if bal["total_bets"] == 0:
        return f"{title}\n   Nessun risultato ancora registrato"
    return (
        f"{title}\n"
        f"   Bilancio: *{bal['current_balance_pct']:+.2f}%* bankroll | "
        f"ROI: *{bal['roi']:+.1f}%*\n"
        f"   {bal['won']}V/{bal['lost']}P (Win {bal['winrate']}%) | "
        f"Migliore: {bal['best_win_pct']:+.2f}% | Peggiore: {bal['worst_loss_pct']:+.2f}%"
    )

async def send_stats(fn):
    stats = db.get_stats()
    bal_tot = db.get_balance_stats()
    bal_t   = db.get_balance_stats(sport="tennis")
    bal_pp  = db.get_balance_stats(sport="tabletennis")

    kb = [
        [InlineKeyboardButton("📈 Grafico bilancio", callback_data="show_balance_chart")],
        [InlineKeyboardButton("🧪 Test stats (solo a me)", callback_data="test_monthly_now")],
        [InlineKeyboardButton(
            f"⏰ Stats automatiche di fine mese: {'🟢 ATTIVE' if _monthly_auto_on() else '🔴 SPENTE'}",
            callback_data="toggle_monthly_auto")],
        [InlineKeyboardButton("📅 Invia stats del mese ai gruppi", callback_data="send_monthly_now")],
        [InlineKeyboardButton("🗑 Reset risultati (mantieni segnali)", callback_data="confirm_reset_stats")],
        [InlineKeyboardButton("💣 Reset COMPLETO (cancella tutto)", callback_data="confirm_purge_all")],
        [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
    ]
    await fn(
        f"📊 *Statistiche*\n"
        f"{'━' * 22}\n"
        f"📨 Totali: *{stats['total']}*\n"
        f"📤 Inviati VIP: *{stats['sent_to_vip']}* | 🆓 Inviati FREE: *{stats['sent_to_free']}*\n"
        f"   🎾 Tennis → VIP {stats['by_sport'].get('tennis', {}).get('sent_vip', 0)} | FREE {stats['by_sport'].get('tennis', {}).get('sent_free', 0)}\n"
        f"   🏓 Ping Pong → VIP {stats['by_sport'].get('tabletennis', {}).get('sent_vip', 0)} | FREE {stats['by_sport'].get('tabletennis', {}).get('sent_free', 0)}\n"
        f"⏳ Pendenti: *{stats['pending']}*\n"
        f"✅ Vinti: *{stats['won']}*\n"
        f"❌ Persi: *{stats['lost']}*\n"
        f"🏆 Win rate: *{stats['winrate']}%*\n\n"
        f"💰 *Bilancio (in % di bankroll, stake {db.get_settings()['stake_pct']}%/segnale)*\n"
        f"{'━' * 22}\n"
        f"{_fmt_balance_block('📊 Totale (entrambi gli sport)', bal_tot)}\n\n"
        f"{_fmt_balance_block('🎾 Tennis', bal_t)}\n\n"
        f"{_fmt_balance_block('🏓 Ping Pong', bal_pp)}\n\n"
        f"🔄 Ultimo scan: *{stats['last_scan']}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

def _conf_label(conf: int) -> str:
    conf_desc = {55: "Bassa (55%)", 60: "Media (60%)", 65: "Media-Alta (65%)",
                 70: "Alta (70%)", 75: "Molto Alta (75%)", 80: "Massima (80%)"}
    return conf_desc.get(conf, f"{conf}%")

# ── Impostazioni: schermata di scelta (sostituisce l'unica lista lunga con ──
# 2 pagine separate, una per sport, più le voci davvero condivise qui sopra.
async def send_settings(fn):
    s = db.get_settings()
    sf = s.get("sport_filter", "both")
    sf_label = {"both": "🏓🎾 Entrambi", "tabletennis": "🏓 Solo Ping Pong", "tennis": "🎾 Solo Tennis"}.get(sf, "🏓🎾 Entrambi")

    kb = [
        [InlineKeyboardButton("🎾 Impostazioni Tennis",     callback_data="settings_tennis")],
        [InlineKeyboardButton("🏓 Impostazioni Ping Pong",  callback_data="settings_pingpong")],
        [InlineKeyboardButton(f"📤 Auto-invio VIP: {'✅ ON' if s['auto_send'] else '❌ OFF'}", callback_data="toggle_autosend")],
        [InlineKeyboardButton(f"💹 Stake per segnale: {s['stake_pct']}% bankroll", callback_data="pick_stake_pct")],
        [InlineKeyboardButton(f"🏅 Sport attivi: {sf_label}", callback_data="pick_sport_filter")],
        [InlineKeyboardButton("📖 Guida impostazioni", callback_data="admin_guide")],
        [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
    ]
    await fn(
        "⚙️ *Impostazioni*\n\n"
        "🎾/🏓 hanno pagine separate per scan e confidenza, specifiche per sport.\n"
        "Le voci qui sotto sono condivise tra i due sport.\n\n"
        "Tocca un'opzione per modificarla:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def send_settings_tennis(fn):
    s = db.get_settings()
    conf_label = _conf_label(s['min_confidence'])
    interval   = s["scan_interval"]
    scan_day   = 15 // interval  # scan tra 07:00 e 22:00 = 15h di finestra
    credits_mo = scan_day * 2 * 31  # ~2 crediti per scan

    dest_label = {"vip": "📤 VIP", "free": "🆓 Free", "both": "📤🆓 Entrambi"}.get(s["send_dest_tennis"], "📤 VIP")
    kb = [
        [InlineKeyboardButton(f"⏱ Scan: ogni {interval}h", callback_data="pick_interval")],
        [InlineKeyboardButton(f"🎯 Confidenza minima: {conf_label}", callback_data="pick_confidence")],
        [InlineKeyboardButton(f"⏰ Anticipo kickoff: {s.get('min_hours_before', 1.0):.0f}h min", callback_data="pick_hours_before")],
        [InlineKeyboardButton(f"📉 Cap edge (no Pinnacle): {s.get('max_edge_no_sharp', 20.0):.0f}%", callback_data="pick_max_edge")],
        [InlineKeyboardButton(f"💎 Value minimo segnale: {s.get('min_value_pct', 3.0):.1f}%", callback_data="pick_value_pct")],
        [InlineKeyboardButton(f"📨 Destinazione invii: {dest_label}", callback_data="pick_dest_tennis")],
        [InlineKeyboardButton(f"🏁 Risultati vinto/perso: {_DEST_NAMES.get(_dest_setting('result', 'tennis'), '📤 VIP')}", callback_data="pick_resdest_tennis")],
        [InlineKeyboardButton(f"📅 Stats di fine mese: {_DEST_NAMES.get(_dest_setting('stats', 'tennis'), '📤 VIP')}", callback_data="pick_statsdest_tennis")],
        [InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")],
    ]
    await fn(
        "🎾 *Impostazioni Tennis*\n"
        f"{'━' * 22}\n\n"
        f"📊 _Crediti The Odds API: ~{credits_mo} req/mese stimati su 500 disponibili_\n\n"
        "Tocca un'opzione per modificarla:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def send_settings_pingpong(fn):
    s = db.get_settings()
    conf_pp_label = _conf_label(s['min_confidence_pp'])
    pp_interval   = s["pingpong_scan_interval"]
    pp_scan_day   = 15 // pp_interval + 1
    pp_calls_mo   = pp_scan_day * 4 * 31

    dest_label = {"vip": "📤 VIP", "free": "🆓 Free", "both": "📤🆓 Entrambi"}.get(s["send_dest_pingpong"], "📤 VIP")
    kb = [
        [InlineKeyboardButton(f"⏱ Scan: ogni {pp_interval}h", callback_data="pick_interval_pp")],
        [InlineKeyboardButton(f"🎯 Confidenza minima: {conf_pp_label}", callback_data="pick_confidence_pp")],
        [InlineKeyboardButton(f"⏰ Anticipo kickoff: {s.get('min_hours_before', 1.0):.0f}h min", callback_data="pick_hours_before")],
        [InlineKeyboardButton(f"💎 Value minimo segnale: {s.get('min_value_pct', 3.0):.1f}%", callback_data="pick_value_pct")],
        [InlineKeyboardButton(f"📨 Destinazione invii: {dest_label}", callback_data="pick_dest_pingpong")],
        [InlineKeyboardButton(f"🏁 Risultati vinto/perso: {_DEST_NAMES.get(_dest_setting('result', 'pingpong'), '📤 VIP')}", callback_data="pick_resdest_pingpong")],
        [InlineKeyboardButton(f"📅 Stats di fine mese: {_DEST_NAMES.get(_dest_setting('stats', 'pingpong'), '📤 VIP')}", callback_data="pick_statsdest_pingpong")],
        [InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")],
    ]
    await fn(
        "🏓 *Impostazioni Ping Pong*\n"
        f"{'━' * 22}\n\n"
        f"📡 _OddsPapi: ~{pp_calls_mo} req/mese su 250 disponibili (quota condivisa col fallback tennis)_\n"
        "ℹ️ Cap edge: fisso 15% (OddsPapi non ha mai un book sharp/Pinnacle per il ping pong)\n\n"
        "Tocca un'opzione per modificarla:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ── Statistiche di fine mese (per sport, ognuna col suo tasto grafico) ──────────
MONTHS_IT = ["", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
_STATS_SPORTS = (("tennis", "🎾 Tennis"), ("tabletennis", "🏓 Ping Pong"))

def _month_balance(sport: str, ym: str) -> dict:
    """Statistiche del solo mese ym (YYYYMM, fuso Roma) per uno sport."""
    stake_pct = db.get_settings().get("stake_pct", 0.5)
    rows = [r for r in db.get_balance_history(sport=sport) if _row_month(r) == ym]
    won  = sum(1 for r in rows if r["result"] == "won")
    lost = sum(1 for r in rows if r["result"] == "lost")
    total = won + lost
    profit = sum(r["profit"] for r in rows)
    staked = total * stake_pct
    return {
        "total_bets": total, "won": won, "lost": lost,
        "winrate": round(won / total * 100, 1) if total else 0,
        "profit_pct": round(profit, 2),
        "roi": round(profit / staked * 100, 1) if staked else 0.0,
        "best": round(max((r["profit"] for r in rows if r["result"] == "won"), default=0), 2),
        "worst": round(min((r["profit"] for r in rows if r["result"] == "lost"), default=0), 2),
    }

async def send_monthly_stats(app: Application, ym: str | None = None, test: bool = False):
    """Invia le statistiche del mese (default: mese corrente), un messaggio
    per sport, ognuno col proprio tasto grafico, ai gruppi scelti per quello
    sport (VIP/Free/Entrambi/Nessuno). Poi riepilogo all'admin.
    test=True: manda i messaggi SOLO all'admin (nessun gruppo), per provarli."""
    now = datetime.datetime.now(ROME)
    ym = ym or now.strftime("%Y%m")
    mese = f"{MONTHS_IT[int(ym[4:])]} {ym[:4]}"
    lines = []
    for sport, label in _STATS_SPORTS:
        st = _month_balance(sport, ym)
        if st["total_bets"] == 0:
            lines.append(f"{label}: nessun risultato nel mese")
            continue
        text = (
            f"📅 *Statistiche {mese} — {label}*\n"
            f"{'━' * 22}\n"
            f"🎯 Segnali conclusi: *{st['total_bets']}* ({st['won']}V / {st['lost']}P)\n"
            f"🏆 Win rate: *{st['winrate']}%*\n"
            f"💰 Bilancio: *{st['profit_pct']:+.2f}%* bankroll | ROI: *{st['roi']:+.1f}%*\n"
            f"⭐ Migliore: {st['best']:+.2f}% | Peggiore: {st['worst']:+.2f}%"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"📈 Grafico {label}", callback_data=f"pubchart_{sport}_{ym}")]])
        if test:
            try:
                await app.bot.send_message(
                    chat_id=ADMIN_ID, text="🧪 *TEST — solo per te, nessun gruppo*\n\n" + text,
                    parse_mode="Markdown", reply_markup=kb,
                )
            except Exception as e:
                logger.error(f"Test stats all'admin fallito: {e}")
            continue
        sent = await send_to_groups(app.bot, sport, "stats", text, kb)
        lines.append(f"{label}: {_sent_note(sport, 'stats', sent)}")
    if test:
        if all("nessun risultato" in l for l in lines) and len(lines) == len(_STATS_SPORTS):
            await app.bot.send_message(chat_id=ADMIN_ID, text=f"🧪 Test stats {mese}: nessun risultato registrato nel mese, niente da mostrare.")
        return
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📅 *Stats {mese}*\n" + "\n".join(lines),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Riepilogo stats mensili all'admin fallito: {e}")

def _monthly_auto_on() -> bool:
    """Invio automatico di fine mese attivo? (default: sì)"""
    return (db.get_setting("monthly_stats_auto", "1") or "1") == "1"

def _stats_dest_summary() -> str:
    """Riga per sport con i gruppi di destinazione delle stats mensili."""
    out = []
    for sport, label in _STATS_SPORTS:
        gids = _dest_group_ids(sport, "stats")
        out.append(f"{label}: " + (", ".join(_group_name(g) for g in gids) if gids else "nessuno"))
    return "\n".join(out)

async def _monthly_stats_job(app: Application):
    """Job schedulato (ultimo giorno del mese): invia solo se l'automatico è attivo."""
    if not _monthly_auto_on():
        logger.info("Stats mensili automatiche: disattivate, non inviate")
        try:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=("⏰ *Stats di fine mese NON inviate*: l'invio automatico è spento.\n"
                      "Puoi mandarle a mano da Statistiche → 📅 Invia stats del mese ai gruppi."),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Avviso stats mensili spente fallito: {e}")
        return
    await send_monthly_stats(app)

_chart_cache: dict = {}   # (sport, ym) -> bytes del PNG
_chart_last_sent: dict = {}  # (chat_id, sport, ym) -> timestamp ultimo invio

async def public_chart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tasto grafico sotto le stats mensili: usabile da CHIUNQUE nel gruppo
    (la callback_handler generale è solo per l'admin)."""
    query = update.callback_query
    try:
        _, sport, ym = query.data.split("_", 2)
    except ValueError:
        await query.answer()
        return
    if sport not in ("tennis", "tabletennis") or len(ym) != 6:
        await query.answer()
        return
    chat_id = query.message.chat_id
    last = _chart_last_sent.get((chat_id, sport, ym), 0)
    if time.time() - last < 600:
        await query.answer("📈 Il grafico è già stato inviato qui sotto da poco.", show_alert=False)
        return
    png = _chart_cache.get((sport, ym))
    if png is None:
        buf = genera_grafico_bilancio(sport, month=ym)
        if buf is None:
            await query.answer("Grafico non disponibile: servono almeno 2 risultati nel mese.", show_alert=True)
            return
        png = buf.getvalue()
        _chart_cache[(sport, ym)] = png
    _chart_last_sent[(chat_id, sport, ym)] = time.time()
    await query.answer()
    label = "🎾 Tennis" if sport == "tennis" else "🏓 Ping Pong"
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=io.BytesIO(png),
        caption=f"📈 Bilancio {label} — {MONTHS_IT[int(ym[4:])]} {ym[:4]}",
    )

# ── CALLBACK HANDLER ─────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        await query.edit_message_text("⛔ Non autorizzato.")
        return
    data = query.data

    # ── Home ─────────────────────────────────────────────────────────────────────
    if data == "admin_home":
        await query.edit_message_text(
            admin_panel_text(), parse_mode="Markdown", reply_markup=admin_panel_kb()
        )

    # ── Guida impostazioni ───────────────────────────────────────────────────────
    if data == "admin_guide":
        guida = (
            "📖 *Guida alle impostazioni*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"

            "⏱ *Scan tennis — ogni quante ore*\n"
            "Controlla nuove partite tennis su The Odds API.\n"
            "Attivo solo tra le 07:00 e le 22:00.\n"
            "• 3h → ~310 crediti/mese ✅ _consigliato_\n"
            "• 2h → ~465 crediti/mese ⚠️\n"
            "• 1h → ~930 crediti/mese ❌\n"
            "Budget disponibile: 500 crediti/mese gratuiti.\n\n"

            "📤 *Auto-invio VIP*\n"
            "Se ON, i segnali vengono inviati automaticamente al gruppo VIP senza approvazione manuale.\n"
            "• OFF → rivedi ogni segnale prima di inviarlo ✅ _consigliato_\n"
            "• ON → invio immediato, meno controllo\n\n"

            "🎯 *Confidenza minima*\n"
            "Soglia sotto cui un segnale viene scartato.\n"
            "• 55% → più segnali, qualità media\n"
            "• 60% → bilanciato ✅ _consigliato_\n"
            "• 65% → meno segnali, più selettivo\n"
            "• 70%+ → pochissimi segnali, solo i migliori\n"
            "Senza Pinnacle la confidenza è cappata a 65% automaticamente.\n\n"

            "🏅 *Filtro sport*\n"
            "• Entrambi → tennis + ping pong ✅ _consigliato_\n"
            "• Solo tennis → ignora ping pong\n"
            "• Solo ping pong → ignora tennis\n\n"

            "💹 *Stake per segnale (% bankroll)*\n"
            "% fissa del bankroll rischiata su ogni segnale (uguale per tutti, "
            "il rating 1-5 resta solo informativo).\n"
            "• 0.5% → conservativo ✅ _consigliato_\n"
            "• 1% → medio\n"
            "• 1.5% → più aggressivo\n"
            "Bilancio e ROI sono sempre mostrati in %, mai in €.\n\n"

            "⏰ *Anticipo minimo kickoff*\n"
            "Scarta segnali troppo vicini all'inizio.\n"
            "• 1h → consigliato ✅\n"
            "• 2h → più conservativo\n"
            "Con 30 min rischi di non trovare la quota in tempo.\n\n"

            "📉 *Cap edge tennis (no Pinnacle)*\n"
            "Limita gli edge gonfiati quando Pinnacle non è disponibile.\n"
            "• 20% → consigliato per tennis ✅\n"
            "• 15% → più severo\n"
            "🏓 Ping pong: fisso a 15% (OddsPapi non ha Pinnacle).\n\n"

            "💎 *Value minimo segnale*\n"
            "Edge minimo (fair value vs quota disponibile) sotto cui una partita viene ignorata.\n"
            "• 2% → molto permissivo (più segnali, meno affidabili)\n"
            "• 3% → bilanciato ✅ _consigliato_\n"
            "• 4-5% → selettivo (pochi segnali, solo i migliori)\n"
            "Se noti giorni interi senza segnali tennis, prova ad abbassarlo.\n\n"

            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🏓 *Ping pong*: scan configurabile come il tennis (07:00-22:00), 3 fixture per scan distribuite nel tempo (non le più vicine, per coprire più ore).\n"
            "Controllo risultati 2 volte/giorno, solo sui giocatori con segnali aperti (max 6 fixture a controllo).\n"
            "Budget OddsPapi: ~150-200 req/mese su 250 disponibili (stima).\n"
            "The Odds API si resetta il 1° del mese. OddsPapi si resetta dalla data di attivazione della chiave (non necessariamente il 1°) — controlla su oddspapi.io/us/account."
        )
        await query.edit_message_text(
            guida,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                # Link diretti ai siti delle due API usate — per controllare
                # quota, fatturazione o rigenerare una chiave senza dover
                # cercare l'URL a memoria.
                [InlineKeyboardButton("📡 The Odds API", url="https://the-odds-api.com/account/"),
                 InlineKeyboardButton("📡 OddsPapi",     url="https://oddspapi.io/us/account")],
                [InlineKeyboardButton("⚙️ Vai alle impostazioni", callback_data="admin_settings")],
                [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
            ])
        )
        return

    # ── Scan ─────────────────────────────────────────────────────────────────────
    elif data == "admin_scan":
        await query.edit_message_text("🔍 Scansione in corso... attendere.")
        count = await run_signal_scan(context.application, manual=True)
        if count == -1:
            text = "⏰ Scan disattivato fuori orario — attivo solo tra le 07:00 e le 22:00."
        else:
            text = f"✅ Scan completato!\n🆕 Nuovi segnali: *{count}*"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Home", callback_data="admin_home")]])
        )

    # ── Lista segnali ─────────────────────────────────────────────────────────────
    elif data == "admin_list":
        await send_signals_list(query.edit_message_text)

    # ── Singolo segnale ──────────────────────────────────────────────────────────
    elif data.startswith("view_signal_"):
        sig_id = int(data.split("_")[-1])
        s = db.get_signal(sig_id)
        if not s:
            await query.edit_message_text("❌ Segnale non trovato.")
            return
        db.update_signal_status(sig_id, "seen")
        kb = list(_send_rows(s))
        kb.append([
            InlineKeyboardButton("✅ Vinto", callback_data=f"result_{sig_id}_won"),
            InlineKeyboardButton("❌ Perso", callback_data=f"result_{sig_id}_lost"),
        ])
        kb.append([InlineKeyboardButton("🚫 Annulla (sospesa)", callback_data=f"result_{sig_id}_void")])
        kb.append([InlineKeyboardButton("🔙 Lista", callback_data="admin_list")])
        await query.edit_message_text(signal_text(s), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ── Invio manuale: VIP / FREE / ENTRAMBI (sendto_*) e vecchio send_vip_* ──
    elif data.startswith(("sendto_", "send_vip_")):
        if data.startswith("sendto_"):
            _, dest, sid = data.split("_", 2)
        else:                               # vecchi messaggi: usa la destinazione impostata
            dest, sid = None, data.split("_")[-1]
        sig_id = int(sid)
        s = db.get_signal(sig_id)
        if not s:
            await query.edit_message_text("❌ Segnale non trovato.")
            return
        sent = await send_signal_to_groups(context.application.bot, s, vip_signal_text(s), dest=dest)
        wanted = {"vip": [VIP_GROUP_ID], "free": [FREE_GROUP_ID], "both": [VIP_GROUP_ID, FREE_GROUP_ID]}.get(
            dest or _dest_setting("send", _sport_short(s.get("sport", "tabletennis"))), [VIP_GROUP_ID])
        failed = [g for g in wanted if g not in sent]
        kb = [[InlineKeyboardButton("📋 Lista", callback_data="admin_list"),
               InlineKeyboardButton("🔙 Home",  callback_data="admin_home")]]
        if not sent:
            await query.edit_message_text(
                f"❌ Invio fallito su: {', '.join(_group_name(g) for g in failed)}.\n"
                f"Controlla che il bot sia nel gruppo (admin o con permesso di scrivere).\n\n{vip_signal_text(s)}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(_send_rows(s) + kb),
            )
            return
        db.mark_signal_sent(sig_id, VIP_GROUP_ID in sent, FREE_GROUP_ID in sent)
        s = db.get_signal(sig_id)
        extra = f"\n⚠️ Non riuscito: {', '.join(_group_name(g) for g in failed)}" if failed else ""
        await query.edit_message_text(
            f"✅ Inviato a: {', '.join(_group_name(g) for g in sent)}{extra}\n\n{vip_signal_text(s)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_send_rows(s) + kb),
        )

    # ── Scarta ───────────────────────────────────────────────────────────────────
    elif data.startswith("discard_"):
        sig_id = int(data.split("_")[-1])
        db.update_signal_status(sig_id, "discarded")
        await query.edit_message_text(
            "🗑 Scartato.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Lista", callback_data="admin_list")]])
        )

    # ── Risultato vinto/perso/annullato ─────────────────────────────────────────
    elif data.startswith("result_"):
        parts  = data.split("_")
        sig_id, result = int(parts[1]), parts[2]
        sig = db.get_signal(sig_id)
        db.update_signal_status(sig_id, result, result)
        # Annullata (partita sospesa/rinviata): niente bilancio, non conta come
        # vinta né persa — result="void" è già escluso da get_stats() perché
        # lì si contano solo result IN ('won','lost').
        if sig and result != "void":
            db.record_balance_entry(sig, result)

        if result == "void":
            emoji, label = "🚫", "Annullata (partita sospesa)"
        else:
            emoji  = "✅" if result == "won" else "❌"
            label  = "VINTO! 🎉" if result == "won" else "Perso."
        kb = [
            [InlineKeyboardButton("📋 Torna alla lista", callback_data="admin_list")],
            [InlineKeyboardButton("🔙 Home",             callback_data="admin_home")],
        ]
        await query.edit_message_text(
            f"{emoji} *{label}*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )
        if sig and result != "void":
            sport_label = sig.get("sport_label", "🏓")
            bal = db.get_balance_stats(sport=sig.get("sport"))
            bal_str = f"{bal['current_balance_pct']:+.2f}% bankroll" if bal["total_bets"] > 0 else "n/d"
            _sp = sig.get("sport", "tabletennis")
            _sent = await send_to_groups(context.application.bot, _sp, "result", _result_group_text(sig, result, bal))
            await query.message.reply_text(
                f"{emoji} *Risultato aggiornato*\n"
                f"{'━' * 20}\n"
                f"{sport_label} {sig['match']}\n"
                f"🎯 {sig['pick']} @ {sig['odds']}\n"
                f"📌 Stake: {sig['stake']}/5\n\n"
                f"{emoji} *{'VINTO!' if result == 'won' else 'Perso.'}*\n\n"
                f"💰 Bilancio {sport_label}: *{bal_str}* | ROI: *{bal['roi']:+.1f}%*\n\n"
                f"{_sent_note(_sp, 'result', _sent)}",
                parse_mode="Markdown"
            )

    # ── Cancella vecchi segnali — conferma ──────────────────────────────────────
    elif data == "confirm_purge":
        kb = [
            [InlineKeyboardButton("⚠️ SÌ, cancella vecchi", callback_data="do_purge")],
            [InlineKeyboardButton("❌ Annulla",              callback_data="admin_list")],
        ]
        await query.edit_message_text(
            "🗑 *Cancella segnali vecchi*\n\n"
            "Verranno eliminati tutti i segnali con risultato (✅/❌) o scartati.\n"
            "I segnali pendenti rimangono.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "do_purge":
        count = db.purge_old_signals()
        await query.edit_message_text(
            f"✅ Eliminati *{count}* segnali vecchi.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Lista", callback_data="admin_list")]])
        )

    # ── Statistiche ──────────────────────────────────────────────────────────────
    elif data == "admin_stats":
        await send_stats(query.edit_message_text)

    elif data == "show_balance_chart":
        bal = db.get_balance_stats()
        if bal["total_bets"] < 2:
            await query.edit_message_text(
                "⚠️ Servono almeno 2 risultati per generare il grafico.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Statistiche", callback_data="admin_stats")]])
            )
            return
        buf = genera_grafico_bilancio()
        kb = [[InlineKeyboardButton("🔙 Statistiche", callback_data="admin_stats")]]
        if buf:
            await query.message.reply_photo(
                photo=buf,
                caption=(
                    f"📊 *Bilancio segnali*\n"
                    f"{'━'*20}\n"
                    f"💵 Attuale: *{bal['current_balance_pct']:+.2f}% bankroll*\n"
                    f"📈 ROI: *{bal['roi']:+.1f}%*\n"
                    f"✅ {bal['won']}V / ❌ {bal['lost']}P | "
                    f"Win%: *{bal['winrate']}%*"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        else:
            await query.message.reply_text(
                "⚠️ Grafico non disponibile (matplotlib non installato).\n"
                "Aggiungi `matplotlib` al requirements.txt.",
                reply_markup=InlineKeyboardMarkup(kb)
            )

    # ── Reset risultati (mantieni segnali) ───────────────────────────────────────
    elif data == "confirm_reset_stats":
        kb = [
            [InlineKeyboardButton("⚠️ SÌ, azzera risultati", callback_data="do_reset_stats")],
            [InlineKeyboardButton("❌ Annulla",               callback_data="admin_stats")],
        ]
        await query.edit_message_text(
            "⚠️ *Reset risultati*\n\n"
            "Vengono azzerati vinti/persi e il bilancio (ora in % bankroll). "
            "I segnali rimangono.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "do_reset_stats":
        db.reset_results()
        await query.edit_message_text(
            "✅ Risultati azzerati. I segnali sono rimasti.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Stats", callback_data="admin_stats")]])
        )

    # ── Reset COMPLETO (cancella tutto) ──────────────────────────────────────────
    elif data == "confirm_purge_all":
        kb = [
            [InlineKeyboardButton("💣 SÌ, cancella TUTTO", callback_data="do_purge_all")],
            [InlineKeyboardButton("❌ Annulla",             callback_data="admin_stats")],
        ]
        await query.edit_message_text(
            "💣 *Reset COMPLETO*\n\n"
            "⚠️ Verranno cancellati TUTTI i segnali e le statistiche.\n"
            "Questa azione è irreversibile!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "do_purge_all":
        count = db.purge_all_signals()
        await query.edit_message_text(
            f"💣 Reset completato. *{count}* segnali eliminati.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Home", callback_data="admin_home")]])
        )

    # ── Impostazioni ─────────────────────────────────────────────────────────────
    elif data == "admin_settings":
        await send_settings(query.edit_message_text)

    elif data == "settings_tennis":
        await send_settings_tennis(query.edit_message_text)

    elif data == "settings_pingpong":
        await send_settings_pingpong(query.edit_message_text)

    elif data == "admin_quota":
        await send_quota(query.edit_message_text)

    elif data == "toggle_autosend":
        db.toggle_setting("auto_send")
        await send_settings(query.edit_message_text)

    # ── Scegli intervallo scan (picker visuale) ───────────────────────────────────
    elif data == "pick_interval":
        current = db.get_settings()["scan_interval"]
        # Finestra attiva 07-22 = 15h → scan_per_giorno = 15 // intervallo
        opts = [1, 2, 3, 4, 6]
        kb = []
        for o in opts:
            scans_day = 15 // o
            credits_mo = scans_day * 2 * 31
            prefix = "✅ " if o == current else ""
            warn = " ⚠️" if credits_mo > 450 else ""
            label = f"{prefix}{o}h — ~{credits_mo} crediti/mese{warn}"
            kb.append([InlineKeyboardButton(label, callback_data=f"set_interval_{o}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "⏱ *Frequenza scan tennis*\n\n"
            "Scan attivi solo tra 07:00 e 22:00.\n"
            "The Odds API: 500 crediti/mese gratuiti.\n"
            f"Attuale: ogni {current}h",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_interval_") and not data.startswith("set_interval_pp_"):
        nxt = int(data.split("_")[-1])
        db.set_setting("scan_interval", nxt)
        _restart_scheduler(context.application, nxt)
        await send_settings(query.edit_message_text)

    # ── Scegli frequenza scan ping pong (stesso meccanismo del tennis) ────────────
    elif data == "pick_interval_pp":
        current = db.get_settings()["pingpong_scan_interval"]
        opts = [24, 12, 8, 6, 4, 3]
        kb = []
        for o in opts:
            scans_day  = 15 // o + 1  # +1: include sempre le 07:00 (07-22 inclusivo)
            # Fixture/scan adattivo (vedi scraper._pingpong_fixtures_per_scan):
            # 3 con 1 scan/giorno, 2 con 2 scan/giorno, 1 con 3+ scan/giorno —
            # così la quota resta sotto controllo anche dividendo gli scan.
            fixtures_per_scan = 3 if scans_day <= 1 else (2 if scans_day == 2 else 1)
            calls_mo   = scans_day * (1 + fixtures_per_scan) * 31  # 1 /fixtures + N /odds per scan
            prefix = "✅ " if o == current else ""
            warn = " ⚠️" if calls_mo > 240 else ""
            label = f"{prefix}{o}h — ~{calls_mo} req/mese{warn}" if o != 24 else f"{prefix}24h (solo 07:00) — ~{calls_mo} req/mese"
            kb.append([InlineKeyboardButton(label, callback_data=f"set_interval_pp_{o}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "🏓 *Frequenza scan ping pong*\n\n"
            "Scan attivi solo tra 07:00 e 22:00 (come il tennis).\n"
            "Quota condivisa con il fallback tennis: OddsPapi 250 richieste/mese gratuite.\n"
            "⚠️ = rischio di esaurire la quota prima di fine mese.\n"
            f"Attuale: ogni {current}h",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_interval_pp_"):
        nxt = int(data.split("_")[-1])
        db.set_setting("pingpong_scan_interval", nxt)
        _restart_pingpong_scheduler(context.application, nxt)
        await send_settings(query.edit_message_text)

    # ── Scegli confidenza (picker visuale con descrizioni) ────────────────────────
    elif data == "pick_confidence":
        current = db.get_settings()["min_confidence"]
        opts = [
            (55, "55% — Bassa\n(più segnali, meno precisi)"),
            (60, "60% — Media\n(bilanciato ✓)"),
            (65, "65% — Media-Alta"),
            (70, "70% — Alta"),
            (75, "75% — Molto Alta"),
            (80, "80% — Massima\n(pochi segnali, più precisi)"),
        ]
        kb = []
        for val, label in opts:
            prefix = "✅ " if val == current else ""
            kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_conf_{val}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "🎯 *Seleziona la confidenza minima dei segnali:*\n\n"
            "Più alta = meno segnali ma più affidabili\n"
            "Più bassa = più segnali ma meno selezionati",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # Nota: il check "set_conf_pp_" deve stare PRIMA di "set_conf_" sotto,
    # perché "set_conf_pp_60".startswith("set_conf_") è vero e finirebbe
    # intercettato dal ramo sbagliato (tennis) se l'ordine fosse invertito.
    elif data.startswith("set_conf_pp_"):
        nxt = int(data.split("_")[-1])
        db.set_setting("min_confidence_pp", nxt)
        await send_settings(query.edit_message_text)

    elif data.startswith("set_conf_"):
        nxt = int(data.split("_")[-1])
        db.set_setting("min_confidence", nxt)
        await send_settings(query.edit_message_text)

    # ── Scegli confidenza ping pong (soglia separata: senza sharp book su ──────
    # OddsPapi la confidenza è tappata a ~70, vedi _confidence in ai_analyzer.py)
    elif data == "pick_confidence_pp":
        current = db.get_settings()["min_confidence_pp"]
        opts = [
            (50, "50% — Bassa\n(più segnali, meno precisi)"),
            (55, "55% — Media-Bassa"),
            (60, "60% — Media\n(bilanciato ✓)"),
            (65, "65% — Media-Alta"),
            (70, "70% — Alta\n(quasi mai raggiunta senza sharp book)"),
        ]
        kb = []
        for val, label in opts:
            prefix = "✅ " if val == current else ""
            kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_conf_pp_{val}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "🏓 *Seleziona la confidenza minima dei segnali ping pong:*\n\n"
            "OddsPapi non ha mai un book sharp (Pinnacle) per il ping pong, quindi "
            "la confidenza dei segnali ping pong resta quasi sempre sotto il 70% "
            "per costruzione — tenerla uguale al tennis significa avere pochissimi "
            "(o nessun) segnale ping pong.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # ── Scegli filtro sport ───────────────────────────────────────────────────
    elif data == "pick_sport_filter":
        current = db.get_settings().get("sport_filter", "both")
        opts = [
            ("both",        "🏓🎾 Entrambi"),
            ("tabletennis", "🏓 Solo Ping Pong"),
            ("tennis",      "🎾 Solo Tennis"),
        ]
        kb = []
        for val, label in opts:
            prefix = "✅ " if val == current else ""
            kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_sport_{val}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "🏅 *Seleziona lo sport per i segnali:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_sport_"):
        val = data.replace("set_sport_", "")
        db.set_setting("sport_filter", val)
        await send_settings(query.edit_message_text)

    # ── Scegli stake per segnale (% di bankroll, flat — stesso valore per ──────
    # ogni segnale indipendentemente dal rating 1-5, che resta solo
    # un'indicazione di confidenza mostrata sul segnale)
    elif data == "pick_stake_pct":
        current = db.get_settings().get("stake_pct", 0.5)
        opts = [0.5, 1.0, 1.5]
        kb = []
        for v in opts:
            prefix = "✅ " if v == current else ""
            kb.append([InlineKeyboardButton(f"{prefix}{v}% bankroll per segnale", callback_data=f"set_stakepct_{v}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "💹 *Stake per segnale (% bankroll)*\n\n"
            "Ogni segnale rischia questa % fissa del tuo bankroll totale, "
            "indipendentemente dal rating 1-5 (quello resta solo un'indicazione "
            "di confidenza mostrata sul segnale).\n\n"
            "_Es. bankroll €1000, stake 0.5% → €5 a segnale. "
            "Bilancio e ROI sono sempre mostrati in % per restare indipendenti "
            "dall'importo esatto del tuo bankroll._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_stakepct_"):
        val = float(data.replace("set_stakepct_", ""))
        db.set_setting("stake_pct", val)
        await send_settings(query.edit_message_text)

    # ── Destinazione invii (VIP/Free/Entrambi), separata per sport ─────────────
    elif data in ("pick_dest_tennis", "pick_dest_pingpong"):
        sport_key = "send_dest_tennis" if data == "pick_dest_tennis" else "send_dest_pingpong"
        back_cb   = "settings_tennis" if data == "pick_dest_tennis" else "settings_pingpong"
        current = db.get_settings()[sport_key]
        opts = [("vip", "📤 Solo VIP"), ("free", "🆓 Solo Free"), ("both", "📤🆓 Entrambi")]
        kb = []
        for val, label in opts:
            prefix = "✅ " if val == current else ""
            kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_{sport_key}_{val}")])
        kb.append([InlineKeyboardButton("🔙 Indietro", callback_data=back_cb)])
        sport_name = "Tennis" if data == "pick_dest_tennis" else "Ping Pong"
        await query.edit_message_text(
            f"📨 *Destinazione invii — {sport_name}*\n\n"
            "Dove inviare i segnali per questo sport.\n"
            "(Risultati e statistiche mensili hanno la loro scelta nelle impostazioni dello sport.)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_send_dest_tennis_"):
        val = data.replace("set_send_dest_tennis_", "")
        db.set_setting("send_dest_tennis", val)
        await send_settings_tennis(query.edit_message_text)

    elif data.startswith("set_send_dest_pingpong_"):
        val = data.replace("set_send_dest_pingpong_", "")
        db.set_setting("send_dest_pingpong", val)
        await send_settings_pingpong(query.edit_message_text)


    # ── Destinazione risultati vinto/perso e stats di fine mese (per sport) ─────
    elif data.startswith(("pick_resdest_", "pick_statsdest_")):
        kind  = "result" if data.startswith("pick_resdest_") else "stats"
        short = data.rsplit("_", 1)[1]            # tennis | pingpong
        current = _dest_setting(kind, short)
        kb = []
        for val in ("vip", "free", "both", "none"):
            prefix = "✅ " if val == current else ""
            kb.append([InlineKeyboardButton(f"{prefix}{_DEST_NAMES[val]}", callback_data=f"set_{kind}dest_{short}_{val}")])
        kb.append([InlineKeyboardButton("🔙 Indietro", callback_data=f"settings_{short}")])
        sport_name = "Tennis" if short == "tennis" else "Ping Pong"
        what = ("i messaggi Vinto/Perso (automatici e manuali)" if kind == "result"
                else "le statistiche di fine mese (con il tasto del grafico)")
        await query.edit_message_text(
            f"📨 *Destinazione — {sport_name}*\n\nDove inviare {what} per questo sport:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith(("set_resultdest_", "set_statsdest_")):
        _, kindname, short, val = data.split("_", 3)
        kind = "result" if kindname == "resultdest" else "stats"
        db.set_setting(f"{kind}_dest_{short}", val)
        if short == "tennis":
            await send_settings_tennis(query.edit_message_text)
        else:
            await send_settings_pingpong(query.edit_message_text)

    elif data == "test_monthly_now":
        await send_monthly_stats(context.application, test=True)

    elif data == "send_monthly_now":
        now_m = datetime.datetime.now(ROME)
        mese = f"{MONTHS_IT[now_m.month]} {now_m.year}"
        await query.edit_message_text(
            f"⚠️ *Confermi l'invio ai gruppi?*\n\n"
            f"Stai per mandare le statistiche di *{mese}* a:\n{_stats_dest_summary()}\n\n"
            f"I messaggi saranno visibili ai membri dei gruppi.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Sì, invia ai gruppi", callback_data="send_monthly_confirm")],
                [InlineKeyboardButton("❌ Annulla", callback_data="admin_stats")],
            ])
        )

    elif data == "send_monthly_confirm":
        # Toglie subito i bottoni, così un doppio tocco non invia due volte.
        await query.edit_message_text("⏳ Invio delle statistiche ai gruppi in corso…")
        await send_monthly_stats(context.application)

    elif data == "toggle_monthly_auto":
        db.set_setting("monthly_stats_auto", "0" if _monthly_auto_on() else "1")
        await send_stats(query.edit_message_text)

    # ── Anticipo minimo kickoff ───────────────────────────────────────────────
    elif data == "pick_hours_before":
        current = db.get_settings().get("min_hours_before", 1.0)
        opts = [
            (0.5, "30 min — accetta segnali dell'ultima ora"),
            (1.0, "1h — consigliato ✓"),
            (2.0, "2h — più selettivo"),
            (3.0, "3h — solo largo anticipo"),
        ]
        kb = []
        for val, label in opts:
            prefix = "✅ " if abs(float(val) - float(current)) < 0.01 else ""
            kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_hours_{val}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "⏰ *Anticipo minimo al kickoff*\n\n"
            "Scarta segnali troppo vicini all'inizio partita.\n"
            "Con 1h non ricevi segnali su partite che iniziano tra meno di 60 min.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_hours_"):
        val = float(data.replace("set_hours_", ""))
        db.set_setting("min_hours_before", val)
        await send_settings(query.edit_message_text)

    # ── Cap edge senza Pinnacle ───────────────────────────────────────────────
    elif data == "pick_max_edge":
        current = db.get_settings().get("max_edge_no_sharp", 20.0)
        opts = [
            (10.0, "10% — molto severo (pochi segnali)"),
            (15.0, "15% — severo"),
            (20.0, "20% — bilanciato ✓"),
            (25.0, "25% — permissivo (più segnali)"),
        ]
        kb = []
        for val, label in opts:
            prefix = "✅ " if abs(float(val) - float(current)) < 0.01 else ""
            kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_maxedge_{val}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "📉 *Cap edge tennis senza Pinnacle*\n\n"
            "Quando Pinnacle non è disponibile il de-vig è meno preciso.\n"
            "Questo limita gli edge gonfiati per il *tennis*.\n\n"
            "🏓 Ping pong: fisso a 15% (OddsPapi non ha Pinnacle)\n"
            "✅ 20% è il valore consigliato per il tennis.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_maxedge_"):
        val = float(data.replace("set_maxedge_", ""))
        db.set_setting("max_edge_no_sharp", val)
        await send_settings(query.edit_message_text)

    # ── Value minimo segnale ─────────────────────────────────────────────────
    elif data == "pick_value_pct":
        current = db.get_settings().get("min_value_pct", 3.0)
        opts = [
            (2.0, "2% — permissivo (più segnali)"),
            (3.0, "3% — bilanciato ✓"),
            (4.0, "4% — selettivo"),
            (5.0, "5% — molto selettivo (pochi segnali)"),
        ]
        kb = []
        for val, label in opts:
            prefix = "✅ " if abs(float(val) - float(current)) < 0.01 else ""
            kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_valuepct_{val}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "💎 *Value minimo per generare un segnale*\n\n"
            "Edge minimo tra quota fair (de-vig) e quota disponibile.\n"
            "Più basso = più segnali ma mediamente meno marcati.\n"
            "Più alto = pochi segnali ma solo i più netti.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_valuepct_"):
        val = float(data.replace("set_valuepct_", ""))
        db.set_setting("min_value_pct", val)
        await send_settings(query.edit_message_text)


# ── Scheduler ────────────────────────────────────────────────────────────────────
_scheduler: AsyncIOScheduler | None = None
_main_loop: asyncio.AbstractEventLoop | None = None  # loop principale dell'Application, catturato in post_init

def _schedule_coro(coro_factory):
    """Esegue una coroutine sul loop principale dell'Application da un thread esterno."""
    if _main_loop is None:
        logger.error("Scheduler: main loop non disponibile, job saltato")
        return
    async def _wrapper():
        try:
            await coro_factory()
        except Exception as exc:
            logger.error(f"Scheduler job errore: {exc}", exc_info=True)
    asyncio.run_coroutine_threadsafe(_wrapper(), _main_loop)

def _restart_scheduler(app: Application, hours: int):
    global _scheduler
    if _scheduler:
        from apscheduler.triggers.cron import CronTrigger
        _scheduler.add_job(
            lambda: _schedule_coro(lambda: run_signal_scan(app)),
            trigger=CronTrigger(hour=_daytime_hours(hours), minute=0, timezone=ROME),
            id="signal_scan",
            replace_existing=True,
        )
        logger.info(f"⏰ Scheduler tennis aggiornato: 07-22h ogni {hours}h ({_daytime_hours(hours)})")

def _restart_pingpong_scheduler(app: Application, hours: int):
    global _scheduler
    if _scheduler:
        from apscheduler.triggers.cron import CronTrigger
        _scheduler.add_job(
            lambda: _schedule_coro(lambda: run_pingpong_scan(app)),
            trigger=CronTrigger(hour=",".join(map(str, pingpong_scan_hours(hours))), minute=0, timezone=ROME),
            id="pingpong_scan",
            replace_existing=True,
        )
        logger.info(f"⏰ Scheduler ping pong aggiornato: ore {pingpong_scan_hours(hours)}")

def _daytime_hours(interval: int, start: int = 7, end: int = 22) -> str:
    """Genera la lista di ore (per CronTrigger) da "start" a "end" distanziate
    di "interval" ore — es. interval=3 → "7,10,13,16,19,22". Usata sia per il
    tennis che per il ping pong: entrambi partono alle 07:00 e non superano le
    22:00, ognuno col proprio intervallo impostato dal pannello."""
    if interval <= 0:
        interval = 24
    hours = []
    h = start
    while h <= end:
        hours.append(h)
        h += interval
    return ",".join(str(x) for x in hours)


async def post_init(app: Application):
    global _scheduler, _main_loop
    settings   = db.get_settings()
    hours      = settings["scan_interval"]
    pp_hours   = settings["pingpong_scan_interval"]
    now        = datetime.datetime.now(ROME)
    _main_loop = asyncio.get_running_loop()   # loop principale, usato dai job per agganciarsi correttamente

    _scheduler = AsyncIOScheduler(timezone=ROME)
    from apscheduler.triggers.cron import CronTrigger

    # Scan tennis: parte alle 07:00 e ripete ogni "hours" ore fino alle 22:00
    # (FIX: prima era un IntervalTrigger che partiva subito al boot e girava
    # ogni N ore da quel momento, non ancorato alle 07:00 — la funzione dello
    # scan si limitava a fare un no-op silenzioso se chiamata fuori 07-22,
    # sprecando comunque il trigger. Ora è un CronTrigger con orari fissi,
    # quindi parte sempre puntuale alle 07:00 e non spreca invocazioni a vuoto.)
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: run_signal_scan(app)),
        trigger=CronTrigger(hour=_daytime_hours(hours), minute=0, timezone=ROME),
        id="signal_scan",
    )

    # Auto-risultati tennis: ogni 60 minuti (ridotto da 30 — insieme al filtro
    # per tornei con segnali pendenti in scraper.py, taglia drasticamente il
    # consumo di crediti The Odds API che si stava esaurendo in 2-3 giorni)
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: _results_job(app, "tennis")),
        trigger=IntervalTrigger(minutes=60, timezone=ROME),
        id="auto_results_tennis",
        next_run_time=now + datetime.timedelta(minutes=10),
    )

    # Auto-risultati ping pong: solo 2 volte al giorno (OddsPapi, 250
    # richieste/mese — un controllo ogni 30 min esaurirebbe la quota in
    # pochi giorni e bloccherebbe anche la ricerca di nuove partite)
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: _results_job(app, "tabletennis")),
        trigger=CronTrigger(hour="14,22", minute=0, timezone=ROME),
        id="auto_results_pingpong",
    )

    # Scan ping pong: stesso schema del tennis — 07:00 → 22:00, ogni "pp_hours"
    # ore, configurabile dal pannello come per il tennis (era fisso solo 07:00).
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: run_pingpong_scan(app)),
        trigger=CronTrigger(hour=",".join(map(str, pingpong_scan_hours(pp_hours))), minute=0, timezone=ROME),
        id="pingpong_scan",
    )

    # ── Recupero scan al boot ─────────────────────────────────────────────────
    # Se il bot riparte dopo le 07:00 (es. dopo un redeploy da GitHub) e lo
    # scan di oggi non è ancora partito, il CronTrigger aspetterebbe il
    # prossimo orario utile in lista. Qui recuperiamo subito il primo scan
    # mancato della giornata, per entrambi gli sport.
    today_str = now.strftime("%Y-%m-%d")
    if now.hour >= 7:
        last_pp_row = db.conn.execute(
            "SELECT value FROM settings WHERE key='last_pingpong_scan_date'"
        ).fetchone()
        if (last_pp_row["value"] if last_pp_row else "") != today_str:
            logger.info(f"🏓 Scan ping pong di oggi ({today_str}) non ancora eseguito — recupero al boot")
            _scheduler.add_job(
                lambda: _schedule_coro(lambda: run_pingpong_scan(app)),
                id="pingpong_scan_catchup",
                next_run_time=now + datetime.timedelta(seconds=20),
            )
        last_t_row = db.conn.execute(
            "SELECT value FROM settings WHERE key='last_tennis_scan_date'"
        ).fetchone()
        if (last_t_row["value"] if last_t_row else "") != today_str:
            logger.info(f"🎾 Scan tennis di oggi ({today_str}) non ancora eseguito — recupero al boot")
            _scheduler.add_job(
                lambda: _schedule_coro(lambda: run_signal_scan(app)),
                id="signal_scan_catchup",
                next_run_time=now + datetime.timedelta(seconds=35),
            )

    # ── Reset automatico intervallo scan a inizio mese ───────────────────────
    # Quando The Odds API va a quota esaurita si abbassa la frequenza dello
    # scan tennis per allungare la vita della quota OddsPapi condivisa col
    # ping pong. Il 1° del mese si resetta anche The Odds API — questo job
    # riporta da solo scan_interval al valore "normale" quel giorno, senza
    # doverselo ricordare a mano ogni volta.
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: _monthly_reset_scan_interval(app)),
        trigger=CronTrigger(day=1, hour=0, minute=5, timezone=ROME),
        id="monthly_reset_scan_interval",
    )

    # Statistiche di fine mese ai gruppi (per sport, con tasto grafico):
    # ultimo giorno del mese alle 22:30 ora di Roma.
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: _monthly_stats_job(app)),
        trigger=CronTrigger(day="last", hour=22, minute=30, timezone=ROME),
        id="monthly_stats",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )

    _scheduler.start()
    logger.info(
        f"⏰ Scheduler avviato — scan tennis 07-22h ogni {hours}h | "
        f"ping pong 07-22h ogni {pp_hours}h | risultati ogni 60min"
    )

# Valore a cui torna scan_interval il 1° di ogni mese (vedi _monthly_reset_scan_interval)
NORMAL_SCAN_INTERVAL_HOURS = 3

async def _monthly_reset_scan_interval(app: Application):
    current = db.get_settings()["scan_interval"]
    if current == NORMAL_SCAN_INTERVAL_HOURS:
        return
    db.set_setting("scan_interval", NORMAL_SCAN_INTERVAL_HOURS)
    _restart_scheduler(app, NORMAL_SCAN_INTERVAL_HOURS)
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🔄 *Inizio mese*: scan tennis riportato a ogni "
                f"*{NORMAL_SCAN_INTERVAL_HOURS}h* (era {current}h) — "
                f"quota The Odds API e OddsPapi ripartite da zero."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Notifica reset mensile fallita: {e}")

# ── Core scan ────────────────────────────────────────────────────────────────────
async def run_signal_scan(app: Application, manual: bool = False, sport_override: str | None = None) -> int:
    """
    sport_override: forza lo scan su un solo sport ("tabletennis" per il job
    ping pong), bypassando la finestra oraria 07-22 e il filtro sport_filter
    dell'utente. Passato come parametro esplicito (non più come attributo
    condiviso sulla funzione) per evitare race condition tra scan concorrenti
    — es. uno scan tennis automatico che parte mentre uno scan ping pong è
    ancora in corso rischiava di "ereditare" l'override sbagliato e girare
    silenziosamente come ping pong invece che come tennis.
    """
    ora = datetime.datetime.now(ROME).hour
    sport_ov = sport_override
    # Finestra 07-22: vale SEMPRE, anche per lo scan manuale dal pannello.
    # FIX: prima "manual" bypassava del tutto il controllo orario, quindi
    # premendo "🔍 Scan" di notte si generavano comunque segnali fuori
    # orario (es. le 00:12) — oltre al rischio di beccare quote notturne
    # meno liquide/affidabili. Ora lo scan manuale fuori 07-22 viene
    # rifiutato come quello automatico, con un messaggio dedicato invece
    # di restituire silenziosamente "0 segnali trovati".
    if not sport_ov and not (7 <= ora <= 21):
        if manual:
            logger.info(f"Scan manuale rifiutato (ora {ora}:xx fuori finestra 07-22)")
            return -1
        logger.info(f"Scan tennis saltato (ora {ora}:xx fuori finestra 07-22)")
        return 0
    logger.info("🔍 Avvio scan tennis..." if not sport_ov else "🏓 Avvio scan ping pong...")
    db.set_setting("last_scan", now_it_str())
    if not sport_ov:
        db.set_setting("last_tennis_scan_date", datetime.datetime.now(ROME).strftime("%Y-%m-%d"))

    # Settings e filtro sport PRIMA della fetch: evita di interrogare OddsPapi
    # (ping pong) quando serve solo il tennis, e viceversa — prima veniva
    # sempre fetchato tutto e scartato dopo, bruciando la quota OddsPapi
    # (250 richieste/mese) ad ogni scan automatico tennis.
    settings = db.get_settings()
    sport_filter = sport_ov or settings.get("sport_filter", "both")
    if sport_filter == "both" and not manual:
        # Scan automatico orario: solo tennis, per non consumare la quota
        # OddsPapi. Il ping pong ha il suo job dedicato giornaliero.
        fetch_sport = "tennis"
    else:
        fetch_sport = sport_filter

    try:
        matches = await scraper.fetch_matches(sport=fetch_sport)
    except Exception as e:
        logger.error(f"Errore scraping: {e}")
        matches = scraper.get_fallback_matches()

    # Log fonti e sport
    sources = set(m.get("source", "?") for m in matches)
    sports  = set(m.get("sport", "?") for m in matches)
    logger.info(f"Fonti: {sources} | Sport: {sports} | Partite: {len(matches)}")

    # ── Avviso quota esaurita/ripristinata (una volta sola per cambio stato) ──
    async def _notify_quota_change(api_name: str, sport_label: str, setting_key: str, quota_ok: bool):
        prev = db.get_setting(setting_key, "1") == "1"
        if quota_ok != prev:
            db.set_setting(setting_key, "1" if quota_ok else "0")
            if quota_ok:
                text = f"✅ *{sport_label}*: quota {api_name} di nuovo disponibile — segnali reali riattivati."
            else:
                text = (
                    f"⚠️ *{sport_label}*: quota {api_name} esaurita.\n"
                    f"Niente nuovi segnali reali finché non si resetta. Ti avviso appena torna disponibile."
                )
            try:
                # FIX: riattacca la tastiera persistente anche qui — questi
                # avvisi automatici arrivano senza che l'admin abbia toccato
                # nulla, quindi sono un buon punto per tenerla "viva" nel
                # client senza dover rifare /start.
                await app.bot.send_message(
                    chat_id=ADMIN_ID, text=text, parse_mode="Markdown",
                    reply_markup=PERSISTENT_KB,
                )
            except Exception as e:
                logger.error(f"Notifica quota {api_name} fallita: {e}")

    await _notify_quota_change("The Odds API", "🎾 Tennis", "tennis_quota_ok", scraper.tennis_quota_ok)
    await _notify_quota_change("OddsPapi", "🏓 Ping Pong", "pingpong_quota_ok", scraper.pingpong_quota_ok)

    # Filtra per sport (safety net: con fetch_sport mirato i match sono già
    # del sport giusto, tranne nel caso manuale "both" dove restano entrambi)
    if sport_filter == "both":
        if manual:
            # Scan manuale: controlla entrambi gli sport, l'utente ha premuto
            # apposta il pulsante e si aspetta di vedere anche il ping pong.
            logger.info(f"Scan manuale (entrambi gli sport): {len(matches)} partite")
        else:
            matches = [m for m in matches if m.get("sport") == "tennis"]
            logger.info(f"Scan tennis: {len(matches)} partite")
    else:
        matches = [m for m in matches if m.get("sport") == sport_filter]
        logger.info(f"Filtro sport '{sport_filter}': {len(matches)} partite rimaste")
    # Se nessuna partita reale avvisa l'admin (solo in orario diurno 07-23 per non spammare)
    real_matches = [m for m in matches if m.get("source") not in ("fallback",)]
    quota_is_the_reason = not (scraper.tennis_quota_ok and scraper.pingpong_quota_ok)
    if not real_matches and (ODDS_KEY or os.environ.get("ODDSPAPI_KEY")):
        logger.info("Nessuna partita reale disponibile in questo momento")
        # Il messaggio generico va mandato solo se il motivo NON è la quota esaurita
        # (in quel caso l'admin è già stato avvisato da _notify_quota_change sopra).
        if not quota_is_the_reason:
            ora = datetime.datetime.now(ROME).hour
            if 7 <= ora <= 23:
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "ℹ️ *Nessuna partita disponibile*\n\n"
                        "Le API non hanno partite quotate al momento.\n"
                        "Il prossimo scan automatico riproverà tra poco."
                    ),
                    parse_mode="Markdown",
                    reply_markup=PERSISTENT_KB,
                )
        return 0

    new_signals = 0

    for match in matches:
        try:
            signals = await analyzer.analyze(match, settings)
            signals.sort(key=lambda x: x["value_pct"], reverse=True)
            for sig in signals:
                # Soglia separata per sport: senza sharp book il ping pong è
                # tappato a ~70% di confidenza per costruzione (vedi
                # _confidence in ai_analyzer.py), quindi usa min_confidence_pp
                # invece della soglia tennis, altrimenti non passa quasi mai.
                min_conf = (
                    settings["min_confidence_pp"] if sig.get("sport") == "tabletennis"
                    else settings["min_confidence"]
                )
                if sig["confidence"] < min_conf:
                    logger.info(
                        f"Segnale scartato (confidenza {sig['confidence']}% < min {min_conf}%): "
                        f"{sig.get('match','?')} — {sig.get('pick','?')}"
                    )
                    continue
                if db.signal_exists(sig["match_key"]):
                    continue
                sig_id = db.save_signal(sig)
                new_signals += 1

                sport_label = sig.get("sport_label", "🏓 Ping Pong")
                kb = _send_rows({"id": sig_id, "status": "pending"}) + [[
                    InlineKeyboardButton("✅ Vinto", callback_data=f"result_{sig_id}_won"),
                    InlineKeyboardButton("❌ Perso", callback_data=f"result_{sig_id}_lost"),
                ],[
                    InlineKeyboardButton("🚫 Annulla (sospesa)", callback_data=f"result_{sig_id}_void"),
                ]]
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🆕 *Nuovo segnale {sport_label}!*\n\n{signal_text(sig)}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                if settings["auto_send"]:
                    sent_g = await send_signal_to_groups(app.bot, sig, vip_signal_text(sig))
                    if sent_g:
                        db.mark_signal_sent(sig_id, VIP_GROUP_ID in sent_g, FREE_GROUP_ID in sent_g)

        except Exception as e:
            logger.error(f"Errore analisi {match.get('name','?')}: {e}")

    logger.info(f"✅ {new_signals} nuovi segnali")

    # Ping leggero solo per gli scan AUTOMATICI (cron) a 0 segnali: prima
    # restavano silenziosi se non trovavano nulla, e non c'era modo di
    # distinguere "il bot non gira più" da "ha girato, nessun segnale
    # valido". Lo scan manuale non ne ha bisogno: il bottone "Scan" mostra
    # già il risultato in chat.
    if not manual and new_signals == 0:
        sport_ping_label = "🏓 Ping Pong" if sport_override == "tabletennis" else "🎾 Tennis"
        try:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"✅ Scan concluso ({sport_ping_label}): 0 segnali",
            )
        except Exception as e:
            logger.debug(f"Impossibile inviare ping scan-vuoto: {e}")

    return new_signals

# ── Auto-aggiornamento risultati ─────────────────────────────────────────────────
PINGPONG_MAX_RETRIES = 2   # tentativi extra se lo scan mattutino fallisce
PINGPONG_RETRY_DELAY_MIN = 45

async def run_pingpong_scan(app: Application, is_retry: bool = False):
    """
    Scan ping pong — parte alle 07:00 (CronTrigger) e si ripete ogni
    "pingpong_scan_interval" ore fino alle 22:00 (1 volta al giorno col
    default 24h, di più se l'intervallo viene abbassato dal pannello).
    Se il bot si riavvia dopo le 07:00 (es. dopo un redeploy) e lo scan di
    oggi non è ancora partito, post_init lo recupera automaticamente al boot
    (vedi "catchup" più sotto).
    Se una chiamata fallisce (errore rete/API), riprova automaticamente dopo
    ~45 minuti, fino a PINGPONG_MAX_RETRIES tentativi extra PER QUELLA chiamata.

    FIX: prima "attempts_today" contava insieme i retry veri E le chiamate
    legittime successive della giornata (quando pingpong_scan_interval < 24h,
    es. ogni 6h = più scan/giorno via CronTrigger). Risultato: se una delle
    scan regolari falliva per un 429 transitorio, il contatore saliva e dopo
    2-3 chiamate il bot pensava di aver "esaurito i tentativi di oggi",
    saltando scan successive già programmate che non erano affatto retry.
    Ora il parametro esspito `is_retry` (passato solo dal job di retry
    schedulato qui sotto) distingue i due casi: una chiamata regolare dal
    CronTrigger (o dal catchup al boot) riparte sempre con un budget di
    retry pulito, indipendentemente da quante scan sono già girate oggi.
    """
    settings = db.get_settings()
    if settings.get("sport_filter") == "tennis":
        logger.info("Ping pong scan saltato (sport_filter=tennis)")
        return

    today_str = datetime.datetime.now(ROME).strftime("%Y-%m-%d")

    # Conta i tentativi di retry della chiamata CORRENTE (reset ad ogni
    # chiamata regolare, non solo al cambio data — vedi FIX sopra).
    retry_key = "pingpong_scan_attempts_date"
    count_key = "pingpong_scan_attempts_count"
    if is_retry:
        last_attempt_date = db.conn.execute(
            "SELECT value FROM settings WHERE key=?", (retry_key,)
        ).fetchone()
        last_attempt_date = last_attempt_date["value"] if last_attempt_date else ""
        if last_attempt_date != today_str:
            attempts_today = 0
            db.set_setting(retry_key, today_str)
        else:
            row = db.conn.execute("SELECT value FROM settings WHERE key=?", (count_key,)).fetchone()
            attempts_today = int(row["value"]) if row and row["value"] else 0
    else:
        # Chiamata regolare (CronTrigger o catchup al boot): non è un retry,
        # budget di tentativi pulito per questa chiamata.
        attempts_today = 0
        db.set_setting(retry_key, today_str)

    logger.info(f"🏓 Avvio scan ping pong (tentativo {attempts_today + 1}{', retry' if is_retry else ''})...")

    ok = False
    try:
        await run_signal_scan(app, sport_override="tabletennis")
        # OddsPapi non lancia eccezioni sui suoi errori (429/timeout): li logga
        # e basta, quindi "nessuna eccezione" non vuol dire "dati reali ottenuti".
        # Controlliamo esplicitamente lo stato quota per non segnare il giorno
        # come completato quando in realtà OddsPapi ha risposto 429.
        ok = scraper.pingpong_quota_ok
        if not ok:
            logger.warning("🏓 Scan ping pong: quota OddsPapi esaurita (429) — non segnato come completato, riprovo")
    except Exception as e:
        logger.error(f"🏓 Scan ping pong fallito: {e}", exc_info=True)

    attempts_today += 1
    db.set_setting(count_key, str(attempts_today))

    if ok:
        # Segna la data di oggi come "già scansionata" — evita che il recupero
        # al boot o un retry pendente rilancino lo scan più volte nello stesso giorno
        db.set_setting("last_pingpong_scan_date", today_str)
        logger.info(f"🏓 Scan ping pong completato — segnato come eseguito per {today_str}")
        return

    if attempts_today <= PINGPONG_MAX_RETRIES:
        retry_time = datetime.datetime.now(ROME) + datetime.timedelta(minutes=PINGPONG_RETRY_DELAY_MIN)
        logger.warning(
            f"🏓 Scan ping pong: riprovo alle {retry_time.strftime('%H:%M')} "
            f"(tentativo {attempts_today + 1}/{PINGPONG_MAX_RETRIES + 1})"
        )
        _scheduler.add_job(
            lambda: _schedule_coro(lambda: run_pingpong_scan(app, is_retry=True)),
            id="pingpong_scan_retry",
            replace_existing=True,
            next_run_time=retry_time,
        )
        try:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"⚠️ *Scan ping pong fallito* (tentativo {attempts_today}/"
                    f"{PINGPONG_MAX_RETRIES + 1}) — riprovo alle {retry_time.strftime('%H:%M')}."
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass
    else:
        # Solo la catena di retry di QUESTA chiamata si ferma — se è
        # schedulata un'altra scan regolare più tardi in giornata
        # (pingpong_scan_interval < 24h) partirà comunque, con un budget di
        # retry pulito (vedi FIX nel docstring della funzione).
        logger.error("🏓 Scan ping pong: esauriti i retry per questa chiamata")
        try:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text="❌ *Scan ping pong*: falliti tutti i retry per questa chiamata.",
                parse_mode="Markdown",
            )
        except Exception:
            pass



# ── Promemoria "risultato mancante" ──────────────────────────────────────────────
# Dopo quante ore dal kickoff, se il segnale è ancora senza risultato, ti avviso
# (una sola volta per segnale). Gli over/under non si aggiornano mai da soli.
REMIND_AFTER_H = {"tennis": 6, "tabletennis": 3}
REMIND_AFTER_H_TOTALS = 3

def _hours_since_kickoff(sig: dict) -> float | None:
    """Ore trascorse dal kickoff del segnale (None se non leggibile)."""
    ko_str = (sig.get("kickoff") or "").strip()
    if not ko_str:
        return None
    now = datetime.datetime.now(ROME)
    try:
        try:
            ko = datetime.datetime.fromisoformat(ko_str)
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=ROME)
        except ValueError:
            ko = datetime.datetime.strptime(f"{now.year}/{ko_str}", "%Y/%d/%m %H:%M").replace(tzinfo=ROME)
            if ko - now > datetime.timedelta(days=180):
                ko = ko.replace(year=ko.year - 1)
    except Exception:
        return None
    return (now - ko).total_seconds() / 3600

async def remind_pending_results(app: Application, sport: str):
    """Avvisa l'admin dei segnali inviati/visti che non hanno ancora un
    risultato dopo REMIND_AFTER_H ore dal kickoff, con i tasti Vinto/Perso."""
    due = []
    for sig in db.get_signals_for_auto_result():
        if sig.get("sport") != sport or sig.get("status") not in ("sent", "seen"):
            continue
        if sig.get("result_reminded"):
            continue
        age = _hours_since_kickoff(sig)
        if age is None:
            continue
        limit = REMIND_AFTER_H.get(sport, 4) if sig.get("signal_type") == "winner" else REMIND_AFTER_H_TOTALS
        if age >= limit:
            due.append(sig)
    if not due:
        return
    for i in range(0, len(due), 8):
        chunk = due[i:i + 8]
        lines, kb = [], []
        for sig in chunk:
            lines.append(f"• {sig.get('sport_label', '')} {sig['match']} — {sig['pick']} @ {sig['odds']} (inizio {sig.get('kickoff', '?')})")
            short = (sig["match"] or "")[:14]
            kb.append([
                InlineKeyboardButton(f"✅ {short}", callback_data=f"result_{sig['id']}_won"),
                InlineKeyboardButton(f"❌ {short}", callback_data=f"result_{sig['id']}_lost"),
            ])
        try:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=("⏰ *Risultato mancante*\n"
                      "Questi segnali non hanno ancora un risultato. Gli over/under si segnano sempre a mano; "
                      "per i vincente il bot continua a cercarlo da solo, ma puoi segnarlo tu:\n\n" + "\n".join(lines)),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            db.mark_result_reminded([sig["id"] for sig in chunk])
        except Exception as e:
            logger.error(f"Promemoria risultati fallito: {e}")

async def _results_job(app: Application, sport: str):
    """Job orario: cerca i risultati e poi manda i promemoria per quelli mancanti."""
    try:
        await run_auto_results(app, sport=sport)
    finally:
        try:
            await remind_pending_results(app, sport)
        except Exception as e:
            logger.error(f"Promemoria risultati ({sport}) errore: {e}")

async def run_auto_results(app: Application, sport: str = "both"):
    """
    Controlla le API per i risultati delle partite completate
    e aggiorna automaticamente i segnali winner pendenti.
    Over/Under rimane manuale (no dati set dalle API gratuite).

    sport: "both" | "tennis" | "tabletennis" — limita il controllo a un solo
    sport. Usato per separare la frequenza dei controlli: il tennis (The Odds
    API, 500 crediti/mese) può girare ogni 30 min, il ping pong (OddsPapi,
    solo 250 richieste/mese) ha una schedulazione propria molto più rada
    (vedi post_init) per non esaurire la quota.
    """
    pending = db.get_signals_for_auto_result()
    # Considera solo segnali winner — over/under non aggiornabili automaticamente
    pending = [s for s in pending if s.get("signal_type") == "winner"]
    if sport != "both":
        pending = [s for s in pending if s.get("sport") == sport]
    # Quota: cerca i risultati solo dei segnali la cui partita può essere già finita
    # (e che l'API copre ancora) — vedi result_due in scraper.py.
    pending = [s for s in pending if result_due(s)]
    if not pending:
        return

    logger.info(f"🔄 Auto-risultati [{sport}]: controllo {len(pending)} segnali winner pendenti...")

    try:
        scores = await scraper.fetch_scores(sport=sport)
    except Exception as e:
        logger.warning(f"Auto-risultati errore fetch: {e}")
        return

    if not scores:
        logger.info("Auto-risultati: nessun risultato disponibile dalle API")
        return

    logger.info(f"Auto-risultati: {len(scores)} risultati — es: {[(s['home'][:10],s['away'][:10]) for s in scores[:3]]}") 

    def normalize(name: str) -> str:
        import unicodedata
        name = unicodedata.normalize("NFKD", name.lower().strip())
        return "".join(c for c in name if not unicodedata.combining(c))

    def names_match(a: str, b: str) -> bool:
        """Match flessibile tra nomi giocatori."""
        a, b = normalize(a), normalize(b)
        if a == b:
            return True
        a_parts, b_parts = a.split(), b.split()
        # Cognome corrisponde DA SOLO: valido solo se uno dei due non ha un nome
        # proprio (es. liste con un solo nome) — altrimenti cognomi comuni
        # (Garcia, Martinez, Petrovic...) rischiano falsi positivi tra giocatori
        # diversi, con impatto diretto sul bilancio.
        if a_parts and b_parts and len(a_parts[-1]) >= 4 and a_parts[-1] == b_parts[-1]:
            if len(a_parts) == 1 or len(b_parts) == 1:
                return True
        # Uno contiene l'altro (es. "T. Boll" in "Timo Boll")
        if len(a) > 4 and len(b) > 4 and (a in b or b in a):
            return True
        # Iniziale + cognome (es. "J. Tjen" vs "Janice Tjen")
        if len(a_parts) >= 2 and len(b_parts) >= 2:
            if a_parts[-1] == b_parts[-1] and a_parts[0][0] == b_parts[0][0]:
                return True
        return False

    updated = 0
    for sig in pending:
        p1 = sig.get("player1", "")
        p2 = sig.get("player2", "")
        pick = sig.get("pick", "").lower()
        result = None
        matched_score = None

        # Cerca il match nei risultati
        for sc in scores:
            home, away = sc.get("home",""), sc.get("away","")
            if (names_match(p1, home) and names_match(p2, away)) or \
               (names_match(p1, away) and names_match(p2, home)):
                matched_score = sc
                break

        if not matched_score:
            logger.info(f"Auto-risultati: nessun match per '{p1}' vs '{p2}'")
            continue

        winner = matched_score.get("winner", "")
        logger.info(f"Auto-risultati: match trovato '{p1}' vs '{p2}' — vincitore: '{winner}' — pick: '{pick}'")

        # Determina vinto/perso: il pick è tipo "Kasatkina vince"
        if names_match(p1, winner):
            picked_won = (normalize(p1) in pick or
                any(part in pick for part in normalize(p1).split() if len(part) > 3))
        else:
            picked_won = (normalize(p2) in pick or
                any(part in pick for part in normalize(p2).split() if len(part) > 3))

        result = "won" if picked_won else "lost"

        if db.auto_update_result(sig["id"], result):
            updated += 1
            # Registra nel bilancio
            db.record_balance_entry(sig, result)

            emoji = "✅" if result == "won" else "❌"
            bal_stats = db.get_balance_stats(sport=sig.get("sport"))
            bal_str = f"{bal_stats['current_balance_pct']:+.2f}% bankroll" if bal_stats["total_bets"] > 0 else "n/d"

            _sp = sig.get("sport", "tabletennis")
            _sent = await send_to_groups(app.bot, _sp, "result", _result_group_text(sig, result, bal_stats))
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"{emoji} *Risultato automatico!*\n"
                    f"{'━' * 22}\n"
                    f"{sig.get('sport_label','🏓')} {sig['match']}\n"
                    f"🎯 {sig['pick']} @ {sig['odds']}\n"
                    f"📌 Stake: {sig['stake']}/5\n\n"
                    f"{'✅ *VINTO!* 🎉' if result == 'won' else '❌ *Perso.*'}\n\n"
                    f"💰 Bilancio {sig.get('sport_label','')}: *{bal_str}*\n"
                    f"📊 W/L: {bal_stats['won']}V/{bal_stats['lost']}P | "
                    f"Win%: {bal_stats['winrate']}% | ROI: {bal_stats['roi']}%\n\n"
                    f"{_sent_note(_sp, 'result', _sent)}"
                ),
                parse_mode="Markdown"
            )

    if updated:
        logger.info(f"✅ Auto-risultati: {updated} segnali aggiornati")
    else:
        logger.info("Auto-risultati: nessun match completato trovato")


# ── Main ─────────────────────────────────────────────────────────────────────────
async def global_error_handler(update, context):
    """Error handler di default per l'Application — prima NON ce n'era uno
    ("No error handlers are registered, logging exception"), quindi un
    crash in un bottone (es. edit_message_text su un testo identico a
    quello già mostrato → BadRequest "Message is not modified", capitato
    premendo "Impostazioni" quando il pannello era già aperto su quella
    stessa schermata) veniva solo loggato e la richiesta finiva nel nulla
    — il bottone sembrava non rispondere più, senza nessun avviso."""
    err = context.error
    from telegram.error import BadRequest
    if isinstance(err, BadRequest) and "message is not modified" in str(err).lower():
        # Contenuto già corretto, nulla da aggiornare — non è un errore reale.
        logger.info("Edit ignorato (contenuto identico, nessuna modifica necessaria)")
        return
    logger.error(f"Errore non gestito: {err}", exc_info=err)
    try:
        if update and getattr(update, "effective_chat", None) and update.effective_chat.id == ADMIN_ID:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Si è verificato un errore interno: `{str(err)[:200]}`\nRiprova o torna a Home.",
                parse_mode="Markdown",
                reply_markup=PERSISTENT_KB,
            )
    except Exception:
        pass

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN non impostato!")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu",  menu))
    app.add_handler(CallbackQueryHandler(public_chart_handler, pattern=r"^pubchart_"))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, kb_handler))
    app.add_error_handler(global_error_handler)

    logger.info("🏓 Bot avviato")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
