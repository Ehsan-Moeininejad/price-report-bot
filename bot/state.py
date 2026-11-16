"""Bot state kept as one JSON file in a PRIVATE GitHub repo (checked out to STATE_DIR).

Every change is committed and pushed before the bot asks Telegram for new
updates, so a crash never loses a voucher assignment or a decision.
"""
import json
import os
import subprocess
import time

EMPTY = {
    "offset": 0,
    "next_report_id": 1,
    "users": {},        # uid -> {name, username, step, draft, banned, reports:[ids]}
    "reports": {},      # rid -> report dict
    "pools": {},        # name -> {desc, codes:[...], used:[{code, rid, uid, at}]}
    "pending_manual": {},  # admin_uid -> {rid, prompt_msg}
}


class State:
    def __init__(self, directory: str):
        self.dir = directory
        self.path = os.path.join(directory, "data", "state.json")
        self.git = os.environ.get("STATE_GIT", "1") == "1"
        self.data = json.loads(json.dumps(EMPTY))
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            self.data.update(loaded)
        self.dirty = False

    # -- shortcuts -----------------------------------------------------
    @property
    def users(self):
        return self.data["users"]

    @property
    def reports(self):
        return self.data["reports"]

    @property
    def pools(self):
        return self.data["pools"]

    def user(self, uid, tg_user=None):
        uid = str(uid)
        u = self.users.setdefault(uid, {"reports": [], "banned": False,
                                        "step": None, "draft": None})
        if tg_user:
            name = " ".join(x for x in [tg_user.get("first_name"),
                                        tg_user.get("last_name")] if x)
            if u.get("name") != name or u.get("username") != tg_user.get("username"):
                u["name"] = name
                u["username"] = tg_user.get("username")
                self.dirty = True
        return u

    def new_report_id(self):
        rid = self.data["next_report_id"]
        self.data["next_report_id"] = rid + 1
        self.dirty = True
        return str(rid)

    def touch(self):
        self.dirty = True

    # -- persistence -----------------------------------------------------
    def save(self, message="bot: update state"):
        if not self.dirty:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self.dirty = False
        if not self.git:
            return
        run = lambda *a: subprocess.run(["git", "-C", self.dir, *a],
                                        capture_output=True, text=True)
        run("add", "-A")
        if run("diff", "--cached", "--quiet").returncode == 0:
            return
        run("commit", "-q", "-m", message)
        for attempt in range(4):
            r = run("push", "-q")
            if r.returncode == 0:
                return
            run("pull", "-q", "--rebase", "-X", "theirs")
            time.sleep(2)
        print("WARNING: state push failed:", r.stderr[:300])
