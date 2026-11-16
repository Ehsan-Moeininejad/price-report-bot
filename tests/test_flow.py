"""End-to-end flow test with a fake Telegram and fake Digikala/Torob data.
Run: python -m tests.test_flow
"""
import json
import os
import shutil
import tempfile

os.environ["STATE_GIT"] = "0"

from bot import checker
from bot.handlers import Bot
from bot.sources import digikala, torob
from bot.state import State

ADMIN = -1001234567890
USER = {"id": 111, "first_name": "Ali", "username": "ali"}
ADM = {"id": 999, "first_name": "Ehsan"}


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.n = [], [], 100

    def send(self, chat_id, text, buttons=None, reply_to=None, force_reply=False):
        self.n += 1
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons, "id": self.n})
        return {"message_id": self.n}

    def edit(self, chat_id, mid, text, buttons=None):
        self.edits.append({"chat": chat_id, "mid": mid, "text": text, "buttons": buttons})

    def answer(self, *a, **k):
        pass

    def copy(self, *a, **k):
        self.n += 1
        return {"message_id": self.n}

    def download(self, fid):
        return b"code\nCSVCODE1\nCSVCODE2\nCSVCODE2\n"

    def last(self, chat):
        return [m for m in self.sent if m["chat"] == chat][-1]

    def btn(self, chat, label_part):
        for m in reversed(self.sent + self.edits):
            if m["chat"] == chat and m.get("buttons"):
                for row in m["buttons"]:
                    for b in row:
                        if label_part in b["text"]:
                            return b["callback_data"]
        raise AssertionError(f"button {label_part} not found")


# ---- fake sources -------------------------------------------------------
PRODUCT = {"id": "555", "title": "گوشی موبایل سامسونگ Galaxy A55 ظرفیت 256 گیگابایت",
           "title_en": "Samsung Galaxy A55 256GB", "url": "https://www.digikala.com/product/dkp-555/",
           "available": True, "price": 20_000_000, "rrp": 30_000_000, "discount": 33,
           "incredible": True, "seller": "دیجی‌کالا", "seller_rating": 4.5}
digikala.get_product = lambda pid: dict(PRODUCT, id=pid)
digikala.price_history = lambda pid: None
torob.search = lambda q, size=10: [{"prk": "abc", "name": "گوشی سامسونگ Galaxy A55 256GB", "price": 19_000_000, "url": ""}]
torob.details = lambda prk: {"name": "Galaxy A55", "url": "https://torob.com/p/abc/", "offers": [
    {"shop": "دیجی‌کالا", "city": "", "price": 20_000_000, "available": True, "url": "", "score": 4.5, "years": 8, "is_ad": False},
    {"shop": "تکنولایف", "city": "تهران", "price": 19_200_000, "available": True, "url": "https://t.example", "score": None, "years": None, "is_ad": False},
    {"shop": "موبایل فلان", "city": "شیراز", "price": 18_000_000, "available": True, "url": "", "score": 2.0, "years": 0, "is_ad": False},
    {"shop": "اسنپ‌شاپ", "city": "", "price": 21_000_000, "available": True, "url": "", "score": None, "years": None, "is_ad": False},
]}
torob.price_history = lambda prk: [("2026-11-01", 20_500_000), ("2026-11-10", 21_000_000)]


def msg(text, frm=USER, chat=None, **extra):
    chat = chat or {"id": frm["id"], "type": "private"}
    global MID
    MID += 1
    return {"message": {"message_id": MID, "from": frm, "chat": chat, "text": text, **extra}}


def cb(data, frm, chat_id, mid=1):
    return {"callback_query": {"id": "x", "from": frm, "data": data,
                               "message": {"message_id": mid, "chat": {"id": chat_id}}}}


MID = 0
GROUP = {"id": ADMIN, "type": "supergroup"}


