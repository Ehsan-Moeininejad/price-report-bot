"""Automatic first-pass check of a report.

Returns {"verdict", "confidence", "lines", "product", "offers", "torob_url"}
verdict: likely_valid | likely_invalid | review
"""
import re
import statistics

from .sources import digikala, torob

FAKE = "fake"
CHEAPER = "cheaper"
ZW = "‌"
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm(s: str) -> str:
    s = (s or "").replace("ي", "ی").replace("ك", "ک").replace(ZW, " ").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def money(n) -> str:
    return f"{int(n):,}" if n else "-"


def parse_price(text: str):
    """Extract a Toman amount the user typed (e.g. 12,500,000 or 12.5 میلیون)."""
    if not text:
        return None
    t = text.translate(FA_DIGITS).replace("٬", ",")
    t = re.sub(r"https?://\S+", " ", t)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(میلیون|million|m\b)", t, re.I)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1_000_000)
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d{5,})", t)
    if m:
        v = int(m.group(1).replace(",", ""))
        if "ریال" in t:
            v //= 10
        return v
    return None


def _shop_in(shop: str, names) -> bool:
    s = norm(shop)
    return any(norm(n) and norm(n) in s for n in names)


def is_trusted(offer, cfg) -> bool:
    ts = cfg["trusted_sellers"]
    if _shop_in(offer["shop"], ts["blacklist"]):
        return False
    if _shop_in(offer["shop"], ts["whitelist"]):
        return True
    if offer.get("score") is not None and offer["score"] >= ts["min_shop_score"]:
        return True
    if offer.get("years") is not None and offer["years"] >= ts["min_years_on_torob"]:
        return True
    return False


def find_torob_match(product, cfg):
    """Search Torob with the Digikala title and pick the most likely same item."""
    best, best_score = None, 0.0
    n = cfg["check"]["torob_search_results_to_inspect"]
    queries = [product["title"][:80]]
    if product.get("title_en"):
        queries.append(product["title_en"][:80])
    seen = set()
    for q in queries:
        try:
            results = torob.search(q)
        except Exception:
            continue
        for r in results[:n]:
            if r["prk"] in seen:
                continue
            seen.add(r["prk"])
            sim = max(similarity(product["title"], r["name"]),
                      similarity(product.get("title_en", ""), r["name"]))
            try:
                d = torob.details(r["prk"])
            except Exception:
                continue
            dk_listed = any(_shop_in(o["shop"], cfg["trusted_sellers"]["blacklist"])
                            for o in d["offers"])
            score = sim + (0.5 if dk_listed else 0)
            if score > best_score:
                best_score = score
                best = {**d, "prk": r["prk"], "sim": sim, "dk_listed": dk_listed,
                        "url": d["url"] or r["url"]}
        if best and best["dk_listed"]:
            break
    if best and (best["dk_listed"] or best["sim"] >= cfg["check"]["min_name_similarity"]):
        return best
    return None


def check(pid: str, kind: str, evidence_text: str, cfg) -> dict:
    lines = []
    res = {"verdict": "review", "confidence": 0, "lines": lines,
           "product": None, "offers": [], "torob_url": ""}
    try:
        p = digikala.get_product(pid)
    except Exception as e:
        lines.append(f"⚠️ اطلاعات کالا از دیجی‌کالا خوانده نشد ({type(e).__name__})")
        return res
    res["product"] = p
    if not p["available"]:
        lines.append("⚠️ کالا در دیجی‌کالا ناموجود است")

    match = None
    try:
        match = find_torob_match(p, cfg)
    except Exception as e:
        lines.append(f"⚠️ ترب در دسترس نبود ({type(e).__name__})")
    if match:
        res["torob_url"] = match["url"]
        trusted = sorted([o for o in match["offers"] if o["available"] and is_trusted(o, cfg)],
                         key=lambda o: o["price"])
        res["offers"] = trusted[:5]
        how = ("قطعی (دیجی‌کالا در لیست فروشنده‌ها)" if match["dk_listed"]
               else "شباهت نام %d%%" % int(match["sim"] * 100))
        lines.append(f"🔎 تطبیق با ترب: {how} — {len(trusted)} فروشنده معتبر موجود")
    else:
        trusted = []
        if not any("ترب" in l for l in lines):
            lines.append("🔎 همین کالا در ترب با اطمینان پیدا نشد")

    user_price = parse_price(evidence_text)
    if user_price:
        lines.append(f"💬 قیمت اعلامی کاربر: {money(user_price)} تومان")

    if kind == CHEAPER:
        _judge_cheaper(p, trusted, match, user_price, cfg, res)
    else:
        _judge_fake(p, trusted, match, cfg, res)
    return res


