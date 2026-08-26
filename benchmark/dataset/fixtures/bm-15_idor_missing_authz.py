"""Invoice viewer for logged-in customers."""
def get_invoice(invoice_id, db):
    return db.query("SELECT * FROM invoices WHERE id = %s", (invoice_id,))

def show_invoice(request, db):
    invoice = get_invoice(request.args.get("invoice_id"), db)
    if invoice is None:
        return {"error": "not found"}, 404
    return {"amount": invoice["amount"],
            "line_items": invoice["items"]}, 200

def list_my_invoices(user_id, db):
    return db.query_all(
        "SELECT * FROM invoices WHERE owner_id = %s ORDER BY created_at",
        (user_id,))
