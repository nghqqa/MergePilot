def calculate_total(items):
    """Sum prices with tax."""
    subtotal = sum(item["price"] for item in items)
    tax = subtotal * 0.08
    return round(subtotal + tax, 2)

def format_receipt(total, items):
    lines = [f"{'Item':<30} {'Price':>10}"]
    for item in items:
        lines.append(f"{item['name']:<30} ${item['price']:>9.2f}")
    lines.append(f"{'Total':<30} ${total:>9.2f}")
    return "\n".join(lines)
