from database import Database


class BettingManager:
    def __init__(self, db: Database):
        self.db = db

    def place_bet(self, user_id: int, match_id: int, player_num: int, amount: float) -> dict:
        """
        Places a bet. Returns dict with ok=True/False and details.
        """
        match = self.db.get_match(match_id)
        if not match:
            return {"ok": False, "error": "Partita non trovata."}
        if match["status"] != "open":
            return {"ok": False, "error": "Questa partita è già chiusa."}
        if amount <= 0:
            return {"ok": False, "error": "L'importo deve essere maggiore di 0."}

        odds = match["odds1"] if player_num == 1 else match["odds2"]
        chosen = match["player1"] if player_num == 1 else match["player2"]

        # Deduct balance (atomic check + deduct)
        ok = self.db.deduct_credits(user_id, amount)
        if not ok:
            return {"ok": False, "error": "Saldo insufficiente."}

        self.db.place_bet(user_id, match_id, player_num, amount, odds)

        balance = self.db.get_balance(user_id)
        potential = round(amount * odds, 2)
        return {
            "ok": True,
            "match": f"{match['player1']} vs {match['player2']}",
            "chosen": chosen,
            "odds": odds,
            "potential": potential,
            "balance": balance,
        }

    def settle_match(self, match_id: int, winner: int) -> dict | None:
        """
        Closes a match and pays out winning bets.
        Returns summary dict or None if match not found/already closed.
        """
        match = self.db.get_match(match_id)
        if not match or match["status"] != "open":
            return None

        bets = self.db.get_bets_for_match(match_id)
        self.db.close_match(match_id, winner)

        settled = 0
        paid_out = 0.0

        for bet in bets:
            if bet["player_num"] == winner:
                payout = round(bet["amount"] * bet["odds"], 2)
                self.db.add_credits(bet["user_id"], payout)
                self.db.settle_bet(bet["id"], "won")
                paid_out += payout
            else:
                self.db.settle_bet(bet["id"], "lost")
            settled += 1

        return {"settled": settled, "paid_out": paid_out}
