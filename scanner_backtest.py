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

v3.6 (2026-11-04): RESEARCH BENCHMARK, no change to the scanner. Two additions, both computed inside the harness:
  1. Counterfactual NULL MODEL. For every session T on which at least one ENTRY_READY signal fired, _simulate_null_baseline()
     draws --null-samples (default 100) RANDOM LIQUID tickers of the same historical universe on that same session and measures what a
     trade with the SAME bracket geometry would have done: the stop and target of the real signals of that day, as percentages of the
     signal-bar close, are dealt out over the draws; fill = next-session open x (1 + slippage); the scanner's own static bracket rules
     (GAP_STOP / GAP_TARGET / AMBIGUOUS_BOTH / STOP / TARGET, stop first), 5 / 10 / 20 sessions. "Liquid" = the scanner's liquidity gate
     (median dollar volume of the 20 sessions BEFORE the signal bar >= LIQ_MEDIAN_MIN_DOLLAR_VOL, close >= LIQ_MIN_PRICE) and >= 260 bars
     of history and a complete 20-session forward window. The real signal names of that day are not drawn. Everything is one vectorised
     pass over a dates x tickers panel (no loop over dates); the draws are counter-based (seed, date, ticker), so a single date gives the
     same rows alone as inside the full run. The 20d R is evaluated by evaluate_brackets_vectorized(), tested against the scanner's own
     evaluate_setup_path on thousands of random paths.
     Files: <out>_null.csv (one row per draw, legacy outcome column names), <out>_null_by_date.csv (per-date baseline: real vs null counts,
     win %, mean Rc, mean 20d return, null sd, regime), <out>_null_table.csv (the numeric comparison). The log ends with the consolidated
     "Null Model Contrast Table": real ENTRY_READY episodes vs the date-matched null (all / EMA28-ok / each regime / in-sample / out-of-sample),
     with date-cluster bootstrap intervals and the mean position of the real outcome inside its own day's null (0.5 = no edge).
     An ENTRY_READY episode = the first ENTRY_READY row of a ticker+pattern after more than 14 days without one (what the live scanner
     alerts on). The older summary tables count an episode as READY only if its FIRST candidate day was already READY - on the same
     rows that is far fewer episodes (they are left unchanged, for comparability with earlier runs).
  2. CROSS-SECTIONAL RANKS. rs_score, rs_swing_score, breakout_pct and close_position are converted into day-T percentiles (0..1) among
     the candidate rows of the SAME session of the run (each ticker counts once per day) and appended as cs_rank_rs,
     cs_rank_swing, cs_rank_breakout, cs_rank_position, plus cs_n_day (distinct tickers with a candidate row that session). A table shows the
     pooled Spearman IC of the rank vs the absolute value, with a date-cluster bootstrap interval (and a Bonferroni-adjusted one).
  Back-compatibility: all legacy columns keep name, value and position (the new ones are appended at the end); with --no-cs-rank the
  results CSV is byte-identical to v3.5 (tested), --no-null writes no extra file; every new table skips itself on an older CSV shape
  instead of failing.
  Not changed: the static bracket is the research evaluator, not the V9.2 exit policy; the null is NOT conditioned on trend or pattern;
  the universe is today's list (survivorship); the sample is a subset of the universe, so cross-sectional cohorts are smaller than live.

Usage (in your own environment, where yfinance is installed):
  python scanner_backtest.py --scanner stock_scanner_unified.py \
      --tickers AAPL,MSFT,NVDA --start 2023-01-01 --end 2026-09-30 --out bt.csv
  or --tickers-file tickers.txt (one symbol per line)
  v3.6 options (all optional):  --null-samples 100  --null-out bt_null.csv  --null-boot 2000  --oos-date 2025-07-01  --no-null  --no-cs-rank
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
import zlib
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
HARNESS_VERSION = "v3.6"
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
        cs_rank_tables(ep, r20, ready_episodes_frame(res))
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


# ---------------------------------------------------------------- v3.6: finite-number guards
def _is_finite_number(value):
    """True only for a real, finite number (not None / NaN / +-inf / text). Scalar twin of the scanner's helper of the same name."""
    try:
        return bool(np.isfinite(float(value)))
    except Exception:
        return False


def _finite_or_nan(values):
    """Array twin of _is_finite_number: every non-finite element (NaN, +-inf, unparsable) becomes NaN, the shape is kept. Applied
    after every division / ratio of a matrix computation, so an Inf can never travel into a mean, a rank or a bootstrap."""
    try:
        a = np.asarray(values, dtype="float64")
    except (TypeError, ValueError):
        a = pd.to_numeric(pd.Series(np.asarray(values).ravel()), errors="coerce").to_numpy(dtype="float64").reshape(np.shape(values))
    return np.where(np.isfinite(a), a, np.nan)


def _num(df, col):
    """float64 array of a column; all-NaN when the column does not exist (legacy CSV shapes). Never raises."""
    if df is None or col not in df.columns:
        return np.full(0 if df is None else len(df), np.nan)
    return _finite_or_nan(pd.to_numeric(df[col], errors="coerce").to_numpy(dtype="float64"))


def _as_bool(series):
    """Legacy-safe boolean: True/False, 1/0 and the text 'True'/'False' (what a CSV read-back gives); NaN / anything else -> False."""
    s = pd.Series(series)
    if s.dtype == bool:
        return s
    return s.astype(str).str.strip().str.lower().isin(("true", "1", "1.0", "yes"))


# ---------------------------------------------------------------- v3.6: cross-sectional percentile ranks
CS_RANK_SOURCES = (("cs_rank_rs", ("rs_score",)),
                   ("cs_rank_swing", ("rs_swing_score", "rs_swing")),    # v3.5 CSVs call the swing score 'rs_swing'
                   ("cs_rank_breakout", ("breakout_pct",)),
                   ("cs_rank_position", ("close_position",)))
CS_RANK_COLUMNS = tuple(name for name, _ in CS_RANK_SOURCES)
RC_LO, RC_HI = -3.0, 5.0                      # the clip used by every Rc number of this harness


def cs_midrank_percentile(values, day, ticker=None):
    """Cross-sectional percentile of every row among the rows of the SAME day, in (0, 1): (W_less + W_equal / 2) / W_total.
    Ties get the mid-rank. Each ticker counts ONCE per day: a ticker that has m candidate rows (several patterns) gives every row
    the weight 1/m, so ticker-level inputs (rs_score, close_position) are not double counted. Non-finite inputs -> NaN and carry no
    weight. With unit weights this is exactly (average_rank - 0.5) / n. Fully vectorised (sort + group cumulative sums)."""
    x = _finite_or_nan(pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype="float64"))
    out = np.full(len(x), np.nan)
    pos = np.flatnonzero(np.isfinite(x))
    if pos.size == 0:
        return out
    d = pd.factorize(pd.Series(day).astype(str).to_numpy()[pos])[0]
    t = pd.factorize(pd.Series(ticker).astype(str).to_numpy()[pos])[0] if ticker is not None else np.arange(pos.size)
    f = pd.DataFrame({"pos": pos, "d": d, "t": t, "x": x[pos]})
    f["w"] = 1.0 / f.groupby(["d", "t"])["pos"].transform("size")
    f = f.sort_values(["d", "x"], kind="mergesort")
    f["cum"] = f.groupby("d")["w"].cumsum()
    tie = f.groupby(["d", "x"], sort=False)
    gw, cend = tie["w"].transform("sum"), tie["cum"].transform("max")
    tot = f.groupby("d")["w"].transform("sum")
    p = _finite_or_nan(((cend - gw) + 0.5 * gw) / tot)
    out[f["pos"].to_numpy()] = np.clip(p.astype("float64"), 0.0, 1.0)
    return out


