import json

from app.config import Config
from app.cookie_watcher import CookieWatcher
from app.cookies import CookieStore
from app.gemini_service import GeminiService


class _Svc:
    def __init__(self):
        self.resets = 0

    async def reset(self):
        self.resets += 1


def _watcher(tmp_path, **cfg_over):
    cf = tmp_path / "cookies.json"
    cf.write_text(json.dumps([{"name": "__Secure-1PSID", "value": "g.aOLD"}]))
    values = {"data_dir": str(tmp_path / "d"), "cookie_file": str(cf)}
    values.update(cfg_over)
    cfg = Config(values, None)
    store = CookieStore(cf)
    svc = _Svc()
    return CookieWatcher(svc, store, cfg), svc, cf


async def test_reset_on_psid_change(tmp_path):
    w, svc, cf = _watcher(tmp_path)
    await w._tick()
    assert svc.resets == 0  # unchanged
    cf.write_text(json.dumps([{"name": "__Secure-1PSID", "value": "g.aNEW"}]))
    await w._tick()
    assert svc.resets == 1
    await w._tick()
    assert svc.resets == 1  # no repeat for the same value


async def test_no_reset_on_psidts_only_change(tmp_path):
    w, svc, cf = _watcher(tmp_path)
    cf.write_text(json.dumps([
        {"name": "__Secure-1PSID", "value": "g.aOLD"},
        {"name": "__Secure-1PSIDTS", "value": "sidts-rotated"},
    ]))
    await w._tick()
    assert svc.resets == 0


async def test_watch_file_mirrored_into_cookie_file(tmp_path):
    wf = tmp_path / "drop.txt"
    wf.write_text("__Secure-1PSID=g.aDROPPED; __Secure-1PSIDTS=sidts-y")
    w, svc, cf = _watcher(tmp_path, cookie_watch_file=str(wf))
    await w._tick()
    written = json.loads(cf.read_text())
    assert any(x["value"] == "g.aDROPPED" for x in written)
    # and that PSID change triggers a rebuild on the same tick or the next
    assert svc.resets >= 1
