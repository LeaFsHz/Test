"""Shared helpers for history-based bots (standard library only).
Reads football-data.co.uk season CSVs (Date dd/mm/yyyy or dd/mm/yy, HomeTeam, AwayTeam, FTHG, FTAG)."""
import csv
import hashlib
from datetime import date

from market_bot import btts_prob, outcome_probs, over_prob, score_matrix  # noqa: F401  (same folder)

MIN_MATCHES = 40


def parse_date(s):
    d, m, y = s.strip().split("/")
    y = int(y)
    if y < 100:
        y += 2000 if y < 50 else 1900
    return date(y, int(m), int(d)).toordinal()


def read_history(paths):
    """Return (matches sorted by date, number of unreadable rows). Duplicates (same day/home/away) are dropped."""
    seen, rows, bad = set(), [], 0
    for p in paths:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    row = {"day": parse_date(r["Date"]), "h": r["HomeTeam"].strip(), "a": r["AwayTeam"].strip(),
                           "hg": int(r["FTHG"]), "ag": int(r["FTAG"])}
                except (KeyError, ValueError, TypeError, AttributeError):
                    bad += 1
                    continue
                key = (row["day"], row["h"], row["a"])
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
    rows.sort(key=lambda x: (x["day"], x["h"], x["a"]))
    return rows, bad


def history_paths(gorev, league):
    pre = "history_%s_" % league
    return [meta["path"] for name, meta in sorted(gorev.get("data", {}).items()) if name.startswith(pre)]


def model_version(rows, params, tag):
    h = hashlib.sha256()
    for r in rows:
        h.update(("%d|%s|%s|%d|%d;" % (r["day"], r["h"], r["a"], r["hg"], r["ag"])).encode())
    h.update(repr(sorted(params.items())).encode())
    return "%s-%s" % (tag, h.hexdigest()[:12])


def emit(preds, fid, lh, la, rho):
    m = score_matrix(lh, la, rho=rho)
    h, d, a = outcome_probs(m)
    over, btts = over_prob(m), btts_prob(m)
    preds += [
        {"fixture_id": fid, "market": "1x2", "selection": "H", "prob": h},
        {"fixture_id": fid, "market": "1x2", "selection": "D", "prob": d},
        {"fixture_id": fid, "market": "1x2", "selection": "A", "prob": max(0.0, 1.0 - h - d)},
        {"fixture_id": fid, "market": "ou25", "selection": "over", "prob": over},
        {"fixture_id": fid, "market": "ou25", "selection": "under", "prob": max(0.0, 1.0 - over)},
        {"fixture_id": fid, "market": "btts", "selection": "yes", "prob": btts},
        {"fixture_id": fid, "market": "btts", "selection": "no", "prob": max(0.0, 1.0 - btts)},
    ]


def kickoff_day(fx):
    """Ordinal day of the fixture's kickoff ('YYYY-MM-DDTHH:MM:SSZ'). Models may only learn from days BEFORE this."""
    k = fx["kickoff_utc"]
    return date(int(k[0:4]), int(k[5:7]), int(k[8:10])).toordinal()