def add_cross_sectional_ranks(res):
    """v3.6: returns the frame with cs_n_day (distinct tickers with a candidate row that session) and the cs_rank_* columns to the END of the frame; every legacy
    column keeps its name, value and position. A source column that does not exist (older CSV shape) gives an empty cs_rank_ column.
    The cross-section of day T = all candidate rows of that signal_date of THIS run (not only ENTRY_READY ones)."""
    if res is None or res.empty or not {"signal_date", "ticker"}.issubset(res.columns):
        return res
    day = res["signal_date"].astype(str)
    tick = res["ticker"].astype(str)
    new = {"cs_n_day": tick.groupby(day).transform("nunique").astype("int64").to_numpy()}
    for name, sources in CS_RANK_SOURCES:
        src = next((s for s in sources if s in res.columns), None)
        new[name] = cs_midrank_percentile(res[src], day, tick) if src else np.full(len(res), np.nan)
    old = [c for c in new if c in res.columns]                                   # idempotent: a rerun replaces the old columns
    return pd.concat([res.drop(columns=old), pd.DataFrame(new, index=res.index)], axis=1)


# ---------------------------------------------------------------- v3.6: price panel + vectorised bracket evaluator
NULL_TRACK_NAME = "RANDOM_LIQUID"
NULL_HORIZONS = (5, 10, 20)
NULL_DEFAULT_SAMPLES = 100
NULL_MIN_DRAWS_PER_DATE = 10                  # a date with fewer COMPLETE null draws is dropped from the comparison
NULL_MIN_REAL_EPISODES = 10                   # a subset with fewer real episodes is not shown
NULL_MIN_HISTORY = 260                        # same warm-up as scan_history(min_bars=260)
NULL_DEFAULT_MIN_DV = 15_000_000.0            # = scanner LIQ_MEDIAN_MIN_DOLLAR_VOL
NULL_DEFAULT_MIN_PRICE = 5.0                  # = scanner LIQ_MIN_PRICE
NULL_LIQ_LOOKBACK = 20                        # = scanner LIQ_LOOKBACK
NULL_SLIPPAGE_BPS = 5.0                       # = scanner RESEARCH_SLIPPAGE_BPS
OOS_SPLIT_DATE = "2025-07-01"
_EVENT_NAMES = np.array(["", "GAP_STOP", "GAP_TARGET", "AMBIGUOUS_BOTH", "STOP", "TARGET"], dtype=object)
_STATUS_NAMES = np.array(["COMPLETE", "INVALID_PRICE_HISTORY", "INVALID_PLANNED_GEOMETRY", "SKIPPED_OPEN_OUTSIDE_BRACKET",
                          "NO_GEOMETRY"], dtype=object)


def evaluate_brackets_vectorized(o, h, l, c, planned, stop, target, slippage_bps=NULL_SLIPPAGE_BPS, horizons=NULL_HORIZONS,
                                 need_geometry=True):
    """Array twin of the scanner's evaluate_setup_path (same rules, same order, same slippage), for any leading shape.
    o / h / l / c: (..., B) bars AFTER the signal bar, B >= max(horizons); planned / stop / target: (...) price levels.
    Rules (copied from the scanner): bars must be finite, > 0 and OHLC-consistent; 0 < stop < planned < target; fill = open of the
    first bar x (1 + slip) and must lie strictly between stop and target; per bar the FIRST event wins in this order: open <= stop
    GAP_STOP, open >= target GAP_TARGET, both touched AMBIGUOUS_BOTH (exit at the stop), low <= stop STOP, high >= target TARGET;
    exits are x (1 - slip); R = (exit - fill) / (fill - stop); at each horizon: still open -> mark-to-close R.
    need_geometry=False skips the bracket (only price / return / MFE / MAE are filled; bracket columns stay empty).
    Returns a dict of arrays (floats are NaN where a row is not COMPLETE or a value is not finite)."""
    if not _is_finite_number(slippage_bps):
        raise ValueError("slippage_bps must be a finite number")
    slip = max(0.0, float(slippage_bps)) / 10000.0
    horizons = tuple(sorted({int(x) for x in horizons}))
    nb = max(horizons)
    o, h, l, c = (np.asarray(x, dtype="float64") for x in (o, h, l, c))
    if o.shape[-1] < nb or not (o.shape == h.shape == l.shape == c.shape):
        raise ValueError("bar arrays must share one shape and hold at least max(horizons) bars on the last axis")
    o, h, l, c = (x[..., :nb] for x in (o, h, l, c))
    planned, stop, target = (np.broadcast_to(np.asarray(x, dtype="float64"), o.shape[:-1]) for x in (planned, stop, target))
    xs = 1.0 - slip
    with np.errstate(all="ignore"):
        bars_ok = (np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (o > 0) & (h > 0) & (l > 0) & (c > 0)
                   & (h >= np.maximum(np.maximum(o, l), c)) & (l <= np.minimum(np.minimum(o, h), c)))
        price_ok = bars_ok.all(axis=-1)
        fill = o[..., 0] * (1.0 + slip)
        if need_geometry:
            geom_ok = (np.isfinite(planned) & np.isfinite(stop) & np.isfinite(target) & (planned > 0) & (stop > 0) & (target > 0)
                       & (stop < planned) & (planned < target))
            in_bracket = (stop < fill) & (fill < target)
        else:
            geom_ok = np.ones(o.shape[:-1], dtype=bool)
            in_bracket = np.ones(o.shape[:-1], dtype=bool)
        status = np.select([~price_ok, ~geom_ok, ~in_bracket], [1, 2, 3], default=0)
        good = status == 0
        risk = fill - stop
        s3, t3 = stop[..., None], target[..., None]
        if need_geometry:
            gap_s = o <= s3
            gap_t = ~gap_s & (o >= t3)
            free = ~gap_s & ~gap_t
            both = free & (l <= s3) & (h >= t3)
            stp = free & ~both & (l <= s3)
            tgt = free & ~both & ~stp & (h >= t3)
            code = np.select([gap_s, gap_t, both, stp, tgt], [1, 2, 3, 4, 5], default=0).astype(np.int8)
            exit_px = np.select([code == 1, code == 2, code == 3, code == 4, code == 5],
                                [o * xs, o * xs, np.broadcast_to(s3 * xs, o.shape), np.broadcast_to(s3 * xs, o.shape),
                                 np.broadcast_to(t3 * xs, o.shape)], default=np.nan)
            happened = code > 0
            first = np.argmax(happened, axis=-1)
            any_ev = happened.any(axis=-1)
            ev_code = np.take_along_axis(code, first[..., None], axis=-1)[..., 0]
            ev_exit = np.take_along_axis(exit_px, first[..., None], axis=-1)[..., 0]
            ev_r = _finite_or_nan((ev_exit - fill) / risk)
        out = {"fill_price": np.where(price_ok & geom_ok, _finite_or_nan(fill), np.nan), "evaluation_status": _STATUS_NAMES[status]}
        if not need_geometry:
            out["evaluation_status"] = np.where(good, _STATUS_NAMES[4], _STATUS_NAMES[status])
        run_hi, run_lo = np.maximum.accumulate(h, axis=-1), np.minimum.accumulate(l, axis=-1)
        for hz in horizons:
            close_h = c[..., hz - 1]
            out[f"price_{hz}d"] = np.where(good, _finite_or_nan(close_h), np.nan)
            out[f"horizon_return_pct_{hz}d"] = np.where(good, _finite_or_nan((close_h * xs / fill - 1.0) * 100.0), np.nan)
            out[f"horizon_mfe_pct_{hz}d"] = np.where(good, _finite_or_nan((run_hi[..., hz - 1] / fill - 1.0) * 100.0), np.nan)
            out[f"horizon_mae_pct_{hz}d"] = np.where(good, _finite_or_nan((run_lo[..., hz - 1] / fill - 1.0) * 100.0), np.nan)
            if need_geometry:
                hit = any_ev & (first < hz)
                net_r = np.where(hit, ev_r, _finite_or_nan((close_h * xs - fill) / risk))
                out[f"bracket_result_{hz}d"] = np.where(good, np.where(hit, _EVENT_NAMES[ev_code], "OPEN"), None)
                out[f"bracket_net_r_{hz}d"] = np.where(good, _finite_or_nan(net_r), np.nan)
            else:
                out[f"bracket_result_{hz}d"] = np.full(o.shape[:-1], None, dtype=object)
                out[f"bracket_net_r_{hz}d"] = np.full(o.shape[:-1], np.nan)
    return out


