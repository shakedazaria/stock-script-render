# -*- coding: utf-8 -*-
"""
scanner_backtest.py — point-in-time research harness for stock_scanner_unified.py

What it does
------------
Imports YOUR scanner file (no changes to it), then for every ticker and every
trading day in the window it rebuilds the history *as it was on that day* and:
  1. runs the V9 pattern detectors (Flat Base, Darvas, VCP, Pullback, Retest),
  2. applies the scanner's own geometry sync, safety check and Entry gate
     (evaluate_entry_quality) with point-in-time SPY data (RS, RS-line, regime),
  3. records the EMA28 global-filter verdict as a flag (not as a hard filter),
  4. measures the outcome with the scanner's own evaluate_setup_path
     (next-session open fill, stop/target bracket, 5/10/20 sessions).

Output: one CSV row per detected candidate, plus a printed summary:
  - candidates and ENTRY_READY signals per week,
  - outcome by pattern, by EMA28 flag, by number of blockers,
  - "single blocker" table: what each gate rejected when it was the ONLY blocker.

NOT simulated (needs data that is not point-in-time on Yahoo):
  earnings filter, market-cap filter, Reverse Scanner, Professional gate,
  legacy patterns (Cup&Handle, Triangle, Double Bottom, Falling Wedge).
These are reported as "not simulated", not silently assumed.

v2 (2026-10-29): adds point-in-time distance columns (EMA10/21/28, MA50/150/200,
ATR%, 20-day gain, 52-week high, stop risk, distance above the pivot) and disjoint
universe shards (--offset). Speed: the V9 indicator frame is computed once per day
instead of six times (identical signals, about 2x faster).
v3 (2026-10-30): --paths-out writes the 25 sessions after every ENTRY_READY signal
(open/high/low/close + EMA10/EMA21/ATR14 as of each bar) so exit rules (partial profit,
breakeven stop, trailing stop, time stop) can be tested offline without new runs.
v3.1 (2026-10-30): adds short-term relative-strength columns vs SPY (1M/3M excess return,
RS line vs its EMA21, 10-day RS-line slope), point-in-time weekly context computed from the
daily bars (true 30-week MA and weekly EMA10 including the current partial week, no look-ahead)
and volume in the saved paths (for candle + volume-climax exit tests).

v3.2 (2026-10-31): for scanner v9.7.6. Exports the point-in-time columns of the two layers the v9.7.5 backtest could
not validate: median_dv20 / mean_dv20 / top_day_share20 (20 sessions BEFORE the signal bar, so the $15M median test can
be replayed), liq_ok / liq_gate_ok, and the smart-money columns (sm_acc_days, sm_dist_days, sm_seq, sm_cmf, sm_cmf_delta,
sm_cmf_exp, sm_bull_div, sm_bear_div, sm_bump). They come from the scanner's own gate_feature_frame(), computed ONCE per
ticker (every value at day t uses bars <= t only - checked by the scanner's tests). It also exports the RS rank penalty the
scanner applied (rank_penalty / rank_bonus / risk_flags) and the scanner's own RS-line measure (rs_line_vs_ema_pct).
RS note: with scanner >= v9.7.6 and RS_MODE=swing the gate-facing rs_score is the LEGACY score, the swing score is
rs_swing_score; with v9.7.5 rs_score itself was the swing score - the harness reads whichever exists.

v3.3 (2026-10-31): for scanner v9.7.7. Exports the institutional-footprint columns (fp_prints, fp_cmf_delta, fp_cmf_exp,
fp_accel, fp_score, fp_bonus and fp_prints_wide = prints counted with a 1.5% range cap instead of 0.5%) from the scanner's
gate_feature_frame(), plus close_position and breakout_pct of the signal bar (the scanner's own entry metrics). It prints
footprint tables (how often a print / an acceleration occurs, and outcome by prints) and finally replays the scanner's weekly
learner (run_automated_threshold_learning) on the backtest rows, into a temp folder - your scanner_learned_params.json is
not touched - so the learning rules can be judged on years of data before the live CSV has enough rows.

v3.5 (2026-11-01): for scanner v9.7.8. Exports the alpha-boost measurements (ab_cmf_streak, ab_cmf_ok, ab_compress_ratio,
ab_compress_ref, ab_compress_tight_n, ab_compress_bonus, ab_bonus) from the scanner's gate_feature_frame() - point-in-time, bars <= t
only - and prints an "Alpha Boost" table: outcome by CMF-rise streak, by number of tight bars before the signal bar, by ab_bonus,
and the cross-sectional median of ab_compress_ratio (the value to put into AB_COMPRESS_REF_MEDIAN if a fixed universe reference is
wanted). Nothing else changes; with an older scanner the columns are empty and the table is skipped.

v3.4 (2026-11-01): for scanner v9.7.7 (the scanner file is NOT changed). The v3.3 CSV had no per-bar volume / range series, so
the footprint could only be judged at two fixed points (0.5% and 1.5% range cap, 1.5x volume). v3.4 re-counts the footprint
"print" over a research GRID, entirely inside the harness, from the same OHLCV the scanner sees (every value at day t uses bars
<= t only; the 20-bar volume baseline and the ATR14 exclude the print bar itself):
  print_i = volume_i >= VOLMULT * mean(volume of the 20 bars before i)  AND  close_i >= open_i  AND  RANGE RULE
  RANGE RULE (one of):  abs   : (high-low)/close <= CAP%                     (CAP in percent, the DARKPOOL_PRICE_MAX unit)
                        atr   : (high-low) <= K * ATR14 of the 14 bars before i  (volatility-relative compression cap)
                        any   : no range rule (how many high-volume up-close bars exist at all)
  count = number of prints in the last FP_WINDOW (10) bars, NaN until the whole window has a full baseline.
Columns written to the results CSV (small integers; empty = not known):
  fp_prints_atr_01 / _02 / _03 (+ _04 / _06 / _08)   prints under 0.1x / 0.2x / 0.3x (+ 0.4x / 0.6x / 0.8x) ATR14 at the scanner's
                                                       own volume multiplier (SM_SPIKE_MULT, 1.5). The extra 0.4-0.8x columns are
                                                       there because the median ATR% is 2.79%, so 0.1x-0.3x of ATR is a 0.28%-0.84%
                                                       bar range - about as rare as the 0.5% cap that proved too rare; keep only the
                                                       three with --atr-mults 0.1,0.2,0.3.
  fp_grid_v{12,15,18,22}_{abs05,abs10,abs15,abs20,atr01..atr08,any}   the full matrix: volume multiplier x range rule
                                                       (codes are tenths: v15 = 1.5x, abs05 = 0.5%, atr03 = 0.3x ATR14).
Sanity anchors (tested): fp_grid_v15_abs05 == fp_prints and fp_grid_v15_abs15 == fp_prints_wide of the scanner.
The summary prints one row per grid cell (how often >=1 / >=2 prints occur, outcome of episodes with >=2 prints vs the rest).
The learner replay no longer fails without scikit-learn: if the package is missing or broken it prints a clear WARNING, skips the
tree, and prints pandas-only quintile tables instead - it never raises. (The tree itself still needs scikit-learn.)

Usage (in your own environment, where yfinance is installed):
  python scanner_backtest.py --scanner stock_scanner_unified.py \
      --tickers AAPL,MSFT,NVDA --start 2023-01-01 --end 2026-09-30 --out bt.csv
  or --tickers-file tickers.txt (one symbol per line)
  v3.4 grid options (all optional; defaults shown):  --vol-grid 1.2,1.5,1.8,2.2  --abs-caps 0.5,1.0,1.5,2.0
      --atr-mults 0.1,0.2,0.3,0.4,0.6,0.8   --no-fp-grid  (switches the grid columns off; the output is then identical to v3.3)

Survivorship bias: a list of today's tickers excludes companies that were
delisted. Treat results as relative comparisons between rules, not as an
expected return.
"""
import argparse
import importlib.util
import os
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd


