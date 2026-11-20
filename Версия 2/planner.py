"""Planning functions shared by the CRM adapter and the demonstration."""
import math
from collections import defaultdict


def validate(snapshot):
    def number(value, *, integer=False):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (integer and int(value) != value):
            raise ValueError("Некорректное число в снимке")
    def identifier(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Пустой идентификатор")
    def unique(items):
        if len(set(items)) != len(items):
            raise ValueError("Повторяющийся идентификатор")
    identifier(snapshot["version"])
    if not isinstance(snapshot["applied_ids"], list):
        raise ValueError("applied_ids должен быть массивом")
    for key in snapshot["applied_ids"]:
        identifier(key)
    rows = snapshot["inventory"]
    unique([(r["sku"], r["store"]) for r in rows])
    for r in rows:
        for k in ("sku", "store", "category", "season"):
            identifier(r[k])
        for k in ("stock", "incoming", "days_left"):
            number(r[k], integer=True)
        for k in ("price", "discount", "rate"):
            number(r[k])
        if r["discount"] > 1:
            raise ValueError("Скидка должна быть долей от 0 до 1")
    unique([(r["from"], r["to"]) for r in snapshot["routes"]])
    for r in snapshot["routes"]:
        identifier(r["from"])
        identifier(r["to"])
        if r["from"] == r["to"]:
            raise ValueError("Маршрут в тот же магазин")
        number(r["days"], integer=True)
        number(r["cost"])
    for h in snapshot["history"]:
        for k in ("store", "category", "season"):
            identifier(h[k])
        for k in ("units", "available_days"):
            number(h[k])
        if h["units"] and not h["available_days"]:
            raise ValueError("Продажи без дней наличия")
    unique([b["batch_id"] for b in snapshot["arrivals"]])
    for b in snapshot["arrivals"]:
        for k in ("batch_id", "sku", "category", "season"):
            identifier(b[k])
        number(b["qty"], integer=True)
        if not b["stores"]:
            raise ValueError("Пустой список магазинов")
        for store in b["stores"]:
            identifier(store)
        unique(b["stores"])


def transfers(snapshot):
    rows = {(r["sku"], r["store"]): r for r in snapshot["inventory"]}
    extra, need = {}, {}
    for key, r in rows.items():
        balance = r["stock"] + r["incoming"] - r["rate"] * r["days_left"]
        extra[key] = min(r["stock"], max(0, math.floor(balance)))
        need[key] = max(0, math.floor(-balance))
    routes = list(snapshot["routes"])
    skus = sorted({k[0] for k in rows})
    output = []
    while routes:
        options = []
        for route in routes:
            lines, gain = [], 0
            for sku in skus:
                src, dst = (sku, route["from"]), (sku, route["to"])
                if src not in rows or dst not in rows:
                    continue
                receiver, donor = rows[dst], rows[src]
                capacity = math.floor(max(0, receiver["days_left"] - route["days"]) * receiver["rate"])
                qty = min(extra[src], need[dst], capacity)
                delta = receiver["price"] - donor["price"] * (1 - donor["discount"])
                if qty and delta > 0:
                    lines.append({"sku": sku, "qty": qty})
                    gain += qty * delta
            net = round(gain - route["cost"], 2)
            if lines and net > 0:
                options.append((net, route, lines))
        if not options:
            break
        net, route, lines = max(options, key=lambda o: o[0])
        routes.remove(route)
        output.append({"kind": "transfer", **route, "lines": lines, "estimated_gain": net})
        for line in lines:
            extra[line["sku"], route["from"]] -= line["qty"]
            need[line["sku"], route["to"]] -= line["qty"]
    return output


def allocate(history, batch):
    # Estimate sales per in-stock day. Shrink sparse observations to the pooled rate.
    samples = defaultdict(lambda: [0, 0])
    for h in history:
        if (h["category"], h["season"]) == (batch["category"], batch["season"]) and h["store"] in batch["stores"]:
            samples[h["store"]][0] += h["units"]
            samples[h["store"]][1] += h["available_days"]
    days = sum(v[1] for v in samples.values())
    pooled = sum(v[0] for v in samples.values()) / days if days else 0
    stores = sorted(batch["stores"])
    rates = [(samples[s][0] + 7 * pooled) / (samples[s][1] + 7) for s in stores]
    total = sum(rates)
    quotas = [batch["qty"] * (r / total if total else 1 / len(stores)) for r in rates]
    counts = [math.floor(q) for q in quotas]
    for i in sorted(range(len(stores)), key=lambda i: quotas[i] - counts[i], reverse=True)[:batch["qty"] - sum(counts)]:
        counts[i] += 1
    return {"kind": "allocation", "batch_id": batch["batch_id"], "sku": batch["sku"],
            "method": "История продаж за дни наличия" if total else "Равномерно: недостаточно продаж",
            "lines": [{"store": s, "qty": n} for s, n in zip(stores, counts) if n]}
