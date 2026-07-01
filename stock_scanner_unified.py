# -*- coding: utf-8 -*-
"""
stock_scanner_unified.py
========================
גרסה מאוחדת של stock_scanner_fixed.py + stock_scanner_final.py

זרימה:
  Phase 1 — Cup & Handle / Ascending Triangle / Double Bottom
  Phase 2 (מקובץ 2) — Falling Wedge breakout
  בסיום — שולח מייל HTML עם עד 3 הסטאפים הטובים ביום

דפוסים מזוהים:
  • Cup & Handle          — גביע וידית
  • Bullish Triangle      — משולש עולה / אופקי
  • EMA28 Breakout/Touch  — חציית / נגיעה ב-EMA28
  • Double Bottom         — תבנית W (היפוך שורי)
  • Falling Wedge (P2)    — שני קווים יורדים מתכנסים, פריצה למעלה

ציון (0–10):
  כל פטרן עובר את אותה compute_setup_score.
  Double Bottom מקבל בונוס של +1.0 (ועוד +0.3 אם עומק >8%, +0.4 אם >20 נרות בין השפלים).
"""

# ============================================================
#  Imports
# ============================================================
import os
import json
import time
import random
import traceback
import smtplib
from datetime import datetime, timedelta
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.signal import argrelextrema
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

try:
    import plotly.graph_objects as go
    import plotly.io as pio
    PLOTLY_AVAILABLE = True
except Exception:
    go = pio = None
    PLOTLY_AVAILABLE = False

# ============================================================
#  CONFIG  — ערכים ברירת-מחדל, הכל אפשר לדרוס דרך env-vars
# ============================================================

# --- אבטחה: סיסמה + מפתחות API ----
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if APP_PASSWORD == "YOUR_APP_PASSWORD_HERE" or not APP_PASSWORD:
    print(
        "Warning: APP_PASSWORD is still set to the default value or is empty. Email sending disabled."
    )
    APP_PASSWORD = os.getenv("APP_PASSWORD", "")
TO_EMAILS = [
    email.strip()
    for email in os.getenv("TO_EMAILS", "").split(",")
    if email.strip()
]
TO_EMAILS = list(dict.fromkeys(TO_EMAILS))

if not TO_EMAILS:
    raise RuntimeError("TO_EMAILS environment variable is missing")
CHARTS_DIR = os.getenv("CHARTS_DIR", "temp_images")
os.makedirs(CHARTS_DIR, exist_ok=True)
LOGFILE = os.getenv("LOGFILE", "stock_scanner_unified_log.txt")

API_KEYS = [
    key.strip()
    for key in os.getenv("TWELVEDATA_API_KEYS", "").split(",")
    if key.strip()
]

if not API_KEYS:
    raise RuntimeError("TWELVEDATA_API_KEYS environment variable is missing")
# --- TwelveData ---
BASE_URL        = "https://api.twelvedata.com/time_series"
TICKERS_PER_KEY = int(os.getenv("TICKERS_PER_KEY", "735"))
MAX_REQUESTS    = int(os.getenv("MAX_REQUESTS", "735"))
SCAN_WORKERS    = int(os.getenv("SCAN_WORKERS", "8"))      # threads במקביל — מוגבל לפי TwelveData rate limit
RESET_TIME      = timedelta(hours=int(os.getenv("RESET_HOURS", "12")))

# --- פריצות ודפוסים ---
# tolerance אחיד לכל הפריצות: 0–0.5% מעל הקו בלבד
BREAKOUT_TOLERANCE    = float(os.getenv("BREAKOUT_TOLERANCE",    "0.005"))  # 0.5%
MAX_OVERBREAK_PCT     = float(os.getenv("MAX_OVERBREAK_PCT",     "0.005"))  # מעל 0.5% = כבר עבר
USE_HIGH_FOR_BREAKOUT = os.getenv("USE_HIGH_FOR_BREAKOUT", "False").lower() in ("1","true","yes")
ALERT_DEDUP_LEVEL_PCT = float(os.getenv("ALERT_DEDUP_LEVEL_PCT", "0.015"))

# --- EMA28 (תנאי גלובלי) ---
# הסגירה חייבת להיות לא יותר מ-3% מעל EMA28
EMA28_MAX_DIST_PCT    = float(os.getenv("EMA28_MAX_DIST_PCT", "0.03"))   # 3%
# EMA28 חייב בשיפוע עולה (הערך היום > אתמול)
EMA28_REQUIRE_RISING  = os.getenv("EMA28_REQUIRE_RISING", "True").lower() in ("1","true","yes")

# --- MA150 ---
MA150_MAX_DISTANCE    = float(os.getenv("MA150_MAX_DISTANCE", "0.05"))   # 5% — מרחק מקסימלי מ-MA150
MA150_MAX_DISTANCE_DB = float(os.getenv("MA150_MAX_DISTANCE_DB", "0.15")) # 15% — Double Bottom בלבד
MIN_ALERT_SCORE       = float(os.getenv("MIN_ALERT_SCORE",    "45.0"))  # ברירת מחדל — יוחלף דינמית לפי Regime

# ============================================================
#  MARKET REVERSAL DETECTOR
#  בודק 4 סיגנלים לזיהוי היפוך שוק ושליחת התראת SPY
# ============================================================

# נתונים היסטוריים קבועים להצגה במייל
_HISTORICAL_FACTS = [
    "ב-87% מהפעמים ש-VIX עלה מעל 30, SPY עלה ב-30 הימים הבאים בממוצע 8%",
    "ב-2020 (קורונה): קנייה כשה-VIX הגיע ל-80 הניבה +65% תוך שנה",
    "ב-2022: קנייה כש-S5FI ירד מתחת ל-20% הניבה +24% תוך 6 חודשים",
    "ממוצע תשואת SPY אחרי 3 ימים אדומים ברצף: +2.3% בשבוע שאחרי",
    "ב-2018 (ירידות דצמבר): קנייה ב-Fear & Greed מתחת ל-10 הניבה +30% תוך 6 חודשים",
    "מאז 1950, S&P500 עלה ב-70% מהשנים שבאו אחרי ירידה של 20%+",
    "Warren Buffett: 'Be greedy when others are fearful' — הזמנים הכי טובים לקנות הם כשכולם מפחדים",
    "ב-כל אחת מ-12 הפעמים ש-VIX עלה מעל 40 מאז 1990 — SPY היה גבוה יותר שנה אחר כך",
]


def _check_vix() -> tuple[bool, float, str]:
    """בודק אם VIX מעל 30. מחזיר (triggered, value, description)."""
    try:
        df = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return False, 0.0, "VIX לא זמין"
        val = float(df["Close"].iloc[-1])
        triggered = val >= 30
        emoji = "✅" if triggered else "❌"
        return triggered, val, f"{emoji} VIX: {val:.1f} ({'מעל' if triggered else 'מתחת ל'}-30)"
    except Exception as e:
        return False, 0.0, f"❌ VIX שגיאה: {e}"


def _check_fear_greed() -> tuple[bool, float, str]:
    """בודק Fear & Greed Index מתחת ל-10 — עם מספר URLs כ-fallback."""
    urls = [
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        "https://fear-and-greed-index.p.rapidapi.com/v1/fgi",
        "https://api.alternative.me/fng/",
    ]
    for url in urls:
        try:
            r = HTTP_SESSION.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            data = r.json()
            # CNN format
            if "fear_and_greed" in data:
                val = float(data["fear_and_greed"]["score"])
                rating = data["fear_and_greed"].get("rating", "")
                triggered = val <= 10
                emoji = "✅" if triggered else "❌"
                return triggered, val, f"{emoji} Fear & Greed: {val:.0f}/100 ({rating})"
            # alternative.me format
            if "data" in data and len(data["data"]) > 0:
                val = float(data["data"][0]["value"])
                rating = data["data"][0].get("value_classification", "")
                triggered = val <= 10
                emoji = "✅" if triggered else "❌"
                return triggered, val, f"{emoji} Fear & Greed: {val:.0f}/100 ({rating})"
        except Exception:
            continue
    return False, 0.0, "❌ Fear & Greed לא זמין"


def _check_s5fi() -> tuple[bool, float, str]:
    """
    בודק רוחב שוק — SPY ביחס ל-MA200.
    מחליף את S5FI שאינו זמין ב-yfinance.
    אם SPY נמצא יותר מ-10% מתחת ל-MA200 — שוק במצוקה קיצונית.
    """
    try:
        df = yf.download("SPY", period="300d", progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 200:
            return False, 0.0, "❌ SPY MA200 לא זמין"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        price  = float(df["close"].iloc[-1])
        ma200  = float(df["close"].rolling(200).mean().iloc[-1])
        dist   = (price - ma200) / ma200 * 100
        triggered = dist <= -10.0  # מתחת ל-10% מ-MA200
        emoji = "✅" if triggered else "❌"
        return triggered, dist, f"{emoji} SPY vs MA200: {dist:+.1f}% ({'מצוקה קיצונית' if triggered else 'תקין'})"
    except Exception as e:
        return False, 0.0, f"❌ SPY MA200 שגיאה: {e}"


def _check_three_red_days() -> tuple[bool, int, str]:
    """בודק 3 ימים אדומים ברצף ב-SPY."""
    try:
        df = yf.download("SPY", period="10d", progress=False, auto_adjust=True)
        if df is None or len(df) < 4:
            return False, 0, "❌ SPY לא זמין"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        closes = [float(v) for v in df["close"].iloc[-4:].values]
        red_days = 0
        for i in range(1, 4):
            if closes[i] < closes[i-1]:
                red_days += 1
            else:
                break
        triggered = red_days >= 3
        emoji = "✅" if triggered else "❌"
        pct = (closes[-1] - closes[-4]) / closes[-4] * 100
        return triggered, red_days, f"{emoji} SPY: {red_days} ימים אדומים ברצף ({pct:.1f}% ירידה)"
    except Exception as e:
        return False, 0, f"❌ SPY שגיאה: {e}"
def _check_reversal_signals() -> tuple[bool, float, str]:
    """
    בודק אם השוק הולך להתהפך — SPY חוצה מעל EMA28 אחרי BEAR.
    מחזיר (reversing, spy_dist_pct, description).
    """
    try:
        df = yf.download("SPY", period="60d", progress=False, auto_adjust=True)
        if df is None or len(df) < 30:
            return False, 0.0, "SPY לא זמין"
        closes = df["Close"]
        ema28 = closes.ewm(span=28, adjust=False).mean()
        price = float(closes.iloc[-1])
        ema  = float(ema28.iloc[-1])
        dist = (price - ema) / ema * 100
        # בדוק אם SPY חצה מעל EMA28 לאחרונה (היום מעל, אתמול מתחת)
        prev_price = float(closes.iloc[-2])
        prev_ema   = float(ema28.iloc[-2])
        crossed_above = prev_price < prev_ema and price > ema
        vix_trig, vix_val, _ = _check_vix()
        vix_dropping = vix_val < 25 and not vix_trig
        reversing = crossed_above and vix_dropping
        return reversing, dist, f"SPY {dist:+.1f}% מ-EMA28 | {'חצה מעל!' if crossed_above else 'עדיין מתחת'}"
    except Exception as e:
        return False, 0.0, f"שגיאה: {e}"


def _send_reversal_email(signals: list[tuple], score: int,
                         reversal_detected: bool = False) -> None:
    """שולח מייל התראה על סיגנלים לקנייה או היפוך שוק."""
    try:
        today = datetime.now().strftime("%d/%m/%Y")
        import random
        facts = random.sample(_HISTORICAL_FACTS, min(4, len(_HISTORICAL_FACTS)))

        if reversal_detected:
            subject = f"🔄 שינוי כיוון שוק מתחיל — {today}"
            headline = "🔄 זוהה שינוי כיוון — השוק עשוי לחזור לעלות"
            headline_color = "#1d4ed8"
            action = "SPY חצה מעל EMA28 — שקול לחזור לסריקה אקטיבית"
        else:
            subject = f"🚨 התראת קנייה SPY — {score}/4 סיגנלים — {today}"
            headline = f"🚨 {score} מתוך 4 סיגנלי פחד קיצוני הופעלו"
            headline_color = "#dc2626"
            action = "שקול לקנות SPY כמה דקות לפני סגירת המסחר (15:50-16:00 ET)"

        signals_html = "".join([
            f'<tr><td style="padding:8px 12px;font-size:15px;">{s}</td></tr>'
            for _, _, s in signals
        ])

        facts_html = "".join([
            f'<li style="margin:8px 0;color:#1e40af;font-size:14px;">📈 {f}</li>'
            for f in facts
        ])

        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f8fafc;padding:20px;">
        <div style="max-width:600px;margin:auto;background:white;border-radius:12px;
                    box-shadow:0 4px 20px rgba(0,0,0,0.1);overflow:hidden;">

          <!-- כותרת -->
          <div style="background:{headline_color};padding:24px;text-align:center;">
            <h1 style="color:white;margin:0;font-size:24px;">{headline}</h1>
            <p style="color:rgba(255,255,255,0.9);margin:8px 0 0;">{today}</p>
          </div>

          <!-- סיגנלים -->
          <div style="padding:20px;">
            <h2 style="color:#374151;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">
              📊 סיגנלים שזוהו
            </h2>
            <table style="width:100%;border-collapse:collapse;">
              {signals_html}
            </table>
          </div>

          <!-- המלצת פעולה -->
          <div style="background:#fef3c7;border:2px solid #f59e0b;
                      border-radius:8px;margin:0 20px;padding:16px;">
            <h3 style="color:#92400e;margin:0 0 8px;">💡 המלצת פעולה</h3>
            <p style="color:#78350f;margin:0;font-size:15px;">{action}</p>
          </div>

          <!-- נתונים היסטוריים -->
          <div style="padding:20px;">
            <h2 style="color:#374151;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">
              📚 למה לקנות דווקא עכשיו? — נתונים היסטוריים
            </h2>
            <ul style="padding-right:20px;list-style:none;margin:0;">
              {facts_html}
            </ul>
          </div>

          <!-- footer -->
          <div style="background:#f1f5f9;padding:16px;text-align:center;">
            <p style="color:#64748b;font-size:12px;margin:0;">
              Stock Scanner — Market Reversal Detector | {today}
            </p>
          </div>
        </div>
        </body></html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = FROM_EMAIL
        msg["To"]      = ", ".join(TO_EMAILS) if isinstance(TO_EMAILS, list) else TO_EMAILS
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(FROM_EMAIL, APP_PASSWORD)
            s.send_message(msg)
        log(f"📧 Reversal email sent: {subject}")
    except Exception as e:
        log(f"_send_reversal_email error: {e}")


def run_market_reversal_detector() -> None:
    """
    מריץ את כל 4 הסיגנלים ומחליט אם לשלוח התראה.
    נקרא בתחילת main() בכל יום.
    """
    log("=" * 60)
    log("🔍 Market Reversal Detector — בודק סיגנלי היפוך שוק...")

    # ── הרץ את 4 הסיגנלים ──────────────────────────────────
    vix_trig,  vix_val,  vix_desc  = _check_vix()
    fg_trig,   fg_val,   fg_desc   = _check_fear_greed()
    s5fi_trig, s5fi_val, s5fi_desc = _check_s5fi()
    red_trig,  red_days, red_desc  = _check_three_red_days()

    signals = [
        (vix_trig,  vix_val,  vix_desc),
        (fg_trig,   fg_val,   fg_desc),
        (s5fi_trig, s5fi_val, s5fi_desc),
        (red_trig,  red_days, red_desc),
    ]

    # ── לוג כל סיגנל ──────────────────────────────────────
    for trig, val, desc in signals:
        log(f"   {desc}")

    score = sum(1 for t, _, _ in signals if t)
    log(f"   📊 סיגנלים פעילים: {score}/4")

    # ── בדוק היפוך כיוון ───────────────────────────────────
    reversing, spy_dist, rev_desc = _check_reversal_signals()
    if reversing:
        log(f"   🔄 זוהה שינוי כיוון! {rev_desc}")

    # ── החלטה ───────────────────────────────────────────────
    if reversing:
        log("🔄 שולח התראת שינוי כיוון שוק...")
        _send_reversal_email(signals, score, reversal_detected=True)
    elif score >= 2:
        log(f"🚨 {score} סיגנלים פעילים — שולח התראת קנייה SPY!")
        _send_reversal_email(signals, score, reversal_detected=False)
    else:
        log(f"   ✅ אין התראה ({score}/4 סיגנלים) — שוק תקין")

    log("=" * 60)


def get_dynamic_min_score() -> float:
    """
    מחזיר ציון מינימלי דינמי לפי מצב השוק:
      🐂 BULL  (SPY > +2% מ-MA50) → 50
      🟡 NEUTRAL (-2% עד +2%)    → 40
      🔴 BEAR  (SPY < -2%)        → 30
    """
    try:
        regime = get_market_regime()
        r = regime.get("regime", "NEUTRAL")
        if r == "BULL":
            return 55.0
        elif r == "BEAR":
            return 40.0
        else:
            return 45.0
    except Exception:
        return MIN_ALERT_SCORE

# --- Falling Wedge ---
TRIANGLE_LOOKBACK      = int(os.getenv("TRIANGLE_LOOKBACK",      "90"))    # 3 חודשים לחיפוש הטריז
TRIANGLE_PEAK_ORDER    = int(os.getenv("TRIANGLE_PEAK_ORDER",    "3"))     # order לזיהוי פסגות/שפלים מקומיים
# הפסגה האחרונה בקו העליון לא יכולה להיות יותר מ-20 ימים אחורה — פריצה טרייה
TRIANGLE_MAX_LAST_PEAK_DAYS = int(os.getenv("TRIANGLE_MAX_LAST_PEAK_DAYS", "20"))
# סבלנות לנגיעה בקו (0.8%) — מרחק מקסימלי שפסגה נחשבת כ"נוגעת" בקו
TRIANGLE_PRICE_TOL     = float(os.getenv("TRIANGLE_PRICE_TOL",   "0.015"))  # 1.5% סבלנות נגיעה

# --- Relative Strength Score ---
# ציון מינימלי (0-100) ביחס ל-SPY — מניות מתחת לציון זה לא ישלחו התראה
# 70 = 30% עליון בשוק | 0 = כבוי
# RS_MIN_SCORE הוסר — פילטר RS בוטל

# --- Double Bottom (W) ---
DB_LOOKBACK             = int(os.getenv("DB_LOOKBACK",             "120"))   # חלון חיפוש 6 חודשים
DB_TROUGH_ORDER         = int(os.getenv("DB_TROUGH_ORDER",         "5"))     # order לזיהוי שפלים מקומיים
# שני שפלים כמעט זהים — הפרש מקסימלי 2%
DB_BOTTOM_DIFF_PCT      = float(os.getenv("DB_BOTTOM_DIFF_PCT",    "0.05"))  # 5% הפרש בין שפלים
# עומק מינימלי של ה-W מה-mid_peak לשפל
DB_MIN_DEPTH_PCT        = float(os.getenv("DB_MIN_DEPTH_PCT",      "0.05"))  # 5%
# מרחק מינימלי בין שני השפלים
DB_MIN_BARS_BETWEEN     = int(os.getenv("DB_MIN_BARS_BETWEEN",     "15"))    # 15 ימים
DB_SCORE_BONUS          = float(os.getenv("DB_SCORE_BONUS",         "1.0"))
# שפל 2 יכול להיות שווה לשפל 1 (לא חייב להיות גבוה יותר)
DB_REQUIRE_HIGHER_LOW   = False
# Volume בשפל השני חייב להיות נמוך מהראשון
DB_REQUIRE_LOWER_VOL    = os.getenv("DB_REQUIRE_LOWER_VOL", "True").lower() in ("1","true","yes")

# --- Cup & Handle ---
CH_LOOKBACK             = int(os.getenv("CH_LOOKBACK",             "200"))   # עד 200 ימים לגביע
# גביע לפחות 4 שבועות — מינימום ימים מ-left_peak לright_peak
CH_MIN_CUP_BARS         = int(os.getenv("CH_MIN_CUP_BARS",         "20"))
# קצות הכוס חייבים להיות בתוך 1% זה מזה — הגדרה חדשה
CH_PEAKS_MAX_DIFF_PCT   = float(os.getenv("CH_PEAKS_MAX_DIFF_PCT",  "0.05"))  # 5% — סבלנות בין פסגות
# ידית ירידה מינימלית 3% — חייבת לרדת קצת
CH_HANDLE_MIN_PCT       = float(os.getenv("CH_HANDLE_MIN_PCT",      "0.03"))
# ידית לפחות 5 ימים
CH_HANDLE_MIN_BARS      = int(os.getenv("CH_HANDLE_MIN_BARS",       "5"))

# --- Volume ---
REQUIRE_VOLUME_CHECK  = os.getenv("REQUIRE_VOLUME_CHECK","False").lower() in ("1","true","yes")
REQUIRE_BODY_CHECK    = os.getenv("REQUIRE_BODY_CHECK",  "False").lower() in ("1","true","yes")
VOLUME_MULTIPLIER     = float(os.getenv("VOLUME_MULTIPLIER",  "1.6"))
MIN_AVG_VOLUME        = int(os.getenv("MIN_AVG_VOLUME",       "150000"))
VOLUME_AVG_LOOKBACK   = int(os.getenv("VOLUME_AVG_LOOKBACK",  "20"))

# --- כללי ---
MIN_MARKET_CAP_USD    = float(os.getenv("MIN_MARKET_CAP_USD",  "2e9"))   # $2B
SCAN_DELAY_SECONDS    = int(os.getenv("SCAN_DELAY_SECONDS",    "10"))
ALERT_COOLDOWN_HOURS  = int(os.getenv("ALERT_COOLDOWN_HOURS",  "24"))
DEDUP_ALERT_HOURS     = ALERT_COOLDOWN_HOURS
DEBUG_SCAN_REASONS    = os.getenv("DEBUG_SCAN_REASONS","True").lower() in ("1","true","yes")
TOP_ALERTS_TO_SEND    = int(os.getenv("TOP_ALERTS_TO_SEND", "3"))

# --- קבצים ---
PROGRESS_FILE      = os.getenv("PROGRESS_FILE",      "progress.json")
ALERT_HISTORY_FILE = os.getenv("ALERT_HISTORY_FILE", "alerts_sent.json")
SIGNALS_CSV        = os.getenv("SIGNALS_CSV",         "signals_log.csv")
BLOCKLIST_FILE     = os.getenv("BLOCKLIST_FILE",      "twelvedata_blocklist.json")
CHARTS_DIR         = os.getenv("CHARTS_DIR",          "temp_images")
os.makedirs(CHARTS_DIR, exist_ok=True)

# --- מעקב ביצועים ---
PERFORMANCE_CSV    = os.getenv("PERFORMANCE_CSV",     "performance_log.csv")
PERF_CHECK_DAYS    = [5, 10, 20]   # בודק ביצועים אחרי X ימי מסחר

# --- סינון דוחות ---
EARNINGS_FILTER_DAYS = int(os.getenv("EARNINGS_FILTER_DAYS", "14"))  # skip אם דוח ב-14 ימים

# --- פילטרים חדשים: Quality Gate ---
GAP_MAX_PCT          = float(os.getenv("GAP_MAX_PCT",        "0.02"))  # 2% גאפ מקסימלי
GAP_LOOKBACK_DAYS    = int(os.getenv("GAP_LOOKBACK_DAYS",    "5"))     # בדיקת גאפים ב-5 ימים אחרונים
VOL_RISING_DAYS      = int(os.getenv("VOL_RISING_DAYS",      "3"))     # volume עולה X ימים רצופים
MA_NEAR_NECK_PCT     = float(os.getenv("MA_NEAR_NECK_PCT",   "0.03"))  # ממוצע קרוב ל-neckline ב-3%

# --- מיפוי סקטורים ל-ETF ---
SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Energy":                 "XLE",
    "Financial Services":     "XLF",
    "Financials":             "XLF",
    "Healthcare":             "XLV",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}

# LOGFILE
LOGFILE = os.getenv("LOGFILE", "stock_scanner_unified_log.txt")

# WATCHLIST LOG — מניות שעברו EMA28+MA150+דוחות אבל נפסלו בשלב אחרון
WATCHLIST_LOG = os.getenv("WATCHLIST_LOG", "watchlist_log.txt")
try:
    open(WATCHLIST_LOG, "a").close()
except Exception:
    WATCHLIST_LOG = "watchlist_log.txt"

WATCHLIST_NEAR_PCT = float(os.getenv("WATCHLIST_NEAR_PCT", "0.03"))  # 3% מתחת לפריצה

# --- רשימת טיקרים (ללא כפילויות) ---
# טוען universe — ממטמון אם קיים, מוריד מחדש אחרת
_tickers_env = os.getenv("TICKERS_LIST")
if _tickers_env:
    try:
        tickers = json.loads(_tickers_env)
    except Exception:
        tickers = [t.strip() for t in _tickers_env.split(",") if t.strip()]
else:
    # יטען ב-main() — כאן רק placeholder
    tickers = []


# ============================================================
#  UNIVERSE — טוען את כל מניות NYSE + NASDAQ ומסנן מעל $1B
# ============================================================
UNIVERSE_CACHE_FILE = os.getenv("UNIVERSE_CACHE_FILE", "universe_cache.json")
UNIVERSE_MIN_CAP    = float(os.getenv("UNIVERSE_MIN_CAP", "1e9"))   # $1B מינימום
UNIVERSE_MIN_PRICE  = float(os.getenv("UNIVERSE_MIN_PRICE", "5.0")) # לא פני-סטוק
UNIVERSE_MIN_VOL    = int(os.getenv("UNIVERSE_MIN_VOL", "100000"))  # volume מינימלי


def _fetch_exchange_tickers(exchange: str) -> list[str]:
    """מוריד רשימת טיקרים מ-yfinance לפי בורסה."""
    try:
        import urllib.request, csv, io
        # NASDAQ + NYSE דרך קובץ NASDAQ FTP — זמין לציבור
        urls = {
            "NASDAQ": "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=nasdaq",
            "NYSE":   "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=nyse",
        }
        url = urls.get(exchange.upper(), "")
        if not url:
            return []
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        rows = data.get("data", {}).get("table", {}).get("rows", [])
        symbols = []
        for row in rows:
            sym = (row.get("symbol") or "").strip()
            # דלג על מניות עם תוים מיוחדים (preferred, warrants וכו')
            if sym and sym.isalpha() and len(sym) <= 5:
                symbols.append(sym)
        return symbols
    except Exception as e:
        log(f"_fetch_exchange_tickers {exchange} error: {e}")
        return []


def build_universe(force_refresh: bool = False) -> list[str]:
    """
    מוריד את כל מניות NASDAQ + NYSE, מסנן לפי market cap > $1B,
    שומר cache ומחזיר רשימה נקייה.

    force_refresh=True מאלץ הורדה מחדש גם אם cache קיים.
    Cache מתחדש אוטומטית פעם בשבוע.
    """
    # בדוק cache
    if not force_refresh and os.path.exists(UNIVERSE_CACHE_FILE):
        try:
            with open(UNIVERSE_CACHE_FILE) as f:
                cached = json.load(f)
            # cache תקף שבוע
            saved_date = datetime.fromisoformat(cached.get("date","2000-01-01")).date()
            if (datetime.now().date() - saved_date).days < 7:
                tickers_cached = cached.get("tickers", [])
                log(f"📋 Universe loaded from cache: {len(tickers_cached)} tickers (date: {saved_date})")
                return tickers_cached
        except Exception:
            pass

    log("🌐 Building universe — downloading NASDAQ + NYSE tickers...")

    # שלב 1: הורד כל הטיקרים
    nasdaq_tickers = _fetch_exchange_tickers("NASDAQ")
    nyse_tickers   = _fetch_exchange_tickers("NYSE")
    all_tickers    = list(dict.fromkeys(nasdaq_tickers + nyse_tickers))
    log(f"   Raw: {len(nasdaq_tickers)} NASDAQ + {len(nyse_tickers)} NYSE = {len(all_tickers)} unique")

    if not all_tickers:
        log("⚠️ FALLBACK MODE — API נכשל, סורק רק 66 מניות! בדוק חיבור לאינטרנט.")
        return _FALLBACK_TICKERS

    # שלב 2: סינון מהיר לפי market cap דרך yfinance batch
    log(f"   Filtering by market cap > ${UNIVERSE_MIN_CAP/1e9:.0f}B ...")
    passed = []
    failed = 0

    for i, sym in enumerate(all_tickers):
        try:
            info  = yf.Ticker(sym).info or {}

            # market cap — עקבי עם fetch_market_cap בscan_ticker
            mc    = info.get("marketCap")
            mc    = float(mc) if mc else None

            # fallback: shares × price
            if not mc:
                shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
                price  = info.get("currentPrice") or info.get("previousClose")
                if shares and price:
                    mc = float(shares) * float(price)

            # מחיר
            price = info.get("currentPrice") or info.get("previousClose") or info.get("regularMarketPrice")
            price = float(price) if price else None

            # חובה: market cap ידוע ומעל $1B
            if not mc or mc < UNIVERSE_MIN_CAP:
                failed += 1; continue
            # חובה: מחיר ידוע ומעל $5
            if not price or price < UNIVERSE_MIN_PRICE:
                failed += 1; continue

            passed.append(sym)

        except Exception:
            failed += 1

        # התקדמות
        if (i + 1) % 500 == 0 or (i + 1) == len(all_tickers):
            log(f"   Progress: {i+1}/{len(all_tickers)} — passed so far: {len(passed)}")

    # שמור cache
    try:
        cache_data = {
            "date":    datetime.now().isoformat(),
            "tickers": passed,
            "total_scanned": len(all_tickers),
            "passed": len(passed),
            "failed": failed,
        }
        with open(UNIVERSE_CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2)
        log(f"💾 Universe cache saved: {len(passed)} tickers → {UNIVERSE_CACHE_FILE}")
    except Exception as e:
        log(f"Universe cache save error: {e}")

    log(f"✅ Universe built: {len(passed)} tickers passed out of {len(all_tickers)}")
    return passed


