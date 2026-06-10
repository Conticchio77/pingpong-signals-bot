import os
import logging
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scraper import SignalScraper
from ai_analyzer import AIAnalyzer
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────
TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "858001417"))
VIP_GROUP_ID = int(os.environ.get("VIP_GROUP_ID", "-1002950341972"))

db       = Database()
scraper  = SignalScraper()
analyzer = AIAnalyzer()


# ── Helpers ─────────────────────────────────────────────────────────────────────
def signal_text(s: dict, for_vip: bool = False) -> str:
    type_icons = {
        "winner":   "🏆",
        "over":     "📈",
        "under":    "📉",
        "handicap": "⚖️",
        "set":      "🎯",
    }
    icon = type_icons.get(s["signal_type"], "🏓")
    stars = "⭐" * min(5, max(1, round(s["confidence"] / 20)))
    value_str = f"+{s['value_pct']:.1f}%" if s["value_pct"] > 0 else f"{s['value_pct']:.1f}%"

    text = (
        f"🏓 *SEGNALE PING PONG*\n"
        f"{'━' * 22}\n"
        f"{icon} *{s['match']}*\n"
        f"🎯 Giocata: *{s['pick']}*\n"
        f"💰 Quota: *{s['odds']}*\n"
        f"📊 Confidenza: *{s['confidence']}%* {stars}\n"
        f"💡 Value edge: *{value_str}*\n"
        f"📌 Stake: *{s['stake']}/5*\n"
        f"⏰ Inizio: *{s['kickoff']}*\n"
        f"🌍 Torneo: {s.get('tournament', 'Ping Pong')}\n"
    )
    if s.get("book_note"):
        text += f"\n📖 _Dove trovarlo: {s['book_note']}_"
    if not for_vip:
        text += f"\n📝 _{s.get('reasoning', '')}_"
    return text


def vip_signal_text(s: dict) -> str:
    return signal_text(s, for_vip=True)


def admin_panel_kb() -> InlineKeyboardMarkup:
    """Tastiera del pannello admin — usata ovunque per ripristinarlo."""
    kb = [
        [InlineKeyboardButton("🔍 Cerca segnali ora", callback_data="admin_scan")],
        [InlineKeyboardButton("📋 Segnali pendenti",  callback_data="admin_pending")],
        [InlineKeyboardButton("📊 Statistiche",       callback_data="admin_stats")],
        [InlineKeyboardButton("⚙️ Impostazioni",      callback_data="admin_settings")],
    ]
    return InlineKeyboardMarkup(kb)


async def send_admin_panel(target, text: str = "👋 *Admin Panel — PingPong Signals*"):
    """
    Invia/aggiorna il pannello admin.
    `target` può essere un Update (→ reply) o un bot (→ send_message ad ADMIN_ID).
    """
    if isinstance(target, Update):
        await target.message.reply_text(text, parse_mode="Markdown", reply_markup=admin_panel_kb())
    else:
        # target è l'oggetto bot
        await target.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown",
                                  reply_markup=admin_panel_kb())