def _judge_cheaper(p, trusted, match, user_price, cfg, res):
    lines = res["lines"]
    gap = cfg["check"]["cheaper_min_gap_percent"] / 100
    limit = p["price"] * (1 - gap)
    cheaper = [o for o in trusted if o["price"] < limit]
    if cheaper:
        best = cheaper[0]
        diff = (p["price"] - best["price"]) / p["price"] * 100
        lines.append(f"🟢 ارزان‌ترین فروشنده معتبر: {best['shop']} — {money(best['price'])} "
                     f"تومان ({diff:.1f}% ارزان‌تر)")
        conf = 60 + (20 if match and match["dk_listed"] else 0)
        if user_price and abs(user_price - best["price"]) / best["price"] < 0.05:
            conf += 15
        res.update(verdict="likely_valid", confidence=min(conf, 95))
        return
    others = []
    if match:
        others = [o for o in match["offers"] if o["available"] and o["price"] < limit
                  and not is_trusted(o, cfg)
                  and not _shop_in(o["shop"], cfg["trusted_sellers"]["blacklist"])]
    if others:
        o = min(others, key=lambda x: x["price"])
        lines.append(f"🟡 فقط فروشنده غیرمعتبر ارزان‌تر است: {o['shop']} — {money(o['price'])} تومان")
        res.update(verdict="review", confidence=40)
    elif user_price and user_price < limit:
        lines.append("🟡 کاربر قیمت کمتری اعلام کرده ولی در فروشنده‌های معتبر ترب دیده نشد")
        res.update(verdict="review", confidence=35)
    elif match and trusted:
        lines.append(f"🔴 دیجی‌کالا ({money(p['price'])}) از همه فروشنده‌های معتبر ارزان‌تر یا برابر است")
        res.update(verdict="likely_invalid", confidence=75 if match["dk_listed"] else 55)
    else:
        res.update(verdict="review", confidence=20)


def _judge_fake(p, trusted, match, cfg, res):
    lines = res["lines"]
    tol = cfg["check"]["fake_rrp_over_market_percent"] / 100
    if not p["discount"] or p["rrp"] <= p["price"]:
        lines.append("🔴 این کالا الان در دیجی‌کالا تخفیف ندارد")
        res.update(verdict="likely_invalid", confidence=85)
        return
    signals_valid, signals_invalid = [], []

    hist = None
    if match:
        hist = torob.price_history(match["prk"])
    if not hist:
        hist = digikala.price_history(p["id"])
    if hist:
        recent = [v for _, v in hist[-30:]]
        hmax = max(recent)
        lines.append(f"📈 بیشترین قیمت ثبت‌شده در تاریخچه اخیر: {money(hmax)} تومان")
        if p["rrp"] > hmax * (1 + tol):
            signals_valid.append("قیمت قبل از تخفیف هیچ‌وقت در تاریخچه اخیر ثبت نشده")
        else:
            signals_invalid.append("قیمت قبل از تخفیف در تاریخچه اخیر واقعی بوده")

    prices = [o["price"] for o in trusted]
    if prices:
        mmax, med = max(prices), statistics.median(prices)
        lines.append(f"🏪 بازار (فروشنده‌های معتبر): میانه {money(med)} — بیشترین {money(mmax)} تومان")
        if p["rrp"] > mmax * (1 + tol):
            signals_valid.append("قیمت قبل از تخفیف از گران‌ترین فروشنده معتبر بازار هم بالاتر است")
        elif p["rrp"] <= med:
            signals_invalid.append("قیمت قبل از تخفیف از قیمت رایج بازار بالاتر نیست")
        if p["price"] > min(prices) * 1.01:
            lines.append(f"ℹ️ حتی بعد از تخفیف، {trusted[0]['shop']} ارزان‌تر است "
                         f"({money(trusted[0]['price'])} تومان)")

    for s in signals_valid:
        lines.append("🟢 " + s)
    for s in signals_invalid:
        lines.append("🔴 " + s)
    if signals_valid and not signals_invalid:
        res.update(verdict="likely_valid", confidence=55 + 20 * len(signals_valid))
    elif signals_invalid and not signals_valid:
        res.update(verdict="likely_invalid", confidence=50 + 20 * len(signals_invalid))
    else:
        res.update(verdict="review", confidence=30)
    res["confidence"] = min(res["confidence"], 95)