# רשימת fallback — אם ה-API נכשל לגמרי
_FALLBACK_TICKERS = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","BRK.B","JPM","JNJ",
    "V","PG","UNH","HD","MA","MRK","CVX","PEP","ABBV","KO","LLY","MCD",
    "BAC","PFE","TMO","COST","AVGO","DIS","CSCO","ACN","DHR","NKE","QCOM",
    "TXN","HON","NEE","AMGN","IBM","INTU","SBUX","GE","GS","BLK","CAT",
    "SPGI","ADP","GILD","ISRG","MDLZ","REGN","ADI","VRTX","EOG","SLB",
    "MMC","ETN","ZTS","CL","CME","FCX","PSA","DUK","SO","D","AEP","EXC",
]

# ============================================================
#  STATE  (בזיכרון)
# ============================================================
api_usage = {
    k: {"count": 0, "last_used": datetime.now(), "blocked_until": None}
    for k in API_KEYS
}
api_index   = 0
_mc_cache   = {}   # market-cap cache
recent_sent = set()

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ============================================================
#  LOGGING
# ============================================================
def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"

    # מציג את ההודעה ב-Render Logs
    print(line, flush=True)

    # שומר גם לקובץ לוג
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def log_watchlist(symbol: str, reason: str, price: float,
                  pattern: str = "", breakout: float = 0.0,
                  score: float = 0.0, details: str = "") -> None:
    """
    כותב מניה ללוג הWatchlist — מניות שעברו EMA28+MA150+דוחות
    אבל נפסלו בשלב אחרון (קרוב לפריצה / אין תבנית / Reverse Scanner).
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # בדוק אם כבר כתבנו כותרת היום
    header = f"\n{'═'*50}\n📅  {today}\n{'═'*50}\n"
    try:
        content = open(WATCHLIST_LOG, "r", encoding="utf-8").read()
        write_header = today not in content
    except Exception:
        write_header = True

    # בחר אמוג'י לפי סיבה
    if "מכירה מוסדית" in reason or "Reverse" in reason:
        emoji = "🚫"
    elif "מתחת לפריצה" in reason or "near" in reason.lower():
        emoji = "⚠️"
    else:
        emoji = "🔍"

    score_str  = f"⭐ ציון:    {score:.0f} / 100\n" if score > 0 else ""
    pattern_str = f"📐 תבנית:  {pattern}\n" if pattern else ""
    breakout_str = f"🎯 פריצה:  ${breakout:.2f}\n" if breakout > 0 else ""
    details_str  = f"📝 פרטים:  {details}\n" if details else ""

    entry = (
        f"{emoji}  {symbol:<6}  |  ${price:.2f}\n"
        f"📌 סיבה:   {reason}\n"
        f"{pattern_str}"
        f"{breakout_str}"
        f"{score_str}"
        f"{details_str}"
        f"{'─'*40}\n"
    )

    try:
        with open(WATCHLIST_LOG, "a", encoding="utf-8") as f:
            if write_header:
                f.write(header)
            f.write(entry)
    except Exception as e:
        log(f"watchlist_log error: {e}")


# ============================================================
#  PERSISTENCE HELPERS
# ============================================================
def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"_load_json error {path}: {e}")
    return {}

def _save_json(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log(f"_save_json error {path}: {e}")

def load_progress() -> dict:
    return _load_json(PROGRESS_FILE) or {"current_key": 0, "start_index": 0}

def save_progress(current_key: int, start_index: int) -> None:
    _save_json(PROGRESS_FILE, {"current_key": current_key, "start_index": start_index})

def load_alert_history() -> dict:
    return _load_json(ALERT_HISTORY_FILE)

def save_alert_history(history: dict) -> None:
    _save_json(ALERT_HISTORY_FILE, history)

def load_blocklist() -> set:
    data = _load_json(BLOCKLIST_FILE)
    return set(data) if isinstance(data, list) else set()

def save_blocklist(bl: set) -> None:
    _save_json(BLOCKLIST_FILE, sorted(list(bl)))

def log_to_csv(ticker: str, reasons: list) -> None:
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = pd.DataFrame([{"Time": now, "Ticker": ticker, "Reasons": " | ".join(reasons)}])
        row.to_csv(SIGNALS_CSV, mode="a", index=False, header=not os.path.exists(SIGNALS_CSV))
    except Exception:
        pass

def log_setup_for_tracking(alert: dict) -> None:
    """שומר סטאפ שנשלח ל-performance_log.csv למעקב עתידי."""
    try:
        row = {
            "date_sent":       datetime.now().strftime("%Y-%m-%d"),
            "ticker":          alert.get("ticker", ""),
            "pattern":         alert.get("pattern_type", ""),
            "entry":           round(float(alert.get("breakout_level", 0)), 2),
            "stop":            round(float(alert.get("stop_loss", 0)), 2),
            "target":          round(float(alert.get("target", 0)), 2),
            "score":           round(float(alert.get("score", 0)), 1),
            "rr":              round(float(alert.get("rr_ratio", 0)), 2),
            "price_5d":        None,
            "price_10d":       None,
            "price_20d":       None,
            "result_5d":       None,
            "result_10d":      None,
            "result_20d":      None,
            "checked":         False,
        }
        df_row = pd.DataFrame([row])
        header = not os.path.exists(PERFORMANCE_CSV)
        df_row.to_csv(PERFORMANCE_CSV, mode="a", index=False, header=header)
    except Exception as e:
        log(f"log_setup_for_tracking error: {e}")

def update_performance_log() -> None:
    """
    בודק סטאפים ישנים ב-performance_log.csv ומעדכן מחירים אחרי 5/10/20 ימים.
    קורא בתחילת כל ריצה.
    """
    if not os.path.exists(PERFORMANCE_CSV):
        return
    try:
        df = pd.read_csv(PERFORMANCE_CSV)
        if df.empty:
            return
        today = datetime.now().date()
        changed = False

        for idx, row in df.iterrows():
            if row.get("checked") == True:
                continue
            try:
                sent_date = pd.to_datetime(row["date_sent"]).date()
                ticker    = str(row["ticker"]).strip().upper()
                entry     = float(row["entry"])
                target    = float(row["target"])
                stop      = float(row["stop"])

                # מביא מחיר נוכחי
                price_df = yf.download(ticker, period="30d", interval="1d",
                                       progress=False, auto_adjust=True)
                if price_df is None or price_df.empty:
                    continue

                price_df.index = pd.to_datetime(price_df.index).date

                all_checked = True
                for days in PERF_CHECK_DAYS:
                    col_p = f"price_{days}d"
                    col_r = f"result_{days}d"
                    if pd.notna(row.get(col_p)):
                        continue  # כבר מולא

                    target_date = sent_date + timedelta(days=days)
                    # מצא את הנר הכי קרוב לתאריך היעד
                    available = [d for d in price_df.index if d >= target_date]
                    if not available:
                        all_checked = False
                        continue

                    close_date  = min(available)
                    close_price = float(price_df.loc[close_date, "Close"])
                    pct_chg     = round((close_price - entry) / max(entry, 1e-9) * 100, 2)

                    # תוצאה: Win / Loss / Partial
                    if close_price >= target:
                        result = f"WIN ({pct_chg:+.1f}%)"
                    elif close_price <= stop:
                        result = f"LOSS ({pct_chg:+.1f}%)"
                    else:
                        result = f"OPEN ({pct_chg:+.1f}%)"

                    df.at[idx, col_p] = round(close_price, 2)
                    df.at[idx, col_r] = result
                    changed = True

                if all_checked:
                    df.at[idx, "checked"] = True
                    changed = True

            except Exception as e:
                log(f"performance update error for {row.get('ticker','?')}: {e}")
                continue

        if changed:
            df.to_csv(PERFORMANCE_CSV, index=False)
            log(f"Performance log updated: {PERFORMANCE_CSV}")

    except Exception as e:
        log(f"update_performance_log error: {e}")

# ============================================================
#  API KEY MANAGEMENT
# ============================================================
def get_available_api_key() -> str | None:
    global api_index
    if not API_KEYS:
        return None
    n = len(API_KEYS)
    while True:
        now = datetime.now()
        for _ in range(n):
            key = API_KEYS[api_index % n]
            api_index = (api_index + 1) % n
            d = api_usage.setdefault(key, {"count": 0, "last_used": now, "blocked_until": None})
            blocked = d.get("blocked_until")
            if blocked and now < blocked:
                continue
            last = d.get("last_used") or now
            if (now - last) >= RESET_TIME:
                d["count"] = 0
                d["blocked_until"] = None
                d["last_used"] = now
            if d.get("count", 0) >= TICKERS_PER_KEY:
                continue
            return key
        # כל המפתחות חסומים — חכה
        wait = 5.0
        try:
            candidates = []
            for k in API_KEYS:
                d = api_usage.get(k) or {}
                bu = d.get("blocked_until")
                lu = d.get("last_used")
                if bu and now < bu:
                    candidates.append((bu - now).total_seconds())
                if lu:
                    candidates.append(max(0.0, RESET_TIME.total_seconds() - (now - lu).total_seconds()))
            if candidates:
                wait = max(1.0, min(candidates))
        except Exception:
            pass
        log(f"All API keys exhausted. Waiting {wait:.1f}s...")
        time.sleep(wait)

def update_api_usage(key: str) -> None:
    if not key:
        return
    now = datetime.now()
    d = api_usage.setdefault(key, {"count": 0, "last_used": now, "blocked_until": None})
    bu = d.get("blocked_until")
    if bu and now < bu:
        return
    d["count"] = int(d.get("count", 0)) + 1
    d["last_used"] = now

# ============================================================
#  BLOCKLIST HELPER
# ============================================================
_BLOCK_PHRASES = (
    "available starting with pro", "pro plan", "grow plan", "upgrade",
    "missing or invalid", "invalid symbol", "symbol is invalid",
    "parameter is missing or invalid", "no price data", "no data found",
    "no data", "not supported", "delisted",
)

def _maybe_block_symbol(symbol: str, message: str) -> bool:
    sym = symbol.strip().upper().replace("$", "")
    if not sym:
        return False
    msg_low = (message or "").lower()
    if not any(p in msg_low for p in _BLOCK_PHRASES):
        return False
    try:
        bl = load_blocklist()
        if sym in bl:
            return False
        bl.add(sym)
        save_blocklist(bl)
        log(f"Blocklisted: {sym} (reason: {message})")
        return True
    except Exception as e:
        log(f"_maybe_block_symbol error for {sym}: {e}")
        return False

# ============================================================
#  DATA FETCHING
# ============================================================
def fetch_data_twelvedata(ticker: str, outputsize: int = 500) -> pd.DataFrame | None:
    """מביא OHLCV מ-TwelveData ומחזיר DataFrame עם עמודות lowercase."""
    symbol = ticker.strip().upper().replace("$", "")
    if not symbol:
        return None
    outputsize = max(50, int(outputsize))
    key = get_available_api_key()
    if not key:
        log("No available API key.")
        return None
    url = f"{BASE_URL}?symbol={symbol}&interval=1day&outputsize={outputsize}&apikey={key}"
    try:
        r = HTTP_SESSION.get(url, timeout=15)
        if r.status_code == 429:
            api_usage[key]["blocked_until"] = datetime.now() + RESET_TIME
            log(f"TwelveData 429 for {symbol}. Key blocked.")
            return None
        if r.status_code in (401, 403):
            api_usage[key]["blocked_until"] = datetime.now() + timedelta(hours=12)
            log(f"TwelveData auth error {r.status_code} for {symbol}.")
            return None
        r.raise_for_status()
        data = r.json() if r.content else {}
        if data.get("status") == "error" or "values" not in data:
            msg = str(data.get("message") or data.get("code") or "no data")
            log(f"TwelveData error for {symbol}: {msg}")
            _maybe_block_symbol(symbol, msg)
            return None
        values = data.get("values") or []
        if not values:
            _maybe_block_symbol(symbol, "empty values")
            return None
        update_api_usage(key)
        df = pd.DataFrame(values)
        df = df.rename(columns={"datetime":"Date","open":"Open","high":"High",
                                  "low":"Low","close":"Close","volume":"Volume"})
        if "Volume" not in df.columns:
            df["Volume"] = 0
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"]).set_index("Date")
        df.index.name = None
        for c in ["Open","High","Low","Close","Volume"]:
            df[c] = pd.to_numeric(df.get(c, 0), errors="coerce")
        df = df.dropna(subset=["Open","High","Low","Close"])
        if df.empty:
            _maybe_block_symbol(symbol, "numeric cleaning empty")
            return None
        df = df.iloc[::-1]   # oldest → newest
        df.columns = df.columns.str.lower()
        return df.copy()
    except requests.HTTPError as e:
        log(f"TwelveData HTTPError {symbol}: {e}")
        return None
    except Exception:
        log(f"TwelveData general error {symbol}:\n{traceback.format_exc()}")
        return None

def _ticker_exists_yahoo(ticker: str, timeout: int = 6) -> bool:
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=price"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        return r.status_code == 200 and bool(r.content)
    except Exception:
        return False

def fetch_data_yfinance(ticker: str, period: str = "500d", max_retries: int = 3) -> pd.DataFrame | None:
    """Fallback: מביא נתונים מ-yfinance — בלי pre-check."""
    for attempt in range(max_retries):
        try:
            df = yf.download(ticker, period=period, interval="1d",
                             progress=False, auto_adjust=False, threads=False)
            if df is None or df.empty:
                raise RuntimeError("empty")
            # תמיכה ב-MultiIndex columns של yfinance החדש (>= 0.2.x)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower()
                               for c in df.columns]
            else:
                df.columns = [str(c).lower() for c in df.columns]
            df.index = pd.to_datetime(df.index)
            required = {"open","high","low","close","volume"}
            if not required.issubset(df.columns):
                raise RuntimeError("missing columns")
            return df[list(required)].copy()
        except Exception as e:
            sleep = 2.0 * (2 ** attempt) * (0.8 + 0.4 * random.random())
            log(f"yfinance attempt {attempt+1} for {ticker}: {e} — retry in {sleep:.1f}s")
            time.sleep(sleep)
    # last resort: Ticker.history
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if df is not None and not df.empty:
            df = df.rename(columns=str.lower)
            df.index = pd.to_datetime(df.index)
            return df[["open","high","low","close","volume"]].copy()
    except Exception as e:
        log(f"yfinance history fallback error for {ticker}: {e}")
    return None

def fetch_market_cap(ticker: str) -> float | None:
    """
    מחזיר market cap עם cache.
    עקבי עם build_universe — info['marketCap'] + fallback shares×price.
    """
    t = ticker.strip().upper()
    if t in _mc_cache:
        return _mc_cache[t]
    try:
        info = _get_yf_info(t)
        mc   = info.get("marketCap")
        mc   = float(mc) if mc else None

        # fallback: shares × price
        if not mc:
            shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
            price  = info.get("currentPrice") or info.get("previousClose")
            if shares and price:
                mc = float(shares) * float(price)

        _mc_cache[t] = mc
        return mc
    except Exception as e:
        log(f"market cap error for {t}: {e}")
        _mc_cache[t] = None
        return None

# cache למידע חברה — נשמר כל הריצה
_info_cache: dict = {}

def _get_yf_info(ticker: str) -> dict:
    """מחזיר info dict מ-yfinance עם cache."""
    t = ticker.strip().upper()
    if t not in _info_cache:
        try:
            _info_cache[t] = getattr(yf.Ticker(t), "info", {}) or {}
        except Exception:
            _info_cache[t] = {}
    return _info_cache[t]

def get_company_info(ticker: str) -> str:
    """מחרוזת תצוגה: שם (סקטור)."""
    info   = _get_yf_info(ticker)
    name   = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector", "N/A")
    return f"{name} ({sector})"

def get_company_card(ticker: str) -> dict:
    """
    מחזיר dict עם פרטי החברה למייל:
    name, sector, description, eps, market_cap_b, earnings_date
    """
    info = _get_yf_info(ticker)
    name   = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector", "N/A")
    desc   = info.get("longBusinessSummary", "") or ""
    # קצר ל-3 משפטים
    sentences = [s.strip() for s in desc.replace("\n"," ").split(".") if s.strip()]
    short_desc = ". ".join(sentences[:3]) + ("." if sentences else "")

    eps   = info.get("trailingEps") or info.get("forwardEps")
    mc    = info.get("marketCap")
    mc_b  = round(mc / 1e9, 1) if mc else None

    # תאריך הדוח הבא
    edate = None
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is not None:
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    edate = pd.to_datetime(ed[0] if isinstance(ed, list) else ed).date()
            elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
                edate = pd.to_datetime(cal["Earnings Date"].iloc[0]).date()
    except Exception:
        pass

    return {
        "name":          name,
        "sector":        sector,
        "description":   short_desc,
        "eps":           eps,
        "market_cap_b":  mc_b,
        "earnings_date": edate,
    }

def earnings_filter_ok(ticker: str) -> tuple[bool, str]:
    """
    מחזיר (True, "") אם אין דוח ב-EARNINGS_FILTER_DAYS הקרובים.
    אם תאריך לא ידוע — מחזיר (True, "unknown") כדי לא לחסום.
    """
    try:
        cal = yf.Ticker(ticker).calendar
        edate = None
        if cal is not None:
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    edate = pd.to_datetime(ed[0] if isinstance(ed, list) else ed).date()
            elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
                edate = pd.to_datetime(cal["Earnings Date"].iloc[0]).date()

        if edate is None:
            return True, "earnings date unknown — allowing"

        days_until = (edate - datetime.now().date()).days
        if 0 <= days_until <= EARNINGS_FILTER_DAYS:
            return False, f"earnings in {days_until} days ({edate})"
        return True, f"next earnings: {edate} ({days_until}d away)"
    except Exception as e:
        return True, f"earnings check error: {e} — allowing"

# ============================================================
#  TECHNICAL INDICATORS
# ============================================================
def _flatten_df_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    מנרמל columns של yfinance — מטפל ב-MultiIndex שנוצר עם threads=True.
    מחזיר DataFrame עם עמודות רגילות (lowercase).
    """
    if isinstance(df.columns, pd.MultiIndex):
        # MultiIndex: (field, ticker) — לוקח רק את שכבת field
        df = df.copy()
        df.columns = [str(c[0]).lower() if isinstance(c, tuple) else str(c).lower()
                      for c in df.columns]
    else:
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]
    return df


def ensure_ma_columns(df: pd.DataFrame) -> None:
    # וודא שהעמודות פשוטות לפני חישוב
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[0]).lower() if isinstance(c, tuple) else str(c).lower()
                      for c in df.columns]
    close = df["close"].squeeze()  # squeeze מבטיח Series גם אם DataFrame
    if "ema28" not in df.columns:
        df["ema28"] = close.ewm(span=28, adjust=False, min_periods=12).mean()
    if "ma150" not in df.columns:
        df["ma150"] = close.rolling(window=150, min_periods=50).mean()

