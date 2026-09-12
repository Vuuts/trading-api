import json
import urllib.request
import numpy as np
import pandas as pd

DATA_URL = "https://raw.githubusercontent.com/lvrusu/QQQ_price_data/main/QQQ5m_regular_raw_1_2018_to_9_30_24.csv"
DATA_PATH = "/tmp/qqq5m.csv"
COST = 0.0004
SEP = 0.0015
STOP_PCT = 0.00625


def metrics(rets):
    r = np.asarray(rets, float)
    if not len(r):
        return {"trades": 0, "win_rate": None, "pf": None, "return_pct": 0.0,
                "max_dd_pct": 0.0, "expectancy_bp": None}
    w, l = r[r > 0].sum(), r[r < 0].sum()
    pf = float(w / abs(l)) if l < 0 else float("inf")
    eq = np.r_[1.0, np.cumprod(1.0 + r)]
    peak = np.maximum.accumulate(eq)
    return {"trades": int(len(r)), "win_rate": float((r > 0).mean() * 100), "pf": pf,
            "return_pct": float((eq[-1] - 1) * 100),
            "max_dd_pct": float(-(eq / peak - 1).min() * 100),
            "expectancy_bp": float(r.mean() * 10000)}


def emit(tag, **payload):
    print(tag, json.dumps(payload, sort_keys=True, allow_nan=True))


