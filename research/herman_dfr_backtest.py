import json
import urllib.request
from collections import defaultdict

import numpy as np
import pandas as pd

DATA_URL = "https://raw.githubusercontent.com/lvrusu/QQQ_price_data/main/QQQ5m_regular_raw_1_2018_to_9_30_24.csv"
DATA_PATH = "/tmp/qqq5m.csv"
ROUNDTRIP_COST = 0.0004  # 4 bps QQQ proxy friction
BASE_SEP = 0.0015        # proportional proxy for Herman's 30-point separation
BASE_STOP = 0.00625      # proportional proxy for Herman's 125-point stop


def load_data():
    urllib.request.urlretrieve(DATA_URL, DATA_PATH)
    df = pd.read_csv(DATA_PATH)
    cmap = {str(c).strip().lower(): c for c in df.columns}
    tcol = next((cmap[k] for k in ["date_time", "datetime", "timestamp", "time", "ds"] if k in cmap), None)
    if tcol is None:
        raise RuntimeError(f"No timestamp column: {list(df.columns)}")
    for k in ["open", "high", "low", "close"]:
        if k not in cmap:
            raise RuntimeError(f"Missing {k}: {list(df.columns)}")
    x = df[[tcol, cmap["open"], cmap["high"], cmap["low"], cmap["close"]]].copy()
    x.columns = ["ts", "open", "high", "low", "close"]
    x["ts"] = pd.to_datetime(x["ts"], errors="coerce")
    for c in ["open", "high", "low", "close"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.dropna().drop_duplicates("ts").sort_values("ts").set_index("ts")
    x = x.between_time("09:30", "15:59").copy()
    x["sma50"] = x.close.rolling(50, min_periods=50).mean()
    x["sma200"] = x.close.rolling(200, min_periods=200).mean()
    x["prev_close"] = x.close.shift(1)
    x["prev_sma50"] = x.sma50.shift(1)
    return x


def build_session_regimes(x):
    rows = []
    for day, g in x.groupby(x.index.date):
        first = g.between_time("09:30", "10:25")
        if first.empty:
            continue
        first_open = float(first.iloc[0].open)
        fh_range = float((first.high.max() - first.low.min()) / first_open)
        rows.append((pd.Timestamp(day), fh_range))
    s = pd.DataFrame(rows, columns=["day", "first_hour_range"]).set_index("day").sort_index()
    # Past-only regime context: median of the prior 10 completed sessions.
    s["prior10_med"] = s.first_hour_range.shift(1).rolling(10, min_periods=10).median()
    # Two deployable gates. Both become known at 10:30 ET.
    s["trailing_compression"] = s.prior10_med <= 0.008
    s["same_day_compression"] = s.trailing_compression & (s.first_hour_range <= 0.008)
    return s


def metrics(rets):
    r = np.asarray(rets, dtype=float)
    if len(r) == 0:
        return dict(trades=0, win_rate=None, pf=None, return_pct=0.0, max_dd_pct=0.0, expectancy_bp=None)
    wins = r[r > 0].sum()
    losses = r[r < 0].sum()
    pf = float(wins / abs(losses)) if losses < 0 else float("inf")
    eq = np.r_[1.0, np.cumprod(1.0 + r)]
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    return dict(
        trades=int(len(r)),
        win_rate=float((r > 0).mean() * 100.0),
        pf=pf,
        return_pct=float((eq[-1] - 1.0) * 100.0),
        max_dd_pct=float(-dd.min() * 100.0),
        expectancy_bp=float(r.mean() * 10000.0),
    )


def bh_on_days(x, eligible_days):
    # Stitched buy-and-hold benchmark for only the eligible sessions: buy session open, sell session close.
    wealth = 1.0
    count = 0
    for day in sorted(eligible_days):
        g = x[x.index.date == day]
        if g.empty:
            continue
        wealth *= float(g.iloc[-1].close / g.iloc[0].open)
        count += 1
    return float((wealth - 1.0) * 100.0), count


def run_strategy(x, regimes, gate_name, displacement=None, fail_lookback=None, max_wait=None,
                 start=None, end=None, eod_flat=False):
    """Run base Herman when displacement is None; otherwise require D->F->R before the Herman entry."""
    rets = []
    records = []
    pos = 0
    entry = None
    entry_i = -1
    entry_ts = None
    entry_dir = 0
    skip_i = -1

    state_day = None
    disp_long = False
    disp_short = False
    fail_long_i = None
    fail_short_i = None

    idx = x.index
    start_ts = pd.Timestamp(start) if start else idx.min()
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) if end else idx.max() + pd.Timedelta(minutes=5)

    for i in range(len(x)):
        ts = idx[i]
        if ts < start_ts or ts >= end_ts:
            continue
        row = x.iloc[i]
        day = ts.date()

        if state_day != day:
            state_day = day
            disp_long = disp_short = False
            fail_long_i = fail_short_i = None

        day_key = pd.Timestamp(day)
        eligible = day_key in regimes.index and bool(regimes.loc[day_key, gate_name]) and ts.time() >= pd.Timestamp("10:30").time()

        # Manage an existing position regardless of whether the regime gate later changes.
        if pos != 0 and i > entry_i:
            stop = entry * (1.0 - BASE_STOP) if pos == 1 else entry * (1.0 + BASE_STOP)
            target = float(row.sma200) if pd.notna(row.sma200) else np.nan
            exit_px = None
            reason = None
            if pos == 1:
                if row.low <= stop:
                    exit_px, reason = stop, "stop"
                elif pd.notna(target) and row.low <= target <= row.high:
                    exit_px, reason = target, "sma200"
            else:
                if row.high >= stop:
                    exit_px, reason = stop, "stop"
                elif pd.notna(target) and row.low <= target <= row.high:
                    exit_px, reason = target, "sma200"

            next_day = idx[i + 1].date() if i < len(x) - 1 else None
            if eod_flat and exit_px is None and next_day != day:
                exit_px, reason = float(row.close), "eod"

            if exit_px is not None:
                gross = entry_dir * (exit_px - entry) / entry
                net = gross - ROUNDTRIP_COST
                rets.append(net)
                records.append(dict(entry_ts=str(entry_ts), exit_ts=str(ts), direction=entry_dir,
                                    entry=entry, exit=exit_px, gross=gross, ret=net, reason=reason))
                pos = 0
                entry = None
                entry_i = -1
                entry_ts = None
                entry_dir = 0
                skip_i = i

        if pos != 0 or i == skip_i or not eligible:
            continue
        if any(pd.isna(v) for v in [row.sma50, row.sma200, row.prev_close, row.prev_sma50]):
            continue

        # Herman's own separation gate.
        if abs(row.sma50 - row.sma200) / row.close < BASE_SEP:
            continue

        cross_up = row.close > row.sma50 and row.prev_close <= row.prev_sma50
        cross_dn = row.close < row.sma50 and row.prev_close >= row.prev_sma50

        if displacement is not None:
            # Step 1: displacement away from the dynamic 200-SMA balance point.
            dist200 = abs(row.close - row.sma200) / row.sma200
            if row.sma50 < row.sma200 and row.close < row.sma50 and dist200 >= displacement:
                disp_long = True
            if row.sma50 > row.sma200 and row.close > row.sma50 and dist200 >= displacement:
                disp_short = True

            # Step 2: continuation attempt fails. Break a prior L-bar extreme intrabar, close back inside it.
            if fail_lookback is not None and i >= fail_lookback:
                prior = x.iloc[i - fail_lookback:i]
                if disp_long and row.sma50 < row.sma200:
                    prior_low = float(prior.low.min())
                    if row.low < prior_low and row.close > prior_low:
                        fail_long_i = i
                if disp_short and row.sma50 > row.sma200:
                    prior_high = float(prior.high.max())
                    if row.high > prior_high and row.close < prior_high:
                        fail_short_i = i

            # Expire failed-continuation permission if rotation takes too long.
            if fail_long_i is not None and i - fail_long_i > max_wait:
                fail_long_i = None
                disp_long = False
            if fail_short_i is not None and i - fail_short_i > max_wait:
                fail_short_i = None
                disp_short = False

        # Step 3: original Herman rotation cross. Filtered version requires prior D->F state.
        long_ok = cross_up and row.sma50 < row.sma200
        short_ok = cross_dn and row.sma50 > row.sma200
        if displacement is not None:
            long_ok = long_ok and fail_long_i is not None and 0 <= i - fail_long_i <= max_wait
            short_ok = short_ok and fail_short_i is not None and 0 <= i - fail_short_i <= max_wait

        if long_ok:
            pos, entry, entry_i, entry_ts, entry_dir = 1, float(row.close), i, ts, 1
            disp_long = False
            fail_long_i = None
        elif short_ok:
            pos, entry, entry_i, entry_ts, entry_dir = -1, float(row.close), i, ts, -1
            disp_short = False
            fail_short_i = None

    # Close an open trade at the final available close so every run has realized P&L.
    if pos != 0:
        final_i = np.flatnonzero((idx >= start_ts) & (idx < end_ts))[-1]
        final_row = x.iloc[final_i]
        final_ts = idx[final_i]
        gross = entry_dir * (float(final_row.close) - entry) / entry
        net = gross - ROUNDTRIP_COST
        rets.append(net)
        records.append(dict(entry_ts=str(entry_ts), exit_ts=str(final_ts), direction=entry_dir,
                            entry=entry, exit=float(final_row.close), gross=gross, ret=net, reason="period_end"))
    return rets, records


