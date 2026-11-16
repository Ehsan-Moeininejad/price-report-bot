"""Connectivity + format check. Prints a report and dumps raw JSON samples to ./samples."""
import json
import os
import sys
import traceback

from .sources import digikala, torob
from .sources.http import get_json

os.makedirs("samples", exist_ok=True)
ok = True


def step(name, fn):
    global ok
    try:
        out = fn()
        print(f"✅ {name}: {out}")
        return True
    except Exception as e:
        ok = False
        print(f"❌ {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=1)
        return False


def dump(name, url):
    data = get_json(url)
    with open(f"samples/{name}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return f"saved samples/{name}.json ({len(json.dumps(data))} bytes)"


arg = sys.argv[1] if len(sys.argv) > 1 else ""
pid = digikala.find_product_id(arg) or "".join(ch for ch in arg if ch.isdigit())
if not pid:
    sys.exit("Give a Digikala product link as input")
print("product id:", pid)
token = os.environ.get("BOT_TOKEN", "")
if token:
    step("Telegram getMe", lambda: get_json(f"https://api.telegram.org/bot{token}/getMe")["result"]["username"])
    gid = os.environ.get("ADMIN_GROUP_ID")
    if gid:
        step("Admin group access", lambda: get_json(
            f"https://api.telegram.org/bot{token}/getChat?chat_id={gid}")["result"].get("title"))

step("Digikala raw", lambda: dump("digikala_product", f"https://api.digikala.com/v2/product/{pid}/"))
p = {}
if step("Digikala parsed", lambda: p.update(digikala.get_product(pid)) or p):
    step("Digikala price history", lambda: (digikala.price_history(pid) or [])[-5:])
    q = p["title"][:80]
    import urllib.parse as up
    qq = up.quote(q)
    step("Torob search raw", lambda: dump(
        "torob_search", f"{torob.BASE}/search/?page=0&sort=popularity&size=10&query={qq}&q={qq}&source=next_desktop"))
    res = []
    if step("Torob search parsed", lambda: res.extend(torob.search(q)) or [(r['name'][:50], r['price']) for r in res[:5]]):
        if res:
            prk = res[0]["prk"]
            step("Torob details raw", lambda: dump(
                "torob_details", f"{torob.BASE}/details-log-click/?source=next_desktop&prk={prk}"))
            step("Torob offers parsed", lambda: [(o['shop'], o['price'], o['score'], o['years'])
                                                 for o in torob.details(prk)['offers'][:8]])
            step("Torob price history", lambda: (torob.price_history(prk) or [])[-5:])
    from .checker import check
    cfg = json.load(open("config.json", encoding="utf-8"))
    for kind in ("fake", "cheaper"):
        step(f"Checker ({kind})", lambda: (lambda r: (r["verdict"], r["confidence"], r["lines"]))(
            check(pid, kind, "", cfg)))

print("\nRESULT:", "ALL OK" if ok else "SOME CHECKS FAILED — send this log to Claude")
