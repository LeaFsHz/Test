"""golazo - adapter that runs the REAL GitHub project  https://github.com/ohugonnot/golazo  (MIT licence).

TeKa does not copy or modify golazo. You clone it once (see README) and this adapter imports its src/model.py:
    demargin (Shin) -> solve_lambdas (Dixon-Coles inversion) -> score_matrix -> calibrate_to_outcomes -> outcome_probs
Everything golazo does for football "pools" (rarity bonus, x2 booster) is ignored; only its probability engine is used.

Where to find the clone (first match wins): env TEKA_GOLAZO_DIR, ../golazo/src next to teka, ~/botlar/golazo/src.
"""
import importlib.util
import math
import os
import sys
from pathlib import Path

import worker_io as w
from market_bot import shin as shin_two_way

RHO = -0.13
HERE = Path(__file__).resolve().parent


def candidates():
    out = []
    if os.environ.get("TEKA_GOLAZO_DIR"):
        out.append(Path(os.environ["TEKA_GOLAZO_DIR"]))
    out += [HERE.parent.parent / "golazo" / "src", Path.home() / "botlar" / "golazo" / "src"]
    return out


def find_model():
    for d in candidates():
        if (d / "model.py").is_file():
            return d / "model.py"
    return None


def load_model(path):
    spec = importlib.util.spec_from_file_location("golazo_model", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def commit_of(model_path):
    """Best-effort git commit of the clone (for model_version / reproducibility)."""
    try:
        git = model_path.parent.parent / ".git"
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            return (git / head.split(" ", 1)[1]).read_text().strip()[:10]
        return head[:10]
    except OSError:
        return "unknown"


def total_from_over(p_over, line=2.5):
    """Expected total goals T such that P(Poisson(T) > line) = p_over (bisection)."""
    k = int(math.floor(line))

    def over(t):
        return 1.0 - sum(math.exp(-t) * t ** i / math.factorial(i) for i in range(k + 1))

    lo, hi = 0.3, 7.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if over(mid) < p_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def main():
    mp = find_model()
    if mp is None:
        w.needs_data(["golazo-clone: git clone https://github.com/ohugonnot/golazo ~/botlar/golazo"])
    sys.path.append(str(mp.parent))  # sibling imports inside the project (appended: must never shadow our own modules)
    try:
        gm = load_model(mp)
        for fn in ("demargin", "solve_lambdas", "score_matrix", "calibrate_to_outcomes", "outcome_probs"):
            if not callable(getattr(gm, fn, None)):
                raise AttributeError("function %s not found (project changed?)" % fn)
    except Exception as e:  # installation/version problem, not a prediction failure
        w.needs_data(["golazo-import: %s: %s" % (type(e).__name__, e)])
    g = w.load_gorev()
    preds, skipped, notes = [], [], []
    for fx in g["fixtures"]:
        fid, o = fx["fixture_id"], fx.get("odds") or {}
        try:
            if not all(o.get(k) and float(o[k]) > 1.0 for k in ("home", "draw", "away")):
                skipped.append({"fixture_id": fid, "reason": "no usable 1X2 odds"})
                continue
            ph, pd, pa = gm.demargin(float(o["home"]), float(o["draw"]), float(o["away"]), method="shin")
            total = None
            if o.get("over25") and o.get("under25"):
                total = total_from_over(shin_two_way([float(o["over25"]), float(o["under25"])])[0])  # Shin, like the 1X2
            lh, la = gm.solve_lambdas(ph, pd, pa, total, RHO)
            mat = gm.calibrate_to_outcomes(gm.score_matrix(lh, la, RHO, 10), ph, pd, pa)
            h, d, a = gm.outcome_probs(mat)
            over = sum(p for (i, j), p in mat.items() if i + j > 2.5)
            btts = sum(p for (i, j), p in mat.items() if i >= 1 and j >= 1)
        except Exception as e:
            skipped.append({"fixture_id": fid, "reason": "golazo error: %s" % e})
            continue
        preds += [
            {"fixture_id": fid, "market": "1x2", "selection": "H", "prob": h},
            {"fixture_id": fid, "market": "1x2", "selection": "D", "prob": d},
            {"fixture_id": fid, "market": "1x2", "selection": "A", "prob": max(0.0, 1.0 - h - d)},
            {"fixture_id": fid, "market": "ou25", "selection": "over", "prob": over},
            {"fixture_id": fid, "market": "ou25", "selection": "under", "prob": max(0.0, 1.0 - over)},
            {"fixture_id": fid, "market": "btts", "selection": "yes", "prob": btts},
            {"fixture_id": fid, "market": "btts", "selection": "no", "prob": max(0.0, 1.0 - btts)},
        ]
    notes.append("engine: ohugonnot/golazo @ %s (MIT), rho=%.2f; 1x2 = Shin de-vigged price; ou25 and btts from golazo's score matrix" % (commit_of(mp), RHO))
    status = "ok" if preds and not skipped else ("partial" if preds else "error")
    if status == "error":
        w.fail("no fixture had usable odds")
    w.write_sonuc(preds, status=status, skipped=skipped, notes=notes, bot_version="adapter-1",
                  model_family="bookmaker", model_version="golazo@" + commit_of(mp))


if __name__ == "__main__":
    main()
