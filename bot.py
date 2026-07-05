import os
import io
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
scraper  = SignalScraper(db=db)
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

def genera_grafico_bilancio() -> io.BytesIO | None:
    """Genera un grafico PNG del bilancio nel tempo. Ritorna BytesIO o None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        history = db.get_balance_history()
        if len(history) < 2:
            return None

        balances = [0.0] + [r["balance"] for r in history]
        labels   = ["Start"] + [f"#{i+1}" for i in range(len(history))]
        colors   = ["#2ecc71" if b >= 0 else "#e74c3c" for b in balances[1:]]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), facecolor="#1a1a2e")
        fig.suptitle("📊 Bilancio Segnali", color="white", fontsize=16, fontweight="bold", y=0.98)

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
        ax1.set_ylabel("Profitto (€)", color="white")
        ax1.tick_params(colors="white")
        ax1.spines[:].set_color("#444")
        ax1.set_xlim(0, len(balances) - 1)
        ax1.grid(axis="y", color="#333", linestyle="--", alpha=0.5)

        # Annotazione ultimo valore
        last_val = balances[-1]
        color_last = "#2ecc71" if last_val >= 0 else "#e74c3c"
        ax1.annotate(
            f"€{last_val:+.0f}",
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
        ax2.set_ylabel("Profitto per bet (€)", color="white")
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
        f"Auto-VIP: *{'✅' if s['auto_send'] else '❌'}* | "
        f"Sport: *{sf_label}*"
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
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    txt = update.message.text

    if txt == "🔍 Scan":
        msg = await update.message.reply_text("🔍 Scansione in corso...")
        count = await run_signal_scan(context.application, manual=True)
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
    bal   = db.get_balance_stats()
    bal_str = f"€{bal['current_balance']:+.0f}" if bal["total_bets"] > 0 else "n/d"
    roi_str = f"{bal['roi']:+.1f}%" if bal["total_bets"] > 0 else "n/d"

    kb = [
        [InlineKeyboardButton("📈 Grafico bilancio", callback_data="show_balance_chart")],
        [InlineKeyboardButton("🗑 Reset risultati (mantieni segnali)", callback_data="confirm_reset_stats")],
        [InlineKeyboardButton("💣 Reset COMPLETO (cancella tutto)", callback_data="confirm_purge_all")],
        [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
    ]
    await fn(
        f"📊 *Statistiche*\n"
        f"{'━' * 22}\n"
        f"📨 Totali: *{stats['total']}*\n"
        f"📤 Inviati VIP: *{stats['sent_vip']}*\n"
        f"⏳ Pendenti: *{stats['pending']}*\n"
        f"✅ Vinti: *{stats['won']}*\n"
        f"❌ Persi: *{stats['lost']}*\n"
        f"🏆 Win rate: *{stats['winrate']}%*\n\n"
        f"💰 *Bilancio*\n"
        f"{'━' * 22}\n"
        f"💵 Bilancio attuale: *{bal_str}*\n"
        f"📈 ROI: *{roi_str}*\n"
        f"🏅 Migliore vincita: *€{bal['best_win']:+.0f}*\n"
        f"📉 Peggiore perdita: *€{bal['worst_loss']:+.0f}*\n"
        f"🔄 Ultimo scan: *{stats['last_scan']}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def send_settings(fn):
    s = db.get_settings()
    conf = s['min_confidence']
    conf_desc = {55: "Bassa (55%)", 60: "Media (60%)", 65: "Media-Alta (65%)",
                 70: "Alta (70%)", 75: "Molto Alta (75%)", 80: "Massima (80%)"}
    conf_label = conf_desc.get(conf, f"{conf}%")

    sf = s.get("sport_filter", "both")
    sf_label = {"both": "🏓🎾 Entrambi", "tabletennis": "🏓 Solo Ping Pong", "tennis": "🎾 Solo Tennis"}.get(sf, "🏓🎾 Entrambi")

    kb = [
        [InlineKeyboardButton(f"⏱ Scan tennis: ogni {s['scan_interval']}h", callback_data="pick_interval")],
        [InlineKeyboardButton(f"📤 Auto-invio VIP: {'✅ ON' if s['auto_send'] else '❌ OFF'}", callback_data="toggle_autosend")],
        [InlineKeyboardButton(f"🎯 Confidenza: {conf_label}", callback_data="pick_confidence")],
        [InlineKeyboardButton(f"🏅 Sport: {sf_label}", callback_data="pick_sport_filter")],
        [InlineKeyboardButton(f"💶 Unità stake: €{int(s.get('unit_value', 10))}", callback_data="pick_unit_value")],
        [InlineKeyboardButton(f"⏰ Anticipo kickoff: {s.get('min_hours_before', 1.0):.0f}h min", callback_data="pick_hours_before")],
        [InlineKeyboardButton(f"📉 Cap edge senza Pinnacle: {s.get('max_edge_no_sharp', 20.0):.0f}%", callback_data="pick_max_edge")],
        [InlineKeyboardButton("🔙 Home", callback_data="admin_home")],
    ]
    # Stima consumo crediti The Odds API
    interval   = s["scan_interval"]
    scan_day   = 15 // interval  # scan tra 07:00 e 22:00 = 15h di finestra
    credits_mo = scan_day * 2 * 31  # ~2 crediti per scan
    await fn(
        "⚙️ *Impostazioni*\n\n"
        f"📊 _Crediti The Odds API: ~{credits_mo} req/mese stimati su 500 disponibili_\n"
        f"🏓 _OddsPapi ping pong: ~{6*31} req/mese su 250 disponibili_\n\n"
        "Tocca un'opzione per modificarla:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
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

    # ── Scan ─────────────────────────────────────────────────────────────────────
    elif data == "admin_scan":
        await query.edit_message_text("🔍 Scansione in corso... attendere.")
        count = await run_signal_scan(context.application, manual=True)
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
        sig = db.get_signal(sig_id)
        db.update_signal_status(sig_id, result, result)
        # Registra nel bilancio
        if sig:
            db.record_balance_entry(sig, result)
        emoji  = "✅" if result == "won" else "❌"
        label  = "VINTO! 🎉" if result == "won" else "Perso."
        kb = [
            [InlineKeyboardButton("📋 Torna alla lista", callback_data="admin_list")],
            [InlineKeyboardButton("🔙 Home",             callback_data="admin_home")],
        ]
        await query.edit_message_text(
            f"{emoji} *{label}*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )
        if sig:
            sport_label = sig.get("sport_label", "🏓")
            bal = db.get_balance_stats()
            bal_str = f"€{bal['current_balance']:+.0f}" if bal["total_bets"] > 0 else "n/d"
            await query.message.reply_text(
                f"{emoji} *Risultato aggiornato*\n"
                f"{'━' * 20}\n"
                f"{sport_label} {sig['match']}\n"
                f"🎯 {sig['pick']} @ {sig['odds']}\n"
                f"📌 Stake: {sig['stake']}/5\n\n"
                f"{emoji} *{'VINTO!' if result == 'won' else 'Perso.'}*\n\n"
                f"💰 Bilancio: *{bal_str}* | ROI: *{bal['roi']:+.1f}%*",
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
            await query.answer("⚠️ Servono almeno 2 risultati per il grafico.", show_alert=True)
            return
        await query.answer("📈 Generazione grafico...")
        buf = genera_grafico_bilancio()
        kb = [[InlineKeyboardButton("🔙 Statistiche", callback_data="admin_stats")]]
        if buf:
            await query.message.reply_photo(
                photo=buf,
                caption=(
                    f"📊 *Bilancio segnali*\n"
                    f"{'━'*20}\n"
                    f"💵 Attuale: *€{bal['current_balance']:+.0f}*\n"
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
            "⚠️ *Reset risultati*\n\nVengono azzerati vinti/persi. I segnali rimangono.",
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

    # ── Scegli valore unità stake ─────────────────────────────────────────────
    elif data == "pick_unit_value":
        current = db.get_settings().get("unit_value", 10.0)
        opts = [1, 2, 5, 10, 20, 25, 50, 100]
        kb = []
        for v in opts:
            prefix = "✅ " if float(v) == current else ""
            kb.append([InlineKeyboardButton(f"{prefix}€{v} per unità", callback_data=f"set_unit_{v}")])
        kb.append([InlineKeyboardButton("🔙 Impostazioni", callback_data="admin_settings")])
        await query.edit_message_text(
            "💶 *Valore unità stake*\n\n"
            "Ogni segnale ha uno stake da 1 a 5 unità.\n"
            "Scegli quanto vale 1 unità in €:\n\n"
            "_Es. €10 → stake 3 = €30 a rischio_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_unit_"):
        val = float(data.replace("set_unit_", ""))
        db.set_setting("unit_value", val)
        await send_settings(query.edit_message_text)


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
            "📉 *Cap edge senza Pinnacle*\n\n"
            "Senza sharp book il calcolo dell'edge può essere gonfiato.\n"
            "Questo limite blocca i valori esagerati.\n\n"
            "20% è il punto di equilibrio consigliato.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("set_maxedge_"):
        val = float(data.replace("set_maxedge_", ""))
        db.set_setting("max_edge_no_sharp", val)
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
        next_run = datetime.datetime.now(ROME) + datetime.timedelta(hours=hours)
        _scheduler.add_job(
            lambda: _schedule_coro(lambda: run_signal_scan(app)),
            trigger=IntervalTrigger(hours=hours, timezone=ROME),
            id="signal_scan",
            replace_existing=True,
            next_run_time=next_run,
        )
        logger.info(f"⏰ Scheduler aggiornato: ogni {hours}h | prossimo scan: {next_run.strftime('%H:%M')}")

async def post_init(app: Application):
    global _scheduler, _main_loop
    hours      = db.get_settings()["scan_interval"]
    now        = datetime.datetime.now(ROME)
    _main_loop = asyncio.get_running_loop()   # loop principale, usato dai job per agganciarsi correttamente

    _scheduler = AsyncIOScheduler(timezone=ROME)

    # Scan segnali: parte subito (next_run_time=now) poi ripete ogni X ore
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: run_signal_scan(app)),
        trigger=IntervalTrigger(hours=hours, timezone=ROME),
        id="signal_scan",
        next_run_time=now + datetime.timedelta(seconds=5),  # 5 sec dopo boot
    )

    # Auto-risultati: ogni 30 minuti, prima esecuzione dopo 10 minuti
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: run_auto_results(app)),
        trigger=IntervalTrigger(minutes=30, timezone=ROME),
        id="auto_results",
        next_run_time=now + datetime.timedelta(minutes=10),
    )

    # Scan ping pong: ogni giorno alle 07:00 (CronTrigger, affidabile)
    from apscheduler.triggers.cron import CronTrigger
    _scheduler.add_job(
        lambda: _schedule_coro(lambda: run_pingpong_scan(app)),
        trigger=CronTrigger(hour=7, minute=0, timezone=ROME),
        id="pingpong_scan",
    )

    _scheduler.start()
    logger.info(f"⏰ Scheduler avviato — scan tennis ogni {hours}h | ping pong alle 07:00 | risultati ogni 30min")

# ── Core scan ────────────────────────────────────────────────────────────────────
async def run_signal_scan(app: Application, manual: bool = False) -> int:
    ora = datetime.datetime.now(ROME).hour
    sport_ov = getattr(run_signal_scan, "_sport_override", None)
    # Scan automatico tennis: solo tra 07:00 e 22:00
    if not manual and not sport_ov and not (7 <= ora <= 21):
        logger.info(f"Scan tennis saltato (ora {ora}:xx fuori finestra 07-22)")
        return 0
    logger.info("🔍 Avvio scan tennis..." if not sport_ov else "🏓 Avvio scan ping pong...")
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

    # Settings prima di tutto
    settings = db.get_settings()

    # Filtra per sport
    sport_filter = getattr(run_signal_scan, "_sport_override", None) or settings.get("sport_filter", "both")
    if sport_filter == "both":
        # Scan automatico: solo tennis. Ping pong ha job dedicato giornaliero.
        matches = [m for m in matches if m.get("sport") == "tennis"]
        logger.info(f"Scan tennis: {len(matches)} partite")
    else:
        matches = [m for m in matches if m.get("sport") == sport_filter]
        logger.info(f"Filtro sport '{sport_filter}': {len(matches)} partite rimaste")
    # Reset override
    if hasattr(run_signal_scan, "_sport_override"):
        del run_signal_scan._sport_override

    # Se nessuna partita reale avvisa l'admin (solo in orario diurno 07-23 per non spammare)
    real_matches = [m for m in matches if m.get("source") not in ("fallback",)]
    if not real_matches and (ODDS_KEY or os.environ.get("ODDSPAPI_KEY")):
        logger.info("Nessuna partita reale disponibile in questo momento")
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
            )
        return 0

    new_signals = 0

    for match in matches:
        try:
            signals = await analyzer.analyze(match, settings)
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
async def run_pingpong_scan(app: Application):
    """
    Scan ping pong giornaliero alle 07:00.
    Scarica le migliori 3 fixture del giorno (partite fino alle 19:00),
    analizza e invia i segnali. 1 sola chiamata API al giorno = ~3 req/giorno.
    """
    settings = db.get_settings()
    if settings.get("sport_filter") == "tennis":
        logger.info("Ping pong scan saltato (sport_filter=tennis)")
        return
    logger.info("🏓 Avvio scan ping pong giornaliero (ore 07:00)...")
    run_signal_scan._sport_override = "tabletennis"
    await run_signal_scan(app)
    logger.info("🏓 Scan ping pong completato — prossimo domani alle 07:00")



async def run_auto_results(app: Application):
    """
    Controlla le API per i risultati delle partite completate
    e aggiorna automaticamente i segnali winner pendenti.
    Over/Under rimane manuale (no dati set dalle API gratuite).
    """
    pending = db.get_signals_for_auto_result()
    # Considera solo segnali winner — over/under non aggiornabili automaticamente
    pending = [s for s in pending if s.get("signal_type") == "winner"]
    if not pending:
        return

    logger.info(f"🔄 Auto-risultati: controllo {len(pending)} segnali winner pendenti...")

    try:
        scores = await scraper.fetch_scores()
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
        # Cognome corrisponde (min 4 char per evitare falsi positivi)
        if a_parts and b_parts and len(a_parts[-1]) >= 4 and a_parts[-1] == b_parts[-1]:
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
            bal_stats = db.get_balance_stats()
            bal_str = f"€{bal_stats['current_balance']:+.0f}" if bal_stats["total_bets"] > 0 else "n/d"

            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"{emoji} *Risultato automatico!*\n"
                    f"{'━' * 22}\n"
                    f"{sig.get('sport_label','🏓')} {sig['match']}\n"
                    f"🎯 {sig['pick']} @ {sig['odds']}\n"
                    f"📌 Stake: {sig['stake']}/5\n\n"
                    f"{'✅ *VINTO!* 🎉' if result == 'won' else '❌ *Perso.*'}\n\n"
                    f"💰 Bilancio attuale: *{bal_str}*\n"
                    f"📊 W/L: {bal_stats['won']}V/{bal_stats['lost']}P | "
                    f"Win%: {bal_stats['winrate']}% | ROI: {bal_stats['roi']}%"
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
