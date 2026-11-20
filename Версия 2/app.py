import argparse
import hashlib
import json
import os
import sqlite3
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from planner import validate, transfers, allocate
from demo import handler, fixture


def connect(path):
    db=sqlite3.connect(path,timeout=20)
    db.row_factory=sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, body TEXT, sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, stamp TEXT, message TEXT);
    CREATE TABLE IF NOT EXISTS mock(body TEXT);
    """)
    return db


def request(base, path, token, data=None, key=None):
    headers={"Accept":"application/json"}
    if token:
        headers["Authorization"]="Bearer "+token
    if data is not None:
        headers.update({"Content-Type":"application/json","Idempotency-Key":key})
    req=Request(base+path,headers=headers,data=None if data is None else json.dumps(data).encode())
    with urlopen(req,timeout=15) as response:
        return json.load(response)


def cycle(db, base, token):
    snap=request(base,"/snapshot",token)
    validate(snap)
    applied=set(snap["applied_ids"])
    known=db.execute("select * from jobs").fetchall()
    # Wait until all previous orders appear in stock and the CRM's applied journal.
    if all(row["id"] in applied for row in known):
        orders=transfers(snap)+[allocate(snap["history"],b) for b in snap["arrivals"] if b["qty"]]
        with db:
            for o in orders:
                identity=(["transfer",snap["version"],o["from"],o["to"]] if o["kind"]=="transfer"
                          else ["allocation",o["batch_id"]])
                key=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                db.execute("insert or ignore into jobs(id,body) values(?,?)",(key,json.dumps(o,ensure_ascii=False)))
    count=0
    for row in db.execute("select * from jobs where sent=0 order by rowid").fetchall():
        if row["id"] not in applied:
            ack=request(base,"/orders",token,json.loads(row["body"]),row["id"])
            if ack.get("accepted") is not True or ack.get("id")!=row["id"]:
                raise ValueError("CRM не подтвердила идентификатор задания")
        with db:
            db.execute("update jobs set sent=1 where id=?",(row["id"],))
        count+=1
    waiting=sum(row["sent"] and row["id"] not in applied for row in known)
    return f'Снимок {snap["version"]}. Передано заданий: {count}.' + (f' Ожидают отражения в остатках: {waiting}.' if waiting else '')


def main():
    parser=argparse.ArgumentParser()
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--live",action="store_true")
    mode.add_argument("--snapshot",type=Path,help="JSON-снимок для учебной CRM")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--interval",type=int,default=None)
    parser.add_argument("--no-browser",action="store_true")
    args=parser.parse_args()
    period=args.interval or (900 if args.live else 15)
    if period<1:
        parser.error("Интервал должен быть положительным")
    root=Path(__file__).resolve().parent
    token=""
    if args.live:
        base=os.environ.get("CRM_URL","").rstrip("/")
        if urlparse(base).scheme!="https" or not urlparse(base).netloc:
            parser.error("Укажите HTTPS-адрес адаптера в CRM_URL")
        token=os.environ.get("CRM_TOKEN","")
        # Isolate journals for different CRM endpoints.
        filename="live-"+hashlib.sha256(base.encode()).hexdigest()[:16]+".sqlite"
    else:
        initial=fixture()
        filename="demo.sqlite"
        if args.snapshot:
            try:
                initial=json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
                validate(initial)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                parser.error(f"Невозможно загрузить снимок CRM: {exc}")
            digest=hashlib.sha256(json.dumps(initial,sort_keys=True).encode()).hexdigest()[:16]
            filename=f"demo-{digest}.sqlite"
    path=root/filename
    # Bind before starting any side effects.
    class Panel(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            if self.path=="/":
                body=(root/"dashboard.html").read_bytes()
                mime="text/html; charset=utf-8"
            elif self.path=="/status":
                db=connect(path)
                try:
                    jobs=[dict(json.loads(r["body"]),sent=bool(r["sent"])) for r in db.execute("select * from jobs order by rowid desc limit 100")]
                    logs=[dict(r) for r in db.execute("select stamp,message from runs order by id desc limit 10")]
                finally:
                    db.close()
                body=json.dumps(dict(mode="CRM сети" if args.live else ("Учебная CRM · "+initial["version"]),period=period,jobs=jobs,logs=logs)).encode()
                mime="application/json"
            else:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type",mime)
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            self.wfile.write(body)
    panel=ThreadingHTTPServer(("127.0.0.1",args.port),Panel)
    if not args.live:
        mock=sqlite3.connect(path,check_same_thread=False)
        connect(path).close()
        with mock:
            if not mock.execute("select 1 from mock").fetchone():
                mock.execute("insert into mock values(?)",(json.dumps(initial),))
        crm=ThreadingHTTPServer(("127.0.0.1",0),handler(mock))
        Thread(target=crm.serve_forever,daemon=True).start()
        base="http://127.0.0.1:"+str(crm.server_port)
    stopped=Event()
    def worker():
        db=connect(path)
        while not stopped.is_set():
            try:
                message=cycle(db,base,token)
            except Exception as e:
                message="Ошибка обмена: "+str(e)+". Повтор при следующей проверке."
            with db:
                db.execute("insert into runs(stamp,message) values(?,?)",(datetime.now(timezone.utc).isoformat(),message))
                db.execute("delete from runs where id < (select max(id)-999 from runs)")
            stopped.wait(period)
        db.close()
    Thread(target=worker,daemon=True).start()
    address=f"http://127.0.0.1:{panel.server_port}"
    print("Панель управления:",address,flush=True)
    if not args.no_browser:
        webbrowser.open(address)
    try:
        panel.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        panel.server_close()

if __name__=="__main__":
    main()
