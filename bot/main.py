"""Entry point: long-poll Telegram for RUN_MINUTES, saving state after every batch."""
import json
import os
import time
import traceback

from .handlers import Bot
from .state import State
from .tg import from_env


def load_config():
    with open(os.environ.get("CONFIG", "config.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg = load_config()
    tg = from_env()
    admin = int(os.environ.get("ADMIN_GROUP_ID") or 0)
    if not admin:
        print("ADMIN_GROUP_ID not set: setup mode — send /id in the admin group to get its id")
    st = State(os.environ.get("STATE_DIR", "state"))
    bot = Bot(tg, st, cfg, admin)
    minutes = float(os.environ.get("RUN_MINUTES") or cfg["run_minutes"])
    end = time.time() + minutes * 60

    tg.call("deleteWebhook")
    me = tg.call("getMe")
    print(f"running as @{me['username']} for {minutes:.0f} min")

    while time.time() < end - 5:
        wait = int(min(20, max(1, end - time.time() - 5)))
        try:
            ups = tg.updates(st.data["offset"] or None, timeout=wait)
        except Exception as e:
            print("getUpdates failed:", e)
            time.sleep(5)
            continue
        for u in ups:
            try:
                bot.handle(u)
            except Exception:
                traceback.print_exc()
            st.data["offset"] = u["update_id"] + 1
            st.touch()
        st.save()
    st.save()
    print("done")


if __name__ == "__main__":
    main()