def run():
    d = tempfile.mkdtemp()
    cfg = json.load(open("config.json", encoding="utf-8"))
    tg, st = FakeTG(), State(d)
    bot = Bot(tg, st, cfg, ADMIN)

    # checker unit checks
    r = checker.check("555", "fake", "", cfg)
    assert r["verdict"] == "likely_valid", r
    r = checker.check("555", "cheaper", "تکنولایف 19,200,000", cfg)
    assert r["verdict"] == "likely_valid" and r["offers"][0]["shop"] == "تکنولایف", r
    assert checker.parse_price("۱۲.۵ میلیون") == 12_500_000
    assert checker.parse_price("قیمت 125,000,000 ریال") == 12_500_000

    # 1) start + fake-discount report
    bot.handle(msg("/start"))
    bot.handle(msg("ببین https://www.digikala.com/product/dkp-555/samsung"))
    assert "چه مشکلی" in tg.last(111)["text"]
    bot.handle(cb("t:fake", USER, 111))
    card = tg.last(ADMIN)
    assert "#1" in card["text"] and "احتمالاً درست" in card["text"], card["text"]
    assert "ثبت شد" in tg.last(111)["text"]

    # 2) admin adds pools: text + file
    bot.handle(msg("/addpool v50 | ۵۰ هزار تومان\nAAA111\nBBB222\nAAA111", frm=ADM, chat=GROUP))
    assert "2 کد جدید" in tg.last(ADMIN)["text"], tg.last(ADMIN)["text"]
    bot.handle(msg("/addpool v100", frm=ADM, chat=GROUP, document={"file_id": "f", "file_name": "c.csv"}))
    assert "2 کد جدید" in tg.last(ADMIN)["text"], tg.last(ADMIN)["text"]

    # 3) admin approves -> picks pool -> user gets code
    bot.handle(cb("ok:1", ADM, ADMIN, card["id"]))
    assert st.reports["1"]["status"] == "valid"
    bot.handle(cb(tg.btn(ADMIN, "v50"), ADM, ADMIN, card["id"]))
    texts = [m["text"] for m in tg.sent if m["chat"] == 111]
    assert any("AAA111" in t for t in texts), texts
    assert st.pools["v50"]["codes"] == ["BBB222"]
    # double click on same pool does nothing
    bot.handle(cb(tg.btn(ADMIN, "v50") if False else "vp:1:0", ADM, ADMIN, card["id"]))
    assert st.pools["v50"]["codes"] == ["BBB222"]

    # 4) fixed
    bot.handle(cb("fx:1", ADM, ADMIN, card["id"]))
    assert "اصلاح شد" in tg.last(111)["text"]

    # 5) cheaper report with evidence -> reject with reason
    bot.handle(msg("https://www.digikala.com/product/dkp-777/"))
    bot.handle(cb("t:cheaper", USER, 111))
    bot.handle(msg("https://technolife.ir/x 19,200,000"))
    card2 = tg.last(ADMIN)
    assert "#2" in card2["text"] and "تکنولایف" in card2["text"], card2["text"]
    bot.handle(cb("no:2", ADM, ADMIN, card2["id"]))
    bot.handle(cb("rj:2:2", ADM, ADMIN, card2["id"]))
    assert st.reports["2"]["status"] == "invalid"
    assert "کالا یا مدل متفاوت" in tg.last(111)["text"]

    # 6) duplicate by same user is blocked
    bot.handle(msg("https://www.digikala.com/product/dkp-777/"))
    bot.handle(cb("t:cheaper", USER, 111))
    bot.handle(cb("t:send", USER, 111))
    assert "اخیراً گزارش" in tg.last(111)["text"], tg.last(111)["text"]

    # 7) another user, product already confirmed (but fixed -> allowed again)
    U2 = {"id": 222, "first_name": "Sara"}
    bot.handle(msg("https://www.digikala.com/product/dkp-555/", frm=U2))
    bot.handle(cb("t:fake", U2, 222))
    assert "ثبت شد" in tg.last(222)["text"], tg.last(222)["text"]
    # manual voucher
    card3 = tg.last(ADMIN)
    bot.handle(cb("ok:3", ADM, ADMIN, card3["id"]))
    bot.handle(cb("vm:3", ADM, ADMIN, card3["id"]))
    bot.handle(msg("MANUAL-777", frm=ADM, chat=GROUP))
    assert any("MANUAL-777" in m["text"] for m in tg.sent if m["chat"] == 222)

    # 8) confirmed & not fixed -> third user blocked as duplicate
    U3 = {"id": 333, "first_name": "Reza"}
    bot.handle(msg("https://www.digikala.com/product/dkp-555/", frm=U3))
    bot.handle(cb("t:fake", U3, 333))
    assert "تأیید شده" in tg.last(333)["text"], tg.last(333)["text"]

    # 9) admin commands
    for c in ("/stats", "/pools", "/open", "/help", "/check https://www.digikala.com/product/dkp-555/"):
        bot.handle(msg(c, frm=ADM, chat=GROUP))
        assert tg.last(ADMIN)["text"]
    bot.handle(msg("/ban 333", frm=ADM, chat=GROUP))
    bot.handle(msg("https://www.digikala.com/product/dkp-1/", frm=U3))
    assert "فعال نیست" in tg.last(333)["text"]
    bot.handle(msg("/my"))
    assert "#1" in tg.last(111)["text"]

    # 10) templates: system override + custom reply via button
    bot.handle(msg("/settpl valid\nآفرین {name}! گزارش *#{rid}* درست بود <3", frm=ADM, chat=GROUP))
    assert "ذخیره شد" in tg.last(ADMIN)["text"]
    bot.handle(msg("/settpl تشکر ویژه\nسلام {name}، بابت گزارش {title} ممنونیم", frm=ADM, chat=GROUP))
    bot.handle(msg("/templates", frm=ADM, chat=GROUP))
    assert "تشکر ویژه" in tg.last(ADMIN)["text"]
    U4 = {"id": 444, "first_name": "Mina"}
    bot.handle(msg("https://www.digikala.com/product/dkp-888/", frm=U4))
    bot.handle(cb("t:fake", U4, 444))
    card4 = tg.last(ADMIN)
    rid4 = str(st.data["next_report_id"] - 1)
    bot.handle(cb(f"ok:{rid4}", ADM, ADMIN, card4["id"]))
    bot.handle(cb(f"vn:{rid4}", ADM, ADMIN, card4["id"]))
    assert "آفرین Mina" in tg.last(444)["text"] and "&lt;3" in tg.last(444)["text"], tg.last(444)["text"]
    bot.handle(cb(f"rp:{rid4}", ADM, ADMIN, card4["id"]))
    idx = list(st.data["templates"]["custom"]).index("تشکر ویژه")
    bot.handle(cb(f"ts:{rid4}:{idx}", ADM, ADMIN, card4["id"]))
    assert "سلام Mina" in tg.last(444)["text"], tg.last(444)["text"]
    # admin replies to card -> user; user answers -> admin group
    bot.handle(msg("کالا را دوباره چک کنید {name}", frm=ADM, chat=GROUP,
                   reply_to_message={"message_id": card4["id"]}))
    assert "دوباره چک کنید Mina" in tg.last(444)["text"]
    bot.handle(msg("باشه ممنون", frm=U4))
    assert "پاسخ کاربر به گزارش" in tg.last(ADMIN)["text"]
    # free reply via button
    bot.handle(cb(f"rf:{rid4}", ADM, ADMIN, card4["id"]))
    bot.handle(msg("پیام آزاد تست", frm=ADM, chat=GROUP))
    assert "پیام آزاد تست" in tg.last(444)["text"]
    # reasons
    bot.handle(msg("/addreason هزینه ارسال حساب نشده", frm=ADM, chat=GROUP))
    assert st.data["reasons"][-1] == "هزینه ارسال حساب نشده"
    bot.handle(msg("/deltpl valid", frm=ADM, chat=GROUP))
    assert "valid" not in st.data["templates"]["system"]

    # 11) daily cap = 5
    U5 = {"id": 555, "first_name": "Cap"}
    for i in range(6):
        bot.handle(msg(f"https://www.digikala.com/product/dkp-90{i}/", frm=U5))
        bot.handle(cb("t:fake", U5, 555))
    assert "سقف گزارش امروز" in tg.last(555)["text"], tg.last(555)["text"]
    assert len(st.users["555"]["reports"]) == 5

    st.save()
    assert os.path.exists(os.path.join(d, "data", "state.json"))
    print(tg.last(ADMIN)["text"][:200])
    print("\n--- sample admin card ---\n" + card["text"])
    print("\n--- stats ---")
    bot.handle(msg("/stats", frm=ADM, chat=GROUP))
    print(tg.last(ADMIN)["text"])
    shutil.rmtree(d)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    run()