class PricePanel:
    """Dates x tickers matrices of open / high / low / close / volume (NaN = no bar). Built once from the historical universe, so the
    null model never touches a DataFrame per date. Tickers are sorted, so a draw does not depend on the order of the input."""

    def __init__(self, dates, tickers, o, h, l, c, v):
        self.dates = pd.DatetimeIndex(dates)
        self.tickers = np.asarray(tickers, dtype=object)
        self.o, self.h, self.l, self.c, self.v = (np.asarray(x, dtype="float64") for x in (o, h, l, c, v))
        self._ticker_index = pd.Index(self.tickers)
        self._cache = {}
        self.ticker_hash = np.array([zlib.crc32(str(t).encode("utf-8")) for t in self.tickers], dtype=np.uint64)

    def ticker_ids(self, symbols):
        return self._ticker_index.get_indexer(pd.Index(pd.Series(symbols).astype(str).str.strip().str.upper()))

    def date_ids(self, dates):
        return self.dates.get_indexer(pd.DatetimeIndex(pd.to_datetime(pd.Series(dates))).normalize())

    def eligibility(self, horizon=20, min_dollar_vol=NULL_DEFAULT_MIN_DV, min_price=NULL_DEFAULT_MIN_PRICE,
                    min_history=NULL_MIN_HISTORY, lookback=NULL_LIQ_LOOKBACK):
        """(base_ok, median_dv) as (dates x tickers) arrays. base_ok[t, k] = ticker k could have been a candidate on session t:
        the signal bar is valid, the median dollar volume of the `lookback` sessions BEFORE t (close x volume, the scanner's
        convention) and the close pass the liquidity gate, >= min_history valid bars of history, and the next `horizon` bars are
        all valid (a COMPLETE 20-session window - the same condition the real rows need to enter any table)."""
        key = (int(horizon), float(min_dollar_vol), float(min_price), int(min_history), int(lookback))
        if key in self._cache:
            return self._cache[key]
        R, T = self.c.shape
        with np.errstate(all="ignore"):
            o, h, l, c, v = self.o, self.h, self.l, self.c, self.v
            bar_ok = (np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (o > 0) & (h > 0) & (l > 0) & (c > 0)
                      & (h >= np.maximum(np.maximum(o, l), c)) & (l <= np.minimum(np.minimum(o, h), c)))
            dv = _finite_or_nan(c * v)
            med = pd.DataFrame(dv).rolling(int(lookback), min_periods=int(lookback)).median().shift(1).to_numpy(dtype="float64")
            med = _finite_or_nan(med)
            n_hist = np.cumsum(bar_ok, axis=0)
            bad = np.vstack([np.zeros((1, T)), np.cumsum(~bar_ok, axis=0)])           # bad[k] = invalid bars among rows < k
            t_idx = np.arange(R)
            end = t_idx + 1 + int(horizon)                                            # exclusive end of the forward window
            fwd_ok = (end <= R)[:, None] & ((bad[np.minimum(end, R)] - bad[t_idx + 1]) == 0)
            base = (bar_ok & np.isfinite(med) & (med >= float(min_dollar_vol)) & (c >= float(min_price))
                    & (n_hist >= int(min_history) + 1) & fwd_ok)
        self._cache[key] = (base, med)
        return base, med


def _coerce_universe(df_universe):
    """Long frame [date, ticker, open, high, low, close, volume] from a long frame or a {ticker: OHLCV frame} dict."""
    if isinstance(df_universe, dict):
        parts = []
        for sym, f in df_universe.items():                                             # one pass over tickers (not over dates)
            if f is None or len(f) == 0:
                continue
            g = f.copy()
            g.columns = [str(x).lower() for x in g.columns]
            if not {"open", "high", "low", "close", "volume"}.issubset(g.columns):
                continue
            g = g[["open", "high", "low", "close", "volume"]]
            g.insert(0, "ticker", str(sym).strip().upper())
            g.insert(0, "date", g.index)
            parts.append(g.reset_index(drop=True))
        if not parts:
            raise ValueError("df_universe holds no usable OHLCV frame")
        return pd.concat(parts, ignore_index=True)
    long = df_universe.copy()
    long.columns = [str(x).lower() for x in long.columns]
    if "date" not in long.columns:
        long = long.reset_index()
        long = long.rename(columns={long.columns[0]: "date"})
        long.columns = [str(x).lower() for x in long.columns]
    need = {"date", "ticker", "open", "high", "low", "close", "volume"}
    if not need.issubset(long.columns):
        raise ValueError("df_universe needs the columns " + ", ".join(sorted(need)))
    return long


def build_price_panel(df_universe):
    """PricePanel from the historical universe (long frame or dict of frames). Duplicate (date, ticker) rows keep the last one;
    non-numeric / infinite values become NaN (= no bar)."""
    if isinstance(df_universe, PricePanel):
        return df_universe
    long = _coerce_universe(df_universe)
    dt = pd.to_datetime(long["date"])
    if getattr(dt.dt, "tz", None) is not None:
        dt = dt.dt.tz_convert(None)
    long = long.assign(date=dt.dt.normalize(), ticker=long["ticker"].astype(str).str.strip().str.upper())
    long = long.drop_duplicates(["date", "ticker"], keep="last")
    cols = ("open", "high", "low", "close", "volume")
    for col in cols:
        long[col] = _finite_or_nan(pd.to_numeric(long[col], errors="coerce").to_numpy(dtype="float64"))
    piv = {col: long.pivot(index="date", columns="ticker", values=col) for col in cols}
    dates = piv["close"].index.sort_values()
    tickers = sorted(piv["close"].columns)
    mats = [piv[col].reindex(index=dates, columns=tickers).to_numpy(dtype="float64") for col in cols]
    return PricePanel(dates, tickers, *mats)


def _as_panel(df_universe):
    return df_universe if isinstance(df_universe, PricePanel) else build_price_panel(df_universe)


# ---------------------------------------------------------------- v3.6: counterfactual null model
_U64 = np.uint64


def _splitmix64(x):
    """Counter-based 64-bit mixer (splitmix64) on a uint64 array: the same input always gives the same output."""
    with np.errstate(over="ignore"):
        x = x + _U64(0x9E3779B97F4A7C15)
        z = (x ^ (x >> _U64(30))) * _U64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> _U64(27))) * _U64(0x94D049BB133111EB)
        return z ^ (z >> _U64(31))


