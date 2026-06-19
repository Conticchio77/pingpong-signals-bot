import os
import logging
import asyncio
import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scraper import SignalScraper
from ai_analyzer import AIAnalyzer
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ROME         = ZoneInfo("Europe/Rome")
TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "858001417"))
VIP_GROUP_ID = int(os.environ.get("VIP_GROUP_ID", "-1002950341972"))
ODDS_KEY     = os.environ.get("ODDS_API_KEY", "")

db       = Database()
scraper  = SignalScraper()
analyzer = AIAnalyzer()

# ── Tastiera persistente (sempre visibile in basso) ──────────────────────────────
PERSISTENT_KB = ReplyKeyboardMarkup(
    [
        ["🔍 Scan",  "📋 Segnali"],
        ["📊 Stats", "⚙️ Impostazioni"],
        ["🏠 Home"],
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
        "odds_api":        "📡 The Odds API",
        "oddspapi":        "📡 OddsPapi",
        "oddspapi_noodds": "📡 OddsPapi (fixture only)",
        "fallback":        "⚠️ Quote stimate",
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
        f"📌 Stake: *{s['stake']}/5*\n"
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

    return (
        f"🏓🎾 *Signals Bot — Admin Panel*\n"
        f"{'━' * 26}\n"
        f"🕐 Ora: {now_it_str()}\n"
        f"🔗 {src_tag}\n\n"
        f"📨 Segnali tot: *{stats['total']}* | ⏳ Pendenti: *{stats['pending']}*\n"
        f"✅ Vinti: *{stats['won']}* | ❌ Persi: *{stats['lost']}* | 🏆 Win%: *{stats['winrate']}%*\n"
        f"🔄 Ultimo scan: *{stats['last_scan']}*\n\n"
        f"⚙️ Scan ogni *{s['scan_interval']}h* | "
        f"Confidenza min: *{s['min_confidence']}%* | "
        f"Auto-VIP: *{'✅' if s['auto_send'] else '❌'}*"
    )

def admin_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Cerca segnali ora", callback_data="admin_scan"),
         InlineKeyboardButton("📋 Segnali",           callback_data="admin_list")],
        [InlineKeyboardButton("📊 Statistiche",       callback_data="admin_stats"),
         InlineKeyboardButton("⚙️ Impostazioni",      callback_data="admin_settings")],
    ])

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
    """Alias /menu — riporta sempre al pannello principale."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        admin_panel_text(),
        parse_mode="Markdown",
        reply_markup=admin_panel_kb()
    )

# ── Tastiera persistente → gestisce tasti fissi in basso ─────────────────────────
async def kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    txt = update.message.text

    if txt == "🔍 Scan":
        msg = await update.message.reply_text("🔍 Scansione in corso...")
        count = await run_signal_scan(context.application)
        await msg.edit_text(f"✅ Scan completato! Nuovi segnali: *{count}*", parse_mode="Markdown")
        await update.message.reply_text(admin_panel_text(), parse_mode="Markdown", reply_markup=admin_panel_kb())

    elif txt == "📋 Segnali":
        await send_signals_list(update.message.reply_text)

    elif txt == "📊 Stats":
        await send_stats(update.message.reply_text)

    elif txt == "⚙️ Impostazioni":
        await send_settings(update.message.reply_text)

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
        si    = status_icon.get(s["status"], "•")
        label = f"{si} {s['kickoff']} | {s['match'][:18]} @{s['odds']}"
        kb.append([InlineKeyboardButton(label, callback_data=f"view_signal_{s['id']}")])
    kb.append([InlineKeyboardButton("🗑 Cancella vecchi segnali", callback_data="confirm_purge")])
    await fn(
        "📋 *Segnali — dal più vicino:*\n\n"
        "🆕 Nuovo  👁 Visto  📤 Inviato VIP\n"
        "✅ Vinto  ❌ Perso  🗑 Scartato",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def send_stats(fn):
    stats = db.get_stats()
    kb = [
        [InlineKeyboardButton("🗑 Reset statistiche", callback_data="confirm_reset_stats")],
        [InlineKeyboardButton("🔙 Home",              callback_data="admin_home")],
    ]
    await fn(
        f"📊 *Statistiche*\n\n"
        f"📨 Totali: *{stats['total']}*\n"
        f"📤 Inviati VIP: *{stats['sent_vip']}*\n"
        f"⏳ Pendenti: *{stats['pending']}*\n"
        f"🗑 Scartati: *{stats['discarded']}*\n"
        f"✅ Vinti: *{stats['won']}*\n"
        f"❌ Persi: *{stats['lost']}*\n"
        f"🏆 Win rate: *{stats['winrate']}%*\n"
        f"💰 ROI: *{stats['roi']}%*\n"
        f"🔄 Ultimo scan: *{stats['last_scan']}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def send_settings(fn):
    s = db.get_settings()
    conf = s['min_confidence']
    # Descrizione leggibile della confidenza
    conf_desc = {55: "Bassa (55%)", 60: "Media (60%)", 65: "Media-Alta (65%)",
                 70: "Alta (70%)", 75: "Molto Alta (75%)", 80: "Massima (80%)"}
    conf_label = conf_desc.get(conf, f"{conf}%")

    kb = [
        [InlineKeyboardButton(f"⏱ Scan: ogni {s['scan_interval']}h", callback_data="pick_interval")],
        [InlineKeyboardButton(f"📤 Auto-invio VIP: {'✅ ON' if s['auto_send'] else '❌ OFF'}", callback_data="toggle_autosend")],
        [InlineKeyboardButton(f"🎯 Confidenza: {conf_label}", callback_data="pick_confidence")],
        [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
    ]
    await fn(
        "⚙️ *Impostazioni*\n\n"
        "Tocca un'opzione per modificarla:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ── CALLBACK HANDLER ─────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        await query.edit_message_text("⛔ Non autorizzato.")
        return
    data = query.data

    # ── Home ─────────────────────────────────────────────────────────────────────
    if data == "admin_home":
        await query.edit_message_text(
            admin_panel_text(), parse_mode="Markdown", reply_markup=admin_panel_kb()
        )

    # ── Scan ─────────────────────────────────────────────────────────────────────
    elif data == "admin_scan":
        await query.edit_message_text("🔍 Scansione in corso... attendere.")
        count = await run_signal_scan(context.application)
        await query.edit_message_text(
            f"✅ Scan completato!\n🆕 Nuovi segnali: *{count}*",
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
        kb = []
        if s["status"] not in ("won", "lost", "sent"):
            kb.append([
                InlineKeyboardButton("📤 Invia al VIP ✅", callback_data=f"send_vip_{sig_id}"),
                InlineKeyboardButton("🗑 Scarta",          callback_data=f"discard_{sig_id}"),
            ])
        kb.append([
            InlineKeyboardButton("✅ Vinto", callback_data=f"result_{sig_id}_won"),
            InlineKeyboardButton("❌ Perso", callback_data=f"result_{sig_id}_lost"),
        ])
        kb.append([InlineKeyboardButton("🔙 Lista", callback_data="admin_list")])
        await query.edit_message_text(signal_text(s), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ── Invia VIP ────────────────────────────────────────────────────────────────
    elif data.startswith("send_vip_"):
        sig_id = int(data.split("_")[-1])
        s = db.get_signal(sig_id)
        if not s:
            await query.edit_message_text("❌ Segnale non trovato.")
            return
        try:
            await context.application.bot.send_message(
                chat_id=VIP_GROUP_ID, text=vip_signal_text(s), parse_mode="Markdown"
            )
            db.update_signal_status(sig_id, "sent")
            kb = [[InlineKeyboardButton("📋 Lista", callback_data="admin_list"),
                   InlineKeyboardButton("🔙 Home",  callback_data="admin_home")]]
            await query.edit_message_text(
                f"✅ Inviato al VIP!\n\n{vip_signal_text(s)}",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Errore invio VIP: {e}")

    # ── Scarta ───────────────────────────────────────────────────────────────────
    elif data.startswith("discard_"):
        sig_id = int(data.split("_")[-1])
        db.update_signal_status(sig_id, "discarded")
        await query.edit_message_text(
            "🗑 Scartato.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Lista", callback_data="admin_list")]])
        )

    # ── Risultato vinto/perso ────────────────────────────────────────────────────
    elif data.startswith("result_"):
        parts  = data.split("_")
        sig_id, result = int(parts[1]), parts[2]
        db.update_signal_status(sig_id, result, result)
        emoji = "✅ *VINTO!* Ottimo segnale!" if result == "won" else "❌ *Perso.* Prossima volta!"
        kb = [
            [InlineKeyboardButton("📋 Torna alla lista", callback_data="admin_list")],
            [InlineKeyboardButton("🔙 Home",             callback_data="admin_home")],
        ]
        await query.edit_message_text(emoji, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

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

    # ── Reset stats — chiedi conferma ────────────────────────────────────────────
    elif data == "confirm_reset_stats":
        kb = [
            [InlineKeyboardButton("⚠️ SÌ, resetta tutto", callback_data="do_reset_stats")],
            [InlineKeyboardButton("❌ Annulla",            callback_data="admin_stats")],
        ]
        await query.edit_message_text(
            "⚠️ *Sei sicuro?*\n\nVerranno azzerati tutti i risultati (vinti/persi).\nI segnali rimarranno in lista.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "do_reset_stats":
        db.reset_results()
        await query.edit_message_text(
            "✅ Statistiche resettate.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Home", callback_data="admin_home")]])
        )

    # ── Impostazioni ─────────────────────────────────────────────────────────────
    elif data == "admin_settings":
        await send_settings(query.edit_message_text)

    elif data == "toggle_autosend":
        db.toggle_setting("auto_send")
        await send_settings(query.edit_message_text)

    # ── Scegli intervallo scan (picker visuale) ───────────────────────────────────
    elif data == "pick_interval":
        current = db.get_settings()["scan_interval"]
        opts    = [1, 2, 4, 6, 12]
        kb = []
        row = []
        for o in opts:
            label = f"✅ {o}h" if o == current else f"{o}h"
            row.append(InlineKeyboardButton(label, callback_data=f"set_interval_{o}"))
        kb.append(row)
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            f"⏱ *Seleziona ogni quante ore fare lo scan*\n(attuale: ogni {current}h):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_interval_"):
        nxt = int(data.split("_")[-1])
        db.set_setting("scan_interval", nxt)
        _restart_scheduler(context.application, nxt)
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

    elif data.startswith("set_conf_"):
        nxt = int(data.split("_")[-1])
        db.set_setting("min_confidence", nxt)
        await send_settings(query.edit_message_text)


# ── Scheduler ────────────────────────────────────────────────────────────────────
_scheduler: AsyncIOScheduler | None = None

def _restart_scheduler(app: Application, hours: int):
    global _scheduler
    if _scheduler:
        _scheduler.add_job(
            lambda: asyncio.ensure_future(run_signal_scan(app)),
            trigger=IntervalTrigger(hours=hours),
            id="signal_scan", replace_existing=True,
        )
        logger.info(f"⏰ Scheduler aggiornato: ogni {hours}h")

async def post_init(app: Application):
    global _scheduler
    hours     = db.get_settings()["scan_interval"]
    _scheduler = AsyncIOScheduler(timezone=ROME)
    _scheduler.add_job(
        lambda: asyncio.ensure_future(run_signal_scan(app)),
        trigger=IntervalTrigger(hours=hours),
        id="signal_scan",
    )
    # Auto-aggiornamento risultati ogni 30 minuti
    _scheduler.add_job(
        lambda: asyncio.ensure_future(run_auto_results(app)),
        trigger=IntervalTrigger(minutes=30),
        id="auto_results",
    )
    _scheduler.start()
    logger.info(f"⏰ Scheduler avviato — scan ogni {hours}h | risultati ogni 30min")
    await asyncio.sleep(3)
    await run_signal_scan(app)

# ── Core scan ────────────────────────────────────────────────────────────────────
async def run_signal_scan(app: Application) -> int:
    logger.info("🔍 Avvio scan...")
    db.set_setting("last_scan", now_it_str())

    try:
        matches = await scraper.fetch_matches()
    except Exception as e:
        logger.error(f"Errore scraping: {e}")
        matches = scraper.get_fallback_matches()

    # Log fonti e sport
    sources = set(m.get("source", "?") for m in matches)
    sports  = set(m.get("sport", "?") for m in matches)
    logger.info(f"Fonti: {sources} | Sport: {sports} | Partite: {len(matches)}")

    # Se nessuna partita reale (solo fallback) avvisa l'admin
    real_matches = [m for m in matches if m.get("source") not in ("fallback",)]
    if not real_matches and (ODDS_KEY or os.environ.get("ODDSPAPI_KEY")):
        logger.info("Nessuna partita reale disponibile in questo momento")
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "ℹ️ *Nessuna partita disponibile*\n\n"
                "Le API non hanno partite quotate al momento.\n"
                "Il prossimo scan automatico riproverà tra poco."
            ),
            parse_mode="Markdown",
        )
        return 0

    new_signals = 0
    settings    = db.get_settings()

    for match in matches:
        try:
            signals = await analyzer.analyze(match)
            signals.sort(key=lambda x: x["value_pct"], reverse=True)
            for sig in signals:
                if sig["confidence"] < settings["min_confidence"]:
                    logger.info(
                        f"Segnale scartato (confidenza {sig['confidence']}% < min {settings['min_confidence']}%): "
                        f"{sig.get('match','?')} — {sig.get('pick','?')}"
                    )
                    continue
                if db.signal_exists(sig["match_key"]):
                    continue
                sig_id = db.save_signal(sig)
                new_signals += 1

                sport_label = sig.get("sport_label", "🏓 Ping Pong")
                kb = [[
                    InlineKeyboardButton("📤 Invia al VIP ✅", callback_data=f"send_vip_{sig_id}"),
                    InlineKeyboardButton("🗑 Scarta",          callback_data=f"discard_{sig_id}"),
                ],[
                    InlineKeyboardButton("✅ Vinto", callback_data=f"result_{sig_id}_won"),
                    InlineKeyboardButton("❌ Perso", callback_data=f"result_{sig_id}_lost"),
                ]]
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🆕 *Nuovo segnale {sport_label}!*\n\n{signal_text(sig)}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                if settings["auto_send"]:
                    await app.bot.send_message(
                        chat_id=VIP_GROUP_ID, text=vip_signal_text(sig), parse_mode="Markdown"
                    )
                    db.update_signal_status(sig_id, "sent")

        except Exception as e:
            logger.error(f"Errore analisi {match.get('name','?')}: {e}")

    logger.info(f"✅ {new_signals} nuovi segnali")
    return new_signals

# ── Auto-aggiornamento risultati ─────────────────────────────────────────────────
async def run_auto_results(app: Application):
    """
    Controlla The Odds API per i risultati delle partite completate
    e aggiorna automaticamente i segnali pendenti.
    """
    pending = db.get_signals_for_auto_result()
    if not pending:
        return

    logger.info(f"🔄 Auto-risultati: controllo {len(pending)} segnali pendenti...")

    try:
        scores = await scraper.fetch_scores()
    except Exception as e:
        logger.warning(f"Auto-risultati errore fetch: {e}")
        return

    if not scores:
        return

    # Crea mappa veloce: "player1 vs player2" → winner
    def normalize(name: str) -> str:
        return name.lower().strip()

    score_map = {}
    for sc in scores:
        key = f"{normalize(sc['home'])} vs {normalize(sc['away'])}"
        score_map[key] = sc["winner"]
        # anche reverse
        key2 = f"{normalize(sc['away'])} vs {normalize(sc['home'])}"
        score_map[key2] = sc["winner"]

    updated = 0
    for sig in pending:
        match_key = normalize(sig["match"])  # "Fan Zhendong vs Wang Chuqin"
        winner = score_map.get(match_key)
        if not winner:
            continue

        # Determina se il segnale è vinto o perso
        pick = sig.get("pick", "").lower()
        sig_type = sig.get("signal_type", "")
        result = None

        if sig_type == "winner":
            # "fan zhendong vince" → controlla se il vincitore corrisponde
            if normalize(winner) in pick or any(
                normalize(p) in pick
                for p in [sig["player1"], sig["player2"]]
                if normalize(p) in normalize(winner)
            ):
                result = "won"
            else:
                result = "lost"

        elif sig_type in ("over", "under"):
            # Per over/under non abbiamo il totale set — skip auto-update
            continue

        elif sig_type == "handicap":
            # Per handicap serve il numero di set — skip auto-update
            continue

        if result and db.auto_update_result(sig["id"], result):
            updated += 1
            emoji = "✅" if result == "won" else "❌"
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"{emoji} *Risultato aggiornato automaticamente!*\n\n"
                    f"{sig.get('sport_label', '🏓')} {sig['match']}\n"
                    f"🎯 {sig['pick']} @ {sig['odds']}\n"
                    f"{'✅ VINTO!' if result == 'won' else '❌ Perso.'}"
                ),
                parse_mode="Markdown"
            )

    if updated:
        logger.info(f"✅ Auto-risultati: {updated} segnali aggiornati")
    else:
        logger.info("Auto-risultati: nessun match completato trovato")


# ── Main ─────────────────────────────────────────────────────────────────────────
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
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, kb_handler))

    logger.info("🏓 Bot avviato")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
