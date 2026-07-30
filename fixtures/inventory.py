class InsufficientStock(Exception):
    pass

def reserve(stock: dict, sku: str, qty: int) -> dict:
    if qty <= 0:
        raise ValueError("qty must be positive")
    if sku not in stock:
        raise KeyError(sku)
    if stock[sku] < qty:
        raise InsufficientStock(f"{sku}: have {stock[sku]}, need {qty}")
    return {**stock, sku: stock[sku] - qty}