def print_result(tag, **kwargs):
    print(tag, json.dumps(kwargs, sort_keys=True, allow_nan=True))


def main():
    x = load_data()
    regimes = build_session_regimes(x)
    print("DATA", x.index.min(), x.index.max(), len(x), "sessions", len(regimes))
    print("REGIME_COUNTS", json.dumps({
        "trailing_compression": int(regimes.trailing_compression.sum()),
        "same_day_compression": int(regimes.same_day_compression.sum()),
    }))

    # Non-optimized, pre-fixed hypothesis test.
    fixed = dict(displacement=0.004, fail_lookback=5, max_wait=12)
    for gate in ["trailing_compression", "same_day_compression"]:
        eligible_days = set(regimes.index[regimes[gate]].date)
        bh_ret, bh_days = bh_on_days(x, eligible_days)
        base_rets, _ = run_strategy(x, regimes, gate)
        filt_rets, filt_records = run_strategy(x, regimes, gate, **fixed)
        print_result("BASE", gate=gate, **metrics(base_rets), bh_pct=bh_ret, bh_days=bh_days)
        print_result("FIXED_DFR", gate=gate, **fixed, **metrics(filt_rets), bh_pct=bh_ret, bh_days=bh_days)

        # June 2021 exact historical check under the same past-only regime gate.
        june_rets, _ = run_strategy(x, regimes, gate, start="2021-06-01", end="2021-06-30", **fixed)
        june_base, _ = run_strategy(x, regimes, gate, start="2021-06-01", end="2021-06-30")
        print_result("JUNE2021_BASE", gate=gate, **metrics(june_base))
        print_result("JUNE2021_FIXED_DFR", gate=gate, **fixed, **metrics(june_rets))

    # Train/OOS sensitivity sweep. Select on 2018-2021 only; 2022-2024 is untouched OOS.
    gate = "same_day_compression"
    grid = []
    for d in [0.0025, 0.0030, 0.0035, 0.0040, 0.0045, 0.0050, 0.0060]:
        for lb in [3, 5, 8]:
            for wait in [6, 12, 18]:
                train, _ = run_strategy(x, regimes, gate, displacement=d, fail_lookback=lb, max_wait=wait,
                                        start="2018-01-01", end="2021-12-31")
                test, _ = run_strategy(x, regimes, gate, displacement=d, fail_lookback=lb, max_wait=wait,
                                       start="2022-01-01", end="2024-09-27")
                mt, ms = metrics(train), metrics(test)
                grid.append(dict(d=d, lb=lb, wait=wait, train=mt, test=ms))

    # Predeclare minimum train sample; rank only on train PF with a small sample penalty via expectancy tie-break.
    candidates = [g for g in grid if g["train"]["trades"] >= 30 and g["train"]["pf"] is not None]
    candidates.sort(key=lambda g: (g["train"]["pf"], g["train"]["expectancy_bp"]), reverse=True)
    best = candidates[0] if candidates else max(grid, key=lambda g: g["train"]["trades"])
    d, lb, wait = best["d"], best["lb"], best["wait"]
    full, _ = run_strategy(x, regimes, gate, displacement=d, fail_lookback=lb, max_wait=wait)
    print_result("TRAIN_SELECTED", displacement=d, fail_lookback=lb, max_wait=wait,
                 train=best["train"], oos=best["test"], full=metrics(full))

    # Robustness neighborhood: summarize how many grid points clear key PF thresholds OOS.
    valid_oos = [g for g in grid if g["test"]["trades"] >= 15 and g["test"]["pf"] is not None]
    summary = {
        "grid_points": len(grid),
        "valid_oos_points": len(valid_oos),
        "oos_pf_ge_1_0": sum(g["test"]["pf"] >= 1.0 for g in valid_oos),
        "oos_pf_ge_1_25": sum(g["test"]["pf"] >= 1.25 for g in valid_oos),
        "oos_pf_ge_1_5": sum(g["test"]["pf"] >= 1.5 for g in valid_oos),
        "oos_positive_expectancy": sum(g["test"]["expectancy_bp"] > 0 for g in valid_oos),
    }
    print("GRID_SUMMARY", json.dumps(summary, sort_keys=True))

    # Print top 5 train-selected rows with their OOS results to expose instability/robustness.
    for rank, g in enumerate(candidates[:5], 1):
        print_result("TOP_TRAIN", rank=rank, displacement=g["d"], fail_lookback=g["lb"], max_wait=g["wait"],
                     train=g["train"], oos=g["test"])


if __name__ == "__main__":
    main()
