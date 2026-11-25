import os, re, sqlite3, subprocess, time, uuid
from flask import Flask, g, jsonify, request, send_from_directory, abort

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(BASE, "goswift.db"))
MEDIA = os.environ.get("MEDIA_DIR", os.path.join(BASE, "media"))  # swap save_media() for S3/Spaces in prod
CFG = {"service_fee_pct": 5, "delivery_fee_ugx": 3000, "currency": "UGX",
       "durations": [6, 24, 72],
       "boost_fees": {"extend": 1000, "pin": 2000, "sponsor": 5000},   # UGX per 6h step
       "plans": {"free": {"ads": 3}, "pro": {"ads": 20, "price": 25000}, "business": {"ads": 200, "price": 70000}}}
FLOW = ["placed", "confirmed", "preparing", "out_for_delivery", "delivered"]
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
os.makedirs(MEDIA, exist_ok=True)

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db
@app.teardown_appcontext
def close(_):
    d = g.pop("db", None); d and d.close()
def q(sql, a=(), one=False):
    r = db().execute(sql, a).fetchall(); return (r[0] if r else None) if one else r
def run(sql, a=()):
    c = db().execute(sql, a); db().commit(); return c.lastrowid
now = lambda: int(time.time())

def me():  # STARTER AUTH: trusts X-User-Id. Replace with phone OTP + signed tokens before launch.
    u = q("SELECT * FROM users WHERE id=?", (request.headers.get("X-User-Id", 0),), True)
    if not u: abort(401)
    return u
def merchant_of(u):
    m = q("SELECT * FROM merchants WHERE user_id=?", (u["id"],), True)
    if not m: abort(403)
    return m

def save_media(f):
    """Save upload, then shrink to 480p H.264 (low data). Returns public URL."""
    name = uuid.uuid4().hex; raw = os.path.join(MEDIA, name + ".raw"); out = os.path.join(MEDIA, name + ".mp4")
    f.save(raw)
    try:
        subprocess.run(["ffmpeg", "-y", "-i", raw, "-t", "30", "-vf", "scale=-2:480", "-c:v", "libx264", "-crf", "30",
                        "-preset", "veryfast", "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", out],
                       check=True, capture_output=True, timeout=120)
        os.remove(raw)
    except Exception:
        os.replace(raw, out)  # ffmpeg missing: keep original (move compression to a background worker later)
    return "/media/" + name + ".mp4"

@app.get("/")
def index(): return app.send_static_file("index.html")

@app.get("/media/<path:f>")
def media(f):
    r = send_from_directory(MEDIA, f, conditional=True)
    r.headers["Cache-Control"] = "public, max-age=31536000, immutable"; return r

@app.get("/api/config")
def config(): return jsonify(CFG)

@app.post("/api/register")
def register():
    d = request.json; phone = re.sub(r"\D", "", d.get("phone", ""))
    if not d.get("name") or len(phone) < 9: return jsonify(error="Enter your name and phone number"), 400
    role = d.get("role", "consumer")
    u = q("SELECT * FROM users WHERE phone=?", (phone,), True)
    uid = u["id"] if u else run("INSERT INTO users(name,phone,role) VALUES(?,?,?)", (d["name"], phone, role))
    if role == "merchant" and not q("SELECT 1 FROM merchants WHERE user_id=?", (uid,), True):
        run("INSERT INTO merchants(user_id,shop_name,area,whatsapp) VALUES(?,?,?,?)",
            (uid, d.get("shop_name") or d["name"] + "'s shop", d.get("area", ""), phone))
    return jsonify(id=uid, name=d["name"], role=role)

@app.get("/api/me")
def profile():
    u = me(); m = q("SELECT * FROM merchants WHERE user_id=?", (u["id"],), True)
    return jsonify(user=dict(u), merchant=dict(m) if m else None)

AD_SQL = """SELECT a.id,a.title,a.description,a.price_ugx,a.video_url,a.poster_url,a.expires_at,a.created_at,
  a.pinned_until>:t AS pinned, a.sponsored_until>:t AS sponsored, m.shop_name, m.area, m.whatsapp
  FROM ads a JOIN merchants m ON m.id=a.merchant_id WHERE a.expires_at>:t"""
@app.get("/api/ads")  # expired ads never leave the DB layer
def ads():
    rows = q(AD_SQL + " ORDER BY sponsored DESC, pinned DESC, a.created_at DESC LIMIT 30", {"t": now()})
    return jsonify([dict(r) for r in rows])

