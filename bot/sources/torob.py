"""Torob price comparison (unofficial public API; prices in Toman).

Torob does not publish an API, so parsing is defensive: offers are found by
shape (a dict with a shop name and a price) wherever they sit in the JSON.
Run the "selftest" workflow to dump real samples if the format changes.
"""
import re
import urllib.parse

from .http import get_json

BASE = "https://api.torob.com/v4/base-product"
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def search(query: str, size=10):
    q = urllib.parse.quote(query)
    raw = get_json(f"{BASE}/search/?page=0&sort=popularity&size={size}"
                   f"&query={q}&q={q}&source=next_desktop")
    out = []
    for r in raw.get("results") or []:
        prk = r.get("random_key") or r.get("prk")
        if not prk:
            continue
        out.append({
            "prk": prk,
            "name": " ".join(x for x in [r.get("name1"), r.get("name2")] if x),
            "price": _int(r.get("price")),
            "url": _abs(r.get("web_client_absolute_url") or ""),
        })
    return out


def details(prk: str):
    for url in (f"{BASE}/details-log-click/?source=next_desktop&prk={prk}",
                f"{BASE}/details/?prk={prk}"):
        try:
            raw = get_json(url)
        except Exception:
            continue
        offers = []
        _collect_offers(raw, offers)
        return {
            "name": " ".join(x for x in [raw.get("name1"), raw.get("name2")] if x),
            "url": _abs(raw.get("web_client_absolute_url") or ""),
            "offers": _dedupe(offers),
        }
    raise ValueError("torob details unavailable")


def price_history(prk: str):
    try:
        raw = get_json(f"{BASE}/price-chart/?prk={prk}", timeout=12)
    except Exception:
        return None
    pts = []

    def walk(o):
        if isinstance(o, dict):
            d = o.get("date") or o.get("x")
            p = o.get("min_price") or o.get("price") or o.get("avg_price") or o.get("y")
            if d and isinstance(p, (int, float)) and p > 0:
                pts.append((str(d), int(p)))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(raw)
    return pts or None


# ---------------------------------------------------------------- helpers
def _abs(u):
    if u and u.startswith("/"):
        return "https://torob.com" + u
    return u


def _int(v):
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"\D", "", v.translate(FA_DIGITS))
        return int(digits) if digits else 0
    return 0


def _years(offer: dict):
    for v in offer.values():
        if isinstance(v, str) and "سال" in v:
            m = re.search(r"(\d+)\s*سال", v.translate(FA_DIGITS))
            if m:
                return int(m.group(1))
        if isinstance(v, dict):
            y = _years(v)
            if y is not None:
                return y
    return None


def _score(offer: dict):
    for k, v in offer.items():
        if "score" in k.lower() or "rate" in k.lower():
            if isinstance(v, (int, float)) and 0 < v <= 5:
                return float(v)
            if isinstance(v, dict):
                s = _score(v)
                if s is not None:
                    return s
    return None


def _collect_offers(obj, out):
    if isinstance(obj, dict):
        shop = obj.get("shop_name") or obj.get("shop_name1")
        if shop and ("price" in obj):
            price = _int(obj.get("price"))
            avail = obj.get("availability", obj.get("is_available", True))
            if isinstance(avail, str):
                avail = "ناموجود" not in avail
            out.append({
                "shop": str(shop).strip(),
                "city": obj.get("shop_name2") or "",
                "price": price,
                "available": bool(avail) and price > 0,
                "url": _abs(obj.get("page_url") or obj.get("more_info_url") or ""),
                "score": _score(obj),
                "years": _years(obj),
                "is_ad": bool(obj.get("is_adv")),
            })
        for v in obj.values():
            _collect_offers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_offers(v, out)


def _dedupe(offers):
    seen, out = set(), []
    for o in offers:
        key = (o["shop"], o["price"])
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out
