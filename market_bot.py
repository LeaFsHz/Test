"""market_shin - the "bookmaker" family bot. Pure standard library (no numpy/scipy/pandas).

Idea: the bookmaker's prices already hold a lot of information. Remove the margin properly
(Shin, 1993), then rebuild a Dixon-Coles scoreline matrix whose 1X2 and total-goals match the market,
and read 1X2 / over-under 2.5 / BTTS from it. This bot adds NO opinion of its own: it is the
yardstick every other bot must beat.

Method follows the published ideas of Shin (1993) and Dixon & Coles (1997); the structure of the
lambda solve (coarse grid, then local refinement) is inspired by ohugonnot/golazo (MIT), re-written here.

gorev fixtures need  odds: {"home":..,"draw":..,"away":..}  and optionally  "over25","under25" (decimal odds).
"""
import math
import sys

import worker_io as w

RHO = -0.13       # Dixon-Coles low-score dependence (typical literature value)
MAXG = 10         # score matrix 0..10 goals
TOTAL_PRIOR = 2.65  # used only when no over/under price is available (weak pull)
VERSION = "shin-dc-1"


# ---------------------------------------------------------------- de-vig
def _check_odds(odds):
    if any((not isinstance(o, (int, float))) or o != o or o <= 1.0 for o in odds):
        raise ValueError("odds must be numbers > 1.0, got %r" % (odds,))


def proportional(odds):
    _check_odds(odds)
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def shin(odds, iters=100):
    """Shin's method: solve the insider-trading share z so the probabilities sum to 1."""
    _check_odds(odds)
    pi = [1.0 / o for o in odds]
    s = sum(pi)
    if s <= 1.0 + 1e-12:
        return [x / s for x in pi]  # no margin to remove

    def probs(z):
        return [(math.sqrt(z * z + 4.0 * (1.0 - z) * (p * p) / s) - z) / (2.0 * (1.0 - z)) for p in pi]

    lo, hi = 0.0, 0.999
    for _ in range(iters):
        z = (lo + hi) / 2.0
        if sum(probs(z)) > 1.0:
            lo = z
        else:
            hi = z
    p = probs((lo + hi) / 2.0)
    t = sum(p)
    return [x / t for x in p]


# ---------------------------------------------------------------- score matrix
def pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def tau(x, y, lh, la, rho):
    if x == 0 and y == 0:
        return 1.0 - lh * la * rho
    if x == 0 and y == 1:
        return 1.0 + lh * rho
    if x == 1 and y == 0:
        return 1.0 + la * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lh, la, rho=RHO, n=MAXG):
    ph = [pois(k, lh) for k in range(n + 1)]
    pa = [pois(k, la) for k in range(n + 1)]
    m = [[ph[x] * pa[y] * max(tau(x, y, lh, la, rho), 0.0) for y in range(n + 1)] for x in range(n + 1)]
    tot = sum(sum(r) for r in m)
    return [[v / tot for v in r] for r in m]


def outcome_probs(m):
    h = d = a = 0.0
    for x, row in enumerate(m):
        for y, v in enumerate(row):
            if x > y:
                h += v
            elif x == y:
                d += v
            else:
                a += v
    return h, d, a


def over_prob(m, line=2.5):
    return sum(v for x, row in enumerate(m) for y, v in enumerate(row) if x + y > line)


def btts_prob(m):
    return sum(v for x, row in enumerate(m) for y, v in enumerate(row) if x >= 1 and y >= 1)


def solve_lambdas(target, over_target=None):
    """Find (lh, la) whose matrix matches de-vigged 1X2 (+ P(over 2.5) if known). Grid, then refine."""
    th, td, ta = target

    def err(lh, la):
        m = score_matrix(lh, la, n=8)
        h, d, a = outcome_probs(m)
        e = (h - th) ** 2 + (d - td) ** 2 + (a - ta) ** 2
        if over_target is not None:
            e += (over_prob(m) - over_target) ** 2
        else:
            e += 0.01 * min((lh + la - TOTAL_PRIOR) ** 2, 1.0)
        return e

    best = (9e9, 1.3, 1.1)
    steps = [0.1 * i for i in range(1, 46)]
    for lh in steps:
        for la in steps:
            e = err(lh, la)
            if e < best[0]:
                best = (e, lh, la)
    _, bh, ba = best
    span, step = 0.12, 0.01
    for _ in range(2):
        cand = best
        i = -int(span / step)
        while i <= int(span / step):
            j = -int(span / step)
            while j <= int(span / step):
                lh, la = bh + i * step, ba + j * step
                if lh > 0.02 and la > 0.02:
                    e = err(lh, la)
                    if e < cand[0]:
                        cand = (e, lh, la)
                j += 1
            i += 1
        best = cand
        _, bh, ba = best
        span, step = step * 2, step / 4.0
    return best[1], best[2]


# ---------------------------------------------------------------- worker
def predict_fixture(odds):
    p1x2 = shin([odds["home"], odds["draw"], odds["away"]])
    over_t = None
    if odds.get("over25") and odds.get("under25"):
        over_t = shin([odds["over25"], odds["under25"]])[0]
    lh, la = solve_lambdas(tuple(p1x2), over_t)
    m = score_matrix(lh, la)
    over = over_t if over_t is not None else over_prob(m)
    btts = btts_prob(m)
    return {"1x2": p1x2, "over": over, "btts": btts, "lh": lh, "la": la}


def main():
    g = w.load_gorev()
    preds, skipped, notes = [], [], ["1x2: de-vigged market price (Shin)",
                                   "btts: MODEL-DERIVED from the market-fitted Dixon-Coles matrix (rho=%.2f), not a market price" % RHO]
    no_ou = 0
    for fx in g["fixtures"]:
        fid = fx["fixture_id"]
        o = fx.get("odds") or {}
        try:
            if not all(o.get(k) and float(o[k]) > 1.0 for k in ("home", "draw", "away")):
                skipped.append({"fixture_id": fid, "reason": "no usable 1X2 odds"})
                continue
            r = predict_fixture({k: float(v) for k, v in o.items() if v})
        except Exception as e:  # one bad fixture must not kill the run
            skipped.append({"fixture_id": fid, "reason": "error: %s" % e})
            continue
        h, d, a = r["1x2"]
        over, btts = r["over"], r["btts"]
        if not (o.get("over25") and o.get("under25")):
            no_ou += 1
        preds += [
            {"fixture_id": fid, "market": "1x2", "selection": "H", "prob": h},
            {"fixture_id": fid, "market": "1x2", "selection": "D", "prob": d},
            {"fixture_id": fid, "market": "1x2", "selection": "A", "prob": max(0.0, 1.0 - h - d)},
            {"fixture_id": fid, "market": "ou25", "selection": "over", "prob": over},
            {"fixture_id": fid, "market": "ou25", "selection": "under", "prob": max(0.0, 1.0 - over)},
            {"fixture_id": fid, "market": "btts", "selection": "yes", "prob": btts},
            {"fixture_id": fid, "market": "btts", "selection": "no", "prob": max(0.0, 1.0 - btts)},
        ]
    if no_ou:
        notes.append("ou25: prior-assisted (no over/under price) for %d fixture(s)" % no_ou)
    status = "ok" if preds and not skipped else ("partial" if preds else "error")
    if status == "error":
        w.fail("no fixture had usable odds")
    w.write_sonuc(preds, status=status, skipped=skipped, notes=notes,
                  bot_version=VERSION, model_family="bookmaker", model_version=VERSION)


if __name__ == "__main__":
    main()
