"""dc_poisson - time-weighted Dixon-Coles style goal model, fitted from results only (pure standard library).

Model:  home goals ~ Poisson(mu0 * gamma * attack[h] * defence[a]),  away goals ~ Poisson(mu0 * attack[a] * defence[h])
Fit:    weighted maximum likelihood by fixed-point iteration (Maher 1982 style) with exponential time decay
        exp(-xi * days_ago) (Dixon & Coles 1997) and mild shrinkage toward the league average (k pseudo-matches);
        the low-score dependence rho is chosen by a 1-D grid on the weighted log tau term.
Method: Maher (1982), Dixon & Coles (1997). Written from the published method; no third-party code was copied.
(Repos consulted as references only, licences not relied upon: jpmouracodex/football-mle, callumfm/dixon-coles.)
No look-ahead: for every fixture the model is fitted ONLY on matches played before the fixture's kickoff day.
"""
import math

import fb_common as fc
import worker_io as w
from market_bot import tau

XI = 0.002      # time decay per day (half-life ~ 1 year)
SHRINK = 2.0    # pseudo-matches pulling every team toward the league average
ITERS = 60
VERSION = "dc-poisson-1"


def fit_dc(matches, ref_day, xi=XI, k=SHRINK, iters=ITERS):
    ms = [m for m in matches if m["day"] < ref_day]
    if not ms:
        raise ValueError("fit_dc: no matches before day %s" % ref_day)
    wts = [math.exp(-xi * (ref_day - m["day"])) for m in ms]
    teams = sorted({m["h"] for m in ms} | {m["a"] for m in ms})
    a = {t: 1.0 for t in teams}
    d = {t: 1.0 for t in teams}
    sw = sum(wts)
    mu0 = sum(wt * m["ag"] for wt, m in zip(wts, ms)) / sw   # mu0 = base rate of AWAY goals; gamma scales it for the home side
    gamma = (sum(wt * m["hg"] for wt, m in zip(wts, ms)) / sw) / mu0
    for _ in range(iters):
        na_n = {t: k * mu0 for t in teams}
        na_d = {t: k * mu0 for t in teams}
        nd_n = {t: k * mu0 for t in teams}
        nd_d = {t: k * mu0 for t in teams}
        for wt, m in zip(wts, ms):
            h, aw = m["h"], m["a"]
            na_n[h] += wt * m["hg"]
            na_d[h] += wt * mu0 * gamma * d[aw]
            na_n[aw] += wt * m["ag"]
            na_d[aw] += wt * mu0 * d[h]
            nd_n[aw] += wt * m["hg"]          # goals conceded by the away team
            nd_d[aw] += wt * mu0 * gamma * a[h]
            nd_n[h] += wt * m["ag"]           # goals conceded by the home team
            nd_d[h] += wt * mu0 * a[aw]
        a = {t: na_n[t] / na_d[t] for t in teams}
        d = {t: nd_n[t] / nd_d[t] for t in teams}
        ma, md = sum(a.values()) / len(a), sum(d.values()) / len(d)
        a = {t: v / ma for t, v in a.items()}
        d = {t: v / md for t, v in d.items()}
        mu0 = sum(wt * m["ag"] for wt, m in zip(wts, ms)) / sum(wt * a[m["a"]] * d[m["h"]] for wt, m in zip(wts, ms))
        gamma = sum(wt * m["hg"] for wt, m in zip(wts, ms)) / sum(wt * mu0 * a[m["h"]] * d[m["a"]] for wt, m in zip(wts, ms))
    # rho: maximise sum w*log(tau) over the low-score cells
    best_rho, best_ll = 0.0, -1e18
    for i in range(-20, 11):
        rho = i / 100.0
        ll = 0.0
        for wt, m in zip(wts, ms):
            if m["hg"] > 1 or m["ag"] > 1:
                continue
            lh = mu0 * gamma * a[m["h"]] * d[m["a"]]
            la = mu0 * a[m["a"]] * d[m["h"]]
            t = tau(m["hg"], m["ag"], lh, la, rho)
            if t <= 1e-9:
                ll = -1e18
                break
            ll += wt * math.log(t)
        if ll > best_ll:
            best_ll, best_rho = ll, rho
    return {"a": a, "d": d, "mu0": mu0, "gamma": gamma, "rho": best_rho, "n": len(ms)}


def rates(model, home, away):
    return (model["mu0"] * model["gamma"] * model["a"][home] * model["d"][away],
            model["mu0"] * model["a"][away] * model["d"][home])


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
            models[key] = (fit_dc(rows_cut, cut) if len(rows_cut) >= fc.MIN_MATCHES else None, len(rows_cut))
        model, n = models[key]
        if model is None:
            skipped.append({"fixture_id": fid, "reason": "not enough history before kickoff (%d < %d matches)" % (n, fc.MIN_MATCHES)})
            continue
        h, a = fx.get("home_fd") or fx["home"], fx.get("away_fd") or fx["away"]
        if h not in model["a"] or a not in model["a"]:
            skipped.append({"fixture_id": fid, "reason": "team missing from history: %s / %s" % (h, a)})
            continue
        lh, la = rates(model, h, a)
        fc.emit(preds, fid, lh, la, model["rho"])
    for (lg, cut), (m, n) in sorted(models.items()):
        if m:
            notes.append("%s cutoff-day %d: n=%d mu0=%.3f gamma=%.3f rho=%.2f xi=%s" % (lg, cut, n, m["mu0"], m["gamma"], m["rho"], XI))
    if not preds:  # nothing could be predicted for DATA reasons: report it, but it is not the bot's fault
        w.needs_data(["data: " + r for r in sorted({x["reason"] for x in skipped})[:3]])
    allrows = [r for rows in hist.values() for r in rows]
    status = "ok" if not skipped else "partial"
    w.write_sonuc(preds, status=status, skipped=skipped, notes=notes, bot_version=VERSION, model_family="poisson",
                  model_version=fc.model_version(allrows, {"xi": XI, "k": SHRINK, "iters": ITERS, "cutoffs": sorted(c for _, c in models)}, "dc"))


if __name__ == "__main__":
    main()