# ── /start e /admin ──────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        await send_admin_panel(update)
    else:
        await update.message.reply_text(
            "🏓 *PingPong Signals Bot*\n\nQuesto bot è riservato agli admin.",
            parse_mode="Markdown"
        )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FIX 1 – /admin mostra sempre il pannello senza dover usare /start."""
    if update.effective_user.id == ADMIN_ID:
        await send_admin_panel(update)


# ── /status ──────────────────────────────────────────────────────────────────────
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    stats = db.get_stats()
    await update.message.reply_text(
        f"📊 *Statistiche Bot*\n\n"
        f"✅ Segnali inviati al VIP: *{stats['sent_vip']}*\n"
        f"⏳ Segnali pendenti: *{stats['pending']}*\n"
        f"🏆 Win rate: *{stats['winrate']}%*\n"
        f"💰 ROI medio: *{stats['roi']}%*\n"
        f"🔄 Ultimo scan: *{stats['last_scan']}*",
        parse_mode="Markdown"
    )


# ── CALLBACK HANDLER ─────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    data = query.data

    if uid != ADMIN_ID:
        await query.edit_message_text("⛔ Non autorizzato.")
        return

    # ── Admin home ──────────────────────────────────────────────────────────────
    if data == "admin_home":
        await query.edit_message_text(
            "👋 *Admin Panel — PingPong Signals*",
            parse_mode="Markdown",
            reply_markup=admin_panel_kb()
        )

    # ── Scan manuale ────────────────────────────────────────────────────────────
    elif data == "admin_scan":
        await query.edit_message_text("🔍 Scansione in corso... attendere.")
        count = await run_signal_scan(context.application)
        # FIX 1 – dopo scan mostra di nuovo il pannello
        await query.edit_message_text(
            f"✅ Scan completato! 🆕 Nuovi segnali: *{count}*\n\n"
            f"👇 _Usa i tasti qui sotto per gestire i segnali._",
            parse_mode="Markdown",
            reply_markup=admin_panel_kb()
        )

    # ── Segnali pendenti ────────────────────────────────────────────────────────
    elif data == "admin_pending":
        # FIX 5 – ordinamento per kickoff (dal più vicino)
        signals = db.get_pending_signals()
        signals = _sort_by_kickoff(signals)
        if not signals:
            await query.edit_message_text(
                "📭 Nessun segnale pendente.",
                reply_markup=admin_panel_kb()   # FIX 1
            )
            return
        s = signals[0]
        text = signal_text(s)
        kb = [
            [
                InlineKeyboardButton("📤 Invia al VIP ✅", callback_data=f"send_vip_{s['id']}"),
                InlineKeyboardButton("🗑 Scarta",          callback_data=f"discard_{s['id']}"),
            ],
            [InlineKeyboardButton(f"📋 Lista completa ({len(signals)})", callback_data="admin_pending_list")],
            [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ── Lista pendenti ──────────────────────────────────────────────────────────
    elif data == "admin_pending_list":
        signals = db.get_pending_signals()
        signals = _sort_by_kickoff(signals)   # FIX 5
        if not signals:
            await query.edit_message_text("📭 Nessun segnale pendente.",
                                          reply_markup=admin_panel_kb())
            return
        kb = []
        for s in signals[:10]:
            label = f"⏰{s['kickoff']} | {s['match'][:20]} | {s['pick'][:12]}"
            kb.append([InlineKeyboardButton(label, callback_data=f"view_signal_{s['id']}")])
        kb.append([InlineKeyboardButton("🔙 Home", callback_data="admin_home")])
        await query.edit_message_text(
            f"📋 *Segnali pendenti ({len(signals)}) — ordinati per orario:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # ── Visualizza singolo segnale ──────────────────────────────────────────────
    elif data.startswith("view_signal_"):
        sig_id = int(data.split("_")[-1])
        s = db.get_signal(sig_id)
        if not s:
            await query.edit_message_text("❌ Segnale non trovato.", reply_markup=admin_panel_kb())
            return
        text = signal_text(s)
        kb = [
            [
                InlineKeyboardButton("📤 Invia al VIP ✅", callback_data=f"send_vip_{sig_id}"),
                InlineKeyboardButton("🗑 Scarta",          callback_data=f"discard_{sig_id}"),
            ],
            [InlineKeyboardButton("🔙 Lista", callback_data="admin_pending_list")],
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ── Invia al VIP ────────────────────────────────────────────────────────────
    elif data.startswith("send_vip_"):
        sig_id = int(data.split("_")[-1])
        s = db.get_signal(sig_id)
        if not s:
            await query.edit_message_text("❌ Segnale non trovato.", reply_markup=admin_panel_kb())
            return
        try:
            await context.application.bot.send_message(
                chat_id=VIP_GROUP_ID,
                text=vip_signal_text(s),
                parse_mode="Markdown"
            )
            db.update_signal_status(sig_id, "sent")
            # FIX 1 – dopo invio ripristina il pannello
            await query.edit_message_text(
                f"✅ *Segnale inviato al canale VIP!*",
                parse_mode="Markdown",
                reply_markup=admin_panel_kb()
            )
        except Exception as e:
            logger.error(f"Errore invio VIP: {e}")
            await query.edit_message_text(f"❌ Errore invio: {e}", reply_markup=admin_panel_kb())

    # ── Scarta segnale ──────────────────────────────────────────────────────────
    elif data.startswith("discard_"):
        sig_id = int(data.split("_")[-1])
        db.update_signal_status(sig_id, "discarded")
        # FIX 1 – dopo scarto ripristina il pannello
        await query.edit_message_text(
            "🗑 Segnale scartato.",
            reply_markup=admin_panel_kb()
        )

    # ── Statistiche ─────────────────────────────────────────────────────────────
    elif data == "admin_stats":
        stats = db.get_stats()
        kb = [[InlineKeyboardButton("🔙 Home", callback_data="admin_home")]]
        await query.edit_message_text(
            f"📊 *Statistiche*\n\n"
            f"✅ Inviati al VIP: *{stats['sent_vip']}*\n"
            f"⏳ Pendenti: *{stats['pending']}*\n"
            f"🗑 Scartati: *{stats['discarded']}*\n"
            f"🏆 Win rate: *{stats['winrate']}%*\n"
            f"💰 ROI medio: *{stats['roi']}%*\n"
            f"🔄 Ultimo scan: *{stats['last_scan']}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # ── Impostazioni ─────────────────────────────────────────────────────────────
    elif data == "admin_settings":
        settings = db.get_settings()
        kb = [
            [InlineKeyboardButton(
                f"⏱ Scan ogni {settings['scan_interval']}h → cambia",
                callback_data="toggle_interval"
            )],
            [InlineKeyboardButton(
                f"📤 Auto-invio VIP: {'✅ ON' if settings['auto_send'] else '❌ OFF'}",
                callback_data="toggle_autosend"
            )],
            # FIX 3 – bottone confidenza funzionante
            [InlineKeyboardButton(
                f"🎯 Min confidenza: {settings['min_confidence']}% → cambia",
                callback_data="toggle_confidence"
            )],
            [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
        ]
        await query.edit_message_text(
            "⚙️ *Impostazioni*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "toggle_autosend":
        db.toggle_setting("auto_send")
        settings = db.get_settings()
        kb = [
            [InlineKeyboardButton(
                f"📤 Auto-invio VIP: {'✅ ON' if settings['auto_send'] else '❌ OFF'}",
                callback_data="toggle_autosend"
            )],
            [InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")],
        ]
        await query.edit_message_text(
            f"📤 Auto-invio VIP: {'✅ Attivato' if settings['auto_send'] else '❌ Disattivato'}",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "toggle_interval":
        intervals = [1, 2, 4, 6, 12]
        current = db.get_settings()["scan_interval"]
        next_i = intervals[(intervals.index(current) + 1) % len(intervals)] if current in intervals else 1
        db.set_setting("scan_interval", next_i)
        kb = [
            [InlineKeyboardButton(f"⏱ Scan ogni {next_i}h → cambia", callback_data="toggle_interval")],
            [InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")],
        ]
        await query.edit_message_text(
            f"⏱ Intervallo scan: ogni *{next_i} ore*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # FIX 3 – selettore confidenza con valori reali
    elif data == "toggle_confidence":
        settings = db.get_settings()
        levels = [55, 60, 65, 70, 75]
        kb = []
        for lvl in levels:
            mark = "✅ " if lvl == settings["min_confidence"] else ""
            kb.append([InlineKeyboardButton(f"{mark}{lvl}%", callback_data=f"set_conf_{lvl}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            f"🎯 *Seleziona la confidenza minima*\n\n"
            f"Attuale: *{settings['min_confidence']}%*\n\n"
            f"_Valori più alti = meno segnali ma più selettivi._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_conf_"):
        new_conf = int(data.split("_")[-1])
        db.set_setting("min_confidence", new_conf)
        kb = [[InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")]]
        await query.edit_message_text(
            f"✅ Confidenza minima impostata: *{new_conf}%*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # ── Keep-alive ping (FIX 6) ─────────────────────────────────────────────────
    elif data == "noop":
        pass


# ── FIX 5: ordinamento segnali per kickoff ───────────────────────────────────────
def _sort_by_kickoff(signals: list) -> list:
    """Ordina i segnali per orario kickoff, dal più vicino al più lontano."""
    def parse_kickoff(s):
        try:
            # Formato atteso: "dd/mm HH:MM"
            now = datetime.now()
            dt = datetime.strptime(s["kickoff"], "%d/%m %H:%M").replace(year=now.year)
            # Se la data è già passata di molto, considerala del prossimo anno
            if (dt - now).days < -30:
                dt = dt.replace(year=now.year + 1)
            return dt
        except Exception:
            return datetime.max
    return sorted(signals, key=parse_kickoff)


# ── CORE: scan automatico ────────────────────────────────────────────────────────
async def run_signal_scan(app: Application) -> int:
    logger.info("🔍 Avvio scan segnali...")
    # FIX 2 – usa orario italiano (viene già gestito in scraper.py)
    from zoneinfo import ZoneInfo
    now_it = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")
    db.set_setting("last_scan", now_it)

    try:
        matches = await scraper.fetch_matches()
    except Exception as e:
        logger.error(f"Errore scraping: {e}")
        matches = scraper.get_fallback_matches()

    new_signals = 0
    settings    = db.get_settings()

    for match in matches:
        try:
            signals = await analyzer.analyze(match)
            for sig in signals:
                if sig["confidence"] < settings["min_confidence"]:
                    continue
                if db.signal_exists(sig["match_key"]):
                    continue
                sig_id = db.save_signal(sig)
                new_signals += 1

                text = signal_text(sig)
                kb = [
                    [
                        InlineKeyboardButton("📤 Invia al VIP ✅", callback_data=f"send_vip_{sig_id}"),
                        InlineKeyboardButton("🗑 Scarta",          callback_data=f"discard_{sig_id}"),
                    ],
                    [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
                ]
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🆕 *Nuovo segnale trovato!*\n\n{text}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb)
                )

                if settings["auto_send"]:
                    await app.bot.send_message(
                        chat_id=VIP_GROUP_ID,
                        text=vip_signal_text(sig),
                        parse_mode="Markdown"
                    )
                    db.update_signal_status(sig_id, "sent")

        except Exception as e:
            logger.error(f"Errore analisi match {match.get('name','?')}: {e}")
            continue

    logger.info(f"✅ Scan completato: {new_signals} nuovi segnali")
    return new_signals


# ── FIX 6: keep-alive per evitare che Railway dorma ─────────────────────────────
async def keep_alive_ping(app: Application):
    """Invia un self-ping all'endpoint Telegram per mantenere il processo vivo."""
    try:
        await app.bot.get_me()
        logger.debug("Keep-alive ping OK")
    except Exception as e:
        logger.warning(f"Keep-alive error: {e}")


# ── SCHEDULER ────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone="Europe/Rome")   # FIX 2
    settings  = db.get_settings()

    scheduler.add_job(
        lambda: asyncio.ensure_future(run_signal_scan(app)),
        trigger="interval",
        hours=settings.get("scan_interval", 1),
        id="signal_scan"
    )
    # FIX 6 – keep-alive ogni 10 minuti
    scheduler.add_job(
        lambda: asyncio.ensure_future(keep_alive_ping(app)),
        trigger="interval",
        minutes=10,
        id="keep_alive"
    )
    scheduler.start()
    logger.info("⏰ Scheduler avviato — scan ogni ora, keep-alive ogni 10 min")

    await asyncio.sleep(5)
    await run_signal_scan(app)


# ── MAIN ─────────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN non impostato!")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("admin",  admin_cmd))   # FIX 1
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("🏓 PingPong Signals Bot avviato")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
