"""Product search over the storefront catalog."""
def search_products(term, db, limit=20):
    rows = db.query_all(
        "SELECT id, title, price FROM products "
        "WHERE title ILIKE %s AND active = TRUE ORDER BY price LIMIT %s",
        ("%" + term + "%", limit))
    return rows

def get_product(product_id, db):
    return db.query(
        "SELECT id, title, description, price FROM products WHERE id = %s",
        (product_id,))

def format_price(product):
    return "${:.2f}".format(product["price"] / 100.0)
