import os
import logging
import asyncio
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
    if not for_vip:
        text += f"\n📝 _{s.get('reasoning', '')}_"
    return text


def vip_signal_text(s: dict) -> str:
    return signal_text(s, for_vip=True)


# ── /start ──────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        kb = [
            [InlineKeyboardButton("🔍 Cerca segnali ora", callback_data="admin_scan")],
            [InlineKeyboardButton("📋 Segnali pendenti", callback_data="admin_pending")],
            [InlineKeyboardButton("📊 Statistiche", callback_data="admin_stats")],
            [InlineKeyboardButton("⚙️ Impostazioni", callback_data="admin_settings")],
        ]
        await update.message.reply_text(
            "👋 *Admin Panel — PingPong Signals*\n\n"
            "Il bot cerca segnali automaticamente ogni ora.\n"
            "Usa i tasti per gestire i segnali.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await update.message.reply_text(
            "🏓 *PingPong Signals Bot*\n\nQuesto bot è riservato agli admin.",
            parse_mode="Markdown"
        )


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
    uid   = update.effective_user.id
    data  = query.data

    if uid != ADMIN_ID:
        await query.edit_message_text("⛔ Non autorizzato.")
        return

    # ── Admin home ──────────────────────────────────────────────────────────────
    if data == "admin_home":
        kb = [
            [InlineKeyboardButton("🔍 Cerca segnali ora", callback_data="admin_scan")],
            [InlineKeyboardButton("📋 Segnali pendenti", callback_data="admin_pending")],
            [InlineKeyboardButton("📊 Statistiche",      callback_data="admin_stats")],
            [InlineKeyboardButton("⚙️ Impostazioni",     callback_data="admin_settings")],
        ]
        await query.edit_message_text(
            "👋 *Admin Panel — PingPong Signals*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # ── Scan manuale ────────────────────────────────────────────────────────────
    elif data == "admin_scan":
        await query.edit_message_text("🔍 Scansione in corso... attendere.")
        count = await run_signal_scan(context.application)
        kb = [[InlineKeyboardButton("🔙 Home", callback_data="admin_home")]]
        await query.edit_message_text(
            f"✅ Scan completato!\n🆕 Nuovi segnali trovati: *{count}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # ── Segnali pendenti ────────────────────────────────────────────────────────
    elif data == "admin_pending":
        signals = db.get_pending_signals()
        if not signals:
            kb = [[InlineKeyboardButton("🔙 Home", callback_data="admin_home")]]
            await query.edit_message_text(
                "📭 Nessun segnale pendente.",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return
        # Show first pending signal
        s = signals[0]
        text = signal_text(s)
        kb = [
            [
                InlineKeyboardButton("📤 Invia al VIP ✅", callback_data=f"send_vip_{s['id']}"),
                InlineKeyboardButton("🗑 Scarta",          callback_data=f"discard_{s['id']}"),
            ],
            [InlineKeyboardButton(f"📋 Altri pendenti: {len(signals)-1}", callback_data="admin_pending_list")],
            [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ── Lista pendenti ──────────────────────────────────────────────────────────
    elif data == "admin_pending_list":
        signals = db.get_pending_signals()
        if not signals:
            await query.edit_message_text("📭 Nessun segnale pendente.")
            return
        kb = []
        for s in signals[:10]:
            label = f"🏓 {s['match'][:25]} | {s['pick'][:15]} | x{s['odds']}"
            kb.append([InlineKeyboardButton(label, callback_data=f"view_signal_{s['id']}")])
        kb.append([InlineKeyboardButton("🔙 Home", callback_data="admin_home")])
        await query.edit_message_text(
            f"📋 *Segnali pendenti ({len(signals)}):*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # ── Visualizza singolo segnale ──────────────────────────────────────────────
    elif data.startswith("view_signal_"):
        sig_id = int(data.split("_")[-1])
        s = db.get_signal(sig_id)
        if not s:
            await query.edit_message_text("❌ Segnale non trovato.")
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
            await query.edit_message_text("❌ Segnale non trovato.")
            return
        try:
            await context.application.bot.send_message(
                chat_id=VIP_GROUP_ID,
                text=vip_signal_text(s),
                parse_mode="Markdown"
            )
            db.update_signal_status(sig_id, "sent")
            kb = [
                [InlineKeyboardButton("📋 Altri segnali", callback_data="admin_pending")],
                [InlineKeyboardButton("🔙 Home",          callback_data="admin_home")],
            ]
            await query.edit_message_text(
                f"✅ *Segnale inviato al canale VIP!*\n\n{vip_signal_text(s)}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            logger.error(f"Errore invio VIP: {e}")
            await query.edit_message_text(f"❌ Errore invio: {e}")

    # ── Scarta segnale ──────────────────────────────────────────────────────────
    elif data.startswith("discard_"):
        sig_id = int(data.split("_")[-1])
        db.update_signal_status(sig_id, "discarded")
        kb = [
            [InlineKeyboardButton("📋 Altri segnali", callback_data="admin_pending")],
            [InlineKeyboardButton("🔙 Home",          callback_data="admin_home")],
        ]
        await query.edit_message_text(
            "🗑 Segnale scartato.",
            reply_markup=InlineKeyboardMarkup(kb)
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
            [InlineKeyboardButton(
                f"🎯 Min confidenza: {settings['min_confidence']}%",
                callback_data="noop"
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
            [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
        ]
        await query.edit_message_text(
            f"⚙️ Auto-invio VIP: {'✅ Attivato' if settings['auto_send'] else '❌ Disattivato'}",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "toggle_interval":
        intervals = [1, 2, 4, 6, 12]
        current = db.get_settings()["scan_interval"]
        next_i  = intervals[(intervals.index(current) + 1) % len(intervals)] if current in intervals else 1
        db.set_setting("scan_interval", next_i)
        kb = [
            [InlineKeyboardButton(f"⏱ Scan ogni {next_i}h → cambia", callback_data="toggle_interval")],
            [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
        ]
        await query.edit_message_text(
            f"⏱ Intervallo scan impostato: ogni *{next_i} ore*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )


# ── CORE: scan automatico ────────────────────────────────────────────────────────
async def run_signal_scan(app: Application) -> int:
    """Scrape matches, analyze with AI, store new signals. Returns count of new signals."""
    logger.info("🔍 Avvio scan segnali...")
    db.set_setting("last_scan", __import__("datetime").datetime.now().strftime("%d/%m %H:%M"))

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

                # Notifica admin
                text = signal_text(sig)
                kb = [
                    [
                        InlineKeyboardButton("📤 Invia al VIP ✅", callback_data=f"send_vip_{sig_id}"),
                        InlineKeyboardButton("🗑 Scarta",          callback_data=f"discard_{sig_id}"),
                    ]
                ]
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🆕 *Nuovo segnale trovato!*\n\n{text}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb)
                )

                # Auto-invio al VIP se attivato
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


# ── SCHEDULER ────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.ensure_future(run_signal_scan(app)),
        trigger="interval",
        hours=1,
        id="signal_scan"
    )
    scheduler.start()
    logger.info("⏰ Scheduler avviato — scan ogni ora")

    # Scan iniziale al boot
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
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("🏓 PingPong Signals Bot avviato")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
