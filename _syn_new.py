
class Cache:
    def compute_total(self, items):
        total = 0
        for item in items:
            total += item.price * item.quantity
        return round(total, 2)