def _u01_keys(seed, date_ord, ticker_hash):
    """(D, T) uniform keys in [0, 1) that depend ONLY on (seed, calendar date, ticker). Sorting a date's eligible tickers by this key
    is a uniform draw without replacement - and it is the same draw whether one date or all dates are requested in the same call."""
    s = _U64(int(seed) & 0xFFFFFFFFFFFFFFFF)
    d = np.asarray(date_ord, dtype=np.int64).astype(_U64)[:, None]
    t = np.asarray(ticker_hash, dtype=_U64)[None, :]
    with np.errstate(over="ignore"):
        z = _splitmix64(_splitmix64(s ^ (d * _U64(0xD6E8FEB86659FD93))) + t)
    return (z >> _U64(11)).astype("float64") / float(1 << 53)


NULL_COLUMNS = ("null_track", "signal_date", "ticker", "null_id", "n_eligible", "stop_pct", "target_pct", "planned_entry", "stop",
                "target", "median_dv20", "fill_price", "evaluation_status")


def _null_geometry_arrays(geometry, date_index, n_eff):
    """Per (date, draw) stop_pct / target_pct, vectorised. The real geometries of a date are dealt out in turn (draw j gets row
    j mod k of that date), so every real geometry is represented about equally. Returns (stop_pct, target_pct) shaped (D, n_eff),
    NaN where the date has no usable geometry."""
    D = len(date_index)
    nan = np.full((D, n_eff), np.nan)
    if geometry is None or len(geometry) == 0:
        return nan, nan.copy()
    g = pd.DataFrame({"sp": _num(geometry, "stop_pct"), "tp": _num(geometry, "target_pct")})
    if "signal_date" in geometry.columns:
        g["dc"] = date_index.get_indexer(pd.DatetimeIndex(pd.to_datetime(geometry["signal_date"].reset_index(drop=True))).normalize())
        g["tk"] = geometry["ticker"].astype(str).to_numpy() if "ticker" in geometry.columns else ""
    else:
        g = g.loc[g.index.repeat(D)].reset_index(drop=True)                      # same geometry list for every requested date
        g["dc"] = np.tile(np.arange(D), len(geometry))
        g["tk"] = ""
    g = g[np.isfinite(g["sp"]) & np.isfinite(g["tp"]) & (g["sp"] < 0) & (g["tp"] > 0) & (g["dc"] >= 0)]
    if g.empty:
        return nan, nan.copy()
    g = g.sort_values(["dc", "tk", "sp", "tp"], kind="mergesort")
    dc = g["dc"].to_numpy()
    counts = np.bincount(dc, minlength=D)
    starts = np.cumsum(counts) - counts
    k = counts[:, None]
    gi = np.minimum(starts[:, None] + (np.arange(n_eff)[None, :] % np.maximum(k, 1)), len(g) - 1)
    has = k > 0
    return (np.where(has, g["sp"].to_numpy()[gi], np.nan), np.where(has, g["tp"].to_numpy()[gi], np.nan))


def _simulate_null_baseline(df_universe, signal_date, n_samples=NULL_DEFAULT_SAMPLES, geometry=None, exclude=None, seed=1,
                                  slippage_bps=NULL_SLIPPAGE_BPS, horizons=NULL_HORIZONS, min_dollar_vol=NULL_DEFAULT_MIN_DV,
                                  min_price=NULL_DEFAULT_MIN_PRICE, min_history=NULL_MIN_HISTORY, liq_lookback=NULL_LIQ_LOOKBACK):
    """Counterfactual benchmark: for the session(s) `signal_date`, draw `n_samples` RANDOM LIQUID tickers of the historical
    universe (the same sessions the real ENTRY_READY signals fired on) and measure what a trade with the SAME bracket geometry
    would have done over the next 20 sessions (next-session open fill, same slippage, same stop-first rules as the scanner).

    df_universe   PricePanel, long frame [date, ticker, open, high, low, close, volume], or {ticker: OHLCV frame}.
    signal_date  one date or an array of dates. All dates are processed in ONE vectorised pass (no per-date loop); a single date
                  gives exactly the same rows as it does inside a many-date call (counter-based random keys).
    geometry      frame with stop_pct (< 0) and target_pct (> 0) as FRACTIONS OF THE SIGNAL-BAR CLOSE (stop/close - 1, target/close - 1)
                  and optionally signal_date (+ ticker): the real geometries of that date are dealt out over the draws. A frame
                  without signal_date applies to every date. None -> no bracket: only return / MFE / MAE columns are filled.
    exclude       frame [signal_date, ticker]: those tickers are not drawn on that date (the real signal names).
    Eligible = the PricePanel.eligibility() rule (valid bar, liquidity gate on the 20 sessions before the signal bar, price >= 5,
    >= 260 bars of history, a complete forward window). Fewer eligible tickers than n_samples -> all of them are used.
    Returns one row per draw with the legacy outcome column names (bracket_net_r_20d, horizon_return_pct_20d, ...)."""
    if not _is_finite_number(n_samples) or int(n_samples) < 1:
        raise ValueError("n_samples must be a finite number >= 1")
    if not _is_finite_number(seed):
        raise ValueError("seed must be a finite number")
    n_samples, seed = int(n_samples), int(seed)
    horizons = tuple(sorted({int(x) for x in horizons}))
    nb = max(horizons)
    panel = _as_panel(df_universe)
    want = pd.DatetimeIndex(pd.to_datetime(pd.Series(np.atleast_1d(signal_date)))).normalize().unique()
    ids = panel.date_ids(want.to_series())
    keep = ids >= 0
    ids, chosen = ids[keep], want[keep]
    empty = pd.DataFrame(columns=list(NULL_COLUMNS))
    if ids.size == 0 or panel.c.shape[1] == 0:
        return empty
    order_dates = np.argsort(ids, kind="stable")
    ids, chosen = ids[order_dates], chosen[order_dates]
    base_ok, med_dv = panel.eligibility(nb, min_dollar_vol, min_price, min_history, liq_lookback)
    T = panel.c.shape[1]
    elig = base_ok[ids].copy()                                                   # (D, T)
    if exclude is not None and len(exclude):
        di = chosen.get_indexer(pd.DatetimeIndex(pd.to_datetime(pd.Series(exclude["signal_date"].to_numpy()))).normalize())
        ti = panel.ticker_ids(exclude["ticker"].to_numpy())
        ok = (di >= 0) & (ti >= 0)
        elig[di[ok], ti[ok]] = False
    n_elig = elig.sum(axis=1)
    n_eff = int(min(n_samples, T))
    date_ord = chosen.values.astype("datetime64[D]").astype("int64")
    keys = np.where(elig, _u01_keys(seed, date_ord, panel.ticker_hash), 2.0)
    pick = np.argsort(keys, axis=1, kind="stable")[:, :n_eff]                    # (D, n_eff) ticker ids
    live = np.arange(n_eff)[None, :] < n_elig[:, None]                           # draw j exists only if the date has > j eligible names
    sp, tp = _null_geometry_arrays(geometry, chosen, n_eff)
    need_geometry = geometry is not None and len(geometry) > 0
    R = panel.c.shape[0]
    rows = np.minimum(ids[:, None, None] + 1 + np.arange(nb)[None, None, :], R - 1)   # (D, 1, nb) - clipped, masked by `live`
    cols = pick[:, :, None]
    bars = [m[rows, cols] for m in (panel.o, panel.h, panel.l, panel.c)]
    c0 = panel.c[ids[:, None], pick]                                             # signal-bar close of every draw
    with np.errstate(all="ignore"):
        stop, target = _finite_or_nan(c0 * (1.0 + sp)), _finite_or_nan(c0 * (1.0 + tp))
    ev = evaluate_brackets_vectorized(*bars, c0, stop, target, slippage_bps=slippage_bps, horizons=horizons,
                                      need_geometry=need_geometry)
    di_, jj = np.nonzero(live)
    out = {"null_track": np.full(di_.size, NULL_TRACK_NAME, dtype=object),
           "signal_date": chosen.strftime("%Y-%m-%d").to_numpy(dtype=object)[di_],
           "ticker": panel.tickers[pick[di_, jj]], "null_id": jj, "n_eligible": n_elig[di_],
           "stop_pct": sp[di_, jj], "target_pct": tp[di_, jj], "planned_entry": c0[di_, jj], "stop": stop[di_, jj],
           "target": target[di_, jj], "median_dv20": med_dv[ids[di_], pick[di_, jj]]}
    for name, arr in ev.items():
        out[name] = arr[di_, jj]
    return pd.DataFrame(out)


