import json
from http.server import BaseHTTPRequestHandler
from threading import Lock


def fixture():
    stores = ["Арбат", "Тверская", "Сокол"]
    rows = []
    for sku, price, category, balances in [
        ("Джинсы · 32", 5900, "Джинсы 32", [(14,.25),(1,1.1),(4,.25)]),
        ("Рубашка · M", 4200, "Рубашки M", [(1,.85),(12,.2),(4,.3)])]:
        for store, (stock, rate) in zip(stores, balances):
            rows.append(dict(sku=sku, price=price, category=category, store=store, stock=stock,
                             rate=rate, season="осень", incoming=0, days_left=14, discount=.35))
    return dict(version="demo-1", applied_ids=[], inventory=rows,
                routes=[{"from":a,"to":b,"cost":550,"days":1} for a in stores for b in stores if a!=b],
                history=[dict(store=r["store"], category=r["category"], season="осень",
                              units=round(r["rate"]*60), available_days=60) for r in rows],
                arrivals=[dict(batch_id="Осень-01", sku="Новая модель джинсов · 32",
                               category="Джинсы 32", season="осень", qty=60, stores=stores)])


def handler(db):
    lock = Lock()
    def load():
        return json.loads(db.execute("select body from mock").fetchone()[0])
    class Demo(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def reply(self, obj, code=200):
            self.send_response(code)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(obj,ensure_ascii=False).encode())
        def do_GET(self):
            if self.path != "/snapshot":
                return self.reply({},404)
            with lock:
                self.reply(load())
        def do_POST(self):
            if self.path != "/orders":
                return self.reply({},404)
            order=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                s=load()
                key=self.headers["Idempotency-Key"]
                if key not in s["applied_ids"]:
                    if order["kind"]=="transfer":
                        for l in order["lines"]:
                            src=next(r for r in s["inventory"] if (r["store"],r["sku"])==(order["from"],l["sku"]))
                            dst=next(r for r in s["inventory"] if (r["store"],r["sku"])==(order["to"],l["sku"]))
                            if src["stock"]<l["qty"]:
                                return self.reply({"error":"insufficient stock"},409)
                            src["stock"]-=l["qty"]
                            dst["incoming"]+=l["qty"]
                    else:
                        s["arrivals"]=[b for b in s["arrivals"] if b["batch_id"]!=order["batch_id"]]
                    s["applied_ids"].append(key)
                    s["version"]="demo-"+str(1+len(s["applied_ids"]))
                    with db:
                        db.execute("update mock set body=?",(json.dumps(s),))
                self.reply({"accepted":True,"id":key})
    return Demo
