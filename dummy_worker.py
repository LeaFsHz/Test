"""Test worker with selectable behaviour (gorev params.mode). NOT a model: probabilities are hash-derived."""
import hashlib
import os
import sys
import time

import worker_io as w


def probs(bot, fid):
    d = hashlib.sha256((bot + fid).encode()).digest()
    a, b, c = d[0] + 1, d[1] + 1, d[2] + 1
    s = float(a + b + c)
    o = (d[3] + 1) / 257.0
    return (a / s, b / s, c / s), o


def main():
    g = w.load_gorev()
    prm = g.get("params", {})
    mode = prm.get("mode", "ok")
    if mode == "crash":
        raise RuntimeError("boom (intentional)")
    if mode == "hang":
        time.sleep(600)
    if mode == "halfwrite":  # writes a broken sonuc.json and never writes DONE
        (w.task_dir() / "sonuc.json").write_text('{"schema": "teka.sonuc/1", "predi')
        return
    if mode == "needs_data":
        w.needs_data(["injuries_T1"])
    if mode == "need_if_missing":
        w.data_path("injuries_T1")  # exits 3 if the dataset is not in gorev["data"]

    notes = []
    if mode == "net":
        import urllib.request
        try:
            urllib.request.urlopen("http://example.com", timeout=3)
            notes.append("net:REACHED")
        except Exception as e:
            notes.append("net:" + type(getattr(e, "reason", e)).__name__)  # urllib wraps the real error in URLError
    if mode == "env":
        notes.append("env_key_present=%s" % ("TEKA_TEST_API_KEY" in os.environ))

    preds, skipped = [], []
    for fx in g["fixtures"]:
        (h, d, a), over = probs(g["bot"], fx["fixture_id"])
        if mode == "badsum":
            h = h + 0.3
        preds += [
            {"fixture_id": fx["fixture_id"], "market": "1x2", "selection": "H", "prob": h},
            {"fixture_id": fx["fixture_id"], "market": "1x2", "selection": "D", "prob": d},
            {"fixture_id": fx["fixture_id"], "market": "1x2", "selection": "A", "prob": a},
            {"fixture_id": fx["fixture_id"], "market": "ou25", "selection": "over", "prob": over},
            {"fixture_id": fx["fixture_id"], "market": "ou25", "selection": "under", "prob": 1.0 - over},
        ]
    w.write_sonuc(preds, skipped=skipped, notes=notes, bot_version="0.1",
                  model_family=prm.get("family", "dummy"), model_version="v0")


if __name__ == "__main__":
    main()