@app.post("/api/ads")
def create_ad():
    m = merchant_of(me()); f = request.form
    hours = int(f.get("duration_hours", 24))
    if hours not in CFG["durations"]: return jsonify(error="Pick 6 hours, 24 hours or 3 days"), 400
    live = q("SELECT COUNT(*) c FROM ads WHERE merchant_id=? AND expires_at>?", (m["id"], now()), True)["c"]
    if live >= CFG["plans"][m["plan"]]["ads"]: return jsonify(error="Live ad limit reached. Upgrade your plan."), 402
    video = save_media(request.files["video"]) if "video" in request.files else None
    t = now(); aid = run("""INSERT INTO ads(merchant_id,title,description,price_ugx,video_url,duration_hours,created_at,expires_at)
        VALUES(?,?,?,?,?,?,?,?)""", (m["id"], f["title"], f.get("description", ""), int(f["price_ugx"]), video, hours, t, t + hours * 3600))
    return jsonify(id=aid, expires_at=t + hours * 3600)

@app.post("/api/ads/<int:aid>/boost")  # kind: extend | pin | sponsor ; payment stub -> wire MoMo/Airtel callback
def boost(aid):
    m = merchant_of(me()); kind = request.json["kind"]; steps = int(request.json.get("steps", 1))
    ad = q("SELECT * FROM ads WHERE id=? AND merchant_id=?", (aid, m["id"]), True) or abort(404)
    fee = CFG["boost_fees"][kind] * steps; hrs = 6 * steps; t = now()
    run("INSERT INTO boosts(ad_id,kind,hours,fee_ugx,paid,created_at) VALUES(?,?,?,?,1,?)", (aid, kind, hrs, fee, t))
    col = {"extend": "expires_at", "pin": "pinned_until", "sponsor": "sponsored_until"}[kind]
    base = max(ad[col], t); run(f"UPDATE ads SET {col}=? WHERE id=?", (base + hrs * 3600, aid))
    return jsonify(fee_ugx=fee)

@app.post("/api/ads/<int:aid>/view")
def view(aid):
    run("UPDATE ads SET views=views+1 WHERE id=?", (aid,)); return "", 204

@app.post("/api/chats")
def open_chat():
    u = me(); ad = q("SELECT * FROM ads WHERE id=?", (request.json["ad_id"],), True) or abort(404)
    c = q("SELECT id FROM chats WHERE ad_id=? AND buyer_id=?", (ad["id"], u["id"]), True)
    cid = c["id"] if c else run("INSERT INTO chats(ad_id,buyer_id,merchant_id,ad_title) VALUES(?,?,?,?)", (ad["id"], u["id"], ad["merchant_id"], ad["title"]))
    run("UPDATE ads SET taps=taps+1 WHERE id=?", (ad["id"],)); return jsonify(id=cid)

@app.get("/api/chats")
def chats():
    u = me(); m = q("SELECT id FROM merchants WHERE user_id=?", (u["id"],), True)
    rows = q("""SELECT c.id,c.ad_title,(SELECT body FROM messages WHERE chat_id=c.id ORDER BY id DESC LIMIT 1) last
        FROM chats c WHERE c.buyer_id=? OR c.merchant_id=? ORDER BY c.id DESC""", (u["id"], m["id"] if m else -1))
    return jsonify([dict(r) for r in rows])

def chat_access(cid):
    u = me(); c = q("SELECT c.*,m.user_id muid FROM chats c JOIN merchants m ON m.id=c.merchant_id WHERE c.id=?", (cid,), True) or abort(404)
    if u["id"] not in (c["buyer_id"], c["muid"]): abort(403)
    return u
@app.route("/api/chats/<int:cid>/messages", methods=["GET", "POST"])
def messages(cid):
    u = chat_access(cid)
    if request.method == "POST":
        run("INSERT INTO messages(chat_id,sender_id,body,created_at) VALUES(?,?,?,?)", (cid, u["id"], request.json["body"][:500], now()))
    rows = q("SELECT id,sender_id,body,created_at FROM messages WHERE chat_id=? AND id>? ORDER BY id", (cid, request.args.get("after", 0)))
    return jsonify([dict(r) for r in rows])

