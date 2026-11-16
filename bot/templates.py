"""Admin-editable message templates.

Templates are plain text written by admins in the admin group.
Formatting: *bold*  and  `code`
Placeholders: {name} {rid} {title} {url} {code} {desc} {reason} {price} {limit}
"""
import re
from html import escape

# key -> (Persian label, default text)
SYSTEM = {
    "welcome": ("خوش‌آمد (/start)", (
        "سلام {name} 👋\n"
        "اینجا می‌توانید *تخفیف فیک* یا *قیمت بالاتر از بازار* در دیجی‌کالا را گزارش کنید.\n\n"
        "*روش کار:*\n"
        "1️⃣ لینک کالای دیجی‌کالا را بفرستید (دکمه «اشتراک‌گذاری» در اپ یا سایت).\n"
        "2️⃣ نوع گزارش را انتخاب کنید.\n"
        "3️⃣ اگر جای دیگری ارزان‌تر دیده‌اید، لینک، اسکرین‌شات یا قیمت آن را بفرستید.\n\n"
        "گزارش شما خودکار با ترب و فروشگاه‌های معتبر مقایسه و سپس توسط تیم بررسی می‌شود. "
        "اگر درست باشد، نتیجه و هدیه برایتان ارسال می‌شود 🎁\n"
        "هر نفر روزانه حداکثر {limit} گزارش می‌تواند ثبت کند.\n\n"
        "/my — وضعیت گزارش‌های من")),
    "submitted": ("ثبت گزارش", (
        "✅ گزارش *#{rid}* ثبت شد.\n"
        "نتیجه بررسی را همین‌جا به شما خبر می‌دهیم. ممنون که کمک می‌کنید 🙏")),
    "valid": ("گزارش درست", (
        "✅ گزارش *#{rid}* درباره «{title}» درست بود.\n"
        "ممنون از دقت و همراهی شما 🙏")),
    "voucher": ("ارسال وچر", "🎁 کد هدیه شما:\n`{code}`\n{desc}"),
    "invalid": ("گزارش نادرست", (
        "گزارش *#{rid}* درباره «{title}» بررسی شد و تأیید نشد.\n"
        "دلیل: {reason}\n\nاز اینکه حواستان به قیمت‌ها هست ممنونیم 🙏")),
    "fixed": ("قیمت اصلاح شد", (
        "🔧 بر اساس گزارش *#{rid}* شما، قیمت «{title}» بررسی و اصلاح شد.\n{url}")),
    "daily_cap": ("سقف روزانه", (
        "سقف گزارش امروز شما ({limit} گزارش) پر شده است. لطفاً فردا دوباره امتحان کنید 🙏")),
}

DEFAULT_CUSTOM = {
    "در حال بررسی": "گزارش *#{rid}* شما در حال بررسی دقیق‌تر است و به‌زودی نتیجه را اعلام می‌کنیم 🙏",
    "مدرک بیشتر": ("برای بررسی گزارش *#{rid}* لطفاً لینک یا اسکرین‌شات صفحه فروشگاه دیگر را "
                   "همین‌جا بفرستید."),
    "قیمت لحظه‌ای": ("قیمت «{title}» در لحظه بررسی با بازار همخوان بود. "
                     "اگر دوباره تفاوت دیدید، حتماً گزارش دهید 🙏"),
}


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(text: str, **values) -> str:
    """Escape admin text, apply *bold* / `code`, then fill placeholders (escaped)."""
    out = escape(text, quote=False)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", out)
    vals = _Safe({k: escape(str(v if v is not None else ""), quote=False)
                  for k, v in values.items()})
    try:
        return out.format_map(vals)
    except (ValueError, IndexError):
        return out


def init(state):
    """Make sure templates/reasons exist in state (first run)."""
    data = state.data
    if "templates" not in data:
        data["templates"] = {"system": {}, "custom": dict(DEFAULT_CUSTOM)}
        state.touch()
    return data["templates"]


def get(state, key):
    t = init(state)
    if key in t["system"]:
        return t["system"][key]
    return SYSTEM[key][1]