def add_technical_indicators(df: pd.DataFrame) -> None:
    """מוסיף ATR14, OBV, RSI14, MACD-hist, ADX14, CCI20."""
    try:
        ensure_ma_columns(df)
        high, low, close = df["high"], df["low"], df["close"]

        # ATR
        tr = pd.concat([
            (high - low).abs(),
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        df["atr14"] = tr.rolling(14, min_periods=7).mean()

        # OBV — numpy vectorised (מהיר פי ~100 מ-loop)
        price_diff = np.sign(close.diff().fillna(0).values)
        signed_vol = price_diff * df["volume"].values
        df["obv"]     = np.cumsum(signed_vol)
        df["obv_ema5"] = df["obv"].ewm(span=5, adjust=False).mean()

        # RSI
        delta  = close.diff()
        ma_up  = delta.clip(lower=0).rolling(14, min_periods=7).mean()
        ma_dn  = (-delta.clip(upper=0)).rolling(14, min_periods=7).mean()
        df["rsi14"] = 100 - 100 / (1 + ma_up / ma_dn.replace(0, 1e-9))

        # MACD histogram
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd  = ema12 - ema26
        df["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()

        # ADX
        up_m   = high.diff()
        dn_m   = -low.diff()
        p_dm   = ((up_m > dn_m) & (up_m > 0)) * up_m
        m_dm   = ((dn_m > up_m) & (dn_m > 0)) * dn_m
        atr_s  = tr.rolling(14, min_periods=7).mean().replace(0, 1e-9)
        p_di   = 100 * p_dm.ewm(alpha=1/14).mean() / atr_s
        m_di   = 100 * m_dm.ewm(alpha=1/14).mean() / atr_s
        dx     = (abs(p_di - m_di) / (p_di + m_di).replace(0, 1e-9)) * 100
        df["adx14"] = dx.ewm(alpha=1/14).mean().fillna(0)

        # CCI
        tp     = (high + low + close) / 3
        sma_tp = tp.rolling(20, min_periods=10).mean()
        mad    = (tp - sma_tp).abs().rolling(20, min_periods=10).mean()
        df["cci20"] = (tp - sma_tp) / (0.015 * mad.replace(0, 1e-9))

    except Exception as e:
        log(f"add_technical_indicators error: {e}")

# ============================================================
#  SCORING
# ============================================================
# ============================================================
#  SCORE HELPERS — פונקציות עזר לציון 0-100
# ============================================================

def _score_insider(ticker: str) -> tuple[float, str]:
    """
    בודק קניות Insider ב-90 הימים האחרונים דרך OpenInsider.
    מחזיר (נקודות, תיאור).  מקס 8 נקודות.
    """
    try:
        url = f"https://openinsider.com/screener?s={ticker}&fd=90&td=0&xp=1&vl=10&sortcol=0&cnt=10&action=1"
        r   = HTTP_SESSION.get(url, timeout=10,
                               headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return 3.0, "✅ Insider לא זמין (+3)"
        text = r.text
        # ספור שורות קנייה
        buy_count = text.count("P - Purchase")
        if buy_count == 0:
            # נסה גם בלי "P - Purchase" — חפש סכום קנייה
            buy_count = text.count("class=\"p\"")
        if buy_count >= 3:
            return 8.0, f"✅ Insider: {buy_count} קניות מנהלים ב-90 יום (+8)"
        elif buy_count == 2:
            return 6.0, f"✅ Insider: {buy_count} קניות מנהלים (+6)"
        elif buy_count == 1:
            return 4.0, f"⚠️ Insider: קנייה אחת של מנהל (+4)"
        else:
            return 1.0, "❌ אין קניות Insider (+1)"
    except Exception as e:
        return 3.0, f"⚠️ Insider שגיאה (+3)"


def _score_institutional(ticker: str) -> tuple[float, str]:
    """
    בודק אחזקות מוסדיות דרך yfinance (major holders).
    מחזיר (נקודות, תיאור).  מקס 7 נקודות.
    """
    try:
        info          = _get_yf_info(ticker)
        inst_pct      = float(info.get("heldPercentInstitutions", 0) or 0) * 100
        inst_change   = info.get("52WeekChange")  # proxy לכיוון

        if inst_pct >= 70:
            pts = 7.0
            desc = f"✅ מוסדיים: {inst_pct:.0f}% אחזקה גבוהה (+7)"
        elif inst_pct >= 50:
            pts = 5.0
            desc = f"✅ מוסדיים: {inst_pct:.0f}% (+5)"
        elif inst_pct >= 30:
            pts = 3.0
            desc = f"⚠️ מוסדיים: {inst_pct:.0f}% (+3)"
        elif inst_pct > 0:
            pts = 1.0
            desc = f"❌ מוסדיים: {inst_pct:.0f}% — נמוך (+1)"
        else:
            pts = 2.0
            desc = "⚠️ 13F לא זמין (+2)"
        return pts, desc
    except Exception:
        return 3.0, "⚠️ 13F שגיאה (+3)"


def _score_news_sentiment(ticker: str) -> tuple[float, str]:
    """
    בודק סנטימנט חדשות דרך yfinance news.
    מחזיר (נקודות, תיאור).  מקס 6 נקודות.
    """
    try:
        t        = yf.Ticker(ticker)
        news     = getattr(t, "news", []) or []
        if not news:
            return 2.0, "⚠️ אין חדשות זמינות (+2)"

        # מילות מפתח חיוביות/שליליות
        pos_kw = ["upgrade", "beat", "record", "growth", "strong", "buy",
                  "outperform", "raises", "raised", "above", "profit", "surge"]
        neg_kw = ["downgrade", "miss", "loss", "weak", "sell", "cut",
                  "below", "investigation", "lawsuit", "decline", "warn"]

        pos = neg = 0
        for item in news[:10]:
            title = (item.get("title") or "").lower()
            pos  += sum(1 for w in pos_kw if w in title)
            neg  += sum(1 for w in neg_kw if w in title)

        net = pos - neg
        if net >= 3:
            return 6.0, f"✅ חדשות חיוביות מאוד (pos={pos}, neg={neg}) (+6)"
        elif net >= 1:
            return 4.0, f"✅ חדשות חיוביות (pos={pos}, neg={neg}) (+4)"
        elif net == 0:
            return 2.0, f"⚠️ חדשות נייטרליות (+2)"
        else:
            return 0.0, f"❌ חדשות שליליות (pos={pos}, neg={neg}) (+0)"
    except Exception:
        return 2.0, "⚠️ חדשות שגיאה (+2)"


def _score_google_trends(ticker: str) -> tuple[float, str]:
    """
    בודק Google Trends עבור הטיקר.
    מחזיר (נקודות, תיאור).  מקס 3 נקודות.
    """
    try:
        import importlib
        if importlib.util.find_spec("pytrends") is None:
            return 1.0, "⚠️ pytrends לא מותקן (+1)"
        from pytrends.request import TrendReq  # type: ignore[import]
        pt = TrendReq(hl="en-US", tz=360, timeout=(5, 10))
        pt.build_payload([ticker], timeframe="today 3-m")
        df_t = pt.interest_over_time()
        if df_t.empty or ticker not in df_t.columns:
            return 1.0, "⚠️ Trends לא זמין (+1)"
        vals  = df_t[ticker].values
        if len(vals) < 4:
            return 1.0, "⚠️ Trends נתונים קצרים (+1)"
        recent = float(vals[-4:].mean())
        older  = float(vals[-12:-4].mean()) if len(vals) >= 12 else float(vals.mean())
        if older == 0:
            return 1.0, "⚠️ Trends אפס (+1)"
        change = (recent - older) / older
        if change >= 0.30:
            return 3.0, f"✅ Google Trends עלה {change*100:.0f}% (+3)"
        elif change >= 0.10:
            return 2.0, f"⚠️ Google Trends עלה מעט {change*100:.0f}% (+2)"
        elif change >= -0.10:
            return 1.0, f"⚠️ Google Trends יציב (+1)"
        else:
            return 0.0, f"❌ Google Trends ירד {change*100:.0f}% (+0)"
    except Exception:
        return 1.0, "⚠️ Google Trends שגיאה (+1)"


def compute_setup_score(df: pd.DataFrame, ticker: str,
                        break_level: float, break_index: int,
                        pattern_info: dict) -> float:
    """
    ציון איכות 0-100 לסטאפ — 20 פרמטרים.
    הציון מורכב מ-5 קבוצות:
      A) תבנית        — עד 30 נקודות
      B) מומנטום      — עד 25 נקודות
      C) כסף חכם     — עד 25 נקודות
      D) קטליזטור    — עד 20 נקודות
      E) סיכון        — בונוס/עונש ±8

    הציון נשמר ב-pattern_info["score"] + ["score_reasons"].
    הסינון הראשוני לא משתנה — רק הציון.
    """
    if break_index < 1 or break_index >= len(df):
        pattern_info.update({"score": 0.0, "score_reasons": ["invalid break_index"]})
        return 0.0

    ensure_ma_columns(df)
    reasons: list[str] = []
    total   = 0.0

    row      = df.iloc[break_index]
    close_p  = float(row["close"])
    open_p   = float(row["open"])
    high_p   = float(row["high"])
    low_p    = float(row["low"])
    ma150    = float(row["ma150"]) if "ma150" in df.columns and not pd.isna(row.get("ma150", float("nan"))) else None
    ema28    = float(row["ema28"]) if "ema28" in df.columns and not pd.isna(row.get("ema28", float("nan"))) else None
    atr14    = float(row["atr14"]) if "atr14" in df.columns and not pd.isna(row.get("atr14", float("nan"))) else None

    # ════════════════════════════════════════════════
    # קבוצה A — תבנית (מקס 30)
    # ════════════════════════════════════════════════

    # A1) כמה זמן נבנתה התבנית (מקס 8)
    pattern_bars = int(pattern_info.get("pattern_bars", 0))
    if pattern_bars <= 0:
        # נסה לחשב מה-meta
        start_idx = pattern_info.get("start_index", None)
        if start_idx is not None:
            pattern_bars = break_index - int(start_idx)
    weeks = pattern_bars / 5.0
    if 3 <= weeks <= 12:
        pts = 8.0
        reasons.append(f"A1 ✅ תבנית נבנתה {weeks:.1f} שבועות — אידיאלי (+8)")
    elif 2 <= weeks < 3:
        pts = 5.0
        reasons.append(f"A1 ⚠️ תבנית קצרה ({weeks:.1f} שבועות) (+5)")
    elif 12 < weeks <= 20:
        pts = 6.0
        reasons.append(f"A1 ✅ תבנית ארוכה ({weeks:.1f} שבועות) (+6)")
    elif weeks > 20:
        pts = 3.0
        reasons.append(f"A1 ⚠️ תבנית ארוכה מאוד ({weeks:.1f} שבועות) (+3)")
    else:
        pts = 1.0
        reasons.append(f"A1 ❌ תבנית קצרה מדי ({weeks:.1f} שבועות) (+1)")
    total += pts

    # A2) עומק התבנית (מקס 6)
    depth_pct = float(pattern_info.get("depth_pct", 0.0))
    if 0.08 <= depth_pct <= 0.33:
        pts = 6.0
        reasons.append(f"A2 ✅ עומק תבנית {depth_pct*100:.1f}% — אידיאלי (+6)")
    elif 0.05 <= depth_pct < 0.08:
        pts = 3.0
        reasons.append(f"A2 ⚠️ תבנית רדודה ({depth_pct*100:.1f}%) (+3)")
    elif depth_pct > 0.33:
        pts = 2.0
        reasons.append(f"A2 ⚠️ תבנית עמוקה מדי ({depth_pct*100:.1f}%) (+2)")
    else:
        pts = 1.0
        reasons.append(f"A2 ❌ עומק לא ידוע (+1)")
    total += pts

    # A3) מיקום ביחס ל-MA150 (מקס 6)
    if ma150 and ma150 > 0:
        dist150 = (close_p - ma150) / ma150
        if 0 <= dist150 <= 0.08:
            pts = 6.0
            reasons.append(f"A3 ✅ מחיר {dist150*100:.1f}% מעל MA150 — קרוב (+6)")
        elif 0.08 < dist150 <= 0.15:
            pts = 4.0
            reasons.append(f"A3 ✅ מחיר {dist150*100:.1f}% מעל MA150 (+4)")
        elif dist150 > 0.15:
            pts = 1.0
            reasons.append(f"A3 ⚠️ רחוק מ-MA150 ({dist150*100:.1f}%) (+1)")
        else:
            pts = 0.0
            reasons.append(f"A3 ❌ מחיר מתחת ל-MA150 (+0)")
    else:
        pts = 2.0
        reasons.append("A3 ⚠️ MA150 לא זמין (+2)")
    total += pts

    # A4) עוצמת נר הפריצה (מקס 6)
    rng      = max(high_p - low_p, 1e-6)
    body_r   = abs(close_p - open_p) / rng
    close_pos = (close_p - low_p) / rng
    if body_r >= 0.70 and close_pos >= 0.75:
        pts = 6.0
        reasons.append(f"A4 ✅ נר פריצה חזק מאוד (body={body_r*100:.0f}%) (+6)")
    elif body_r >= 0.50 and close_pos >= 0.60:
        pts = 4.0
        reasons.append(f"A4 ✅ נר פריצה טוב (body={body_r*100:.0f}%) (+4)")
    elif body_r >= 0.35:
        pts = 2.0
        reasons.append(f"A4 ⚠️ נר בינוני (body={body_r*100:.0f}%) (+2)")
    else:
        pts = 0.0
        reasons.append(f"A4 ❌ נר חלש (body={body_r*100:.0f}%) (+0)")
    total += pts

    # A5) נפח ביום הפריצה (מקס 4)
    if "volume" in df.columns and break_index >= VOLUME_AVG_LOOKBACK:
        avg_v = float(df["volume"].iloc[break_index - VOLUME_AVG_LOOKBACK:break_index].mean())
        vol_p = float(df["volume"].iloc[break_index])
        if avg_v > 0:
            ratio = vol_p / avg_v
            if ratio >= 2.0:
                pts = 4.0
                reasons.append(f"A5 ✅ נפח פריצה ×{ratio:.1f} — חזק מאוד (+4)")
            elif ratio >= 1.5:
                pts = 3.0
                reasons.append(f"A5 ✅ נפח פריצה ×{ratio:.1f} (+3)")
            elif ratio >= 1.0:
                pts = 1.5
                reasons.append(f"A5 ⚠️ נפח ממוצע ×{ratio:.1f} (+1.5)")
            else:
                pts = 0.0
                reasons.append(f"A5 ❌ נפח נמוך ×{ratio:.1f} (+0)")
        else:
            pts = 1.0
            reasons.append("A5 ⚠️ נפח לא זמין (+1)")
    else:
        pts = 1.0
        reasons.append("A5 ⚠️ נפח לא זמין (+1)")
    total += pts

    # ════════════════════════════════════════════════
    # קבוצה B — מומנטום (מקס 25)
    # ════════════════════════════════════════════════

    # B1) RS ציון vs S&P500 (מקס 8)
    try:
        rs = compute_rs_score(ticker, df)
        if rs >= 90:
            pts = 8.0; reasons.append(f"B1 ✅ RS={rs} — טופ 10% (+8)")
        elif rs >= 75:
            pts = 6.0; reasons.append(f"B1 ✅ RS={rs} — חזק (+6)")
        elif rs >= 60:
            pts = 4.0; reasons.append(f"B1 ⚠️ RS={rs} — בינוני (+4)")
        elif rs >= 40:
            pts = 2.0; reasons.append(f"B1 ⚠️ RS={rs} — חלש (+2)")
        else:
            pts = 0.0; reasons.append(f"B1 ❌ RS={rs} — חלש מאוד (+0)")
    except Exception:
        pts = 3.0; reasons.append("B1 ⚠️ RS לא זמין (+3)")
    total += pts

    # B2) RS vs סקטור (מקס 7)
    try:
        info       = _get_yf_info(ticker)
        sector     = info.get("sector", "")
        sector_etf = {
            "Technology": "XLK", "Health Care": "XLV", "Financials": "XLF",
            "Consumer Discretionary": "XLY", "Industrials": "XLI",
            "Communication Services": "XLC", "Energy": "XLE",
            "Consumer Staples": "XLP", "Utilities": "XLU",
            "Real Estate": "XLRE", "Materials": "XLB",
        }.get(sector, "")
        if sector_etf:
            sec_df = fetch_data_yfinance(sector_etf, period="6mo")
            if sec_df is not None and len(sec_df) >= 63:
                sec_ret    = (float(sec_df["close"].iloc[-1]) / float(sec_df["close"].iloc[-63]) - 1)
                stock_ret  = (float(df["close"].iloc[-1])     / float(df["close"].iloc[-63])     - 1) if len(df) >= 63 else 0
                outperf    = stock_ret - sec_ret
                if outperf >= 0.10:
                    pts = 7.0; reasons.append(f"B2 ✅ עודף תשואה vs {sector_etf}: +{outperf*100:.1f}% (+7)")
                elif outperf >= 0.05:
                    pts = 5.0; reasons.append(f"B2 ✅ עודף תשואה vs {sector_etf}: +{outperf*100:.1f}% (+5)")
                elif outperf >= 0:
                    pts = 3.0; reasons.append(f"B2 ⚠️ מעט מעל הסקטור (+{outperf*100:.1f}%) (+3)")
                else:
                    pts = 0.0; reasons.append(f"B2 ❌ מתחת לסקטור ({outperf*100:.1f}%) (+0)")
            else:
                pts = 3.0; reasons.append("B2 ⚠️ נתוני סקטור לא זמינים (+3)")
        else:
            pts = 3.0; reasons.append(f"B2 ⚠️ סקטור לא מזוהה ({sector}) (+3)")
    except Exception:
        pts = 3.0; reasons.append("B2 ⚠️ RS סקטור שגיאה (+3)")
    total += pts

    # B3) חוזק סקטור (מקס 5)
    try:
        rotation_map = build_sector_rotation_map()
        rot_adj, rot_reason = get_sector_rotation_adjustment(ticker, rotation_map)
        if rot_adj > 0:
            pts = min(5.0, 2.5 + rot_adj * 10)
            reasons.append(f"B3 ✅ סקטור חם: {rot_reason} (+{pts:.1f})")
        elif rot_adj == 0:
            pts = 2.5
            reasons.append(f"B3 ⚠️ סקטור נייטרלי (+2.5)")
        else:
            pts = max(0.0, 2.5 + rot_adj * 10)
            reasons.append(f"B3 ❌ סקטור קר: {rot_reason} (+{pts:.1f})")
    except Exception:
        pts = 2.0; reasons.append("B3 ⚠️ Rotation לא זמין (+2)")
    total += pts

    # B4) ATR — תנודתיות בריאה (מקס 5)
    if atr14 and break_level and break_level > 0:
        atr_pct = atr14 / break_level
        if 0.015 <= atr_pct <= 0.04:
            pts = 5.0; reasons.append(f"B4 ✅ ATR בריא {atr_pct*100:.1f}% (+5)")
        elif 0.04 < atr_pct <= 0.07:
            pts = 3.0; reasons.append(f"B4 ⚠️ ATR גבוה {atr_pct*100:.1f}% (+3)")
        elif atr_pct > 0.07:
            pts = 1.0; reasons.append(f"B4 ❌ ATR גבוה מאוד {atr_pct*100:.1f}% (+1)")
        else:
            pts = 3.0; reasons.append(f"B4 ⚠️ ATR נמוך {atr_pct*100:.1f}% (+3)")
    else:
        pts = 2.0; reasons.append("B4 ⚠️ ATR לא זמין (+2)")
    total += pts

    # ════════════════════════════════════════════════
    # קבוצה C — כסף חכם (מקס 25)
    # ════════════════════════════════════════════════

    # C1) Insider Buying — OpenInsider (מקס 8)
    try:
        insider_pts, insider_reason = _score_insider(ticker)
        total += insider_pts
        reasons.append(f"C1 {insider_reason}")
    except Exception:
        total += 3.0; reasons.append("C1 ⚠️ Insider לא זמין (+3)")

    # C2) מוסדיים — 13F (מקס 7)
    try:
        inst_pts, inst_reason = _score_institutional(ticker)
        total += inst_pts
        reasons.append(f"C2 {inst_reason}")
    except Exception:
        total += 3.0; reasons.append("C2 ⚠️ 13F לא זמין (+3)")

    # C3) Dark Pool prints (מקס 5)
    try:
        dp_result = get_dark_pool_prints(ticker, df)
        dp = int(dp_result.get("bullish_prints", 0)) if isinstance(dp_result, dict) else 0
        if dp >= 2:
            pts = 5.0; reasons.append(f"C3 ✅ Dark Pool: {dp} פרינטים (+5)")
        elif dp == 1:
            pts = 3.0; reasons.append(f"C3 ⚠️ Dark Pool: {dp} פרינט (+3)")
        else:
            pts = 1.0; reasons.append("C3 ❌ אין Dark Pool (+1)")
    except Exception:
        pts = 2.0; reasons.append("C3 ⚠️ Dark Pool לא זמין (+2)")
    total += pts

    # C4) Short Interest (מקס 5)
    try:
        info    = _get_yf_info(ticker)
        si_pct  = float(info.get("shortPercentOfFloat", 0) or 0) * 100
        if si_pct >= 15:
            pts = 5.0; reasons.append(f"C4 ✅ Short Interest {si_pct:.1f}% — squeeze potential (+5)")
        elif si_pct >= 8:
            pts = 3.0; reasons.append(f"C4 ⚠️ Short Interest {si_pct:.1f}% (+3)")
        elif si_pct > 0:
            pts = 1.5; reasons.append(f"C4 ℹ️ Short Interest {si_pct:.1f}% (+1.5)")
        else:
            pts = 1.0; reasons.append("C4 ⚠️ Short Interest לא זמין (+1)")
    except Exception:
        pts = 1.0; reasons.append("C4 ⚠️ Short Interest שגיאה (+1)")
    total += pts

    # ════════════════════════════════════════════════
    # קבוצה D — קטליזטור (מקס 20)
    # ════════════════════════════════════════════════

    # D1) חדשות חיוביות ב-30 יום (מקס 6)
    try:
        news_pts, news_reason = _score_news_sentiment(ticker)
        total += news_pts
        reasons.append(f"D1 {news_reason}")
    except Exception:
        total += 2.0; reasons.append("D1 ⚠️ חדשות לא זמינות (+2)")

    # D2) Earnings Growth (מקס 7)
    try:
        info         = _get_yf_info(ticker)
        eg           = info.get("earningsGrowth") or info.get("revenueGrowth")
        if eg is not None:
            eg = float(eg)
            if eg >= 0.25:
                pts = 7.0; reasons.append(f"D2 ✅ Earnings Growth {eg*100:.0f}% (+7)")
            elif eg >= 0.10:
                pts = 5.0; reasons.append(f"D2 ✅ Earnings Growth {eg*100:.0f}% (+5)")
            elif eg >= 0:
                pts = 3.0; reasons.append(f"D2 ⚠️ Earnings Growth {eg*100:.0f}% (+3)")
            else:
                pts = 0.0; reasons.append(f"D2 ❌ Earnings שלילי {eg*100:.0f}% (+0)")
        else:
            pts = 2.0; reasons.append("D2 ⚠️ Earnings Growth לא זמין (+2)")
    except Exception:
        pts = 2.0; reasons.append("D2 ⚠️ Earnings שגיאה (+2)")
    total += pts

    # D3) Analyst Upgrades (מקס 4)
    try:
        rec = _get_yf_info(ticker).get("recommendationKey", "")
        if rec in ("strongBuy", "buy"):
            pts = 4.0; reasons.append(f"D3 ✅ Analyst: {rec} (+4)")
        elif rec == "hold":
            pts = 2.0; reasons.append(f"D3 ⚠️ Analyst: hold (+2)")
        elif rec in ("sell", "strongSell"):
            pts = 0.0; reasons.append(f"D3 ❌ Analyst: {rec} (+0)")
        else:
            pts = 2.0; reasons.append("D3 ⚠️ Analyst לא זמין (+2)")
    except Exception:
        pts = 2.0; reasons.append("D3 ⚠️ Analyst שגיאה (+2)")
    total += pts

    # D4) Google Trends (מקס 3)
    try:
        trend_pts, trend_reason = _score_google_trends(ticker)
        total += trend_pts
        reasons.append(f"D4 {trend_reason}")
    except Exception:
        total += 1.0; reasons.append("D4 ⚠️ Google Trends לא זמין (+1)")

    # ════════════════════════════════════════════════
    # קבוצה E — סיכון (בונוס/עונש ±8)
    # ════════════════════════════════════════════════

    # E1) Market Regime (±3)
    try:
        regime = get_market_regime()
        if regime.get("regime") == "BULL":
            total += 3.0; reasons.append("E1 ✅ Market Regime: BULL (+3)")
        elif regime.get("regime") == "NEUTRAL":
            reasons.append("E1 ⚠️ Market Regime: NEUTRAL (+0)")
        else:
            total -= 3.0; reasons.append("E1 ❌ Market Regime: BEAR (-3)")
    except Exception:
        reasons.append("E1 ⚠️ Regime לא זמין (+0)")

    # E2) דוחות בפחות מ-30 יום (-3)
    try:
        next_e = pattern_info.get("next_earnings_days")
        if next_e is not None and int(next_e) < 30:
            total -= 3.0; reasons.append(f"E2 ⚠️ דוחות בעוד {next_e} ימים (-3)")
        else:
            reasons.append("E2 ✅ דוחות רחוקים (+0)")
    except Exception:
        pass

    # E3) נפח ממוצע נמוך — סיכון נזילות (-2)
    if "volume" in df.columns and break_index >= VOLUME_AVG_LOOKBACK:
        avg_v = float(df["volume"].iloc[break_index - VOLUME_AVG_LOOKBACK:break_index].mean())
        if avg_v < MIN_AVG_VOLUME:
            total -= 2.0; reasons.append(f"E3 ⚠️ נפח נמוך ({avg_v:,.0f}) (-2)")
        else:
            reasons.append(f"E3 ✅ נזילות תקינה ({avg_v:,.0f}) (+0)")

    # ════════════════════════════════════════════════
    final = round(max(0.0, min(100.0, total)), 1)
    pattern_info["score"]         = final
    pattern_info["score_reasons"] = reasons
    return final


def _adx_ok(df: pd.DataFrame, min_adx: float = 20.0) -> tuple[bool, float]:
    try:
        val = float(df["adx14"].iloc[-1]) if "adx14" in df.columns else 0.0
        return (val >= min_adx and not np.isnan(val)), val
    except Exception:
        return False, 0.0

def _volume_zscore(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float]:
    try:
        vols = df["volume"].tail(lookback)
        mean = float(vols.mean()); std = float(vols.std(ddof=0)) or 1e-9
        return (float(df["volume"].iloc[-1]) - mean) / std, mean
    except Exception:
        return 0.0, 0.0

def atr_stop_and_position(break_level: float, df: pd.DataFrame,
                           pattern_meta: dict | None = None,
                           capital: float = 100_000.0,
                           risk_pct: float = 0.01,
                           atr_mult: float = 1.5) -> tuple:
    """מחזיר (stop, target, size_shares, atr, rr_ratio)."""
    try:
        if df is None or df.empty:
            raise ValueError("empty df")
        if "atr14" not in df.columns:
            add_technical_indicators(df)
        entry   = max(float(break_level or 0), float(df["close"].iloc[-1]))
        atr     = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else 0.0

        if atr <= 0 or np.isnan(atr):
            stop = entry * 0.95
        else:
            stop = entry - atr_mult * atr
            stop = max(stop, entry * (1.0 - 0.25))   # max 25% stop

        # ── סטופ מינימלי: לפחות 1.5% מתחת לכניסה ──────────
        min_stop = entry * (1.0 - 0.015)
        stop = min(stop, min_stop)   # הסטופ לא יכול להיות קרוב יותר מ-1.5%

        # ── וודא שהסטופ תמיד מתחת לכניסה ───────────────────
        stop = min(stop, entry - (entry * 0.015))

        risk   = max(entry - stop, 1e-9)
        size   = int(capital * risk_pct / risk)

        # Double Bottom: target = start_price (תחילת התבנית)
        target_price = float((pattern_meta or {}).get("target_price") or 0.0)
        height       = float((pattern_meta or {}).get("pattern_height") or 0.0)
        if target_price > entry:
            target = target_price   # Double Bottom — יעד = start_price
        elif height > 0:
            target = entry + height
        else:
            target = entry + risk * 1.5

        rr     = (target - entry) / risk
        return float(stop), float(target), int(size), float(atr), round(float(rr), 2)
    except Exception as e:
        log(f"atr_stop_and_position error: {e}")
        entry = float(break_level or 0)
        return entry * 0.95, entry * 1.10, 0, 0.0, 0.0

# ============================================================
#  GLOBAL FILTER — EMA28 proximity check
# ============================================================
def _ema28_filter_ok(df: pd.DataFrame) -> tuple[bool, str]:
    """
    תנאי גלובלי: הסגירה לא יותר מ-EMA28_MAX_DIST_PCT מעל EMA28,
    ו-EMA28 בשיפוע עולה.
    מחזיר (ok, reason).
    """
    try:
        ensure_ma_columns(df)
        if len(df) < 3:
            return False, "not enough bars"
        close_now  = float(df["close"].iloc[-1])
        ema_now    = float(df["ema28"].iloc[-1])
        ema_prev   = float(df["ema28"].iloc[-2])
        if ema_now <= 0:
            return False, "ema28=0"
        dist = (close_now - ema_now) / ema_now
        if dist < 0:
            return False, f"price below EMA28 ({dist*100:.1f}%)"
        if dist > EMA28_MAX_DIST_PCT:
            return False, f"price too far above EMA28 ({dist*100:.1f}% > {EMA28_MAX_DIST_PCT*100:.0f}%)"
        if EMA28_REQUIRE_RISING and ema_now <= ema_prev:
            return False, f"EMA28 not rising ({ema_now:.2f} <= {ema_prev:.2f})"
        return True, "ok"
    except Exception as e:
        return False, f"ema28 filter error: {e}"

# ============================================================
#  QUALITY GATE FILTERS
# ============================================================

def _no_gap_filter(df: pd.DataFrame) -> tuple[bool, str]:
    """
    פוסל טיקר אם יש גאפ >= GAP_MAX_PCT (2%) בשבוע האחרון.
    גאפ = |open[i] - close[i-1]| / close[i-1]
    """
    try:
        window = df.tail(GAP_LOOKBACK_DAYS + 1).copy()
        if len(window) < 2:
            return True, "not enough bars"
        opens  = window["open"].values
        closes = window["close"].values
        for i in range(1, len(window)):
            prev_close = closes[i - 1]
            curr_open  = opens[i]
            if prev_close <= 0:
                continue
            gap_pct = abs(curr_open - prev_close) / prev_close
            if gap_pct >= GAP_MAX_PCT:
                date_str = str(window.index[i].date()) if hasattr(window.index[i], 'date') else str(window.index[i])
                return False, f"gap {gap_pct*100:.1f}% on {date_str}"
        return True, "no large gaps"
    except Exception as e:
        return True, f"gap check error: {e}"


def _volume_rising_filter(df: pd.DataFrame) -> tuple[bool, str]:
    """
    בודק שהvolume עלה VOL_RISING_DAYS ימים רצופים לפני היום.
    """
    try:
        if "volume" not in df.columns:
            return True, "no volume data"
        # נבדוק את VOL_RISING_DAYS הימים לפני הנר האחרון
        window = df["volume"].iloc[-(VOL_RISING_DAYS + 1):-1].values
        if len(window) < VOL_RISING_DAYS:
            return True, "not enough volume bars"
        rising = all(window[i] < window[i + 1] for i in range(len(window) - 1))
        if not rising:
            vals = ", ".join(f"{int(v):,}" for v in window)
            return False, f"volume not rising 3 days ({vals})"
        return True, f"volume rising {VOL_RISING_DAYS} days ✓"
    except Exception as e:
        return True, f"volume rising check error: {e}"


def _ma_near_neckline_filter(df: pd.DataFrame, neckline: float) -> tuple[bool, str]:
    """
    בודק שגם EMA28 וגם MA150 קרובים ל-neckline (בתוך MA_NEAR_NECK_PCT = 3%).
    """
    try:
        ensure_ma_columns(df)
        ema28  = float(df["ema28"].iloc[-1])
        ma150  = float(df["ma150"].iloc[-1]) if not pd.isna(df["ma150"].iloc[-1]) else None
        neck   = float(neckline)
        if neck <= 0:
            return False, "neckline=0"

        ema_dist = abs(ema28 - neck) / neck
        if ema_dist > MA_NEAR_NECK_PCT:
            return False, f"EMA28 {ema28:.2f} far from neckline {neck:.2f} ({ema_dist*100:.1f}%)"

        if ma150 is not None:
            ma_dist = abs(ma150 - neck) / neck
            if ma_dist > MA_NEAR_NECK_PCT:
                return False, f"MA150 {ma150:.2f} far from neckline {neck:.2f} ({ma_dist*100:.1f}%)"

        return True, f"EMA28={ema28:.2f} MA150={ma150:.2f} near neck={neck:.2f} ✓"
    except Exception as e:
        return True, f"ma near neckline check error: {e}"


def get_sector_analysis(ticker: str) -> dict:
    """
    מחזיר ניתוח סקטור:
    - שם הסקטור
    - ביצועי ETF ב-30 יום
    - כותרות חדשות (דרך yfinance news)
    - הערכה: STRONG / NEUTRAL / WEAK
    """
    result = {
        "sector":       "N/A",
        "etf":          None,
        "etf_perf_30d": None,
        "sentiment":    "NEUTRAL",
        "news":         [],
        "summary":      "לא נמצא מידע על הסקטור",
    }
    try:
        info   = _get_yf_info(ticker)
        sector = info.get("sector", "")
        if not sector:
            return result
        result["sector"] = sector

        # ETF הסקטור
        etf = SECTOR_ETF_MAP.get(sector)
        result["etf"] = etf

        if etf:
            etf_df = yf.download(etf, period="35d", interval="1d",
                                  progress=False, auto_adjust=True)
            if etf_df is not None and len(etf_df) >= 20:
                # handle MultiIndex
                if isinstance(etf_df.columns, pd.MultiIndex):
                    etf_df.columns = [c[0].lower() for c in etf_df.columns]
                else:
                    etf_df.columns = [c.lower() for c in etf_df.columns]
                close_col = "close" if "close" in etf_df.columns else etf_df.columns[0]
                price_now  = float(etf_df[close_col].iloc[-1])
                price_30d  = float(etf_df[close_col].iloc[-21]) if len(etf_df) >= 21 else float(etf_df[close_col].iloc[0])
                perf_30d   = (price_now - price_30d) / max(price_30d, 1e-9) * 100
                result["etf_perf_30d"] = round(perf_30d, 1)

                if perf_30d >= 3:
                    result["sentiment"] = "STRONG"
                elif perf_30d <= -3:
                    result["sentiment"] = "WEAK"
                else:
                    result["sentiment"] = "NEUTRAL"

        # חדשות על הסקטור דרך yfinance news של ה-ETF
        try:
            if etf:
                news_raw = yf.Ticker(etf).news or []
                headlines = []
                for item in news_raw[:4]:
                    title = item.get("title") or item.get("content", {}).get("title", "")
                    if title:
                        headlines.append(title)
                result["news"] = headlines
        except Exception:
            pass

        # סיכום
        perf_str = f"{result['etf_perf_30d']:+.1f}%" if result['etf_perf_30d'] is not None else "N/A"
        sent_emoji = {"STRONG": "🟢", "WEAK": "🔴", "NEUTRAL": "🟡"}.get(result["sentiment"], "🟡")
        result["summary"] = f"{sent_emoji} {sector} ({etf or 'N/A'}): {perf_str} ב-30 יום"

    except Exception as e:
        result["summary"] = f"שגיאה בניתוח סקטור: {e}"

    return result


# ============================================================
#  PATTERN DETECTION
# ============================================================
def is_cup_and_handle(df: pd.DataFrame) -> tuple[bool, float | None, dict]:
    """
    Cup & Handle — הגדרה מדויקת:

    כוס:
      1. ירידה מ-left_peak לתחתית, ועלייה חזרה ל-right_peak
      2. left_peak ≈ right_peak בתוך 1% — קצות הכוס באותו מחיר
      3. צורת U: שיפוע הירידה שונה מהעלייה (לא V סמטרי) —
         בודק שהתחתית מתפרסת על לפחות 30% מאורך הכוס
      4. תחתית הכוס מופיעה בין 30%-70% מאורך הכוס (לא קצה)

    ידית:
      5. ירידה מ-right_peak כלשהי
      6. תחתית הידית מעל תחתית הכוס — חובה
      7. ידית מתחילה אחרי right_peak

    פריצה:
      8. המחיר היום 0–0.5% מעל right_peak (= neckline)
    """
    try:
        window = df.tail(CH_LOOKBACK).copy()
        closes = window["close"].values
        highs  = window["high"].values
        lows   = window["low"].values
        n = len(closes)
        if n < CH_MIN_CUP_BARS + CH_HANDLE_MIN_BARS + 5:
            return False, None, {"reason": "not enough bars"}

        # ── שלב 1: מצא left_peak ─────────────────────────────
        # חיפוש בחצי הראשון של החלון
        left_half = closes[:n // 2]
        left_peak_idx = int(np.argmax(left_half))
        left_peak     = float(closes[left_peak_idx])
        if left_peak <= 0:
            return False, None, {"reason": "no left peak"}

        # ── שלב 2: תחתית הכוס ───────────────────────────────
        cup_section = lows[left_peak_idx:]
        if len(cup_section) < CH_MIN_CUP_BARS:
            return False, None, {"reason": "cup too short"}
        cup_bottom_rel = int(np.argmin(cup_section))
        cup_bottom_idx = left_peak_idx + cup_bottom_rel
        cup_bottom     = float(lows[cup_bottom_idx])
        cup_length     = n - 1 - left_peak_idx  # ימים מ-left_peak עד היום

        # ── שלב 3: בדיקת U — תחתית לא בקצה (30%-70%) ───────
        relative_pos = cup_bottom_rel / max(cup_length, 1)
        if relative_pos < 0.20 or relative_pos > 0.80:
            return False, None, {"reason": f"cup bottom at edge ({relative_pos*100:.0f}%) — V shape suspected"}

        # ── שלב 4: בדיקת U — שיפוע ירידה ≠ שיפוע עלייה ────
        # ירידה: left_peak → cup_bottom
        descent_bars  = cup_bottom_idx - left_peak_idx
        descent_slope = (cup_bottom - left_peak) / max(descent_bars, 1)

        # עלייה: cup_bottom → סוף חלון הכוס (לפני הידית)
        cup_end_idx   = min(cup_bottom_idx + descent_bars, n - 1)
        ascent_prices = closes[cup_bottom_idx:cup_end_idx + 1]
        if len(ascent_prices) < 3:
            return False, None, {"reason": "ascent too short"}
        ascent_slope = (ascent_prices[-1] - cup_bottom) / max(len(ascent_prices) - 1, 1)

        # V-shape: שני השיפועים כמעט זהים בגודל
        if descent_slope != 0:
            slope_ratio = abs(ascent_slope / descent_slope)
            # אם שניהם כמעט אותו שיפוע — זה V, לא U
            if 0.75 <= slope_ratio <= 1.35:
                return False, None, {"reason": f"V-shape detected (slope ratio={slope_ratio:.2f})"}

        # ── שלב 5: right_peak (= neckline) ───────────────────
        # מצא את הפסגה הגבוהה ביותר אחרי תחתית הכוס
        right_section = closes[cup_bottom_idx:]
        # חפש ב-2/3 הראשונים של החלק הימני (לפני הידית)
        search_end    = max(cup_bottom_idx + len(right_section) * 2 // 3, cup_bottom_idx + CH_HANDLE_MIN_BARS + 2)
        search_end    = min(search_end, n - CH_HANDLE_MIN_BARS - 1)
        if search_end <= cup_bottom_idx:
            return False, None, {"reason": "no room for right peak + handle"}
        right_peak_rel = int(np.argmax(closes[cup_bottom_idx:search_end]))
        right_peak_idx = cup_bottom_idx + right_peak_rel
        neckline       = float(closes[right_peak_idx])

        # ── שלב 6: קצות הכוס ≈ אותו מחיר (בתוך CH_PEAKS_MAX_DIFF_PCT) ──
        peaks_diff = abs(neckline - left_peak) / max(left_peak, 1e-9)
        if peaks_diff > CH_PEAKS_MAX_DIFF_PCT:
            return False, None, {"reason": f"cup rims differ {peaks_diff*100:.1f}% (max {CH_PEAKS_MAX_DIFF_PCT*100:.0f}%)"}

        # ── שלב 7: ידית ─────────────────────────────────────
        handle_section_closes = closes[right_peak_idx:]
        handle_section_lows   = lows[right_peak_idx:]
        if len(handle_section_closes) < CH_HANDLE_MIN_BARS:
            return False, None, {"reason": "handle too short"}

        handle_low     = float(np.min(handle_section_lows))
        handle_drop    = (neckline - handle_low) / max(neckline, 1e-9)

        if handle_drop < CH_HANDLE_MIN_PCT:
            return False, None, {"reason": f"handle drop too small ({handle_drop*100:.1f}%)"}

        # ── שלב 8: תחתית הידית מעל תחתית הכוס ─────────────
        if handle_low <= cup_bottom:
            return False, None, {"reason": f"handle bottom ({handle_low:.2f}) below cup bottom ({cup_bottom:.2f})"}

        # ── שלב 9: פריצה 0–0.5% מעל neckline ───────────────
        price_now = float(df["close"].iloc[-1])
        if price_now < neckline:
            return False, None, {"reason": f"no breakout yet ({price_now:.2f} < {neckline:.2f})"}
        overbreak = (price_now - neckline) / max(neckline, 1e-9)
        if overbreak > BREAKOUT_TOLERANCE:
            return False, None, {"reason": f"overextended ({overbreak*100:.1f}% > {BREAKOUT_TOLERANCE*100:.1f}%)"}

        return True, neckline, {
            "neckline":        neckline,
            "cup_bottom":      cup_bottom,
            "left_peak":       left_peak,
            "handle_low":      handle_low,
            "handle_drop_pct": float(handle_drop),
            "peaks_diff_pct":  float(peaks_diff),
            "slope_ratio":     float(slope_ratio) if descent_slope != 0 else 0,
            "cup_bottom_pos":  float(relative_pos),
            "pattern_height":  float(neckline - cup_bottom),
        }
    except Exception as e:
        log(f"is_cup_and_handle error: {e}")
        return False, None, {"reason": str(e)}
def detect_bullish_triangle(df: pd.DataFrame) -> tuple[bool, float | None, dict]:
    """
    Ascending Triangle — הגדרה מדויקת:

    קו התנגדות אופקי (flat resistance):
      - לפחות 3 נגיעות בפסגות (highs) בטווח TRIANGLE_PRICE_TOL מהקו
      - הקו חייב להיות אופקי (slope ≈ 0) — לא יורד

    קו תמיכה עולה (rising lows):
      - לפחות 2 שפלים עולים (higher lows)
      - מרחק מינימלי בין שפלים

    פריצה:
      - הנגיעה ה-4 בקו האופקי — המחיר פורץ מעליו 0–0.5%
    """
    try:
        lookback = max(TRIANGLE_LOOKBACK, 30)
        window   = df.tail(lookback).copy()
        highs_s  = window["high"]
        lows_s   = window["low"]
        n        = len(window)
        if n < 30:
            return False, None, {"reason": "not enough bars"}

        price_now = float(df["close"].iloc[-1])

        # ── מצא פסגות מקומיות ───────────────────────────────
        peak_idxs = argrelextrema(highs_s.values, np.greater, order=TRIANGLE_PEAK_ORDER)[0]
        peak_idxs = [i for i in peak_idxs if i < n - 1]
        if len(peak_idxs) < 2:
            return False, None, {"reason": "not enough peaks for resistance line"}

        # ── מצא שפלים מקומיים ───────────────────────────────
        trough_idxs = argrelextrema(lows_s.values, np.less, order=TRIANGLE_PEAK_ORDER)[0]
        trough_idxs = [i for i in trough_idxs if i < n - 1]

        best = None

        # ── בנה קו התנגדות מכל זוג פסגות ───────────────────
        for i in range(len(peak_idxs) - 1):
            for j in range(i + 1, len(peak_idxs)):
                pi, pj = peak_idxs[i], peak_idxs[j]
                hp_i   = float(highs_s.iloc[pi])
                hp_j   = float(highs_s.iloc[pj])

                # קו חייב להיות אופקי — slope קרוב ל-0
                slope = (hp_j - hp_i) / max(pj - pi, 1)
                max_slope = 0.003 * hp_i / max(n, 1)  # סטייה מקסימלית 0.3% לנר
                if abs(slope) > max_slope:
                    continue  # לא אופקי מספיק

                # neckline = ממוצע הפסגות (קו אופקי)
                neckline = (hp_i + hp_j) / 2.0
                if neckline <= 0:
                    continue

                # ── ספור נגיעות בקו האופקי — חייב לפחות 3 ──
                touches = sum(
                    1 for k in peak_idxs
                    if abs(float(highs_s.iloc[k]) - neckline) / max(neckline, 1e-9) <= TRIANGLE_PRICE_TOL
                )
                if touches < 3:  # חובה 3 נגיעות בקו האופקי
                    continue

                # הפסגה האחרונה לא יותר מ-TRIANGLE_MAX_LAST_PEAK_DAYS ימים אחורה
                days_since_last_peak = n - 1 - pj
                if days_since_last_peak > TRIANGLE_MAX_LAST_PEAK_DAYS:
                    continue

                # ── בדוק Higher Lows — לפחות 2 שפלים עולים ─
                troughs_in = [k for k in trough_idxs if pi <= k <= n - 2]
                if len(troughs_in) < 2:
                    continue  # חייב לפחות 2 שפלים
                trough_prices = [float(lows_s.iloc[k]) for k in troughs_in]
                # בדוק שהשפלים עולים
                if not all(trough_prices[x] < trough_prices[x+1]
                           for x in range(len(trough_prices)-1)):
                    continue

                # מרחק מינימלי בין שפלים
                if len(troughs_in) >= 2:
                    min_gap = min(troughs_in[k+1] - troughs_in[k]
                                  for k in range(len(troughs_in)-1))
                    if min_gap < 5:  # לפחות 5 ימים בין שפלים
                        continue

                # ── פריצה: 0–0.5% מעל הקו האופקי ───────────
                if price_now < neckline:
                    continue
                overbreak = (price_now - neckline) / max(neckline, 1e-9)
                if overbreak > BREAKOUT_TOLERANCE:
                    continue

                candidate = {
                    "neckline":          neckline,
                    "resistance_slope":  float(slope),
                    "touches":           int(touches),
                    "higher_lows_count": len(troughs_in),
                    "days_since_peak":   int(days_since_last_peak),
                    "pattern_height":    float(neckline - min(trough_prices)),
                }
                # quality: יותר נגיעות + פסגה קרובה
                quality = touches * 10 - days_since_last_peak + len(troughs_in) * 3
                if best is None or quality > best.get("_quality", -999):
                    candidate["_quality"] = quality
                    best = candidate

        if best is None:
            return False, None, {"reason": "no valid ascending triangle"}

        best.pop("_quality", None)
        return True, best["neckline"], best

    except Exception as e:
        log(f"detect_bullish_triangle error: {e}")
        return False, None, {"reason": str(e)}
def detect_double_bottom(df: pd.DataFrame) -> tuple[bool, float | None, dict]:
    """
    Double Bottom — אות W מדויקת:

    מבנה W:
      ירידה מ-start_price → שפל 1 → עלייה ל-mid_peak (40%-60% מהדרך) →
      ירידה → שפל 2 (בגובה שפל 1 ± DB_BOTTOM_DIFF_PCT) → פריצת mid_peak

    תנאים:
      1. שני שפלים קרובים — הפרש מקסימלי DB_BOTTOM_DIFF_PCT
      2. mid_peak בין 40%-60% מהמרחק בין שפל 1 ל-start_price
      3. פריצה 0–0.5% מעל mid_peak
      4. יעד: start_price (תחילת התבנית)
      5. מרחק מינימלי DB_MIN_BARS_BETWEEN בין השפלים
      6. עומק מינימלי DB_MIN_DEPTH_PCT
    """
    try:
        if df is None or len(df) < DB_LOOKBACK // 2:
            return False, None, {"reason": "not enough data"}

        window  = df.tail(DB_LOOKBACK).copy()
        lows_s  = window["low"]
        highs_s = window["high"]
        closes  = window["close"]
        volumes = window["volume"] if "volume" in window.columns else None
        n       = len(window)

        trough_idx = argrelextrema(lows_s.values, np.less, order=DB_TROUGH_ORDER)[0]
        if len(trough_idx) < 2:
            return False, None, {"reason": "not enough troughs"}

        confirm_price = float(closes.iloc[-1])
        best = None

        for i in range(len(trough_idx) - 1):
            for j in range(i + 1, len(trough_idx)):
                b1_idx = int(trough_idx[i])
                b2_idx = int(trough_idx[j])
                b1     = float(lows_s.iloc[b1_idx])
                b2     = float(lows_s.iloc[b2_idx])

                # ── תנאי 1: שני שפלים קרובים (±DB_BOTTOM_DIFF_PCT) ──
                diff_pct = abs(b1 - b2) / max(b1, 1e-9)
                if diff_pct > DB_BOTTOM_DIFF_PCT:
                    continue

                # ── תנאי 2: מרחק זמן מינימלי ────────────────
                bars_between = b2_idx - b1_idx
                if bars_between < DB_MIN_BARS_BETWEEN:
                    continue

                # ── start_price: שיא לפני שפל 1 ──────────────
                # מחפשים את השיא בחלון שלפני שפל 1
                pre_b1 = closes.iloc[:b1_idx + 1]
                if len(pre_b1) < 3:
                    continue
                start_price = float(pre_b1.max())

                # ── mid_peak: השיא בין שני השפלים ────────────
                mid_section_highs = highs_s.iloc[b1_idx:b2_idx + 1]
                mid_peak          = float(mid_section_highs.max())
                mid_peak_idx      = b1_idx + int(mid_section_highs.values.argmax())

                # mid_peak חייב להיות לפני שפל 2
                if mid_peak_idx >= b2_idx:
                    continue

                # ── תנאי 3: mid_peak בין 40%-60% מהמרחק שפל→start ──
                total_range = start_price - min(b1, b2)
                if total_range <= 0:
                    continue
                mid_ratio = (mid_peak - min(b1, b2)) / total_range
                if not (0.40 <= mid_ratio <= 0.60):
                    continue  # האמצע לא ב-40%-60% — לא W אמיתי

                # ── תנאי 4: עומק מינימלי ─────────────────────
                depth_pct = (mid_peak - b2) / max(mid_peak, 1e-9)
                if depth_pct < DB_MIN_DEPTH_PCT:
                    continue

                # ── תנאי 5: Volume בשפל 2 נמוך משפל 1 ────────
                if DB_REQUIRE_LOWER_VOL and volumes is not None:
                    vol1 = float(volumes.iloc[b1_idx])
                    vol2 = float(volumes.iloc[b2_idx])
                    if vol2 >= vol1:
                        continue

                # ── תנאי 6: אין שפל נמוך יותר אחרי שפל 2 ────
                after_b2 = lows_s.iloc[b2_idx + 1:]
                if len(after_b2) > 0 and float(after_b2.min()) < b2 * (1 - 0.01):
                    continue

                # ── תנאי 7: פריצה 0–0.5% מעל mid_peak ───────
                if confirm_price < mid_peak:
                    continue
                overbreak = (confirm_price - mid_peak) / max(mid_peak, 1e-9)
                if overbreak > BREAKOUT_TOLERANCE:
                    continue

                # ── יעד = start_price (תחילת התבנית) ─────────
                target_move = start_price - mid_peak

                candidate = {
                    "pattern_type":   "Double Bottom",
                    "neckline":       mid_peak,
                    "bottom1":        b1,
                    "bottom2":        b2,
                    "bottom1_idx":    b1_idx,
                    "bottom2_idx":    b2_idx,
                    "mid_peak":       mid_peak,
                    "mid_peak_idx":   mid_peak_idx,
                    "start_price":    start_price,
                    "target_price":   float(start_price),
                    "bars_between":   bars_between,
                    "depth_pct":      float(depth_pct),
                    "diff_pct":       float(diff_pct),
                    "mid_ratio":      float(mid_ratio),
                    "pattern_height": float(target_move),
                    "higher_low":     b2 > b1,
                }
                quality = (depth_pct * 0.3
                           + (1 - diff_pct) * 0.3
                           + (bars_between / DB_LOOKBACK) * 0.2
                           + (0.2 if 0.45 <= mid_ratio <= 0.55 else 0.1))
                if best is None or quality > best.get("_quality", -1):
                    candidate["_quality"] = quality
                    best = candidate

        if best is None:
            return False, None, {"reason": "no valid W pattern found"}

        best.pop("_quality", None)
        return True, best["neckline"], best

    except Exception as e:
        log(f"detect_double_bottom error: {e}")
        return False, None, {"reason": str(e)}
def _is_close_to_level(p1: float, p2: float, tol: float = TRIANGLE_PRICE_TOL) -> bool:
    return bool(p2) and abs(p1 - p2) / p2 <= tol

def check_for_consolidation_breakout(df: pd.DataFrame, ticker: str) -> list[dict]:
    """
    Falling Wedge — הגדרה מדויקת (bullish):

    מבנה:
      - שני קווים יורדים ומתכנסים
      - קו עליון (resistance): lower highs — לפחות 2 נגיעות
      - קו תחתון (support): lower lows — לפחות 2 נגיעות
      - קו עליון יורד בתלילות גבוהה יותר מהתחתון (convergence)
      - Volume יורד בתוך התבנית

    פריצה:
      - המחיר פורץ מעל הקו העליון 0–0.5%
      - הפריצה בולישית — למעלה
    """
    alerts = []
    if df is None or df.empty or len(df) < TRIANGLE_LOOKBACK:
        return alerts

    window    = df.iloc[-TRIANGLE_LOOKBACK:].copy()
    highs_s   = window["high"]
    lows_s    = window["low"]
    closes_s  = window["close"]
    n         = len(window)
    price_now = float(closes_s.iloc[-1])

    # מצא פסגות ושפלים מקומיים
    peak_idxs   = argrelextrema(highs_s.values, np.greater, order=TRIANGLE_PEAK_ORDER)[0]
    trough_idxs = argrelextrema(lows_s.values,  np.less,    order=TRIANGLE_PEAK_ORDER)[0]
    peak_idxs   = [i for i in peak_idxs   if i < n - 1]
    trough_idxs = [i for i in trough_idxs if i < n - 1]

    if len(peak_idxs) < 2 or len(trough_idxs) < 2:
        return alerts

    best = None

    # בנה קו עליון יורד מכל זוג פסגות
    for ii in range(len(peak_idxs) - 1):
        for jj in range(ii + 1, len(peak_idxs)):
            pi, pj = peak_idxs[ii], peak_idxs[jj]
            hp_i   = float(highs_s.iloc[pi])
            hp_j   = float(highs_s.iloc[pj])

            # קו עליון חייב לרדת (lower highs)
            upper_slope = (hp_j - hp_i) / max(pj - pi, 1)
            if upper_slope >= 0:
                continue  # לא Falling Wedge — הקו עולה

            # neckline (קו עליון) ביום הנוכחי
            neckline = hp_j + upper_slope * (n - 1 - pj)
            if neckline <= 0:
                continue

            # פריצה 0–0.5% מעל הקו העליון
            if price_now < neckline:
                continue
            overbreak = (price_now - neckline) / max(neckline, 1e-9)
            if overbreak > BREAKOUT_TOLERANCE:
                continue

            # מצא קו תחתון יורד — lower lows
            # בחר שפלים שנמצאים בתוך החלון של הפסגות
            troughs_in = [k for k in trough_idxs if pi <= k <= n - 2]
            if len(troughs_in) < 2:
                continue

            # חשב slope של הקו התחתון
            lp_first_idx = troughs_in[0]
            lp_last_idx  = troughs_in[-1]
            lp_first     = float(lows_s.iloc[lp_first_idx])
            lp_last      = float(lows_s.iloc[lp_last_idx])
            lower_slope  = (lp_last - lp_first) / max(lp_last_idx - lp_first_idx, 1)

            # קו תחתון חייב לרדת (lower lows)
            if lower_slope >= 0:
                continue

            # Convergence: קו עליון יורד בתלילות גבוהה יותר מהתחתון
            # upper_slope < lower_slope (שניהם שליליים, upper יותר שלילי)
            if upper_slope >= lower_slope:
                continue  # לא מתכנסים — זה channel, לא wedge

            # בדוק שהקווים אכן מתכנסים (לא מתרחקים)
            gap_start = hp_i - lp_first
            gap_end   = neckline - (lp_last + lower_slope * (n - 1 - lp_last_idx))
            if gap_end >= gap_start:
                continue  # לא מתכנסים

            # הפסגה האחרונה לא יותר מ-TRIANGLE_MAX_LAST_PEAK_DAYS ימים אחורה
            days_since = n - 1 - pj
            if days_since > TRIANGLE_MAX_LAST_PEAK_DAYS:
                continue

            # Volume יורד בתוך התבנית (אופציונלי — bonus)
            vol_declining = False
            if "volume" in window.columns:
                vol_in = window["volume"].iloc[pi:n-1].values
                if len(vol_in) >= 4:
                    half = len(vol_in) // 2
                    vol_declining = vol_in[:half].mean() > vol_in[half:].mean()

            candidate = {
                "pattern_type":    "Falling Wedge",
                "breakout_level":  float(neckline),
                "upper_slope":     float(upper_slope),
                "lower_slope":     float(lower_slope),
                "touches_upper":   2,
                "touches_lower":   len(troughs_in),
                "days_since_peak": int(days_since),
                "vol_declining":   vol_declining,
                "pattern_height":  float(gap_start),
                "score_reasons":   [
                    f"Falling Wedge — קו עליון slope={upper_slope:.4f}",
                    f"Lower lows: {len(troughs_in)} שפלים",
                    f"{'Volume יורד בתבנית ✓' if vol_declining else 'Volume N/A'}",
                ],
            }
            quality = (2 * 10) + len(troughs_in) * 5 - days_since + (5 if vol_declining else 0)
            if best is None or quality > best.get("_quality", -999):
                candidate["_quality"] = quality
                best = candidate

    if not best:
        return alerts

    best.pop("_quality", None)
    score = compute_setup_score(df, ticker, best["breakout_level"], len(df) - 1, best)
    if score < MIN_ALERT_SCORE:
        return alerts

    stop, target, size, atr, rr = atr_stop_and_position(best["breakout_level"], df, best)
    if rr < 2.5:
        return alerts

    alerts.append({
        "ticker":         ticker,
        "phase":          2,
        "pattern_type":   best["pattern_type"],
        "breakout_level": best["breakout_level"],
        "score":          score,
        "stop_loss":      stop,
        "target":         target,
        "rr_ratio":       rr,
        "meta": {**best,
                 "score_reasons": [],
                 "atr14":         float(atr),
                 "position_size": int(size)},
    })
    return alerts


# ============================================================
#  DEDUPLICATION
# ============================================================
def _norm_level(level) -> float | None:
    try:
        lv = float(level)
        return round(lv, 2) if abs(lv) >= 1 else round(lv, 4)
    except Exception:
        return None

def alert_already_sent(ticker: str, pattern_name: str, break_level,
                        history: dict, cooldown_hours: int = DEDUP_ALERT_HOURS,
                        level_sim_pct: float = ALERT_DEDUP_LEVEL_PCT) -> bool:
    """בדיקה האם ההתראה כבר נשלחה לאחרונה (לפי cooldown ורמת מחיר דומה)."""
    try:
        norm = _norm_level(break_level)
        key  = f"{ticker}|{pattern_name}|{norm}"
        if key in recent_sent:
            return True
        # dedup key style 2 (מקובץ 2)
        if break_level and ALERT_DEDUP_LEVEL_PCT > 0:
            bucket = int(float(break_level) / max(float(break_level) * ALERT_DEDUP_LEVEL_PCT, 1e-9))
            key2   = f"{ticker}_{pattern_name}_{bucket}"
        else:
            key2 = f"{ticker}_{pattern_name}_0"
        rec = history.get(ticker) or history.get(key2)
        if not rec:
            return False
        now_ts = datetime.now().timestamp()
        # history style 1 (nested patterns)
        if isinstance(rec, dict) and "patterns" in rec:
            for e in rec["patterns"]:
                if not isinstance(e, dict) or e.get("name") != pattern_name:
                    continue
                t = float(e.get("time", 0))
                if now_ts - t >= cooldown_hours * 3600:
                    continue
                el = e.get("level")
                if el is None or break_level is None:
                    return True
                if abs(float(el) - float(break_level)) / max(abs(float(el)), 1e-9) < level_sim_pct:
                    return True
        # history style 2 (ISO string)
        if isinstance(rec, str):
            try:
                last_dt = datetime.fromisoformat(rec)
                if (datetime.now() - last_dt) < timedelta(hours=cooldown_hours):
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False

def record_alert_sent(ticker: str, pattern_name: str, break_level,
                       history: dict) -> None:
    try:
        now_ts  = datetime.now().timestamp()
        now_iso = datetime.now().isoformat()
        entry   = {"name": pattern_name, "level": float(break_level) if break_level else None, "time": now_ts}
        prev    = history.get(ticker, {})
        patterns = [p for p in prev.get("patterns", []) if isinstance(p, dict)]
        patterns = [p for p in patterns if p.get("time", 0) >= now_ts - 30*24*3600]
        patterns.append(entry)
        history[ticker] = {"time": now_ts, "patterns": patterns}
        # style-2 key: price-level bucket so same ticker+pattern at very different prices are separate
        if break_level and ALERT_DEDUP_LEVEL_PCT > 0:
            bucket = int(float(break_level) / max(float(break_level) * ALERT_DEDUP_LEVEL_PCT, 1e-9))
            history[f"{ticker}_{pattern_name}_{bucket}"] = now_iso
        recent_sent.add(f"{ticker}|{pattern_name}|{_norm_level(break_level)}")
    except Exception as e:
        log(f"record_alert_sent error: {e}")

# ============================================================
#  CORE SCAN
# ============================================================
def scan_ticker(ticker: str, alert_history: dict, filter_stats: dict | None = None, min_score: float | None = None) -> list[dict]:
    """
    מבצע את שתי הפאזות על טיקר אחד ומחזיר רשימת התראות חדשות.
    Phase 1: Cup&Handle / Bullish-Triangle / Double Bottom (+ EMA28 global filter)
    Phase 2: Falling Wedge
    """
    symbol = ticker.strip().upper().replace("$", "")
    log(f"Scanning {symbol}...")
    effective_min_score = min_score if min_score is not None else MIN_ALERT_SCORE

    candidates: list[dict] = []   # כל הסטאפים שנמצאו
    def _fs(key: str):
        """עדכון counter ב-filter_stats."""
        if filter_stats is not None and key in filter_stats:
            filter_stats[key] += 1

    # --- Market Cap ---
    mc = fetch_market_cap(symbol)
    if mc is not None and mc < MIN_MARKET_CAP_USD:
        log(f"{symbol}: market cap too low (${mc/1e6:.0f}M < $1B). Skip."); _fs("market_cap"); return []
    # אם mc=None (לא זמין) — ממשיכים, לא דוחים

    # --- Data ---
    df = fetch_data_twelvedata(symbol, outputsize=500)
    if df is None or df.empty:
        df = fetch_data_yfinance(symbol)
    if df is None or df.empty or len(df) < 50:
        log(f"{symbol}: no data. Skip."); _fs("no_data"); return []

    # ── פילטר דוחות: skip אם דוח קרוב ────────────────────────
    earn_ok, earn_reason = earnings_filter_ok(symbol)
    if not earn_ok:
        log(f"{symbol}: ⏭️ Earnings filter — {earn_reason}"); _fs("earnings"); return []
    elif DEBUG_SCAN_REASONS and "unknown" not in earn_reason:
        log(f"{symbol}: 📅 {earn_reason}")

    # ── Double Bottom מוקדם — לפני EMA28 (תבנית שבונה מתחת לממוצע) ──
    # Double Bottom יכול להיבנות מתחת ל-EMA28 ולפרוץ ממנו — לכן בודקים לפני
    # חשב price_now ו-ma150_val כבר כאן לצורך DB early
    ensure_ma_columns(df)
    price_now  = float(df["close"].iloc[-1])
    ma150_val  = float(df["ma150"].iloc[-1]) if "ma150" in df.columns and not pd.isna(df["ma150"].iloc[-1]) else np.nan
    ma_dist    = abs(price_now - ma150_val) / ma150_val if not np.isnan(ma150_val) and ma150_val else np.inf
    try:
        ok_db_early, neck_db_early, meta_db_early = detect_double_bottom(df)
        if ok_db_early and neck_db_early:
            _ma_dist_early = abs(price_now - ma150_val) / ma150_val if not np.isnan(ma150_val) and ma150_val else np.inf
            if _ma_dist_early <= MA150_MAX_DISTANCE_DB:
                ma_ok_e, _ = _ma_near_neckline_filter(df, neck_db_early)
                if ma_ok_e:
                    meta_db_early["pattern_type"] = "Double Bottom"
                    score_e = compute_setup_score(df, symbol, neck_db_early, len(df)-1, meta_db_early)
                    if score_e >= effective_min_score:
                        stop_e, target_e, size_e, atr_e, rr_e = atr_stop_and_position(neck_db_early, df, meta_db_early)
                        if rr_e >= 2.5:
                            log(f"{symbol}: ✅ Double Bottom (early) score={score_e:.0f}")
                            candidates.append({
                                "ticker": symbol, "phase": 1,
                                "pattern_type": "Double Bottom",
                                "breakout_level": neck_db_early,
                                "score": score_e, "stop_loss": stop_e,
                                "target": target_e, "rr_ratio": rr_e,
                                "meta": {**meta_db_early, "position_size": size_e, "atr14": atr_e},
                            })
    except Exception as e:
        log(f"{symbol} Double Bottom early error: {e}")

    # ── פילטר גלובלי: EMA28 — מדלג אם Double Bottom כבר נמצא ──
    if not candidates:
        ema_ok, ema_reason = _ema28_filter_ok(df)
        if not ema_ok:
            if DEBUG_SCAN_REASONS:
                log(f"{symbol}: ❌ EMA28 global filter — {ema_reason}")
            _fs("ema28"); return []

    # ── Reverse Scanner — בדוק מכירה מוסדית ────────────────
    try:
        reverse = scan_for_institutional_selling(symbol, df)
        if reverse.get("avoid"):
            if DEBUG_SCAN_REASONS:
                log(f"{symbol}: ❌ Reverse Scanner — {reverse['summary']}")
            # ── Watchlist: עבר EMA28+MA150 אבל נעצר ע"י Reverse Scanner ──
            try:
                log_watchlist(
                    symbol=symbol, price=price_now,
                    reason="מכירה מוסדית — Reverse Scanner",
                    details=reverse.get("summary", ""),
                )
            except Exception:
                pass
            _fs("reverse_scan")
            return []
    except Exception:
        pass


    rs_data = {}  # RS Score מחושב בשלב המייל בלבד

    # ====== PHASE 1 ======

    # Cup & Handle
    try:
        ok_ch, neck_ch, meta_ch = is_cup_and_handle(df)
        if ok_ch and neck_ch and ma_dist <= MA150_MAX_DISTANCE:
            ma_ok, ma_reason = _ma_near_neckline_filter(df, neck_ch)
            if not ma_ok:
                if DEBUG_SCAN_REASONS: log(f"{symbol}: Cup & Handle — MA not near neckline: {ma_reason}")
                _fs("ma_neckline")
            else:
                if DEBUG_SCAN_REASONS: log(f"{symbol}: Cup & Handle MA check ✓ — {ma_reason}")
                meta_ch["pattern_type"] = "Cup & Handle"
                score = compute_setup_score(df, symbol, neck_ch, len(df)-1, meta_ch)
                if score >= effective_min_score:
                    stop, target, size, atr, rr = atr_stop_and_position(neck_ch, df, meta_ch)
                    candidates.append({
                        "ticker": symbol, "phase": 1,
                        "pattern_type": "Cup & Handle",
                        "breakout_level": neck_ch,
                        "score": score, "stop_loss": stop, "target": target, "rr_ratio": rr,
                        "meta": {**meta_ch, "position_size": size, "atr14": atr},
                    })
                    if DEBUG_SCAN_REASONS: log(f"{symbol}: ✅ Cup & Handle — score={score:.1f} neck={neck_ch:.2f}")
                elif DEBUG_SCAN_REASONS:
                    log(f"{symbol}: Cup & Handle — score too low ({score:.1f})")
                # ── Watchlist: תבנית נמצאה — ציון נמוך או קרוב לפריצה ──
                try:
                    dist_from_breakout = (neck_ch - price_now) / neck_ch if neck_ch > 0 else 0
                    if dist_from_breakout >= 0 and dist_from_breakout <= WATCHLIST_NEAR_PCT:
                        log_watchlist(
                            symbol=symbol, price=price_now,
                            reason=f"מחיר {dist_from_breakout*100:.1f}% מתחת לפריצה",
                            pattern="Cup & Handle", breakout=neck_ch, score=score,
                        )
                    elif score > 0:
                        log_watchlist(
                            symbol=symbol, price=price_now,
                            reason=f"ציון נמוך מהסף ({score:.1f} < {effective_min_score})",
                            pattern="Cup & Handle", breakout=neck_ch, score=score,
                        )
                except Exception:
                    pass
        elif DEBUG_SCAN_REASONS:
            reason = meta_ch.get("reason","") if isinstance(meta_ch, dict) else ""
            if not ok_ch:
                log(f"{symbol}: Cup & Handle — {reason}")
            else:
                log(f"{symbol}: Cup & Handle — MA150 too far ({ma_dist*100:.1f}%)"); _fs("ma150_dist")
    except Exception as e:
        log(f"{symbol} Cup&Handle error: {e}")

    # Bullish Triangle
    try:
        ok_tri, neck_tri, meta_tri = detect_bullish_triangle(df)
        if ok_tri and neck_tri and ma_dist <= MA150_MAX_DISTANCE:
            ma_ok, ma_reason = _ma_near_neckline_filter(df, neck_tri)
            if not ma_ok:
                if DEBUG_SCAN_REASONS: log(f"{symbol}: Bullish Triangle — MA not near neckline: {ma_reason}")
                _fs("ma_neckline")
            else:
                meta_tri["pattern_type"] = "Bullish Triangle"
                score = compute_setup_score(df, symbol, neck_tri, len(df)-1, meta_tri)
                if score >= effective_min_score:
                    stop, target, size, atr, rr = atr_stop_and_position(neck_tri, df, meta_tri)
                    candidates.append({
                        "ticker": symbol, "phase": 1,
                        "pattern_type": "Bullish Triangle",
                        "breakout_level": neck_tri,
                        "score": score, "stop_loss": stop, "target": target, "rr_ratio": rr,
                        "meta": {**meta_tri, "position_size": size, "atr14": atr},
                    })
                    if DEBUG_SCAN_REASONS: log(f"{symbol}: ✅ Bullish Triangle — score={score:.1f} neck={neck_tri:.2f}")
                elif DEBUG_SCAN_REASONS:
                    log(f"{symbol}: Bullish Triangle — score too low ({score:.1f})")
                # ── Watchlist ──
                try:
                    dist_from_breakout = (neck_tri - price_now) / neck_tri if neck_tri > 0 else 0
                    if dist_from_breakout >= 0 and dist_from_breakout <= WATCHLIST_NEAR_PCT:
                        log_watchlist(
                            symbol=symbol, price=price_now,
                            reason=f"מחיר {dist_from_breakout*100:.1f}% מתחת לפריצה",
                            pattern="Bullish Triangle", breakout=neck_tri, score=score,
                        )
                    elif score > 0:
                        log_watchlist(
                            symbol=symbol, price=price_now,
                            reason=f"ציון נמוך מהסף ({score:.1f} < {effective_min_score})",
                            pattern="Bullish Triangle", breakout=neck_tri, score=score,
                        )
                except Exception:
                    pass
        elif DEBUG_SCAN_REASONS:
            reason = meta_tri.get("reason","") if isinstance(meta_tri, dict) else ""
            if not ok_tri:
                log(f"{symbol}: Bullish Triangle — {reason}")
            else:
                log(f"{symbol}: Bullish Triangle — MA150 too far ({ma_dist*100:.1f}%)")
    except Exception as e:
        log(f"{symbol} BullishTriangle error: {e}")

    # Double Bottom
    try:
        ok_db, neck_db, meta_db = detect_double_bottom(df)
        if ok_db and neck_db and ma_dist <= MA150_MAX_DISTANCE_DB:
            ma_ok, ma_reason = _ma_near_neckline_filter(df, neck_db)
            if not ma_ok:
                if DEBUG_SCAN_REASONS: log(f"{symbol}: Double Bottom — MA not near neckline: {ma_reason}")
                _fs("ma_neckline")
            else:
                meta_db["pattern_type"] = "Double Bottom"
                score = compute_setup_score(df, symbol, neck_db, len(df)-1, meta_db)
                if score >= effective_min_score:
                    stop, target, size, atr, rr = atr_stop_and_position(neck_db, df, meta_db)
                    candidates.append({
                        "ticker": symbol, "phase": 1,
                        "pattern_type": "Double Bottom",
                        "breakout_level": neck_db,
                        "score": score, "stop_loss": stop, "target": target, "rr_ratio": rr,
                        "meta": {**meta_db, "position_size": size, "atr14": atr},
                    })
                    if DEBUG_SCAN_REASONS: log(f"{symbol}: ✅ Double Bottom — score={score:.1f} neck={neck_db:.2f}")
                elif DEBUG_SCAN_REASONS:
                    log(f"{symbol}: Double Bottom — score too low ({score:.1f})")
                # ── Watchlist ──
                try:
                    dist_from_breakout = (neck_db - price_now) / neck_db if neck_db > 0 else 0
                    if dist_from_breakout >= 0 and dist_from_breakout <= WATCHLIST_NEAR_PCT:
                        log_watchlist(
                            symbol=symbol, price=price_now,
                            reason=f"מחיר {dist_from_breakout*100:.1f}% מתחת לפריצה",
                            pattern="Double Bottom", breakout=neck_db, score=score,
                        )
                    elif score > 0:
                        log_watchlist(
                            symbol=symbol, price=price_now,
                            reason=f"ציון נמוך מהסף ({score:.1f} < {effective_min_score})",
                            pattern="Double Bottom", breakout=neck_db, score=score,
                        )
                except Exception:
                    pass
        elif DEBUG_SCAN_REASONS:
            reason = meta_db.get("reason","") if isinstance(meta_db, dict) else ""
            if not ok_db:
                log(f"{symbol}: Double Bottom — {reason}")
            else:
                log(f"{symbol}: Double Bottom — MA150 too far ({ma_dist*100:.1f}%)")
    except Exception as e:
        log(f"{symbol} Double Bottom error: {e}")

    # ====== PHASE 2 ======
    try:
        phase2 = check_for_consolidation_breakout(df, symbol)
        # סנן סטאפים שרחוקים מדי מ-EMA28 או מ-MA150
        if phase2:
            phase2 = [p for p in phase2
                      if abs((price_now - float(df["ema28"].iloc[-1])) / float(df["ema28"].iloc[-1])) <= EMA28_MAX_DIST_PCT
                      and ma_dist <= MA150_MAX_DISTANCE]
        candidates.extend(phase2)
    except Exception as e:
        log(f"{symbol} Phase2 error: {e}")



    if not candidates:
        if DEBUG_SCAN_REASONS:
            log(f"NO ALERT {symbol} | close={price_now:.2f} | ma150={'NA' if np.isnan(ma150_val) else f'{ma150_val:.2f}'}")
        # ── Watchlist: עבר EMA28+MA150+דוחות אבל אין תבנית ──
        try:
            log_watchlist(
                symbol=symbol, price=price_now,
                reason="לא נמצאה תבנית (עבר EMA28 + MA150 + דוחות)",
                details=f"MA150: ${ma150_val:.2f}" if not np.isnan(ma150_val) else ""
            )
        except Exception:
            pass
        return []

    # --- dedup / cooldown ---
    new_alerts = []
    for alert in candidates:
        if not isinstance(alert, dict):
            continue
        alert["_df"] = df   # df מצורף להתראה לשימוש בגרף/מייל
        pname = alert.get("pattern_type","")
        bl    = alert.get("breakout_level", 0)
        if alert_already_sent(symbol, pname, bl, alert_history):
            log(f"{symbol} {pname} already sent. Skip."); continue
        new_alerts.append(alert)

    return new_alerts

# ============================================================
#  EMAIL
# ============================================================

# ============================================================
#  DEEP INTELLIGENCE — Multi-TF / Accumulation / Insider / Short
# ============================================================

# ============================================================
#  RELATIVE STRENGTH SCORE (RS Score 0-100 vs SPY)
#  IBD method: 40%×3M + 20%×6M + 40%×12M relative to S&P500
#  RS >= 80 = 20% עליון — מנצח שוק
#  RS >= 60 = ממוצע
#  RS <  60 = חלש — לא כדאי לסחור
# ============================================================

_rs_spy_cache: dict = {}


def _get_spy_returns() -> dict:
    """Cache יומי לביצועי SPY ל-3/6/12 חודשים."""
    import datetime
    today = str(datetime.date.today())
    if _rs_spy_cache.get("date") == today:
        return _rs_spy_cache.get("returns", {})
    try:
        spy_df = yf.download("SPY", period="13mo", interval="1d",
                             progress=False, auto_adjust=True)
        if spy_df is None or len(spy_df) < 60:
            return {}
        if isinstance(spy_df.columns, pd.MultiIndex):
            spy_df.columns = [c[0].lower() for c in spy_df.columns]
        else:
            spy_df.columns = [c.lower() for c in spy_df.columns]
        close = spy_df["close"] if "close" in spy_df.columns else spy_df.iloc[:, 0]
        now   = float(close.iloc[-1])
        ret = {
            "3m":  (now - float(close.iloc[-63]))  / max(float(close.iloc[-63]),  1e-9) if len(close) >= 63  else None,
            "6m":  (now - float(close.iloc[-126])) / max(float(close.iloc[-126]), 1e-9) if len(close) >= 126 else None,
            "12m": (now - float(close.iloc[-252])) / max(float(close.iloc[-252]), 1e-9) if len(close) >= 252 else None,
        }
        _rs_spy_cache["date"]    = today
        _rs_spy_cache["returns"] = ret
        return ret
    except Exception:
        return {}


def compute_rs_score(ticker: str, df: pd.DataFrame) -> dict:
    """
    RS Score (0-100) ביחס ל-SPY.
    משמש לתצוגה במייל בלבד (פילטר RS בוטל).
    """
    result = {
        "rs_score":  None,
        "rs_label":  "N/A",
        "perf_3m":   None,
        "perf_6m":   None,
        "perf_12m":  None,
        "vs_spy_3m": None,
        "summary":   "RS N/A",
    }
    try:
        if df is None or len(df) < 63:
            return result
        close = df["close"] if "close" in df.columns else df.iloc[:, 0]
        now   = float(close.iloc[-1])

        # ביצועי המניה
        p3m  = (now - float(close.iloc[-63]))  / max(float(close.iloc[-63]),  1e-9) if len(close) >= 63  else None
        p6m  = (now - float(close.iloc[-126])) / max(float(close.iloc[-126]), 1e-9) if len(close) >= 126 else None
        p12m = (now - float(close.iloc[-252])) / max(float(close.iloc[-252]), 1e-9) if len(close) >= 252 else None
        result["perf_3m"]  = round(p3m  * 100, 1) if p3m  is not None else None
        result["perf_6m"]  = round(p6m  * 100, 1) if p6m  is not None else None
        result["perf_12m"] = round(p12m * 100, 1) if p12m is not None else None

        # ביצועי SPY
        spy  = _get_spy_returns()
        s3m  = spy.get("3m",  0) or 0
        s6m  = spy.get("6m",  0) or 0
        s12m = spy.get("12m", 0) or 0

        # עודף תשואה יחסי
        r3m  = (p3m  - s3m)  if p3m  is not None else 0.0
        r6m  = (p6m  - s6m)  if p6m  is not None else 0.0
        r12m = (p12m - s12m) if p12m is not None else 0.0
        result["vs_spy_3m"] = round(r3m * 100, 1)

        # ציון IBD משוקלל → tanh → 0-100
        import math
        combined = 0.4 * r3m + 0.2 * r6m + 0.4 * r12m
        rs = 50 + 50 * math.tanh(combined * 4)
        rs = round(max(1.0, min(99.0, rs)), 1)
        result["rs_score"] = rs

        if rs >= 80:
            emoji, tier = "🟢", "מנצח שוק"
        elif rs >= 60:
            emoji, tier = "🟡", "ממוצע"
        else:
            emoji, tier = "🔴", "חלש מהשוק"

        result["rs_label"] = f"RS {rs:.0f} — {tier}"
        vs_str  = f"{r3m*100:+.1f}% vs SPY"
        p6_str  = f" | 6M: {result['perf_6m']:+.1f}%" if result["perf_6m"] is not None else ""
        p12_str = f" | 12M: {result['perf_12m']:+.1f}%" if result["perf_12m"] is not None else ""
        result["summary"] = f"{emoji} RS {rs:.0f} | {vs_str}{p6_str}{p12_str}"

    except Exception:
        result["summary"] = "RS N/A"
    return result


# ============================================================
#  CATALYST ENGINE — מנוע הסברה "למה עכשיו?"
# ============================================================

# ============================================================
#  SELF-LEARNING ENGINE — מנוע למידה עצמית
#  רץ כל שבת, מנתח performance_log.csv ומעדכן פרמטרים
# ============================================================

LEARNING_CONFIG_FILE = os.getenv("LEARNING_CONFIG_FILE", "scanner_learned_params.json")
LEARNING_MIN_SAMPLES = 30    # מינימום סטאפים לניתוח
LEARNING_LOOKBACK_DAYS = 90  # מנתח את 90 הימים האחרונים

# ============================================================
#  MARKET REGIME — האם השוק בכלל בריא לקניות?
#  S&P500 מתחת MA50 = שוק יורד = לא לשלוח התראות כניסה
# ============================================================
REGIME_ENABLED   = os.getenv("REGIME_ENABLED",  "False").lower() in ("1","true","yes")
REGIME_MA_PERIOD = int(os.getenv("REGIME_MA_PERIOD", "50"))   # MA50 של SPY

_regime_cache: dict | None = None

def get_market_regime() -> dict:
    """
    בודק את מצב השוק לפי SPY vs MA50:
    BULL  — SPY מעל MA50 ועולה  → מסחר רגיל
    NEUTRAL — SPY סביב MA50     → מסחר זהיר
    BEAR  — SPY מתחת MA50       → לא לשלוח התראות כניסה

    מחזיר: { regime, spy_price, ma50, vs_ma50_pct, emoji, allow_trading }
    """
    global _regime_cache
    if _regime_cache is not None:
        return _regime_cache

    result = {
        "regime":        "UNKNOWN",
        "spy_price":     None,
        "ma50":          None,
        "vs_ma50_pct":   None,
        "emoji":         "🟡",
        "allow_trading": True,   # ברירת מחדל — תמיד אפשר
        "summary":       "מצב שוק לא ידוע",
    }
    try:
        df = yf.download("SPY", period="3mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < REGIME_MA_PERIOD + 2:
            _regime_cache = result
            return result

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]

        close = df["close"] if "close" in df.columns else df.iloc[:, 0]
        price = float(close.iloc[-1])
        ma50  = float(close.rolling(REGIME_MA_PERIOD).mean().iloc[-1])
        prev_ma50 = float(close.rolling(REGIME_MA_PERIOD).mean().iloc[-2])
        vs_ma50   = round((price - ma50) / max(ma50, 1e-9) * 100, 2)
        ma_rising = ma50 > prev_ma50

        if price > ma50 and ma_rising:
            regime, emoji, allow = "BULL",    "🟢", True
        elif price > ma50 and not ma_rising:
            regime, emoji, allow = "NEUTRAL", "🟡", True
        elif price < ma50 and vs_ma50 > -3:
            regime, emoji, allow = "NEUTRAL", "🟡", True
        else:
            regime, emoji, allow = "BEAR",    "🔴", not REGIME_ENABLED

        result.update({
            "regime":        regime,
            "spy_price":     round(price, 2),
            "ma50":          round(ma50, 2),
            "vs_ma50_pct":   vs_ma50,
            "emoji":         emoji,
            "allow_trading": allow,
            "summary":       f"{emoji} Market Regime: {regime} | SPY {vs_ma50:+.1f}% vs MA{REGIME_MA_PERIOD}",
        })
        log(f"🌍 {result['summary']}")
        if not allow:
            log("⛔ BEAR Market — trading suspended (set REGIME_ENABLED=False to override)")

    except Exception as e:
        log(f"get_market_regime error: {e}")

    _regime_cache = result
    return result


# ============================================================
#  SECTOR ROTATION INTELLIGENCE
#  בודק כל בוקר לאן זורם הכסף — ומשפיע על ציוני הסטאפים
# ============================================================

# בונוס/קנס לסטאפים לפי חוזק הסקטור
SECTOR_ROTATION_BONUS  = float(os.getenv("SECTOR_ROTATION_BONUS",  "0.8"))   # בונוס לסקטור HOT
SECTOR_ROTATION_PENALTY= float(os.getenv("SECTOR_ROTATION_PENALTY","-0.6"))  # קנס לסקטור COLD
SECTOR_ROTATION_LOOKBACK = int(os.getenv("SECTOR_ROTATION_LOOKBACK", "20"))  # ימים להשוואה

# cache גלובלי — נטען פעם אחת לריצה
_sector_rotation_cache: dict | None = None

def build_sector_rotation_map() -> dict:
    """
    בונה מפת רוטציה לכל הסקטורים:
    לכל ETF מחשב:
      - ביצועים 5d / 20d
      - מיקום vs MA50
      - RS vs SPY (relative strength)
      - דירוג: HOT / WARM / NEUTRAL / COLD / FROZEN

    מחזיר dict: { sector_name: { etf, perf_5d, perf_20d, vs_ma50, rs_vs_spy, rank } }
    """
    global _sector_rotation_cache
    if _sector_rotation_cache is not None:
        return _sector_rotation_cache

    log("🌊 Building sector rotation map...")
    result = {}

    # הורד SPY כבסיס להשוואה
    try:
        spy_df = yf.download("SPY", period="3mo", interval="1d",
                             progress=False, auto_adjust=True)
        if isinstance(spy_df.columns, pd.MultiIndex):
            spy_df.columns = [c[0].lower() for c in spy_df.columns]
        else:
            spy_df.columns = [c.lower() for c in spy_df.columns]
        spy_close = spy_df["close"] if "close" in spy_df.columns else spy_df.iloc[:, 0]
        spy_now   = float(spy_close.iloc[-1])
        spy_20d   = float(spy_close.iloc[-21]) if len(spy_close) >= 21 else float(spy_close.iloc[0])
        spy_perf  = (spy_now - spy_20d) / max(spy_20d, 1e-9) * 100
    except Exception:
        spy_perf = 0.0
        spy_now  = None

    all_etfs = list(set(SECTOR_ETF_MAP.values()))

    for sector, etf in SECTOR_ETF_MAP.items():
        if sector in result:  # כבר חושב (כמה שמות לאותו ETF)
            result[sector] = result.get(list(SECTOR_ETF_MAP.keys())[
                list(SECTOR_ETF_MAP.values()).index(etf)], {})
            continue
        try:
            df = yf.download(etf, period="3mo", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or len(df) < 25:
                result[sector] = {"etf": etf, "rank": "NEUTRAL", "error": "no data"}
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]

            close = df["close"] if "close" in df.columns else df.iloc[:, 0]
            now    = float(close.iloc[-1])
            d5     = float(close.iloc[-6])  if len(close) >= 6  else float(close.iloc[0])
            d20    = float(close.iloc[-21]) if len(close) >= 21 else float(close.iloc[0])
            ma50   = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else float(close.mean())

            perf_5d  = round((now - d5)  / max(d5,  1e-9) * 100, 2)
            perf_20d = round((now - d20) / max(d20, 1e-9) * 100, 2)
            vs_ma50  = round((now - ma50) / max(ma50, 1e-9) * 100, 2)
            rs_spy   = round(perf_20d - spy_perf, 2)  # ביצוע יחסי ל-SPY

            # ── דירוג ───────────────────────────────────────
            # HOT:    מעל MA50 + RS חיובי + ביצוע 5d חיובי
            # WARM:   מעל MA50 + RS ניטרלי
            # NEUTRAL: מעל MA50 בקושי / RS שלילי קצת
            # COLD:   מתחת MA50 / RS שלילי
            # FROZEN: מתחת MA50 + RS מאוד שלילי + 20d שלילי
            if vs_ma50 > 2 and rs_spy > 1 and perf_5d > 0:
                rank = "HOT"
            elif vs_ma50 > 0 and rs_spy > 0:
                rank = "WARM"
            elif vs_ma50 < -3 and rs_spy < -3 and perf_20d < -3:
                rank = "FROZEN"
            elif vs_ma50 < 0 or rs_spy < -2:
                rank = "COLD"
            else:
                rank = "NEUTRAL"

            result[sector] = {
                "etf":      etf,
                "perf_5d":  perf_5d,
                "perf_20d": perf_20d,
                "vs_ma50":  vs_ma50,
                "rs_spy":   rs_spy,
                "rank":     rank,
            }
        except Exception as e:
            result[sector] = {"etf": etf, "rank": "NEUTRAL", "error": str(e)}

    # ── לוג סיכום ────────────────────────────────────────────
    rank_emoji = {"HOT": "🔥", "WARM": "🟢", "NEUTRAL": "🟡", "COLD": "🔴", "FROZEN": "❄️"}
    hot   = [s for s,v in result.items() if v.get("rank") == "HOT"]
    cold  = [s for s,v in result.items() if v.get("rank") in ("COLD","FROZEN")]
    log(f"🌊 Sector Rotation: HOT={hot} | COLD/FROZEN={cold}")

    _sector_rotation_cache = result
    return result


def get_sector_rotation_adjustment(ticker: str, rotation_map: dict) -> tuple[float, str]:
    """
    מחזיר (score_adjustment, reason) לפי חוזק הסקטור של המניה.
    HOT   → +0.8
    WARM  → +0.3
    NEUTRAL → 0
    COLD  → -0.6
    FROZEN → -1.2
    """
    try:
        info   = _get_yf_info(ticker)
        sector = info.get("sector", "")
        if not sector or sector not in rotation_map:
            return 0.0, ""

        data = rotation_map.get(sector, {})
        rank = data.get("rank", "NEUTRAL")
        etf  = data.get("etf", "")
        rs   = data.get("rs_spy", 0)

        adjustment_map = {
            "HOT":     SECTOR_ROTATION_BONUS,
            "WARM":    round(SECTOR_ROTATION_BONUS * 0.4, 2),
            "NEUTRAL": 0.0,
            "COLD":    SECTOR_ROTATION_PENALTY,
            "FROZEN":  round(SECTOR_ROTATION_PENALTY * 2, 2),
        }
        emoji_map = {"HOT": "🔥", "WARM": "🟢", "NEUTRAL": "🟡", "COLD": "🔴", "FROZEN": "❄️"}

        adj    = adjustment_map.get(rank, 0.0)
        emoji  = emoji_map.get(rank, "🟡")
        reason = f"Sector {emoji} {rank} ({etf}, RS vs SPY: {rs:+.1f}%)"

        return adj, reason
    except Exception:
        return 0.0, ""


# ============================================================
#  DYNAMIC STOP LOSS — מעקב אחרי פוזיציות פתוחות
#  רץ בכל ריצה, בודק כל סטאפ שנשלח ועדיין פתוח
# ============================================================

POSITIONS_FILE      = os.getenv("POSITIONS_FILE",      "open_positions.json")
TRAILING_ATR_MULT   = float(os.getenv("TRAILING_ATR_MULT",   "1.5"))   # trailing stop = ATR × 1.5 מתחת לשיא
POSITION_MAX_DAYS   = int(os.getenv("POSITION_MAX_DAYS",     "30"))    # סגור פוזיציה אחרי 30 יום


def _load_positions() -> list[dict]:
    """טוען פוזיציות פתוחות מ-JSON."""
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_positions(positions: list[dict]) -> None:
    """שומר פוזיציות פתוחות ל-JSON."""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2, default=str)
    except Exception as e:
        log(f"save_positions error: {e}")


def open_position(alert: dict) -> None:
    """
    פותח פוזיציה חדשה כשנשלחת התראה.
    שומר: ticker, entry, stop_initial, target, date_open, highest_price
    """
    try:
        positions = _load_positions()
        ticker = alert.get("ticker", "")

        # אל תפתח כפול
        if any(p["ticker"] == ticker and not p.get("closed") for p in positions):
            return

        entry   = float(alert.get("breakout_level", 0) or 0)
        stop    = float(alert.get("stop_loss", 0) or 0)
        target  = float(alert.get("target", 0) or 0)
        if entry <= 0:
            return

        positions.append({
            "ticker":        ticker,
            "pattern":       alert.get("pattern_type", ""),
            "entry":         entry,
            "stop_initial":  stop,
            "stop_current":  stop,       # מתעדכן עם trailing
            "target":        target,
            "date_open":     datetime.now().strftime("%Y-%m-%d"),
            "highest_price": entry,      # שיא מאז הכניסה
            "closed":        False,
            "close_reason":  None,
            "close_price":   None,
            "close_date":    None,
            "pnl_pct":       None,
        })
        _save_positions(positions)
        log(f"📂 Position opened: {ticker} @ {entry:.2f} | stop={stop:.2f} | target={target:.2f}")
    except Exception as e:
        log(f"open_position error: {e}")


def run_position_tracker(send_exit_email_fn=None) -> list[dict]:
    """
    בודק כל פוזיציה פתוחה:
    1. מעדכן Trailing Stop לפי שיא חדש
    2. בודק אם פגעה ב-stop → שולח התראת יציאה
    3. בודק אם הגיעה ל-target → שולח התראת רווח
    4. בודק אם EMA28 עדיין תומך
    5. סוגר אחרי POSITION_MAX_DAYS

    מחזיר רשימת התראות יציאה שנשלחו.
    """
    positions  = _load_positions()
    open_pos   = [p for p in positions if not p.get("closed")]
    exit_alerts = []

    if not open_pos:
        return []

    log(f"📊 Position Tracker: checking {len(open_pos)} open positions...")

    today = datetime.now().date()

    for p in open_pos:
        ticker = p["ticker"]
        try:
            # הורד נתונים עדכניים
            df = yf.download(ticker, period="60d", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]

            price_now = float(df["close"].iloc[-1])
            high_now  = float(df["high"].iloc[-1])
            entry     = float(p["entry"])
            stop_curr = float(p["stop_current"])
            target    = float(p["target"])
            date_open = pd.Timestamp(p["date_open"]).date()
            days_open = (today - date_open).days

            # ── Trailing Stop ────────────────────────────────
            # חשב ATR14
            add_technical_indicators(df)
            atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else price_now * 0.02

            # עדכן שיא
            new_high = max(float(p["highest_price"]), price_now)
            p["highest_price"] = new_high

            # trailing stop = שיא - (ATR × mult)
            trailing = round(new_high - TRAILING_ATR_MULT * atr, 2)
            new_stop = max(stop_curr, trailing)  # רק מעלה — לא מורידים stop

            if new_stop > stop_curr:
                log(f"   🔼 {ticker}: Trailing stop {stop_curr:.2f} → {new_stop:.2f} (high={new_high:.2f})")
                p["stop_current"] = new_stop

            # ── EMA28 check ──────────────────────────────────
            ema28_ok = True
            if "ema28" in df.columns:
                ema28 = float(df["ema28"].iloc[-1])
                ema28_ok = price_now >= ema28 * 0.99  # מרווח 1%

            # ── בדוק תנאי יציאה ─────────────────────────────
            close_reason = None
            close_emoji  = ""

            if price_now <= new_stop:
                close_reason = f"🛑 פגע ב-Stop Loss ({price_now:.2f} ≤ {new_stop:.2f})"
                close_emoji  = "🛑"
            elif price_now >= target:
                close_reason = f"🎯 הגיע ליעד! ({price_now:.2f} ≥ {target:.2f})"
                close_emoji  = "🎯"
            elif not ema28_ok:
                close_reason = f"⚠️ ירד מתחת ל-EMA28 ({price_now:.2f})"
                close_emoji  = "⚠️"
            elif days_open >= 3:
                # ── ירד מתחת ל-Low של אתמול ──────────────────────
                try:
                    if len(df) >= 2:
                        prev_low = float(df["low"].iloc[-2])
                        pnl_pct_now = round((price_now - entry) / max(entry, 1e-9) * 100, 2)
                        if price_now < prev_low:
                            close_reason = f"📉 ירד מתחת ל-Low אתמול ({price_now:.2f} < {prev_low:.2f}) | רווח: {pnl_pct_now:+.1f}%"
                            close_emoji  = "📉"
                except Exception:
                    pass
            if close_reason is None and days_open >= POSITION_MAX_DAYS:
                close_reason = f"⏰ פג זמן ({days_open} ימים)"
                close_emoji  = "⏰"

            pnl_pct = round((price_now - entry) / max(entry, 1e-9) * 100, 2)

            if close_reason:
                # סגור פוזיציה
                p["closed"]       = True
                p["close_reason"] = close_reason
                p["close_price"]  = price_now
                p["close_date"]   = today.isoformat()
                p["pnl_pct"]      = pnl_pct

                log(f"   {close_emoji} {ticker} CLOSED: {close_reason} | P&L: {pnl_pct:+.1f}%")

                exit_alerts.append({
                    "ticker":       ticker,
                    "pattern":      p.get("pattern", ""),
                    "entry":        entry,
                    "exit_price":   price_now,
                    "stop":         new_stop,
                    "target":       target,
                    "pnl_pct":      pnl_pct,
                    "days_open":    days_open,
                    "reason":       close_reason,
                    "emoji":        close_emoji,
                })
            else:
                # פוזיציה פתוחה — לוג סטטוס
                pct_to_target = round((target - price_now) / max(price_now, 1e-9) * 100, 1)
                pct_to_stop   = round((price_now - new_stop) / max(price_now, 1e-9) * 100, 1)
                log(f"   📈 {ticker}: {price_now:.2f} | P&L={pnl_pct:+.1f}% | "
                    f"to_target={pct_to_target:.1f}% | to_stop={pct_to_stop:.1f}% | {days_open}d open")

        except Exception as e:
            log(f"   position_tracker error for {ticker}: {e}")
            continue

    _save_positions(positions)

    # שלח מייל יציאה אם יש
    if exit_alerts and send_exit_email_fn:
        try:
            send_exit_email_fn(exit_alerts)
        except Exception as e:
            log(f"send_exit_email error: {e}")

    return exit_alerts


def send_exit_email(exit_alerts: list[dict]) -> None:
    """
    שולח מייל התראת יציאה לכל פוזיציה שנסגרה.
    """
    if not exit_alerts:
        return
    try:
        cards = []
        for a in exit_alerts:
            pnl     = a["pnl_pct"]
            pnl_col = "#15803d" if pnl >= 0 else "#b91c1c"
            pnl_bg  = "#f0fdf4" if pnl >= 0 else "#fef2f2"
            cards.append(f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:500px;margin:12px auto;
     border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;background:#fff;">
  <div style="padding:14px 16px;background:{pnl_bg};border-bottom:1px solid #e5e7eb;">
    <div style="font-size:18px;font-weight:700;color:{pnl_col};">
      {a['emoji']} {a['ticker']} — {'+' if pnl>=0 else ''}{pnl:.1f}%
    </div>
    <div style="font-size:12px;color:#374151;margin-top:4px;">{a['reason']}</div>
  </div>
  <div style="padding:12px 16px;font-size:12px;color:#374151;">
    <table style="width:100%;border-collapse:collapse;">
      <tr><td style="padding:3px 0;font-weight:600;">כניסה</td><td>${a['entry']:.2f}</td>
          <td style="font-weight:600;">יציאה</td><td>${a['exit_price']:.2f}</td></tr>
      <tr><td style="padding:3px 0;font-weight:600;">Stop</td><td>${a['stop']:.2f}</td>
          <td style="font-weight:600;">יעד</td><td>${a['target']:.2f}</td></tr>
      <tr><td style="padding:3px 0;font-weight:600;">תבנית</td><td colspan="3">{a['pattern']}</td></tr>
      <tr><td style="padding:3px 0;font-weight:600;">זמן פתוח</td><td colspan="3">{a['days_open']} ימים</td></tr>
    </table>
  </div>
</div>""")

        html_body = "\n".join(cards)
        subject   = f"🔔 יציאה מ-{len(exit_alerts)} פוזיציה/ות — Stock Scanner"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = FROM_EMAIL
        msg["To"]      = ", ".join(TO_EMAILS) if isinstance(TO_EMAILS, list) else TO_EMAILS
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(FROM_EMAIL, APP_PASSWORD)
            s.send_message(msg)

        log(f"📧 Exit email sent: {len(exit_alerts)} positions closed")
    except Exception as e:
        log(f"send_exit_email error: {e}")


def _load_learned_params() -> dict:
    """טוען פרמטרים שנלמדו מקובץ JSON. אם לא קיים — מחזיר ריק."""
    try:
        if os.path.exists(LEARNING_CONFIG_FILE):
            with open(LEARNING_CONFIG_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_learned_params(params: dict) -> None:
    """שומר פרמטרים שנלמדו לקובץ JSON."""
    try:
        params["last_updated"] = datetime.now().isoformat()
        with open(LEARNING_CONFIG_FILE, "w") as f:
            json.dump(params, f, indent=2)
        log(f"💾 Learned params saved → {LEARNING_CONFIG_FILE}")
    except Exception as e:
        log(f"save_learned_params error: {e}")

def _apply_learned_params() -> None:
    """
    טוען פרמטרים שנלמדו ומחיל אותם על המשתנים הגלובליים.
    נקרא בתחילת כל ריצה.
    """
    global MIN_ALERT_SCORE, BREAKOUT_TOLERANCE, EMA28_MAX_DIST_PCT
    global MA150_MAX_DISTANCE, DB_BOTTOM_DIFF_PCT, CH_PEAKS_MAX_DIFF_PCT
    try:
        params = _load_learned_params()
        if not params:
            return
        changed = []
        if "MIN_ALERT_SCORE" in params:
            old = MIN_ALERT_SCORE
            MIN_ALERT_SCORE = float(params["MIN_ALERT_SCORE"])
            if abs(old - MIN_ALERT_SCORE) > 0.01:
                changed.append(f"MIN_ALERT_SCORE: {old:.1f}→{MIN_ALERT_SCORE:.1f}")
        if "BREAKOUT_TOLERANCE" in params:
            old = BREAKOUT_TOLERANCE
            BREAKOUT_TOLERANCE = float(params["BREAKOUT_TOLERANCE"])
            if abs(old - BREAKOUT_TOLERANCE) > 0.0001:
                changed.append(f"BREAKOUT_TOLERANCE: {old*100:.2f}%→{BREAKOUT_TOLERANCE*100:.2f}%")
        if "EMA28_MAX_DIST_PCT" in params:
            old = EMA28_MAX_DIST_PCT
            EMA28_MAX_DIST_PCT = float(params["EMA28_MAX_DIST_PCT"])
            if abs(old - EMA28_MAX_DIST_PCT) > 0.001:
                changed.append(f"EMA28_MAX_DIST_PCT: {old*100:.1f}%→{EMA28_MAX_DIST_PCT*100:.1f}%")
        if changed:
            log(f"🧠 Learned params applied: {', '.join(changed)}")
        else:
            log(f"🧠 Learned params loaded (no changes from defaults)")
    except Exception as e:
        log(f"_apply_learned_params error: {e}")

def run_self_learning() -> dict:
    """
    מנוע הלמידה המרכזי — מנתח performance_log.csv ומחלץ תובנות:

    1. שיעור הצלחה לפי תבנית
    2. ציון מינימלי אופטימלי (MIN_ALERT_SCORE)
    3. זמן ממוצע לפריצה לפי תבנית
    4. איזה פרמטר BREAKOUT_TOLERANCE אופטימלי
    5. RR ממוצע בפועל vs מה שנחזה
    מחזיר dict עם תובנות + מעדכן JSON
    """
    result = {
        "status":       "no_data",
        "samples":      0,
        "insights":     [],
        "new_params":   {},
        "pattern_stats": {},
    }

    if not os.path.exists(PERFORMANCE_CSV):
        log("🧠 Self-learning: no performance CSV found yet")
        return result

    try:
        df = pd.read_csv(PERFORMANCE_CSV)
        if df.empty:
            return result

        # סנן: רק סטאפים שנבדקו + 90 הימים האחרונים
        df["date_sent"] = pd.to_datetime(df["date_sent"], errors="coerce")
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=LEARNING_LOOKBACK_DAYS)
        df = df[df["date_sent"] >= cutoff].copy()

        # רק שורות עם result_20d (נבדקו)
        checked = df[df["result_20d"].notna()].copy()
        n = len(checked)
        result["samples"] = n

        if n < LEARNING_MIN_SAMPLES:
            log(f"🧠 Self-learning: only {n} samples (need {LEARNING_MIN_SAMPLES})")
            result["status"] = "insufficient_data"
            return result

        log(f"🧠 Self-learning: analyzing {n} completed setups...")

        # ── ניתוח 1: שיעור הצלחה לפי תבנית ─────────────────
        pattern_stats = {}
        for pattern, grp in checked.groupby("pattern"):
            wins   = grp["result_20d"].str.startswith("WIN").sum()
            losses = grp["result_20d"].str.startswith("LOSS").sum()
            total  = len(grp)
            win_r  = wins / max(total, 1)
            avg_rr = grp["rr"].mean() if "rr" in grp.columns else 0
            # פריצה ממוצעת: חלץ % מ"WIN (+8.3%)"
            pct_vals = grp["result_20d"].str.extract(r"([+-]?[\d.]+)%").iloc[:,0].astype(float)
            avg_pct  = float(pct_vals.mean()) if not pct_vals.isna().all() else 0.0
            pattern_stats[pattern] = {
                "total":   total,
                "wins":    int(wins),
                "losses":  int(losses),
                "win_rate": round(win_r, 3),
                "avg_rr":  round(float(avg_rr), 2),
                "avg_pct_20d": round(avg_pct, 2),
            }
        result["pattern_stats"] = pattern_stats

        insights = []
        new_params = {}

        # ── ניתוח 2: ציון אופטימלי (MIN_ALERT_SCORE) ────────
        # חפש את הציון שממנו win_rate עולה מעל 55%
        best_score_threshold = None
        for threshold in [5.0, 5.5, 6.0, 6.5, 7.0, 7.5]:
            subset = checked[checked["score"] >= threshold]
            if len(subset) < 10:
                continue
            wins_s = subset["result_20d"].str.startswith("WIN").sum()
            wr = wins_s / len(subset)
            if wr >= 0.55:
                best_score_threshold = threshold
                break  # קח את הנמוך ביותר שעובד

        current_wr_all = checked["result_20d"].str.startswith("WIN").sum() / n
        if best_score_threshold and best_score_threshold > MIN_ALERT_SCORE + 0.4:
            new_params["MIN_ALERT_SCORE"] = best_score_threshold
            insights.append(
                f"📈 הגדל MIN_ALERT_SCORE ל-{best_score_threshold} "
                f"(win rate עולה ל-55%+ לעומת {current_wr_all*100:.0f}% כיום)"
            )
        elif current_wr_all >= 0.60 and MIN_ALERT_SCORE > 5.0:
            # win rate גבוה — אפשר להוריד סף כדי לקבל יותר סטאפים
            new_params["MIN_ALERT_SCORE"] = max(5.0, MIN_ALERT_SCORE - 0.5)
            insights.append(
                f"📊 הורד MIN_ALERT_SCORE ל-{new_params['MIN_ALERT_SCORE']} "
                f"(win rate {current_wr_all*100:.0f}% — יש מקום לעוד סטאפים)"
            )

        # ── ניתוח 3: BREAKOUT_TOLERANCE ─────────────────────
        # בדוק אם הרוב מרחוק הפריצה קרוב ל-0 או מגיע ל-0.5%
        if "entry" in checked.columns and "breakout_level" in checked.columns:
            try:
                checked["over_pct"] = (
                    (checked["entry"] - checked["breakout_level"]) /
                    checked["breakout_level"].replace(0, float("nan"))
                ) * 100
                avg_over = float(checked["over_pct"].mean())
                if avg_over < 0.15 and BREAKOUT_TOLERANCE > 0.003:
                    new_params["BREAKOUT_TOLERANCE"] = 0.003
                    insights.append(
                        f"🎯 הפחת BREAKOUT_TOLERANCE ל-0.3% "
                        f"(ממוצע פריצה בפועל: {avg_over:.2f}%)"
                    )
            except Exception:
                pass

        # ── ניתוח 4: EMA28_MAX_DIST_PCT ─────────────────────
        # סטאפים שנכשלו — מה המרחק שלהם מ-EMA28 ביום הכניסה?
        losses_df = checked[checked["result_20d"].str.startswith("LOSS")]
        if len(losses_df) >= 10:
            # אם יש הרבה הפסדים — הידק את EMA28
            loss_rate = len(losses_df) / n
            if loss_rate > 0.45 and EMA28_MAX_DIST_PCT > 0.02:
                new_params["EMA28_MAX_DIST_PCT"] = max(0.02, EMA28_MAX_DIST_PCT - 0.005)
                insights.append(
                    f"🔴 הפחת EMA28_MAX_DIST_PCT ל-{new_params['EMA28_MAX_DIST_PCT']*100:.1f}% "
                    f"(שיעור הפסד: {loss_rate*100:.0f}%)"
                )

        # ── ניתוח 5: תבנית הכי מצליחה / הכי גרועה ──────────
        if pattern_stats:
            best_p  = max(pattern_stats, key=lambda p: pattern_stats[p]["win_rate"])
            worst_p = min(pattern_stats, key=lambda p: pattern_stats[p]["win_rate"])
            best_wr  = pattern_stats[best_p]["win_rate"]
            worst_wr = pattern_stats[worst_p]["win_rate"]
            if best_wr > 0.60:
                insights.append(f"⭐ התבנית הכי מוצלחת: {best_p} ({best_wr*100:.0f}% win rate)")
            if worst_wr < 0.35 and pattern_stats[worst_p]["total"] >= 5:
                insights.append(f"⚠️ התבנית הכי חלשה: {worst_p} ({worst_wr*100:.0f}% win rate) — שקול להגדיל סף ציון")

        # ── שמור ─────────────────────────────────────────────
        result["insights"]   = insights
        result["new_params"] = new_params
        result["status"]     = "ok"
        result["win_rate_overall"] = round(current_wr_all, 3)

        if new_params:
            _save_learned_params(new_params)
            log(f"🧠 Self-learning complete — {len(new_params)} params updated, {len(insights)} insights")
        else:
            log(f"🧠 Self-learning complete — no param changes needed (win rate: {current_wr_all*100:.0f}%)")

        # ── לוג מפורט ────────────────────────────────────────
        log("=" * 55)
        log(f"🧠 SELF-LEARNING REPORT ({n} setups, last {LEARNING_LOOKBACK_DAYS}d)")
        log(f"   Win Rate כולל: {current_wr_all*100:.0f}%")
        for p, s in sorted(pattern_stats.items(), key=lambda x: -x[1]["win_rate"]):
            log(f"   {p:<22} | win={s['win_rate']*100:.0f}% | avg={s['avg_pct_20d']:+.1f}% | n={s['total']}")
        for ins in insights:
            log(f"   💡 {ins}")
        log("=" * 55)

    except Exception as e:
        log(f"run_self_learning error: {e}")
        result["status"] = "error"

    return result


def get_analyst_activity(ticker: str) -> dict:
    """
    מחזיר פעילות אנליסטים אחרונה:
    - מספר upgrades/downgrades ב-30 יום
    - יעד מחיר ממוצע vs מחיר נוכחי
    - המלצה קונצנזוס (Buy/Hold/Sell)
    """
    result = {
        "consensus":      None,
        "target_price":   None,
        "upside_pct":     None,
        "upgrades_30d":   0,
        "downgrades_30d": 0,
        "summary":        "אין מידע אנליסטים",
    }
    try:
        info = _get_yf_info(ticker)

        # קונצנזוס
        rec = info.get("recommendationKey", "")
        rec_map = {
            "strong_buy": "Strong Buy", "buy": "Buy",
            "hold": "Hold", "sell": "Sell", "strong_sell": "Strong Sell"
        }
        consensus = rec_map.get(rec.lower(), rec) if rec else None
        result["consensus"] = consensus

        # יעד מחיר
        target = info.get("targetMeanPrice")
        current = info.get("currentPrice") or info.get("previousClose")
        if target and current:
            upside = (float(target) - float(current)) / float(current) * 100
            result["target_price"] = round(float(target), 2)
            result["upside_pct"]   = round(upside, 1)

        # upgrades/downgrades
        try:
            ticker_obj = yf.Ticker(ticker)
            upgrades_df = getattr(ticker_obj, "upgrades_downgrades", None)
            if upgrades_df is not None and not upgrades_df.empty:
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
                # בדוק שה-index הוא datetime
                if hasattr(upgrades_df.index, "tz_localize"):
                    try:
                        upgrades_df.index = upgrades_df.index.tz_localize("UTC")
                    except Exception:
                        pass
                recent = upgrades_df[upgrades_df.index >= cutoff]
                if not recent.empty:
                    grade_col = [c for c in recent.columns if "grade" in c.lower() or "action" in c.lower()]
                    if grade_col:
                        grades = recent[grade_col[0]].str.lower()
                        result["upgrades_30d"]   = int((grades.str.contains("upgrade|buy|outperform|overweight")).sum())
                        result["downgrades_30d"] = int((grades.str.contains("downgrade|sell|underperform|underweight")).sum())
        except Exception:
            pass

        # סיכום
        parts = []
        if consensus:
            emoji = "🟢" if "buy" in consensus.lower() else "🔴" if "sell" in consensus.lower() else "🟡"
            parts.append(f"{emoji} {consensus}")
        if result["upside_pct"] is not None:
            parts.append(f"יעד: ${result['target_price']} ({result['upside_pct']:+.0f}%)")
        if result["upgrades_30d"] > 0:
            parts.append(f"⬆️ {result['upgrades_30d']} upgrades ב-30 יום")
        if result["downgrades_30d"] > 0:
            parts.append(f"⬇️ {result['downgrades_30d']} downgrades")
        result["summary"] = " | ".join(parts) if parts else "אין מידע אנליסטים"

    except Exception as e:
        result["summary"] = "אין מידע אנליסטים"
    return result


def get_sector_strength(ticker: str) -> dict:
    """
    בודק אם ETF הסקטור נמצא מעל MA50 שלו.
    מחזיר: strong (True/False), ETF, מחיר vs MA50, סיכום.
    """
    result = {
        "etf":          None,
        "sector":       None,
        "above_ma50":   None,
        "etf_vs_ma50":  None,
        "strong":       False,
        "summary":      "אין מידע על ביצועי הסקטור",
    }
    try:
        info   = _get_yf_info(ticker)
        sector = info.get("sector", "")
        if not sector:
            return result
        result["sector"] = sector

        etf = SECTOR_ETF_MAP.get(sector)
        if not etf:
            return result
        result["etf"] = etf

        # הורד נתוני ETF
        etf_df = yf.download(etf, period="3mo", interval="1d",
                             progress=False, auto_adjust=True)
        if etf_df is None or len(etf_df) < 52:
            return result

        if isinstance(etf_df.columns, pd.MultiIndex):
            etf_df.columns = [c[0].lower() for c in etf_df.columns]
        else:
            etf_df.columns = [c.lower() for c in etf_df.columns]

        close = etf_df["close"] if "close" in etf_df.columns else etf_df.iloc[:, 0]
        ma50  = float(close.rolling(50).mean().iloc[-1])
        price = float(close.iloc[-1])

        above    = price > ma50
        vs_ma50  = round((price - ma50) / max(ma50, 1e-9) * 100, 1)

        result["above_ma50"]  = above
        result["etf_vs_ma50"] = vs_ma50
        result["strong"]      = above

        emoji = "🟢" if above else "🔴"
        result["summary"] = (
            f"{emoji} {sector} ({etf}): "
            f"{'מעל' if above else 'מתחת'} MA50 "
            f"({vs_ma50:+.1f}%)"
        )

    except Exception as e:
        result["summary"] = "אין מידע על ביצועי הסקטור"
    return result


def get_catalyst_engine(ticker: str, company: dict) -> dict:
    """
    מרכז את כל מנוע ההסברה:
    - דוח קרוב (מ-company card)
    - ביצועי סקטור (ETF מעל MA50)
    - פעילות אנליסטים (upgrades/target)
    - Insider buying (מ-get_insider_buying)
    מחזיר dict מלא + score (0-4) כמה קטליזטורים חיוביים
    """
    # דוח קרוב — מ-company card
    earnings_date = company.get("earnings_date")
    days_to_earn  = None
    earnings_str  = "לא ידוע"
    earnings_ok   = False
    if earnings_date:
        try:
            from datetime import date
            ed = pd.Timestamp(str(earnings_date)).date()
            days_to_earn = (ed - date.today()).days
            if 14 <= days_to_earn <= 42:
                earnings_ok = True
                earnings_str = f"🗓️ דוח בעוד {days_to_earn} ימים — Catalyst Window!"
            elif days_to_earn > 0:
                earnings_str = f"דוח בעוד {days_to_earn} ימים"
            else:
                earnings_str = f"דוח עבר לפני {abs(days_to_earn)} ימים"
        except Exception:
            pass

    # ביצועי סקטור
    sector_strength = get_sector_strength(ticker)

    # אנליסטים
    analyst = get_analyst_activity(ticker)

    # Insider
    insider = get_insider_buying(ticker)
    insider_ok = bool(insider.get("transactions"))

    # ציון קטליזטורים (כמה חיוביים)
    catalyst_score = sum([
        earnings_ok,
        sector_strength.get("strong", False),
        analyst.get("upgrades_30d", 0) > 0,
        insider_ok,
    ])

    return {
        "earnings_str":     earnings_str,
        "earnings_ok":      earnings_ok,
        "days_to_earnings": days_to_earn,
        "sector_strength":  sector_strength,
        "analyst":          analyst,
        "insider":          insider,
        "catalyst_score":   catalyst_score,  # 0-4
        "catalyst_label":   ["⚪ אין קטליזטור", "🟡 קטליזטור חלש", "🟠 קטליזטור בינוני", "🟢 קטליזטור חזק", "🔥 כל הקטליזטורים!"][min(catalyst_score, 4)],
    }


def get_weekly_timeframe(ticker: str) -> dict:
    """
    מחזיר ניתוח שבועי: מגמה, MA30 שבועי, ביצועים 3 חודשים.
    """
    result = {"trend": "UNKNOWN", "above_ma30w": None, "perf_3m": None, "summary": "N/A"}
    try:
        df_w = yf.download(ticker, period="6mo", interval="1wk",
                           progress=False, auto_adjust=True)
        if df_w is None or len(df_w) < 12:
            return result
        if isinstance(df_w.columns, pd.MultiIndex):
            df_w.columns = [c[0].lower() for c in df_w.columns]
        else:
            df_w.columns = [c.lower() for c in df_w.columns]
        close = df_w["close"] if "close" in df_w.columns else df_w.iloc[:, 0]
        ma30w = float(close.rolling(13).mean().iloc[-1])  # 13 שבועות ≈ MA30 שבועי
        price_now = float(close.iloc[-1])
        price_3m  = float(close.iloc[-13]) if len(close) >= 13 else float(close.iloc[0])
        perf_3m   = round((price_now - price_3m) / max(price_3m, 1e-9) * 100, 1)
        above     = price_now > ma30w
        # מגמה: 3 שבועות אחרונים עולים?
        last3 = close.iloc[-3:].values
        trend = "UP" if last3[-1] > last3[0] else "DOWN"
        result = {
            "trend":       trend,
            "above_ma30w": above,
            "ma30w":       round(ma30w, 2),
            "price_now":   round(price_now, 2),
            "perf_3m":     perf_3m,
            "summary":     f"{'🟢' if above and trend=='UP' else '🔴' if not above else '🟡'} שבועי: {'מעל' if above else 'מתחת'} MA30 | {perf_3m:+.1f}% ב-3 חודשים | מגמה: {trend}",
        }
    except Exception as e:
        result["summary"] = f"שגיאה בניתוח שבועי: {e}"
    return result


def get_accumulation_score(df: pd.DataFrame) -> dict:
    """
    On-Balance Volume + Chaikin Money Flow.
    מודד האם מוסדיים צוברים (קונים בשקט) או מוכרים.
    """
    result = {"obv_trend": "NEUTRAL", "cmf": None, "score": "NEUTRAL", "summary": "N/A"}
    try:
        if df is None or len(df) < 20:
            return result
        closes  = df["close"].values
        highs   = df["high"].values
        lows    = df["low"].values
        volumes = df["volume"].values if "volume" in df.columns else None
        if volumes is None:
            return result

        # On-Balance Volume
        obv = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i-1]:
                obv.append(obv[-1] - volumes[i])
            else:
                obv.append(obv[-1])
        obv = np.array(obv)
        # OBV trend: האם OBV עולה ב-10 ימים אחרונים?
        obv_10 = obv[-10:]
        obv_slope = float(np.polyfit(range(len(obv_10)), obv_10, 1)[0])
        obv_trend = "UP" if obv_slope > 0 else "DOWN"

        # Chaikin Money Flow (14 ימים)
        period = min(14, len(df))
        mf_multiplier = ((closes - lows) - (highs - closes)) / np.where((highs - lows) > 0, highs - lows, 1)
        mf_volume = mf_multiplier * volumes
        cmf = float(np.sum(mf_volume[-period:]) / max(np.sum(volumes[-period:]), 1))
        cmf = round(cmf, 3)

        # ציון כולל
        if obv_trend == "UP" and cmf > 0.05:
            score = "ACCUMULATION"
            emoji = "🟢"
        elif obv_trend == "DOWN" and cmf < -0.05:
            score = "DISTRIBUTION"
            emoji = "🔴"
        else:
            score = "NEUTRAL"
            emoji = "🟡"

        result = {
            "obv_trend": obv_trend,
            "cmf":       cmf,
            "score":     score,
            "summary":   f"{emoji} {score} | OBV: {obv_trend} | CMF: {cmf:+.3f}",
        }
    except Exception as e:
        result["summary"] = f"שגיאה ב-Accumulation: {e}"
    return result


def get_insider_buying(ticker: str) -> dict:
    """
    מחפש קניות Insider של C-Level ב-90 ימים האחרונים דרך SEC EDGAR.
    מחזיר רשימת עסקאות וסיכום.
    """
    result = {"transactions": [], "summary": "אין מידע על קניות Insider"}
    try:
        import urllib.request
        # SEC EDGAR — חיפוש form 4 לפי טיקר
        cik_url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={(__import__('datetime').datetime.now() - __import__('datetime').timedelta(days=90)).strftime('%Y-%m-%d')}&enddt={__import__('datetime').datetime.now().strftime('%Y-%m-%d')}&forms=4"
        req = urllib.request.Request(cik_url, headers={"User-Agent": "StockScanner research@scanner.com"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        hits = data.get("hits", {}).get("hits", [])
        transactions = []
        clevel_titles = ["chief executive", "ceo", "chief financial", "cfo",
                         "chief operating", "coo", "president", "chairman"]

        for hit in hits[:20]:
            src_data = hit.get("_source", {})
            filer     = src_data.get("display_names", [""])[0] if src_data.get("display_names") else ""
            filed     = src_data.get("file_date", "")
            # בדוק שזה קנייה (P) ולא מכירה
            trans_type = src_data.get("period_of_report", "")
            title = filer.lower()
            is_clevel = any(t in title for t in clevel_titles)
            if is_clevel:
                transactions.append({
                    "filer": filer,
                    "date":  filed,
                })

        result["transactions"] = transactions[:3]
        if transactions:
            names = ", ".join(t["filer"] for t in transactions[:2])
            result["summary"] = f"🟢 C-Level קנה: {names} ({transactions[0]['date']})"
        else:
            result["summary"] = "⚪ אין קניות C-Level ב-90 יום האחרונים"
    except Exception as e:
        result["summary"] = f"⚪ Insider data N/A"
    return result


def get_short_interest(ticker: str) -> dict:
    """
    מחזיר Short Interest % ו-Days to Cover דרך yfinance.
    """
    result = {"short_pct": None, "days_to_cover": None, "summary": "N/A"}
    try:
        info = _get_yf_info(ticker)
        short_pct  = info.get("shortPercentOfFloat")
        shares_short = info.get("sharesShort")
        avg_vol    = info.get("averageVolume")
        days_cover = round(shares_short / avg_vol, 1) if shares_short and avg_vol else None

        if short_pct is not None:
            pct = round(short_pct * 100, 1)
            emoji = "🔥" if pct >= 15 else "🟡" if pct >= 8 else "⚪"
            squeeze = " — Squeeze Potential!" if pct >= 15 and days_cover and days_cover >= 3 else ""
            result = {
                "short_pct":     pct,
                "days_to_cover": days_cover,
                "summary":       f"{emoji} Short Interest: {pct}% | Days to Cover: {days_cover or 'N/A'}{squeeze}",
            }
        else:
            result["summary"] = "⚪ Short Interest: N/A"
    except Exception as e:
        result["summary"] = "⚪ Short Interest: N/A"
    return result


# ============================================================
#  OPTIONS FLOW — זיהוי פעילות אופציות חריגה (Unusual Options)
#  מחפש: call volume חריג, put/call ratio נמוך, OTM calls גדולות
# ============================================================

OPTIONS_MIN_VOLUME     = int(os.getenv("OPTIONS_MIN_VOLUME",     "500"))   # מינימום volume לאופציה בודדת
OPTIONS_OI_MIN         = int(os.getenv("OPTIONS_OI_MIN",         "1000"))  # מינימום open interest
OPTIONS_UNUSUAL_MULT   = float(os.getenv("OPTIONS_UNUSUAL_MULT", "3.0"))   # volume פי 3 מ-OI = חריג
OPTIONS_PC_RATIO_MAX   = float(os.getenv("OPTIONS_PC_RATIO_MAX", "0.7"))   # put/call < 0.7 = bullish flow
OPTIONS_EXPIRY_MAX_DAYS= int(os.getenv("OPTIONS_EXPIRY_MAX_DAYS","45"))    # אופציות עד 45 יום קדימה

# ============================================================
#  DARK POOL PRINTS — זיהוי עסקאות מוסדיות גדולות
#  Dark pools = בורסות פרטיות שמוסדיים משתמשים בהן
#  סימן: נרות ה-Volume חריגים עם תנועת מחיר מינימלית
# ============================================================

DARKPOOL_VOL_MULT    = float(os.getenv("DARKPOOL_VOL_MULT",    "2.5"))  # volume פי 2.5 מהממוצע
DARKPOOL_PRICE_MAX   = float(os.getenv("DARKPOOL_PRICE_MAX",   "0.5"))  # תנועת מחיר מקסימלית 0.5%
DARKPOOL_LOOKBACK    = int(os.getenv("DARKPOOL_LOOKBACK",      "20"))   # 20 ימים אחורה
DARKPOOL_MIN_DAYS    = int(os.getenv("DARKPOOL_MIN_DAYS",      "2"))    # לפחות 2 ימי dark pool

# ============================================================
#  13F TRACKING — מעקב אחרי פוזיציות קרנות גידור
#  13F = דוח רבעוני שכל מוסד >$100M חייב להגיש ל-SEC
#  מחפש: האם קרנות גידול גדולות מחזיקות/קנו לאחרונה
# ============================================================

# ============================================================
#  SOCIAL SENTIMENT — סנטימנט רשת + Google Trends proxy
#  מקורות: yfinance news sentiment + מילות מפתח בכותרות
#  (Google Trends API דורש הרשאות — משתמשים בproxy חינמי)
# ============================================================

SENTIMENT_LOOKBACK_DAYS = int(os.getenv("SENTIMENT_LOOKBACK_DAYS", "7"))   # 7 ימים אחורה
SENTIMENT_MIN_ARTICLES  = int(os.getenv("SENTIMENT_MIN_ARTICLES",  "3"))   # מינימום כתבות לניתוח

# מילות מפתח חיוביות/שליליות לניתוח כותרות
_BULLISH_WORDS = {
    "beat", "beats", "record", "surge", "soar", "rally", "upgrade", "outperform",
    "strong", "growth", "win", "partnership", "deal", "contract", "approved",
    "breakthrough", "launch", "acquire", "profit", "exceed", "raise", "bullish",
}
_BEARISH_WORDS = {
    "miss", "misses", "cut", "downgrade", "underperform", "weak", "decline",
    "drop", "fall", "loss", "layoff", "lawsuit", "recall", "investigation",
    "fraud", "concern", "warning", "risk", "probe", "delay", "cancel",
}

# ============================================================
#  CONGRESSIONAL TRADING — מעקב אחרי קניות חברי קונגרס
#  חוק STOCK Act: חברי קונגרס חייבים לדווח תוך 45 יום
#  מקור: quiverquant.com API (חינמי) + housestockwatcher.com
# ============================================================

CONGRESS_LOOKBACK_DAYS = int(os.getenv("CONGRESS_LOOKBACK_DAYS", "90"))  # 3 חודשים אחורה

def get_congressional_trading(ticker: str) -> dict:
    """
    בודק האם חברי קונגרס קנו/מכרו את המניה לאחרונה.
    מקור ראשי: quiverquant.com (API חינמי, ללא key)
    Fallback: housestockwatcher.com

    מחזיר: transactions, net_bias (BUY/SELL/NEUTRAL), summary
    """
    result = {
        "transactions": [],
        "buy_count":    0,
        "sell_count":   0,
        "net_bias":     "NEUTRAL",
        "bullish":      False,
        "summary":      "אין נתוני Congressional Trading",
    }
    try:
        # ── Quiver Quant API (חינמי, ללא key) ────────────────
        url = f"https://api.quiverquant.com/beta/historical/congresstrading/{ticker}"
        headers = {"User-Agent": "Mozilla/5.0 StockScanner/1.0"}
        resp = requests.get(url, headers=headers, timeout=8)

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                cutoff = datetime.now() - timedelta(days=CONGRESS_LOOKBACK_DAYS)
                buys = sells = 0
                txns = []

                for item in data[:20]:
                    try:
                        date_str = item.get("Date") or item.get("TransactionDate", "")
                        tx_date  = pd.Timestamp(date_str).to_pydatetime() if date_str else None
                        if tx_date and tx_date < cutoff:
                            continue

                        tx_type  = str(item.get("Transaction", "")).lower()
                        amount   = item.get("Amount", "")
                        member   = item.get("Representative") or item.get("Name", "")
                        party    = item.get("Party", "")

                        is_buy  = "purchase" in tx_type or "buy" in tx_type
                        is_sell = "sale" in tx_type or "sell" in tx_type

                        if is_buy:
                            buys += 1
                        elif is_sell:
                            sells += 1

                        txns.append({
                            "date":   date_str[:10] if date_str else "",
                            "member": f"{member} ({party})" if party else member,
                            "type":   "BUY" if is_buy else "SELL" if is_sell else tx_type,
                            "amount": str(amount)[:20],
                        })
                    except Exception:
                        continue

                result["transactions"] = txns[:5]
                result["buy_count"]    = buys
                result["sell_count"]   = sells

                if buys > sells and buys >= 2:
                    result["net_bias"] = "BUY"
                    result["bullish"]  = True
                elif sells > buys and sells >= 2:
                    result["net_bias"] = "SELL"

                # סיכום
                if txns:
                    bias_emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "🟡"}.get(result["net_bias"], "🟡")
                    top = txns[0]
                    result["summary"] = (
                        f"{bias_emoji} Congress: {buys} קניות / {sells} מכירות ב-{CONGRESS_LOOKBACK_DAYS} יום"
                        f" | {top['member']} {top['type']} ({top['date']})"
                    )
                    return result

        # ── Fallback: housestockwatcher (JSON פתוח) ───────────
        url2 = "https://house-stock-watcher-data.s3-us-gov-west-1.amazonaws.com/data/all_transactions.json"
        resp2 = requests.get(url2, headers=headers, timeout=10)
        if resp2.status_code == 200:
            all_data = resp2.json()
            ticker_up = ticker.upper()
            cutoff    = datetime.now() - timedelta(days=CONGRESS_LOOKBACK_DAYS)
            buys = sells = 0
            txns = []

            for item in all_data:
                if str(item.get("ticker","")).upper() != ticker_up:
                    continue
                try:
                    date_str = item.get("transaction_date","")
                    tx_date  = pd.Timestamp(date_str).to_pydatetime() if date_str else None
                    if tx_date and tx_date < cutoff:
                        continue
                    tx_type = str(item.get("type","")).lower()
                    member  = item.get("representative","")
                    amount  = item.get("amount","")
                    is_buy  = "purchase" in tx_type
                    is_sell = "sale" in tx_type
                    if is_buy: buys += 1
                    elif is_sell: sells += 1
                    txns.append({"date": date_str[:10], "member": member,
                                 "type": "BUY" if is_buy else "SELL", "amount": str(amount)[:20]})
                except Exception:
                    continue

            result["transactions"] = txns[:5]
            result["buy_count"]    = buys
            result["sell_count"]   = sells
            if buys > sells and buys >= 1:
                result["net_bias"] = "BUY"; result["bullish"] = True
            elif sells > buys:
                result["net_bias"] = "SELL"
            if txns:
                bias_emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "🟡"}.get(result["net_bias"], "🟡")
                result["summary"] = f"{bias_emoji} Congress: {buys} קניות / {sells} מכירות | {txns[0]['member']} {txns[0]['type']}"

    except Exception as e:
        result["summary"] = "שגיאה בנתוני Congressional Trading"

    return result


# ============================================================
#  REVERSE SCANNER — סריקה הפוכה: איפה המוסדיים מוכרים?
#  מזהה מניות שמוסדיים יוצאים מהן — להימנע מהן
#  לוגיקה: Dark Pool BEARISH + Short Interest גבוה + Options Put Flow
# ============================================================

REVERSE_MIN_SCORE = float(os.getenv("REVERSE_MIN_SCORE", "2.0"))  # לפחות 2 סימנים אדומים

def scan_for_institutional_selling(ticker: str, df: pd.DataFrame) -> dict:
    """
    בודק האם מוסדיים יוצאים ממניה — אות אזהרה.
    לוגיקה משולבת:
      +1: Dark Pool bearish prints
      +1: Short Interest > 10% + עולה
      +1: Put/Call ratio > 1.2 (יותר puts מcalls)
      +1: מחיר מתחת MA50 + MA50 יורד
      +1: בעלות מוסדית ירדה ברבעון

    score >= 2 = סימן אזהרה לסריקה הפוכה
    """
    result = {
        "warning_score":   0,
        "signals":         [],
        "avoid":           False,
        "summary":         "",
    }
    try:
        score  = 0
        signals = []

        # ── Dark Pool bearish ─────────────────────────────────
        dp = get_dark_pool_prints(ticker, df)
        if dp.get("net_bias") == "BEARISH" and dp.get("bearish_prints", 0) >= 2:
            score += 1
            signals.append(f"🔴 Dark Pool bearish ({dp['bearish_prints']} prints)")

        # ── Short Interest > 10% ──────────────────────────────
        si = get_short_interest(ticker)
        si_pct = si.get("short_pct_float") or 0
        if float(si_pct) > 10:
            score += 1
            signals.append(f"🔴 Short Interest גבוה: {si_pct:.1f}%")

        # ── Put/Call ratio > 1.2 ─────────────────────────────
        opt = get_options_flow(ticker)
        pc  = opt.get("pc_ratio") or 0
        if float(pc) > 1.2:
            score += 1
            signals.append(f"🔴 Put/Call Ratio: {pc:.2f} (bearish flow)")

        # ── מחיר מתחת MA50 יורד ──────────────────────────────
        if "close" in df.columns and len(df) >= 52:
            close  = df["close"]
            ma50   = close.rolling(50).mean()
            price  = float(close.iloc[-1])
            ma_now = float(ma50.iloc[-1])
            ma_prv = float(ma50.iloc[-5])
            if price < ma_now and ma_now < ma_prv:
                score += 1
                signals.append(f"🔴 מחיר מתחת MA50 יורד ({price:.2f} < {ma_now:.2f})")

        # ── בעלות מוסדית נמוכה ───────────────────────────────
        inst = get_institutional_ownership(ticker)
        inst_pct = inst.get("inst_ownership_pct") or 0
        if float(inst_pct) < 30:
            score += 1
            signals.append(f"🔴 בעלות מוסדית נמוכה: {inst_pct:.0f}%")

        result["warning_score"] = score
        result["signals"]       = signals
        result["avoid"]         = score >= REVERSE_MIN_SCORE

        if signals:
            result["summary"] = f"⚠️ Reverse Scan: {score} סימני מכירה מוסדית — {'הימנע!' if result['avoid'] else 'עקוב'}"
        else:
            result["summary"] = "✅ אין סימני מכירה מוסדית"

    except Exception as e:
        result["summary"] = "שגיאה ב-Reverse Scanner"

    return result


def get_social_sentiment(ticker: str) -> dict:
    """
    מנתח סנטימנט מחדשות yfinance + Google Trends proxy:

    1. News Sentiment — ניתוח כותרות 7 ימים אחורה
       → כמה כתבות חיוביות vs שליליות
       → ציון סנטימנט -1 עד +1

    2. Google Trends Proxy — דרך pytrends (אם מותקן)
       → trend score יחסי 0-100
       → עלייה בחיפושים = עניין גובר

    מחזיר: score, sentiment, bullish_count, bearish_count, summary
    """
    result = {
        "score":          0.0,    # -1 עד +1
        "sentiment":      "NEUTRAL",
        "bullish_count":  0,
        "bearish_count":  0,
        "total_articles": 0,
        "trend_score":    None,   # Google Trends 0-100
        "top_headlines":  [],
        "summary":        "אין נתוני סנטימנט",
    }
    try:
        tk = yf.Ticker(ticker)

        # ── News Sentiment ────────────────────────────────────
        try:
            news = tk.news or []
        except Exception:
            news = []

        cutoff = datetime.now() - timedelta(days=SENTIMENT_LOOKBACK_DAYS)
        bullish = 0
        bearish = 0
        headlines = []

        for item in news[:20]:
            try:
                # תאריך
                pub_ts = item.get("providerPublishTime") or item.get("published", 0)
                if pub_ts:
                    pub_date = datetime.fromtimestamp(float(pub_ts))
                    if pub_date < cutoff:
                        continue

                # כותרת
                title = (item.get("title") or
                         item.get("content", {}).get("title", "") if isinstance(item.get("content"), dict) else "")
                if not title:
                    continue

                title_lower = title.lower()
                words = set(title_lower.split())

                b_hits = len(words & _BULLISH_WORDS)
                r_hits = len(words & _BEARISH_WORDS)

                if b_hits > r_hits:
                    bullish += 1
                    headlines.append(f"🟢 {title[:60]}")
                elif r_hits > b_hits:
                    bearish += 1
                    headlines.append(f"🔴 {title[:60]}")
                else:
                    headlines.append(f"🟡 {title[:60]}")

            except Exception:
                continue

        total = bullish + bearish
        result["bullish_count"]  = bullish
        result["bearish_count"]  = bearish
        result["total_articles"] = total
        result["top_headlines"]  = headlines[:4]

        if total >= SENTIMENT_MIN_ARTICLES:
            score = (bullish - bearish) / max(total, 1)
            result["score"] = round(score, 2)
            if score >= 0.4:
                result["sentiment"] = "BULLISH"
            elif score <= -0.4:
                result["sentiment"] = "BEARISH"
            else:
                result["sentiment"] = "NEUTRAL"

        # ── Google Trends Proxy (pytrends — אופציונלי) ───────
        try:
            import importlib
            pytrends_spec = importlib.util.find_spec("pytrends")
            if pytrends_spec is not None:
                from pytrends.request import TrendReq  # type: ignore[import]
                pt = TrendReq(hl="en-US", tz=360, timeout=(5, 10))
                pt.build_payload([ticker], timeframe="now 7-d", geo="US")
                trend_df = pt.interest_over_time()
                if not trend_df.empty and ticker in trend_df.columns:
                    trend_now  = float(trend_df[ticker].iloc[-1])
                    trend_week = float(trend_df[ticker].mean())
                    result["trend_score"] = int(trend_now)
                    if trend_now > trend_week * 1.3:
                        result["score"] = min(1.0, result["score"] + 0.2)
        except Exception:
            pass  # pytrends לא מותקן / rate limit — לא קריטי

        # ── סיכום ────────────────────────────────────────────
        sent_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(result["sentiment"], "🟡")
        parts = [f"{sent_emoji} סנטימנט: {result['sentiment']}"]
        if total > 0:
            parts.append(f"{bullish}↑ / {bearish}↓ מתוך {total} כתבות")
        if result["trend_score"] is not None:
            parts.append(f"Trends: {result['trend_score']}/100")
        result["summary"] = " | ".join(parts)

    except Exception as e:
        result["summary"] = "שגיאה בניתוח סנטימנט"

    return result


def get_institutional_ownership(ticker: str) -> dict:
    """
    מנתח בעלות מוסדית דרך yfinance:
    1. % בעלות מוסדית כוללת
    2. מספר מחזיקים מוסדיים
    3. שינוי רבעוני (קנו/מכרו מוסדות)
    4. Top 3 מחזיקים

    המידע מגיע מ-13F filings דרך yfinance.
    """
    result = {
        "inst_ownership_pct":  None,
        "inst_holders_count":  None,
        "quarterly_change":    None,   # + = קנייה מוסדית, - = מכירה
        "top_holders":         [],
        "bullish_signal":      False,
        "summary":             "אין נתוני 13F",
    }
    try:
        info = _get_yf_info(ticker)

        # % בעלות מוסדית
        inst_pct = info.get("heldPercentInstitutions")
        if inst_pct:
            result["inst_ownership_pct"] = round(float(inst_pct) * 100, 1)

        # מחזיקים מוסדיים
        tk = yf.Ticker(ticker)

        try:
            inst_holders = tk.institutional_holders
            if inst_holders is not None and not inst_holders.empty:
                result["inst_holders_count"] = len(inst_holders)

                # Top 3 מחזיקים
                top = []
                for _, row in inst_holders.head(3).iterrows():
                    holder = str(row.get("Holder", row.get("holder", "")))
                    shares = row.get("Shares", row.get("shares", 0))
                    pct    = row.get("% Out", row.get("pctOut", 0))
                    if holder:
                        top.append({
                            "name":   holder[:30],
                            "shares": int(shares) if shares else 0,
                            "pct":    round(float(pct) * 100, 2) if pct and float(pct) < 1 else round(float(pct), 2) if pct else 0,
                        })
                result["top_holders"] = top
        except Exception:
            pass

        # שינוי רבעוני — מ-major_holders
        try:
            major = tk.major_holders
            if major is not None and not major.empty:
                # yfinance מחזיר data frame עם Value ו-Breakdown
                for _, row in major.iterrows():
                    label = str(row.get("Breakdown", row.iloc[1] if len(row) > 1 else "")).lower()
                    val   = row.get("Value", row.iloc[0] if len(row) > 0 else None)
                    if "institution" in label and val is not None:
                        try:
                            pct_val = float(str(val).replace("%","").strip())
                            if pct_val > 1:
                                pct_val /= 100
                            result["inst_ownership_pct"] = round(pct_val * 100, 1)
                        except Exception:
                            pass
        except Exception:
            pass

        # בניית ציון bullish
        inst_pct_val = result["inst_ownership_pct"] or 0
        # בעלות מוסדית גבוהה (>60%) + top holders ידועים = חיובי
        top_names = " ".join(h["name"].lower() for h in result["top_holders"])
        big_names  = any(name in top_names for name in
                         ["vanguard","blackrock","fidelity","state street","berkshire",
                          "capital group","t. rowe","jpmorgan","goldman"])
        result["bullish_signal"] = inst_pct_val >= 60 or big_names

        # סיכום
        parts = []
        if inst_pct_val:
            emoji = "🟢" if inst_pct_val >= 60 else "🟡" if inst_pct_val >= 40 else "🔴"
            parts.append(f"{emoji} בעלות מוסדית: {inst_pct_val:.0f}%")
        if result["inst_holders_count"]:
            parts.append(f"{result['inst_holders_count']} מוסדות")
        if result["top_holders"]:
            top1 = result["top_holders"][0]["name"]
            parts.append(f"Top: {top1}")
        if big_names:
            parts.append("⭐ Big Money מחזיק")
        result["summary"] = " | ".join(parts) if parts else "אין נתוני 13F"

    except Exception as e:
        result["summary"] = "שגיאה בנתוני 13F"

    return result


def get_dark_pool_prints(ticker: str, df: pd.DataFrame | None = None) -> dict:
    """
    מזהה עסקאות Dark Pool לפי סימני מחיר-נפח:

    Dark Pool Print = יום שבו:
      1. Volume גבוה פי DARKPOOL_VOL_MULT מהממוצע
      2. תנועת מחיר (high-low)/close קטנה מ-DARKPOOL_PRICE_MAX%
         → מוסד קנה/מכר כמות גדולה בלי להזיז את המחיר

    תוצאות: רשימת הימים, כיוון מצטבר (net_bias), סיכום
    """
    result = {
        "prints":        [],
        "count":         0,
        "net_bias":      "NEUTRAL",  # BULLISH / BEARISH / NEUTRAL
        "bullish_prints":0,
        "bearish_prints":0,
        "summary":       "אין נתוני Dark Pool",
    }
    try:
        if df is None or df.empty or len(df) < DARKPOOL_LOOKBACK + 5:
            return result

        window = df.tail(DARKPOOL_LOOKBACK + 5).copy()
        if "volume" not in window.columns:
            return result

        # ממוצע volume ל-20 יום
        vols     = window["volume"].values
        closes   = window["close"].values
        highs    = window["high"].values
        lows     = window["low"].values
        opens    = window["open"].values

        avg_vol  = float(pd.Series(vols).rolling(20).mean().iloc[-1])
        if avg_vol <= 0:
            return result

        prints = []
        for i in range(max(0, len(window) - DARKPOOL_LOOKBACK), len(window)):
            vol   = float(vols[i])
            close = float(closes[i])
            high  = float(highs[i])
            low   = float(lows[i])
            op    = float(opens[i])

            # תנאי Dark Pool: volume חריג + טווח מחיר קטן
            vol_ratio  = vol / max(avg_vol, 1)
            price_range= (high - low) / max(close, 1e-9) * 100

            if vol_ratio >= DARKPOOL_VOL_MULT and price_range <= DARKPOOL_PRICE_MAX:
                # כיוון: נר ירוק = קנייה, נר אדום = מכירה
                bullish = close >= op
                date_str = str(window.index[i])[:10] if hasattr(window.index[i], '__str__') else str(i)
                prints.append({
                    "date":        date_str,
                    "vol_ratio":   round(vol_ratio, 1),
                    "price_range": round(price_range, 2),
                    "bullish":     bullish,
                    "close":       round(close, 2),
                })

        result["prints"]   = prints[-5:]  # 5 אחרונים
        result["count"]    = len(prints)

        if prints:
            bull = sum(1 for p in prints if p["bullish"])
            bear = len(prints) - bull
            result["bullish_prints"] = bull
            result["bearish_prints"] = bear

            # net bias לפי רוב
            if bull >= DARKPOOL_MIN_DAYS and bull > bear:
                result["net_bias"] = "BULLISH"
            elif bear >= DARKPOOL_MIN_DAYS and bear > bull:
                result["net_bias"] = "BEARISH"

            bias_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(result["net_bias"], "🟡")
            result["summary"] = (
                f"{bias_emoji} Dark Pool: {len(prints)} prints ב-{DARKPOOL_LOOKBACK} יום "
                f"| {bull} קנייה / {bear} מכירה"
            )
        else:
            result["summary"] = "אין Dark Pool prints"

    except Exception as e:
        result["summary"] = "שגיאה בניתוח Dark Pool"

    return result


def get_options_flow(ticker: str) -> dict:
    """
    מנתח פעילות אופציות דרך yfinance:
    1. Put/Call Ratio — calls > puts = bullish
    2. Unusual Call Volume — volume >> open interest
    3. OTM Calls גדולות — מישהו מהמר על עלייה משמעותית
    4. Implied Move — כמה השוק מצפה שהמניה תזוז

    מחזיר dict עם: bullish_flow, pc_ratio, unusual_calls, summary
    """
    result = {
        "bullish_flow":   False,
        "pc_ratio":       None,
        "unusual_calls":  [],
        "total_call_vol": 0,
        "total_put_vol":  0,
        "implied_move":   None,
        "summary":        "אין נתוני אופציות",
    }
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options
        if not expirations:
            return result

        today = datetime.now().date()
        total_call_vol = 0
        total_put_vol  = 0
        unusual_calls  = []

        # סרוק עד 3 תאריכי פקיעה הקרובים
        for exp_str in expirations[:3]:
            try:
                exp_date = pd.Timestamp(exp_str).date()
                days_to_exp = (exp_date - today).days
                if days_to_exp < 1 or days_to_exp > OPTIONS_EXPIRY_MAX_DAYS:
                    continue

                chain = tk.option_chain(exp_str)
                calls = chain.calls
                puts  = chain.puts

                if calls.empty and puts.empty:
                    continue

                # ── Put/Call Volume Ratio ─────────────────────
                c_vol = int(calls["volume"].fillna(0).sum()) if not calls.empty else 0
                p_vol = int(puts["volume"].fillna(0).sum())  if not puts.empty  else 0
                total_call_vol += c_vol
                total_put_vol  += p_vol

                # ── Unusual Calls — volume >> OI ─────────────
                if not calls.empty:
                    current_price = float(
                        (tk.fast_info.last_price if hasattr(tk, "fast_info") else 0)
                        or calls["strike"].median()
                    )

                    for _, row in calls.iterrows():
                        vol = float(row.get("volume") or 0)
                        oi  = float(row.get("openInterest") or 0)
                        strike = float(row.get("strike") or 0)

                        if vol < OPTIONS_MIN_VOLUME:
                            continue
                        if oi < OPTIONS_OI_MIN and oi > 0:
                            continue

                        # חריג: volume פי OPTIONS_UNUSUAL_MULT מ-OI
                        unusual = oi > 0 and vol >= oi * OPTIONS_UNUSUAL_MULT
                        # OTM call: strike מעל המחיר הנוכחי
                        otm_pct = (strike - current_price) / max(current_price, 1e-9) * 100 if current_price > 0 else 0

                        if unusual or (vol >= OPTIONS_MIN_VOLUME * 2 and otm_pct > 2):
                            unusual_calls.append({
                                "expiry":   exp_str,
                                "strike":   strike,
                                "volume":   int(vol),
                                "oi":       int(oi),
                                "otm_pct":  round(otm_pct, 1),
                                "unusual":  unusual,
                                "days_exp": days_to_exp,
                            })

            except Exception:
                continue

        result["total_call_vol"] = total_call_vol
        result["total_put_vol"]  = total_put_vol
        result["unusual_calls"]  = sorted(unusual_calls, key=lambda x: -x["volume"])[:3]

        # ── Put/Call Ratio ────────────────────────────────────
        if total_call_vol + total_put_vol > 0:
            pc_ratio = round(total_put_vol / max(total_call_vol, 1), 2)
            result["pc_ratio"] = pc_ratio
            bullish_pc = pc_ratio <= OPTIONS_PC_RATIO_MAX
        else:
            bullish_pc = False

        # ── Implied Move (מ-ATM straddle) ────────────────────
        try:
            exp0 = expirations[0]
            chain0 = tk.option_chain(exp0)
            if not chain0.calls.empty and not chain0.puts.empty:
                info = _get_yf_info(ticker)
                cp   = info.get("currentPrice") or info.get("previousClose") or 0
                if cp > 0:
                    # מצא ATM call ו-put
                    calls0 = chain0.calls.copy()
                    puts0  = chain0.puts.copy()
                    calls0["dist"] = (calls0["strike"] - cp).abs()
                    puts0["dist"]  = (puts0["strike"]  - cp).abs()
                    atm_call = calls0.sort_values("dist").iloc[0]
                    atm_put  = puts0.sort_values("dist").iloc[0]
                    straddle_price = float(atm_call.get("lastPrice",0) or 0) + float(atm_put.get("lastPrice",0) or 0)
                    if straddle_price > 0 and cp > 0:
                        result["implied_move"] = round(straddle_price / cp * 100, 1)
        except Exception:
            pass

        # ── bullish_flow ─────────────────────────────────────
        result["bullish_flow"] = bullish_pc or len(unusual_calls) >= 2

        # ── סיכום ────────────────────────────────────────────
        parts = []
        if result["pc_ratio"] is not None:
            pc_emoji = "🟢" if bullish_pc else "🔴"
            parts.append(f"{pc_emoji} P/C Ratio: {result['pc_ratio']:.2f}")
        if unusual_calls:
            top = unusual_calls[0]
            parts.append(
                f"🔥 Unusual Call: ${top['strike']:.0f} "
                f"({top['otm_pct']:+.1f}% OTM) "
                f"vol={top['volume']:,} exp={top['expiry']}"
            )
        if result["implied_move"]:
            parts.append(f"📐 Implied Move: ±{result['implied_move']:.1f}%")
        result["summary"] = " | ".join(parts) if parts else "אין פעילות חריגה"

    except Exception as e:
        result["summary"] = "שגיאה בנתוני אופציות"

    return result


def get_deep_intelligence(ticker: str, df=None) -> dict:
    """
    מרכז את כל הניתוח המעמיק: Weekly + Accumulation + Insider + Short + Options + Dark Pool + 13F + Sentiment + Congress.
    """
    weekly      = get_weekly_timeframe(ticker)
    accum       = get_accumulation_score(df) if df is not None else {"summary": "N/A"}
    insider     = get_insider_buying(ticker)
    short       = get_short_interest(ticker)
    options     = get_options_flow(ticker)
    darkpool    = get_dark_pool_prints(ticker, df)
    inst        = get_institutional_ownership(ticker)
    sentiment   = get_social_sentiment(ticker)
    congress    = get_congressional_trading(ticker)
    return {
        "weekly":    weekly,
        "accum":     accum,
        "insider":   insider,
        "short":     short,
        "options":   options,
        "darkpool":  darkpool,
        "inst":      inst,
        "sentiment": sentiment,
        "congress":  congress,
    }

def _build_html_card(alert: dict, company: dict, send_date: str, sector: dict | None = None, intel: dict | None = None, catalyst: dict | None = None, rs: dict | None = None) -> str:
    ticker  = alert.get("ticker","")
    score   = float(alert.get("score", 0))
    rr      = float(alert.get("rr_ratio", 0))
    entry   = float(alert.get("breakout_level", 0))
    stop    = float(alert.get("stop_loss", 0))
    target  = float(alert.get("target", 0))
    meta    = alert.get("meta", {}) or {}
    reasons = meta.get("score_reasons", []) or []
    fails   = meta.get("fail_reasons", []) or []
    size    = int(meta.get("position_size", 0) or 0)
    risk_ps = abs(entry - stop)
    color   = "#15803d" if score >= 70.0 else "#a16207"

    ok_lis  = "".join(f'<li>✅ {r}</li>' for r in reasons) or "<li>✅ ללא פירוט</li>"
    bad_lis = "".join(f'<li>⚠️ {r}</li>' for r in fails)
    warns   = (f'<div style="padding:12px 16px;"><h3 style="color:#b91c1c">אזהרות</h3>'
               f'<ul>{bad_lis}</ul></div>') if fails else ""

    # Company card
    name        = company.get("name", ticker)
    sector      = company.get("sector", "N/A")
    desc        = company.get("description", "") or ""
    eps         = company.get("eps")
    mc_b        = company.get("market_cap_b")
    edate       = company.get("earnings_date")
    eps_str     = f"${eps:.2f}" if eps is not None else "N/A"
    mc_str      = f"${mc_b}B" if mc_b else "N/A"
    earn_str    = str(edate) if edate else "לא ידוע"
    desc_html   = f'<p style="margin:6px 0;font-size:12px;color:#374151;line-height:1.5;">{desc[:300]}{"..." if len(desc)>300 else ""}</p>' if desc else ""

    company_html = f'''
  <div style="padding:12px 16px;border-bottom:1px solid #e5e7eb;background:#f8fafc;">
    <div style="font-size:13px;font-weight:700;color:#1e40af;margin-bottom:4px;">{name} · {sector}</div>
    {desc_html}
    <div style="display:flex;gap:16px;margin-top:6px;font-size:12px;color:#6b7280;">
      <span>📊 EPS: <b style="color:#111">{eps_str}</b></span>
      <span>💰 שווי: <b style="color:#111">{mc_str}</b></span>
      <span>📅 דוח: <b style="color:#111">{earn_str}</b></span>
    </div>
  </div>'''

    # Sector HTML
    sector_html = ""
    if sector and isinstance(sector, dict):
        sent = sector.get("sentiment", "NEUTRAL")
        sent_color = {"STRONG": "#15803d", "WEAK": "#b91c1c", "NEUTRAL": "#92400e"}.get(sent, "#92400e")
        sent_bg    = {"STRONG": "#f0fdf4", "WEAK": "#fef2f2", "NEUTRAL": "#fffbeb"}.get(sent, "#fffbeb")
        news_items = sector.get("news", [])
        news_html  = "".join(f'<li style="font-size:11px;color:#374151;margin-bottom:3px;">📰 {h}</li>'
                             for h in news_items[:3])

        # הוסף נתוני רוטציה אם קיימים
        rotation_map = _sector_rotation_cache or {}
        sector_name  = sector.get("sector", "")
        rot_data     = rotation_map.get(sector_name, {})
        rot_rank     = rot_data.get("rank", "")
        rot_rs       = rot_data.get("rs_spy")
        rot_line     = ""
        if rot_rank:
            rank_emoji = {"HOT": "🔥", "WARM": "🟢", "NEUTRAL": "🟡", "COLD": "🔴", "FROZEN": "❄️"}.get(rot_rank, "")
            rs_str     = f" | RS vs SPY: {rot_rs:+.1f}%" if rot_rs is not None else ""
            rot_line   = f'<div style="font-size:11px;color:#374151;margin-top:4px;">{rank_emoji} Rotation: <b>{rot_rank}</b>{rs_str}</div>'
        sector_html = f'''
  <div style="padding:12px 16px;border-bottom:1px solid #e5e7eb;background:{sent_bg};">
    <div style="font-size:12px;font-weight:700;color:{sent_color};margin-bottom:6px;">
      📊 {sector.get("summary","N/A")}
    </div>
    {rot_line}
    {f'<ul style="margin:4px 0 0;padding-right:16px;">{news_html}</ul>' if news_html else ""}
  </div>'''

    # ── Deep Intelligence HTML ─────────────────────────
    intel_html = ""
    if (intel and isinstance(intel, dict)) or (rs and isinstance(rs, dict)):
        weekly  = (intel or {}).get("weekly",  {}) or {}
        accum   = (intel or {}).get("accum",   {}) or {}
        insider = (intel or {}).get("insider", {}) or {}
        short   = (intel or {}).get("short",   {}) or {}
        options = (intel or {}).get("options",  {}) or {}
        darkpool= (intel or {}).get("darkpool", {}) or {}
        inst    = (intel or {}).get("inst",      {}) or {}
        sentiment=(intel or {}).get("sentiment", {}) or {}
        congress = (intel or {}).get("congress",  {}) or {}

        rows = []
        # RS Score — תמיד בראש
        if rs and isinstance(rs, dict) and rs.get("rs_score") is not None:
            rs_sum = rs.get("summary", "")
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">📊 RS Score</td>'
                       f'<td style="padding:5px 8px;color:#111;">{rs_sum}</td></tr>')
        if weekly.get("summary"):
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">📈 שבועי</td>'
                       f'<td style="padding:5px 8px;color:#111;">{weekly["summary"]}</td></tr>')
        if accum.get("summary"):
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">📦 A/D Score</td>'
                       f'<td style="padding:5px 8px;color:#111;">{accum["summary"]}</td></tr>')
        if insider.get("summary"):
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;">👔 Insider</td>'
                       f'<td style="padding:5px 8px;color:#111;">{insider["summary"]}</td></tr>')
        if short.get("summary"):
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;">📉 Short</td>'
                       f'<td style="padding:5px 8px;color:#111;">{short["summary"]}</td></tr>')
        # Options Flow
        opt_sum = options.get("summary", "")
        if opt_sum and opt_sum != "אין נתוני אופציות" and opt_sum != "אין פעילות חריגה":
            opt_emoji = "🔥" if options.get("bullish_flow") else "📊"
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">{opt_emoji} Options</td>'
                       f'<td style="padding:5px 8px;color:#111;">{opt_sum}</td></tr>')
        # Dark Pool
        dp_sum = darkpool.get("summary", "")
        if dp_sum and "אין Dark Pool" not in dp_sum and "שגיאה" not in dp_sum:
            dp_emoji = "🟢" if darkpool.get("net_bias") == "BULLISH" else "🔴" if darkpool.get("net_bias") == "BEARISH" else "🏊"
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">{dp_emoji} Dark Pool</td>'
                       f'<td style="padding:5px 8px;color:#111;">{dp_sum}</td></tr>')
        # 13F מוסדיים
        inst_sum = inst.get("summary", "")
        if inst_sum and "אין" not in inst_sum and "שגיאה" not in inst_sum:
            inst_emoji = "⭐" if inst.get("bullish_signal") else "🏛️"
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">{inst_emoji} 13F</td>'
                       f'<td style="padding:5px 8px;color:#111;">{inst_sum}</td></tr>')
        # Social Sentiment
        sent_sum = sentiment.get("summary", "")
        if sent_sum and "שגיאה" not in sent_sum and sentiment.get("total_articles", 0) >= 3:
            sent_col = {"BULLISH": "#15803d", "BEARISH": "#b91c1c", "NEUTRAL": "#92400e"}.get(sentiment.get("sentiment",""), "#374151")
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">💬 Sentiment</td>'
                       f'<td style="padding:5px 8px;color:{sent_col};font-weight:600;">{sent_sum}</td></tr>')
        # Congressional Trading
        cong_sum = congress.get("summary", "")
        if cong_sum and "שגיאה" not in cong_sum and "אין" not in cong_sum:
            cong_emoji = "🟢" if congress.get("bullish") else "🔴" if congress.get("net_bias") == "SELL" else "🏛️"
            rows.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">{cong_emoji} Congress</td>'
                       f'<td style="padding:5px 8px;color:#111;">{cong_sum}</td></tr>')

        if rows:
            rows_html = "".join(rows)
            intel_html = f'''
  <div style="padding:12px 16px;border-bottom:1px solid #e5e7eb;background:#fafafa;">
    <div style="font-size:12px;font-weight:700;color:#1e40af;margin-bottom:8px;">🔬 ניתוח מעמיק</div>
    <table style="width:100%;font-size:12px;border-collapse:collapse;">
      {rows_html}
    </table>
  </div>'''

    # ── Catalyst Engine HTML ────────────────────────────────
    catalyst_html = ""
    if catalyst and isinstance(catalyst, dict):
        c_label  = catalyst.get("catalyst_label", "")
        c_score  = catalyst.get("catalyst_score", 0)
        earn_str = catalyst.get("earnings_str", "")
        sect_sum = catalyst.get("sector_strength", {}).get("summary", "")
        anal_sum = catalyst.get("analyst", {}).get("summary", "")
        insd_sum = catalyst.get("insider", {}).get("summary", "")

        bg_color = {0: "#f9fafb", 1: "#fffbeb", 2: "#fff7ed", 3: "#f0fdf4", 4: "#ecfdf5"}.get(c_score, "#f9fafb")
        hd_color = {0: "#6b7280", 1: "#92400e", 2: "#c2410c", 3: "#15803d", 4: "#065f46"}.get(c_score, "#374151")

        rows_c = []
        if earn_str:
            rows_c.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;width:120px;">🗓️ דוח</td>'
                         f'<td style="padding:5px 8px;color:#111;">{earn_str}</td></tr>')
        if sect_sum:
            rows_c.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;">📊 סקטור</td>'
                         f'<td style="padding:5px 8px;color:#111;">{sect_sum}</td></tr>')
        if anal_sum and anal_sum != "אין מידע אנליסטים":
            rows_c.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;">🎯 אנליסטים</td>'
                         f'<td style="padding:5px 8px;color:#111;">{anal_sum}</td></tr>')
        if insd_sum and "אין" not in insd_sum:
            rows_c.append(f'<tr><td style="padding:5px 8px;font-weight:600;color:#374151;">👔 Insider</td>'
                         f'<td style="padding:5px 8px;color:#111;">{insd_sum}</td></tr>')

        if rows_c:
            rows_html_c = "".join(rows_c)
            catalyst_html = f'''
  <div style="padding:12px 16px;border-bottom:1px solid #e5e7eb;background:{bg_color};">
    <div style="font-size:13px;font-weight:700;color:{hd_color};margin-bottom:8px;">💡 מנוע הסברה — {c_label}</div>
    <table style="width:100%;font-size:12px;border-collapse:collapse;">
      {rows_html_c}
    </table>
  </div>'''

    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:860px;margin:0 auto 24px;
     border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;background:#fff;">
  <div style="background:{color};color:#fff;padding:14px;text-align:center;">
    <div style="font-size:26px;font-weight:800;">{ticker}</div>
    <div style="opacity:.9;margin-top:4px;">{alert.get('pattern_type','')}</div>
  </div>
  {company_html}
  <div style="display:flex;gap:10px;padding:14px;border-bottom:1px solid #e5e7eb;">
    <div style="flex:1;background:#ecfdf5;border:2px solid #22c55e;border-radius:8px;
                padding:10px;text-align:center;">
      <div style="font-size:11px;color:#374151;font-weight:700;">ציון</div>
      <div style="font-size:26px;font-weight:900;color:#166534;">{score:.0f}/100</div>
    </div>
    <div style="flex:1;background:#fffbeb;border:2px solid #f59e0b;border-radius:8px;
                padding:10px;text-align:center;">
      <div style="font-size:11px;color:#374151;font-weight:700;">R:R</div>
      <div style="font-size:26px;font-weight:900;color:#92400e;">{rr:.2f}:1</div>
    </div>
    <div style="flex:1;background:#eff6ff;border:2px solid #3b82f6;border-radius:8px;
                padding:10px;text-align:center;">
      <div style="font-size:11px;color:#374151;font-weight:700;">כניסה</div>
      <div style="font-size:26px;font-weight:900;color:#1d4ed8;">${entry:.2f}</div>
    </div>
  </div>
  <div style="padding:14px;border-bottom:1px solid #e5e7eb;">
    <h3 style="margin:0 0 8px;color:#1d4ed8;">ניהול סיכונים</h3>
    <table style="width:100%;font-size:13px;border-collapse:collapse;">
      <tr>
        <td style="padding:6px;border-bottom:1px solid #f3f4f6;">סטופ-לוס</td>
        <td style="padding:6px;border-bottom:1px solid #f3f4f6;font-weight:800;color:#b91c1c;">${stop:.2f}</td>
        <td style="padding:6px;border-bottom:1px solid #f3f4f6;">סיכון/מניה: <b>${risk_ps:.2f}</b></td>
      </tr>
      <tr>
        <td style="padding:6px;">יעד</td>
        <td style="padding:6px;font-weight:800;color:#166534;">${target:.2f}</td>
        <td style="padding:6px;">גודל פוזיציה: <b>{size:,}</b></td>
      </tr>
    </table>
  </div>
  <div style="padding:14px;border-bottom:1px solid #e5e7eb;">
    <h3 style="margin:0 0 8px;color:#1d4ed8;">קריטריונים שאומתו</h3>
    <ul style="margin:0;padding-right:16px;">{ok_lis}</ul>
  </div>
  {warns}
  {sector_html}
  {intel_html}
  {catalyst_html}
  <div style="padding:10px;text-align:center;font-size:11px;color:#6b7280;">
    נשלח: <b>{send_date}</b>
  </div>
</div>"""

def create_chart(ticker: str, df: pd.DataFrame, alert: dict) -> str | None:
    if not PLOTLY_AVAILABLE or df is None:
        return None
    try:
        # חלון גרף: לפי סוג הדפוס — Double Bottom צריך יותר בר
        chart_lookback = DB_LOOKBACK if alert.get("pattern_type","").startswith("Double") else TRIANGLE_LOOKBACK
        df_c = df.tail(max(chart_lookback, 60)).copy()
        fig  = go.Figure(data=[go.Candlestick(
            x=df_c.index, open=df_c["open"], high=df_c["high"],
            low=df_c["low"],  close=df_c["close"], name="Price",
        )])
        if "ema28" in df_c.columns:
            fig.add_trace(go.Scatter(x=df_c.index, y=df_c["ema28"], name="EMA28",
                                      line=dict(color="blue", width=1)))
        if "ma150" in df_c.columns:
            fig.add_trace(go.Scatter(x=df_c.index, y=df_c["ma150"], name="MA150",
                                      line=dict(color="orange", width=1, dash="dash")))
        bl = alert.get("breakout_level")
        if bl:
            fig.add_hline(y=bl, line_color="green", line_dash="dash", line_width=2)
        sl = alert.get("stop_loss")
        if sl:
            fig.add_hline(y=sl, line_color="red",   line_dash="dash", line_width=1)
        tg = alert.get("target")
        if tg:
            fig.add_hline(y=tg, line_color="purple",line_dash="dash", line_width=1)
        fig.update_layout(
            title=f"{ticker} — {alert.get('pattern_type','')} (Score: {alert.get('score',0):.1f})",
            xaxis_rangeslider_visible=False, height=550, width=880,
        )
        path = os.path.join(CHARTS_DIR, f"{ticker}_{int(time.time())}.png")
        pio.write_image(fig, path)
        return path
    except Exception as e:
        log(f"create_chart error {ticker}: {e}")
        return None

def send_email_alerts(alerts: list[dict]) -> None:
    if not alerts:
        return
    if not APP_PASSWORD:
        log("APP_PASSWORD not set — email disabled."); return

    alerts = sorted(alerts, key=lambda a: float(a.get("score",0) or 0), reverse=True)
    now_str   = datetime.now().strftime("%d/%m/%Y")
    subj_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    ci_cache  = {}
    all_attachments = []

    header = f"""<html><head><meta charset="utf-8"></head>
<body dir="rtl" style="margin:0;padding:0;background:#f3f4f6;">
<div style="max-width:900px;margin:0 auto;padding:12px;text-align:center;">
  <h2 style="color:#1d4ed8;">🚨 סורק המניות — {len(alerts)} התראות ({subj_str})</h2>
</div>"""
    footer = """<p style="text-align:center;font-size:11px;color:#6b7280;margin:20px 0;">
  הודעה אוטומטית — אינה מהווה ייעוץ פיננסי.</p></body></html>"""

    cards = ""
    sector_cache: dict = {}
    intel_cache:  dict = {}
    for idx, alert in enumerate(alerts):
        ticker = (alert.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        if ticker not in ci_cache:
            ci_cache[ticker] = get_company_card(ticker)
        company = ci_cache[ticker]

        # ניתוח סקטור
        if ticker not in sector_cache:
            try:
                result = get_sector_analysis(ticker)
                sector_cache[ticker] = result if isinstance(result, dict) else {"summary": "N/A", "news": [], "sentiment": "NEUTRAL"}
            except Exception:
                sector_cache[ticker] = {"summary": "N/A", "news": [], "sentiment": "NEUTRAL"}
        sector_data = sector_cache[ticker]
        if not isinstance(sector_data, dict):
            sector_data = {"summary": "N/A", "news": [], "sentiment": "NEUTRAL"}
        alert["_sector"] = sector_data

        # ── Deep Intelligence ──────────────────────────────────
        if ticker not in intel_cache:
            try:
                df_for_intel = alert.get("_df")
                intel_cache[ticker] = get_deep_intelligence(ticker, df_for_intel)
            except Exception as e:
                intel_cache[ticker] = {}
        intel_data = intel_cache[ticker] if isinstance(intel_cache.get(ticker), dict) else {}

        # ── Catalyst Engine ────────────────────────────────────
        try:
            catalyst_data = get_catalyst_engine(ticker, company)
        except Exception as e:
            log(f"catalyst_engine error {ticker}: {e}")
            catalyst_data = {}

        # ── RS Score ───────────────────────────────────────────
        try:
            df_for_rs = alert.get("_df")
            rs_data   = compute_rs_score(ticker, df_for_rs) if df_for_rs is not None else {}
        except Exception:
            rs_data = {}

        chart_path = None
        df = alert.pop("_df", None)   # ← מוחק מה-alert לשחרור RAM
        if df is None or (hasattr(df, "empty") and df.empty):
            df = fetch_data_twelvedata(ticker, outputsize=TRIANGLE_LOOKBACK + 10)
        if df is not None and not df.empty:
            # חישוב אינדיקטורים רק אם חסרים (df מ-scan_ticker כבר מחושב)
            if "atr14" not in df.columns:
                try:
                    add_technical_indicators(df)
                except Exception:
                    pass
            chart_path = create_chart(ticker, df, alert)

        cards += f'<div style="max-width:900px;margin:0 auto;padding:8px 0;">' \
                 f'{_build_html_card(alert, company, now_str, sector_data, intel_data, catalyst_data, rs_data)}</div>'

        if chart_path and os.path.exists(chart_path):
            cid = f"chart_{ticker}_{idx}"
            cards += (f'<div style="max-width:900px;margin:0 auto;text-align:center;padding-bottom:20px;">'
                      f'<img src="cid:{cid}" alt="{ticker}" style="max-width:840px;border-radius:10px;"></div>')
            try:
                part = MIMEBase("image","png")
                with open(chart_path,"rb") as fp:
                    part.set_payload(fp.read())
                encoders.encode_base64(part)
                part.add_header("Content-ID", f"<{cid}>")
                part.add_header("Content-Disposition","inline",filename=os.path.basename(chart_path))
                all_attachments.append(part)
            except Exception as e:
                log(f"chart attach error: {e}")
            try:
                os.remove(chart_path)
            except Exception:
                pass
        cards += '<hr style="border:1px solid #ddd;max-width:900px;margin:0 auto;">'

    msg = MIMEMultipart("related")
    msg["From"]    = FROM_EMAIL
    msg["To"]      = ", ".join(TO_EMAILS)
    if len(alerts) == 1:
        a = alerts[0]
        msg["Subject"] = f"📈 {a.get('ticker','')} | {a.get('pattern_type','')} | Score {a.get('score',0):.1f}"
    else:
        msg["Subject"] = f"🚨 {len(alerts)} התראות פריצה — {subj_str}"
    msg.attach(MIMEText(header + cards + footer, "html", "utf-8"))
    for att in all_attachments:
        msg.attach(att)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(FROM_EMAIL, APP_PASSWORD)
            srv.sendmail(FROM_EMAIL, TO_EMAILS, msg.as_string())
        log(f"Email sent: {len(alerts)} alert(s) to {len(TO_EMAILS)} recipients.")
    except Exception as e:
        log(f"Email send error: {e}")

# ============================================================
#  MAIN LOOP
# ============================================================




# ============================================================
#  PRE-FILTER — סינון מהיר לפני הסריקה המלאה
#  מוריד 35 ימים ל-כל המניות בbatch → מסנן לפי EMA28
#  חיסכון: ~80% מהמניות נדחות לפני הורדת נתונים מלאים
# ============================================================

PREFILTER_BATCH_SIZE = int(os.getenv("PREFILTER_BATCH_SIZE", "200"))  # מניות לבקשה אחת
PREFILTER_PERIOD     = "35d"   # מספיק ל-EMA28


def prefilter_by_ema28(ticker_list: list[str]) -> tuple[list[str], list[str]]:
    """
    מוריד 35 ימים לכל המניות ב-batch וסורן לפי EMA28.
    מחזיר (passed, failed) — passed ממשיכות לסריקה מלאה.
    
    קריטריון מעבר:
    - מחיר מעל EMA28
    - EMA28 בשיפוע עולה
    - מחיר לא יותר מ-EMA28_MAX_DIST_PCT מעל EMA28
    """
    passed = []
    failed = []
    total  = len(ticker_list)
    
    log(f"⚡ Pre-filter: בודק {total} מניות לפי EMA28...")
    
    # עבד ב-batches
    for batch_start in range(0, total, PREFILTER_BATCH_SIZE):
        batch = ticker_list[batch_start:batch_start + PREFILTER_BATCH_SIZE]
        
        try:
            # הורד batch אחד
            raw = yf.download(
                batch,
                period=PREFILTER_PERIOD,
                interval="1d",
                progress=False,
                auto_adjust=True,
                group_by="ticker",
                threads=True,
            )
            
            if raw is None or raw.empty:
                # אם batch נכשל — תן ל-scan_ticker לטפל
                passed.extend(batch)
                continue
            
            for ticker in batch:
                try:
                    # שלוף נתוני הטיקר
                    if isinstance(raw.columns, pd.MultiIndex):
                        if ticker not in raw.columns.get_level_values(0):
                            passed.append(ticker)  # לא ידוע — תן לעבור
                            continue
                        df_t = raw[ticker].copy()
                    else:
                        # batch של מניה אחת
                        df_t = raw.copy()
                    
                    df_t = df_t.dropna(how="all")
                    if len(df_t) < 10:
                        passed.append(ticker)
                        continue
                    
                    # נרמל עמודות
                    df_t.columns = [str(c).lower() for c in df_t.columns]
                    if "close" not in df_t.columns:
                        passed.append(ticker)
                        continue
                    
                    close = df_t["close"].squeeze()
                    if hasattr(close, "columns"):  # עדיין DataFrame
                        close = close.iloc[:, 0]
                    
                    # חשב EMA28
                    ema28     = close.ewm(span=28, adjust=False).mean()
                    price_now = float(close.iloc[-1])
                    ema_now   = float(ema28.iloc[-1])
                    ema_prev  = float(ema28.iloc[-2]) if len(ema28) >= 2 else ema_now
                    
                    if ema_now <= 0:
                        passed.append(ticker)
                        continue
                    
                    dist = (price_now - ema_now) / ema_now
                    
                    # סנן: מתחת ל-EMA28 או רחוק מדי מעליו
                    if dist < 0:
                        failed.append(ticker)
                        continue
                    if dist > EMA28_MAX_DIST_PCT:
                        failed.append(ticker)
                        continue
                    # EMA28 חייב לעלות
                    if EMA28_REQUIRE_RISING and ema_now <= ema_prev:
                        failed.append(ticker)
                        continue
                    
                    passed.append(ticker)
                    
                except Exception:
                    passed.append(ticker)  # במקרה של שגיאה — תן לעבור
                    
        except Exception as e:
            log(f"Pre-filter batch error: {e}")
            passed.extend(batch)  # batch שנכשל — כולם עוברים
        
        done = batch_start + len(batch)
        log(f"   Pre-filter: {done}/{total} — עברו: {len(passed)} | נכשלו: {len(failed)}")
    
    pct_saved = len(failed) / max(total, 1) * 100
    log(f"⚡ Pre-filter סיים: {len(passed)} עוברות | {len(failed)} נדחו ({pct_saved:.0f}% חיסכון)")
    return passed, failed


def main() -> None:
    log("=" * 60)
    log("Stock Scanner Unified — START")
    log("=" * 60)
try:
    msg = MIMEText("GitHub Actions הצליח להתחבר ל-Gmail ולשלוח מייל בדיקה.", "plain", "utf-8")
    msg["Subject"] = "✅ Stock Scanner Email Test"
    msg["From"] = FROM_EMAIL
    msg["To"] = FROM_EMAIL
    msg["Bcc"] = ", ".join(TO_EMAILS)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(FROM_EMAIL, APP_PASSWORD)
        smtp.send_message(msg)

    log(f"✅ Test email sent successfully to {len(TO_EMAILS)} recipients")

except Exception as e:
    log(f"❌ Test email failed: {e}")
    # ── מנע Sleep במהלך הסריקה ──────────────────────────────
    try:
        import ctypes
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED — מונע שינה עד סיום
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000002)
        log("✅ Sleep prevention: active")
    except Exception:
        pass

    # ── טען פרמטרים שנלמדו מריצות קודמות ──────────────────
    _apply_learned_params()

    # ── עדכן ביצועי סטאפים קודמים (5/10/20 יום) ────────────
    try:
        update_performance_log()
    except Exception as e:
        log(f"update_performance_log error: {e}")

    # ── בדוק פוזיציות פתוחות — Trailing Stop + יציאות ──────
    try:
        exit_alerts = run_position_tracker(send_exit_email_fn=send_exit_email)
        if exit_alerts:
            log(f"📤 {len(exit_alerts)} exit alerts sent")
    except Exception as e:
        log(f"Position tracker error: {e}")

    # בדיקת תנאים הכרחיים לפני ריצה
    if not APP_PASSWORD:
        log("❌ FATAL: APP_PASSWORD לא הוגדר. הגדר את משתנה הסביבה APP_PASSWORD ואז הרץ שוב.")
        log("   דוגמה: export APP_PASSWORD='xxxx xxxx xxxx xxxx'")
        return
    if not API_KEYS:
        log("❌ FATAL: אין מפתחות TwelveData API. הגדר TWELVEDATA_API_KEYS.")
        return
    # ── בנה Universe — כל מניות NYSE + NASDAQ מעל $1B ──────
    global tickers
    if not tickers:
        log("🌐 Loading universe (first run or no cache)...")
        tickers = build_universe()
    if not tickers:
        log("❌ No tickers available. Exiting.")
        return
    log(f"✅ הגדרות: {len(API_KEYS)} API keys, {len(tickers)} טיקרים (universe), MIN_SCORE={MIN_ALERT_SCORE}")

    # ── בנה מפת רוטציה סקטורים — פעם אחת לכל הריצה ─────────
    try:
        build_sector_rotation_map()
    except Exception as e:
        log(f"Sector rotation build error: {e}")

    # ── Market Regime — האם השוק מאפשר קניות? ───────────────
    regime = get_market_regime()
    if not regime.get("allow_trading", True):
        log("⛔ BEAR Market detected — skipping scan. Set REGIME_ENABLED=False to override.")
        return

    # ── ציון מינימלי דינמי לפי מצב השוק ─────────────────────
    dynamic_score = get_dynamic_min_score()
    regime_name   = regime.get("regime", "NEUTRAL")
    log(f"🎯 MIN_ALERT_SCORE דינמי: {dynamic_score} (Regime={regime_name})")

    # ── Market Reversal Detector ──────────────────────────────
    try:
        run_market_reversal_detector()
    except Exception as e:
        log(f"Market Reversal Detector error: {e}")

    blocklist     = load_blocklist()
    alert_history = load_alert_history()

    all_alerts: list[dict] = []
    stats = {"scanned":0, "skipped_bl":0, "no_alert":0, "found":0, "errors":0}
    filter_stats = {
        "market_cap":    0,
        "no_data":       0,
        "earnings":      0,
        "ema28":         0,
        "gap":           0,
        "volume":        0,
        # "rs_score" הוסר,
        "ma150_dist":    0,
        "ma_neckline":   0,
        "no_pattern":    0,
        "score_low":     0,
        "reverse_scan":  0,
        "passed":        0,
    }

    ticker_list = list(tickers)
    random.shuffle(ticker_list)

    # ── Pre-filter מהיר לפי EMA28 ────────────────────────────
    log("⚡ מריץ Pre-filter...")
    try:
        ticker_list, prefilter_failed = prefilter_by_ema28(ticker_list)
        filter_stats["ema28"] += len(prefilter_failed)
        log(f"⚡ Pre-filter: {len(ticker_list)} מניות ממשיכות לסריקה מלאה")
    except Exception as e:
        log(f"Pre-filter error — ממשיך בלעדיו: {e}")

    total = len(ticker_list)

    for i, raw in enumerate(ticker_list, 1):
        symbol = raw.strip().upper().replace("$","")
        if not symbol:
            continue
        if symbol in blocklist:
            stats["skipped_bl"] += 1
            continue

        # כל 50 טיקרים: רענון blocklist + שמירת היסטוריה
        if i % 50 == 0:
            try: blocklist = load_blocklist()
            except Exception: pass
            try: save_alert_history(alert_history)
            except Exception: pass

        log(f"[{i}/{total}] {symbol}")
        stats["scanned"] += 1
        time.sleep(SCAN_DELAY_SECONDS)

        try:
            new = scan_ticker(symbol, alert_history, filter_stats, min_score=dynamic_score) or []
            if not new:
                stats["no_alert"] += 1
                continue
            for alert in new:
                all_alerts.append(alert)
                record_alert_sent(symbol, alert.get("pattern_type",""),
                                  alert.get("breakout_level"), alert_history)
                log_setup_for_tracking(alert)
                open_position(alert)
                log_to_csv(symbol, alert.get("meta", {}).get("score_reasons", []))
            stats["found"] += len(new)
        except Exception:
            stats["errors"] += 1
            log(f"Critical error {symbol}:\n{traceback.format_exc()}")

    # נקה _df (DataFrames) לפני שמירה ל-JSON
    for alerts_list in [all_alerts]:
        for a in alerts_list:
            a.pop("_df", None)
    save_alert_history(alert_history)

    if not all_alerts:
        log("No valid setups found today.")
    else:
        all_alerts.sort(key=lambda a: float(a.get("score",0) or 0), reverse=True)
        # בחר את הסטאפ הכי טוב לכל טיקר, אחרי שכל הסריקה הסתיימה
        best_per_ticker = {}
        for a in all_alerts:
            t = a.get("ticker","")
            if t not in best_per_ticker:
                best_per_ticker[t] = a  # הראשון = הכי גבוה (כבר ממוין)
        # מיין את הטיקרים לפי ציון הסטאפ הטוב ביותר שלהם ובחר TOP 3
        top = sorted(best_per_ticker.values(),
                     key=lambda a: float(a.get("score",0) or 0),
                     reverse=True)[:TOP_ALERTS_TO_SEND]
        # הוסף trophy רק למייל — לא מוטציה על dict שכבר נרשם בהיסטוריה
        top_for_email = [dict(a) for a in top]  # shallow copy
        if top_for_email:
            top_for_email[0]["pattern_type"] = "🏆 " + top_for_email[0].get("pattern_type","Best Setup")
        try:
            send_email_alerts(top_for_email)
            stats["sent"] = len(top)
            log("TOP: " + ", ".join(f"{a['ticker']}({a['score']})" for a in top))
        except Exception:
            stats["errors"] += 1
            log(f"Email error:\n{traceback.format_exc()}")

    log(f"STATS: {stats}")

    # ── דוח פילטרים מפורט ──────────────────────────────────
    total_scanned = stats["scanned"]
    log("=" * 60)
    log("📊 FILTER BREAKDOWN:")
    log(f"   {'מניות נסרקו':<28} {total_scanned:>6,}")
    log(f"   {'❌ Market Cap / No Data':<28} {filter_stats['market_cap'] + filter_stats['no_data']:>6,}  ({(filter_stats['market_cap']+filter_stats['no_data'])/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'⏭️  דוחות קרובים':<28} {filter_stats['earnings']:>6,}  ({filter_stats['earnings']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'❌ EMA28 (מרחק > 3%)':<28} {filter_stats['ema28']:>6,}  ({filter_stats['ema28']/max(total_scanned,1)*100:.0f}%)")
    # gap filter הוסר
    # volume filter הוסר
    # RS Score filter הוסר
    log(f"   {'❌ MA150 רחוק (> 5%)':<28} {filter_stats['ma150_dist']:>6,}  ({filter_stats['ma150_dist']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'❌ MA לא קרוב ל-neckline':<28} {filter_stats['ma_neckline']:>6,}  ({filter_stats['ma_neckline']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'❌ לא נמצאה תבנית':<28} {filter_stats['no_pattern']:>6,}  ({filter_stats['no_pattern']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'✅ עברו הכל ונשלחו':<28} {stats.get('sent',0):>6,}")
    log("=" * 60)

    # ── Self-Learning — רץ כל שבת ──────────────────────────
    try:
        if datetime.now().weekday() == 5:  # שבת = 5
            log("🧠 Saturday — running self-learning analysis...")
            run_self_learning()
        else:
            # גם בימי חול — טען ויישם פרמטרים אם קיימים
            days_left = 5 - datetime.now().weekday()
            if days_left < 0:
                days_left += 7
            log(f"🧠 Self-learning scheduled for Saturday ({days_left} days away)")
    except Exception as e:
        log(f"Self-learning error: {e}")

    log("Stock Scanner Unified — DONE")
    log("=" * 60)

    # ── שחרר Sleep prevention ────────────────────────────────
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass

if __name__ == "__main__":
    main()
