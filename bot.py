import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from database import Database
from betting import BettingManager

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.environ.get("ADMIN_IDS", "0").split(",")))

db = Database()
bm = BettingManager(db)


# ─── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or user.first_name)

    balance = db.get_balance(user.id)
    text = (
        f"🏓 *Benvenuto nel PingPong Bet Bot*, {user.first_name}!\n\n"
        f"💰 Il tuo saldo: *{balance:.2f} crediti*\n\n"
        "Usa i comandi qui sotto per iniziare:"
    )
    keyboard = [
        [InlineKeyboardButton("📋 Partite disponibili", callback_data="matches")],
        [InlineKeyboardButton("💰 Il mio saldo", callback_data="balance"),
         InlineKeyboardButton("📊 Le mie scommesse", callback_data="mybets")],
        [InlineKeyboardButton("🏆 Classifica", callback_data="leaderboard")],
    ]
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── /saldo ────────────────────────────────────────────────────────────────────
async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or user.first_name)
    balance = db.get_balance(user.id)
    await update.message.reply_text(
        f"💰 Il tuo saldo attuale: *{balance:.2f} crediti*",
        parse_mode="Markdown"
    )


# ─── /partite ──────────────────────────────────────────────────────────────────
async def partite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches = db.get_open_matches()
    if not matches:
        await update.message.reply_text("⚠️ Nessuna partita disponibile al momento.")
        return
    keyboard = []
    for m in matches:
        label = f"🏓 {m['player1']} vs {m['player2']} | Q: {m['odds1']}x / {m['odds2']}x"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"bet_{m['id']}")])
    await update.message.reply_text(
        "📋 *Partite disponibili:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── /classifica ───────────────────────────────────────────────────────────────
async def classifica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = db.get_leaderboard()
    if not top:
        await update.message.reply_text("🏆 Classifica vuota.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *Top 10 Classifica*\n"]
    for i, row in enumerate(top[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {row['username']} — {row['balance']:.2f} crediti")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── ADMIN: /aggiungi_partita ───────────────────────────────────────────────────
async def aggiungi_partita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Non hai i permessi.")
        return
    # usage: /aggiungi_partita Tizio Caio 1.8 2.1
    args = context.args
    if len(args) != 4:
        await update.message.reply_text(
            "Uso: /aggiungi_partita <giocatore1> <giocatore2> <quota1> <quota2>\n"
            "Esempio: /aggiungi_partita Tizio Caio 1.80 2.10"
        )
        return
    p1, p2 = args[0], args[1]
    try:
        o1, o2 = float(args[2]), float(args[3])
    except ValueError:
        await update.message.reply_text("❌ Le quote devono essere numeri decimali.")
        return
    match_id = db.add_match(p1, p2, o1, o2)
    await update.message.reply_text(
        f"✅ Partita aggiunta! ID: `{match_id}`\n"
        f"🏓 {p1} (x{o1}) vs {p2} (x{o2})",
        parse_mode="Markdown"
    )


# ─── ADMIN: /chiudi_partita ────────────────────────────────────────────────────
async def chiudi_partita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Non hai i permessi.")
        return
    # usage: /chiudi_partita <match_id> <1|2>
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Uso: /chiudi_partita <match_id> <1|2>\n"
            "1 = vince giocatore 1, 2 = vince giocatore 2"
        )
        return
    try:
        match_id = int(args[0])
        winner = int(args[1])
        assert winner in (1, 2)
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Parametri non validi.")
        return
    results = bm.settle_match(match_id, winner)
    if results is None:
        await update.message.reply_text("❌ Partita non trovata o già chiusa.")
        return
    await update.message.reply_text(
        f"✅ Partita {match_id} chiusa. Vince giocatore {winner}.\n"
        f"💸 Scommesse risolte: {results['settled']} | Pagati: {results['paid_out']:.2f} crediti",
        parse_mode="Markdown"
    )


# ─── ADMIN: /recredita ─────────────────────────────────────────────────────────
async def recredita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Non hai i permessi.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Uso: /recredita <user_id> <importo>")
        return
    try:
        uid = int(args[0])
        amount = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Parametri non validi.")
        return
    db.add_credits(uid, amount)
    await update.message.reply_text(f"✅ Aggiunti {amount:.2f} crediti a user {uid}.")


# ─── /mybets ───────────────────────────────────────────────────────────────────
async def mybets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bets = db.get_user_bets(user.id)
    if not bets:
        await update.message.reply_text("📭 Non hai ancora scommesse.")
        return
    lines = ["📊 *Le tue scommesse:*\n"]
    for b in bets[-10:]:
        status_icon = {"pending": "⏳", "won": "✅", "lost": "❌"}.get(b["status"], "❓")
        lines.append(
            f"{status_icon} {b['player1']} vs {b['player2']} → "
            f"su *{b['chosen_player']}* | {b['amount']:.2f}cr | quota x{b['odds']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    if data == "matches":
        matches = db.get_open_matches()
        if not matches:
            await query.edit_message_text("⚠️ Nessuna partita disponibile.")
            return
        keyboard = []
        for m in matches:
            label = f"🏓 {m['player1']} vs {m['player2']} | Q: {m['odds1']}x / {m['odds2']}x"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"bet_{m['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Indietro", callback_data="back_home")])
        await query.edit_message_text(
            "📋 *Partite disponibili — clicca per scommettere:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "balance":
        balance = db.get_balance(user.id)
        await query.edit_message_text(
            f"💰 Il tuo saldo: *{balance:.2f} crediti*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Indietro", callback_data="back_home")]])
        )

    elif data == "mybets":
        bets = db.get_user_bets(user.id)
        if not bets:
            text = "📭 Non hai ancora scommesse."
        else:
            lines = ["📊 *Le tue ultime scommesse:*\n"]
            for b in bets[-8:]:
                status_icon = {"pending": "⏳", "won": "✅", "lost": "❌"}.get(b["status"], "❓")
                lines.append(
                    f"{status_icon} {b['player1']} vs {b['player2']} → "
                    f"su *{b['chosen_player']}* | {b['amount']:.2f}cr x{b['odds']}"
                )
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Indietro", callback_data="back_home")]])
        )

    elif data == "leaderboard":
        top = db.get_leaderboard()
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 *Top 10 Classifica*\n"]
        for i, row in enumerate(top[:10]):
            medal = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{medal} {row['username']} — {row['balance']:.2f} crediti")
        await query.edit_message_text(
            "\n".join(lines) if top else "🏆 Classifica vuota.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Indietro", callback_data="back_home")]])
        )

    elif data.startswith("bet_"):
        match_id = int(data.split("_")[1])
        match = db.get_match(match_id)
        if not match:
            await query.edit_message_text("❌ Partita non trovata.")
            return
        keyboard = [
            [InlineKeyboardButton(f"🟢 {match['player1']} (x{match['odds1']})", callback_data=f"choose_{match_id}_1")],
            [InlineKeyboardButton(f"🔵 {match['player2']} (x{match['odds2']})", callback_data=f"choose_{match_id}_2")],
            [InlineKeyboardButton("🔙 Indietro", callback_data="matches")]
        ]
        await query.edit_message_text(
            f"🏓 *{match['player1']}* vs *{match['player2']}*\n\nSu chi vuoi scommettere?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("choose_"):
        _, match_id, player_num = data.split("_")
        match_id, player_num = int(match_id), int(player_num)
        # store pending bet choice in user_data
        context.user_data["pending_bet"] = {"match_id": match_id, "player_num": player_num}
        match = db.get_match(match_id)
        chosen = match["player1"] if player_num == 1 else match["player2"]
        odds = match["odds1"] if player_num == 1 else match["odds2"]
        balance = db.get_balance(user.id)
        await query.edit_message_text(
            f"💸 Hai scelto *{chosen}* (x{odds})\n"
            f"💰 Saldo disponibile: *{balance:.2f} crediti*\n\n"
            f"Scrivi l'importo da scommettere (es. `50`):",
            parse_mode="Markdown"
        )

    elif data == "back_home":
        balance = db.get_balance(user.id)
        keyboard = [
            [InlineKeyboardButton("📋 Partite disponibili", callback_data="matches")],
            [InlineKeyboardButton("💰 Il mio saldo", callback_data="balance"),
             InlineKeyboardButton("📊 Le mie scommesse", callback_data="mybets")],
            [InlineKeyboardButton("🏆 Classifica", callback_data="leaderboard")],
        ]
        await query.edit_message_text(
            f"🏓 *PingPong Bet Bot*\n\n💰 Saldo: *{balance:.2f} crediti*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ─── AMOUNT INPUT ──────────────────────────────────────────────────────────────
async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or user.first_name)
    pending = context.user_data.get("pending_bet")
    if not pending:
        return  # not waiting for an amount

    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        assert amount > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ Inserisci un importo valido (es. `50`).", parse_mode="Markdown")
        return

    match_id = pending["match_id"]
    player_num = pending["player_num"]
    result = bm.place_bet(user.id, match_id, player_num, amount)

    if result["ok"]:
        context.user_data.pop("pending_bet", None)
        await update.message.reply_text(
            f"✅ *Scommessa piazzata!*\n\n"
            f"🏓 {result['match']}\n"
            f"🎯 Giocatore: *{result['chosen']}* (x{result['odds']})\n"
            f"💸 Importo: *{amount:.2f} crediti*\n"
            f"🏆 Vincita potenziale: *{result['potential']:.2f} crediti*\n"
            f"💰 Saldo rimasto: *{result['balance']:.2f} crediti*",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ {result['error']}")


# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN non impostato!")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("partite", partite))
    app.add_handler(CommandHandler("classifica", classifica))
    app.add_handler(CommandHandler("mybets", mybets))
    app.add_handler(CommandHandler("aggiungi_partita", aggiungi_partita))
    app.add_handler(CommandHandler("chiudi_partita", chiudi_partita))
    app.add_handler(CommandHandler("recredita", recredita))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount))

    logger.info("Bot avviato...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
