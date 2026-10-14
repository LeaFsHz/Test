"""elo_poisson - Elo ratings (World-Football-Elo style goal-difference multiplier) mapped to expected goals,
then an independent-Poisson score grid. Pure standard library.

Elo update:  R += K * G * (S - E),  E = 1 / (1 + 10^(-(dr)/400)),  dr = R_home + HFA - R_away,
             G = 1 (gd<=1) | 1.5 (gd=2) | (11+gd)/8 (gd>=3).
Goals:       lh = T/2 * exp(+c*dr),  la = T/2 * exp(-c*dr),  T = average total goals.
K, c and HFA are picked from a small grid by WALK-FORWARD log-likelihood (each match is scored with ratings
that existed BEFORE it), so there is no look-ahead in the choice of parameters.
For every fixture everything (ratings, parameters, average goals) is computed ONLY from matches before its kickoff day.
"""
import math

import fb_common as fc
import worker_io as w

VERSION = "elo-poisson-1"
K_GRID = (15.0, 25.0, 35.0)
C_GRID = (0.0010, 0.0016, 0.0022)
HFA_GRID = (40.0, 70.0, 100.0)
BURN = 40  # matches ignored when scoring parameter choices


def gmult(gd):
    gd = abs(gd)
    return 1.0 if gd <= 1 else (1.5 if gd == 2 else (11.0 + gd) / 8.0)


def pois_ll(g, lam):
    return g * math.log(lam) - lam - math.lgamma(g + 1)


def walk(matches, K, hfa, c, total=None):
    """Run Elo through history; return (final ratings, walk-forward log-likelihood after burn-in).
    total=None -> the average-goals level used to score match i is the running mean of matches BEFORE i."""
    R, ll, goals = {}, 0.0, 0
    for i, m in enumerate(matches):
        rh, ra = R.get(m["h"], 1500.0), R.get(m["a"], 1500.0)
        dr = rh + hfa - ra
        if i >= BURN:
            tot = total if total is not None else goals / float(i)
            lh, la = tot / 2.0 * math.exp(c * dr), tot / 2.0 * math.exp(-c * dr)
            ll += pois_ll(m["hg"], lh) + pois_ll(m["ag"], la)
        e = 1.0 / (1.0 + 10 ** (-dr / 400.0))
        s = 1.0 if m["hg"] > m["ag"] else (0.5 if m["hg"] == m["ag"] else 0.0)
        delta = K * gmult(m["hg"] - m["ag"]) * (s - e)
        R[m["h"]], R[m["a"]] = rh + delta, ra - delta
        goals += m["hg"] + m["ag"]
    return R, ll


def fit(matches):
    total = sum(m["hg"] + m["ag"] for m in matches) / float(len(matches))
    best = None
    for K in K_GRID:
        for hfa in HFA_GRID:
            for c in C_GRID:
                R, ll = walk(matches, K, hfa, c)   # rolling total: no look-ahead
                if best is None or ll > best[0]:
                    best = (ll, K, hfa, c, R)
    ll, K, hfa, c, R = best
    return {"R": R, "K": K, "hfa": hfa, "c": c, "total": total, "ll": ll}


def main():
    g = w.load_gorev()
    preds, skipped, notes = [], [], []
    hist, models = {}, {}
    for fx in g["fixtures"]:
        fid, lg = fx["fixture_id"], fx["league"]
        if lg not in hist:
            paths = fc.history_paths(g, lg)
            if not paths:
                w.needs_data(["history_%s" % lg])
            rows, bad = fc.read_history(paths)
            if bad:
                notes.append("%s: %d unreadable history rows ignored" % (lg, bad))
            hist[lg] = rows
        cut = fc.kickoff_day(fx)
        key = (lg, cut)
        if key not in models:
            rows_cut = [r for r in hist[lg] if r["day"] < cut]
            models[key] = (fit(rows_cut) if len(rows_cut) >= fc.MIN_MATCHES else None, len(rows_cut))
        model, n = models[key]
        if model is None:
            skipped.append({"fixture_id": fid, "reason": "not enough history before kickoff (%d < %d matches)" % (n, fc.MIN_MATCHES)})
            continue
        h, a = fx.get("home_fd") or fx["home"], fx.get("away_fd") or fx["away"]
        if h not in model["R"] or a not in model["R"]:
            skipped.append({"fixture_id": fid, "reason": "team missing from history: %s / %s" % (h, a)})
            continue
        dr = model["R"][h] + model["hfa"] - model["R"][a]
        lh = model["total"] / 2.0 * math.exp(model["c"] * dr)
        la = model["total"] / 2.0 * math.exp(-model["c"] * dr)
        fc.emit(preds, fid, lh, la, 0.0)
    for (lg, cut), (m, n) in sorted(models.items()):
        if m:
            notes.append("%s cutoff-day %d: n=%d K=%s hfa=%s c=%s total=%.2f" % (lg, cut, n, m["K"], m["hfa"], m["c"], m["total"]))
            edge = [x for x, grid, v in (("K", K_GRID, m["K"]), ("c", C_GRID, m["c"]), ("hfa", HFA_GRID, m["hfa"])) if v in (grid[0], grid[-1])]
            if edge:
                notes.append("%s cutoff-day %d: chosen parameter(s) on the grid EDGE: %s (grid may be too narrow)" % (lg, cut, ",".join(edge)))
    if not preds:  # nothing could be predicted for DATA reasons: report it, but it is not the bot's fault
        w.needs_data(["data: " + r for r in sorted({x["reason"] for x in skipped})[:3]])
    allrows = [r for rows in hist.values() for r in rows]
    status = "ok" if not skipped else "partial"
    w.write_sonuc(preds, status=status, skipped=skipped, notes=notes, bot_version=VERSION, model_family="elo",
                  model_version=fc.model_version(allrows, {"K": K_GRID, "c": C_GRID, "hfa": HFA_GRID, "cutoffs": sorted(c for _, c in models)}, "elo"))


if __name__ == "__main__":
    main()