def load_scanner(path):
    os.environ.setdefault("TWELVEDATA_API_KEYS", "BACKTEST_NOT_USED")
    os.environ.setdefault("STATE_DIR", os.path.join(os.path.dirname(os.path.abspath(path)), "backtest_state"))
    os.environ.setdefault("CHARTS_DIR", os.path.join(os.environ["STATE_DIR"], "charts"))
    spec = importlib.util.spec_from_file_location("scanner_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def download(symbol, start, end, yf):
    df = yf.download(symbol, start=start, end=end, interval="1d", progress=False,
                     auto_adjust=False, threads=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    need = ["open", "high", "low", "close", "volume"]
    if not set(need).issubset(df.columns):
        return None
    return df[need].dropna(subset=["open", "high", "low", "close"]).copy()


# ---------------------------------------------------------------- point-in-time patches
class PointInTimeSPY:
    """Replaces the scanner's live SPY lookups with history cut at the signal date."""

    def __init__(self, spy_full):
        self.full = spy_full
        self.cut = spy_full

    def set_date(self, day):
        self.cut = self.full.loc[:day]

    def spy_returns(self):
        close = self.cut["close"]
        now = float(close.iloc[-1])
        out = {}
        for key, n in (("1m", 21), ("3m", 63), ("6m", 126), ("12m", 252)):
            out[key] = (now - float(close.iloc[-n])) / float(close.iloc[-n]) if len(close) >= n else None
        return out

    def regime(self, ma_period=50):
        # Same rules as get_market_regime(): SPY vs MA50, MA50 slope, -3% band.
        close = self.cut["close"]
        if len(close) < ma_period + 2:
            return {"regime": "UNKNOWN", "data_ok": False, "allow_trading": True, "summary": "UNKNOWN"}
        ma = close.rolling(ma_period).mean()
        price, ma50, prev = float(close.iloc[-1]), float(ma.iloc[-1]), float(ma.iloc[-2])
        vs = round((price - ma50) / ma50 * 100.0, 2)
        rising = ma50 > prev
        if price > ma50 and rising:
            name = "BULL"
        elif price > ma50 and not rising:
            name = "NEUTRAL"
        elif price < ma50 and vs > -3:
            name = "NEUTRAL"
        else:
            name = "BEAR"
        return {"regime": name, "data_ok": True, "allow_trading": True, "vs_ma50_pct": vs,
                "summary": f"{name} | SPY {vs:+.1f}% vs MA{ma_period}"}

    def rs_line_new_high(self, df, lookback=63):
        stock = pd.to_numeric(df["close"], errors="coerce").dropna()
        joined = pd.concat([stock.rename("s"), self.cut["close"].rename("p")], axis=1).dropna()
        if len(joined) < max(lookback, 30):
            return None
        line = (joined["s"] / joined["p"]).tail(lookback)
        return bool(float(line.iloc[-1]) >= float(line.max()) * 0.995)


def patch(m, pit, reuse_indicators=True):
    if reuse_indicators and not getattr(m, "_bt_indicator_reuse", False):
        # find_v9_pattern_candidates computes the V9 indicators once and then every detector
        # recomputes them from that same frame. Recomputing an already-computed frame gives the
        # same numbers (verified), so hand back a copy instead: same signals, about 2x faster.
        original = m.add_v9_indicators
        last = {"out": None}
        def add_v9_indicators_reused(df):
            if last["out"] is not None and df is last["out"]:
                return last["out"].copy()
            out = original(df)
            last["out"] = out
            return out
        m.add_v9_indicators = add_v9_indicators_reused
        m._bt_indicator_reuse = True
    m._get_spy_returns = pit.spy_returns
    m._get_spy_close = lambda: pit.cut["close"]       # point-in-time SPY closes for the RS-line check (v9.7.5+)
    m._rs_line_new_high = lambda df, lookback=63: pit.rs_line_new_high(df, lookback)
    m.get_market_regime = lambda: pit.regime(m.REGIME_MA_PERIOD)
    m._regime_cache = None
    m.log = lambda msg: None  # keep the console readable
    if hasattr(m, "GATE_SHADOW_METRICS"):
        m.GATE_SHADOW_METRICS = False   # v9.7.6+: liquidity / smart-money columns come from gate_feature_frame (once per ticker)


# ---------------------------------------------------------------- point-in-time features
def weekly_point_in_time(close):
    """30-week MA / weekly EMA10 / 3-week trend as known on each DAY (current week = partial)."""
    wk = close.resample("W-FRI").last()                      # completed-week closes, labelled by Friday
    label = close.index.to_period("W-FRI").to_timestamp(how="end").normalize()
    wk.index = wk.index.normalize()
    sum29_prev = wk.rolling(29, min_periods=29).sum().shift(1)
    alpha = 2.0 / (10 + 1)
    ema10_prev = wk.ewm(span=10, adjust=False).mean().shift(1)
    close_2w_ago = wk.shift(2)
    m = lambda s: pd.Series(s.reindex(label).to_numpy(), index=close.index)
    ma30w = (m(sum29_prev) + close) / 30.0
    ema10w = alpha * close + (1 - alpha) * m(ema10_prev)
    trend_up = close > m(close_2w_ago)
    return ma30w, ema10w, trend_up


def compute_features(df, spy=None):
    """Indicator series on the full history. Value at day i uses only bars <= i (no look-ahead)."""
    c = pd.to_numeric(df["close"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    lo = pd.to_numeric(df["low"], errors="coerce")
    f = pd.DataFrame(index=df.index)
    for n in (10, 21, 28):
        f[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    for n in (50, 150, 200):
        f[f"ma{n}"] = c.rolling(n, min_periods=n).mean()
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    f["atr14"] = tr.rolling(14, min_periods=14).mean()
    f["hi252"] = h.rolling(252, min_periods=200).max()
    f["close_20ago"] = c.shift(20)
    f["close"] = c
    v = pd.to_numeric(df["volume"], errors="coerce")
    f["vol"] = v
    f["vavg20"] = v.shift(1).rolling(20, min_periods=20).mean()      # prior 20 sessions (signal bar excluded)
    f["ma30w"], f["ema10w"], f["wtrend_up"] = weekly_point_in_time(c)
    if spy is not None and "close" in spy:
        sc = pd.to_numeric(spy["close"], errors="coerce").reindex(df.index)
        line = c / sc
        # Same offsets as compute_rs_score(): close.iloc[-21] / [-63] / [-126] are 20 / 62 / 125 bars back.
        f["exc_1m"] = ((c / c.shift(20) - 1) - (sc / sc.shift(20) - 1)) * 100.0
        f["exc_3m"] = ((c / c.shift(62) - 1) - (sc / sc.shift(62) - 1)) * 100.0
        f["exc_6m"] = ((c / c.shift(125) - 1) - (sc / sc.shift(125) - 1)) * 100.0
        f["rs_decay"] = f["exc_1m"] - f["exc_3m"] / 3.0     # last month vs the monthly pace of the last quarter
        f["rsl_vs_ema21"] = (line / line.ewm(span=21, adjust=False).mean() - 1) * 100.0
        f["rsl_slope10"] = (line / line.shift(10) - 1) * 100.0
    return f


PATH_BARS = 25

GATE_COLUMNS = ("median_dv20", "mean_dv20", "top_day_share20", "liq_ok", "liq_gate_ok", "sm_acc_days", "sm_dist_days",
                "sm_seq", "sm_cmf", "sm_cmf_delta", "sm_cmf_exp", "sm_bull_div", "sm_bear_div", "sm_bump",
                "fp_prints", "fp_cmf_delta", "fp_cmf_exp", "fp_accel", "fp_score", "fp_bonus", "fp_prints_wide",
                # v3.5: scanner >= v9.7.8 alpha-boost measurements (None for an older scanner)
                "ab_cmf_streak", "ab_cmf_ok", "ab_compress_ratio", "ab_compress_ref", "ab_compress_tight_n", "ab_compress_bonus",
                "ab_bonus")


def gate_features(m, df):
    """v3.2: one gate_feature_frame() per ticker (scanner >= v9.7.6); None when the scanner does not have it."""
    fn = getattr(m, "gate_feature_frame", None)
    if fn is None:
        return None
    try:
        return fn(df)
    except Exception as exc:                      # a research column must never stop the run
        print(f"gate_feature_frame failed ({type(exc).__name__}) - gate columns skipped for this ticker")
        return None


def metric_row(quality):
    """v3.3: the two entry metrics the learner uses that are not in the gate frame (the scanner computes them per candidate)."""
    mt = (quality or {}).get("metrics") or {}
    out = {}
    for k in ("close_position", "breakout_pct"):
        v = mt.get(k)
        out[k] = round(float(v), 6) if v is not None and np.isfinite(float(v)) else None
    return out


def gate_row(gates, day):
    """Gate columns at the signal date as plain floats (None when missing)."""
    out = {c: None for c in GATE_COLUMNS}
    if gates is None or day not in gates.index:
        return out
    r = gates.loc[day]
    for c in GATE_COLUMNS:
        if c in r.index and pd.notna(r[c]):
            out[c] = round(float(r[c]), 6)
    return out


# ---------------------------------------------------------------- v3.4: footprint grid (volume multiplier x range rule)
HARNESS_VERSION = "v3.5"
FP_GRID_VOL_MULTS = (1.2, 1.5, 1.8, 2.2)
FP_GRID_ABS_CAPS_PCT = (0.5, 1.0, 1.5, 2.0)
FP_GRID_ATR_MULTS = (0.1, 0.2, 0.3, 0.4, 0.6, 0.8)
FP_GRID_ATR_LEN = 14
FP_GRID_MIN_PRINTS = 2          # same meaning as the scanner's FP_MIN_PRINTS: ">=2 prints in the window"
FP_GRID_MAX_VALUES = 12         # per dimension; keeps the CSV and the summary table readable
DEFAULT_FP_GRID = {"enabled": True, "vol_mults": FP_GRID_VOL_MULTS, "abs_caps": FP_GRID_ABS_CAPS_PCT,
                   "atr_mults": FP_GRID_ATR_MULTS}


def parse_float_list(text, name):
    """'1.2, 1.5' -> (1.2, 1.5). Positive finite numbers only, duplicates dropped, order kept. '' -> () (dimension switched off).
    Raises ValueError with a readable message otherwise."""
    out = []
    for part in str(text if text is not None else "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = float(part)
        except ValueError:
            raise ValueError(f"{name}: '{part}' is not a number")
        if not np.isfinite(v) or v <= 0:
            raise ValueError(f"{name}: {part} must be a positive finite number")
        if v not in out:
            out.append(v)
    if len(out) > FP_GRID_MAX_VALUES:
        raise ValueError(f"{name}: at most {FP_GRID_MAX_VALUES} values (got {len(out)})")
    return tuple(out)


def _code(x):
    """Column code of a grid value: tenths when it is a multiple of 0.1 (1.5 -> '15', 0.3 -> '03', 2.0 -> '20'), else 0p-style."""
    t = float(x) * 10.0
    if abs(t - round(t)) < 1e-9 and round(t) >= 1:
        return f"{int(round(t)):02d}"
    return f"{float(x):g}".replace(".", "p")


def fp_grid_specs(cfg, default_vol):
    """Ordered (column, volume multiplier, kind, value) for every grid column; kind in abs | atr | any."""
    specs = []
    for k in cfg.get("atr_mults", ()):
        specs.append((f"fp_prints_atr_{_code(k)}", float(default_vol), "atr", float(k)))
    for vm in cfg.get("vol_mults", ()):
        for cap in cfg.get("abs_caps", ()):
            specs.append((f"fp_grid_v{_code(vm)}_abs{_code(cap)}", float(vm), "abs", float(cap)))
        for k in cfg.get("atr_mults", ()):
            specs.append((f"fp_grid_v{_code(vm)}_atr{_code(k)}", float(vm), "atr", float(k)))
        specs.append((f"fp_grid_v{_code(vm)}_any", float(vm), "any", None))
    names = [sp[0] for sp in specs]
    if len(set(names)) != len(names):
        raise ValueError("footprint grid: two settings map to the same column name - use values that differ by at least 0.1")
    return specs


def footprint_grid_frame(df, cfg=None, default_vol=1.5, window=10, lookback=20, atr_len=FP_GRID_ATR_LEN):
    """Vectorised, causal print counts for every grid cell. Same index as `df`; float columns (NaN = not known).

    Definition (same as the scanner's footprint, see the module docstring): a bar is a print when it is a valid bar (finite OHLCV,
    close > 0, high > low), volume >= vol_mult * mean(volume of the `lookback` bars BEFORE it), it closes at/above its open, and
    it passes the range rule. The count is the rolling sum over `window` bars; NaN until the whole window has a full baseline
    (for the ATR cells also a full ATR14 of prior bars)."""
    cfg = DEFAULT_FP_GRID if cfg is None else cfg
    specs = fp_grid_specs(cfg, default_vol)
    if len(df) == 0 or not specs:
        return pd.DataFrame(index=df.index, columns=[sp[0] for sp in specs], dtype=float)
    window, lookback = max(2, int(window)), max(2, int(lookback))

    def num(key):
        return pd.to_numeric(df[key], errors="coerce").replace([np.inf, -np.inf], np.nan)

    o, h, lo, c, v = (num(k) for k in ("open", "high", "low", "close", "volume"))
    base = v.shift(1).rolling(lookback, min_periods=lookback).mean()           # prior-bars volume baseline (bar excluded)
    base_ok = base.notna() & (base > 0)
    bar_ok = o.notna() & h.notna() & lo.notna() & c.notna() & v.notna() & (c > 0) & (v >= 0) & (h > lo)
    rng = h - lo
    rng_frac = rng / c.where(c > 0)
    tr = pd.concat([rng, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    atr_prev = tr.rolling(atr_len, min_periods=atr_len).mean().shift(1)         # ATR14 of the bars BEFORE the print bar
    atr_ok = atr_prev.notna() & (atr_prev > 0)
    full_vol = base_ok.astype(float).rolling(window, min_periods=window).min().eq(1.0)
    full_atr = (base_ok & atr_ok).astype(float).rolling(window, min_periods=window).min().eq(1.0)
    up = c >= o
    spikes = {}

    def spike(vm):
        if vm not in spikes:
            spikes[vm] = bar_ok & base_ok & up & (v >= vm * base)
        return spikes[vm]

    def count(flag, full):
        return flag.astype(float).rolling(window, min_periods=window).sum().where(full)

    out = {}
    for name, vm, kind, val in specs:
        sp = spike(vm)
        if kind == "abs":
            out[name] = count(sp & (rng_frac <= val / 100.0), full_vol)
        elif kind == "atr":
            out[name] = count(sp & atr_ok & (rng <= val * atr_prev), full_atr)
        else:
            out[name] = count(sp, full_vol)
    return pd.DataFrame(out, index=df.index)


class FootprintGrid:
    """Per-ticker grid with O(1) lookup of one signal day as plain ints (None = not known)."""

    def __init__(self, frame):
        self.columns = list(frame.columns)
        self.values = frame.to_numpy(dtype=float)
        self.pos = {ts: i for i, ts in enumerate(frame.index)}

    def row(self, day):
        i = self.pos.get(day)
        if i is None:
            return {c: None for c in self.columns}
        vals = self.values[i]
        return {c: (int(round(x)) if np.isfinite(x) else None) for c, x in zip(self.columns, vals)}


def footprint_grid(m, df, cfg=None):
    """One grid per ticker (None when switched off or when anything goes wrong - a research column must never stop the run)."""
    cfg = DEFAULT_FP_GRID if cfg is None else cfg
    if not cfg.get("enabled", True):
        return None
    try:
        return FootprintGrid(footprint_grid_frame(df, cfg, default_vol=float(getattr(m, "SM_SPIKE_MULT", 1.5)),
                                                  window=int(getattr(m, "FP_WINDOW", 10)),
                                                  lookback=int(getattr(m, "VOLUME_AVG_LOOKBACK", 20))))
    except Exception as exc:
        print(f"footprint grid failed ({type(exc).__name__}: {exc}) - grid columns skipped for this ticker")
        return None


def finalize_grid_columns(res):
    """Grid counts are small integers: write them as nullable Int8 ('0', '1', ... or empty) instead of '0.0'. In place."""
    for col in res.columns:
        if col.startswith("fp_grid_") or col.startswith("fp_prints_atr_"):
            res[col] = pd.to_numeric(res[col], errors="coerce").round().astype("Int8")
    return res


def path_rows(df, feats, i, symbol, day, pattern):
    """Forward bars i+1..i+PATH_BARS; indicator values at bar k use data up to bar k only."""
    out = []
    end = min(len(df), i + 1 + PATH_BARS)
    for k in range(i + 1, end):
        bar = df.iloc[k]
        out.append({"ticker": symbol, "signal_date": day.date().isoformat(), "pattern": pattern, "k": k - i,
                    "date": df.index[k].date().isoformat(),
                    "o": round(float(bar["open"]), 4), "h": round(float(bar["high"]), 4),
                    "l": round(float(bar["low"]), 4), "c": round(float(bar["close"]), 4),
                    "ema10": round(float(feats["ema10"].iloc[k]), 4), "ema21": round(float(feats["ema21"].iloc[k]), 4),
                    "v": float(feats["vol"].iloc[k]) if pd.notna(feats["vol"].iloc[k]) else None,
                    "vavg20": round(float(feats["vavg20"].iloc[k]), 1) if pd.notna(feats["vavg20"].iloc[k]) else None,
                    "atr14": round(float(feats["atr14"].iloc[k]), 4) if pd.notna(feats["atr14"].iloc[k]) else None})
    return out


def feature_row(f, day, alert):
    def pct(a, b):
        try:
            a, b = float(a), float(b)
            return round((a / b - 1.0) * 100.0, 3) if b > 0 and np.isfinite(a) and np.isfinite(b) else None
        except (TypeError, ValueError):
            return None
    r = f.loc[day]
    close = r["close"]
    out = {f"dist_{k}_pct": pct(close, r[k]) for k in ("ema10", "ema21", "ema28", "ma50", "ma150", "ma200")}
    atr = r["atr14"]
    out["atr_pct"] = round(float(atr) / float(close) * 100.0, 3) if pd.notna(atr) and close > 0 else None
    out["ma50_dist_in_atr"] = round((float(close) - float(r["ma50"])) / float(atr), 2) if pd.notna(atr) and atr > 0 and pd.notna(r["ma50"]) else None
    out["gain_20d_pct"] = pct(close, r["close_20ago"])
    out["from_52w_high_pct"] = pct(close, r["hi252"])
    entry, stop = alert.get("planned_entry"), alert.get("stop_loss")
    try:
        out["risk_pct"] = round((float(entry) - float(stop)) / float(entry) * 100.0, 3) if entry and stop else None
    except (TypeError, ValueError):
        out["risk_pct"] = None
    pivot = (alert.get("meta") or {}).get("pattern_pivot")
    out["above_pivot_pct"] = pct(close, pivot) if pivot else None
    out["dist_ma30w_pct"] = pct(close, r["ma30w"])
    out["dist_ema10w_pct"] = pct(close, r["ema10w"])
    out["weekly_trend_up"] = bool(r["wtrend_up"]) if pd.notna(r["ma30w"]) else None
    out["signal_vol_ratio"] = round(float(r["vol"]) / float(r["vavg20"]), 3) if pd.notna(r["vavg20"]) and r["vavg20"] > 0 else None
    for k in ("exc_1m", "exc_3m", "exc_6m", "rs_decay", "rsl_vs_ema21", "rsl_slope10"):
        out[k] = round(float(r[k]), 3) if k in r.index and pd.notna(r[k]) else None
    return out


# ---------------------------------------------------------------- core loop
def scan_history(m, symbol, df, spy, pit, start, end, horizons=(5, 10, 20), min_bars=260, window_bars=320, paths=None,
                 grid_cfg=None):
    cfg = m._make_v9_pattern_config()
    rows = []
    dates = df.index
    feats = compute_features(df, spy)
    gates = gate_features(m, df)
    grid = footprint_grid(m, df, grid_cfg)                  # v3.4: volume x range-rule matrix, once per ticker
    for i in range(min_bars, len(df) - 1):
        day = dates[i]
        if day < pd.Timestamp(start) or day > pd.Timestamp(end):
            continue
        first = max(0, i + 1 - window_bars) if window_bars and window_bars > 0 else 0
        hist = df.iloc[first: i + 1].copy()
        # cheap pre-screen: every V9 family needs price within ~15% of the 60-bar high
        hi60 = float(hist["high"].iloc[-61:-1].max())
        if float(hist["close"].iloc[-1]) < 0.85 * hi60:
            continue
        pit.set_date(day)
        regime = pit.regime(m.REGIME_MA_PERIOD)
        rs = m.compute_rs_score(symbol, hist).get("rs_score")
        rs_legacy = rs_swing = rs_decay_pts = rs_line_pct = None
        if hasattr(m, "RS_MODE"):                         # v9.7.5+: score the same bar both ways (gate keeps running mode)
            keep_mode = m.RS_MODE
            try:
                m.RS_MODE = "legacy"
                rs_legacy = m.compute_rs_score(symbol, hist).get("rs_score")
                m.RS_MODE = "swing"
                sw = m.compute_rs_score(symbol, hist)
                # v9.7.6: in "swing" the gate score stays legacy and the swing score is rs_swing_score; v9.7.5: rs_score was the swing score
                rs_swing = sw.get("rs_swing_score", sw.get("rs_score"))
                rs_decay_pts, rs_line_pct = sw.get("rs_decay_1m_vs_3m"), sw.get("rs_line_vs_ema_pct")
            finally:
                m.RS_MODE = keep_mode
        cands = m.find_v9_pattern_candidates(symbol, hist, rs_score=rs, market_regime=regime["regime"],
                                             config=cfg, include_rejected=False)
        if not cands:
            continue
        lower = hist.copy()
        m.ensure_ma_columns(lower)
        ema_ok, ema_reason = m._ema28_filter_ok(lower)
        future = df.iloc[i + 1: i + 1 + max(horizons) + 2]
        for c in cands:
            alert = m._v9_pattern_candidate_to_alert(c)
            m._sync_trade_geometry(alert, hist)
            ok, problems, _ = m._validate_long_trade_levels(alert)
            if not ok:
                quality = {"entry_ready": False, "quality_score": 0.0, "blocking_fails": ["SAFETY: " + "; ".join(problems)]}
            else:
                quality = m.evaluate_entry_quality(df=lower.copy(), ticker=symbol,
                                                   breakout_level=alert.get("breakout_level", 0),
                                                   pattern_meta=alert.get("meta", {}),
                                                   base_score=float(alert.get("score", 0) or 0),
                                                   rr=alert.get("rr_ratio"), regime=regime)
            obs = {"signal_date": day.date().isoformat(),
                   "eligible_date": (day + timedelta(days=1)).date().isoformat(),
                   "planned_entry": alert.get("planned_entry"), "stop": alert.get("stop_loss"),
                   "target": alert.get("target"), "slippage_bps": m.RESEARCH_SLIPPAGE_BPS}
            outcome = m.evaluate_setup_path(future, obs, horizons=horizons) if len(future) else {}
            blockers = quality.get("blocking_fails", []) or []
            row = {"ticker": symbol, "signal_date": obs["signal_date"], "pattern": c.pattern_name,
                   "v9_status": c.status, "entry_ready": bool(quality.get("entry_ready")),
                   "entry_quality": quality.get("quality_score"), "n_blockers": len(blockers),
                   "blockers": " | ".join(blockers), "ema28_ok": ema_ok, "ema28_reason": ema_reason,
                   "regime": regime["regime"], "rs_score": rs, "planned_entry": obs["planned_entry"],
                   "stop": obs["stop"], "target": obs["target"], "rr": alert.get("rr_ratio")}
            row.update(feature_row(feats, day, alert))
            row.update({"rs_legacy": rs_legacy, "rs_swing": rs_swing, "rs_swing_decay_pts": rs_decay_pts,
                        "rs_line_vs_ema_pct": rs_line_pct})
            row.update(gate_row(gates, day))
            if grid is not None:
                row.update(grid.row(day))
            row.update(metric_row(quality))
            risk_flags = quality.get("risk_flags") or []
            row.update({"rank_penalty": quality.get("rank_penalty"), "rank_bonus": quality.get("rank_bonus"),
                        "risk_flags": " | ".join(risk_flags) if isinstance(risk_flags, (list, tuple)) else None})
            row.update({k: v for k, v in outcome.items() if not isinstance(v, (list, dict))})
            rows.append(row)
            if paths is not None and row["entry_ready"]:
                paths.extend(path_rows(df, feats, i, symbol, day, c.pattern_name))
    return rows


def summarize(res):
    if res.empty:
        print("No candidates.")
        return
    res = res.copy()
    res["week"] = pd.to_datetime(res["signal_date"]).dt.to_period("W").astype(str)
    # one row per setup episode: first day a ticker+pattern appears within 10 sessions
    res = res.sort_values(["ticker", "pattern", "signal_date"])
    first = res.groupby(["ticker", "pattern"])["signal_date"].transform(
        lambda s: pd.to_datetime(s).diff().dt.days.fillna(999) > 14)
    ep = res[first].copy()
    r20 = "bracket_net_r_20d"
    weeks = ep["week"].nunique()
    print(f"\nEpisodes: {len(ep)} | weeks covered: {weeks}")
    print(f"ENTRY_READY episodes per week: {ep['entry_ready'].sum() / max(weeks, 1):.2f}")
    print(f"ENTRY_READY & EMA28-ok per week (what the live scanner could send, before earnings/PRO): "
          f"{(ep['entry_ready'] & ep['ema28_ok']).sum() / max(weeks, 1):.2f}")
    if r20 in ep:
        ep[r20] = pd.to_numeric(ep[r20], errors="coerce")
        def table(group_col):
            g = ep.dropna(subset=[r20]).groupby(group_col)[r20]
            return pd.DataFrame({"n": g.size(), "mean_R_20d": g.mean().round(3),
                                 "win_%": (g.apply(lambda s: (s > 0).mean()) * 100).round(1)})
        print("\nBy pattern:\n", table("pattern").to_string())
        print("\nBy entry_ready:\n", table("entry_ready").to_string())
        print("\nBy EMA28 filter verdict:\n", table("ema28_ok").to_string())
        ep["blockers_bucket"] = pd.cut(ep["n_blockers"], [-1, 0, 1, 2, 4, 99], labels=["0", "1", "2", "3-4", "5+"])
        print("\nBy number of entry blockers:\n", table("blockers_bucket").to_string())
        gate_tables(ep, r20)
        footprint_tables(ep, r20)
        footprint_grid_tables(ep, r20)
        alpha_boost_tables(ep, r20)
        single = ep[ep["n_blockers"] == 1].copy()
        if not single.empty:
            single["only_blocker"] = single["blockers"].str.replace(r"\s*\(.*?\)", "", regex=True)
            print("\nWhen a gate was the ONLY blocker:\n", table_single(single, r20).to_string())


def gate_tables(ep, r20):
    """v3.2: outcome by the new columns. Printed only for columns that exist; never raises."""
    try:
        d = ep.dropna(subset=[r20]).copy()
        if d.empty:
            return
        def tab(col):
            g = d.dropna(subset=[col]).groupby(col, observed=True)[r20]
            return pd.DataFrame({"n": g.size(), "mean_R_20d": g.mean().round(3),
                                 "win_%": (g.apply(lambda s: (s > 0).mean()) * 100).round(1)})
        if "median_dv20" in d and d["median_dv20"].notna().any():
            d["median_dv20_bucket"] = pd.cut(d["median_dv20"], [0, 5e6, 15e6, 50e6, 200e6, float("inf")],
                                             labels=["<5M", "5-15M", "15-50M", "50-200M", ">200M"])
            print("\nBy median dollar volume (20 sessions before the signal):\n", tab("median_dv20_bucket").to_string())
            print("\nBy liquidity gate (median>=15M, price>=5):\n", tab("liq_gate_ok").to_string())
        if "sm_bump" in d and d["sm_bump"].notna().any():
            d["sm_bump_bucket"] = pd.cut(d["sm_bump"], [-99, -0.01, 0.01, 4.99, 99], labels=["<0", "0", "1-4", ">=5"])
            print("\nBy smart-money bump:\n", tab("sm_bump_bucket").to_string())
        if "rank_penalty" in d and d["rank_penalty"].notna().any():
            d["rank_penalty_on"] = d["rank_penalty"].astype(float) > 0
            print("\nBy RS rank penalty applied:\n", tab("rank_penalty_on").to_string())
    except Exception as exc:
        print(f"gate tables skipped ({type(exc).__name__}: {exc})")


def footprint_tables(ep, r20):
    """v3.3: how often the footprint fires and what happened afterwards. Reports win-rate, mean R AND mean 20d return,
    because R is confounded by stop width (a tight stop inflates R without a better trade). Never raises."""
    try:
        d = ep.copy()
        if "fp_prints" not in d or not d["fp_prints"].notna().any():
            return
        for c in ("fp_prints", "fp_accel", "fp_score", "fp_prints_wide", "horizon_return_pct_20d"):
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        known = d["fp_prints"].notna()
        print(f"\nFootprint frequency on {int(known.sum())} episodes with a full window: "
              f">=1 print {(d.loc[known, 'fp_prints'] >= 1).mean():.1%} | >=2 prints {(d.loc[known, 'fp_prints'] >= 2).mean():.1%} | "
              f"acceleration (>=2 prints + CMF expansion) {d.loc[known, 'fp_accel'].mean():.1%}")
        if "fp_prints_wide" in d and d["fp_prints_wide"].notna().any():
            kw = d["fp_prints_wide"].notna()
            print(f"  with the looser 1.5% range cap: >=1 print {(d.loc[kw, 'fp_prints_wide'] >= 1).mean():.1%} | >=2 prints {(d.loc[kw, 'fp_prints_wide'] >= 2).mean():.1%}")
        dd = d.dropna(subset=[r20, "fp_prints"]).copy()
        if dd.empty:
            return
        dd[r20] = pd.to_numeric(dd[r20], errors="coerce")
        dd["prints_bucket"] = pd.cut(dd["fp_prints"], [-1, 0, 1, 2, 99], labels=["0", "1", "2", "3+"])
        def tab(col):
            g = dd.groupby(col, observed=True)
            return pd.DataFrame({"n": g.size(), "win_%": (g[r20].apply(lambda s: (s > 0).mean()) * 100).round(1),
                                 "mean_R_20d": g[r20].mean().round(3), "ret_20d_%": g["horizon_return_pct_20d"].mean().round(2),
                                 f"R>={2.5:g}_%": (g[r20].apply(lambda s: (s >= 2.5).mean()) * 100).round(1)})
        print("\nBy footprint prints in the last 10 sessions:\n", tab("prints_bucket").to_string())
        if dd["fp_accel"].notna().any():
            print("\nBy smart-money acceleration (prints>=2 and CMF expansion):\n", tab("fp_accel").to_string())
        if dd["fp_score"].notna().any():
            dd["fp_score_bucket"] = pd.cut(dd["fp_score"], [-1, 0.001, 25, 50, 75, 100], labels=["0", "0-25", "25-50", "50-75", "75-100"])
            print("\nBy footprint score:\n", tab("fp_score_bucket").to_string())
    except Exception as exc:
        print(f"footprint tables skipped ({type(exc).__name__}: {exc})")


def _decode_code(code):
    return float(code.replace("p", ".")) if "p" in code else int(code) / 10.0


def _grid_label(name):
    """fp_grid_v15_abs05 -> (1.5, 'cap 0.5%'); fp_grid_v22_atr03 -> (2.2, '0.3x ATR14'); fp_grid_v12_any -> (1.2, 'no range cap')."""
    import re
    mt = re.match(r"^fp_grid_v([0-9p]+)_(abs|atr)([0-9p]+)$", name)
    if mt:
        val = _decode_code(mt.group(3))
        return _decode_code(mt.group(1)), (f"cap {val:g}%" if mt.group(2) == "abs" else f"{val:g}x ATR14")
    mt = re.match(r"^fp_grid_v([0-9p]+)_any$", name)
    if mt:
        return _decode_code(mt.group(1)), "no range cap"
    return float("nan"), name


def alpha_boost_tables(ep, r20):
    """v3.5: outcome (win %, mean Rc = R clipped to [-3, 5], mean 20d return) by the v9.7.8 alpha-boost measurements, one row per
    bucket, plus the cross-sectional median of ab_compress_ratio. Printed only when the columns hold data; never raises."""
    try:
        if "ab_bonus" not in ep or pd.to_numeric(ep["ab_bonus"], errors="coerce").notna().sum() == 0:
            return
        d = ep.copy()
        d[r20] = pd.to_numeric(d[r20], errors="coerce")
        d = d.dropna(subset=[r20])
        d["_rc"] = d[r20].clip(-3.0, 5.0)
        d["_ret"] = pd.to_numeric(d["horizon_return_pct_20d"], errors="coerce") if "horizon_return_pct_20d" in d else np.nan
        d["_win"] = (d[r20] > 0).astype(float)

        def tab(label, key):
            g = d.assign(_k=key).dropna(subset=["_k"]).groupby("_k", observed=True)
            t = pd.DataFrame({"n": g.size(), "win_%": (100 * g["_win"].mean()).round(2), "mean_Rc": g["_rc"].mean().round(3),
                              "ret20_%": g["_ret"].mean().round(3)})
            print(f"\n  {label}:\n" + t.to_string())

        print("\nAlpha Boost (scanner >= v9.7.8; episodes, point-in-time):")
        num = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d else pd.Series(np.nan, index=d.index)
        tab("by CMF rising-streak (sessions)", pd.cut(num("ab_cmf_streak"), [-1, 0, 2, 4, 6, 999], labels=["0", "1-2", "3-4", "5-6", "7+"]))
        tab("by tight bars among the 5 before the signal bar", num("ab_compress_tight_n"))
        tab("by ab_bonus (rank points)", pd.cut(num("ab_bonus"), [-1, 0, 2, 4, 6, 8, 10], labels=["0", "0-2", "2-4", "4-6", "6-8", "8-10"]))
        med = pd.to_numeric(ep["ab_compress_ratio"], errors="coerce").median() if "ab_compress_ratio" in ep else np.nan
        if np.isfinite(med):
            print(f"\n  median ab_compress_ratio over all episodes (range / prior ATR14) = {med:.4f}")
    except Exception as exc:
        print(f"Alpha Boost table skipped ({type(exc).__name__})")


def footprint_grid_tables(ep, r20):
    """v3.4: one row per grid cell (volume multiplier x range rule): how often >=1 / >=2 prints occur in the last 10 sessions and
    what the episodes with >=2 prints did compared with all other episodes of the same cell. Mean Rc = R clipped to [-3, 5].
    Cells with only a handful of >=2 episodes cannot be judged - n is printed for exactly that reason. Never raises."""
    try:
        cells = [c for c in ep.columns if c.startswith("fp_grid_")]
        if not cells:
            return
        d = ep.copy()
        d[r20] = pd.to_numeric(d[r20], errors="coerce")
        d["_rc"] = d[r20].clip(-3.0, 5.0)
        d["_ret"] = pd.to_numeric(d["horizon_return_pct_20d"], errors="coerce") if "horizon_return_pct_20d" in d else np.nan
        has_cmf = "fp_cmf_exp" in d
        if has_cmf:
            d["_cmf"] = pd.to_numeric(d["fp_cmf_exp"], errors="coerce")
        need = FP_GRID_MIN_PRINTS
        rows = []
        for col in cells:
            x = pd.to_numeric(d[col], errors="coerce").astype("float64")
            known = x.notna()
            if not known.any():
                continue
            vol, rule = _grid_label(col)
            hit = known & (x >= need)
            sub = d[hit & d[r20].notna()]
            rest = d[known & ~hit & d[r20].notna()]
            row = {"cell": col.replace("fp_grid_", ""), "vol_x": vol, "range_rule": rule, "known": int(known.sum()),
                   "n>=1": int((known & (x >= 1)).sum()), "%>=1": round(100.0 * float((x[known] >= 1).mean()), 2),
                   f"n>={need}": int(hit.sum()), f"%>={need}": round(100.0 * float((x[known] >= need).mean()), 2),
                   "win_%": round(100.0 * float((sub[r20] > 0).mean()), 1) if len(sub) else np.nan,
                   "win_%_rest": round(100.0 * float((rest[r20] > 0).mean()), 1) if len(rest) else np.nan,
                   "Rc": round(float(sub["_rc"].mean()), 3) if len(sub) else np.nan,
                   "Rc_rest": round(float(rest["_rc"].mean()), 3) if len(rest) else np.nan,
                   "ret_20d_%": round(float(sub["_ret"].mean()), 2) if len(sub) else np.nan}
            if has_cmf:
                acc = sub[sub["_cmf"] == 1.0]
                row.update({"n_accel": int(len(acc)),
                            "win_%_accel": round(100.0 * float((acc[r20] > 0).mean()), 1) if len(acc) else np.nan,
                            "Rc_accel": round(float(acc["_rc"].mean()), 3) if len(acc) else np.nan})
            rows.append(row)
        if not rows:
            return
        table = pd.DataFrame(rows).set_index("cell")
        print(f"\nFootprint grid (episodes; a cell = volume multiplier x range rule; prints counted over the last 10 sessions;\n"
              f"win_% / Rc / ret_20d_% are for episodes with >={need} prints, *_rest for all other episodes of the same cell; "
              f"n_accel = those with CMF expansion):")
        print(table.to_string())
        print("Read n>=%d first: a cell with fewer than ~100 such episodes cannot show an edge of a few points either way." % need)
    except Exception as exc:
        print(f"footprint grid tables skipped ({type(exc).__name__}: {exc})")


LEARN_FEATURE_COLUMNS = {"rs_score": "rs_score", "rs_swing_score": "rs_swing", "breakout_pct": "breakout_pct",
                         "median_dv20": "median_dv20", "close_position": "close_position", "fp_score": "fp_score"}
LEARN_RESULT_COLUMNS = ("signal_date", "ticker", "pattern", "entry_quality", "evaluation_status", "bracket_result_20d",
                        "bracket_net_r_20d")


def sklearn_available():
    """(True, version) when scikit-learn imports, else (False, reason). Catches every Exception, not only ImportError: a broken
    binary install (for example a numpy ABI mismatch) raises ValueError / AttributeError / OSError instead."""
    try:
        import sklearn
        from sklearn.tree import DecisionTreeClassifier  # noqa: F401
        return True, str(getattr(sklearn, "__version__", "?"))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _learn_warning(reason):
    print("\n=== Learner replay on backtest rows (research only) ===")
    print(f"WARNING: scikit-learn is not usable in this environment ({reason}).")
    print("         The decision-tree learner replay is SKIPPED - this is not a failure of the backtest itself, the CSV is complete.")
    print("         To enable it add scikit-learn to the pip install step of the workflow (pip install scikit-learn).")
    print("         Showing a descriptive, pandas-only fallback below (no tree, no threshold proposal).")


def learn_descriptive(res, target=2.5):
    """pandas-only stand-in for the tree: for every learner feature, outcome by quintile of the feature (one row per episode,
    COMPLETE outcomes, AMBIGUOUS_BOTH excluded). Descriptive only - it proposes nothing. Never raises."""
    try:
        r20 = "bracket_net_r_20d"
        d = res.copy()
        d["_dt"] = pd.to_datetime(d["signal_date"])
        d = d.sort_values(["ticker", "pattern", "_dt"])
        gap = d.groupby(["ticker", "pattern"])["_dt"].diff().dt.days
        ep = d[gap.isna() | (gap > 14)].copy()
        if "evaluation_status" in ep:
            ep = ep[ep["evaluation_status"] == "COMPLETE"]
        if "bracket_result_20d" in ep:
            ep = ep[ep["bracket_result_20d"].astype(str) != "AMBIGUOUS_BOTH"]
        ep = ep.copy()
        ep[r20] = pd.to_numeric(ep[r20], errors="coerce")
        ep = ep[ep[r20].notna()].copy()
        if len(ep) < 50:
            print(f"Descriptive fallback: only {len(ep)} complete episodes - not enough for quintile tables.")
            return
        ep["_rc"] = ep[r20].clip(-3.0, 5.0)
        print(f"Descriptive fallback on {len(ep)} episodes: base win {100 * (ep[r20] > 0).mean():.1f}% | mean Rc {ep['_rc'].mean():+.3f} | "
              f"R>={target:g} {100 * (ep[r20] >= target).mean():.1f}%")
        for feat, col in LEARN_FEATURE_COLUMNS.items():
            if col not in ep:
                continue
            x = pd.to_numeric(ep[col], errors="coerce")
            ok = x.notna()
            if ok.sum() < 50 or x[ok].nunique() < 3:
                continue
            q = pd.qcut(x[ok], 5, duplicates="drop")
            g = ep[ok].groupby(q, observed=True)
            tab = pd.DataFrame({"n": g.size(), "win_%": (g[r20].apply(lambda s: (s > 0).mean()) * 100).round(1),
                                "mean_Rc": g["_rc"].mean().round(3),
                                f"R>={target:g}_%": (g[r20].apply(lambda s: (s >= target).mean()) * 100).round(1)})
            print(f"\nBy {feat} (quintiles of column '{col}'):\n{tab.to_string()}")
    except Exception as exc:
        print(f"Descriptive fallback skipped ({type(exc).__name__}: {exc})")


def learn_report(res, m):
    """v3.3/v3.4: replay run_automated_threshold_learning on the backtest rows (temp folder; the real learned-params file is not
    touched). Rows are converted to the live evaluations-CSV layout, so the exact same code path runs.
    v3.4: never raises. Without a usable scikit-learn (or a scanner without the learner, or any failure inside it) it prints a
    clear WARNING with the reason and falls back to learn_descriptive() instead of a tree."""
    try:
        if res is None or res.empty:
            return
        target = float(getattr(m, "LEARN_TARGET_R", 2.5))
        ok, info = sklearn_available()
        if not ok:
            _learn_warning(info)
            learn_descriptive(res, target)
            return
        fn = getattr(m, "run_automated_threshold_learning", None)
        if fn is None:
            print("\n=== Learner replay on backtest rows (research only) ===")
            print("WARNING: this scanner has no run_automated_threshold_learning (needs v9.7.7 or later) - replay SKIPPED.")
            learn_descriptive(res, target)
            return
        import json
        import tempfile
        keep = [c for c in LEARN_RESULT_COLUMNS + tuple(LEARN_FEATURE_COLUMNS.values()) if c in res.columns]
        d = res[keep]                                   # only the columns the learner needs (the full frame has 150+ columns)
        metas = []
        for rec in d.to_dict("records"):
            metrics = {}
            for feat, col in LEARN_FEATURE_COLUMNS.items():
                v = rec.get(col)
                if v is not None and isinstance(v, (int, float)) and np.isfinite(v):
                    metrics[feat] = float(v)
            metas.append(json.dumps({"entry_diagnostics_v3": {"metrics": metrics}}))
        frame = pd.DataFrame({"code_version": getattr(m, "CODE_VERSION", ""), "schema_version": 2,
                              "signal_date": d["signal_date"], "ticker": d["ticker"], "pattern": d["pattern"], "stage": "BACKTEST",
                              "entry_quality": d.get("entry_quality"), "evaluation_status": d.get("evaluation_status"),
                              "bracket_result_20d": d.get("bracket_result_20d"), "bracket_net_r_20d": d.get("bracket_net_r_20d"),
                              "metadata_json": metas})
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "bt_evaluations.csv")
            frame.to_csv(csv_path, index=False)
            rep = fn(csv_path=csv_path, params_path=os.path.join(tmp, "bt_learned_params.json"), write=True)
        if (rep or {}).get("status") == "NO_SKLEARN":    # the scanner's own import check disagreed with ours
            _learn_warning("the scanner reported NO_SKLEARN")
            learn_descriptive(res, target)
            return
        data = rep.get("data") or {}
        print("\n=== Learner replay on backtest rows (research only) ===")
        print(f"scikit-learn {info}")
        print(f"status={rep.get('status')} | episodes={data.get('episodes')} | positives(R>={target:g})={data.get('positives')} "
              f"| base rate={data.get('base_rate')} | split={data.get('cut_date')}")
        print(f"features used={data.get('features_used')} | dropped={data.get('features_dropped')}")
        if (rep.get("model") or {}).get("tree_text"):
            print(rep["model"]["tree_text"])
        print("best leaf:", rep.get("best_leaf"))
        print("proposed gates:", rep.get("gates"), "| score floor:", rep.get("min_score_floor"))
        print("ignored constraints:", rep.get("ignored_constraints"))
        print("validation:", rep.get("validation"))
        print("rejection reasons:", rep.get("rejection_reasons"))
    except Exception as exc:
        try:
            print("\n=== Learner replay on backtest rows (research only) ===")
            print(f"WARNING: learner replay failed and was skipped ({type(exc).__name__}: {exc}). The backtest CSV is unaffected.")
            learn_descriptive(res, 2.5)
        except Exception:
            pass


def sample_universe(path, n, seed, min_cap, offset=0):
    """Random, reproducible sample of the scanner's real universe (known market cap >= min_cap only).
    Same seed + different offsets = disjoint shards of one shuffled list."""
    import json
    import random
    with open(path, encoding="utf-8") as fh:
        cache = json.load(fh)
    caps = cache.get("market_caps") or {}
    pool = []
    for t in cache.get("tickers", []):
        sym = str(t).strip().upper()
        cap = caps.get(sym)
        if sym.isalpha() and len(sym) <= 5 and cap is not None and float(cap) >= min_cap:
            pool.append(sym)
    pool = sorted(set(pool))
    random.Random(seed).shuffle(pool)
    offset = max(0, int(offset))
    return pool[offset: offset + max(0, n)]


def table_single(single, r20):
    g = single.dropna(subset=[r20]).groupby("only_blocker")[r20]
    return pd.DataFrame({"n": g.size(), "mean_R_20d": g.mean().round(3),
                         "win_%": (g.apply(lambda s: (s > 0).mean()) * 100).round(1)}).sort_values("n", ascending=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scanner", required=True)
    ap.add_argument("--tickers", default="")
    ap.add_argument("--tickers-file", default="")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", default="scanner_backtest_results.csv")
    ap.add_argument("--universe-cache", default="",
                    help="scanner_state/universe_cache.json — used when no tickers are given")
    ap.add_argument("--sample", type=int, default=150, help="random tickers drawn from the universe cache")
    ap.add_argument("--seed", type=int, default=1, help="same seed = same sample (reproducible)")
    ap.add_argument("--offset", type=int, default=0, help="skip this many tickers of the shuffled list (disjoint shards)")
    ap.add_argument("--window-bars", type=int, default=0, help="bars of history per signal day (0 = all, the default)")
    ap.add_argument("--paths-out", default="", help="also write the forward price path of every ENTRY_READY signal to this CSV")

    def float_list(name):
        def conv(text):
            try:
                return parse_float_list(text, name)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(str(exc))
        return conv
    ap.add_argument("--no-fp-grid", action="store_true", help="v3.4: do not compute the footprint grid columns")
    ap.add_argument("--vol-grid", type=float_list("--vol-grid"), default=FP_GRID_VOL_MULTS,
                    help="v3.4: volume multipliers of the footprint grid (default 1.2,1.5,1.8,2.2)")
    ap.add_argument("--abs-caps", type=float_list("--abs-caps"), default=FP_GRID_ABS_CAPS_PCT,
                    help="v3.4: absolute range caps in percent of the close (default 0.5,1.0,1.5,2.0)")
    ap.add_argument("--atr-mults", type=float_list("--atr-mults"), default=FP_GRID_ATR_MULTS,
                    help="v3.4: range caps as multiples of ATR14 (default 0.1,0.2,0.3,0.4,0.6,0.8)")
    args = ap.parse_args()
    grid_cfg = {"enabled": not args.no_fp_grid, "vol_mults": args.vol_grid, "abs_caps": args.abs_caps, "atr_mults": args.atr_mults}
    run_t0 = time.time()

    import yfinance as yf
    m = load_scanner(args.scanner)
    symbols = [s.strip().upper() for s in args.tickers.split(",") if s.strip()]
    if args.tickers_file:
        with open(args.tickers_file) as fh:
            symbols += [ln.strip().upper() for ln in fh if ln.strip()]
    if not symbols and args.universe_cache:
        symbols = sample_universe(args.universe_cache, args.sample, args.seed, m.MIN_MARKET_CAP_USD, args.offset)
        print(f"Universe sample: {len(symbols)} tickers (seed={args.seed}, offset={args.offset}) -> {','.join(symbols)}")
    if not symbols:
        sys.exit("No tickers: pass --tickers, --tickers-file or --universe-cache")
    hist_start = (pd.Timestamp(args.start) - pd.Timedelta(days=500)).date().isoformat()
    hist_end = (pd.Timestamp(args.end) + pd.Timedelta(days=45)).date().isoformat()
    spy = download("SPY", hist_start, hist_end, yf)
    pit = PointInTimeSPY(spy)
    patch(m, pit)
    if grid_cfg["enabled"]:
        fp_grid_specs(grid_cfg, float(getattr(m, "SM_SPIKE_MULT", 1.5)))      # fail fast on colliding settings, before any download
        print(f"Harness scanner_backtest {HARNESS_VERSION} | footprint grid: volume x{','.join(f'{x:g}' for x in grid_cfg['vol_mults'])} | "
              f"abs caps {','.join(f'{x:g}' for x in grid_cfg['abs_caps'])}% | ATR14 mults {','.join(f'{x:g}' for x in grid_cfg['atr_mults'])}")
    else:
        print(f"Harness scanner_backtest {HARNESS_VERSION} | footprint grid: off")

    rows = []
    paths = [] if args.paths_out else None
    for k, sym in enumerate(dict.fromkeys(symbols), 1):
        t0 = time.time()
        df = download(sym, hist_start, hist_end, yf)
        if df is None or len(df) < 300:
            print(f"[{k}/{len(symbols)}] {sym}: not enough data")
            continue
        got = scan_history(m, sym, df, spy, pit, args.start, args.end, window_bars=args.window_bars, paths=paths,
                           grid_cfg=grid_cfg)
        rows.extend(got)
        print(f"[{k}/{len(symbols)}] {sym}: {len(got)} candidate-days in {time.time() - t0:.0f}s")
    res = pd.DataFrame(rows)
    if not res.empty:
        finalize_grid_columns(res)
    res.to_csv(args.out, index=False)
    if paths is not None:
        pd.DataFrame(paths).to_csv(args.paths_out, index=False)
        print(f"Saved {len(paths)} forward-path rows -> {args.paths_out}")
    print(f"\nSaved {len(res)} rows -> {args.out} | scanner {getattr(m, 'CODE_VERSION', '?')} | total {time.time() - run_t0:.0f}s")
    summarize(res)
    learn_report(res, m)                      # never raises (v3.4): without scikit-learn it prints a WARNING and a descriptive fallback


if __name__ == "__main__":
    main()