def real_geometry_frame(ready, panel):
    """stop / target of the real ENTRY_READY rows as fractions of the signal-bar close (panel lookup; planned_entry when the close
    is not in the panel). Rows with an impossible geometry (not stop < close < target) are dropped."""
    if ready is None or ready.empty:
        return pd.DataFrame(columns=["signal_date", "ticker", "stop_pct", "target_pct"])
    ti, di = panel.ticker_ids(ready["ticker"].to_numpy()), panel.date_ids(ready["signal_date"].to_numpy())
    close0 = np.full(len(ready), np.nan)
    ok = (ti >= 0) & (di >= 0)
    close0[ok] = panel.c[di[ok], ti[ok]]
    ref = np.where(np.isfinite(close0) & (close0 > 0), close0, _num(ready, "planned_entry"))
    with np.errstate(all="ignore"):
        sp, tp = _finite_or_nan(_num(ready, "stop") / ref - 1.0), _finite_or_nan(_num(ready, "target") / ref - 1.0)
    keep = np.isfinite(sp) & np.isfinite(tp) & (sp < 0) & (tp > 0)
    return pd.DataFrame({"signal_date": ready["signal_date"].astype(str).to_numpy()[keep],
                         "ticker": ready["ticker"].astype(str).to_numpy()[keep], "stop_pct": sp[keep], "target_pct": tp[keep]})


def episodes_frame(res):
    """The one-row-per-episode frame summarize() builds (first day of a ticker+pattern within 14 days). Same rule, reused here."""
    d = res.sort_values(["ticker", "pattern", "signal_date"])
    first = d.groupby(["ticker", "pattern"])["signal_date"].transform(lambda s: pd.to_datetime(s).diff().dt.days.fillna(999) > 14)
    return d[first].copy()


def ready_episodes_frame(res):
    """One row per ENTRY_READY episode: the first ENTRY_READY row of a ticker+pattern after more than 14 days without one - the unit the
    live scanner alerts on. NOTE: the older summary tables count an episode as READY only when its FIRST candidate day (of any status)
    was already READY; this frame counts every episode that reached ENTRY_READY at any time."""
    if res is None or res.empty or "entry_ready" not in res.columns:
        return pd.DataFrame() if res is None else res.iloc[0:0]
    r = res[_as_bool(res["entry_ready"])].sort_values(["ticker", "pattern", "signal_date"])
    first = r.groupby(["ticker", "pattern"])["signal_date"].transform(lambda s: pd.to_datetime(s).diff().dt.days.fillna(999) > 14)
    return r[first].copy()


# ---------------------------------------------------------------- v3.6: real vs null statistics (date-clustered)
NULL_METRICS = (("win", "win % (R>0)"), ("rc", "mean Rc"), ("ret20", "mean ret 20d %"), ("stop_hit", "stop hit %"),
                ("target_hit", "target hit %"), ("ambig", "ambiguous %"), ("mae20", "mean MAE 20d %"), ("mfe20", "mean MFE 20d %"))


def _outcome_metrics(df):
    """Per-row outcome metrics from the legacy outcome columns (rows without a finite 20d R are dropped). Never raises."""
    r = _num(df, "bracket_net_r_20d")
    res = df["bracket_result_20d"].astype(str) if "bracket_result_20d" in df.columns else pd.Series("", index=df.index)
    fin = np.isfinite(r)
    m = pd.DataFrame({"R": r, "win": np.where(fin, (r > 0).astype(float) * 100.0, np.nan),
                      "rc": np.clip(r, RC_LO, RC_HI), "ret20": _num(df, "horizon_return_pct_20d"),
                      "stop_hit": res.isin(("STOP", "GAP_STOP")).to_numpy(dtype=float) * 100.0,
                      "target_hit": res.isin(("TARGET", "GAP_TARGET")).to_numpy(dtype=float) * 100.0,
                      "ambig": (res == "AMBIGUOUS_BOTH").to_numpy(dtype=float) * 100.0,
                      "mae20": _num(df, "horizon_mae_pct_20d"), "mfe20": _num(df, "horizon_mfe_pct_20d")}, index=df.index)
    return m[fin].copy()


def _null_percentiles(real_r, real_dc, null_r, null_dc, n_dates):
    """Mid-rank percentile of every real R inside the null draws of ITS OWN date (0.5 = indistinguishable from random). Dense
    (date x draw) matrix, one comparison pass - no per-date loop. NaN where the date has no null draws."""
    pct = np.full(len(real_r), np.nan)
    if len(real_r) == 0 or len(null_r) == 0:
        return pct
    order = np.argsort(null_dc, kind="stable")
    dcs, rs = null_dc[order], null_r[order]
    counts = np.bincount(dcs, minlength=n_dates)
    starts = np.cumsum(counts) - counts
    mat = np.full((n_dates, max(int(counts.max()), 1)), np.nan)
    mat[dcs, np.arange(len(dcs)) - starts[dcs]] = rs
    sub = mat[real_dc]
    with np.errstate(invalid="ignore"):
        less, eq, cnt = (sub < real_r[:, None]).sum(axis=1), (sub == real_r[:, None]).sum(axis=1), np.isfinite(sub).sum(axis=1)
    return np.where(cnt > 0, (less + 0.5 * eq) / np.maximum(cnt, 1), np.nan)


