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

Usage (in your own environment, where yfinance is installed):
  python scanner_backtest.py --scanner stock_scanner_unified.py \
      --tickers AAPL,MSFT,NVDA --start 2023-01-01 --end 2026-09-30 --out bt.csv
  or --tickers-file tickers.txt (one symbol per line)

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
def scan_history(m, symbol, df, spy, pit, start, end, horizons=(5, 10, 20), min_bars=260, window_bars=320, paths=None):
    cfg = m._make_v9_pattern_config()
    rows = []
    dates = df.index
    feats = compute_features(df, spy)
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
        rs_legacy = rs_swing = rs_decay_pts = None
        if hasattr(m, "RS_MODE"):                         # v9.7.5+: score the same bar both ways (gate keeps running mode)
            keep_mode = m.RS_MODE
            try:
                m.RS_MODE = "legacy"
                rs_legacy = m.compute_rs_score(symbol, hist).get("rs_score")
                m.RS_MODE = "swing"
                sw = m.compute_rs_score(symbol, hist)
                rs_swing, rs_decay_pts = sw.get("rs_score"), sw.get("rs_decay_1m_vs_3m")
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
            row.update({"rs_legacy": rs_legacy, "rs_swing": rs_swing, "rs_swing_decay_pts": rs_decay_pts})
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
        single = ep[ep["n_blockers"] == 1].copy()
        if not single.empty:
            single["only_blocker"] = single["blockers"].str.replace(r"\s*\(.*?\)", "", regex=True)
            print("\nWhen a gate was the ONLY blocker:\n", table_single(single, r20).to_string())


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
    args = ap.parse_args()
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

    rows = []
    paths = [] if args.paths_out else None
    for k, sym in enumerate(dict.fromkeys(symbols), 1):
        t0 = time.time()
        df = download(sym, hist_start, hist_end, yf)
        if df is None or len(df) < 300:
            print(f"[{k}/{len(symbols)}] {sym}: not enough data")
            continue
        got = scan_history(m, sym, df, spy, pit, args.start, args.end, window_bars=args.window_bars, paths=paths)
        rows.extend(got)
        print(f"[{k}/{len(symbols)}] {sym}: {len(got)} candidate-days in {time.time() - t0:.0f}s")
    res = pd.DataFrame(rows)
    res.to_csv(args.out, index=False)
    if paths is not None:
        pd.DataFrame(paths).to_csv(args.paths_out, index=False)
        print(f"Saved {len(paths)} forward-path rows -> {args.paths_out}")
    print(f"\nSaved {len(res)} rows -> {args.out} | scanner {getattr(m, 'CODE_VERSION', '?')} | total {time.time() - run_t0:.0f}s")
    summarize(res)


if __name__ == "__main__":
    main()
