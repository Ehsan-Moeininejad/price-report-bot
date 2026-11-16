"""Digikala product data (public web API, prices returned in Rial)."""
import re

from .http import get_json, resolve_url

PID_RE = re.compile(r"dkp-(\d{3,12})", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)


def find_product_id(text: str):
    """Return the Digikala product id found in a message, or None."""
    if not text:
        return None
    m = PID_RE.search(text)
    if m:
        return m.group(1)
    for url in URL_RE.findall(text):
        if any(d in url.lower() for d in ("digikala", "dgka.la", "dkala")):
            final = resolve_url(url)
            m = PID_RE.search(final)
            if m:
                return m.group(1)
    return None


def _toman(rial):
    try:
        return int(rial) // 10 if rial else 0
    except (TypeError, ValueError):
        return 0


def get_product(pid: str) -> dict:
    """Return a normalised product dict. Raises on network/format errors."""
    raw = get_json(f"https://api.digikala.com/v2/product/{pid}/")
    p = (raw.get("data") or {}).get("product") or {}
    if not p:
        raise ValueError("product not found")
    v = p.get("default_variant") or {}
    if isinstance(v, list):
        v = v[0] if v else {}
    price = v.get("price") or {}
    seller = v.get("seller") or {}
    return {
        "id": str(pid),
        "title": p.get("title_fa") or p.get("title_en") or "",
        "title_en": p.get("title_en") or "",
        "url": f"https://www.digikala.com/product/dkp-{pid}/",
        "available": p.get("status") == "marketable" and bool(price),
        "price": _toman(price.get("selling_price")),
        "rrp": _toman(price.get("rrp_price")),
        "discount": int(price.get("discount_percent") or 0),
        "incredible": bool(price.get("is_incredible")),
        "seller": seller.get("title") or "",
        "seller_rating": ((seller.get("stars") or 0) or
                          (seller.get("rating") or {}).get("total_rate") or 0),
    }


def price_history(pid: str):
    """Best-effort price history: list of (date, toman). None if unavailable."""
    for url in (f"https://api.digikala.com/v1/product/{pid}/price-chart/",
                f"https://api.digikala.com/v2/product/{pid}/price-chart/"):
        try:
            raw = get_json(url, timeout=12)
        except Exception:
            continue
        points = []
        _collect_points(raw, points)
        if points:
            return points
    return None


def _collect_points(obj, out):
    """Find {date/x, price/y} like points anywhere in the JSON."""
    if isinstance(obj, dict):
        price = obj.get("selling_price") or obj.get("price") or obj.get("y")
        date = obj.get("date") or obj.get("x") or obj.get("created_at")
        if date and isinstance(price, (int, float)) and price > 0:
            out.append((str(date), _toman(price)))
        for v in obj.values():
            _collect_points(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_points(v, out)