def _null_vs_real(real_m, real_dc, null_m, null_dc, n_dates, boot, rng, min_null=None):
    """Date-matched comparison. Null mean = sum over dates of (real episodes that date) x (null mean that date) / sum of the weights, so
    the null sees the same days (= the same market regimes) as the real signals. Delta = real - null; the 95% interval and the
    two-sided p-value come from a date-cluster bootstrap (dates resampled with replacement, multinomial weights @ per-date sums).
    The last metric 'pctile' is the mean mid-rank of the real outcome inside its own date's null (null expectation 0.5)."""
    min_null = NULL_MIN_DRAWS_PER_DATE if min_null is None else min_null
    names = [k for k, _ in NULL_METRICS if k in real_m.columns and real_m[k].notna().any() and k in null_m.columns and null_m[k].notna().any()]
    if not names or len(real_m) == 0 or len(null_m) == 0:
        return pd.DataFrame()
    pct = _null_percentiles(real_m["R"].to_numpy(), real_dc, null_m["R"].to_numpy(), null_dc, n_dates)
    cols_r = [real_m[k].to_numpy(dtype="float64") for k in names] + [pct]
    cols_n = [null_m[k].to_numpy(dtype="float64") for k in names]
    n_null = np.bincount(null_dc, minlength=n_dates)
    A = np.zeros((n_dates, len(cols_r)))
    B = np.zeros_like(A)
    C = np.zeros_like(A)
    for j, xr in enumerate(cols_r):                                               # one pass over metrics (not over dates)
        fr = np.isfinite(xr)
        rs = np.bincount(real_dc[fr], weights=xr[fr], minlength=n_dates)
        rc = np.bincount(real_dc[fr], minlength=n_dates).astype(float)
        if j < len(names):
            xn = cols_n[j]
            fn = np.isfinite(xn)
            ns = np.bincount(null_dc[fn], weights=xn[fn], minlength=n_dates)
            nc = np.bincount(null_dc[fn], minlength=n_dates).astype(float)
            use = (rc > 0) & (nc >= min_null)
            with np.errstate(all="ignore"):
                nmean = _finite_or_nan(ns / nc)
            A[:, j], B[:, j], C[:, j] = np.where(use, rs, 0.0), np.where(use, rc, 0.0), np.where(use, rc * nmean, 0.0)
        else:
            use = (rc > 0) & (n_null >= min_null)
            A[:, j], B[:, j], C[:, j] = np.where(use, rs, 0.0), np.where(use, rc, 0.0), np.where(use, 0.5 * rc, 0.0)
    used = np.flatnonzero(B.sum(axis=1) > 0)                                      # dates that carry weight: the bootstrap units
    if used.size < 2:
        return pd.DataFrame()
    A, B, C, n_null = A[used], B[used], C[used], n_null[used]
    with np.errstate(all="ignore"):
        real_mean, null_mean = _finite_or_nan(A.sum(0) / B.sum(0)), _finite_or_nan(C.sum(0) / B.sum(0))
        W = rng.multinomial(used.size, np.full(used.size, 1.0 / used.size), size=int(boot)).astype("float64")   # (boot, dates)
        delta_b = _finite_or_nan((W @ A - W @ C) / (W @ B))
        lo, hi = np.nanpercentile(delta_b, 2.5, axis=0), np.nanpercentile(delta_b, 97.5, axis=0)
        p = np.minimum(1.0, 2.0 * np.minimum(np.nanmean(delta_b <= 0, axis=0), np.nanmean(delta_b >= 0, axis=0)))
    idx = names + ["pctile"]
    out = pd.DataFrame({"real": real_mean, "null": null_mean, "delta": real_mean - null_mean, "lo": lo, "hi": hi, "p": p}, index=idx)
    out.attrs.update({"n_real": int(B[:, names.index("rc")].sum()) if "rc" in names else int(B[:, 0].sum()),
                      "n_null": int(n_null.sum()), "dates": int(used.size)})
    return out


def null_by_date_frame(real_m, real_dc, null_m, null_dc, all_dates, regime_by_date=None):
    """Per-date baseline for the tracking file: real episodes vs null draws of that session (counts, win %, mean Rc, mean ret 20d)."""
    n = len(all_dates)
    def agg(m, dc):
        cnt = np.bincount(dc, minlength=n).astype(float)
        out = {"n": cnt}
        for k in ("win", "rc", "ret20"):
            x = m[k].to_numpy(dtype="float64")
            f = np.isfinite(x)
            s = np.bincount(dc[f], weights=x[f], minlength=n)
            c = np.bincount(dc[f], minlength=n).astype(float)
            with np.errstate(all="ignore"):
                out[k] = _finite_or_nan(s / c)
        x = m["rc"].to_numpy(dtype="float64")
        f = np.isfinite(x)
        c = np.bincount(dc[f], minlength=n).astype(float)
        s1, s2 = np.bincount(dc[f], weights=x[f], minlength=n), np.bincount(dc[f], weights=x[f] ** 2, minlength=n)
        with np.errstate(all="ignore"):
            out["sd_rc"] = _finite_or_nan(np.sqrt(np.maximum(s2 / c - (s1 / c) ** 2, 0.0) * c / np.maximum(c - 1.0, 1.0)))
        return out
    r, q = agg(real_m, real_dc), agg(null_m, null_dc)
    frame = pd.DataFrame({"signal_date": all_dates, "regime": [regime_by_date.get(d) if regime_by_date else None for d in all_dates],
                          "n_real": r["n"].astype(int), "real_win_pct": r["win"], "real_mean_rc": r["rc"], "real_mean_ret20_pct": r["ret20"],
                          "n_null": q["n"].astype(int), "null_win_pct": q["win"], "null_mean_rc": q["rc"], "null_sd_rc": q["sd_rc"],
                          "null_mean_ret20_pct": q["ret20"]})
    return frame[(frame["n_real"] > 0) | (frame["n_null"] > 0)].reset_index(drop=True)


def _fmt_delta(row, key, nd=2, signed=True):
    f = f"{{:{'+' if signed else ''}.{nd}f}}"
    try:
        return f"{f.format(row.loc[key, 'delta'])} [{f.format(row.loc[key, 'lo'])},{f.format(row.loc[key, 'hi'])}]"
    except Exception:
        return "n/a"


def null_comparison_table(ep, null, boot=2000, seed=1, oos_date=OOS_SPLIT_DATE, min_real=None):
    """Real ENTRY_READY episodes (ready_episodes_frame) vs the date-matched null. Returns (printable_main, printable_mechanics, numeric_long, by_date).
    Subsets: all READY, READY & EMA28-ok (what the live scanner could send), each market regime, in-sample / out-of-sample."""
    min_real = NULL_MIN_REAL_EPISODES if min_real is None else min_real
    if ep is None or ep.empty or null is None or null.empty or "entry_ready" not in ep.columns:
        return None
    real = ep[_as_bool(ep["entry_ready"])].copy()
    real["signal_date"] = real["signal_date"].astype(str)
    rm = _outcome_metrics(real)
    nm = _outcome_metrics(null)
    if rm.empty or nm.empty:
        return None
    real_dates = real.loc[rm.index, "signal_date"].to_numpy()
    null_dates = null.loc[nm.index, "signal_date"].astype(str).to_numpy()
    all_dates = np.union1d(real_dates, null_dates)
    real_dc, null_dc = np.searchsorted(all_dates, real_dates), np.searchsorted(all_dates, null_dates)
    rng = np.random.default_rng(int(seed) + 101)
    subsets = [("ENTRY_READY (all)", np.ones(len(rm), dtype=bool))]
    if "ema28_ok" in real.columns:
        subsets.append(("READY & EMA28-ok", _as_bool(real.loc[rm.index, "ema28_ok"]).to_numpy()))
    if "regime" in real.columns:
        reg = real.loc[rm.index, "regime"].astype(str).to_numpy()
        for name in sorted(set(reg)):
            subsets.append((f"regime={name}", reg == name))
    subsets.append((f"IS  (< {oos_date})", real_dates < str(oos_date)))
    subsets.append((f"OOS (>= {oos_date})", real_dates >= str(oos_date)))
    main_rows, mech_rows, long_rows = [], [], []
    for label, mask in subsets:
        if int(mask.sum()) < min_real:
            continue
        stats = _null_vs_real(rm[mask], real_dc[mask], nm, null_dc, len(all_dates), boot, rng)
        if stats.empty:
            continue
        a = stats.attrs
        def cell(key, nd, fmt="{:.%df}"):
            try:
                return (fmt % nd).format(stats.loc[key, "real"]) + " / " + (fmt % nd).format(stats.loc[key, "null"])
            except Exception:
                return "n/a"
        main_rows.append({"subset": label, "dates": a["dates"], "n_real": a["n_real"], "n_null": a["n_null"],
                          "win% real/null": cell("win", 1), "d_win_pp [95% CI]": _fmt_delta(stats, "win", 1),
                          "Rc real/null": cell("rc", 3), "d_Rc [95% CI]": _fmt_delta(stats, "rc", 3),
                          "p(Rc)": f"{stats.loc['rc', 'p']:.3f}" if "rc" in stats.index else "n/a",
                          "ret20% real/null": cell("ret20", 2), "d_ret20 [95% CI]": _fmt_delta(stats, "ret20", 2),
                          "mean pctile in null [95% CI]": (f"{0.5 + stats.loc['pctile', 'delta']:.3f} "
                                                           f"[{0.5 + stats.loc['pctile', 'lo']:.3f},{0.5 + stats.loc['pctile', 'hi']:.3f}]")
                          if "pctile" in stats.index else "n/a"})
        mech_rows.append({"subset": label, "n_real": a["n_real"], "stop% r/n": cell("stop_hit", 1), "target% r/n": cell("target_hit", 1),
                          "ambig% r/n": cell("ambig", 1), "MAE% r/n": cell("mae20", 2), "MFE% r/n": cell("mfe20", 2)})
        for key, row in stats.iterrows():
            long_rows.append({"subset": label, "metric": key, "real": row["real"], "null": row["null"], "delta": row["delta"],
                              "ci_lo": row["lo"], "ci_hi": row["hi"], "p_two_sided": row["p"], "n_real": a["n_real"],
                              "n_null": a["n_null"], "dates": a["dates"]})
    if not main_rows:
        return None
    regime_by_date = None
    if "regime" in real.columns:
        regime_by_date = real.groupby("signal_date")["regime"].first().to_dict()
    by_date = null_by_date_frame(rm, real_dc, nm, null_dc, all_dates, regime_by_date)
    return pd.DataFrame(main_rows), pd.DataFrame(mech_rows), pd.DataFrame(long_rows), by_date


