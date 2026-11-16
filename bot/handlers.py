"""All update handling: user chat flow + admin group flow."""
import csv
import io
import re
import time
from datetime import datetime, timedelta, timezone

from . import checker, templates as TP, texts as T
from .sources import digikala
from .texts import h

TEHRAN = timezone(timedelta(hours=3, minutes=30))
DAY = 86400


def now():
    return int(time.time())


def tehran(ts):
    return datetime.fromtimestamp(ts, TEHRAN).strftime("%m/%d %H:%M")


class Bot:
    def __init__(self, tg, state, cfg, admin_group: int):
        self.tg, self.st, self.cfg, self.admin = tg, state, cfg, admin_group
        TP.init(state)
        state.data.setdefault("reasons", list(cfg["reject_reasons"]))
        state.data.setdefault("msgmap", {})

    # ---------------------------------------------------------------- templates
    def tpl(self, key, r=None, **extra):
        vals = {"limit": self.cfg["limits"]["reports_per_user_per_day"]}
        if r:
            u = self.st.users.get(r["uid"], {})
            vals.update(rid=r["id"], title=r["title"][:80], url=r["url"],
                        name=(u.get("name") or "").split(" ")[0],
                        price=checker.money((r.get("snapshot") or {}).get("price")))
        vals.update(extra)
        text = TP.get(self.st, key) if key in TP.SYSTEM else key
        return TP.render(text, **vals)

    def map_msg(self, message_id, rid):
        if message_id:
            mm = self.st.data["msgmap"]
            mm[str(message_id)] = rid
            if len(mm) > 5000:  # keep the map small
                for k in list(mm)[:1000]:
                    mm.pop(k)
            self.st.touch()

    # ================================================================ dispatch
    def handle(self, upd):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat = (cq.get("message") or {}).get("chat", {}).get("id")
            if chat == self.admin:
                return self.admin_callback(cq)
            return self.user_callback(cq)
        msg = upd.get("message")
        if not msg:
            return
        chat = msg["chat"]
        if (msg.get("text") or "").split("@")[0].strip() == "/id" and chat["id"] != self.admin:
            return self.tg.send(chat["id"], f"chat id: <code>{chat['id']}</code>")
        if chat["id"] == self.admin:
            return self.admin_message(msg)
        if chat.get("type") == "private":
            return self.user_message(msg)

    # ================================================================ USER
    def user_message(self, msg):
        uid = msg["from"]["id"]
        u = self.st.user(uid, msg["from"])
        text = msg.get("text") or msg.get("caption") or ""
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if cmd in ("/start", "/help"):
            u["step"], u["draft"] = None, None
            self.st.touch()
            return self.tg.send(uid, self.tpl("welcome", name=msg["from"].get("first_name", "")))
        if cmd == "/my":
            return self.tg.send(uid, self.my_reports(u))
        if cmd == "/cancel":
            u["step"], u["draft"] = None, None
            self.st.touch()
            return self.tg.send(uid, T.CANCELLED)
        if u.get("banned"):
            return self.tg.send(uid, T.BANNED)

        # waiting for evidence of a "cheaper elsewhere" report
        if u.get("step") == "evidence" and u.get("draft"):
            if self._draft_expired(u):
                return self.tg.send(uid, T.EXPIRED)
            d = u["draft"]
            d["evidence"] = (d.get("evidence", "") + "\n" + text).strip()
            d.setdefault("msgs", []).append(msg["message_id"])
            self.st.touch()
            return self.submit(uid, u)

        pid = digikala.find_product_id(text)
        if not pid:
            th = u.get("thread")
            if th and now() - th["at"] < 2 * DAY and th["rid"] in self.st.reports:
                return self.user_followup(uid, u, th["rid"], msg)
            return self.tg.send(uid, T.NEED_LINK)
        try:
            title = digikala.get_product(pid)["title"]
        except Exception:
            title = f"کالای dkp-{pid}"
        u["draft"] = {"pid": pid, "title": title, "at": now(),
                      "evidence": "", "msgs": [], "link_text": text[:500]}
        u["step"] = "kind"
        self.st.touch()
        self.tg.send(uid, T.ASK_KIND.format(title=h(title)), buttons=[
            [{"text": T.KIND_FA["fake"], "callback_data": "t:fake"},
             {"text": T.KIND_FA["cheaper"], "callback_data": "t:cheaper"}],
            [{"text": "انصراف", "callback_data": "t:cancel"}]])

    def user_callback(self, cq):
        uid = cq["from"]["id"]
        u = self.st.user(uid, cq["from"])
        data = cq.get("data", "")
        mid = cq["message"]["message_id"]
        self.tg.answer(cq["id"])
        if not data.startswith("t:"):
            return
        act = data[2:]
        if act == "cancel" or not u.get("draft"):
            u["step"], u["draft"] = None, None
            self.st.touch()
            return self.tg.edit(uid, mid, T.CANCELLED if act == "cancel" else T.EXPIRED)
        if self._draft_expired(u):
            return self.tg.edit(uid, mid, T.EXPIRED)
        d = u["draft"]
        if act == "fake":
            d["kind"] = "fake"
            self.tg.edit(uid, mid, T.ASK_KIND.format(title=h(d["title"])) +
                         f"\n\n➡️ {T.KIND_FA['fake']}")
            return self.submit(uid, u)
        if act == "cheaper":
            d["kind"] = "cheaper"
            u["step"] = "evidence"
            self.st.touch()
            self.tg.edit(uid, mid, T.ASK_KIND.format(title=h(d["title"])) +
                         f"\n\n➡️ {T.KIND_FA['cheaper']}")
            return self.tg.send(uid, T.ASK_EVIDENCE, buttons=[
                [{"text": "ارسال بدون مدرک", "callback_data": "t:send"}],
                [{"text": "انصراف", "callback_data": "t:cancel"}]])
        if act == "send" and d.get("kind"):
            self.tg.edit(uid, mid, "ارسال بدون مدرک ✔️")
            return self.submit(uid, u)

    def _draft_expired(self, u):
        d = u.get("draft") or {}
        if now() - d.get("at", 0) > self.cfg["draft_expire_minutes"] * 60:
            u["step"], u["draft"] = None, None
            self.st.touch()
            return True
        return False

    # ---------------------------------------------------------------- submit
    def submit(self, uid, u):
        d = u["draft"]
        u["step"], u["draft"] = None, None
        self.st.touch()
        if not self.admin:
            return self.tg.send(uid, "بات در حال راه‌اندازی است؛ لطفاً کمی بعد دوباره امتحان کنید.")
        lim = self.cfg["limits"]
        mine = [self.st.reports[r] for r in u["reports"] if r in self.st.reports]

        day = datetime.fromtimestamp(now(), TEHRAN).date()
        today = [r for r in mine if datetime.fromtimestamp(r["created"], TEHRAN).date() == day]
        if len(today) >= lim["reports_per_user_per_day"]:
            return self.tg.send(uid, self.tpl("daily_cap"))
        for r in mine:
            if (r["pid"] == d["pid"] and r["kind"] == d["kind"]
                    and now() - r["created"] < lim["same_product_cooldown_days"] * DAY):
                return self.tg.send(uid, T.DUP_SELF.format(
                    rid=r["id"], status=T.STATUS_FA[r["status"]]))
        for r in self.st.reports.values():
            if (r["pid"] == d["pid"] and r["kind"] == d["kind"] and r["status"] == "valid"
                    and not r.get("fixed")
                    and now() - r["created"] < lim["confirmed_product_cooldown_days"] * DAY):
                return self.tg.send(uid, T.DUP_CONFIRMED)

        self.tg.send(uid, T.CHECKING)
        evidence = (d.get("evidence") or "")
        res = checker.check(d["pid"], d["kind"], evidence, self.cfg)
        p = res["product"] or {}
        rid = self.st.new_report_id()
        rep = {
            "id": rid, "uid": str(uid), "kind": d["kind"], "pid": d["pid"],
            "title": p.get("title") or d["title"],
            "url": p.get("url") or f"https://www.digikala.com/product/dkp-{d['pid']}/",
            "evidence": evidence[:1500], "created": now(), "status": "open",
            "auto": {k: res[k] for k in ("verdict", "confidence", "lines", "torob_url")},
            "snapshot": {k: p.get(k) for k in ("price", "rrp", "discount", "seller", "available")},
            "offers": res["offers"][:3],
            "voucher": None, "voucher_step": False, "fixed": False,
        }
        self.st.reports[rid] = rep
        u["reports"].append(rid)
        self.st.touch()

        similar = [r["id"] for r in self.st.reports.values()
                   if r["pid"] == rep["pid"] and r["status"] == "open" and r["id"] != rid]
        rep["similar_open"] = similar[:5]

        card = self.tg.send(self.admin, self.card_text(rep), buttons=self.card_buttons(rep))
        rep["card"] = card["message_id"] if card else None
        self.map_msg(rep["card"], rid)
        for m in d.get("msgs", []):
            try:
                c = self.tg.copy(self.admin, uid, m, reply_to=rep["card"])
                self.map_msg((c or {}).get("message_id"), rid)
            except Exception:
                pass
        self.st.touch()
        self.st.save(f"report #{rid}")
        self.tg.send(uid, self.tpl("submitted", rep))

        ck = self.cfg["check"]
        if (ck.get("auto_reject_likely_invalid") and res["verdict"] == "likely_invalid"
                and res["confidence"] >= 70):
            self.decide_invalid(rep, "بررسی خودکار: " + _first_red(res["lines"]), by="auto")

    def my_reports(self, u):
        ids = u.get("reports", [])[-10:]
        if not ids:
            return "هنوز گزارشی ثبت نکرده‌اید. لینک کالای دیجی‌کالا را بفرستید."
        lines = ["<b>آخرین گزارش‌های شما</b>"]
        for rid in reversed(ids):
            r = self.st.reports.get(rid)
            if not r:
                continue
            extra = " 🔧 اصلاح شد" if r.get("fixed") else ""
            lines.append(f"#{rid} · {T.STATUS_FA[r['status']]}{extra}\n"
                         f"   {h(r['title'][:60])}")
        return "\n".join(lines)

    # ================================================================ ADMIN
    def card_text(self, r):
        u = self.st.users.get(r["uid"], {})
        hist = [self.st.reports[x] for x in u.get("reports", []) if x in self.st.reports]
        ok = sum(1 for x in hist if x["status"] == "valid")
        bad = sum(1 for x in hist if x["status"] == "invalid")
        uname = f" @{h(u['username'])}" if u.get("username") else ""
        s = r.get("snapshot") or {}
        a = r["auto"]
        lines = [
            f"<b>#{r['id']}</b> · {T.KIND_FA[r['kind']]} · {tehran(r['created'])}",
            f"👤 {h(u.get('name'))}{uname} · ID: <code>{r['uid']}</code> "
            f"· سابقه: {len(hist)} گزارش (✅{ok} ❌{bad})",
            f"🛒 <a href=\"{r['url']}\">{h(r['title'][:120])}</a>",
            f"💰 دیجی‌کالا: <b>{checker.money(s.get('price'))}</b> تومان"
            + (f" (قبل: {checker.money(s.get('rrp'))} · {s.get('discount')}%)" if s.get("discount") else "")
            + (f" · فروشنده: {h(s.get('seller'))}" if s.get("seller") else ""),
            "",
            f"🤖 بررسی خودکار: <b>{T.VERDICT_FA[a['verdict']]}</b> (اطمینان {a['confidence']}%)",
        ]
        lines += [h(x) for x in a["lines"]]
        if r.get("offers"):
            lines.append("فروشنده‌های معتبر (ارزان‌ترین‌ها):")
            for o in r["offers"]:
                link = f"<a href=\"{o['url']}\">{h(o['shop'])}</a>" if o.get("url") else h(o["shop"])
                lines.append(f"  • {link}: {checker.money(o['price'])}")
        if a.get("torob_url"):
            lines.append(f"<a href=\"{a['torob_url']}\">صفحه ترب</a>")
        if r.get("evidence"):
            lines.append(f"\n📝 کاربر: {h(r['evidence'][:600])}")
        if r.get("similar_open"):
            lines.append("⚠️ گزارش باز مشابه: " + ", ".join("#" + x for x in r["similar_open"]))
        # decision footer
        if r["status"] != "open":
            who = r.get("decided_by_name") or r.get("decided_by")
            lines.append(f"\n<b>{T.STATUS_FA[r['status']]}</b> توسط {h(who)} · {tehran(r['decided_at'])}")
            if r["status"] == "invalid":
                lines.append(f"دلیل: {h(r.get('reason'))}")
            if r.get("voucher"):
                v = r["voucher"]
                lines.append(f"🎁 وچر ({h(v['pool'])}): <code>{h(v['code'])}</code>")
            elif r["status"] == "valid" and not r.get("voucher_step"):
                lines.append("🎁 بدون وچر")
            if r.get("fixed"):
                lines.append(f"🔧 اصلاح شد توسط {h(r.get('fixed_by_name'))} · {tehran(r['fixed_at'])}")
        if r.get("replies"):
            last = r["replies"][-1]
            lines.append(f"💬 {len(r['replies'])} پیام به کاربر · آخرین: «{h(last['label'])}» "
                         f"توسط {h(last['by'])} · {tehran(last['at'])}")
        return "\n".join(lines)[:4000]

    def card_buttons(self, r):
        rid = r["id"]
        reply_row = [{"text": "💬 پاسخ به کاربر", "callback_data": f"rp:{rid}"}]
        if r.get("mode") == "reply":
            rows = [[{"text": f"📄 {name}", "callback_data": f"ts:{rid}:{i}"}]
                    for i, name in enumerate(self.st.data["templates"]["custom"])]
            rows.append([{"text": "✍️ پیام آزاد", "callback_data": f"rf:{rid}"},
                         {"text": "↩️ بازگشت", "callback_data": f"bk:{rid}"}])
            return rows
        if r["status"] == "open":
            if r.get("mode") == "reject":
                rows = [[{"text": reason[:60], "callback_data": f"rj:{rid}:{i}"}]
                        for i, reason in enumerate(self.st.data["reasons"])]
                rows.append([{"text": "↩️ بازگشت", "callback_data": f"bk:{rid}"}])
                return rows
            return [[{"text": "✅ درست", "callback_data": f"ok:{rid}"},
                     {"text": "❌ نادرست", "callback_data": f"no:{rid}"}],
                    [{"text": "🔁 بررسی مجدد", "callback_data": f"rc:{rid}"}] + reply_row]
        if r["status"] == "valid" and r.get("voucher_step"):
            rows = []
            for i, (name, p) in enumerate(self.st.pools.items()):
                rows.append([{"text": f"🎁 {name} (مانده {len(p['codes'])})",
                              "callback_data": f"vp:{rid}:{i}"}])
            rows.append([{"text": "✍️ تایپ کد دستی", "callback_data": f"vm:{rid}"},
                         {"text": "بدون وچر", "callback_data": f"vn:{rid}"}])
            return rows
        if r["status"] == "valid" and not r.get("fixed"):
            return [[{"text": "🔧 اصلاح شد (اطلاع به کاربر)", "callback_data": f"fx:{rid}"}], reply_row]
        if r["status"] == "invalid":
            return [[{"text": "↩️ بازگشایی", "callback_data": f"ro:{rid}"}] + reply_row]
        return [reply_row]

    def refresh_card(self, r):
        if r.get("card"):
            self.tg.edit(self.admin, r["card"], self.card_text(r), self.card_buttons(r))

    def admin_callback(self, cq):
        data = cq.get("data", "")
        parts = data.split(":")
        act, rid = parts[0], parts[1] if len(parts) > 1 else ""
        r = self.st.reports.get(rid)
        admin = cq["from"]
        aname = admin.get("first_name") or admin.get("username") or str(admin["id"])
        if not r:
            return self.tg.answer(cq["id"], "گزارش پیدا نشد", alert=True)
        if not r.get("card"):
            r["card"] = cq["message"]["message_id"]

        if act == "ok" and r["status"] == "open":
            r.update(status="valid", decided_by=admin["id"], decided_by_name=aname,
                     decided_at=now(), voucher_step=True, mode=None)
            self.tg.answer(cq["id"], "تأیید شد؛ وچر را انتخاب کنید")
        elif act == "no" and r["status"] == "open":
            r["mode"] = "reject"
            self.tg.answer(cq["id"], "دلیل را انتخاب کنید")
        elif act == "bk":
            r["mode"] = None
            self.tg.answer(cq["id"])
        elif act == "rp":
            r["mode"] = "reply"
            self.tg.answer(cq["id"], "تمپلیت را انتخاب کنید")
        elif act == "ts":
            names = list(self.st.data["templates"]["custom"])
            i = int(parts[2])
            if i >= len(names):
                return self.tg.answer(cq["id"], "تمپلیت پیدا نشد", alert=True)
            text = self.tpl(self.st.data["templates"]["custom"][names[i]], r)
            r["mode"] = None
            self.send_reply(r, text, aname, label=names[i])
            self.tg.answer(cq["id"], "ارسال شد")
        elif act == "rf":
            prompt = self.tg.send(self.admin,
                                  f"{h(aname)}، پیام خود برای کاربر گزارش #{rid} را در <b>پاسخ به همین پیام</b> "
                                  "بنویسید (می‌توانید از {{name}} و {{title}} هم استفاده کنید؛ لغو: /cancel).",
                                  reply_to=r["card"], force_reply=True)
            self.st.data["pending_manual"][str(admin["id"])] = {
                "rid": rid, "type": "reply", "prompt": prompt["message_id"] if prompt else None}
            r["mode"] = None
            self.tg.answer(cq["id"])
        elif act == "rj" and r["status"] == "open":
            i = int(parts[2])
            reasons = self.st.data["reasons"]
            self.tg.answer(cq["id"], "ثبت شد")
            return self.decide_invalid(r, reasons[i] if i < len(reasons) else "-",
                                       by=admin["id"], by_name=aname)
        elif act == "rc" and r["status"] == "open":
            self.tg.answer(cq["id"], "در حال بررسی مجدد...")
            res = checker.check(r["pid"], r["kind"], r.get("evidence", ""), self.cfg)
            r["auto"] = {k: res[k] for k in ("verdict", "confidence", "lines", "torob_url")}
            r["offers"] = res["offers"][:3]
            p = res["product"] or {}
            if p:
                r["snapshot"] = {k: p.get(k) for k in ("price", "rrp", "discount", "seller", "available")}
        elif act == "vp" and r["status"] == "valid" and r.get("voucher_step"):
            names = list(self.st.pools.keys())
            i = int(parts[2])
            if i >= len(names) or not self.st.pools[names[i]]["codes"]:
                return self.tg.answer(cq["id"], "این استخر خالی است", alert=True)
            name = names[i]
            pool = self.st.pools[name]
            code = pool["codes"].pop(0)
            pool.setdefault("used", []).append({"code": code, "rid": rid, "uid": r["uid"], "at": now()})
            r["voucher"] = {"pool": name, "code": code, "by": aname}
            r["voucher_step"] = False
            self.st.touch()
            self.st.save(f"voucher for #{rid}")  # persist before sending
            self.tg.answer(cq["id"], "وچر ارسال شد")
            self.notify_valid(r, code, pool.get("desc", ""))
            left = len(pool["codes"])
            if left <= self.cfg["low_stock_alert"]:
                self.tg.send(self.admin, f"⚠️ موجودی استخر «{h(name)}» کم است: {left} کد مانده.")
        elif act == "vm" and r["status"] == "valid" and r.get("voucher_step"):
            prompt = self.tg.send(self.admin,
                                  f"{h(aname)}، کد وچر گزارش #{rid} را در <b>پاسخ به همین پیام</b> بنویسید "
                                  "(برای لغو: /cancel).",
                                  reply_to=r["card"], force_reply=True)
            self.st.data["pending_manual"][str(admin["id"])] = {
                "rid": rid, "prompt": prompt["message_id"] if prompt else None}
            self.tg.answer(cq["id"])
        elif act == "vn" and r["status"] == "valid" and r.get("voucher_step"):
            r["voucher_step"] = False
            self.tg.answer(cq["id"], "بدون وچر ثبت شد")
            self.notify_valid(r, None, "")
        elif act == "fx" and r["status"] == "valid" and not r.get("fixed"):
            r.update(fixed=True, fixed_by=admin["id"], fixed_by_name=aname, fixed_at=now())
            self.tg.answer(cq["id"], "به کاربر اطلاع داده شد")
            self._to_user(r["uid"], self.tpl("fixed", r))
        elif act == "ro" and r["status"] == "invalid":
            r.update(status="open", reason=None, mode=None)
            self.tg.answer(cq["id"], "دوباره باز شد")
        else:
            return self.tg.answer(cq["id"], "این اقدام قبلاً انجام شده است")
        self.st.touch()
        self.refresh_card(r)

    def decide_invalid(self, r, reason, by, by_name=None):
        r.update(status="invalid", reason=reason, decided_by=by,
                 decided_by_name=by_name or ("بات" if by == "auto" else str(by)),
                 decided_at=now(), mode=None)
        self.st.touch()
        self._to_user(r["uid"], self.tpl("invalid", r, reason=reason))
        self.refresh_card(r)

    def notify_valid(self, r, code, desc):
        self._to_user(r["uid"], self.tpl("valid", r))
        if code:
            self._to_user(r["uid"], self.tpl("voucher", r, code=code, desc=desc))
        self._open_thread(r)

    def _to_user(self, uid, text):
        try:
            self.tg.send(int(uid), text)
            return True
        except Exception as e:  # user blocked the bot, etc.
            self.tg.send(self.admin, f"⚠️ ارسال پیام به کاربر {uid} ناموفق بود: {h(str(e)[:150])}")
            return False

    def _open_thread(self, r):
        """Let the user's next free-text message reach the admins as a reply to this card."""
        u = self.st.users.get(r["uid"])
        if u is not None:
            u["thread"] = {"rid": r["id"], "at": now()}
            self.st.touch()

    def send_reply(self, r, html_text, admin_name, label="پیام آزاد"):
        if self._to_user(r["uid"], html_text):
            r.setdefault("replies", []).append({"by": admin_name, "label": label, "at": now()})
            self._open_thread(r)
            self.st.touch()
            self.refresh_card(r)

    def user_followup(self, uid, u, rid, msg):
        r = self.st.reports[rid]
        head = self.tg.send(self.admin, f"↩️ پاسخ کاربر به گزارش <b>#{rid}</b> "
                                        "(برای جواب، روی همین پیام یا پیام زیرش Reply کنید)",
                            reply_to=r.get("card"))
        self.map_msg((head or {}).get("message_id"), rid)
        c = self.tg.copy(self.admin, uid, msg["message_id"], reply_to=(head or {}).get("message_id"))
        self.map_msg((c or {}).get("message_id"), rid)
        return self.tg.send(uid, "پیام شما برای تیم بررسی ارسال شد ✔️")

    # ---------------------------------------------------------------- admin messages
    def admin_message(self, msg):
        aid = str(msg["from"]["id"])
        text = msg.get("text") or msg.get("caption") or ""
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        arg = text[len(text.split()[0]):].strip() if cmd else ""

        pend = self.st.data["pending_manual"].get(aid)
        if pend and not cmd and msg.get("text"):
            if pend.get("type") == "reply":
                return self.manual_reply(aid, pend, msg["text"], msg)
            return self.manual_code(aid, pend, msg["text"].strip(), msg)
        replied = (msg.get("reply_to_message") or {}).get("message_id")
        if replied and not cmd and msg.get("text"):
            rid = self.st.data["msgmap"].get(str(replied))
            if rid and rid in self.st.reports:
                aname = msg["from"].get("first_name") or aid
                self.send_reply(self.st.reports[rid], self.tpl(msg["text"], self.st.reports[rid]), aname)
                return self.tg.send(self.admin, f"برای کاربر گزارش #{rid} ارسال شد ✔️",
                                    reply_to=msg["message_id"])
        if cmd == "/cancel" and pend:
            self.st.data["pending_manual"].pop(aid, None)
            self.st.touch()
            return self.tg.send(self.admin, "لغو شد.", reply_to=msg["message_id"])

        if cmd in ("/help", "/start"):
            return self.tg.send(self.admin, T.ADMIN_HELP)
        if cmd == "/id":
            return self.tg.send(self.admin, f"chat id: <code>{self.admin}</code>")
        if cmd in ("/templates", "/settpl", "/deltpl", "/reasons", "/addreason", "/delreason"):
            return self.template_command(cmd, arg)
        if cmd == "/stats":
            return self.tg.send(self.admin, self.stats())
        if cmd == "/open":
            return self.tg.send(self.admin, self.open_list())
        if cmd == "/pools":
            return self.tg.send(self.admin, self.pools_text())
        if cmd == "/addpool":
            return self.add_pool(arg, msg)
        if cmd == "/delpool":
            if arg in self.st.pools:
                n = len(self.st.pools[arg]["codes"])
                del self.st.pools[arg]
                self.st.touch()
                return self.tg.send(self.admin, f"استخر «{h(arg)}» حذف شد ({n} کد مصرف‌نشده).")
            return self.tg.send(self.admin, "استخری با این نام نیست. /pools")
        if cmd in ("/ban", "/unban"):
            uid = re.sub(r"\D", "", arg)
            if not uid:
                return self.tg.send(self.admin, "مثال: /ban 123456789")
            self.st.user(uid)["banned"] = cmd == "/ban"
            self.st.touch()
            return self.tg.send(self.admin, f"کاربر {uid} {'مسدود' if cmd == '/ban' else 'آزاد'} شد.")
        if cmd == "/msg":
            m = re.match(r"(\d+)\s+(.+)", arg, re.S)
            if not m:
                return self.tg.send(self.admin, "مثال: /msg 123456789 متن پیام")
            self._to_user(m.group(1), h(m.group(2)))
            return self.tg.send(self.admin, "ارسال شد ✔️", reply_to=msg["message_id"])
        if cmd == "/check":
            pid = digikala.find_product_id(arg)
            if not pid:
                return self.tg.send(self.admin, "مثال: /check https://www.digikala.com/product/dkp-123456/")
            out = []
            for kind in ("fake", "cheaper"):
                res = checker.check(pid, kind, "", self.cfg)
                out.append(f"<b>{T.KIND_FA[kind]}</b>: {T.VERDICT_FA[res['verdict']]} "
                           f"({res['confidence']}%)\n" + "\n".join(h(x) for x in res["lines"]))
            p = res["product"] or {}
            head = f"🧪 <b>{h(p.get('title', pid))}</b>\nقیمت: {checker.money(p.get('price'))} · قبل: {checker.money(p.get('rrp'))}\n\n"
            return self.tg.send(self.admin, (head + "\n\n".join(out))[:4000])

    def manual_reply(self, aid, pend, text, msg):
        self.st.data["pending_manual"].pop(aid, None)
        self.st.touch()
        r = self.st.reports.get(pend["rid"])
        if not r:
            return self.tg.send(self.admin, "گزارش پیدا نشد.", reply_to=msg["message_id"])
        aname = msg["from"].get("first_name") or aid
        self.send_reply(r, self.tpl(text, r), aname)
        return self.tg.send(self.admin, f"برای کاربر گزارش #{r['id']} ارسال شد ✔️",
                            reply_to=msg["message_id"])

    def template_command(self, cmd, arg):
        t = self.st.data["templates"]
        send = lambda x: self.tg.send(self.admin, x[:4000])
        if cmd == "/templates":
            out = ["<b>تمپلیت‌های سیستمی</b> (پیام‌های خودکار بات)"]
            for key, (label, default) in TP.SYSTEM.items():
                mark = "✏️ ویرایش‌شده" if key in t["system"] else "پیش‌فرض"
                out.append(f"\n• <code>{key}</code> — {label} ({mark})\n{h(TP.get(self.st, key)[:300])}")
            out.append("\n<b>تمپلیت‌های پاسخ</b> (دکمه 💬 روی کارت گزارش)")
            for name, text in t["custom"].items():
                out.append(f"\n• <code>{h(name)}</code>\n{h(text[:300])}")
            out.append("\n" + T.TEMPLATE_HELP)
            return send("\n".join(out))
        if cmd == "/settpl":
            key, _, text = arg.partition("\n")
            key, text = key.strip(), text.strip()
            if not key or not text:
                return send(T.TEMPLATE_HELP)
            if key in TP.SYSTEM:
                t["system"][key] = text
                where = f"سیستمی «{TP.SYSTEM[key][0]}»"
            else:
                if len(t["custom"]) >= 25 and key not in t["custom"]:
                    return send("حداکثر 25 تمپلیت پاسخ مجاز است؛ ابتدا یکی را حذف کنید.")
                t["custom"][key[:40]] = text
                where = f"پاسخ «{h(key[:40])}»"
            self.st.touch()
            return send(f"تمپلیت {where} ذخیره شد ✔️\nپیش‌نمایش:\n\n" +
                        TP.render(text, name="علی", rid="123", title="نمونه کالا",
                                  url="https://www.digikala.com/product/dkp-1/", code="ABC123",
                                  desc="توضیح وچر", reason="دلیل نمونه", price="1,000,000",
                                  limit=self.cfg["limits"]["reports_per_user_per_day"]))
        if cmd == "/deltpl":
            key = arg.strip()
            if key in t["system"]:
                del t["system"][key]
                self.st.touch()
                return send(f"تمپلیت سیستمی <code>{key}</code> به حالت پیش‌فرض برگشت.")
            if key in t["custom"]:
                del t["custom"][key]
                self.st.touch()
                return send(f"تمپلیت «{h(key)}» حذف شد.")
            return send("تمپلیتی با این نام نیست. /templates")
        if cmd == "/reasons":
            rs = self.st.data["reasons"]
            return send("<b>دلایل رد</b>\n" + "\n".join(f"{i + 1}. {h(x)}" for i, x in enumerate(rs))
                        + "\n\n/addreason متن — افزودن\n/delreason شماره — حذف")
        if cmd == "/addreason":
            if not arg:
                return send("مثال: /addreason قیمت فروشگاه دیگر شامل هزینه ارسال نیست")
            self.st.data["reasons"].append(arg.strip()[:120])
            self.st.touch()
            return send("دلیل اضافه شد ✔️")
        if cmd == "/delreason":
            rs = self.st.data["reasons"]
            try:
                removed = rs.pop(int(re.sub(r"\D", "", arg)) - 1)
            except (ValueError, IndexError):
                return send("مثال: /delreason 2")
            self.st.touch()
            return send(f"حذف شد: {h(removed)}")

    def manual_code(self, aid, pend, code, msg):
        r = self.st.reports.get(pend["rid"])
        self.st.data["pending_manual"].pop(aid, None)
        self.st.touch()
        if not r or r["status"] != "valid" or not r.get("voucher_step"):
            return self.tg.send(self.admin, "این گزارش دیگر منتظر وچر نیست.", reply_to=msg["message_id"])
        code = code.split()[0][:64]
        aname = msg["from"].get("first_name") or str(aid)
        r["voucher"] = {"pool": "دستی", "code": code, "by": aname}
        r["voucher_step"] = False
        self.st.save(f"manual voucher #{r['id']}")
        self.notify_valid(r, code, "")
        self.tg.send(self.admin, f"کد برای گزارش #{r['id']} ارسال شد ✔️", reply_to=msg["message_id"])
        self.refresh_card(r)

    def add_pool(self, arg, msg):
        first_line, _, rest = arg.partition("\n")
        name, _, desc = first_line.partition("|")
        name, desc = name.strip(), desc.strip()
        if not name:
            return self.tg.send(self.admin, "مثال:\n/addpool وچر۵۰ | ۵۰ هزار تومان تخفیف روی خرید بالای ۵۰۰ هزار\nCODE1\nCODE2")
        codes = _parse_codes(rest)
        doc = msg.get("document")
        if doc:
            raw = self.tg.download(doc["file_id"])
            codes += _parse_file(doc.get("file_name", ""), raw)
        pool = self.st.pools.setdefault(name, {"desc": desc, "codes": [], "used": []})
        if desc:
            pool["desc"] = desc
        existing = set(pool["codes"]) | {x["code"] for x in pool.get("used", [])}
        new = [c for c in dict.fromkeys(codes) if c not in existing]
        pool["codes"].extend(new)
        self.st.touch()
        return self.tg.send(self.admin, f"استخر «{h(name)}»: {len(new)} کد جدید اضافه شد "
                                        f"(تکراری: {len(codes) - len(new)}) · موجودی: {len(pool['codes'])}")

    def pools_text(self):
        if not self.st.pools:
            return "هنوز استخری ساخته نشده. /addpool"
        lines = ["<b>استخرهای وچر</b>"]
        for name, p in self.st.pools.items():
            lines.append(f"• {h(name)}: مانده {len(p['codes'])} · مصرف‌شده {len(p.get('used', []))}"
                         + (f"\n   {h(p['desc'])}" if p.get("desc") else ""))
        return "\n".join(lines)

    def open_list(self):
        opn = [r for r in self.st.reports.values() if r["status"] == "open"]
        waiting = [r for r in self.st.reports.values() if r["status"] == "valid" and r.get("voucher_step")]
        if not opn and not waiting:
            return "گزارش بازی نیست ✨"
        cid = str(self.admin).replace("-100", "")
        fmt = lambda r: (f"<a href=\"https://t.me/c/{cid}/{r.get('card')}\">#{r['id']}</a> "
                         f"{T.VERDICT_FA[r['auto']['verdict']]} · {h(r['title'][:40])}")
        out = [f"<b>باز ({len(opn)})</b>"] + [fmt(r) for r in opn[-30:]]
        if waiting:
            out += [f"\n<b>منتظر وچر ({len(waiting)})</b>"] + [fmt(r) for r in waiting[-30:]]
        return "\n".join(out)

    def stats(self):
        reps = list(self.st.reports.values())
        if not reps:
            return "هنوز گزارشی ثبت نشده."
        c = lambda f: sum(1 for r in reps if f(r))
        decided = [r for r in reps if r["status"] != "open" and r.get("decided_at")]
        avg = (sum(r["decided_at"] - r["created"] for r in decided) / len(decided) / 60) if decided else 0
        agree = [r for r in decided if r["auto"]["verdict"] != "review"]
        acc = c(lambda r: r in agree and (
            (r["auto"]["verdict"] == "likely_valid") == (r["status"] == "valid")))
        today = c(lambda r: now() - r["created"] < DAY)
        top = {}
        for r in reps:
            if r["status"] == "valid":
                top[r["title"][:40]] = top.get(r["title"][:40], 0) + 1
        top_s = "\n".join(f"  • {h(k)}: {v}" for k, v in sorted(top.items(), key=lambda x: -x[1])[:5])
        vouchers = c(lambda r: bool(r.get("voucher")))
        return (
            "<b>📊 آمار</b>\n"
            f"کل گزارش‌ها: {len(reps)} (24 ساعت اخیر: {today})\n"
            f"کاربران: {len({r['uid'] for r in reps})}\n"
            f"تخفیف فیک: {c(lambda r: r['kind'] == 'fake')} · ارزان‌تر: {c(lambda r: r['kind'] == 'cheaper')}\n"
            f"✅ {c(lambda r: r['status'] == 'valid')} · ❌ {c(lambda r: r['status'] == 'invalid')} "
            f"· ⏳ {c(lambda r: r['status'] == 'open')} · 🔧 {c(lambda r: r.get('fixed'))}\n"
            f"وچر ارسال‌شده: {vouchers}\n"
            f"میانگین زمان تصمیم: {avg:.0f} دقیقه\n"
            f"دقت بررسی خودکار (روی موارد قطعی): {acc}/{len(agree)}\n"
            + (f"پرتکرارترین کالاهای تأییدشده:\n{top_s}" if top_s else "")
        )


# ---------------------------------------------------------------- helpers
CODE_RE = re.compile(r"^[A-Za-z0-9\-_]{4,64}$")
HEADERS = {"code", "codes", "voucher", "vouchers", "coupon", "promo", "promocode"}


def _ok_code(c):
    return bool(CODE_RE.match(c)) and c.lower() not in HEADERS


def _parse_codes(text):
    out = []
    for line in (text or "").splitlines():
        cell = re.split(r"[,;\t ]", line.strip())[0].strip().strip('"')
        if _ok_code(cell):
            out.append(cell)
    return out


def _parse_file(name, raw: bytes):
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
        cells = []
        for row in wb.active.iter_rows(values_only=True):
            if row and row[0] is not None:
                cells.append(str(row[0]).strip())
        return [c for c in cells if _ok_code(c)]
    text = raw.decode("utf-8-sig", errors="ignore")
    rows = csv.reader(io.StringIO(text))
    return [r[0].strip() for r in rows if r and _ok_code(r[0].strip())]


def _first_red(lines):
    for l in lines:
        if l.startswith("🔴"):
            return l[2:].strip()
    return "قیمت دیجی‌کالا با بازار همخوان است"