def load():
    urllib.request.urlretrieve(DATA_URL, DATA_PATH)
    raw = pd.read_csv(DATA_PATH)
    cmap = {str(c).strip().lower(): c for c in raw.columns}
    tcol = next(cmap[k] for k in ["date_time", "datetime", "timestamp", "time", "ds"] if k in cmap)
    x = raw[[tcol, cmap["open"], cmap["high"], cmap["low"], cmap["close"]]].copy()
    x.columns = ["ts", "open", "high", "low", "close"]
    x.ts = pd.to_datetime(x.ts, errors="coerce")
    for c in ["open", "high", "low", "close"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.dropna().drop_duplicates("ts").sort_values("ts").set_index("ts").between_time("09:30", "15:59").copy()
    x["sma50"] = x.close.rolling(50, min_periods=50).mean()
    x["sma200"] = x.close.rolling(200, min_periods=200).mean()
    x["prev_close"] = x.close.shift(1)
    x["prev_sma50"] = x.sma50.shift(1)
    return x


def main():
    x = load()
    n = len(x)
    idx = x.index
    dates = np.array(idx.date)
    tm = np.array(idx.time)
    op, hi, lo, cl = (x[c].to_numpy(float) for c in ["open", "high", "low", "close"])
    s50, s200 = x.sma50.to_numpy(float), x.sma200.to_numpy(float)
    pc, ps50 = x.prev_close.to_numpy(float), x.prev_sma50.to_numpy(float)

    # Session boundaries and deployable compression state.
    unique_days, day_first, day_counts = np.unique(dates, return_index=True, return_counts=True)
    day_last = day_first + day_counts - 1
    day_to_ord = {d: k for k, d in enumerate(unique_days)}
    day_ord = np.array([day_to_ord[d] for d in dates], dtype=int)
    fh = np.full(len(unique_days), np.nan)
    for k, (a, b) in enumerate(zip(day_first, day_last)):
        # 12 five-minute bars: 09:30 through 10:25.
        z = min(a + 11, b)
        fh[k] = (np.max(hi[a:z + 1]) - np.min(lo[a:z + 1])) / op[a]
    prior10 = pd.Series(fh).shift(1).rolling(10, min_periods=10).median().to_numpy()
    trailing_day = prior10 <= 0.008
    sameday_day = trailing_day & (fh <= 0.008)
    trailing_bar = trailing_day[day_ord]
    sameday_bar = sameday_day[day_ord]
    after1030 = np.array([(t.hour > 10) or (t.hour == 10 and t.minute >= 30) for t in tm])

    valid = np.isfinite(s50) & np.isfinite(s200) & np.isfinite(pc) & np.isfinite(ps50)
    sepok = np.abs(s50 - s200) / cl >= SEP
    cross_up = valid & (cl > s50) & (pc <= ps50) & (s50 < s200) & sepok
    cross_dn = valid & (cl < s50) & (pc >= ps50) & (s50 > s200) & sepok
    direction = np.zeros(n, dtype=np.int8)
    direction[cross_up] = 1
    direction[cross_dn] = -1
    signal_idx = np.flatnonzero(direction)

    # Precompute outcome for every raw Herman signal once. Conservative same-bar rule: stop before target.
    outcome_exit = np.full(n, -1, dtype=int)
    outcome_ret = np.full(n, np.nan)
    for i in signal_idx:
        d = int(direction[i]); entry = cl[i]
        stop = entry * (1 - STOP_PCT) if d == 1 else entry * (1 + STOP_PCT)
        exit_i = n - 1; exit_px = cl[-1]
        for j in range(i + 1, n):
            target = s200[j]
            if d == 1:
                if lo[j] <= stop:
                    exit_i, exit_px = j, stop; break
                if np.isfinite(target) and lo[j] <= target <= hi[j]:
                    exit_i, exit_px = j, target; break
            else:
                if hi[j] >= stop:
                    exit_i, exit_px = j, stop; break
                if np.isfinite(target) and lo[j] <= target <= hi[j]:
                    exit_i, exit_px = j, target; break
        outcome_exit[i] = exit_i
        outcome_ret[i] = d * (exit_px - entry) / entry - COST

    # Max qualifying displacement seen so far in the current session, by direction.
    dist200 = np.abs(cl - s200) / s200
    long_disp_val = np.where(valid & (s50 < s200) & (cl < s50), dist200, -np.inf)
    short_disp_val = np.where(valid & (s50 > s200) & (cl > s50), dist200, -np.inf)
    max_long_disp = np.full(n, -np.inf)
    max_short_disp = np.full(n, -np.inf)
    for a, b in zip(day_first, day_last):
        max_long_disp[a:b + 1] = np.maximum.accumulate(long_disp_val[a:b + 1])
        max_short_disp[a:b + 1] = np.maximum.accumulate(short_disp_val[a:b + 1])

    # Failed continuation arrays: take prior L-bar extreme, break it intrabar, close back inside.
    fail_long = {}
    fail_short = {}
    for lb in [3, 5, 8]:
        fl = np.zeros(n, bool); fs = np.zeros(n, bool)
        for a, b in zip(day_first, day_last):
            for j in range(a + lb, b + 1):
                plow = np.min(lo[j - lb:j]); phigh = np.max(hi[j - lb:j])
                fl[j] = (s50[j] < s200[j]) and (lo[j] < plow) and (cl[j] > plow)
                fs[j] = (s50[j] > s200[j]) and (hi[j] > phigh) and (cl[j] < phigh)
        fail_long[lb], fail_short[lb] = fl, fs

    def dfr_mask(disp, lb, wait):
        out = np.zeros(n, bool)
        fl, fs = fail_long[lb], fail_short[lb]
        for i in signal_idx:
            a = day_first[day_ord[i]]
            j0 = max(a, i - wait)
            if direction[i] == 1:
                js = np.flatnonzero(fl[j0:i + 1]) + j0
                if len(js) and np.any(max_long_disp[js] >= disp): out[i] = True
            else:
                js = np.flatnonzero(fs[j0:i + 1]) + j0
                if len(js) and np.any(max_short_disp[js] >= disp): out[i] = True
        return out

    def simulate(allowed, start=None, end=None):
        a = 0 if start is None else int(np.searchsorted(idx.values, np.datetime64(start), side="left"))
        end_dt = idx[-1] + pd.Timedelta(minutes=5) if end is None else pd.Timestamp(end) + pd.Timedelta(days=1)
        b = int(np.searchsorted(idx.values, np.datetime64(end_dt), side="left")) - 1
        if b < a: return []
        rets = []
        last_exit = a - 1
        for i in signal_idx:
            if i < a or i > b or not allowed[i] or i <= last_exit: continue
            ei = outcome_exit[i]
            if ei <= b:
                rets.append(float(outcome_ret[i])); last_exit = ei
            else:
                d = int(direction[i]); rets.append(float(d * (cl[b] - cl[i]) / cl[i] - COST)); last_exit = b
        return rets

    def bh_for_gate(day_gate):
        wealth = 1.0; count = 0
        for k, ok in enumerate(day_gate):
            if not ok: continue
            a, b = day_first[k], day_last[k]
            wealth *= cl[b] / op[a]; count += 1
        return float((wealth - 1) * 100), count

    print("DATA", idx.min(), idx.max(), n, "sessions", len(unique_days), "raw_signals", len(signal_idx))
    print("REGIME_COUNTS", json.dumps({"trailing": int(trailing_day.sum()), "same_day": int(sameday_day.sum())}))

    fixed = (0.004, 5, 12)
    fixed_mask = dfr_mask(*fixed)
    for gate_name, gate_bar, gate_day in [("trailing_compression", trailing_bar, trailing_day),
                                         ("same_day_compression", sameday_bar, sameday_day)]:
        base_allowed = gate_bar & after1030 & (direction != 0)
        filt_allowed = base_allowed & fixed_mask
        bh, bh_days = bh_for_gate(gate_day)
        emit("BASE", gate=gate_name, **metrics(simulate(base_allowed)), bh_pct=bh, bh_days=bh_days)
        emit("FIXED_DFR", gate=gate_name, displacement=fixed[0], fail_lookback=fixed[1], max_wait=fixed[2],
             **metrics(simulate(filt_allowed)), bh_pct=bh, bh_days=bh_days)
        emit("JUNE2021_BASE", gate=gate_name, **metrics(simulate(base_allowed, "2021-06-01", "2021-06-30")))
        emit("JUNE2021_FIXED_DFR", gate=gate_name, displacement=fixed[0], fail_lookback=fixed[1], max_wait=fixed[2],
             **metrics(simulate(filt_allowed, "2021-06-01", "2021-06-30")))

    # Train-only selection, then untouched 2022-2024 OOS evaluation.
    base_gate = sameday_bar & after1030 & (direction != 0)
    grid = []
    for disp in [0.0025, 0.0030, 0.0035, 0.0040, 0.0045, 0.0050, 0.0060]:
        for lb in [3, 5, 8]:
            for wait in [6, 12, 18]:
                allowed = base_gate & dfr_mask(disp, lb, wait)
                train = metrics(simulate(allowed, "2018-01-01", "2021-12-31"))
                oos = metrics(simulate(allowed, "2022-01-01", "2024-09-27"))
                grid.append({"disp": disp, "lb": lb, "wait": wait, "train": train, "oos": oos, "allowed": allowed})

    candidates = [g for g in grid if g["train"]["trades"] >= 30 and g["train"]["pf"] is not None]
    candidates.sort(key=lambda g: (g["train"]["pf"], g["train"]["expectancy_bp"]), reverse=True)
    best = candidates[0] if candidates else max(grid, key=lambda g: g["train"]["trades"])
    full = metrics(simulate(best["allowed"]))
    emit("TRAIN_SELECTED", displacement=best["disp"], fail_lookback=best["lb"], max_wait=best["wait"],
         train=best["train"], oos=best["oos"], full=full)

    valid = [g for g in grid if g["oos"]["trades"] >= 15 and g["oos"]["pf"] is not None]
    print("GRID_SUMMARY", json.dumps({
        "grid_points": len(grid), "valid_oos_points": len(valid),
        "oos_pf_ge_1_0": sum(g["oos"]["pf"] >= 1.0 for g in valid),
        "oos_pf_ge_1_25": sum(g["oos"]["pf"] >= 1.25 for g in valid),
        "oos_pf_ge_1_5": sum(g["oos"]["pf"] >= 1.5 for g in valid),
        "oos_positive_expectancy": sum(g["oos"]["expectancy_bp"] > 0 for g in valid)
    }, sort_keys=True))
    for rank, g in enumerate(candidates[:5], 1):
        emit("TOP_TRAIN", rank=rank, displacement=g["disp"], fail_lookback=g["lb"], max_wait=g["wait"],
             train=g["train"], oos=g["oos"])


if __name__ == "__main__":
    main()
