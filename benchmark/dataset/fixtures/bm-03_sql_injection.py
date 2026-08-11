import sqlite3

def search_products(keyword):
    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM products WHERE name LIKE '%{keyword}%'"
    cursor.execute(query)
    return cursor.fetchall()

def get_order(order_id):
    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM orders WHERE id = {order_id}"
    cursor.execute(query)
    return cursor.fetchone()