def print_null_comparison(parts, n_samples, boot, null_all):
    """The consolidated 'Null Model Contrast Table' block (printed LAST in the log, between two marker lines). ASCII only."""
    main, mech = parts[0], parts[1]
    bar = "=" * 100
    print("\n" + bar)
    print("=== Null Model Contrast Table (research only: real ENTRY_READY episodes vs random liquid tickers, same days, same bracket) ===")
    print(bar)
    print(f"Null: {n_samples} random liquid tickers per signal date ({len(null_all)} draws in total); geometry = the real stop / target as % of the "
          f"signal-bar close; fill = next open x (1+slip); static bracket, 20 sessions.")
    print(f"Date-matched: the null mean weights each date by its number of real episodes; intervals = date-cluster bootstrap ({boot} draws), "
          f"d_* = real - null.")
    print("Rc = R clipped to [-3, 5]. 'mean pctile in null' = average position of the real outcome inside its own day's null (0.5 = no edge).")
    print(main[["subset", "dates", "n_real", "n_null", "win% real/null", "d_win_pp [95% CI]", "Rc real/null", "d_Rc [95% CI]",
                "p(Rc)"]].to_string(index=False))
    print("\n20d return and position inside the null:")
    print(main[["subset", "ret20% real/null", "d_ret20 [95% CI]", "mean pctile in null [95% CI]"]].to_string(index=False))
    print("\nBracket mechanics (real / null):")
    print(mech.to_string(index=False))
    sk = null_all["evaluation_status"].astype(str).value_counts(normalize=True) * 100.0 if "evaluation_status" in null_all else None
    if sk is not None:
        print("\nNull draw status %: " + " | ".join(f"{k} {v:.1f}" for k, v in sk.items()))
    print("Read: an interval that contains 0 means this harness cannot tell the pattern's outcome from random liquid tickers with the "
          "same geometry on the same days.")
    print(f"Multiple comparisons: only 'ENTRY_READY (all)' is one pre-specified comparison; the other {len(main) - 1} rows are descriptive "
          f"subsets of it, and every extra row gives a 1-in-20 chance of an interval that excludes 0 by luck alone.")
    print("Caveats: current-universe survivorship; static bracket (no trailing exit); the null is NOT conditioned on trend or pattern.")
    print(bar + "\n=== end of Null Model Contrast Table ===\n")


def run_null_track(res, frames, n_samples, seed, out_path, boot, min_dv, min_price, lookback, slippage_bps, oos_date):
    """Builds the null track from the finished run, writes the draws / per-date baseline / table CSVs and prints the Null Comparison
    Table. NEVER raises: a failure prints a WARNING and leaves the main results CSV (already written) untouched."""
    try:
        if res is None or res.empty or not frames or "entry_ready" not in res.columns:
            print("\nNull model: skipped (no candidate rows or no price history kept).")
            return None
        ready = res[_as_bool(res["entry_ready"])]
        if ready.empty:
            print("\nNull model: skipped (no ENTRY_READY row in this run).")
            return None
        panel = build_price_panel(frames)
        dates = np.sort(ready["signal_date"].astype(str).unique())
        geometry = real_geometry_frame(ready, panel)
        exclude = ready[["signal_date", "ticker"]]
        null = _simulate_null_baseline(panel, dates, n_samples=n_samples, geometry=geometry, exclude=exclude, seed=seed,
                                             slippage_bps=slippage_bps, min_dollar_vol=min_dv, min_price=min_price,
                                             liq_lookback=lookback)
        null.to_csv(out_path, index=False)
        stem, _ = os.path.splitext(out_path)
        print(f"\nNull model: {len(null)} draws on {null['signal_date'].nunique() if len(null) else 0} signal dates -> {out_path}")
        parts = null_comparison_table(ready_episodes_frame(res), null, boot=boot, seed=seed, oos_date=oos_date)
        if parts is None:
            print("Null Model Contrast Table: not enough complete real episodes / null draws to compare.")
            return null
        parts[3].to_csv(stem + "_by_date.csv", index=False)
        parts[2].to_csv(stem + "_table.csv", index=False)
        print(f"Null baseline per date -> {stem}_by_date.csv | comparison (numeric) -> {stem}_table.csv")
        print_null_comparison(parts, n_samples, boot, null)
        return null
    except Exception as exc:
        print(f"\nWARNING: null model failed and was skipped ({type(exc).__name__}: {exc}). The results CSV is unaffected.")
        return None


# ---------------------------------------------------------------- v3.6: cross-sectional rank tables
def _boot_corr(xr, yr, dc, n_dates, boot, rng, levels=((2.5, 97.5),)):
    """Pearson correlation of two (rank) vectors + date-cluster bootstrap percentile intervals for each (lo, hi) level."""
    f = np.isfinite(xr) & np.isfinite(yr)
    xr, yr, dc = xr[f], yr[f], dc[f]
    if len(xr) < 20:
        return np.nan, [(np.nan, np.nan)] * len(levels)
    S = np.column_stack([np.bincount(dc, minlength=n_dates).astype(float), np.bincount(dc, weights=xr, minlength=n_dates),
                         np.bincount(dc, weights=yr, minlength=n_dates), np.bincount(dc, weights=xr * yr, minlength=n_dates),
                         np.bincount(dc, weights=xr * xr, minlength=n_dates), np.bincount(dc, weights=yr * yr, minlength=n_dates)])
    def corr(t):
        n, sx, sy, sxy, sxx, syy = (t[..., i] for i in range(6))
        with np.errstate(all="ignore"):
            return _finite_or_nan((n * sxy - sx * sy) / np.sqrt((n * sxx - sx * sx) * (n * syy - sy * sy)))
    point = float(corr(S.sum(axis=0)))
    W = rng.multinomial(n_dates, np.full(n_dates, 1.0 / n_dates), size=int(boot)).astype("float64")
    cb = corr(W @ S)
    cb = cb[np.isfinite(cb)]
    if cb.size < 50:
        return point, [(np.nan, np.nan)] * len(levels)
    return point, [(float(np.percentile(cb, lo)), float(np.percentile(cb, hi))) for lo, hi in levels]


