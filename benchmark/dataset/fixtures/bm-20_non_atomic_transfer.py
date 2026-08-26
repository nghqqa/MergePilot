"""Wallet service handling transfers between accounts."""
def transfer(src_id, dst_id, amount, db):
    src = db.query("SELECT balance FROM accounts WHERE id = %s", (src_id,))
    if src["balance"] < amount:
        return {"error": "insufficient funds"}, 400
    db.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s",
               (amount, src_id))
    db.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s",
               (amount, dst_id))
    return {"status": "sent"}, 200

def get_balance(account_id, db):
    row = db.query("SELECT balance FROM accounts WHERE id = %s", (account_id,))
    return row["balance"]