@app.post("/api/orders")
def place_order():
    u = me(); d = request.json; qty = max(1, int(d.get("qty", 1)))
    ad = q("SELECT * FROM ads WHERE id=? AND expires_at>?", (d["ad_id"], now()), True)
    if not ad: return jsonify(error="This flash ad has ended"), 410
    items = ad["price_ugx"] * qty; fee = round(items * CFG["service_fee_pct"] / 100); dlv = CFG["delivery_fee_ugx"]
    oid = run("""INSERT INTO orders(ad_id,ad_title,buyer_id,merchant_id,qty,item_total_ugx,service_fee_ugx,delivery_fee_ugx,total_ugx,address,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (ad["id"], ad["title"], u["id"], ad["merchant_id"], qty, items, fee, dlv, items + fee + dlv, d.get("address", ""), now(), now()))
    run("INSERT INTO order_events(order_id,status,note,created_at) VALUES(?, 'placed','Order sent to seller',?)", (oid, now()))
    return jsonify(id=oid, total_ugx=items + fee + dlv)

@app.get("/api/orders")
def orders():
    u = me(); m = q("SELECT id FROM merchants WHERE user_id=?", (u["id"],), True)
    out = []
    for o in q("SELECT * FROM orders WHERE buyer_id=? OR merchant_id=? ORDER BY id DESC", (u["id"], m["id"] if m else -1)):
        d = dict(o); d["is_seller"] = bool(m and m["id"] == o["merchant_id"])
        d["events"] = [dict(e) for e in q("SELECT status,note,created_at FROM order_events WHERE order_id=? ORDER BY id", (o["id"],))]
        out.append(d)
    return jsonify(out)

@app.post("/api/orders/<int:oid>/status")  # seller moves order forward, or buyer cancels while 'placed'
def order_status(oid):
    u = me(); o = q("SELECT * FROM orders WHERE id=?", (oid,), True) or abort(404)
    m = q("SELECT id FROM merchants WHERE user_id=?", (u["id"],), True); seller = bool(m and m["id"] == o["merchant_id"])
    st = request.json["status"]
    ok = (seller and st in FLOW[1:] + ["cancelled"]) or (u["id"] == o["buyer_id"] and st == "cancelled" and o["status"] == "placed")
    if not ok: abort(403)
    run("UPDATE orders SET status=?,updated_at=? WHERE id=?", (st, now(), oid))
    run("INSERT INTO order_events(order_id,status,note,created_at) VALUES(?,?,?,?)", (oid, st, request.json.get("note", ""), now()))
    return jsonify(ok=True)

@app.post("/api/plan")  # SaaS subscription stub: confirm payment via webhook in prod
def plan():
    m = merchant_of(me()); p = request.json["plan"]
    if p not in CFG["plans"]: abort(400)
    run("UPDATE merchants SET plan=?,plan_expires_at=? WHERE id=?", (p, now() + 30 * 86400, m["id"])); return jsonify(plan=p)

@app.get("/api/analytics")  # pro/business only
def analytics():
    m = merchant_of(me())
    if m["plan"] == "free": return jsonify(error="Upgrade to Pro for analytics"), 402
    rows = q("SELECT title,views,taps,(SELECT COUNT(*) FROM orders o WHERE o.ad_id=a.id) orders FROM ads a WHERE merchant_id=? ORDER BY id DESC LIMIT 20", (m["id"],))
    return jsonify([dict(r) for r in rows])

def init():
    with sqlite3.connect(DB) as c:
        c.executescript(open(os.path.join(BASE, "schema.sql")).read())
        if not c.execute("SELECT 1 FROM users").fetchone():  # demo seed
            t = now()
            c.execute("INSERT INTO users(name,phone,role) VALUES('Aisha','256700000001','merchant'),('Brian','256700000002','merchant')")
            c.execute("INSERT INTO merchants(user_id,shop_name,area,whatsapp) VALUES(1,'Aisha Fabrics','Owino Market, Kampala','256700000001'),(2,'Brian Kicks','Garden City, Kampala','256700000002')")
            for mid, ti, d, p, h in [(1, "Kitenge dresses, 3 left", "Fresh stock, all sizes", 45000, 6), (2, "Air-style sneakers", "Sizes 38-45", 120000, 24), (1, "Gomesi set + sash", "Ready for weddings", 180000, 72)]:
                c.execute("INSERT INTO ads(merchant_id,title,description,price_ugx,duration_hours,created_at,expires_at) VALUES(?,?,?,?,?,?,?)", (mid, ti, d, p, h, t, t + h * 3600))
init()
if __name__ == "__main__": app.run(debug=True, port=5000)