def cs_rank_tables(ep, r20, ready_ep=None, boot=1000):
    """v3.6: do the day-T percentile ranks carry information the absolute values did not? ep = the summary's episodes (first candidate
    day of a ticker+pattern), ready_ep = ready_episodes_frame(). Pooled Spearman IC with the 20d R, with a
    date-cluster bootstrap interval (95% and Bonferroni-adjusted for the number of tests shown), plus top / bottom quintile outcomes.
    Printed only for columns that exist; never raises."""
    try:
        cols = [c for c in CS_RANK_COLUMNS if c in ep.columns and pd.to_numeric(ep[c], errors="coerce").notna().sum() >= 50]
        if not cols or r20 not in ep.columns:
            return
        d = ep.copy()
        d[r20] = pd.to_numeric(d[r20], errors="coerce")
        d = d[np.isfinite(d[r20])]
        sets = [("all episodes", d)]
        if ready_ep is not None and len(ready_ep) and r20 in ready_ep.columns:
            rd = ready_ep.copy()
            rd[r20] = pd.to_numeric(rd[r20], errors="coerce")
            sets.append(("ENTRY_READY episodes", rd[np.isfinite(rd[r20])]))
        tests = sum(1 for _, s in sets if len(s) >= 50) * len(cols)
        if tests == 0:
            return
        alpha = 5.0 / tests
        levels = ((2.5, 97.5), (alpha / 2.0, 100.0 - alpha / 2.0))
        rng = np.random.default_rng(7)
        rows = []
        for label, s in sets:
            if len(s) < 50:
                continue
            dc = pd.factorize(s["signal_date"].astype(str))[0]
            nd = int(dc.max()) + 1
            y = s[r20].rank(method="average").to_numpy(dtype="float64")
            rc = s[r20].clip(RC_LO, RC_HI)
            for col in cols:
                raw_name = dict(CS_RANK_SOURCES)[col]
                raw_col = next((c for c in raw_name if c in s.columns), None)
                x = pd.to_numeric(s[col], errors="coerce")
                ic, (ci95, cib) = _boot_corr(x.rank(method="average").to_numpy(dtype="float64"), y, dc, nd, boot, rng, levels)
                ic_raw = np.nan
                if raw_col is not None:
                    xr = pd.to_numeric(s[raw_col], errors="coerce").rank(method="average").to_numpy(dtype="float64")
                    f = np.isfinite(xr)
                    ic_raw = float(np.corrcoef(xr[f], y[f])[0, 1]) if f.sum() > 20 else np.nan
                top, bot = x >= 0.8, x <= 0.2
                rows.append({"set": label, "feature": col.replace("cs_rank_", ""), "n": int(x.notna().sum()),
                             "IC raw": round(ic_raw, 3), "IC cs-rank": round(ic, 3), "95% CI": f"[{ci95[0]:+.3f},{ci95[1]:+.3f}]",
                             "Bonf. CI": f"[{cib[0]:+.3f},{cib[1]:+.3f}]",
                             "top20% n/win%/Rc": f"{int(top.sum())}/{100 * (s.loc[top, r20] > 0).mean():.1f}/{rc[top].mean():+.3f}" if top.any() else "n/a",
                             "bot20% n/win%/Rc": f"{int(bot.sum())}/{100 * (s.loc[bot, r20] > 0).mean():.1f}/{rc[bot].mean():+.3f}" if bot.any() else "n/a"})
        if rows:
            print("\nCross-sectional ranks (v3.6): day-T percentile of each candidate among the candidates of the SAME session (0 = lowest, 1 = highest;\n"
                  "each ticker counts once per day). IC = pooled Spearman with the 20d R (positive = higher rank, better outcome); 'IC raw' uses the\n"
                  f"absolute value instead. Intervals: date-cluster bootstrap; 'Bonf.' is adjusted for the {tests} tests shown.")
            print(pd.DataFrame(rows).to_string(index=False))
    except Exception as exc:
        print(f"cross-sectional rank tables skipped ({type(exc).__name__}: {exc})")


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
    ap.add_argument("--no-cs-rank", action="store_true",
                    help="v3.6: do not append cs_n_day / cs_rank_* (the CSV is then identical to v3.5)")
    ap.add_argument("--no-null", action="store_true", help="v3.6: skip the counterfactual null model (no null files, no extra memory)")
    ap.add_argument("--null-samples", type=int, default=NULL_DEFAULT_SAMPLES,
                    help="v3.6: random liquid tickers drawn per signal date (default 100)")
    ap.add_argument("--null-out", default="", help="v3.6: draws CSV (default <out>_null.csv; <stem>_by_date.csv and <stem>_table.csv go next to it)")
    ap.add_argument("--null-boot", type=int, default=2000, help="v3.6: date-cluster bootstrap draws for the intervals (default 2000)")
    ap.add_argument("--null-min-dv", type=float, default=None,
                    help="v3.6: liquidity floor of the null draws, median dollar volume (default: the scanner's LIQ_MEDIAN_MIN_DOLLAR_VOL)")
    ap.add_argument("--oos-date", default=OOS_SPLIT_DATE, help="v3.6: in-sample / out-of-sample split of the null comparison (default 2025-07-01)")
    args = ap.parse_args()
    if not 1 <= args.null_samples <= 5000:
        ap.error("--null-samples must be between 1 and 5000")
    if not 200 <= args.null_boot <= 100000:
        ap.error("--null-boot must be between 200 and 100000")
    if args.null_min_dv is not None and (not _is_finite_number(args.null_min_dv) or args.null_min_dv < 0):
        ap.error("--null-min-dv must be a finite number >= 0")
    try:
        pd.Timestamp(args.oos_date)
    except Exception:
        ap.error("--oos-date must be a date such as 2025-07-01")
    null_on = not args.no_null
    null_out = args.null_out or (os.path.splitext(args.out)[0] + "_null.csv")
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
    print(f"v3.6: cross-sectional ranks {'off' if args.no_cs_rank else 'on'} | null model "
          f"{'off' if not null_on else str(args.null_samples) + ' draws per signal date, seed ' + str(args.seed) + ', draws -> ' + null_out}")

    rows = []
    frames = {}
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
        if null_on:
            frames[sym] = df                      # kept (references only) for the null-model price panel
        print(f"[{k}/{len(symbols)}] {sym}: {len(got)} candidate-days in {time.time() - t0:.0f}s")
    res = pd.DataFrame(rows)
    if not res.empty:
        finalize_grid_columns(res)
        if not args.no_cs_rank:
            res = add_cross_sectional_ranks(res)  # v3.6: appended at the END of the frame - legacy columns untouched
    res.to_csv(args.out, index=False)
    if paths is not None:
        pd.DataFrame(paths).to_csv(args.paths_out, index=False)
        print(f"Saved {len(paths)} forward-path rows -> {args.paths_out}")
    print(f"\nSaved {len(res)} rows -> {args.out} | scanner {getattr(m, 'CODE_VERSION', '?')} | total {time.time() - run_t0:.0f}s")
    summarize(res)
    learn_report(res, m)                      # never raises (v3.4): without scikit-learn it prints a WARNING and a descriptive fallback
    if null_on:                               # v3.6: never raises; prints the Null Model Contrast Table as the LAST block of the log
        run_null_track(res, frames, args.null_samples, args.seed, null_out, args.null_boot,
                       args.null_min_dv if args.null_min_dv is not None else float(getattr(m, "LIQ_MEDIAN_MIN_DOLLAR_VOL", NULL_DEFAULT_MIN_DV)),
                       float(getattr(m, "LIQ_MIN_PRICE", NULL_DEFAULT_MIN_PRICE)), int(getattr(m, "LIQ_LOOKBACK", NULL_LIQ_LOOKBACK)),
                       float(getattr(m, "RESEARCH_SLIPPAGE_BPS", NULL_SLIPPAGE_BPS)), args.oos_date)


if __name__ == "__main__":
    main()
