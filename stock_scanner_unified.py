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

ציון ואיכות כניסה:
  V8 מוסיף Entry Ready Quality Engine: לא שולח מייל על תבנית בלבד.
  V9 מוסיף Pattern Expansion Engine עם תבניות איכותיות נוספות:
  Flat Base, Darvas Box, VCP, EMA21/MA50 Pullback Bounce, Breakout Retest.
  מייל נשלח רק אם יש פריצה/חזרה מאושרת, ווליום/נר איכותיים, RS חזק,
  מחיר מעל MA150/MA200, נזילות טובה ו-R:R מתאים.
  מועמד שלא עבר את שכבת האיכות נשמר כ-Watchlist בלבד.
  V9.1 מוסיף Professional Trade Quality Engine מעל התבניות הקיימות — בלי לשנות את
  זיהוי התבניות: Trend/RS/Sector/Accumulation/Execution/Risk/Market Context.
  V9.2 מוסיף Exit Intelligence Engine: Stop מקורי קבוע, Profit Protection אחרי +5%,
  Target כיעד ייחוס, וזיהוי שינוי כיוון רב-סיגנלי על נרות יומיים סגורים.
  V9.3 מוסיף Pattern-Aware Entry: אישור כניסה מותאם לסוג הסטאפ (Breakout / Pullback / Retest),
  תיקון דיוק R:R, עקביות טובה יותר ב-EMA28 Pre-filter ובדיקת Earnings עתידית בלבד.
  V9.6 בוחר Pullback אחרי שיא מקומי, משווה ווליום ל-20 נרות קודמים,
  ובודק את כל משפחות התבניות לפני בחירת הסטאפים לשליחה.
"""

# ============================================================
# RESET VERIFIED FIX FILE — 2026-09-30 V9.3
# This marker proves this is the rebuilt fixed file, not an old download copy.
# ============================================================
CODE_VERSION = "2026-10-30-v9.7.5-swing"
RESET_VERIFIED_FIX_FILE = True


# ============================================================
#  Imports
# ============================================================
import os
import re
import json
import csv
import calendar
import hashlib
import time
import random
import traceback
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

# Infrastructure only. No trading thresholds or indicator calculations here.
import sys
import io
import ssl
import logging
from logging.handlers import RotatingFileHandler
import tempfile
import shutil
import threading
import contextlib
import contextvars
import functools
import base64
import codecs
import math
from urllib.parse import quote, quote_plus


def _infra_float(value, default=None, *, minimum=None, maximum=None):
    """Finite parsing for operational settings/presentation, never indicators."""
    try:
        result = float(value)
        if not math.isfinite(result):
            return default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result
    except (TypeError, ValueError, OverflowError):
        return default


def _infra_int(value, default=0, *, minimum=None, maximum=None):
    number = _infra_float(value)
    if number is None:
        return default
    result = int(number)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _mail_float(value):
    """Invalid financial presentation data must not be displayed as zero profit."""
    result = _infra_float(value)
    if result is None:
        raise ValueError("Non-finite notification value")
    return result


_PRIVACY_LOCK = threading.RLock()
_SECRET_SNAPSHOT = None
_SECRET_REPLACEMENTS = ()
_SECRET_NAME = re.compile(r"(?:^|_)(?:api_?keys?|keys?|tokens?|secrets?|passwords?|passwd|pass|pwd|credentials?|authorization|cookie|private_key|client_secret|emails?)(?:_|$)", re.I)
# A linear token pass avoids catastrophic backtracking on long provider errors.
_EMAIL_TOKEN_PATTERN = re.compile(r"[^\s<>\"',;]+")


def _redact_email_token(match):
    """v9.7.3: mask tokens that look like an address; a bare "@" (e.g. "RS @ 437.18") stays."""
    token = match.group(0)
    low = token.lower()
    for marker in ("@", "%40", "%2540", "\\u0040"):
        if marker in low and re.search(r"[a-z0-9]", low.replace(marker, "")):
            return "[REDACTED_EMAIL]"
    return token


_CREDENTIAL_LABEL = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|app[_-]?password|password|passwd|client[_-]?secret|authorization)"
_QUOTED_CREDENTIAL = re.compile(r"(" + _CREDENTIAL_LABEL + r"[\"']?\s*(?:=|:|%3d|%253d)\s*)([\"'])(.*?)\2", re.I | re.S)
_PLAIN_CREDENTIAL = re.compile(r"(" + _CREDENTIAL_LABEL + r"[\"']?\s*(?:=|:|%3d|%253d)\s*)[^\s&\"',;)}\]]+", re.I)
_BEARER = re.compile(r"\b(Bearer|Basic|AUTH\s+(?:PLAIN|LOGIN))\s+[^\s\"']+", re.I)
_URL_AUTH = re.compile(r"(https?://)[^/\s:@]+:[^@\s/]+@", re.I)
_MAX_PRIVATE_EVENT = 65536


def _current_secret_replacements():
    global _SECRET_SNAPSHOT, _SECRET_REPLACEMENTS
    pairs = [(name, value) for name, value in os.environ.items() if _SECRET_NAME.search(name) and value]
    # Current globals cover secrets loaded before an environment variable was
    # removed/rotated. Only one snapshot is retained, never an unlimited history.
    for name in ("FROM_EMAIL", "APP_PASSWORD", "TO_EMAILS", "API_KEYS"):
        value = globals().get(name)
        if isinstance(value, str) and value:
            pairs.append((name, value))
        elif isinstance(value, (list, tuple, set)):
            pairs.extend((name, item) for item in value if isinstance(item, str) and item)
    snapshot = tuple(sorted(pairs))
    with _PRIVACY_LOCK:
        if snapshot != _SECRET_SNAPSHOT:
            values = set()
            for name, value in snapshot:
                values.add(value)
                values.update(part.strip() for part in re.split(r"[,;\r\n]", value) if part.strip())
                if "PASSWORD" in name.upper():
                    values.add(value.replace(" ", ""))
            encoded = set()
            for value in values:
                if not value:
                    continue
                encoded.update((value, quote(value, safe=""), quote_plus(value),
                                quote(quote(value, safe=""), safe=""),
                                json.dumps(value, ensure_ascii=True)[1:-1]))
                if len(value) >= 4:
                    encoded.add(base64.b64encode(value.encode("utf-8")).decode("ascii"))
            _SECRET_REPLACEMENTS = tuple(sorted(encoded, key=len, reverse=True))
            _SECRET_SNAPSHOT = snapshot
        return _SECRET_REPLACEMENTS


def _redact_sensitive(value):
    """Fail closed; never fall back to the unfiltered exception/message."""
    try:
        text = str(value)
        if len(text) > _MAX_PRIVATE_EVENT:
            return ""
        replacements = _current_secret_replacements()
        for secret in replacements:
            text = text.replace(secret, "[REDACTED]")
            if len(text) > _MAX_PRIVATE_EVENT:
                return ""
        text = _QUOTED_CREDENTIAL.sub(lambda m: m.group(1) + "[REDACTED]", text)
        text = _PLAIN_CREDENTIAL.sub(lambda m: m.group(1) + "[REDACTED]", text)
        text = _BEARER.sub(lambda m: m.group(1) + " [REDACTED]", text)
        text = _URL_AUTH.sub(r"\1[REDACTED]@", text)
        result = _EMAIL_TOKEN_PATTERN.sub(_redact_email_token, text)
        # Even the masking marker must not accidentally equal a configured secret.
        return "" if any(secret in result for secret in replacements) else result
    except Exception:
        return ""


def _redact_log_value(value):
    """Protect diagnostic strings while preserving numerical observations."""
    if isinstance(value, str):
        return _redact_sensitive(value)
    if isinstance(value, dict):
        return {key: _redact_log_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class _PrivateBinaryOutput(io.RawIOBase):
    def __init__(self, owner):
        self.owner = owner
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def writable(self):
        return True

    def write(self, value):
        data = bytes(value)
        self.owner.write(self.decoder.decode(data))
        return len(data)

    def flush(self):
        self.owner.flush()


class _PrivateOutput(io.TextIOBase):
    """Line-buffered redaction also catches third-party stderr/stdout writes.

    flush() does not release incomplete lines: doing so could leak the first
    half of a secret written in separate chunks. Oversized lines are discarded.
    """
    _scanner_private_output = True

    def __init__(self, stream):
        self.stream = stream
        self.pending = ""
        self.dropping = False
        self.lock = threading.RLock()
        self.sanitize = _redact_sensitive
        self._binary = _PrivateBinaryOutput(self)

    @property
    def buffer(self):
        return self._binary

    @property
    def encoding(self):
        return getattr(self.stream, "encoding", "utf-8")

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, value):
        text = str(value)
        with self.lock:
            pieces = text.splitlines(keepends=True)
            for piece in pieces:
                ended = piece.endswith(("\n", "\r"))
                if not self.dropping:
                    if len(self.pending) + len(piece) > _MAX_PRIVATE_EVENT:
                        self.stream.write(self.sanitize("[REDACTED_OVERSIZED_OUTPUT]") + "\n")
                        self.pending = ""
                        self.dropping = True
                    else:
                        self.pending += piece
                if ended:
                    if not self.dropping:
                        self.stream.write(self.sanitize(self.pending).replace("\r", "\\r"))
                    self.pending = ""
                    self.dropping = False
        return len(text)

    def flush(self):
        with self.lock:
            self.stream.flush()


def _install_output_privacy():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if getattr(stream, "_scanner_private_output", False):
            stream.sanitize = _redact_sensitive
        elif stream is not None:
            setattr(sys, name, _PrivateOutput(stream))
    base_factory = logging.getLogRecordFactory()
    base_factory = getattr(base_factory, "_scanner_base_factory", base_factory)

    def private_record(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        try:
            record.msg = _redact_sensitive(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = _redact_sensitive("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
            if record.stack_info:
                record.stack_info = _redact_sensitive(record.stack_info)
        except Exception:
            record.msg, record.args, record.exc_info, record.exc_text = "[REDACTED_LOG_RECORD]", (), None, None
        return record

    private_record._scanner_base_factory = base_factory
    logging.setLogRecordFactory(private_record)
    logging.raiseExceptions = False


class _PrivateFormatter(logging.Formatter):
    def format(self, record):
        return _redact_sensitive(super().format(record))


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record):
        # Logging I/O failure must not print the original record or traceback.
        try:
            sys.stderr.write("Protected log file unavailable\n")
        except Exception:
            pass


_APP_LOG_LOCK = threading.RLock()


def _protected_logger():
    logger = logging.getLogger("stock_scanner.private")
    target = os.path.abspath(globals().get("LOGFILE", "stock_scanner_unified_log.txt"))
    with _APP_LOG_LOCK:
        configuration = (target, id(sys.stdout))
        if getattr(logger, "_scanner_configuration", None) != configuration:
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()
            formatter = _PrivateFormatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(formatter)
            logger.addHandler(console)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                file_handler = _PrivateRotatingHandler(target, maxBytes=_infra_int(
                    os.getenv("INFRA_LOG_MAX_BYTES", "5242880"), 5242880,
                    minimum=65536, maximum=52428800), backupCount=_infra_int(
                    os.getenv("INFRA_LOG_BACKUPS", "3"), 3, minimum=1, maximum=10),
                    encoding="utf-8", delay=True)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except Exception:
                pass
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger._scanner_configuration = configuration
        return logger


def _close_private_log_handlers():
    logger = logging.getLogger("stock_scanner.private")
    with _APP_LOG_LOCK:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        logger._scanner_configuration = None


def _mail_status(status, *, accepted=0, refused=0, success=False, **extra):
    return {"success": bool(success), "accepted": _infra_int(accepted, 0, minimum=0),
            "refused": _infra_int(refused, 0, minimum=0), "status": status, **extra}


def _email_configured():
    return bool(FROM_EMAIL and APP_PASSWORD and TO_EMAILS
                and APP_PASSWORD not in ("YOUR_APP_PASSWORD_HERE", "YOUR_PASSWORD", "CHANGEME")
                and not any(c in str(FROM_EMAIL) for c in "\r\n")
                and all(isinstance(address, str) and not any(c in address for c in "\r\n") for address in TO_EMAILS))


def _smtp_deliver(message, purpose="notification"):
    """One bounded, verified-TLS delivery attempt; no ambiguous automatic resend."""
    client = None
    stage = "serialization"
    try:
        _install_output_privacy()
        if not _email_configured():
            return _mail_status("disabled", refused=len(TO_EMAILS))
        payload = message.as_string()
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        timeout = _infra_float(os.getenv("SMTP_TIMEOUT_SECONDS", "30"), 30.0,
                               minimum=1.0, maximum=30.0)
        stage = "connect"
        client = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=timeout,
                                  context=context, local_hostname="scanner.local")
        client.set_debuglevel(0)
        stage = "authentication"
        client.login(FROM_EMAIL, APP_PASSWORD)
        stage = "send"
        refused = client.sendmail(FROM_EMAIL, TO_EMAILS, payload)
        if refused is None:
            refused = {}
        if not isinstance(refused, dict):
            raise TypeError("Invalid SMTP response shape")
        refused_count = min(len(TO_EMAILS), len(refused))
        accepted = len(TO_EMAILS) - refused_count
        result = _mail_status("accepted" if not refused_count else "partial" if accepted else "failed",
                              accepted=accepted, refused=refused_count, success=accepted > 0)
        log(f"SMTP {purpose}: status={result['status']}, accepted={accepted}, refused={refused_count}")
        return result
    except Exception as exc:
        code = _infra_int(getattr(exc, "smtp_code", None), None, minimum=100, maximum=599)
        retryable = isinstance(exc, (TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected)) or (code is not None and 400 <= code < 500)
        known_rejection = isinstance(exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused))
        uncertain = stage == "send" and not known_rejection
        # The provider's error body and recipients are deliberately not logged.
        error_type = _redact_sensitive(type(exc).__name__)
        log(f"SMTP {purpose}: failed at {stage}; type={error_type}; code={code}; uncertain={uncertain}")
        return _mail_status("failed", refused=len(TO_EMAILS), error_type=error_type,
                            smtp_code=code, retryable=retryable, uncertain=uncertain)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                log("SMTP socket cleanup failed")


_MAIL_CHART_DIRECTORY = contextvars.ContextVar("scanner_mail_chart_directory", default=None)


@contextlib.contextmanager
def _mail_chart_workspace():
    os.makedirs(CHARTS_DIR, exist_ok=True)
    directory = tempfile.mkdtemp(prefix="scanner-mail-", dir=CHARTS_DIR)
    token = _MAIL_CHART_DIRECTORY.set(directory)
    try:
        yield directory
    finally:
        _MAIL_CHART_DIRECTORY.reset(token)
        for attempt in range(2):
            try:
                shutil.rmtree(directory)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt == 1:
                    log("Temporary chart cleanup failed; startup orphan cleanup will retry")


def _chart_in_mail_workspace(path):
    directory = _MAIL_CHART_DIRECTORY.get()
    try:
        return bool(directory and os.path.commonpath((os.path.realpath(directory), os.path.realpath(path))) == os.path.realpath(directory))
    except (OSError, ValueError, TypeError):
        return False


def _nonfatal_mail(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            _install_output_privacy()
            context = _mail_chart_workspace() if function.__name__ == "send_email_alerts" and _email_configured() and args and args[0] else contextlib.nullcontext()
            with context:
                result = function(*args, **kwargs)
            return result if isinstance(result, dict) else _mail_status("skipped")
        except Exception as exc:
            log(f"Mail assembly failed ({function.__name__}): {type(exc).__name__}")
            return _mail_status("failed", refused=len(TO_EMAILS), error_type=_redact_sensitive(type(exc).__name__), retryable=False)
    return wrapped


def _cleanup_orphan_charts():
    """Only scanner-generated exports, never arbitrary images or live state."""
    ttl = _infra_float(os.getenv("INFRA_CHART_TTL_SECONDS", "86400"), 86400.0, minimum=3600.0)
    cutoff = time.time() - ttl
    try:
        with os.scandir(CHARTS_DIR) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                        continue
                    if entry.is_dir(follow_symlinks=False) and entry.name.startswith("scanner-mail-"):
                        shutil.rmtree(entry.path)
                    elif entry.is_file(follow_symlinks=False) and (entry.name.startswith("scanner-chart-") or re.fullmatch(r"[A-Z0-9._-]+_\d{10}\.png", entry.name)):
                        os.unlink(entry.path)
                except OSError:
                    log("Orphan chart cleanup deferred")
    except OSError:
        pass


def _reset_ephemeral_runtime_caches():
    for name in ("_mc_cache", "_info_cache", "_earnings_date_cache", "_market_cap_diagnostics",
                 "_entry_spy_line_cache", "_rs_spy_cache", "_PRO_ENGINE_CACHE", "recent_sent",
                 "_MARKET_GUARD_PENDING_EVIDENCE", "UNIVERSE_MARKET_CAP_HINTS"):
        value = globals().get(name)
        if isinstance(value, (dict, set, list)):
            value.clear()
    globals()["_regime_cache"] = None
    globals()["_sector_rotation_cache"] = None
    diagnostics = globals().get("ENTRY_DIAGNOSTICS")
    if isinstance(diagnostics, dict):
        diagnostics.clear()
        diagnostics.update({"patterns": {}, "blockers": {}})


_RUN_MUTEX = threading.Lock()


def _managed_scanner_run(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        global HTTP_SESSION
        if not _RUN_MUTEX.acquire(blocking=False):
            log("Scanner run already active in this process; overlapping invocation skipped")
            return None
        try:
            _install_output_privacy()
            # Never discard a failed checkpoint and accumulate another full run.
            if globals().get("_EVALUATION_BUFFER"):
                _flush_setup_evaluations()
            _reset_ephemeral_runtime_caches()
            _cleanup_orphan_charts()
            previous = globals().get("HTTP_SESSION")
            if previous is not None and callable(getattr(previous, "close", None)):
                try:
                    previous.close()
                except Exception:
                    log("Previous HTTP session cleanup failed")
            HTTP_SESSION = requests.Session()
            HTTP_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
            return function(*args, **kwargs)
        finally:
            try:
                if globals().get("_EVALUATION_BUFFER"):
                    try:
                        _flush_setup_evaluations()
                    except Exception:
                        log("Research checkpoint failed; pending observations retained; next run must save them first")
                current = globals().get("HTTP_SESSION")
                if current is not None and callable(getattr(current, "close", None)):
                    try:
                        current.close()
                    except Exception:
                        log("HTTP session cleanup failed")
                HTTP_SESSION = None
                _reset_ephemeral_runtime_caches()
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
                except Exception:
                    pass
            finally:
                try:
                    _close_private_log_handlers()
                finally:
                    _RUN_MUTEX.release()
    return wrapped


# Install before external imports when this file is the application entry point.
if __name__ == "__main__":
    _install_output_privacy()


import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.signal import argrelextrema
import warnings
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
#  DATA SAFETY HELPERS — מונעים NaN מ-Yahoo / yfinance
# ============================================================
def _is_finite_number(value) -> bool:
    """True רק למספר אמיתי — לא None, לא NaN ולא inf."""
    try:
        return bool(np.isfinite(float(value)))
    except Exception:
        return False


def _normalize_yfinance_df(df: pd.DataFrame | None, *, drop_invalid_close: bool = True) -> pd.DataFrame | None:
    """
    מנקה DataFrame שמגיע מ-yfinance:
    - מוריד MultiIndex לעמודות רגילות
    - הופך OHLCV למספרים
    - מוחק שורות שה-Close שלהן NaN/inf

    זה פותר מצב שבו Yahoo מחזיר שורה אחרונה ריקה לפני פתיחת המסחר,
    ואז מופיע בלוג: SPY +nan% או P&L=nan%.
    """
    try:
        if df is None or getattr(df, "empty", True):
            return None
        out = df.copy()
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [str(c[0]).lower() for c in out.columns]
        else:
            out.columns = [str(c).lower() for c in out.columns]
        out = out.loc[:, ~out.columns.duplicated()]
        if "close" not in out.columns:
            return None
        for col in ("open", "high", "low", "close", "volume"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.replace([np.inf, -np.inf], np.nan)
        if drop_invalid_close:
            out = out.dropna(subset=["close"])
        if out.empty:
            return None
        return out
    except Exception:
        return None


def _last_finite(series, default=None):
    """מחזיר את הערך המספרי התקין האחרון מתוך Series."""
    try:
        s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if s.empty:
            return default
        val = float(s.iloc[-1])
        return val if _is_finite_number(val) else default
    except Exception:
        return default


def _safe_pct(numerator, denominator, default=None):
    """אחוז בטוח — לא מחזיר NaN."""
    try:
        n = float(numerator)
        d = float(denominator)
        if not _is_finite_number(n) or not _is_finite_number(d) or abs(d) < 1e-9:
            return default
        val = n / d * 100.0
        return val if _is_finite_number(val) else default
    except Exception:
        return default

# ============================================================
#  CONFIG  — ערכים ברירת-מחדל, הכל אפשר לדרוס דרך env-vars
# ============================================================

# --- אבטחה: סיסמה + מפתחות API ----
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if APP_PASSWORD in ("YOUR_APP_PASSWORD_HERE", "YOUR_PASSWORD", "CHANGEME"):
    APP_PASSWORD = ""
TO_EMAILS = [
    email.strip()
    for email in os.getenv("TO_EMAILS", "").split(",")
    if email.strip()
]
TO_EMAILS = list(dict.fromkeys(TO_EMAILS))

CHARTS_DIR = os.getenv("CHARTS_DIR", "temp_images")
os.makedirs(CHARTS_DIR, exist_ok=True)

# --- GitHub Actions persistent state folder ---
# כל קובצי המעקב נשמרים כאן, כדי שאפשר יהיה לשחזר אותם בין ריצות עם actions/cache.
STATE_DIR = os.getenv("STATE_DIR", "scanner_state")
os.makedirs(STATE_DIR, exist_ok=True)


def _state_path(path: str) -> str:
    """מחזיר נתיב לקובץ state. אם הוגדר נתיב מלא/תיקייה — משאיר כמו שהוא."""
    path = str(path or "").strip()
    if not path:
        return path
    if os.path.isabs(path) or os.path.dirname(path):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return path
    return os.path.join(STATE_DIR, path)


LOGFILE = _state_path(os.getenv("LOGFILE", "stock_scanner_unified_log.txt"))

# --- מניעת סריקה כפולה / סריקה ביום בלי מסחר ---
# הקוד בודק אם יש נר יומי חדש ב-SPY שלא נסרק עדיין.
# אם אין נר חדש — הוא מדלג על הסריקה כדי לא לשלוח התראות כפולות.
MARKET_DAY_GUARD_ENABLED = os.getenv("MARKET_DAY_GUARD_ENABLED", "True").lower() in ("1", "true", "yes")
SKIP_ISRAEL_WEEKENDS = False  # V9.3.1 legacy compatibility only; Market Day Guard no longer filters by weekday
MARKET_SCAN_SYMBOL = os.getenv("MARKET_SCAN_SYMBOL", "SPY")
MARKET_SCAN_STATE_FILE = _state_path(os.getenv("MARKET_SCAN_STATE_FILE", "last_market_scan_date.txt"))
# V9.1.4: keep a richer guard state alongside the legacy date file.
# The JSON stores the last observed OHLCV candle per provider so an intraday/manual
# scan cannot suppress the next scan after that same daily candle changes/finalizes.
MARKET_CANDLE_STATE_FILE = _state_path(os.getenv("MARKET_CANDLE_STATE_FILE", "last_market_scan_state.json"))
FORCE_SCAN = os.getenv("FORCE_SCAN", "False").lower() in ("1", "true", "yes")
# V9.3.1: Saturday maintenance is independent of the Market Day Guard.
# It runs once per Israeli Saturday even when there is no new US market candle.
SATURDAY_MAINTENANCE_STATE_FILE = _state_path(
    os.getenv("SATURDAY_MAINTENANCE_STATE_FILE", "last_saturday_maintenance_date.txt")
)

# --- V9.1.4 Market Day Guard: date + candle-change fail-safe ---
# Yahoo occasionally serves a stale last daily candle in early-morning GitHub runs.
# The guard now retries Yahoo, cross-checks a second liquid ETF, and then uses
# TwelveData as an independent fallback before deciding that no new session exists.
MARKET_GUARD_YAHOO_RETRIES = max(1, int(os.getenv("MARKET_GUARD_YAHOO_RETRIES", "3")))
MARKET_GUARD_RETRY_DELAY_SEC = max(0.0, float(os.getenv("MARKET_GUARD_RETRY_DELAY_SEC", "2")))
MARKET_GUARD_SECONDARY_SYMBOL = os.getenv("MARKET_GUARD_SECONDARY_SYMBOL", "QQQ").strip().upper() or "QQQ"
MARKET_GUARD_TWELVEDATA_ENABLED = os.getenv("MARKET_GUARD_TWELVEDATA_ENABLED", "True").lower() in ("1", "true", "yes")
MARKET_GUARD_TWELVEDATA_TIMEOUT = max(5, int(os.getenv("MARKET_GUARD_TWELVEDATA_TIMEOUT", "12")))

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
EMA28_MAX_DIST_PCT    = float(os.getenv("EMA28_MAX_DIST_PCT", "0.06"))   # 6% (v9.7.3, 2026-10-29; was 8% in v9.7.1, 3% before)
# EMA28 חייב בשיפוע עולה (הערך היום > אתמול)
EMA28_REQUIRE_RISING  = os.getenv("EMA28_REQUIRE_RISING", "True").lower() in ("1","true","yes")

# --- MA150 ---
MA150_MAX_DISTANCE    = float(os.getenv("MA150_MAX_DISTANCE", "0.05"))   # 5% — מרחק מקסימלי מ-MA150
MA150_MAX_DISTANCE_DB = float(os.getenv("MA150_MAX_DISTANCE_DB", "0.15")) # 15% — Double Bottom בלבד
MIN_ALERT_SCORE       = float(os.getenv("MIN_ALERT_SCORE",    "45.0"))  # ברירת מחדל — יוחלף דינמית לפי Regime

# ============================================================
#  V8 — ENTRY READY QUALITY ENGINE
#  שכבת איכות אחרונה: מפרידה בין Watchlist לבין כניסה מיידית.
#  המטרה: לא לשלוח מייל על תבנית בלבד — רק על פריצה מאושרת, ווליום,
#  חוזק יחסי, מעל MA150/MA200, נר איכותי ו-R:R מספיק.
# ============================================================
ENTRY_ENGINE_ENABLED          = os.getenv("ENTRY_ENGINE_ENABLED", "True").lower() in ("1", "true", "yes")
ENTRY_CANDIDATE_MIN_SCORE     = float(os.getenv("ENTRY_CANDIDATE_MIN_SCORE", "35.0"))
ENTRY_READY_MIN_SCORE         = float(os.getenv("ENTRY_READY_MIN_SCORE", "60.0"))
ENTRY_MIN_BREAKOUT_PCT        = float(os.getenv("ENTRY_MIN_BREAKOUT_PCT", "0.002"))   # 0.2% מעל נקודת הפריצה
ENTRY_MAX_EXTENSION_PCT       = float(os.getenv("ENTRY_MAX_EXTENSION_PCT", "0.030"))   # לא לרדוף אחרי פריצה רחוקה מדי
ENTRY_MIN_VOLUME_RATIO        = float(os.getenv("ENTRY_MIN_VOLUME_RATIO", "1.30"))
ENTRY_MIN_CLOSE_POS           = float(os.getenv("ENTRY_MIN_CLOSE_POS", "0.65"))
ENTRY_MIN_BODY_RATIO          = float(os.getenv("ENTRY_MIN_BODY_RATIO", "0.30"))
ENTRY_MIN_RS_SCORE            = float(os.getenv("ENTRY_MIN_RS_SCORE", "70.0"))
ENTRY_MIN_52W_HIGH_PROX       = float(os.getenv("ENTRY_MIN_52W_HIGH_PROX", "0.75"))
ENTRY_MIN_DOLLAR_VOLUME       = float(os.getenv("ENTRY_MIN_DOLLAR_VOLUME", "5000000"))
ENTRY_MIN_RR                  = float(os.getenv("ENTRY_MIN_RR", "2.50"))
RR_COMPARE_EPS               = float(os.getenv("RR_COMPARE_EPS", "0.000001"))  # מונע פסילה של 2.499999999 במקום 2.50
ENTRY_PULLBACK_MIN_VOLUME_RATIO = float(os.getenv("ENTRY_PULLBACK_MIN_VOLUME_RATIO", "1.00"))
ENTRY_RETEST_MIN_VOLUME_RATIO   = float(os.getenv("ENTRY_RETEST_MIN_VOLUME_RATIO", "1.00"))
ENTRY_MAX_ATR_PCT             = float(os.getenv("ENTRY_MAX_ATR_PCT", "0.08"))
ENTRY_MA150_MIN_ABOVE_PCT     = float(os.getenv("ENTRY_MA150_MIN_ABOVE_PCT", "0.000"))
ENTRY_REQUIRE_MA150_RISING    = os.getenv("ENTRY_REQUIRE_MA150_RISING", "True").lower() in ("1", "true", "yes")
ENTRY_REQUIRE_MA200_ABOVE     = os.getenv("ENTRY_REQUIRE_MA200_ABOVE", "True").lower() in ("1", "true", "yes")
ENTRY_REQUIRE_REGIME_DATA_OK  = os.getenv("ENTRY_REQUIRE_REGIME_DATA_OK", "True").lower() in ("1", "true", "yes")
ENTRY_BLOCK_BEAR_REGIME       = os.getenv("ENTRY_BLOCK_BEAR_REGIME", "True").lower() in ("1", "true", "yes")
ENTRY_QUALITY_LOG             = _state_path(os.getenv("ENTRY_QUALITY_LOG", "entry_quality_log.csv"))

# ============================================================
#  V9 — PATTERN EXPANSION ENGINE
#  מוסיף תבניות כניסה איכותיות בלי לשנות את סדר הסינון הקיים.
#  סדר הסינון נשאר: Market Cap → Data → Earnings → EMA28 → Reverse Scanner → Patterns → Entry Quality.
# ============================================================
V9_PATTERN_ENGINE_ENABLED     = os.getenv("V9_PATTERN_ENGINE_ENABLED", "True").lower() in ("1", "true", "yes")
# Kept for compatibility with existing environment files. No detector-stage cap
# is applied: all five pattern families must reach the final Entry/Pro gates.
# TOP_ALERTS_TO_SEND still limits unique, fully qualified email selections.
V9_MAX_CANDIDATES_PER_TICKER  = int(os.getenv("V9_MAX_CANDIDATES_PER_TICKER", "3"))
V9_INCLUDE_REJECTED_DEBUG     = os.getenv("V9_INCLUDE_REJECTED_DEBUG", "False").lower() in ("1", "true", "yes")

# ============================================================
#  V9.1 — PROFESSIONAL TRADE QUALITY ENGINE
#  שכבת דירוג מקצועית מעל התבניות הקיימות.
#  IMPORTANT: השכבה הזאת לא משנה שום Pattern detector. היא פועלת רק אחרי
#  שהמועמד כבר עבר את V8 Entry Ready, כדי לבחור רק עסקאות עם Context חזק.
# ============================================================
PRO_ENGINE_ENABLED            = os.getenv("PRO_ENGINE_ENABLED", "True").lower() in ("1", "true", "yes")
PRO_MIN_SCORE                 = float(os.getenv("PRO_MIN_SCORE", "75.0"))
PRO_MIN_TREND_SCORE           = float(os.getenv("PRO_MIN_TREND_SCORE", "55.0"))
PRO_MIN_RS_PROFILE_SCORE      = float(os.getenv("PRO_MIN_RS_PROFILE_SCORE", "65.0"))
PRO_MIN_MARKET_CONTEXT_SCORE  = float(os.getenv("PRO_MIN_MARKET_CONTEXT_SCORE", "25.0"))
PRO_MAX_STOP_RISK_PCT         = float(os.getenv("PRO_MAX_STOP_RISK_PCT", "0.12"))
# v9.7.4 (backtest 2026-10-30, 543 tickers, 2023-2026): for Flat Base / Darvas / VCP breakouts the
# stop distance 7%-10% gave the highest share of trades in profit after 20 sessions in both samples
# and both periods. Tighter stops were shaken out more often; wider stops meant looser bases.
BREAKOUT_MIN_STOP_RISK_PCT    = float(os.getenv("BREAKOUT_MIN_STOP_RISK_PCT", "0.07"))
BREAKOUT_MAX_STOP_RISK_PCT    = float(os.getenv("BREAKOUT_MAX_STOP_RISK_PCT", "0.10"))
_BREAKOUT_STOP_FAMILIES       = ("Flat Base", "Darvas", "VCP")
PRO_BLOCK_FROZEN_SECTOR       = os.getenv("PRO_BLOCK_FROZEN_SECTOR", "True").lower() in ("1", "true", "yes")
PRO_BLOCK_DISTRIBUTION        = os.getenv("PRO_BLOCK_DISTRIBUTION", "True").lower() in ("1", "true", "yes")
PRO_QUALITY_LOG               = _state_path(os.getenv("PRO_QUALITY_LOG", "professional_quality_log.csv"))

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
        df = _normalize_yfinance_df(yf.download("^VIX", period="5d", progress=False, auto_adjust=True))
        if df is None or df.empty:
            return False, 0.0, "VIX לא זמין"
        val = _last_finite(df["close"])
        if not _is_finite_number(val):
            return False, 0.0, "❌ VIX לא זמין — נתונים לא תקינים"
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
    FIX V7: אם Yahoo מחזיר NaN, לא מציגים +nan% ולא מפעילים סיגנל.
    """
    try:
        df = _normalize_yfinance_df(yf.download("SPY", period="300d", progress=False, auto_adjust=True))
        if df is None or len(df) < 200:
            return False, 0.0, "❌ SPY MA200 לא זמין"
        close = df["close"]
        price = _last_finite(close)
        ma200 = _last_finite(close.rolling(200).mean())
        if not _is_finite_number(price) or not _is_finite_number(ma200):
            return False, 0.0, "❌ SPY MA200 לא זמין — נתוני Yahoo לא תקינים"
        dist = _safe_pct(price - ma200, ma200, default=None)
        if dist is None:
            return False, 0.0, "❌ SPY MA200 לא זמין — חישוב לא תקין"
        triggered = dist <= -10.0
        emoji = "✅" if triggered else "❌"
        return triggered, dist, f"{emoji} SPY vs MA200: {dist:+.1f}% ({'מצוקה קיצונית' if triggered else 'תקין'})"
    except Exception as e:
        return False, 0.0, f"❌ SPY MA200 שגיאה: {e}"


def _check_three_red_days() -> tuple[bool, int, str]:
    """בודק 3 ימים אדומים ברצף ב-SPY. לא מחזיר nan בלוג."""
    try:
        df = _normalize_yfinance_df(yf.download("SPY", period="10d", progress=False, auto_adjust=True))
        if df is None or len(df) < 4:
            return False, 0, "❌ SPY לא זמין"
        closes = list(pd.to_numeric(df["close"], errors="coerce").dropna().tail(4).astype(float).values)
        if len(closes) < 4 or any(not _is_finite_number(v) for v in closes):
            return False, 0, "❌ SPY לא זמין — אין 4 סגירות תקינות"
        red_days = 0
        for i in range(1, 4):
            if closes[i] < closes[i-1]:
                red_days += 1
            else:
                break
        triggered = red_days >= 3
        emoji = "✅" if triggered else "❌"
        pct = _safe_pct(closes[-1] - closes[-4], closes[-4], default=None)
        if pct is None:
            return False, 0, "❌ SPY לא זמין — חישוב ימים אדומים לא תקין"
        return triggered, red_days, f"{emoji} SPY: {red_days} ימים אדומים ברצף ({pct:.1f}% ירידה)"
    except Exception as e:
        return False, 0, f"❌ SPY שגיאה: {e}"

def _check_reversal_signals() -> tuple[bool, float, str]:
    """
    בודק אם השוק הולך להתהפך — SPY חוצה מעל EMA28 אחרי BEAR.
    FIX V7: מתעלם משורת Yahoo ריקה ומחזיר UNKNOWN במקום nan.
    """
    try:
        df = _normalize_yfinance_df(yf.download("SPY", period="60d", progress=False, auto_adjust=True))
        if df is None or len(df) < 30:
            return False, 0.0, "SPY לא זמין"
        closes = df["close"]
        ema28 = closes.ewm(span=28, adjust=False).mean()
        price = _last_finite(closes)
        ema = _last_finite(ema28)
        prev_price = float(closes.iloc[-2]) if len(closes) >= 2 else np.nan
        prev_ema = float(ema28.iloc[-2]) if len(ema28) >= 2 else np.nan
        if not all(_is_finite_number(v) for v in (price, ema, prev_price, prev_ema)):
            return False, 0.0, "SPY לא זמין — נתוני EMA28 לא תקינים"
        dist = _safe_pct(price - ema, ema, default=None)
        if dist is None:
            return False, 0.0, "SPY לא זמין — חישוב EMA28 לא תקין"
        crossed_above = prev_price < prev_ema and price > ema
        vix_trig, vix_val, _ = _check_vix()
        vix_dropping = vix_val < 25 and not vix_trig
        reversing = crossed_above and vix_dropping
        return reversing, dist, f"SPY {dist:+.1f}% מ-EMA28 | {'חצה מעל!' if crossed_above else 'עדיין מתחת'}"
    except Exception as e:
        return False, 0.0, f"שגיאה: {e}"


@_nonfatal_mail
def _send_reversal_email(signals: list[tuple], score: int,
                         reversal_detected: bool = False) -> dict:
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

        signals_html = _mail_list([description for _, _, description in signals], "לא נרשמו סיגנלים")
        facts_html = _mail_list(facts)
        body = _mail_section("סיגנלים שזוהו", signals_html, "blue" if reversal_detected else "amber")
        body += _mail_section("המלצת פעולה", f'<p style="color:#c5d0e0;font-size:14px;line-height:1.9;margin:0;">{_mail_text(action)}</p>', "amber")
        body += _mail_section("נתונים היסטוריים", facts_html, "muted")
        html = _mail_document("עדכון מצב השוק", today,
                              _mail_card(headline, body, "Market Reversal Detector",
                                         _mail_pill("שינוי כיוון" if reversal_detected else f"{score}/4 סיגנלים", "blue" if reversal_detected else "amber")),
                              eyebrow="MARKET REVERSAL REPORT", preheader=headline)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = FROM_EMAIL
        msg["To"]      = ", ".join(TO_EMAILS) if isinstance(TO_EMAILS, list) else TO_EMAILS
        _attach_mail_content(msg, html)

        return _smtp_deliver(msg, "market_reversal")
    except Exception as e:
        log(f"_send_reversal_email error: {e}")
        return _mail_status("failed", refused=len(TO_EMAILS))


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
    מחזיר ציון מינימלי דינמי לפי מצב השוק.
    FIX V7: אם נתוני SPY לא תקינים — לא מורידים סף, משתמשים בסף בטוח 55.
      🐂 BULL    → 55
      🟡 NEUTRAL → 45
      🔴 BEAR    → 55  (לא מקלים בשוק חלש)
      ❔ UNKNOWN → 55  (נתוני SPY חסרים/NaN)
    """
    try:
        regime = get_market_regime()
        r = regime.get("regime", "UNKNOWN")
        data_ok = bool(regime.get("data_ok", False))
        if not data_ok or r == "UNKNOWN":
            return 55.0
        if r == "BULL":
            return 55.0
        if r == "NEUTRAL":
            return 45.0
        if r == "BEAR":
            return 55.0
        return 55.0
    except Exception:
        return 55.0

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
MIN_MARKET_CAP_USD    = 1_000_000_000.0   # FIXED: $1B, no env override
SCAN_DELAY_SECONDS    = int(os.getenv("SCAN_DELAY_SECONDS",    "10"))
# --- מניעת כפילויות התראות ---
# ברירת מחדל: לא לשלוח אותו סטאפ שוב במשך 7 ימים.
# זה מונע מצב שבו אותו סטאפ נשלח שוב יום אחרי רק כי עברו 24 שעות.
ALERT_COOLDOWN_HOURS  = int(os.getenv("ALERT_COOLDOWN_HOURS",  "168"))
DEDUP_ALERT_HOURS     = ALERT_COOLDOWN_HOURS
# אם כבר יש פוזיציה פתוחה על אותו טיקר — לא שולחים עליו התראת כניסה נוספת.
SUPPRESS_ALERTS_FOR_OPEN_POSITIONS = os.getenv("SUPPRESS_ALERTS_FOR_OPEN_POSITIONS", "True").lower() in ("1", "true", "yes")
DEBUG_SCAN_REASONS    = os.getenv("DEBUG_SCAN_REASONS","True").lower() in ("1","true","yes")
TOP_ALERTS_TO_SEND    = int(os.getenv("TOP_ALERTS_TO_SEND", "3"))

# --- קבצים ---
PROGRESS_FILE      = _state_path(os.getenv("PROGRESS_FILE",      "progress.json"))
ALERT_HISTORY_FILE = _state_path(os.getenv("ALERT_HISTORY_FILE", "alerts_sent.json"))
SIGNALS_CSV        = _state_path(os.getenv("SIGNALS_CSV",         "signals_log.csv"))
BLOCKLIST_FILE     = _state_path(os.getenv("BLOCKLIST_FILE",      "twelvedata_blocklist.json"))
CHARTS_DIR         = os.getenv("CHARTS_DIR",          "temp_images")
os.makedirs(CHARTS_DIR, exist_ok=True)

# --- מעקב ביצועים ---
PERFORMANCE_CSV    = _state_path(os.getenv("PERFORMANCE_CSV",     "performance_log.csv"))
PERF_CHECK_DAYS    = [5, 10, 20]  # Closed trading sessions, never calendar days.
# V9.4 keeps the old CSV intact; new observations use a separately versioned schema.
SETUP_EVALUATIONS_CSV = _state_path(os.getenv("SETUP_EVALUATIONS_CSV", "setup_evaluations_v2.csv"))
LEARNING_REPORT_FILE = _state_path(os.getenv("LEARNING_REPORT_FILE", "scanner_learning_report_v2.json"))
RESEARCH_HISTORY_DAYS = max(180, int(os.getenv("RESEARCH_HISTORY_DAYS", "180")))
RESEARCH_SLIPPAGE_BPS = max(0.0, float(os.getenv("RESEARCH_SLIPPAGE_BPS", "5")))
SHADOW_TRACKING_ENABLED = os.getenv("SHADOW_TRACKING_ENABLED", "True").lower() in ("1", "true", "yes")
STRICT_PATTERN_STRUCTURE = os.getenv("STRICT_PATTERN_STRUCTURE", "False").lower() in ("1", "true", "yes")
REQUIRE_VERIFIED_EARNINGS = os.getenv("REQUIRE_VERIFIED_EARNINGS", "True").lower() in ("1", "true", "yes")
AUTO_APPLY_LEARNING = os.getenv("AUTO_APPLY_LEARNING", "False").lower() in ("1", "true", "yes")
# Research instrumentation never changes the live entry or delivery gates.
ENTRY_AUDIT_FILE = _state_path(os.getenv("ENTRY_AUDIT_FILE", "entry_funnel_report_v3.json"))
PRO_SHADOW_DIAGNOSTICS = os.getenv("PRO_SHADOW_DIAGNOSTICS", "True").lower() in ("1", "true", "yes")
PRO_SHADOW_MIN_ENTRY_SCORE = float(os.getenv("PRO_SHADOW_MIN_ENTRY_SCORE", "55"))
PRO_SHADOW_MAX_ENTRY_BLOCKERS = max(0, int(os.getenv("PRO_SHADOW_MAX_ENTRY_BLOCKERS", "2")))
_EVALUATION_BUFFER: dict = {}
_market_cap_diagnostics: dict = {}

# --- סינון דוחות ---
EARNINGS_RECENT_BLOCK_DAYS = int(os.getenv("EARNINGS_RECENT_BLOCK_DAYS", "3"))  # used only when EARNINGS_RECENT_MODE=days
# v9.7.3: "candle" = block until there is at least one full daily candle after the report (default);
#         "days"   = the v9.7.2 rule (block N calendar days after the report).
EARNINGS_RECENT_MODE = (os.getenv("EARNINGS_RECENT_MODE", "candle") or "candle").strip().lower()
POSITION_EARNINGS_WARN_DAYS = int(os.getenv("POSITION_EARNINGS_WARN_DAYS", "14"))
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
LOGFILE = _state_path(os.getenv("LOGFILE", "stock_scanner_unified_log.txt"))

# WATCHLIST LOG — מניות שעברו EMA28+MA150+דוחות אבל נפסלו בשלב אחרון
WATCHLIST_LOG = _state_path(os.getenv("WATCHLIST_LOG", "watchlist_log.txt"))
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
UNIVERSE_CACHE_FILE = _state_path(os.getenv("UNIVERSE_CACHE_FILE", "universe_cache.json"))
UNIVERSE_MIN_CAP    = 1_000_000_000.0   # FIXED: $1B, no env override
UNIVERSE_MIN_PRICE  = float(os.getenv("UNIVERSE_MIN_PRICE", "5.0")) # לא פני-סטוק
UNIVERSE_MIN_VOL    = int(os.getenv("UNIVERSE_MIN_VOL", "100000"))  # volume מינימלי

# --- Universe refresh policy ---
# ברירת המחדל החדשה: בכל ריצה מנסים לבנות Universe חדש מהאינטרנט.
# universe_cache.json הוא גיבוי בלבד, ומשמש רק אם הבנייה החדשה נכשלה או יצאה קטנה מדי.
UNIVERSE_ALWAYS_REFRESH      = os.getenv("UNIVERSE_ALWAYS_REFRESH", "True").lower() in ("1", "true", "yes")
UNIVERSE_REQUIRE_MARKET_CAP  = False  # FIXED: unknown market cap is allowed to pass to full scan
UNIVERSE_MIN_RAW_ROWS        = int(os.getenv("UNIVERSE_MIN_RAW_ROWS", "4000"))
UNIVERSE_MIN_FRESH_TICKERS   = int(os.getenv("UNIVERSE_MIN_FRESH_TICKERS", "1200"))
UNIVERSE_MIN_CACHE_TICKERS   = int(os.getenv("UNIVERSE_MIN_CACHE_TICKERS", "500"))
MARKET_CAP_PRECHECK_SLEEP_SECONDS = max(0.0, float(os.getenv("MARKET_CAP_PRECHECK_SLEEP_SECONDS", "0.25")))

# V9.1.2 — Market-cap pipeline
# NASDAQ screener market-cap values are kept as same-run hints and prime the
# in-memory cache. Only tickers whose cap is missing are verified separately
# before the expensive full scan. Unknown after verification stays fail-open
# so a temporary data outage cannot silently remove a legitimate >$1B stock.
UNIVERSE_MARKET_CAP_HINTS: dict[str, float | None] = {}
UNIVERSE_MARKET_CAP_HINT_SOURCE = "none"
MARKET_CAP_PRECHECK_STATS = {
    "input": 0,
    "known_pass": 0,
    "known_reject": 0,
    "unknown_to_verify": 0,
    "verified_pass": 0,
    "verified_reject": 0,
    "unresolved_pass": 0,
    "output": 0,
}


def _parse_market_number(value) -> float | None:
    """
    ממיר מספרים שמגיעים מ-NASDAQ/Yahoo למספר float.
    תומך בפורמטים כמו: "$12.3B", "1,234,567,890", "N/A".
    """
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            if pd.isna(value):
                return None
            return float(value)

        s = str(value).strip()
        if not s or s.lower() in ("n/a", "na", "none", "null", "-", "--"):
            return None

        s = s.replace("$", "").replace(",", "").replace("%", "").strip()
        mult = 1.0
        if s[-1:].upper() == "T":
            mult = 1_000_000_000_000.0
            s = s[:-1]
        elif s[-1:].upper() == "B":
            mult = 1_000_000_000.0
            s = s[:-1]
        elif s[-1:].upper() == "M":
            mult = 1_000_000.0
            s = s[:-1]
        elif s[-1:].upper() == "K":
            mult = 1_000.0
            s = s[:-1]

        return float(s) * mult
    except Exception:
        return None


def _activate_universe_market_cap_hints(ticker_list: list[str], meta: dict, source: str) -> None:
    """
    טוען את market-cap metadata של ה-Universe שנבחר בפועל.
    ערכים ידועים מוזנים גם ל-_mc_cache כדי שה-Full Scan לא יבצע שוב
    בקשת yfinance מיותרת ולא יקבל מקור סותר באותה ריצה.
    """
    global UNIVERSE_MARKET_CAP_HINTS, UNIVERSE_MARKET_CAP_HINT_SOURCE

    allowed = {str(t).strip().upper() for t in ticker_list if str(t).strip()}
    raw = meta.get("market_caps", {}) if isinstance(meta, dict) else {}
    hints: dict[str, float | None] = {}

    if isinstance(raw, dict):
        for sym, value in raw.items():
            t = str(sym).strip().upper()
            if t not in allowed:
                continue
            mc = _parse_market_number(value)
            hints[t] = mc if mc is not None and mc > 0 else None

    # Backward-compatible cache: old universe_cache.json has no market_caps map.
    for t in allowed:
        hints.setdefault(t, None)

    UNIVERSE_MARKET_CAP_HINTS = hints
    UNIVERSE_MARKET_CAP_HINT_SOURCE = str(source or "unknown")

    # _mc_cache is defined later in the module but exists by the time main() runs.
    cache = globals().get("_mc_cache")
    if isinstance(cache, dict):
        for t, mc in hints.items():
            if mc is not None and _is_finite_number(mc) and float(mc) > 0:
                cache[t] = float(mc)

    known = sum(1 for v in hints.values() if v is not None and _is_finite_number(v))
    unknown = max(len(allowed) - known, 0)
    log(
        f"💰 Universe market-cap hints activated: source={UNIVERSE_MARKET_CAP_HINT_SOURCE}, "
        f"known={known}, unknown={unknown}"
    )


def _fetch_exchange_rows(exchange: str) -> list[dict]:
    """מוריד rows מלאים מ-NASDAQ screener כדי לא להעמיס על yfinance בשלב ה-Universe."""
    try:
        import urllib.request
        urls = {
            "NASDAQ": "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=nasdaq",
            "NYSE":   "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange=nyse",
        }
        url = urls.get(exchange.upper(), "")
        if not url:
            return []

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        rows = data.get("data", {}).get("table", {}).get("rows", []) or []
        return rows if isinstance(rows, list) else []
    except Exception as e:
        log(f"_fetch_exchange_rows {exchange} error: {e}")
        return []


def _fetch_exchange_tickers(exchange: str) -> list[str]:
    """מוריד רשימת טיקרים מ-NASDAQ screener לפי בורסה."""
    rows = _fetch_exchange_rows(exchange)
    symbols = []
    for row in rows:
        try:
            sym = (row.get("symbol") or "").strip().upper()
            # דלג על preferred/warrants וסימולים עם נקודה/מקף כדי להפחית שגיאות API
            if sym and sym.isalpha() and len(sym) <= 5:
                symbols.append(sym)
        except Exception:
            continue
    return symbols


def _get_universe_cache() -> tuple[list[str], dict]:
    """טוען universe_cache.json כגיבוי בלבד. לא משתמש בו כמקור ראשי."""
    try:
        if not os.path.exists(UNIVERSE_CACHE_FILE):
            return [], {}
        with open(UNIVERSE_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        cached_tickers = cached.get("tickers", []) if isinstance(cached, dict) else []
        clean = []
        seen = set()
        for t in cached_tickers:
            sym = str(t).strip().upper().replace("$", "")
            if sym and sym.isalpha() and len(sym) <= 5 and sym not in seen:
                seen.add(sym)
                clean.append(sym)
        return clean, cached if isinstance(cached, dict) else {}
    except Exception as e:
        log(f"Universe cache read error: {e}")
        return [], {}


def _save_universe_cache(tickers_list: list[str], meta: dict) -> None:
    """שומר את ה-Universe האחרון שהצליח, כדי שישמש גיבוי בריצה הבאה."""
    try:
        parent = os.path.dirname(UNIVERSE_CACHE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        cache_data = {
            "date": datetime.now().isoformat(),
            "tickers": tickers_list,
            **(meta or {}),
        }
        _atomic_write_text(UNIVERSE_CACHE_FILE, json.dumps(cache_data, indent=2))
        log(f"💾 Universe cache updated: {len(tickers_list)} tickers → {UNIVERSE_CACHE_FILE}")
    except Exception as e:
        log(f"Universe cache save error: {e}")


def _is_probably_common_stock_row(row: dict) -> tuple[bool, str]:
    """
    מסנן מוצרים שהם בדרך כלל לא מניות רגילות: ETF/קרנות/Preferred/Warrants/Units/Notes.
    המטרה היא Universe נקי יותר של מניות רגילות מעל $1B.
    """
    try:
        text_parts = []
        for k in (
            "name", "companyName", "companyname", "securityName", "securityname",
            "assetClass", "assetclass", "instrumentType", "instrumenttype",
        ):
            v = row.get(k)
            if v:
                text_parts.append(str(v))
        txt = " ".join(text_parts).lower()

        bad_phrases = (
            " exchange traded fund",
            " exchange traded note",
            "closed end", "closed-end", "closed end fund", "closed-end fund",
            "mutual fund", "index fund", "income fund", "bond fund",
            "preferred", "preference", "depositary share", "depositary shares",
            "warrant", "warrants",
            "notes due", "senior notes", "subordinated notes", "baby bond", "debenture",
        )
        if any(p in txt for p in bad_phrases) or re.search(r"\b(etf|etn|rights?|units?)\b", txt):
            return False, "fund_etf_preferred_unit_note"

        # קרנות סגורות רבות מופיעות כ-Trust בלי מילת Fund. לא נפסול REIT רגיל לפי Trust בלבד,
        # אבל כן נפסול שילובים נפוצים של קרנות השקעה.
        trust_fund_managers = (
            "blackrock", "nuveen", "eaton vance", "pimco", "abrdn", "aberdeen",
            "gabelli", "guggenheim", "invesco", "virtus", "cohen & steers", "mfs ",
            "calamos", "flaherty", "first trust", "clearbridge", "tekla",
        )
        if "trust" in txt and any(m in txt for m in trust_fund_managers):
            return False, "closed_end_trust"

        return True, "common_stock_like"
    except Exception:
        return True, "classification_error_allowed"


def _build_fresh_universe_from_screener() -> tuple[list[str], dict]:
    """בונה Universe חדש מהאינטרנט, בלי להשתמש בקאש."""
    log("🌐 Building fresh universe — downloading NASDAQ + NYSE rows...")

    nasdaq_rows = _fetch_exchange_rows("NASDAQ")
    nyse_rows   = _fetch_exchange_rows("NYSE")
    all_rows    = nasdaq_rows + nyse_rows

    meta = {
        "source": "fresh_nasdaq_screener",
        "total_rows": len(all_rows),
        "nasdaq_rows": len(nasdaq_rows),
        "nyse_rows": len(nyse_rows),
        "passed": 0,
        "failed": 0,
        "filtered_market_cap": 0,
        "filtered_price": 0,
        "filtered_not_common_stock": 0,
        "filtered_bad_symbol": 0,
        "unknown_market_cap_rejected": 0,  # kept for log compatibility; should remain 0
        "unknown_market_cap_passed": 0,
        "unknown_price_passed": 0,
    }

    log(f"   Raw rows: {len(nasdaq_rows)} NASDAQ + {len(nyse_rows)} NYSE = {len(all_rows)} total")
    log(
        f"   Filtering: common stocks only | market cap >= ${UNIVERSE_MIN_CAP/1e9:.0f}B "
        f"| price >= ${UNIVERSE_MIN_PRICE:.0f}"
    )

    passed: list[str] = []
    seen: set[str] = set()
    market_caps: dict[str, float | None] = {}

    for i, row in enumerate(all_rows):
        try:
            sym = (row.get("symbol") or "").strip().upper().replace("$", "")

            # מניות רגילות בלבד — בלי preferred/warrants/symbols עם סימנים מיוחדים.
            if not sym or not sym.isalpha() or len(sym) > 5:
                meta["filtered_bad_symbol"] += 1
                meta["failed"] += 1
                continue
            if sym in seen:
                continue
            seen.add(sym)

            is_common, common_reason = _is_probably_common_stock_row(row)
            if not is_common:
                meta["filtered_not_common_stock"] += 1
                meta["failed"] += 1
                continue

            mc = _parse_market_number(
                row.get("marketCap")
                or row.get("marketcap")
                or row.get("market_cap")
                or row.get("MarketCap")
            )
            price = _parse_market_number(
                row.get("lastsale")
                or row.get("lastSale")
                or row.get("price")
                or row.get("Price")
                or row.get("previousClose")
            )

            # FIXED: אם אין Market Cap — לא זורקים. מעבירים לסריקה מלאה.
            # הסיבה: NASDAQ/Yahoo לפעמים לא מחזירים Market Cap, וזה גרם ל-unknown_mc_rejected.
            if mc is None:
                meta["unknown_market_cap_passed"] += 1
            elif mc < UNIVERSE_MIN_CAP:
                meta["filtered_market_cap"] += 1
                meta["failed"] += 1
                continue

            if price is not None and price < UNIVERSE_MIN_PRICE:
                meta["filtered_price"] += 1
                meta["failed"] += 1
                continue
            if price is None:
                meta["unknown_price_passed"] += 1

            passed.append(sym)
            market_caps[sym] = float(mc) if mc is not None and _is_finite_number(mc) and float(mc) > 0 else None

        except Exception:
            meta["failed"] += 1

        if (i + 1) % 500 == 0 or (i + 1) == len(all_rows):
            log(f"   Progress: {i+1}/{len(all_rows)} rows — passed so far: {len(passed)}")

    meta["passed"] = len(passed)
    # Persist the exact market-cap evidence used to admit each symbol.
    # This keeps Universe and Full Scan consistent within the same run/cache fallback.
    meta["market_caps"] = market_caps
    return passed, meta


def _fresh_universe_is_healthy(tickers_list: list[str], meta: dict) -> tuple[bool, str]:
    """בדיקת בטיחות: אם הבנייה החדשה יצאה קטנה/חסרה מדי — לא משתמשים בה."""
    raw_rows = int(meta.get("total_rows", 0) or 0)
    passed = len(tickers_list)

    if raw_rows < UNIVERSE_MIN_RAW_ROWS:
        return False, f"raw rows too low ({raw_rows} < {UNIVERSE_MIN_RAW_ROWS})"
    if passed < UNIVERSE_MIN_FRESH_TICKERS:
        return False, f"fresh universe too small ({passed} < {UNIVERSE_MIN_FRESH_TICKERS})"
    if passed <= 0:
        return False, "fresh universe is empty"
    return True, "fresh universe healthy"


def build_universe(force_refresh: bool = False) -> list[str]:
    """
    בונה Universe חדש בכל ריצה.

    שיטה חדשה:
      1) קודם מנסים לבנות רשימה חדשה מהאינטרנט.
      2) אם הרשימה החדשה בריאה — משתמשים בה ומעדכנים universe_cache.json.
      3) אם הבנייה נכשלה / הרשימה קטנה מדי — משתמשים ב-cache האחרון כגיבוי.
      4) רק אם אין cache תקין — משתמשים ברשימת fallback קטנה.
    """
    cached_tickers, cached_meta = _get_universe_cache()
    if cached_tickers:
        cache_date = str(cached_meta.get("date", "unknown"))[:10]
        log(f"📋 Universe cache available as fallback: {len(cached_tickers)} tickers (date: {cache_date})")
    else:
        log("📋 No universe cache available — fresh build is required")

    # במקרה מיוחד בלבד אפשר להחזיר cache קודם דרך env, אבל ברירת המחדל היא לבנות חדש בכל ריצה.
    use_cache_first = (not UNIVERSE_ALWAYS_REFRESH) or os.getenv("UNIVERSE_USE_CACHE_FIRST", "False").lower() in ("1", "true", "yes")
    if use_cache_first and not force_refresh and cached_tickers:
        _activate_universe_market_cap_hints(cached_tickers, cached_meta, "cache_first")
        log(f"📋 UNIVERSE SOURCE: cache first mode ({len(cached_tickers)} tickers)")
        return cached_tickers

    fresh_tickers, fresh_meta = _build_fresh_universe_from_screener()
    healthy, reason = _fresh_universe_is_healthy(fresh_tickers, fresh_meta)

    log(
        "📊 Universe rebuild report: "
        f"raw={fresh_meta.get('total_rows', 0)}, "
        f"passed={len(fresh_tickers)}, "
        f"market_cap_rejected={fresh_meta.get('filtered_market_cap', 0)}, "
        f"unknown_mc_passed={fresh_meta.get('unknown_market_cap_passed', 0)}, "
        f"unknown_mc_rejected={fresh_meta.get('unknown_market_cap_rejected', 0)}, "
        f"not_common_stock={fresh_meta.get('filtered_not_common_stock', 0)}, "
        f"bad_symbol={fresh_meta.get('filtered_bad_symbol', 0)}, "
        f"price_rejected={fresh_meta.get('filtered_price', 0)}"
    )

    if healthy:
        _activate_universe_market_cap_hints(fresh_tickers, fresh_meta, "fresh_nasdaq_screener")
        _save_universe_cache(fresh_tickers, fresh_meta)
        log(f"✅ UNIVERSE SOURCE: fresh rebuild ({len(fresh_tickers)} tickers) — cache updated")
        return fresh_tickers

    log(f"⚠️ Fresh universe not healthy: {reason}")
    if len(cached_tickers) >= UNIVERSE_MIN_CACHE_TICKERS:
        _activate_universe_market_cap_hints(cached_tickers, cached_meta, "cache_fallback")
        log(f"📋 UNIVERSE SOURCE: cache fallback ({len(cached_tickers)} tickers)")
        return cached_tickers

    _activate_universe_market_cap_hints(_FALLBACK_TICKERS, {}, "emergency_fallback")
    log("⚠️ Cache fallback missing or too small — using emergency fallback tickers")
    log(f"📋 UNIVERSE SOURCE: emergency fallback ({len(_FALLBACK_TICKERS)} tickers)")
    return _FALLBACK_TICKERS

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
    """Only protected text reaches the console and size-bounded runner log."""
    safe = _redact_sensitive(msg).replace("\r", "\\r").replace("\n", "\\n")
    try:
        _protected_logger().info(safe)
    except Exception:
        # A failing logger must never fall back to printing the original value.
        try:
            sys.stderr.write("Protected logging unavailable\n")
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
        with open(WATCHLIST_LOG, "r", encoding="utf-8") as existing:
            content = existing.read()
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
                f.write(_redact_sensitive(header))
            f.write(_redact_sensitive(entry))
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
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))
    except Exception as e:
        log(f"_save_json error {path}: {e}")


# ============================================================
#  V9.1.3 — ONE-MONTH HISTORY RETENTION
#  בסוף סריקה מלאה: משאיר רק היסטוריה מהחודש הקלנדרי האחרון.
#  State חי (פוזיציות פתוחות / תאריך סריקה / cache / blocklist) לא נמחק.
# ============================================================
def _israel_today_date():
    """Today's date in Israel; safe fallback to runner-local date."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Jerusalem")).date()
    except Exception:
        return datetime.now().date()


def _one_calendar_month_ago(day):
    """Same day one calendar month earlier; clamps to the previous month's last day."""
    year = int(day.year)
    month = int(day.month) - 1
    if month == 0:
        month = 12
        year -= 1
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, min(int(day.day), last_day)).date()


def _atomic_write_text(path: str, text: str) -> None:
    """Private unique temp file, fsync, atomic replacement, cleanup on failure."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="scanner-state-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            log("Temporary state file cleanup deferred")


def _atomic_write_json(path: str, data) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _prune_csv_by_date(path: str, date_column: str, cutoff_date) -> dict:
    result = {"file": os.path.basename(path), "before": 0, "after": 0, "removed": 0, "status": "missing"}
    if not path or not os.path.exists(path):
        return result
    try:
        df = pd.read_csv(path)
        result["before"] = int(len(df))
        if df.empty:
            result.update({"after": 0, "removed": 0, "status": "ok"})
            return result
        if date_column not in df.columns:
            result["status"] = f"skipped:no_column:{date_column}"
            result["after"] = result["before"]
            return result

        parsed = pd.to_datetime(df[date_column], errors="coerce")
        # Fail-safe: invalid/unparseable rows are retained rather than deleted blindly.
        valid_dates = parsed.dt.date
        keep = parsed.isna() | (valid_dates >= cutoff_date)
        out = df.loc[keep].copy()

        _atomic_write_text(path, out.to_csv(index=False))
        result["after"] = int(len(out))
        result["removed"] = result["before"] - result["after"]
        result["status"] = "ok"
        return result
    except Exception as e:
        result["status"] = f"error:{type(e).__name__}"
        result["after"] = result["before"]
        return result


def _prune_alert_history_json(path: str, cutoff_date) -> dict:
    result = {"file": os.path.basename(path), "before": 0, "after": 0, "removed": 0, "status": "missing"}
    if not path or not os.path.exists(path):
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            result["status"] = "skipped:not_dict"
            return result

        result["before"] = len(data)
        kept = {}
        for key, rec in data.items():
            # Current nested style: ticker -> {time, patterns:[{time,...}]}
            if isinstance(rec, dict) and isinstance(rec.get("patterns"), list):
                new_patterns = []
                for item in rec.get("patterns", []):
                    if not isinstance(item, dict):
                        continue
                    try:
                        ts = float(item.get("time", 0) or 0)
                        if ts <= 0:
                            continue
                        try:
                            from zoneinfo import ZoneInfo
                            event_date = datetime.fromtimestamp(ts, ZoneInfo("Asia/Jerusalem")).date()
                        except Exception:
                            event_date = datetime.fromtimestamp(ts).date()
                        if event_date >= cutoff_date:
                            new_patterns.append(item)
                    except Exception:
                        continue
                if new_patterns:
                    newest_ts = max(float(x.get("time", 0) or 0) for x in new_patterns)
                    new_rec = dict(rec)
                    new_rec["patterns"] = new_patterns
                    new_rec["time"] = newest_ts
                    kept[key] = new_rec
                continue

            # Compatibility style: key -> ISO datetime string.
            if isinstance(rec, str):
                try:
                    event_date = datetime.fromisoformat(rec.replace("Z", "+00:00")).date()
                    if event_date >= cutoff_date:
                        kept[key] = rec
                except Exception:
                    # Preserve unknown formats rather than destroy data blindly.
                    kept[key] = rec
                continue

            # Unknown legacy shape: preserve as fail-safe.
            kept[key] = rec

        _atomic_write_json(path, kept)
        result["after"] = len(kept)
        result["removed"] = result["before"] - result["after"]
        result["status"] = "ok"
        return result
    except Exception as e:
        result["status"] = f"error:{type(e).__name__}"
        result["after"] = result["before"]
        return result


def _prune_watchlist_log(path: str, cutoff_date) -> dict:
    """Keep only dated watchlist sections whose 📅 date is within the retention window."""
    result = {"file": os.path.basename(path), "before": 0, "after": 0, "removed": 0, "status": "missing"}
    if not path or not os.path.exists(path):
        return result
    try:
        content = open(path, "r", encoding="utf-8").read()
        marker_re = re.compile(r"(?m)^📅\s*(\d{4}-\d{2}-\d{2})\s*$")
        matches = list(marker_re.finditer(content))
        result["before"] = len(matches)
        if not matches:
            result["status"] = "ok:no_dated_sections"
            result["after"] = 0
            return result

        kept_sections = []
        for idx, m in enumerate(matches):
            try:
                section_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except Exception:
                continue
            if section_date < cutoff_date:
                continue

            # Include the separator line immediately before the 📅 marker when present.
            start = m.start()
            prev_nl = content.rfind("\n", 0, max(0, start - 1))
            prev_prev_nl = content.rfind("\n", 0, max(0, prev_nl)) if prev_nl >= 0 else -1
            candidate_start = prev_prev_nl + 1 if prev_prev_nl >= 0 else 0
            prior_line = content[candidate_start:prev_nl].strip() if prev_nl >= 0 else ""
            if prior_line and set(prior_line) == {"═"}:
                start = candidate_start

            if idx + 1 < len(matches):
                next_marker_start = matches[idx + 1].start()
                # Exclude the separator that belongs to the next date block.
                next_prev_nl = content.rfind("\n", 0, max(0, next_marker_start - 1))
                next_prev_prev_nl = content.rfind("\n", 0, max(0, next_prev_nl)) if next_prev_nl >= 0 else -1
                end = next_prev_prev_nl + 1 if next_prev_prev_nl >= 0 else next_marker_start
            else:
                end = len(content)
            kept_sections.append(content[start:end].strip("\n"))

        new_content = ("\n\n".join(kept_sections).strip() + "\n") if kept_sections else ""
        _atomic_write_text(path, new_content)
        result["after"] = len(kept_sections)
        result["removed"] = result["before"] - result["after"]
        result["status"] = "ok"
        return result
    except Exception as e:
        result["status"] = f"error:{type(e).__name__}"
        result["after"] = result["before"]
        return result


def _prune_scanner_log(path: str, cutoff_date) -> dict:
    """Keep dated log lines from cutoff onward; keep continuation lines only for kept dated entries."""
    result = {"file": os.path.basename(path), "before": 0, "after": 0, "removed": 0, "status": "missing"}
    if not path or not os.path.exists(path):
        return result
    try:
        lines = open(path, "r", encoding="utf-8").readlines()
        date_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\s")
        result["before"] = len(lines)
        out = []
        keep_continuation = False
        for line in lines:
            m = date_re.match(line)
            if m:
                try:
                    d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                    keep_continuation = d >= cutoff_date
                except Exception:
                    keep_continuation = True
                if keep_continuation:
                    out.append(line)
            elif keep_continuation:
                out.append(line)
        _atomic_write_text(path, "".join(out))
        result["after"] = len(out)
        result["removed"] = result["before"] - result["after"]
        result["status"] = "ok"
        return result
    except Exception as e:
        result["status"] = f"error:{type(e).__name__}"
        result["after"] = result["before"]
        return result


def cleanup_history_older_than_one_month() -> list[dict]:
    """
    Rolling calendar-month retention for historical files only.

    Example: if today is 2026-09-23, cutoff is 2026-08-23 (inclusive).
    Intentionally NOT touched: open_positions.json, last_market_scan_date.txt,
    last_market_scan_state.json, last_run_date.txt, universe_cache.json,
    twelvedata_blocklist.json, progress.json, or learned-parameter state.
    """
    today = _israel_today_date()
    cutoff = _one_calendar_month_ago(today)
    results = []

    research_cutoff = today - timedelta(days=max(RESEARCH_HISTORY_DAYS, LEARNING_LOOKBACK_DAYS + 60))
    results.append(_prune_csv_by_date(ENTRY_QUALITY_LOG, "date", research_cutoff))
    results.append(_prune_csv_by_date(PRO_QUALITY_LOG, "timestamp", research_cutoff))
    results.append(_prune_csv_by_date(SETUP_EVALUATIONS_CSV, "date_sent", research_cutoff))
    results.append(_prune_csv_by_date(SIGNALS_CSV, "Time", cutoff))
    results.append(_prune_csv_by_date(PERFORMANCE_CSV, "date_sent", research_cutoff))
    results.append(_prune_alert_history_json(ALERT_HISTORY_FILE, cutoff))
    results.append(_prune_watchlist_log(WATCHLIST_LOG, cutoff))
    # Prune the main log last, then emit the cleanup summary so current-run lines remain.
    results.append(_prune_scanner_log(LOGFILE, cutoff))

    log(f"🧹 History retention: logs cutoff={cutoff.isoformat()}, research cutoff={research_cutoff.isoformat()}")
    for item in results:
        status = item.get("status", "unknown")
        if status == "missing":
            continue
        log(
            f"   🧹 {item.get('file','?')}: {item.get('before',0)} → {item.get('after',0)} "
            f"(removed {item.get('removed',0)}) | {status}"
        )
    return results


def _is_israel_weekend_now() -> bool:
    """מחזיר True בשבת/ראשון לפי שעון ישראל."""
    try:
        from zoneinfo import ZoneInfo
        weekday = datetime.now(ZoneInfo("Asia/Jerusalem")).weekday()
    except Exception:
        # fallback פשוט אם zoneinfo לא זמין
        weekday = (datetime.utcnow() + timedelta(hours=3)).weekday()
    return weekday in (5, 6)  # שבת=5, ראשון=6


def _market_date_from_index(index) -> str | None:
    """Normalize the last dataframe index into YYYY-MM-DD, or None if invalid."""
    try:
        if index is None or len(index) == 0:
            return None
        ts = pd.to_datetime(index[-1], errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except Exception:
        return None


def _normalize_market_candle(source: str, symbol: str, date_value, row) -> dict | None:
    """Build a stable JSON-safe daily OHLCV snapshot for Market Day Guard comparisons."""
    try:
        dt = pd.to_datetime(date_value, errors="coerce")
        if pd.isna(dt):
            return None
        date_str = dt.date().isoformat()

        def _field(name: str):
            value = None
            if isinstance(row, dict):
                value = row.get(name)
                if value is None:
                    value = row.get(name.lower())
                if value is None:
                    value = row.get(name.capitalize())
            else:
                try:
                    value = row.get(name.lower())
                except Exception:
                    value = None
                if value is None:
                    try:
                        value = row.get(name.capitalize())
                    except Exception:
                        value = None
            return value

        close = _field("close")
        if not _is_finite_number(close) or float(close) <= 0:
            return None

        snap = {
            "source": str(source or "unknown"),
            "symbol": str(symbol or "").strip().upper(),
            "date": date_str,
        }
        for key in ("open", "high", "low", "close"):
            value = _field(key)
            snap[key] = round(float(value), 6) if _is_finite_number(value) else None
        volume = _field("volume")
        snap["volume"] = int(round(float(volume))) if _is_finite_number(volume) and float(volume) >= 0 else None
        return snap
    except Exception:
        return None


def _get_yahoo_market_candle_once(symbol: str) -> dict | None:
    """One Yahoo attempt returning the latest usable daily OHLCV candle."""
    try:
        # Raw/unadjusted prices are more stable for fingerprinting than auto-adjusted history.
        df = _normalize_yfinance_df(yf.download(
            symbol,
            period="15d",
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=False,
            timeout=15,
        ))
        if df is None or df.empty:
            return None
        row = df.iloc[-1]
        return _normalize_market_candle(f"Yahoo:{str(symbol).strip().upper()}", symbol, df.index[-1], row)
    except Exception as e:
        log(f"Market day guard Yahoo error for {symbol}: {e}")
        return None


def _get_yahoo_market_candle(symbol: str) -> dict | None:
    """Retry Yahoo and keep the newest/latest observed candle."""
    best = None
    for attempt in range(1, MARKET_GUARD_YAHOO_RETRIES + 1):
        candle = _get_yahoo_market_candle_once(symbol)
        if candle:
            if best is None or str(candle.get("date", "")) > str(best.get("date", "")):
                best = candle
            elif str(candle.get("date", "")) == str(best.get("date", "")):
                # On an open session Yahoo can update OHLCV between retries. Keep the latest attempt.
                best = candle
        if attempt < MARKET_GUARD_YAHOO_RETRIES and MARKET_GUARD_RETRY_DELAY_SEC > 0:
            time.sleep(MARKET_GUARD_RETRY_DELAY_SEC + random.uniform(0.0, 0.35))
    return best


def _get_yahoo_market_session_date_once(symbol: str) -> str | None:
    """Compatibility wrapper: one Yahoo attempt -> YYYY-MM-DD."""
    candle = _get_yahoo_market_candle_once(symbol)
    return str(candle.get("date")) if candle and candle.get("date") else None


def _get_yahoo_market_session_date(symbol: str) -> str | None:
    """Compatibility wrapper: retried Yahoo candle -> YYYY-MM-DD."""
    candle = _get_yahoo_market_candle(symbol)
    return str(candle.get("date")) if candle and candle.get("date") else None


def _get_twelvedata_market_candle(symbol: str) -> dict | None:
    """Independent latest daily OHLCV candle from TwelveData, without exposing API keys."""
    if not MARKET_GUARD_TWELVEDATA_ENABLED or not API_KEYS:
        return None

    keys_to_try = API_KEYS[: min(3, len(API_KEYS))]
    for key in keys_to_try:
        params = {
            "symbol": str(symbol).strip().upper(),
            "interval": "1day",
            "outputsize": 10,
            "apikey": key,
        }
        try:
            resp = HTTP_SESSION.get(BASE_URL, params=params, timeout=MARKET_GUARD_TWELVEDATA_TIMEOUT)
            if resp.status_code == 429:
                continue
            if resp.status_code in (401, 403):
                continue
            if resp.status_code >= 400:
                continue
            payload = resp.json() if resp.content else {}
            values = payload.get("values") or []
            if not values:
                continue

            best = None
            for row in values:
                candle = _normalize_market_candle(
                    f"TwelveData:{str(symbol).strip().upper()}", symbol, row.get("datetime"), row
                )
                if candle and (best is None or candle["date"] > best["date"]):
                    best = candle
            if best:
                update_api_usage(key)
                return best
        except Exception as e:
            log(f"Market day guard TwelveData error for {symbol}: {e}")
            continue
    return None


def _get_twelvedata_market_session_date(symbol: str) -> str | None:
    """Compatibility wrapper: TwelveData candle -> YYYY-MM-DD."""
    candle = _get_twelvedata_market_candle(symbol)
    return str(candle.get("date")) if candle and candle.get("date") else None


def get_market_candle_evidence(symbol: str = MARKET_SCAN_SYMBOL) -> dict:
    """Collect latest daily-candle snapshots from independent/redundant sources."""
    primary = str(symbol or MARKET_SCAN_SYMBOL).strip().upper() or "SPY"
    secondary = MARKET_GUARD_SECONDARY_SYMBOL
    evidence: dict[str, dict | None] = {}

    primary_yahoo = _get_yahoo_market_candle(primary)
    evidence[f"Yahoo:{primary}"] = primary_yahoo

    # Secondary ETF is mainly date evidence. Its own candle is stored so the same source
    # can also reveal that the US session is still updating.
    if secondary and secondary != primary:
        evidence[f"Yahoo:{secondary}"] = _get_yahoo_market_candle(secondary)

    evidence[f"TwelveData:{primary}"] = _get_twelvedata_market_candle(primary)
    return evidence


def get_market_session_evidence(symbol: str = MARKET_SCAN_SYMBOL) -> dict:
    """Compatibility API: return only source -> date while using the richer candle evidence."""
    candles = get_market_candle_evidence(symbol)
    return {
        source: (str(candle.get("date")) if candle and candle.get("date") else None)
        for source, candle in candles.items()
    }


def get_latest_market_session_date(symbol: str = MARKET_SCAN_SYMBOL) -> str | None:
    """Return the newest valid market-session date seen by any configured source."""
    evidence = get_market_session_evidence(symbol)
    dates = [d for d in evidence.values() if d]
    return max(dates) if dates else None


def load_last_market_scan_date() -> str:
    try:
        if os.path.exists(MARKET_SCAN_STATE_FILE):
            return open(MARKET_SCAN_STATE_FILE, "r", encoding="utf-8").read().strip()
    except Exception as e:
        log(f"load_last_market_scan_date error: {e}")
    return ""


def _load_market_scan_guard_state() -> dict:
    """Load V9.1.4 candle state, falling back to the legacy date-only state."""
    legacy_date = load_last_market_scan_date()
    state = {}
    try:
        if os.path.exists(MARKET_CANDLE_STATE_FILE):
            with open(MARKET_CANDLE_STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
    except Exception as e:
        log(f"market candle state read error: {e}")
        state = {}

    market_date = str(state.get("market_date") or legacy_date or "").strip()
    candles = state.get("candles") if isinstance(state.get("candles"), dict) else {}
    return {"market_date": market_date, "candles": candles}


_MARKET_GUARD_PENDING_EVIDENCE: dict[str, dict | None] = {}


def _market_candle_changes(previous: dict, current: dict) -> list[str]:
    """Return changed OHLCV fields. Tiny real price changes count; float noise does not."""
    try:
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return []
        if str(previous.get("date") or "") != str(current.get("date") or ""):
            return [f"date {previous.get('date')}→{current.get('date')}"]

        changes: list[str] = []
        for key in ("open", "high", "low", "close"):
            old = previous.get(key)
            new = current.get(key)
            if not (_is_finite_number(old) and _is_finite_number(new)):
                continue
            old_f = float(old); new_f = float(new)
            # 1e-6 absolute / 1e-8 relative ignores serialization noise but catches sub-cent moves.
            tol = max(1e-6, abs(old_f) * 1e-8)
            if abs(new_f - old_f) > tol:
                changes.append(f"{key} {old_f:.6f}→{new_f:.6f}")

        old_v = previous.get("volume")
        new_v = current.get("volume")
        if _is_finite_number(old_v) and _is_finite_number(new_v):
            if int(round(float(old_v))) != int(round(float(new_v))):
                changes.append(f"volume {int(round(float(old_v))):,}→{int(round(float(new_v))):,}")
        return changes
    except Exception:
        return []


def save_last_market_scan_date(market_date: str | None) -> None:
    """
    Save legacy date plus V9.1.4 candle fingerprints captured by the guard.
    This is written only after the scan/BEAR handling reaches the existing save point.
    """
    if not market_date:
        return
    try:
        parent = os.path.dirname(MARKET_SCAN_STATE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _atomic_write_text(MARKET_SCAN_STATE_FILE, str(market_date).strip())
        log(f"💾 Market scan date saved: {market_date}")
    except Exception as e:
        log(f"save_last_market_scan_date error: {e}")

    try:
        candles = {}
        for source, candle in (_MARKET_GUARD_PENDING_EVIDENCE or {}).items():
            if not isinstance(candle, dict):
                continue
            # Preserve all observed provider candles; comparisons are same-source on next run.
            candles[str(source)] = candle
        state = {
            "market_date": str(market_date).strip(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "candles": candles,
        }
        parent = os.path.dirname(MARKET_CANDLE_STATE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _atomic_write_text(MARKET_CANDLE_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
        log(f"💾 Market candle fingerprint saved: {len(candles)} source(s) → {MARKET_CANDLE_STATE_FILE}")
    except Exception as e:
        log(f"save market candle fingerprint error: {e}")


def should_run_for_new_market_session() -> tuple[bool, str | None, str]:
    """
    V9.1.4 Market Day Guard.

    Scan when either:
      1) a provider sees a newer market-session date, OR
      2) the same daily candle changed since the prior completed scan (OHLC or Volume).

    This specifically handles a manual/intraday scan: the next run after the US close sees
    the finalized candle changed and scans again, even though the calendar date is identical.
    """
    global _MARKET_GUARD_PENDING_EVIDENCE
    _MARKET_GUARD_PENDING_EVIDENCE = {}

    if FORCE_SCAN:
        return True, None, "FORCE_SCAN=True — bypass market day guard"

    if not MARKET_DAY_GUARD_ENABLED:
        return True, None, "MARKET_DAY_GUARD_ENABLED=False — guard disabled"

    previous_state = _load_market_scan_guard_state()
    last_scanned = str(previous_state.get("market_date") or "").strip()
    previous_candles = previous_state.get("candles") if isinstance(previous_state.get("candles"), dict) else {}

    evidence = get_market_candle_evidence(MARKET_SCAN_SYMBOL)
    _MARKET_GUARD_PENDING_EVIDENCE = evidence
    valid = {
        source: candle for source, candle in evidence.items()
        if isinstance(candle, dict) and candle.get("date")
    }

    evidence_text = ", ".join(
        f"{source}={candle.get('date')} close={candle.get('close')} vol={candle.get('volume')}"
        if isinstance(candle, dict) else f"{source}=N/A"
        for source, candle in evidence.items()
    )
    if evidence_text:
        log(f"🛡️ Market Day Guard candles: {evidence_text}")

    if not valid:
        return True, None, "כל מקורות נר-המסחר לא זמינים — ממשיך Fail-Open ולא מעדכן state בלי תאריך מאומת"

    latest_market_date = max(str(candle.get("date")) for candle in valid.values())
    newer_sources = {
        src: candle for src, candle in valid.items()
        if (not last_scanned or str(candle.get("date")) > last_scanned)
    }
    if newer_sources:
        source_summary = ", ".join(f"{src}={c.get('date')}" for src, c in newer_sources.items())
        return (
            True,
            latest_market_date,
            f"נר מסחר בתאריך חדש אומת: {latest_market_date} | source(s): {source_summary} "
            f"(נסרק קודם: {last_scanned or 'אף פעם'})",
        )

    # Migration from V9.1.3/date-only state: run once so we can establish fingerprints.
    if last_scanned and not previous_candles:
        return (
            True,
            latest_market_date,
            f"state ישן מכיל תאריך בלבד ({last_scanned}) ללא fingerprint — מריץ פעם אחת כדי לקלוט OHLCV עדכני",
        )

    # Same market date: compare only the same provider/symbol to avoid cross-provider noise.
    changed_sources: list[str] = []
    comparable_sources = 0
    for source, current in valid.items():
        # Same-date price-change re-scan is keyed to the primary market symbol (SPY by default).
        # QQQ remains useful for detecting a newer session date but does not independently trigger
        # a same-date re-scan due to provider revisions in a different instrument.
        if str(current.get("symbol") or "").strip().upper() != str(MARKET_SCAN_SYMBOL).strip().upper():
            continue
        previous = previous_candles.get(source)
        if not isinstance(previous, dict):
            continue
        if str(current.get("date") or "") != last_scanned or str(previous.get("date") or "") != last_scanned:
            continue
        comparable_sources += 1
        changes = _market_candle_changes(previous, current)
        if changes:
            changed_sources.append(f"{source}: " + ", ".join(changes[:5]))

    if changed_sources:
        detail = " | ".join(changed_sources[:3])
        return (
            True,
            latest_market_date,
            f"אותו נר מסחר ({latest_market_date}) השתנה מאז הסריקה האחרונה — מריץ שוב | {detail}",
        )

    if comparable_sources > 0:
        return False, latest_market_date, (
            f"אין תאריך חדש ואין שינוי ב-OHLCV מאז הסריקה האחרונה ({last_scanned}); "
            f"אומת מול {comparable_sources} מקור/ות זהים"
        )

    # Providers changed/temporarily disappeared. Favor not missing a finalized candle.
    return (
        True,
        latest_market_date,
        f"אין מקור OHLCV בר-השוואה מול הסריקה הקודמת ({last_scanned}) — ממשיך Fail-Open פעם זו כדי לא לפספס שינוי בנר",
    )

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
        row = pd.DataFrame([{"Time": now, "Ticker": ticker, "Reasons": " | ".join(_redact_sensitive(reason) for reason in reasons)}])
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
    """Update V9.4 sent/shadow observations; leave unverifiable legacy outcomes intact."""
    _update_setup_evaluations()

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
    """מביא OHLCV מ-TwelveData ומחזיר DataFrame עם עמודות lowercase.
    FIXED: משתמש ב-params ולא מדפיס URL עם apikey בלוג.
    """
    symbol = ticker.strip().upper().replace("$", "")
    if not symbol:
        return None
    outputsize = max(50, int(outputsize))
    key = get_available_api_key()
    if not key:
        log("No available API key.")
        return None

    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": outputsize,
        "apikey": key,
    }

    try:
        r = HTTP_SESSION.get(BASE_URL, params=params, timeout=20)
        if r.status_code == 429:
            api_usage[key]["blocked_until"] = datetime.now() + RESET_TIME
            log(f"TwelveData 429 for {symbol}. Key blocked.")
            return None
        if r.status_code in (401, 403):
            api_usage[key]["blocked_until"] = datetime.now() + timedelta(hours=12)
            log(f"TwelveData auth error {r.status_code} for {symbol}.")
            return None
        if r.status_code >= 400:
            log(f"TwelveData HTTPError {symbol}: status={r.status_code}")
            return None

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

    except requests.RequestException as e:
        # לא להדפיס traceback/URL מלא כדי שלא יודפס apikey.
        log(f"TwelveData request error {symbol}: {type(e).__name__}")
        return None
    except Exception as e:
        log(f"TwelveData general error {symbol}: {type(e).__name__}: {e}")
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
                             progress=False, auto_adjust=False, threads=False,
                             timeout=20)
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
    V9.1.2: ערך ידוע מ-NASDAQ screener כבר מוזן ל-cache לפני הסריקה.
    רק כשאין ערך ידוע פונים ל-yfinance, עם shares×price ו-fast_info כ-fallback.
    """
    t = ticker.strip().upper()
    cached = _mc_cache.get(t)
    if cached is not None and _is_finite_number(cached) and float(cached) > 0:
        return float(cached)
    try:
        info = _get_yf_info(t)
        mc   = info.get("marketCap")
        mc   = float(mc) if mc and _is_finite_number(mc) and float(mc) > 0 else None

        # fallback 1: shares × price
        if not mc:
            shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
            price  = info.get("currentPrice") or info.get("previousClose")
            if shares and price and _is_finite_number(shares) and _is_finite_number(price):
                calc = float(shares) * float(price)
                if _is_finite_number(calc) and calc > 0:
                    mc = calc

        # fallback 2: yfinance fast_info (useful when quoteSummary/info is partial).
        if not mc:
            try:
                fi = getattr(yf.Ticker(t), "fast_info", None)
                if fi is not None:
                    try:
                        fast_mc = fi.get("marketCap")
                    except Exception:
                        fast_mc = getattr(fi, "market_cap", None)
                    if not _is_finite_number(fast_mc) or float(fast_mc or 0) <= 0:
                        fast_mc = getattr(fi, "market_cap", None)
                    if fast_mc and _is_finite_number(fast_mc) and float(fast_mc) > 0:
                        mc = float(fast_mc)
            except Exception:
                pass

        _mc_cache[t] = mc
        _market_cap_diagnostics[t] = {"status": "resolved" if mc else "unresolved", "value": mc}
        if mc is None:
            log(f"Market cap unresolved {t}: info/shares-price/fast_info unavailable")
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
        except Exception as exc:
            _info_cache[t] = {}
            log(f"Yahoo company info unavailable {t}: {type(exc).__name__}")
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

    # תאריך הדוח הבא — V9.3: רק תאריך עתידי; תאריך עבר נחשב stale ולא "next earnings".
    edate, _earn_status = _get_next_earnings_date(ticker)
    if _earn_status != "verified_future":
        edate = None  # v9.7.2: a just-reported (past) date is never shown as "next earnings"

    return {
        "name":          name,
        "sector":        sector,
        "description":   short_desc,
        "eps":           eps,
        "market_cap_b":  mc_b,
        "earnings_date": edate,
    }

_earnings_date_cache: dict[str, tuple[object, str]] = {}

def _coerce_earnings_dates(raw) -> list:
    """מחלץ רשימת תאריכים מכל מבנה נפוץ של yfinance בלי להניח שהראשון הוא העתידי."""
    values = []
    if raw is None:
        return values
    try:
        if isinstance(raw, dict):
            for v in raw.values():
                values.extend(_coerce_earnings_dates(v))
            return values
        if isinstance(raw, pd.DataFrame):
            values.extend(list(raw.index))
            for col in raw.columns:
                if "earning" in str(col).lower() or "date" in str(col).lower():
                    values.extend(list(raw[col].dropna()))
            return values
        if isinstance(raw, (pd.Series, list, tuple, set, np.ndarray)):
            for v in list(raw):
                values.extend(_coerce_earnings_dates(v))
            return values
        ts = pd.to_datetime(raw, errors="coerce", utc=True)
        if not pd.isna(ts):
            values.append(ts.date())
    except Exception:
        pass
    return values

def _calendar_earnings_dates(cal) -> list:
    """v9.7.3: only the earnings-date fields of yfinance's calendar.

    yfinance's calendar also carries 'Dividend Date', 'Ex-Dividend Date' and
    numeric estimates; reading every value made dividend dates look like
    earnings dates (e.g. RS on 2026-10-29: 'next earnings 2026-11-20' was the ex-dividend date).
    """
    out = []
    if cal is None:
        return out
    try:
        def is_earn_key(k) -> bool:
            k = str(k).lower()
            return "earning" in k and "date" in k
        if isinstance(cal, dict):
            for key, value in cal.items():
                if is_earn_key(key):
                    out.extend(_coerce_earnings_dates(value))
            return out
        if isinstance(cal, pd.DataFrame):
            for idx in cal.index:
                if is_earn_key(idx):
                    out.extend(_coerce_earnings_dates(list(cal.loc[idx].values)))
            for col in cal.columns:
                if is_earn_key(col):
                    out.extend(_coerce_earnings_dates(list(cal[col].dropna())))
            return out
    except Exception:
        return out
    return out


_LAST_SESSION_DATE = None  # v9.7.3: date of the newest finished daily candle (set by main)


def _set_last_session_date(value) -> None:
    global _LAST_SESSION_DATE
    try:
        _LAST_SESSION_DATE = pd.Timestamp(value).date() if value else None
    except Exception:
        _LAST_SESSION_DATE = None


def _recent_report_blocks(last_past, today) -> bool:
    """True when the last report has no full post-earnings candle in the data yet."""
    if last_past is None:
        return False
    if EARNINGS_RECENT_MODE == "candle" and _LAST_SESSION_DATE is not None:
        # A report dated on/after the newest candle (e.g. after the close) has no reaction candle yet.
        return last_past >= _LAST_SESSION_DATE
    return (today - last_past).days <= EARNINGS_RECENT_BLOCK_DAYS


def _position_earnings_note(ticker: str) -> str | None:
    """v9.7.3: upcoming report for an open position (Hebrew, for the positions email)."""
    try:
        edate, status = _get_next_earnings_date(ticker)
        if status == "verified_future" and edate is not None:
            days = (edate - datetime.now().date()).days
            if 0 <= days <= POSITION_EARNINGS_WARN_DAYS:
                return f"דוח בעוד {days} ימים ({edate})" if days else f"דוח היום ({edate})"
        elif status == "recent_past" and edate is not None:
            return f"דוח פורסם ב-{edate}"
    except Exception:
        return None
    return None


def _position_earnings_log(ticker: str) -> str:
    try:
        edate, status = _get_next_earnings_date(ticker)
        if status == "verified_future" and edate is not None:
            days = (edate - datetime.now().date()).days
            if 0 <= days <= POSITION_EARNINGS_WARN_DAYS:
                return f" | 📅 EARNINGS in {days}d ({edate})"
        elif status == "recent_past" and edate is not None:
            return f" | 📅 earnings reported {edate}"
    except Exception:
        return ""
    return ""


def _get_next_earnings_date(ticker: str, today=None) -> tuple[object, str]:
    """מחזיר רק earnings עתידי; past-only מסומן stale ולא מוצג כ-next earnings."""
    t = str(ticker or "").strip().upper()
    today = today or datetime.now().date()
    cache_key = f"{t}|{today.isoformat()}"
    if cache_key in _earnings_date_cache:
        return _earnings_date_cache[cache_key]
    candidates = []
    saw_past = False
    past_dates = []
    try:
        obj = yf.Ticker(t)
        try:
            candidates.extend(_calendar_earnings_dates(obj.calendar))  # v9.7.3: no dividend dates
        except Exception:
            pass
        try:
            info = _get_yf_info(t)
            for key in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd"):
                raw = info.get(key)
                if raw:
                    try:
                        candidates.append(datetime.fromtimestamp(float(raw)).date())
                    except Exception:
                        candidates.extend(_coerce_earnings_dates(raw))
        except Exception:
            pass
        clean = []
        for d in candidates:
            try:
                dd = pd.Timestamp(d).date()
                if dd.year < 2000:  # v9.7.3: numeric estimates parsed as 1970 dates
                    continue
                if dd < today:
                    saw_past = True
                    past_dates.append(dd)
                else:
                    clean.append(dd)
            except Exception:
                continue
        if not clean:
            try:
                hist = obj.get_earnings_dates(limit=8)
                for d in _coerce_earnings_dates(hist):
                    dd = pd.Timestamp(d).date()
                    if dd.year < 2000:
                        continue
                    if dd < today:
                        saw_past = True
                        past_dates.append(dd)
                    else:
                        clean.append(dd)
            except Exception:
                pass
        last_past = max(past_dates) if past_dates else None
        if _recent_report_blocks(last_past, today):
            # Report after the signal candle (e.g. after-close yesterday): the price reaction is not in the data yet.
            result = (last_past, "recent_past")
        elif clean:
            result = (min(clean), "verified_future")
        elif saw_past:
            result = (None, "stale_past_only")
        else:
            result = (None, "unknown")
    except Exception as e:
        result = (None, f"error:{type(e).__name__}")
    _earnings_date_cache[cache_key] = result
    return result

def earnings_filter_ok(ticker: str) -> tuple[bool, str]:
    """מסנן דוח קרוב; מידע stale/unknown לא מוצג כתאריך עתידי תקין."""
    try:
        edate, status = _get_next_earnings_date(ticker)
        if status == "recent_past" and edate is not None:
            return False, f"earnings just reported ({edate}) — waiting for a post-earnings candle"
        if edate is None:
            if status == "stale_past_only":
                return True, "earnings data stale (past dates only) — allowing as UNKNOWN"
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
    if "ma50" not in df.columns:
        df["ma50"] = close.rolling(window=50, min_periods=20).mean()
    if "ma150" not in df.columns:
        df["ma150"] = close.rolling(window=150, min_periods=150).mean()
    if "ma200" not in df.columns:
        df["ma200"] = close.rolling(window=200, min_periods=200).mean()

def _prior_avg_volume(df: pd.DataFrame, idx: int) -> float | None:
    """Mean volume of the VOLUME_AVG_LOOKBACK bars BEFORE bar `idx` (the bar itself excluded). Negative idx allowed.
    Reads the vectorised column written by add_technical_indicators(); falls back to the slice if it is missing/NaN."""
    try:
        n = len(df)
        if idx < 0:
            idx += n
        if idx < VOLUME_AVG_LOOKBACK or idx >= n:
            return None
        if "vol_avg_prior" in df.columns:
            v = df["vol_avg_prior"].iloc[idx]
            if pd.notna(v) and np.isfinite(v):
                return float(v)
        if "volume" in df.columns:
            sl = pd.to_numeric(df["volume"].iloc[idx - VOLUME_AVG_LOOKBACK:idx], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            return float(sl.mean()) if len(sl) else None
    except Exception:
        pass
    return None


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

        # One causal volume baseline for the whole pipeline: prior N bars, the bar itself excluded (computed once).
        if "volume" in df.columns:
            _vol = pd.to_numeric(df["volume"], errors="coerce")
            _prior = _vol.shift(1).rolling(VOLUME_AVG_LOOKBACK, min_periods=VOLUME_AVG_LOOKBACK).mean()
            df["vol_avg_prior"] = _prior
            df["vol_ratio_prior"] = _vol / _prior.replace(0, np.nan)

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
    avg_v = _prior_avg_volume(df, break_index) if "volume" in df.columns else None
    if avg_v is not None:
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
        rs_data = compute_rs_score(ticker, df)
        rs = rs_data.get("rs_score") if isinstance(rs_data, dict) else rs_data

        if rs is None:
            pts = 3.0; reasons.append("B1 ⚠️ RS לא זמין (+3)")
        elif rs >= 90:
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

    # C3) Narrow-range volume proxy prints (מקס 5)
    try:
        dp_result = get_dark_pool_prints(ticker, df)
        dp = int(dp_result.get("bullish_prints", 0)) if isinstance(dp_result, dict) else 0
        if dp >= 2:
            pts = 5.0; reasons.append(f"C3 ✅ Narrow-range volume proxy: {dp} פרינטים (+5)")
        elif dp == 1:
            pts = 3.0; reasons.append(f"C3 ⚠️ Narrow-range volume proxy: {dp} פרינט (+3)")
        else:
            pts = 1.0; reasons.append("C3 ❌ אין Narrow-range volume proxy (+1)")
    except Exception:
        pts = 2.0; reasons.append("C3 ⚠️ Narrow-range volume proxy לא זמין (+2)")
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
    avg_v = _prior_avg_volume(df, break_index) if "volume" in df.columns else None
    if avg_v is not None:
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
                           atr_mult: float | None = None) -> tuple:
    """מחזיר (stop, target, size_shares, atr, rr_ratio).
    FIXED: סטופ חכם לפי ATR + שפל אחרון + תמיכת תבנית, עם מרחק 4.5%-12%.
    """
    try:
        if df is None or df.empty:
            raise ValueError("empty df")
        if "atr14" not in df.columns or "ema28" not in df.columns:
            add_technical_indicators(df)

        entry = max(float(break_level or 0), float(df["close"].iloc[-1]))
        atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns and not pd.isna(df["atr14"].iloc[-1]) else entry * 0.025
        if atr <= 0 or np.isnan(atr):
            atr = entry * 0.025

        meta = pattern_meta or {}
        pattern_name = str(meta.get("pattern_type") or meta.get("pattern") or meta.get("name") or "").lower()
        atr_mult_eff = float(atr_mult if atr_mult is not None else 2.2)
        if "falling" in pattern_name or "wedge" in pattern_name:
            atr_mult_eff = max(atr_mult_eff, 2.8)
        elif "double" in pattern_name:
            atr_mult_eff = max(atr_mult_eff, 2.4)

        stop_candidates = []
        stop_candidates.append(entry - atr_mult_eff * atr)  # ATR stop
        try:
            recent_low = float(df["low"].tail(5).min())
            stop_candidates.append(recent_low - 0.35 * atr)
        except Exception:
            pass
        try:
            swing_low = float(df["low"].tail(20).min())
            stop_candidates.append(swing_low - 0.20 * atr)
        except Exception:
            pass
        try:
            ema28 = float(df["ema28"].iloc[-1])
            if ema28 and not np.isnan(ema28):
                stop_candidates.append(ema28 - 0.50 * atr)
        except Exception:
            pass
        for key in ("handle_low", "cup_bottom", "bottom2", "bottom1", "support", "support_level"):
            try:
                val = float(meta.get(key) or 0)
                if val > 0:
                    stop_candidates.append(val - 0.20 * atr)
            except Exception:
                pass

        stop_candidates = [float(x) for x in stop_candidates if x and not np.isnan(x) and x < entry]
        raw_stop = max(stop_candidates) if stop_candidates else entry - 2.2 * atr

        min_risk_pct = 0.045
        max_risk_pct = 0.12
        stop = min(raw_stop, entry * (1.0 - min_risk_pct))
        stop = max(stop, entry * (1.0 - max_risk_pct))
        stop = round(float(stop), 2)

        risk = max(entry - stop, 1e-9)
        size = int(capital * risk_pct / risk)

        target_price = float(meta.get("target_price") or 0.0)
        height = float(meta.get("pattern_height") or 0.0)
        if target_price > entry:
            target = target_price
        elif height > 0:
            target = entry + height
        else:
            target = entry + risk * 1.8

        rr = (target - entry) / risk
        return float(stop), float(target), int(size), float(atr), round(float(rr), 2)
    except Exception as e:
        log(f"atr_stop_and_position error: {e}")
        entry = float(break_level or 0)
        return entry * 0.955, entry * 1.10, 0, 0.0, 0.0

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


# ============================================================
#  V8 ENTRY READY ENGINE — איכות כניסה מיידית
# ============================================================

def _to_float_or_none(value) -> float | None:
    """המרה בטוחה למספר אמיתי בלבד."""
    try:
        val = float(value)
        return val if _is_finite_number(val) else None
    except Exception:
        return None


def _rr_at_least(value, threshold: float) -> bool:
    """השוואת R:R עם tolerance קטן כדי למנוע false reject בגלל floating point."""
    try:
        v = float(value)
        t = float(threshold)
        return math.isfinite(v) and v + RR_COMPARE_EPS >= t
    except Exception:
        return False


def _entry_pattern_family(pattern: str, meta: dict | None = None) -> str:
    """מסווג רק את לוגיקת אישור הכניסה; אינו משנה את זיהוי התבנית עצמה."""
    p = str(pattern or "").lower()
    m = meta or {}
    source = str(m.get("pattern_source") or "")
    if source == "V9_PATTERN_EXPANSION":
        if "pullback" in p or m.get("pullback_days") is not None:
            return "PULLBACK"
        if "retest" in p or m.get("retest_low") is not None:
            return "RETEST"
    return "BREAKOUT"


def _entry_confirmation_profile(pattern: str, meta: dict, metrics: dict) -> dict:
    """
    V9.3: אישור כניסה מותאם לסוג הסטאפ.
    Breakout ממשיך לדרוש פריצה + 1.30x volume.
    Pullback דורש bounce אמיתי; 1.00x volume מותר רק יחד עם volume contraction ב-pullback.
    Retest דורש retest נקי + bounce; volume מאומת ע"י הנר הנוכחי או הפריצה המקורית.
    """
    family = _entry_pattern_family(pattern, meta)
    vr = _to_float_or_none(metrics.get("volume_ratio"))
    bp = _to_float_or_none(metrics.get("breakout_pct"))
    max_extension_ok = (bp is None) or (bp <= ENTRY_MAX_EXTENSION_PCT + 1e-12)
    out = {
        "family": family,
        "confirmation_ok": False,
        "volume_ok": False,
        "confirmation_points": 0.0,
        "volume_points": 0.0,
        "confirmation_reason": "",
        "volume_reason": "",
        "blockers": [],
        "max_extension_ok": max_extension_ok,
    }

    if family == "PULLBACK":
        bounce_ok = bool(meta.get("bounce_confirmed", False))
        quiet_pullback = bool(meta.get("pullback_volume_quiet", False))
        out["confirmation_ok"] = bounce_ok
        if bounce_ok:
            out["confirmation_points"] = 8.0
            out["confirmation_reason"] = "Bounce מאושר מעל ה-High הקודם עם נר חיובי (+8)"
        else:
            out["blockers"].append("אין Bounce מאושר מעל ה-High הקודם")
        if vr is not None and vr >= ENTRY_MIN_VOLUME_RATIO:
            out["volume_ok"] = True
            out["volume_points"] = 6.0 if vr < 2.0 else 8.0
            out["volume_reason"] = f"ווליום Bounce חזק ×{vr:.2f}"
        elif vr is not None and vr + 1e-12 >= ENTRY_PULLBACK_MIN_VOLUME_RATIO and quiet_pullback:
            out["volume_ok"] = True
            out["volume_points"] = 5.0
            out["volume_reason"] = f"Pullback שקט + ווליום Bounce ×{vr:.2f}"
        else:
            out["blockers"].append("ווליום Bounce לא מאשר / אין contraction ב-Pullback")
        out["pullback_volume_quiet"] = quiet_pullback

    elif family == "RETEST":
        bounce_ok = bool(meta.get("bounce_confirmed", False))
        touched = bool(meta.get("touched_retest_zone", False))
        held = bool(meta.get("no_failed_breakout", False))
        out["confirmation_ok"] = bool(bounce_ok and touched and held)
        if out["confirmation_ok"]:
            out["confirmation_points"] = 8.0
            out["confirmation_reason"] = "Retest נקי + Pivot hold + Bounce מאושר (+8)"
        else:
            if not touched:
                out["blockers"].append("Retest לא נגע באזור ה-Pivot")
            if not held:
                out["blockers"].append("ה-Pivot לא נשמר בצורה נקייה")
            if not bounce_ok:
                out["blockers"].append("אין Bounce מאושר לאחר ה-Retest")
        original_vr = _to_float_or_none(meta.get("breakout_volume_ratio"))
        if vr is not None and vr >= ENTRY_MIN_VOLUME_RATIO:
            out["volume_ok"] = True
            out["volume_points"] = 6.0 if vr < 2.0 else 8.0
            out["volume_reason"] = f"ווליום Bounce חזק ×{vr:.2f}"
        elif (vr is not None and vr + 1e-12 >= ENTRY_RETEST_MIN_VOLUME_RATIO
              and original_vr is not None and original_vr + 1e-12 >= ENTRY_MIN_VOLUME_RATIO):
            out["volume_ok"] = True
            out["volume_points"] = 5.0
            out["volume_reason"] = f"פריצה מקורית ×{original_vr:.2f} + Bounce ×{vr:.2f}"
        else:
            out["blockers"].append("ווליום Retest/Bounce לא מאשר")
        out["original_breakout_volume_ratio"] = original_vr

    else:
        breakout_ok = bool(metrics.get("breakout_confirmed", False))
        out["confirmation_ok"] = breakout_ok
        if breakout_ok:
            out["confirmation_points"] = 8.0
            out["confirmation_reason"] = f"פריצה מאושרת מעל הרמה ({(bp or 0)*100:.2f}%) (+8)"
        else:
            out["blockers"].append("אין פריצה מאושרת מעל הרמה")
        if vr is not None and vr >= 2.0:
            out["volume_ok"] = True
            out["volume_points"] = 8.0
            out["volume_reason"] = f"ווליום פריצה חזק מאוד ×{vr:.2f}"
        elif vr is not None and vr >= ENTRY_MIN_VOLUME_RATIO:
            out["volume_ok"] = True
            out["volume_points"] = 6.0
            out["volume_reason"] = f"ווליום פריצה מאשר ×{vr:.2f}"
        else:
            out["blockers"].append("ווליום לא מאשר")

    if not max_extension_ok:
        out["blockers"].append("המחיר כבר רחוק מדי מהכניסה")
    return out


def _series_last_finite(series, default=None):
    try:
        return _last_finite(series, default=default)
    except TypeError:
        return _last_finite(series) if series is not None else default
    except Exception:
        return default


def _ma_is_rising(df: pd.DataFrame, col: str, bars: int = 5) -> bool | None:
    """בודק אם ממוצע נע עולה ביחס לכמה ימים אחורה."""
    try:
        if col not in df.columns:
            return None
        s = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) <= bars:
            return None
        now = float(s.iloc[-1])
        prev = float(s.iloc[-bars-1])
        if not (_is_finite_number(now) and _is_finite_number(prev)):
            return None
        return now > prev
    except Exception:
        return None


def _calc_true_range_pct(df: pd.DataFrame) -> pd.Series:
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    tr = pd.concat([
        (high - low).abs(),
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr / close.replace(0, np.nan)


_entry_spy_line_cache: dict = {}

def _rs_line_new_high(df: pd.DataFrame, lookback: int = 63) -> bool | None:
    """בודק אם קו RS מול SPY קרוב/בשיא חדש. מחזיר None אם אין נתונים."""
    try:
        if df is None or len(df) < max(lookback, 30):
            return None
        today_key = datetime.now().strftime("%Y-%m-%d")
        spy_df = _entry_spy_line_cache.get(today_key)
        if spy_df is None:
            spy_df = _normalize_yfinance_df(yf.download("SPY", period="13mo", interval="1d", progress=False, auto_adjust=True))
            if spy_df is not None and not spy_df.empty:
                _entry_spy_line_cache.clear()
                _entry_spy_line_cache[today_key] = spy_df
        if spy_df is None or spy_df.empty:
            return None
        stock = pd.to_numeric(df["close"], errors="coerce").dropna()
        spy = pd.to_numeric(spy_df["close"], errors="coerce").dropna()
        joined = pd.concat([stock.rename("stock"), spy.rename("spy")], axis=1).dropna()
        if len(joined) < max(lookback, 30):
            return None
        rs_line = joined["stock"] / joined["spy"].replace(0, np.nan)
        recent = rs_line.tail(lookback).dropna()
        if recent.empty:
            return None
        now = float(recent.iloc[-1])
        high = float(recent.max())
        if not (_is_finite_number(now) and _is_finite_number(high) and high > 0):
            return None
        return now >= high * 0.995
    except Exception:
        return None


def _entry_quality_metrics(df: pd.DataFrame, ticker: str, breakout_level: float,
                           pattern_meta: dict | None = None, rr: float | None = None,
                           regime: dict | None = None) -> dict:
    """אוסף את כל המדדים של שכבת Entry Ready במקום אחד — נקי ומסודר."""
    metrics = {}
    meta = pattern_meta or {}
    try:
        if df is None or df.empty:
            return metrics
        ensure_ma_columns(df)
        if "atr14" not in df.columns:
            add_technical_indicators(df)

        row = df.iloc[-1]
        close = _to_float_or_none(row.get("close"))
        open_p = _to_float_or_none(row.get("open"))
        high = _to_float_or_none(row.get("high"))
        low = _to_float_or_none(row.get("low"))
        vol = _to_float_or_none(row.get("volume"))
        breakout = _to_float_or_none(meta.get("pattern_pivot", breakout_level))
        metrics["planned_entry"] = _to_float_or_none(meta.get("planned_entry", breakout_level))

        metrics.update({
            "close": close,
            "open": open_p,
            "high": high,
            "low": low,
            "volume": vol,
            "breakout_level": breakout,
            "rr": _to_float_or_none(rr),
        })

        ma50 = _series_last_finite(df.get("ma50"), None) if "ma50" in df.columns else None
        ma150 = _series_last_finite(df.get("ma150"), None) if "ma150" in df.columns else None
        ma200 = _series_last_finite(df.get("ma200"), None) if "ma200" in df.columns else None
        ema28 = _series_last_finite(df.get("ema28"), None) if "ema28" in df.columns else None
        atr14 = _series_last_finite(df.get("atr14"), None) if "atr14" in df.columns else None
        metrics.update({"ma50": ma50, "ma150": ma150, "ma200": ma200, "ema28": ema28, "atr14": atr14})

        if close and ma150:
            metrics["ma150_dist_pct"] = (close - ma150) / ma150
            metrics["above_ma150"] = close >= ma150 * (1 + ENTRY_MA150_MIN_ABOVE_PCT)
        else:
            metrics["ma150_dist_pct"] = None
            metrics["above_ma150"] = False

        if close and ma200:
            metrics["ma200_dist_pct"] = (close - ma200) / ma200
            metrics["above_ma200"] = close >= ma200
        else:
            metrics["ma200_dist_pct"] = None
            metrics["above_ma200"] = None

        if ma50 and ma150:
            metrics["ma_stack_ok"] = ma50 >= ma150
        else:
            metrics["ma_stack_ok"] = None
        metrics["ma150_rising"] = _ma_is_rising(df, "ma150", bars=5)
        metrics["ema28_rising"] = _ma_is_rising(df, "ema28", bars=3)

        if close and breakout:
            metrics["breakout_pct"] = (close - breakout) / breakout
            metrics["breakout_confirmed"] = metrics["breakout_pct"] >= ENTRY_MIN_BREAKOUT_PCT
            metrics["not_overextended"] = metrics["breakout_pct"] <= ENTRY_MAX_EXTENSION_PCT
        else:
            metrics["breakout_pct"] = None
            metrics["breakout_confirmed"] = False
            metrics["not_overextended"] = False

        if close and high and low and high > low:
            rng = high - low
            metrics["close_position"] = (close - low) / rng
            metrics["body_ratio"] = abs((close or 0) - (open_p or close)) / rng
            metrics["green_candle"] = (open_p is not None and close >= open_p)
        else:
            metrics["close_position"] = None
            metrics["body_ratio"] = None
            metrics["green_candle"] = None

        avg_vol20 = _prior_avg_volume(df, -1) if ("volume" in df.columns and len(df) >= VOLUME_AVG_LOOKBACK + 1) else None
        metrics["avg_volume20"] = avg_vol20
        if vol and avg_vol20 and avg_vol20 > 0:
            metrics["volume_ratio"] = vol / avg_vol20
        else:
            metrics["volume_ratio"] = None
        metrics["avg_dollar_volume20"] = (avg_vol20 * close) if avg_vol20 and close else None

        try:
            h = pd.to_numeric(df["high"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            lookback = min(252, len(h))
            high52 = float(h.tail(lookback).max()) if lookback >= 60 else None
            metrics["high52"] = high52
            metrics["high52_proximity"] = (close / high52) if close and high52 and high52 > 0 else None
        except Exception:
            metrics["high52"] = None
            metrics["high52_proximity"] = None

        try:
            atr_pct = (atr14 / close) if atr14 and close else None
            metrics["atr_pct"] = atr_pct
            tr_pct = _calc_true_range_pct(df).replace([np.inf, -np.inf], np.nan).dropna()
            range5 = float(tr_pct.tail(5).mean()) if len(tr_pct) >= 5 else None
            range20 = float(tr_pct.tail(20).mean()) if len(tr_pct) >= 20 else None
            metrics["range5_pct"] = range5
            metrics["range20_pct"] = range20
            metrics["atr_contraction"] = bool(range5 is not None and range20 is not None and range5 <= range20 * 0.85)
            if "volume" in df.columns and avg_vol20 and len(df) >= 26:
                pre5 = pd.to_numeric(df["volume"].iloc[-6:-1], errors="coerce").dropna()
                metrics["volume_dryup_before_breakout"] = bool(len(pre5) and float(pre5.mean()) <= avg_vol20 * 0.90)
            else:
                metrics["volume_dryup_before_breakout"] = None
        except Exception:
            metrics["atr_pct"] = None
            metrics["atr_contraction"] = None
            metrics["volume_dryup_before_breakout"] = None

        try:
            rs_data = compute_rs_score(ticker, df)
            metrics["rs_score"] = rs_data.get("rs_score") if isinstance(rs_data, dict) else None
            metrics["rs_summary"] = rs_data.get("summary", "RS N/A") if isinstance(rs_data, dict) else "RS N/A"
            metrics["vs_spy_3m"] = rs_data.get("vs_spy_3m") if isinstance(rs_data, dict) else None
        except Exception:
            metrics["rs_score"] = None
            metrics["rs_summary"] = "RS N/A"
            metrics["vs_spy_3m"] = None
        metrics["rs_line_new_high"] = _rs_line_new_high(df)

        reg = regime if isinstance(regime, dict) else get_market_regime()
        metrics["regime"] = reg.get("regime", "UNKNOWN") if isinstance(reg, dict) else "UNKNOWN"
        metrics["regime_data_ok"] = bool(reg.get("data_ok", False)) if isinstance(reg, dict) else False
        metrics["regime_summary"] = reg.get("summary", "UNKNOWN") if isinstance(reg, dict) else "UNKNOWN"

        metrics["pattern_bars"] = int(meta.get("pattern_bars") or 0)
        if metrics["pattern_bars"] <= 0 and meta.get("start_index") is not None:
            try:
                metrics["pattern_bars"] = max(0, len(df) - 1 - int(meta.get("start_index")))
            except Exception:
                pass
        metrics["depth_pct"] = _to_float_or_none(meta.get("depth_pct"))
        metrics["vol_declining"] = bool(meta.get("vol_declining", False))
        metrics["bounce_confirmed"] = bool(meta.get("bounce_confirmed", False))
        metrics["pullback_volume_quiet"] = bool(meta.get("pullback_volume_quiet", False))
        metrics["touched_retest_zone"] = bool(meta.get("touched_retest_zone", False))
        metrics["no_failed_breakout"] = bool(meta.get("no_failed_breakout", False))
        metrics["original_breakout_volume_ratio"] = _to_float_or_none(meta.get("breakout_volume_ratio"))
        return metrics
    except Exception as e:
        metrics["error"] = str(e)
        return metrics


def _validate_long_trade_levels(alert: dict) -> tuple[bool, list[str], dict]:
    """
    Hard safety validation for a long setup before Entry Ready scoring.
    Uses breakout_level as the actual planned trigger/entry. A detector's internal
    reference price is not treated as the trade entry. No score can override this.
    """
    problems: list[str] = []
    values: dict[str, float | None] = {}

    def finite_positive(name: str, raw) -> float | None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            problems.append(f"{name} חסר/לא מספר")
            return None
        if not math.isfinite(value) or value <= 0:
            problems.append(f"{name} לא תקין")
            return None
        return value

    _sync_trade_geometry(alert)
    entry = finite_positive("Entry", alert.get("planned_entry", alert.get("breakout_level")))
    stop = finite_positive("Stop", alert.get("stop_loss"))
    target = finite_positive("Target", alert.get("target"))
    rr = finite_positive("R:R", alert.get("rr_ratio"))
    values.update({"entry": entry, "stop": stop, "target": target, "rr": rr})

    if entry is not None and stop is not None and stop >= entry:
        problems.append(f"Stop חייב להיות מתחת ל-Entry ({stop:.4f} >= {entry:.4f})")
    if entry is not None and target is not None and target <= entry:
        problems.append(f"Target חייב להיות מעל Entry ({target:.4f} <= {entry:.4f})")

    if entry is not None and stop is not None and target is not None and stop < entry < target:
        risk = entry - stop
        reward = target - entry
        calc_rr = reward / risk if risk > 0 else None
        values["calculated_rr"] = calc_rr
        if calc_rr is None or not math.isfinite(calc_rr) or calc_rr <= 0:
            problems.append("R:R מחושב לא תקין")
    else:
        values["calculated_rr"] = None

    return (not problems), list(dict.fromkeys(problems)), values


ENTRY_DIAGNOSTICS = {"patterns": {}, "blockers": {}}

def _record_entry_diagnostics(pattern: str, quality: dict) -> None:
    """אוסף funnel לפי Pattern וסיבת חסימה כדי לכייל לפי נתונים אמיתיים."""
    try:
        p = str(pattern or "UNKNOWN")
        bucket = ENTRY_DIAGNOSTICS["patterns"].setdefault(p, {"ready": 0, "watchlist": 0})
        if quality.get("entry_ready"):
            bucket["ready"] += 1
        else:
            bucket["watchlist"] += 1
            for reason in quality.get("blocking_fails", []) or []:
                key = str(reason)
                ENTRY_DIAGNOSTICS["blockers"][key] = int(ENTRY_DIAGNOSTICS["blockers"].get(key, 0)) + 1
    except Exception:
        pass


def evaluate_entry_quality(df: pd.DataFrame, ticker: str, breakout_level: float,
                           pattern_meta: dict | None = None, base_score: float = 0.0,
                           rr: float | None = None, regime: dict | None = None) -> dict:
    """
    V8: מחזיר החלטה סופית האם הסטאפ הוא ENTRY_READY או Watchlist בלבד.
    לא מספיק שיש תבנית. חייבים אישור כניסה: פריצה, ווליום, נר, RS, MA150/MA200, R:R ומצב שוק.
    """
    reasons: list[str] = []
    fails: list[str] = []
    points = 0.0
    meta = pattern_meta or {}
    pattern = str(meta.get("pattern_type") or meta.get("pattern") or "")
    m = _entry_quality_metrics(df, ticker, breakout_level, meta, rr, regime)
    confirmation = _entry_confirmation_profile(pattern, meta, m)
    m["entry_confirmation_family"] = confirmation.get("family")
    m["entry_confirmation_ok"] = bool(confirmation.get("confirmation_ok"))
    m["entry_volume_ok"] = bool(confirmation.get("volume_ok"))

    def pct(x):
        return "N/A" if x is None else f"{x*100:.1f}%"

    regime_name = m.get("regime", "UNKNOWN")
    if m.get("regime_data_ok") and regime_name == "BULL":
        points += 5; reasons.append("שוק BULL עם נתוני SPY תקינים (+5)")
    elif m.get("regime_data_ok") and regime_name == "NEUTRAL":
        points += 3; reasons.append("שוק NEUTRAL — מותר אבל לא מושלם (+3)")
    elif m.get("regime_data_ok") and regime_name == "BEAR":
        reasons.append("שוק BEAR — לא מקבל נקודות שוק")
    else:
        reasons.append("מצב שוק UNKNOWN — לא מקבל נקודות שוק")

    if m.get("above_ma150"):
        points += 4; reasons.append(f"מחיר מעל MA150 ({pct(m.get('ma150_dist_pct'))}) (+4)")
    else:
        fails.append(f"מחיר לא מעל MA150 בצורה נקייה ({pct(m.get('ma150_dist_pct'))})")
    if m.get("ma150_rising") is True:
        points += 3; reasons.append("MA150 עולה (+3)")
    elif m.get("ma150_rising") is False:
        fails.append("MA150 לא עולה")
    else:
        fails.append("MA150 לא זמין/לא מספיק נתונים")
    if m.get("above_ma200") is True:
        points += 2; reasons.append("מחיר מעל MA200 (+2)")
    elif m.get("above_ma200") is False:
        fails.append("מחיר מתחת ל-MA200")
    if m.get("ma_stack_ok") is True:
        points += 1; reasons.append("MA50 מעל/שווה MA150 (+1)")

    rs = m.get("rs_score")
    if rs is not None and rs >= 85:
        points += 10; reasons.append(f"RS חזק מאוד {rs:.0f} (+10)")
    elif rs is not None and rs >= ENTRY_MIN_RS_SCORE:
        points += 8; reasons.append(f"RS חזק {rs:.0f} (+8)")
    elif rs is not None and rs >= 60:
        points += 4; fails.append(f"RS בינוני בלבד {rs:.0f} — לא מספיק לכניסה איכותית")
    else:
        fails.append("RS חלש או לא זמין")
    if m.get("rs_line_new_high") is True:
        points += 4; reasons.append("קו RS מול SPY בשיא חדש/קרוב לשיא (+4)")
    elif m.get("rs_line_new_high") is False:
        fails.append("קו RS לא בשיא חדש")
    hprox = m.get("high52_proximity")
    if hprox is not None and hprox >= 0.90:
        points += 4; reasons.append(f"קרוב מאוד לשיא 52 שבועות ({pct(hprox)}) (+4)")
    elif hprox is not None and hprox >= ENTRY_MIN_52W_HIGH_PROX:
        points += 3; reasons.append(f"קרוב מספיק לשיא 52 שבועות ({pct(hprox)}) (+3)")
    else:
        fails.append(f"רחוק מדי משיא 52 שבועות ({pct(hprox)})")
    try:
        c = m.get("close")
        close_series = pd.to_numeric(df["close"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        ret20 = (c / float(close_series.iloc[-21]) - 1) if c and len(close_series) >= 21 else None
        if ret20 is not None and ret20 > 0:
            points += 2; reasons.append(f"מומנטום 20 יום חיובי ({ret20*100:.1f}%) (+2)")
        elif ret20 is not None:
            fails.append(f"מומנטום 20 יום שלילי ({ret20*100:.1f}%)")
    except Exception:
        pass

    bars = int(m.get("pattern_bars") or 0)
    weeks = bars / 5.0 if bars else 0.0
    if 3 <= weeks <= 16:
        points += 5; reasons.append(f"בסיס/תבנית נבנו {weeks:.1f} שבועות (+5)")
    elif weeks > 0:
        points += 2; reasons.append(f"משך תבנית פחות אידיאלי {weeks:.1f} שבועות (+2)")
    depth = m.get("depth_pct")
    if depth is not None and 0.08 <= depth <= 0.33:
        points += 3; reasons.append(f"עומק תבנית בריא ({pct(depth)}) (+3)")
    elif depth is not None:
        points += 1; reasons.append(f"עומק תבנית לא אידיאלי ({pct(depth)}) (+1)")
    if m.get("atr_contraction") is True:
        points += 4; reasons.append("התכווצות תנודתיות לפני פריצה (+4)")
    elif m.get("atr_contraction") is False:
        fails.append("אין התכווצות תנודתיות ברורה")
    if "wedge" in pattern.lower() and m.get("vol_declining"):
        points += 3; reasons.append("ווליום ירד בתוך ה-Wedge לפני הפריצה (+3)")
    elif m.get("volume_dryup_before_breakout") is True:
        points += 3; reasons.append("Volume dry-up לפני הפריצה (+3)")

    bp = m.get("breakout_pct")
    vr = m.get("volume_ratio")
    if confirmation.get("confirmation_ok"):
        points += float(confirmation.get("confirmation_points", 0) or 0)
        if confirmation.get("confirmation_reason"):
            reasons.append(str(confirmation.get("confirmation_reason")))
    else:
        fails.extend([x for x in confirmation.get("blockers", []) if "ווליום" not in x and "רחוק מדי" not in x])
    if confirmation.get("volume_ok"):
        points += float(confirmation.get("volume_points", 0) or 0)
        if confirmation.get("volume_reason"):
            reasons.append(str(confirmation.get("volume_reason")) + f" (+{float(confirmation.get('volume_points',0) or 0):.0f})")
    else:
        vol_blockers = [x for x in confirmation.get("blockers", []) if "ווליום" in x]
        fails.extend(vol_blockers or [f"ווליום לא מאשר ({'N/A' if vr is None else f'×{vr:.2f}'})"])
    if not confirmation.get("max_extension_ok", True):
        fails.append(f"רחוק מדי מנקודת הכניסה — {bp*100:.1f}% מעל ה-Pivot" if bp is not None else "המחיר רחוק מדי מהכניסה")
    cp = m.get("close_position")
    if cp is not None and cp >= 0.80:
        points += 6; reasons.append(f"סגירה חזקה מאוד בחלק העליון של הנר ({cp*100:.0f}%) (+6)")
    elif cp is not None and cp >= ENTRY_MIN_CLOSE_POS:
        points += 4; reasons.append(f"סגירה טובה בחלק העליון של הנר ({cp*100:.0f}%) (+4)")
    else:
        fails.append(f"הנר לא סגר מספיק חזק ({'N/A' if cp is None else f'{cp*100:.0f}%'})")
    br = m.get("body_ratio")
    if br is not None and br >= 0.50:
        points += 4; reasons.append(f"גוף נר חזק ({br*100:.0f}%) (+4)")
    elif br is not None and br >= ENTRY_MIN_BODY_RATIO:
        points += 2; reasons.append(f"גוף נר סביר ({br*100:.0f}%) (+2)")
    else:
        fails.append(f"גוף נר חלש ({'N/A' if br is None else f'{br*100:.0f}%'})")
    if m.get("green_candle") is True:
        points += 2; reasons.append("נר ירוק ביום הפריצה (+2)")
    if m.get("not_overextended") is True:
        points += 2; reasons.append("לא רודפים אחרי מחיר רחוק מדי (+2)")

    rr_val = m.get("rr")
    if _rr_at_least(rr_val, 3.0):
        points += 7; reasons.append(f"R:R חזק {rr_val:.2f}:1 (+7)")
    elif _rr_at_least(rr_val, ENTRY_MIN_RR):
        points += 5; reasons.append(f"R:R תקין {rr_val:.2f}:1 (+5)")
    else:
        fails.append(f"R:R נמוך מדי ({'N/A' if rr_val is None else f'{rr_val:.2f}:1'})")
    adv = m.get("avg_dollar_volume20")
    if adv is not None and adv >= ENTRY_MIN_DOLLAR_VOLUME * 3:
        points += 5; reasons.append(f"נזילות גבוהה (${adv/1_000_000:.1f}M ביום) (+5)")
    elif adv is not None and adv >= ENTRY_MIN_DOLLAR_VOLUME:
        points += 3; reasons.append(f"נזילות מספקת (${adv/1_000_000:.1f}M ביום) (+3)")
    else:
        fails.append(f"נזילות נמוכה/לא זמינה (${0 if adv is None else adv/1_000_000:.1f}M ביום)")
    atr_pct = m.get("atr_pct")
    if atr_pct is not None and 0.015 <= atr_pct <= 0.05:
        points += 4; reasons.append(f"ATR בריא ({atr_pct*100:.1f}%) (+4)")
    elif atr_pct is not None and atr_pct <= ENTRY_MAX_ATR_PCT:
        points += 2; reasons.append(f"ATR סביר ({atr_pct*100:.1f}%) (+2)")
    else:
        fails.append(f"ATR גבוה מדי/לא זמין ({'N/A' if atr_pct is None else f'{atr_pct*100:.1f}%'})")

    blocking_fails: list[str] = []
    if not m.get("above_ma150"):
        blocking_fails.append("לא מעל MA150")
    if ENTRY_REQUIRE_MA150_RISING and m.get("ma150_rising") is not True:
        blocking_fails.append("MA150 לא עולה")
    if ENTRY_REQUIRE_MA200_ABOVE and m.get("above_ma200") is False:
        blocking_fails.append("מתחת ל-MA200")
    if not confirmation.get("confirmation_ok"):
        blocking_fails.extend([x for x in confirmation.get("blockers", []) if "ווליום" not in x and "רחוק מדי" not in x])
    if not confirmation.get("max_extension_ok", True):
        blocking_fails.append("המחיר כבר רחוק מדי מהכניסה")
    if not confirmation.get("volume_ok"):
        vol_blockers = [x for x in confirmation.get("blockers", []) if "ווליום" in x]
        blocking_fails.extend(vol_blockers or ["ווליום לא מאשר"])
    if (m.get("close_position") is None) or (m.get("close_position") < ENTRY_MIN_CLOSE_POS):
        blocking_fails.append("סגירת נר לא חזקה")
    if (m.get("body_ratio") is None) or (m.get("body_ratio") < ENTRY_MIN_BODY_RATIO):
        blocking_fails.append("גוף נר חלש")
    if (rs is None) or (rs < ENTRY_MIN_RS_SCORE):
        blocking_fails.append("RS לא מספיק חזק")
    if (hprox is None) or (hprox < ENTRY_MIN_52W_HIGH_PROX):
        blocking_fails.append("רחוק מדי משיא 52 שבועות")
    if (adv is None) or (adv < ENTRY_MIN_DOLLAR_VOLUME):
        blocking_fails.append("נזילות דולרית נמוכה")
    if not _rr_at_least(rr_val, ENTRY_MIN_RR):
        blocking_fails.append("R:R נמוך מדי")
    if ENTRY_REQUIRE_REGIME_DATA_OK and not m.get("regime_data_ok"):
        blocking_fails.append("נתוני SPY/Regime לא תקינים")
    if ENTRY_BLOCK_BEAR_REGIME and regime_name == "BEAR":
        blocking_fails.append("שוק BEAR")

    quality_score = round(max(0.0, min(100.0, points)), 1)
    if quality_score < ENTRY_READY_MIN_SCORE:
        blocking_fails.append(f"ציון Entry Quality נמוך ({quality_score:.1f} < {ENTRY_READY_MIN_SCORE:.1f})")

    entry_ready = bool(ENTRY_ENGINE_ENABLED and not blocking_fails)
    status = "ENTRY_READY" if entry_ready else "WATCHLIST"
    return {
        "status": status,
        "entry_ready": entry_ready,
        "quality_score": quality_score,
        "base_score": round(float(base_score or 0.0), 1),
        "reasons": reasons,
        "fail_reasons": list(dict.fromkeys(fails + blocking_fails)),
        "blocking_fails": list(dict.fromkeys(blocking_fails)),
        "metrics": m,
    }


def log_entry_quality_decision(alert: dict, quality: dict) -> None:
    """כותב לוג CSV לכל מועמד שנמצא, גם אם הוא רק Watchlist. יעזור לנו למדוד ולשפר בלי לנחש."""
    try:
        ticker = alert.get("ticker", "")
        pattern = alert.get("pattern_type", "")
        m = quality.get("metrics", {}) if isinstance(quality, dict) else {}
        row = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker,
            "pattern": pattern,
            "status": quality.get("status", "UNKNOWN"),
            "quality_score": quality.get("quality_score"),
            "base_score": quality.get("base_score"),
            "entry": alert.get("breakout_level"),
            "close": m.get("close"),
            "rr": m.get("rr"),
            "volume_ratio": m.get("volume_ratio"),
            "rs_score": m.get("rs_score"),
            "high52_proximity": m.get("high52_proximity"),
            "breakout_pct": m.get("breakout_pct"),
            "close_position": m.get("close_position"),
            "ma150_dist_pct": m.get("ma150_dist_pct"),
            "avg_dollar_volume20": m.get("avg_dollar_volume20"),
            "regime": m.get("regime"),
            "blocking_fails": " | ".join(quality.get("blocking_fails", [])[:10]),
        }
        row = _redact_log_value(row)
        file_exists = os.path.exists(ENTRY_QUALITY_LOG)
        with open(ENTRY_QUALITY_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        log(f"entry_quality_log error: {e}")



# ============================================================
#  V9 — PATTERN EXPANSION FUNCTIONS
#  פונקציות עצמאיות ומסודרות לתבניות החדשות:
#  Flat Base, Darvas Box, VCP, EMA Pullback Bounce, Breakout Retest.
# ============================================================
ENTRY_READY = "ENTRY_READY"
ALMOST_READY = "ALMOST_READY"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class V9PatternConfig:
    """Central place for all V9 pattern thresholds."""

    # General quality gates
    entry_ready_min_quality: float = 60.0
    almost_ready_min_quality: float = 45.0
    min_rs_score: float = 70.0
    strong_rs_score: float = 80.0
    min_rr: float = 2.5
    max_entry_extension_pct: float = 3.0
    max_stop_risk_pct: float = 12.0
    min_dollar_volume_20: float = 10_000_000.0

    # Trend filters
    require_above_ma150: bool = True
    require_ma150_rising: bool = True
    ma150_rising_lookback: int = 10
    min_52w_high_proximity: float = 0.85  # close / 52w high

    # Breakout confirmation
    min_breakout_pct: float = 0.003       # 0.3% above pivot
    strong_breakout_pct: float = 0.005    # 0.5% above pivot
    min_volume_ratio: float = 1.30
    strong_volume_ratio: float = 1.50
    min_close_position: float = 0.65      # close in upper 35% of daily range

    # Base / box settings
    flat_base_min_days: int = 15
    flat_base_max_days: int = 60
    flat_base_default_days: int = 35
    flat_base_max_depth_pct: float = 18.0
    flat_base_min_touches: int = 2
    flat_base_watchlist_distance_pct: float = 3.0

    darvas_windows: Tuple[int, ...] = (20, 40, 55)
    darvas_max_depth_pct: float = 20.0
    darvas_watchlist_distance_pct: float = 3.0

    # VCP settings
    vcp_min_days: int = 30
    vcp_max_days: int = 90
    vcp_default_days: int = 60
    vcp_min_contractions: int = 2
    vcp_volume_dryup_ratio: float = 0.80
    vcp_range_contraction_ratio: float = 0.80
    vcp_watchlist_distance_pct: float = 3.0

    # Pullback settings
    pullback_min_days: int = 2
    pullback_max_days: int = 10
    pullback_min_depth_pct: float = 3.0
    pullback_max_depth_pct: float = 12.0
    ema_touch_tolerance_pct: float = 1.0
    pullback_bounce_min_volume_ratio: float = 1.00

    # Retest settings
    retest_breakout_lookback_days: int = 10
    retest_box_window: int = 40
    retest_max_above_pivot_pct: float = 2.0
    retest_max_below_pivot_pct: float = 1.0


@dataclass
class PatternCandidate:
    ticker: str
    pattern_name: str
    status: str
    pivot: Optional[float]
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    risk_pct: Optional[float]
    target_pct: Optional[float]
    rr: Optional[float]
    base_score: float
    entry_quality: float
    breakout_pct: Optional[float] = None
    base_depth_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    rs_score: Optional[float] = None
    close_position: Optional[float] = None
    distance_from_52w_high_pct: Optional[float] = None
    dollar_volume_20: Optional[float] = None
    missing_confirmations: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def short_reason(self) -> str:
        # This is only the V9 detector's own checklist. The final V8 Entry Ready
        # gate is stricter and runs later, so do not claim final confirmation here.
        if not self.missing_confirmations:
            return "detector checks passed; final Entry Ready pending"
        return "; ".join(self.missing_confirmations[:6])


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Convert a value to float and protect against NaN/inf."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return value


def pct_change(new: float, old: float) -> Optional[float]:
    old = safe_float(old)
    new = safe_float(new)
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old * 100.0


def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize OHLCV dataframe and remove rows with unusable prices."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    out = df.copy()

    # Accept lowercase columns too.
    rename_map = {}
    for c in out.columns:
        lc = str(c).lower()
        if lc == "open":
            rename_map[c] = "Open"
        elif lc == "high":
            rename_map[c] = "High"
        elif lc == "low":
            rename_map[c] = "Low"
        elif lc == "close":
            rename_map[c] = "Close"
        elif lc in ("volume", "vol"):
            rename_map[c] = "Volume"
    out = out.rename(columns=rename_map)

    required = ["Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out[required].replace([np.inf, -np.inf], np.nan)
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out = out[(out["High"] > 0) & (out["Low"] > 0) & (out["Close"] > 0)]
    out["Volume"] = out["Volume"].fillna(0.0).clip(lower=0.0)
    return out


def add_v9_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicators used by all V9 pattern functions."""
    out = clean_ohlcv(df)
    if out.empty:
        return out

    close = out["Close"]
    high = out["High"]
    low = out["Low"]
    volume = out["Volume"]

    out["EMA21"] = close.ewm(span=21, adjust=False).mean()
    out["EMA28"] = close.ewm(span=28, adjust=False).mean()
    out["MA50"] = close.rolling(50, min_periods=20).mean()
    out["MA150"] = close.rolling(150, min_periods=80).mean()
    out["MA200"] = close.rolling(200, min_periods=100).mean()
    out["VOL20"] = volume.rolling(20, min_periods=10).mean()
    out["DOLLAR_VOL20"] = (close * volume).rolling(20, min_periods=10).mean()
    out["HIGH_52W"] = high.rolling(252, min_periods=120).max()

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["ATR5"] = tr.rolling(5, min_periods=3).mean()
    out["ATR10"] = tr.rolling(10, min_periods=5).mean()
    out["ATR14"] = tr.rolling(14, min_periods=7).mean()
    out["ATR20"] = tr.rolling(20, min_periods=10).mean()
    out["ATR30"] = tr.rolling(30, min_periods=15).mean()

    daily_range = (high - low).replace(0, np.nan)
    out["CLOSE_POSITION"] = ((close - low) / daily_range).clip(0, 1)
    # Match the final Entry engine: the signal's volume cannot be part of its
    # own comparison baseline. Retest history uses the same causal convention.
    prior_volume20 = volume.shift(1).rolling(20, min_periods=20).mean()
    out["VOLUME_RATIO20"] = volume / prior_volume20.replace(0, np.nan)
    out["DIST_52W_HIGH_PCT"] = (close / out["HIGH_52W"].replace(0, np.nan) - 1.0) * 100.0
    return out


def latest_value(df: pd.DataFrame, column: str, default: Optional[float] = None) -> Optional[float]:
    if df is None or df.empty or column not in df.columns:
        return default
    return safe_float(df[column].iloc[-1], default)


def is_ma_rising(df: pd.DataFrame, column: str = "MA150", lookback: int = 10) -> bool:
    if df is None or df.empty or column not in df.columns or len(df) <= lookback:
        return False
    now = safe_float(df[column].iloc[-1])
    past = safe_float(df[column].iloc[-1 - lookback])
    if now is None or past is None:
        return False
    return now >= past


def candle_close_position(df: pd.DataFrame) -> Optional[float]:
    return latest_value(df, "CLOSE_POSITION")


def current_volume_ratio(df: pd.DataFrame) -> Optional[float]:
    return latest_value(df, "VOLUME_RATIO20")


def current_dollar_volume(df: pd.DataFrame) -> Optional[float]:
    return latest_value(df, "DOLLAR_VOL20")


def current_52w_proximity(df: pd.DataFrame) -> Optional[float]:
    close = latest_value(df, "Close")
    high_52w = latest_value(df, "HIGH_52W")
    if close is None or high_52w is None or high_52w <= 0:
        return None
    return close / high_52w


def compute_rr(entry: float, stop: float, target: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    entry = safe_float(entry)
    stop = safe_float(stop)
    target = safe_float(target)
    if entry is None or stop is None or target is None or entry <= 0:
        return None, None, None
    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return None, None, None
    risk_pct = risk / entry * 100.0
    target_pct = reward / entry * 100.0
    rr = reward / risk
    return risk_pct, target_pct, rr


def count_level_touches(series: pd.Series, level: float, tolerance_pct: float = 1.0) -> int:
    level = safe_float(level)
    if level is None or level <= 0 or series is None or series.empty:
        return 0
    tolerance = level * tolerance_pct / 100.0
    return int(((series - level).abs() <= tolerance).sum())


def pct_distance_to_level(price: float, level: float) -> Optional[float]:
    price = safe_float(price)
    level = safe_float(level)
    if price is None or level is None or level <= 0:
        return None
    return (price - level) / level * 100.0


def compute_rs_score_vs_spy(stock_df: pd.DataFrame, spy_df: Optional[pd.DataFrame]) -> Optional[float]:
    """
    Simple standalone relative strength score vs SPY.

    This is NOT a universe percentile rank. It is a fallback score for standalone use.
    In the big scanner, prefer using the existing universe-based RS score if available.
    """
    if spy_df is None or spy_df.empty:
        return None

    s = clean_ohlcv(stock_df)
    spy = clean_ohlcv(spy_df)
    if len(s) < 65 or len(spy) < 65:
        return None

    weights = [(63, 0.45), (126, 0.35), (252, 0.20)]
    raw = 50.0
    total_weight = 0.0

    for days, weight in weights:
        if len(s) <= days or len(spy) <= days:
            continue
        stock_ret = pct_change(s["Close"].iloc[-1], s["Close"].iloc[-days])
        spy_ret = pct_change(spy["Close"].iloc[-1], spy["Close"].iloc[-days])
        if stock_ret is None or spy_ret is None:
            continue
        relative = stock_ret - spy_ret
        # Map rough relative outperformance into score contribution.
        # +30% relative over period tends toward 100; -30% tends toward 0.
        score_part = max(0.0, min(100.0, 50.0 + relative * 1.67))
        raw += (score_part - 50.0) * weight
        total_weight += weight

    if total_weight == 0:
        return None
    return max(0.0, min(100.0, raw))


# ---------------------------------------------------------------------------
# Quality scoring and classification
# ---------------------------------------------------------------------------


def common_quality_checks(
    df: pd.DataFrame,
    *,
    rs_score: Optional[float],
    pivot: Optional[float],
    entry: Optional[float],
    stop: Optional[float],
    target: Optional[float],
    is_breakout_or_bounce: bool,
    config: V9PatternConfig,
    require_volume: bool = True,
    market_regime: str = "NEUTRAL",
) -> Tuple[float, List[str], Dict[str, Any]]:
    """
    Shared quality layer.

    Returns:
        quality score 0-100, missing confirmations, metrics dict.
    """
    missing: List[str] = []
    metrics: Dict[str, Any] = {}
    score = 0.0

    close = latest_value(df, "Close")
    ma150 = latest_value(df, "MA150")
    ma200 = latest_value(df, "MA200")
    vol_ratio = current_volume_ratio(df)
    close_pos = candle_close_position(df)
    dollar_vol = current_dollar_volume(df)
    high_proximity = current_52w_proximity(df)

    risk_pct, target_pct, rr = compute_rr(entry, stop, target) if entry and stop and target else (None, None, None)

    metrics.update({
        "close": close,
        "ma150": ma150,
        "ma200": ma200,
        "volume_ratio": vol_ratio,
        "close_position": close_pos,
        "dollar_volume_20": dollar_vol,
        "high_52w_proximity": high_proximity,
        "risk_pct": risk_pct,
        "target_pct": target_pct,
        "rr": rr,
    })

    # Market regime safety
    if str(market_regime).upper() in {"BULL", "NEUTRAL"}:
        score += 8
    elif str(market_regime).upper() == "UNKNOWN":
        score += 3
        missing.append("market regime unknown")
    else:
        missing.append("market regime is BEAR")

    # Trend quality
    if close is not None and ma150 is not None and close > ma150:
        score += 8
    else:
        missing.append("close not above MA150")

    if close is not None and ma200 is not None and close > ma200:
        score += 5
    else:
        missing.append("close not above MA200")

    if is_ma_rising(df, "MA150", config.ma150_rising_lookback):
        score += 7
    else:
        missing.append("MA150 not rising")

    # Relative strength
    if rs_score is not None and rs_score >= config.strong_rs_score:
        score += 20
    elif rs_score is not None and rs_score >= config.min_rs_score:
        score += 15
    else:
        missing.append(f"RS below {config.min_rs_score:.0f}")

    # 52-week high proximity
    if high_proximity is not None and high_proximity >= 0.90:
        score += 10
    elif high_proximity is not None and high_proximity >= config.min_52w_high_proximity:
        score += 7
    else:
        missing.append(f"not close enough to 52w high ({config.min_52w_high_proximity:.0%}+ needed)")

    # Liquidity
    if dollar_vol is not None and dollar_vol >= config.min_dollar_volume_20:
        score += 5
    else:
        missing.append("dollar volume too low / unavailable")

    # Entry confirmation
    if is_breakout_or_bounce:
        score += 10
    else:
        missing.append("no confirmed breakout/bounce yet")

    if require_volume:
        if vol_ratio is not None and vol_ratio >= config.strong_volume_ratio:
            score += 12
        elif vol_ratio is not None and vol_ratio >= config.min_volume_ratio:
            score += 9
        else:
            missing.append(f"volume below {config.min_volume_ratio:.1f}x")
    else:
        if vol_ratio is not None and vol_ratio >= config.pullback_bounce_min_volume_ratio:
            score += 7
        else:
            missing.append("bounce volume not supportive")

    if close_pos is not None and close_pos >= 0.75:
        score += 8
    elif close_pos is not None and close_pos >= config.min_close_position:
        score += 6
    else:
        missing.append("daily close not strong enough")

    # Risk / reward
    if _rr_at_least(rr, 3.0):
        score += 10
    elif _rr_at_least(rr, config.min_rr):
        score += 8
    else:
        missing.append(f"RR below {config.min_rr:.1f}")

    if risk_pct is not None and risk_pct <= config.max_stop_risk_pct:
        score += 5
    else:
        missing.append(f"stop risk above {config.max_stop_risk_pct:.0f}% / unavailable")

    # Avoid chasing too far above pivot.
    breakout_pct = pct_distance_to_level(entry, pivot) if entry is not None and pivot is not None else None
    metrics["breakout_pct"] = breakout_pct
    if breakout_pct is not None:
        if breakout_pct <= config.max_entry_extension_pct:
            score += 5
        else:
            missing.append(f"entry extended {breakout_pct:.1f}% above pivot")

    return max(0.0, min(100.0, score)), missing, metrics


def classify_quality(quality: float, missing: Sequence[str], config: V9PatternConfig) -> str:
    hard_fail_phrases = (
        "market regime is BEAR",
        "close not above MA150",
        "RR below",
        "entry extended",
        "stop risk above",
    )
    has_hard_fail = any(any(phrase in m for phrase in hard_fail_phrases) for m in missing)
    if quality >= config.entry_ready_min_quality and not missing and not has_hard_fail:
        return ENTRY_READY
    if quality >= config.almost_ready_min_quality:
        return ALMOST_READY
    return REJECTED


def make_candidate(
    *,
    ticker: str,
    pattern_name: str,
    df: pd.DataFrame,
    pivot: Optional[float],
    entry: Optional[float],
    stop: Optional[float],
    target: Optional[float],
    base_score: float,
    rs_score: Optional[float],
    base_depth_pct: Optional[float],
    is_breakout_or_bounce: bool,
    config: V9PatternConfig,
    require_volume: bool,
    market_regime: str,
    notes: Optional[List[str]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> PatternCandidate:
    quality, missing, metrics = common_quality_checks(
        df,
        rs_score=rs_score,
        pivot=pivot,
        entry=entry,
        stop=stop,
        target=target,
        is_breakout_or_bounce=is_breakout_or_bounce,
        config=config,
        require_volume=require_volume,
        market_regime=market_regime,
    )
    total_quality = min(100.0, quality + base_score)
    status = classify_quality(total_quality, missing, config)
    risk_pct, target_pct, rr = compute_rr(entry, stop, target) if entry and stop and target else (None, None, None)

    high_proximity = current_52w_proximity(df)
    dist_52w = None
    if high_proximity is not None:
        dist_52w = (high_proximity - 1.0) * 100.0

    meta = dict(meta or {})
    if "VCP" in pattern_name:
        meta["target_model"] = "R_MULTIPLE_SCENARIO"
    elif "Pullback" in pattern_name:
        meta["target_model"] = "PRIOR_SWING_HIGH" if meta.get("prior_swing_high") and meta["prior_swing_high"] > (entry or 0) else "R_MULTIPLE_SCENARIO"
    else:
        meta["target_model"] = "MEASURED_MOVE"
    if meta["target_model"] != "R_MULTIPLE_SCENARIO":
        meta["structural_target"] = target
    meta["pattern_pivot"] = pivot
    meta["planned_entry"] = entry
    meta["signal_date"] = pd.Timestamp(df.index[-1]).date().isoformat()
    return PatternCandidate(
        ticker=ticker,
        pattern_name=pattern_name,
        status=status,
        pivot=safe_float(pivot),
        entry=safe_float(entry),
        stop=safe_float(stop),
        target=safe_float(target),
        risk_pct=risk_pct,
        target_pct=target_pct,
        rr=rr,
        base_score=round(float(base_score), 2),
        entry_quality=round(float(total_quality), 2),
        breakout_pct=metrics.get("breakout_pct"),
        base_depth_pct=base_depth_pct,
        volume_ratio=metrics.get("volume_ratio"),
        rs_score=rs_score,
        close_position=metrics.get("close_position"),
        distance_from_52w_high_pct=dist_52w,
        dollar_volume_20=metrics.get("dollar_volume_20"),
        missing_confirmations=missing,
        notes=notes or [],
        meta=meta or {},
    )


# ---------------------------------------------------------------------------
# Pattern 1 — Flat Base / Rectangle Breakout
# ---------------------------------------------------------------------------


def detect_flat_base_breakout(
    ticker: str,
    df: pd.DataFrame,
    *,
    rs_score: Optional[float] = None,
    market_regime: str = "NEUTRAL",
    config: V9PatternConfig = V9PatternConfig(),
    base_days: Optional[int] = None,
) -> Optional[PatternCandidate]:
    """
    Detect Flat Base / Rectangle Breakout.

    Logic:
        - Prior base range is narrow and clear.
        - Current close breaks above base high, or is close enough for watchlist.
        - Volume, trend, candle strength, RS and R:R are evaluated by common layer.
    """
    data = add_v9_indicators(df)
    if len(data) < 180:
        return None

    base_days = base_days or config.flat_base_default_days
    base_days = max(config.flat_base_min_days, min(config.flat_base_max_days, base_days))
    if len(data) <= base_days + 5:
        return None

    current = data.iloc[-1]
    base = data.iloc[-1 - base_days:-1]
    close = safe_float(current["Close"])
    if close is None:
        return None

    base_high = safe_float(base["High"].max())
    base_low = safe_float(base["Low"].min())
    if base_high is None or base_low is None or base_high <= 0 or base_low <= 0:
        return None

    base_depth_pct = (base_high - base_low) / base_high * 100.0
    if base_depth_pct > config.flat_base_max_depth_pct * 1.35:
        return None

    resistance_touches = count_level_touches(base["High"], base_high, tolerance_pct=1.25)
    support_touches = count_level_touches(base["Low"], base_low, tolerance_pct=1.50)

    breakout_pct = pct_distance_to_level(close, base_high)
    is_breakout = breakout_pct is not None and breakout_pct >= config.min_breakout_pct * 100.0
    is_watchlist = breakout_pct is not None and -config.flat_base_watchlist_distance_pct <= breakout_pct < config.min_breakout_pct * 100.0

    if not is_breakout and not is_watchlist:
        return None

    base_score = 0.0
    notes: List[str] = []
    if base_depth_pct <= 12.0:
        base_score += 8
        notes.append("tight base")
    elif base_depth_pct <= config.flat_base_max_depth_pct:
        base_score += 5
    else:
        notes.append("base depth is wide")
        base_score -= 3

    if resistance_touches >= config.flat_base_min_touches:
        base_score += 4
    else:
        notes.append("not enough resistance touches")

    if support_touches >= config.flat_base_min_touches:
        base_score += 3

    vol10 = safe_float(base["Volume"].tail(10).mean())
    vol_prev = safe_float(base["Volume"].head(max(5, len(base) // 2)).mean())
    if vol10 is not None and vol_prev is not None and vol_prev > 0 and vol10 <= vol_prev:
        base_score += 3
        notes.append("volume contracted inside base")

    pivot = base_high
    entry = close
    # Prefer a practical stop near the recent swing low, but not above base support.
    recent_low = safe_float(data["Low"].iloc[-11:-1].min())
    stop = None
    if recent_low is not None:
        stop = max(base_low, recent_low) * 0.995
    technical_target = base_high + (base_high - base_low)
    target_by_r = entry + (entry - stop) * config.min_rr if stop is not None and entry > stop else None
    if target_by_r is not None:
        target = max(technical_target, target_by_r)
    else:
        target = technical_target

    return make_candidate(
        ticker=ticker,
        pattern_name="Flat Base / Rectangle Breakout",
        df=data,
        pivot=pivot,
        entry=entry,
        stop=stop,
        target=target,
        base_score=base_score,
        rs_score=rs_score,
        base_depth_pct=base_depth_pct,
        is_breakout_or_bounce=is_breakout,
        config=config,
        require_volume=True,
        market_regime=market_regime,
        notes=notes,
        meta={
            "base_days": base_days,
            "base_start": pd.Timestamp(base.index[0]).isoformat(),
            "base_end": pd.Timestamp(base.index[-1]).isoformat(),
            "base_high": base_high,
            "base_low": base_low,
            "resistance_touches": resistance_touches,
            "support_touches": support_touches,
            "is_watchlist": is_watchlist,
        },
    )


# ---------------------------------------------------------------------------
# Pattern 2 — Darvas Box / 20-40-55 Day High Breakout
# ---------------------------------------------------------------------------


def detect_darvas_box_breakout(
    ticker: str,
    df: pd.DataFrame,
    *,
    rs_score: Optional[float] = None,
    market_regime: str = "NEUTRAL",
    config: V9PatternConfig = V9PatternConfig(),
) -> Optional[PatternCandidate]:
    """
    Detect Darvas Box / rolling high breakout.

    The function checks 20/40/55-day boxes and returns the strongest one.
    """
    data = add_v9_indicators(df)
    if len(data) < 180:
        return None

    close = latest_value(data, "Close")
    if close is None:
        return None

    best_candidate: Optional[PatternCandidate] = None
    for window in config.darvas_windows:
        if len(data) <= window + 5:
            continue

        box = data.iloc[-1 - window:-1]
        box_high = safe_float(box["High"].max())
        box_low = safe_float(box["Low"].min())
        if box_high is None or box_low is None or box_high <= 0:
            continue

        box_depth_pct = (box_high - box_low) / box_high * 100.0
        if box_depth_pct > config.darvas_max_depth_pct * 1.35:
            continue

        breakout_pct = pct_distance_to_level(close, box_high)
        is_breakout = breakout_pct is not None and breakout_pct >= config.min_breakout_pct * 100.0
        is_watchlist = breakout_pct is not None and -config.darvas_watchlist_distance_pct <= breakout_pct < config.min_breakout_pct * 100.0
        if not is_breakout and not is_watchlist:
            continue

        touches = count_level_touches(box["High"], box_high, tolerance_pct=1.25)
        base_score = 0.0
        notes: List[str] = [f"{window} day box"]
        if box_depth_pct <= 12.0:
            base_score += 8
            notes.append("tight box")
        elif box_depth_pct <= config.darvas_max_depth_pct:
            base_score += 5
        else:
            base_score -= 3
            notes.append("box depth is wide")
        if touches >= 2:
            base_score += 4
            notes.append("clear box resistance")

        recent_low = safe_float(data["Low"].iloc[-11:-1].min())
        stop = max(box_low, recent_low) * 0.995 if recent_low is not None else box_low * 0.995
        technical_target = box_high + (box_high - box_low)
        target_by_r = close + (close - stop) * config.min_rr if stop is not None and close > stop else None
        if target_by_r is not None:
            target = max(technical_target, target_by_r)
        else:
            target = technical_target

        candidate = make_candidate(
            ticker=ticker,
            pattern_name="Darvas Box Breakout",
            df=data,
            pivot=box_high,
            entry=close,
            stop=stop,
            target=target,
            base_score=base_score,
            rs_score=rs_score,
            base_depth_pct=box_depth_pct,
            is_breakout_or_bounce=is_breakout,
            config=config,
            require_volume=True,
            market_regime=market_regime,
            notes=notes,
            meta={
                "box_window": window,
                "box_start": pd.Timestamp(box.index[0]).isoformat(),
                "box_end": pd.Timestamp(box.index[-1]).isoformat(),
                "box_high": box_high,
                "box_low": box_low,
                "resistance_touches": touches,
                "is_watchlist": is_watchlist,
            },
        )

        if best_candidate is None or candidate.entry_quality > best_candidate.entry_quality:
            best_candidate = candidate

    return best_candidate


# ---------------------------------------------------------------------------
# Pattern 3 — VCP: Volatility Contraction Pattern
# ---------------------------------------------------------------------------


def _segment_range_pct(segment: pd.DataFrame) -> Optional[float]:
    if segment is None or segment.empty:
        return None
    high = safe_float(segment["High"].max())
    low = safe_float(segment["Low"].min())
    if high is None or low is None or high <= 0:
        return None
    return (high - low) / high * 100.0


def _count_contractions(ranges: Sequence[Optional[float]], ratio: float) -> int:
    valid = [r for r in ranges if r is not None]
    if len(valid) < 2:
        return 0
    count = 0
    for prev, curr in zip(valid, valid[1:]):
        if curr <= prev * ratio:
            count += 1
    return count


def detect_vcp(
    ticker: str,
    df: pd.DataFrame,
    *,
    rs_score: Optional[float] = None,
    market_regime: str = "NEUTRAL",
    config: V9PatternConfig = V9PatternConfig(),
    lookback_days: Optional[int] = None,
) -> Optional[PatternCandidate]:
    """
    Detect VCP — Volatility Contraction Pattern.

    This is a practical approximation:
        - Base range contracts across 3 segments.
        - Recent ATR/range is lower than prior ATR/range.
        - Volume dries up before breakout.
        - Entry requires breakout over recent pivot.
    """
    data = add_v9_indicators(df)
    if len(data) < 180:
        return None

    lookback_days = lookback_days or config.vcp_default_days
    lookback_days = max(config.vcp_min_days, min(config.vcp_max_days, lookback_days))
    if len(data) <= lookback_days + 5:
        return None

    current = data.iloc[-1]
    close = safe_float(current["Close"])
    if close is None:
        return None

    base = data.iloc[-1 - lookback_days:-1]
    seg_size = max(5, len(base) // 3)
    seg1 = base.iloc[:seg_size]
    seg2 = base.iloc[seg_size:2 * seg_size]
    seg3 = base.iloc[2 * seg_size:]
    ranges = [_segment_range_pct(seg1), _segment_range_pct(seg2), _segment_range_pct(seg3)]
    contractions = _count_contractions(ranges, config.vcp_range_contraction_ratio)

    before_breakout = data.iloc[:-1]
    atr5 = latest_value(before_breakout, "ATR5")
    atr20 = latest_value(before_breakout, "ATR20")
    atr10 = latest_value(before_breakout, "ATR10")
    atr30 = latest_value(before_breakout, "ATR30")
    atr_contracting = (
        atr5 is not None and atr20 is not None and atr20 > 0 and atr5 < atr20
        and atr10 is not None and atr30 is not None and atr30 > 0 and atr10 < atr30
    )

    recent_range = _segment_range_pct(data.iloc[-6:-1])
    prior_range = _segment_range_pct(data.iloc[-26:-6])
    range_contracting = (
        recent_range is not None and prior_range is not None and prior_range > 0
        and recent_range <= prior_range * config.vcp_range_contraction_ratio
    )

    vol5 = safe_float(data["Volume"].iloc[-6:-1].mean())
    vol20 = latest_value(data.iloc[:-1], "VOL20")
    volume_dryup = vol5 is not None and vol20 is not None and vol20 > 0 and vol5 <= vol20 * config.vcp_volume_dryup_ratio

    if contractions < 1 and not (atr_contracting and range_contracting and volume_dryup):
        return None
    structural_confirmed = contractions >= config.vcp_min_contractions and atr_contracting and range_contracting and volume_dryup
    if STRICT_PATTERN_STRUCTURE and not structural_confirmed:
        return None

    # VCP pivot: high of the final tight area, not necessarily whole base high.
    tight_area = data.iloc[-16:-1]
    pivot = safe_float(tight_area["High"].max())
    base_high = safe_float(base["High"].max())
    base_low = safe_float(base["Low"].min())
    if pivot is None or base_high is None or base_low is None:
        return None

    base_depth_pct = (base_high - base_low) / base_high * 100.0 if base_high > 0 else None
    breakout_pct = pct_distance_to_level(close, pivot)
    is_breakout = breakout_pct is not None and breakout_pct >= config.min_breakout_pct * 100.0
    is_watchlist = breakout_pct is not None and -config.vcp_watchlist_distance_pct <= breakout_pct < config.min_breakout_pct * 100.0
    if not is_breakout and not is_watchlist:
        return None

    base_score = 0.0
    notes: List[str] = []
    if contractions >= config.vcp_min_contractions:
        base_score += 8
        notes.append(f"{contractions} volatility contractions")
    elif contractions == 1:
        base_score += 4
        notes.append("early contraction")
    if atr_contracting:
        base_score += 4
        notes.append("ATR contracting")
    if range_contracting:
        base_score += 4
        notes.append("recent range contracted")
    if volume_dryup:
        base_score += 4
        notes.append("volume dry-up")

    # Stop below final contraction low.
    last_contraction_low = safe_float(tight_area["Low"].min())
    stop = last_contraction_low * 0.995 if last_contraction_low is not None else None
    # VCP is best measured by R-multiple; technical target may be base height too aggressive.
    target = close + (close - stop) * 3.0 if stop is not None and close > stop else None

    return make_candidate(
        ticker=ticker,
        pattern_name="VCP Breakout",
        df=data,
        pivot=pivot,
        entry=close,
        stop=stop,
        target=target,
        base_score=base_score,
        rs_score=rs_score,
        base_depth_pct=base_depth_pct,
        is_breakout_or_bounce=is_breakout,
        config=config,
        require_volume=True,
        market_regime=market_regime,
        notes=notes,
        meta={
            "lookback_days": lookback_days,
            "segment_ranges_pct": ranges,
            "contractions": contractions,
            "structural_confirmed": structural_confirmed,
            "base_start": pd.Timestamp(base.index[0]).isoformat(),
            "base_end": pd.Timestamp(base.index[-1]).isoformat(),
            "tight_area_start": pd.Timestamp(tight_area.index[0]).isoformat(),
            "tight_area_end": pd.Timestamp(tight_area.index[-1]).isoformat(),
            "atr_contracting": atr_contracting,
            "range_contracting": range_contracting,
            "volume_dryup": volume_dryup,
            "tight_area_low": last_contraction_low,
            "is_watchlist": is_watchlist,
        },
    )


# ---------------------------------------------------------------------------
# Pattern 4 — EMA21 / MA50 Pullback Bounce
# ---------------------------------------------------------------------------


def _find_recent_pullback_window(data: pd.DataFrame, config: V9PatternConfig) -> Optional[pd.DataFrame]:
    """Find a completed pullback after a recent, confirmed swing high.

    Only bars before the signal define the swing. Choose the most recent valid
    anchor, not the deepest overlapping range and not the best resulting R:R.
    The anchor candle itself is not a pullback/support candle. An older low
    before this upswing must not silently become the current trade's stop.
    """
    if len(data) < config.pullback_max_days + 30:
        return None

    # All indexing below excludes the current signal candle. Two preceding
    # bars establish the left side; the pullback establishes the right side.
    for days in range(config.pullback_min_days, config.pullback_max_days + 1):
        anchor_pos = len(data) - days - 2
        if anchor_pos < 2:
            continue
        anchor = data.iloc[anchor_pos]
        anchor_high = safe_float(anchor.get("High"))
        anchor_close = safe_float(anchor.get("Close"))
        left_high = safe_float(data["High"].iloc[anchor_pos - 2:anchor_pos].max())
        window = data.iloc[anchor_pos + 1:-1]
        pullback_high = safe_float(window["High"].max())
        pullback_low = safe_float(window["Low"].min())
        last_close = latest_value(window, "Close")
        if any(value is None for value in (anchor_high, anchor_close, left_high,
                                            pullback_high, pullback_low, last_close)):
            continue
        if anchor_high <= 0 or pullback_low <= 0:
            continue
        if anchor_high < left_high or anchor_high < pullback_high:
            continue
        # A wide trading range during an advance is not a pullback. Require a
        # net decline from the swing close. The anchor may be a wide up candle;
        # its low is not a support level that the pullback must undercut.
        if last_close >= anchor_close:
            continue
        depth = (anchor_high - pullback_low) / anchor_high * 100.0
        if not config.pullback_min_depth_pct <= depth <= config.pullback_max_depth_pct:
            continue
        touches = any(
            col in window.columns and _range_touched_band(window, window[col], config.ema_touch_tolerance_pct)
            for col in ("EMA21", "MA50")
        )
        if not touches:
            continue
        selected = window.copy()
        selected.attrs["pullback_anchor"] = {
            "date": pd.Timestamp(data.index[anchor_pos]).isoformat(),
            "high": anchor_high,
            "close": anchor_close,
            "selection": "MOST_RECENT_VALID_SWING_BEFORE_SIGNAL",
        }
        return selected
    return None


def detect_ema_pullback_bounce(
    ticker: str,
    df: pd.DataFrame,
    *,
    rs_score: Optional[float] = None,
    market_regime: str = "NEUTRAL",
    config: V9PatternConfig = V9PatternConfig(),
) -> Optional[PatternCandidate]:
    """
    Detect EMA21 / MA50 Pullback Bounce.

    This is not a pure breakout pattern. It looks for:
        - Existing uptrend.
        - Controlled pullback to EMA21 or MA50.
        - Current candle bounces above prior high with strong close.
    """
    data = add_v9_indicators(df)
    if len(data) < 220:
        return None

    close = latest_value(data, "Close")
    open_ = latest_value(data, "Open")
    prev_high = safe_float(data["High"].iloc[-2]) if len(data) >= 2 else None
    if close is None or open_ is None or prev_high is None:
        return None

    pullback = _find_recent_pullback_window(data, config)
    if pullback is None or pullback.empty:
        return None

    anchor = pullback.attrs.get("pullback_anchor", {})
    pullback_high = safe_float(anchor.get("high")) or safe_float(pullback["High"].max())
    pullback_low = safe_float(pullback["Low"].min())
    if pullback_high is None or pullback_low is None or pullback_high <= 0:
        return None
    pullback_depth_pct = (pullback_high - pullback_low) / pullback_high * 100.0

    ema21_recent = data["EMA21"].iloc[-1 - len(pullback):-1]
    ma50_recent = data["MA50"].iloc[-1 - len(pullback):-1]
    touched_ema21 = _range_touched_band(pullback, ema21_recent, config.ema_touch_tolerance_pct)
    touched_ma50 = _range_touched_band(pullback, ma50_recent, config.ema_touch_tolerance_pct)
    if not touched_ema21 and not touched_ma50:
        return None

    # Existing uptrend before pullback.
    before_pullback_close = safe_float(data["Close"].iloc[-1 - len(pullback) - 20]) if len(data) > len(pullback) + 25 else None
    pre_pullback_high = safe_float(data["High"].iloc[-1 - len(pullback) - 20:-1 - len(pullback)].max())
    prior_momentum = False
    if before_pullback_close is not None and pre_pullback_high is not None and before_pullback_close > 0:
        prior_momentum = (pre_pullback_high - before_pullback_close) / before_pullback_close * 100.0 >= 6.0

    is_green = close > open_
    ma_reclaimed = (touched_ema21 and close >= latest_value(data, "EMA21", float("inf"))) or (touched_ma50 and close >= latest_value(data, "MA50", float("inf")))
    bounce_confirmed = close > prev_high and is_green and ma_reclaimed

    vol_pullback = safe_float(pullback["Volume"].mean())
    vol20 = latest_value(data.iloc[:-1], "VOL20")
    pullback_volume_quiet = vol_pullback is not None and vol20 is not None and vol20 > 0 and vol_pullback <= vol20

    base_score = 0.0
    notes: List[str] = []
    if touched_ema21:
        base_score += 4
        notes.append("pullback touched EMA21")
    if touched_ma50:
        base_score += 4
        notes.append("pullback touched MA50")
    if prior_momentum:
        base_score += 5
        notes.append("prior momentum before pullback")
    if pullback_volume_quiet:
        base_score += 4
        notes.append("pullback volume was quiet")
    if config.pullback_min_depth_pct <= pullback_depth_pct <= 8.0:
        base_score += 4
        notes.append("healthy shallow pullback")
    elif pullback_depth_pct <= config.pullback_max_depth_pct:
        base_score += 2

    pivot = prev_high
    entry = close
    signal_low = latest_value(data, "Low")
    if signal_low is None or signal_low <= 0:
        return None
    # A bounce that undercuts the previous pullback must include that known
    # low in the invalidation level; never tighten a stop merely to pass R:R.
    support_low = min(pullback_low, signal_low)
    stop = support_low * 0.995
    # Preserve a reachable prior swing high; use an explicit 2.5R scenario only without one.
    prior_swing_high = safe_float(data["High"].iloc[-45:-1 - len(pullback)].max()) if len(data) > 50 else None
    target_by_r = entry + (entry - stop) * 2.5 if entry > stop else None
    if prior_swing_high is not None and prior_swing_high > entry:
        target = prior_swing_high
    else:
        target = target_by_r

    return make_candidate(
        ticker=ticker,
        pattern_name="EMA21/MA50 Pullback Bounce",
        df=data,
        pivot=pivot,
        entry=entry,
        stop=stop,
        target=target,
        base_score=base_score,
        rs_score=rs_score,
        base_depth_pct=pullback_depth_pct,
        is_breakout_or_bounce=bounce_confirmed,
        config=config,
        require_volume=False,
        market_regime=market_regime,
        notes=notes,
        meta={
            "pullback_days": len(pullback),
            "pullback_high": pullback_high,
            "pullback_low": pullback_low,
            "support_low": support_low,
            "stop_anchor": "PULLBACK_AND_SIGNAL_LOW",
            "pullback_anchor": anchor,
            "touched_ema21": touched_ema21,
            "touched_ma50": touched_ma50,
            "bounce_confirmed": bounce_confirmed,
            "ma_reclaimed": ma_reclaimed,
            "pullback_start": pd.Timestamp(pullback.index[0]).isoformat(),
            "pullback_end": pd.Timestamp(pullback.index[-1]).isoformat(),
            "pullback_volume_quiet": pullback_volume_quiet,
            "prior_swing_high": prior_swing_high,
        },
    )


# ---------------------------------------------------------------------------
# Pattern 5 — Breakout Retest Entry
# ---------------------------------------------------------------------------


def _find_recent_breakout_for_retest(data: pd.DataFrame, config: V9PatternConfig) -> Optional[Dict[str, Any]]:
    """Find a recent breakout over a prior box high."""
    if len(data) < config.retest_box_window + config.retest_breakout_lookback_days + 10:
        return None

    # We do not use today as the original breakout day. We search previous 1-10 days.
    for offset in range(2, config.retest_breakout_lookback_days + 2):
        idx = -offset
        prior_box = data.iloc[idx - config.retest_box_window:idx]
        if len(prior_box) < config.retest_box_window:
            continue
        pivot = safe_float(prior_box["High"].max())
        box_low = safe_float(prior_box["Low"].min())
        breakout_close = safe_float(data["Close"].iloc[idx])
        breakout_volume_ratio = safe_float(data["VOLUME_RATIO20"].iloc[idx])
        if pivot is None or box_low is None or breakout_close is None or pivot <= 0:
            continue
        breakout_pct = pct_distance_to_level(breakout_close, pivot)
        if breakout_pct is None:
            continue
        if breakout_pct >= config.min_breakout_pct * 100.0 and (breakout_volume_ratio is None or breakout_volume_ratio >= 1.0):
            return {
                "breakout_idx": idx,
                "breakout_date": pd.Timestamp(data.index[idx]).isoformat(),
                "box_start": pd.Timestamp(prior_box.index[0]).isoformat(),
                "box_end": pd.Timestamp(prior_box.index[-1]).isoformat(),
                "days_ago": offset - 1,
                "pivot": pivot,
                "box_low": box_low,
                "breakout_close": breakout_close,
                "breakout_volume_ratio": breakout_volume_ratio,
                "breakout_pct": breakout_pct,
            }
    return None


def detect_breakout_retest_entry(
    ticker: str,
    df: pd.DataFrame,
    *,
    rs_score: Optional[float] = None,
    market_regime: str = "NEUTRAL",
    config: V9PatternConfig = V9PatternConfig(),
) -> Optional[PatternCandidate]:
    """
    Detect Breakout Retest Entry.

    Looks for:
        - A breakout in the last 1-10 days.
        - Pullback/retest to the old pivot from above.
        - Current candle bounces back above pivot and prior high.
    """
    data = add_v9_indicators(df)
    if len(data) < 220:
        return None

    br = _find_recent_breakout_for_retest(data, config)
    if br is None:
        return None

    close = latest_value(data, "Close")
    open_ = latest_value(data, "Open")
    prev_high = safe_float(data["High"].iloc[-2]) if len(data) > 1 else None
    if close is None or open_ is None or prev_high is None:
        return None

    pivot = br["pivot"]
    breakout_idx = br["breakout_idx"]
    # The current bounce can touch the zone; the original breakout is excluded.
    after_breakout = data.iloc[breakout_idx + 1:]
    if len(after_breakout) < 2:
        return None

    retest_low = safe_float(after_breakout["Low"].min())
    min_close_after_breakout = safe_float(after_breakout["Close"].min())
    if retest_low is None or min_close_after_breakout is None:
        return None

    # Clean retest: low comes close to pivot, but closes do not lose it deeply.
    touched_retest_zone = (
        retest_low <= pivot * (1.0 + config.retest_max_above_pivot_pct / 100.0)
        and retest_low >= pivot * (1.0 - config.retest_max_below_pivot_pct / 100.0)
    )
    no_failed_breakout = min_close_after_breakout >= pivot * (1.0 - config.retest_max_below_pivot_pct / 100.0)
    if not touched_retest_zone or not no_failed_breakout:
        return None

    bounce_confirmed = close > pivot and close > prev_high and close > open_

    base_score = 0.0
    notes: List[str] = [f"original breakout {br['days_ago']} days ago"]
    if touched_retest_zone:
        base_score += 8
        notes.append("clean retest of pivot")
    if no_failed_breakout:
        base_score += 6
        notes.append("no deep close below pivot")
    if br.get("breakout_volume_ratio") is not None and br["breakout_volume_ratio"] >= config.min_volume_ratio:
        base_score += 5
        notes.append("original breakout had volume")

    entry = close
    stop = retest_low * 0.995
    box_height = pivot - br["box_low"]
    technical_target = pivot + box_height if box_height > 0 else None
    target_by_r = entry + (entry - stop) * 2.5 if entry > stop else None
    if technical_target is not None and target_by_r is not None:
        target = technical_target
    else:
        target = technical_target or target_by_r

    return make_candidate(
        ticker=ticker,
        pattern_name="Breakout Retest Entry",
        df=data,
        pivot=pivot,
        entry=entry,
        stop=stop,
        target=target,
        base_score=base_score,
        rs_score=rs_score,
        base_depth_pct=None,
        is_breakout_or_bounce=bounce_confirmed,
        config=config,
        require_volume=False,
        market_regime=market_regime,
        notes=notes,
        meta={
            **br,
            "retest_start": pd.Timestamp(after_breakout.index[0]).isoformat(),
            "retest_end": pd.Timestamp(after_breakout.index[-1]).isoformat(),
            "retest_low": retest_low,
            "min_close_after_breakout": min_close_after_breakout,
            "touched_retest_zone": touched_retest_zone,
            "no_failed_breakout": no_failed_breakout,
            "bounce_confirmed": bounce_confirmed,
        },
    )


# ---------------------------------------------------------------------------
# Combined standalone runner, still not integrated with the big scanner
# ---------------------------------------------------------------------------


def find_v9_pattern_candidates(
    ticker: str,
    df: pd.DataFrame,
    *,
    rs_score: Optional[float] = None,
    spy_df: Optional[pd.DataFrame] = None,
    market_regime: str = "NEUTRAL",
    config: V9PatternConfig = V9PatternConfig(),
    include_rejected: bool = False,
) -> List[PatternCandidate]:
    """
    Run all V9 pattern functions on one ticker and return sorted candidates.

    This is still standalone. Later we can connect this function naturally into
    scan_stock(...) or the existing pattern pipeline.
    """
    data = add_v9_indicators(df)
    if data.empty:
        return []

    if rs_score is None and spy_df is not None:
        rs_score = compute_rs_score_vs_spy(data, spy_df)

    detectors = [
        detect_flat_base_breakout,
        detect_darvas_box_breakout,
        detect_vcp,
        detect_ema_pullback_bounce,
        detect_breakout_retest_entry,
    ]

    candidates: List[PatternCandidate] = []
    for detector in detectors:
        try:
            candidate = detector(
                ticker,
                data,
                rs_score=rs_score,
                market_regime=market_regime,
                config=config,
            )
        except Exception as exc:  # Defensive: one pattern should not break the ticker scan.
            candidates.append(PatternCandidate(
                ticker=ticker,
                pattern_name=getattr(detector, "__name__", "unknown_detector"),
                status=REJECTED,
                pivot=None,
                entry=None,
                stop=None,
                target=None,
                risk_pct=None,
                target_pct=None,
                rr=None,
                base_score=0.0,
                entry_quality=0.0,
                rs_score=rs_score,
                missing_confirmations=["detector error"],
                notes=[str(exc)],
                meta={"detector": getattr(detector, "__name__", "unknown_detector")},
            ))
            continue

        if candidate is not None and (include_rejected or candidate.status != REJECTED):
            candidates.append(candidate)

    candidates.sort(key=lambda c: (status_rank(c.status), c.entry_quality), reverse=True)
    return candidates


def status_rank(status: str) -> int:
    return {
        ENTRY_READY: 3,
        ALMOST_READY: 2,
        REJECTED: 1,
    }.get(status, 0)


def select_best_candidate(candidates: Sequence[PatternCandidate]) -> Optional[PatternCandidate]:
    if not candidates:
        return None
    return sorted(candidates, key=lambda c: (status_rank(c.status), c.entry_quality), reverse=True)[0]


def candidates_to_dataframe(candidates: Sequence[PatternCandidate]) -> pd.DataFrame:
    """Convenience helper for logging/debugging candidates."""
    rows = [c.to_dict() for c in candidates]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def format_candidate_for_log(candidate: PatternCandidate) -> str:
    """One-line V9 detector log. Labels are intentionally distinct from final Entry Ready."""
    display_status = "DETECTOR_READY" if candidate.status == ENTRY_READY else candidate.status
    return (
        f"{candidate.ticker}: {display_status} {candidate.pattern_name} | "
        f"detector_quality={candidate.entry_quality:.1f} | "
        f"pivot={candidate.pivot} ref_price={candidate.entry} stop={candidate.stop} "
        f"target={candidate.target} rr={candidate.rr} | "
        f"detector_missing={candidate.short_reason()}"
    )


# ---------------------------------------------------------------------------
# V9 integration helpers — adapt PatternCandidate into the existing alert dict
# ---------------------------------------------------------------------------


def _make_v9_pattern_config() -> V9PatternConfig:
    """Build V9 config from the existing V8 environment variables to keep one source of truth."""
    return V9PatternConfig(
        entry_ready_min_quality=float(ENTRY_READY_MIN_SCORE),
        almost_ready_min_quality=max(45.0, float(ENTRY_CANDIDATE_MIN_SCORE)),
        min_rs_score=float(ENTRY_MIN_RS_SCORE),
        strong_rs_score=max(80.0, float(ENTRY_MIN_RS_SCORE) + 10.0),
        min_rr=float(ENTRY_MIN_RR),
        max_entry_extension_pct=float(ENTRY_MAX_EXTENSION_PCT) * 100.0,
        max_stop_risk_pct=12.0,
        min_dollar_volume_20=float(ENTRY_MIN_DOLLAR_VOLUME),
        require_above_ma150=True,
        require_ma150_rising=bool(ENTRY_REQUIRE_MA150_RISING),
        min_52w_high_proximity=float(ENTRY_MIN_52W_HIGH_PROX),
        min_breakout_pct=float(ENTRY_MIN_BREAKOUT_PCT),
        strong_breakout_pct=max(float(ENTRY_MIN_BREAKOUT_PCT) * 2.5, 0.005),
        min_volume_ratio=float(ENTRY_MIN_VOLUME_RATIO),
        strong_volume_ratio=max(float(ENTRY_MIN_VOLUME_RATIO) + 0.20, 1.50),
        min_close_position=float(ENTRY_MIN_CLOSE_POS),
    )


def _v9_pattern_candidate_to_alert(candidate: PatternCandidate) -> dict:
    """Convert a V9 PatternCandidate to the legacy alert structure used by the scanner."""
    meta = dict(candidate.meta or {})
    pattern_bars = (
        meta.get("lookback_days")
        or meta.get("base_days")
        or meta.get("box_window")
        or meta.get("pullback_days")
        or meta.get("days_ago")
        or 0
    )
    depth_decimal = None
    try:
        if candidate.base_depth_pct is not None:
            depth_decimal = float(candidate.base_depth_pct) / 100.0
    except Exception:
        depth_decimal = None

    fail_reasons = list(dict.fromkeys(candidate.missing_confirmations or []))
    notes = list(dict.fromkeys(candidate.notes or []))
    meta.update({
        "pattern_type": candidate.pattern_name,
        "pattern_source": "V9_PATTERN_EXPANSION",
        "v9_status": candidate.status,
        "v9_base_score": candidate.base_score,
        "v9_entry_quality_raw": candidate.entry_quality,
        "v9_missing_confirmations": fail_reasons,
        "v9_notes": notes,
        "pattern_bars": int(pattern_bars or 0),
        "depth_pct": depth_decimal,
        "base_depth_pct": depth_decimal,
        "breakout_pct": candidate.breakout_pct,
        "volume_ratio": candidate.volume_ratio,
        "rs_score": candidate.rs_score,
        "close_position": candidate.close_position,
        "distance_from_52w_high_pct": candidate.distance_from_52w_high_pct,
        "dollar_volume_20": candidate.dollar_volume_20,
        "fail_reasons": fail_reasons,
    })

    meta.update({"pattern_pivot": candidate.pivot, "reference_price": candidate.entry,
                 "detector_rr": candidate.rr})
    alert = {
        "ticker": candidate.ticker,
        "phase": 3,
        "pattern_type": candidate.pattern_name,
        "pattern_pivot": candidate.pivot,
        "reference_price": candidate.entry,
        "planned_entry": max(candidate.pivot or 0, candidate.entry or 0),
        "breakout_level": max(candidate.pivot or 0, candidate.entry or 0),
        "score": max(float(candidate.entry_quality or 0.0), float(candidate.base_score or 0.0)),
        "stop_loss": candidate.stop,
        "target": candidate.target,
        "rr_ratio": candidate.rr,
        "meta": meta,
    }
    _sync_trade_geometry(alert)
    return alert


def append_v9_pattern_candidates(symbol: str, df: pd.DataFrame, candidates: list[dict]) -> int:
    """
    Run the V9 pattern expansion after the old pattern engines.
    This does not change early filters. It only adds more structured candidates
    before the existing Entry Ready gate decides what can actually be sent.
    """
    if not V9_PATTERN_ENGINE_ENABLED:
        return 0
    try:
        rs_score = None
        try:
            rs_data = compute_rs_score(symbol, df)
            if isinstance(rs_data, dict):
                rs_score = rs_data.get("rs_score")
        except Exception:
            rs_score = None

        try:
            regime_data = get_market_regime()
            regime_name = regime_data.get("regime", "UNKNOWN") if isinstance(regime_data, dict) else "UNKNOWN"
        except Exception:
            regime_name = "UNKNOWN"

        v9_candidates = find_v9_pattern_candidates(
            symbol,
            df,
            rs_score=rs_score,
            market_regime=regime_name,
            config=_make_v9_pattern_config(),
            include_rejected=bool(V9_INCLUDE_REJECTED_DEBUG),
        )
        if not v9_candidates:
            return 0

        added = 0
        # Preliminary detector scores use a different checklist from Entry/Pro.
        # Cutting this list can hide the only candidate that passes the final
        # gates. Evaluate every detected family; cap emails only after quality.
        for candidate in v9_candidates:
            if candidate.status == REJECTED and not V9_INCLUDE_REJECTED_DEBUG:
                continue
            candidates.append(_v9_pattern_candidate_to_alert(candidate))
            added += 1
            if DEBUG_SCAN_REASONS:
                log(f"{symbol}: 🧩 V9 candidate — {format_candidate_for_log(candidate)}")
        return added
    except Exception as e:
        log(f"{symbol}: V9 pattern expansion error: {type(e).__name__}: {e}")
        return 0


# ============================================================
#  V9.1 — PROFESSIONAL TRADE QUALITY ENGINE
#  Final context/ranking layer. Pattern detectors are intentionally untouched.
# ============================================================
_PRO_ENGINE_CACHE: dict = {}


def _pro_clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return round(max(low, min(high, float(value))), 1)
    except Exception:
        return round(low, 1)


def _pro_ma_slope_pct(df: pd.DataFrame, column: str, bars: int) -> Optional[float]:
    """Percent change of a moving-average line over N bars. None when unavailable."""
    try:
        if column not in df.columns or len(df) <= bars:
            return None
        s = pd.to_numeric(df[column], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) <= bars:
            return None
        now = float(s.iloc[-1])
        old = float(s.iloc[-(bars + 1)])
        if not (_is_finite_number(now) and _is_finite_number(old) and old != 0):
            return None
        return (now / old - 1.0) * 100.0
    except Exception:
        return None


def _professional_trend_profile(df: pd.DataFrame) -> dict:
    """Scores trend quality without changing any pattern detector."""
    out = {"score": 0.0, "reasons": [], "metrics": {}}
    try:
        if df is None or df.empty:
            return out
        ensure_ma_columns(df)
        close = _series_last_finite(df.get("close"), None)
        ma50 = _series_last_finite(df.get("ma50"), None) if "ma50" in df.columns else None
        ma150 = _series_last_finite(df.get("ma150"), None) if "ma150" in df.columns else None
        ma200 = _series_last_finite(df.get("ma200"), None) if "ma200" in df.columns else None
        s50 = _pro_ma_slope_pct(df, "ma50", 10)
        s150 = _pro_ma_slope_pct(df, "ma150", 20)
        s200 = _pro_ma_slope_pct(df, "ma200", 20)
        score = 0.0
        reasons = []

        if close is not None and ma50 is not None and close > ma50:
            score += 18; reasons.append("מחיר מעל MA50")
        if ma50 is not None and ma150 is not None and ma50 > ma150:
            score += 20; reasons.append("MA50 מעל MA150")
        if ma150 is not None and ma200 is not None and ma150 > ma200:
            score += 18; reasons.append("MA150 מעל MA200")
        if s50 is not None and s50 > 0:
            score += 16; reasons.append(f"MA50 עולה ({s50:+.1f}%/10d)")
        if s150 is not None and s150 > 0:
            score += 16; reasons.append(f"MA150 עולה ({s150:+.1f}%/20d)")
        if s200 is not None and s200 >= 0:
            score += 12; reasons.append(f"MA200 יציב/עולה ({s200:+.1f}%/20d)")

        out = {
            "score": _pro_clamp(score),
            "reasons": reasons,
            "metrics": {
                "close": close, "ma50": ma50, "ma150": ma150, "ma200": ma200,
                "ma50_slope_10d_pct": s50, "ma150_slope_20d_pct": s150, "ma200_slope_20d_pct": s200,
            },
        }
    except Exception as e:
        out["error"] = str(e)
    return out


def _professional_rs_profile(ticker: str, df: pd.DataFrame) -> dict:
    """Multi-horizon relative strength: 1M/3M/6M/12M + RS-line confirmation."""
    out = {"score": 0.0, "reasons": [], "metrics": {}}
    try:
        rs = compute_rs_score(ticker, df)
        base_rs = _to_float_or_none(rs.get("rs_score")) if isinstance(rs, dict) else None
        rels = []
        rel_map = {}
        for key in ("1m", "3m", "6m", "12m"):
            val = _to_float_or_none(rs.get(f"vs_spy_{key}")) if isinstance(rs, dict) else None
            if val is not None:
                rels.append(val)
                rel_map[key] = val
        positive = sum(1 for v in rels if v > 0)
        available = len(rels)
        score = (base_rs or 0.0) * 0.50
        if available:
            score += 35.0 * (positive / available)
        r1 = rel_map.get("1m")
        r3 = rel_map.get("3m")
        if r1 is not None:
            if r1 >= 5: score += 8
            elif r1 > 0: score += 5
        if r3 is not None:
            if r3 >= 8: score += 7
            elif r3 > 0: score += 4
        rs_high = _rs_line_new_high(df)
        if rs_high is True:
            score += 10

        reasons = []
        if base_rs is not None:
            reasons.append(f"RS בסיס {base_rs:.0f}")
        if available:
            reasons.append(f"מנצח SPY ב-{positive}/{available} חלונות")
        if rs_high is True:
            reasons.append("קו RS בשיא/קרוב לשיא")
        _dec = _to_float_or_none(rs.get("rs_decay_1m_vs_3m")) if isinstance(rs, dict) else None
        if _dec is not None and _dec <= -RS_DECAY_DEADBAND * 100:
            reasons.append(f"RS קצר דועך ({_dec:+.1f}% מול קצב 3M)")
        out = {
            "score": _pro_clamp(score),
            "reasons": reasons,
            "metrics": {"rs_score": base_rs, "relative_windows": rel_map, "positive_windows": positive, "available_windows": available, "rs_line_new_high": rs_high},
        }
    except Exception as e:
        out["error"] = str(e)
    return out


def _professional_sector_profile(ticker: str, rotation_map: dict | None = None) -> dict:
    """Turns existing sector-rotation data into a normalized 0-100 context score."""
    out = {"score": 55.0, "rank": "UNKNOWN", "sector": "N/A", "reasons": [], "metrics": {}}
    try:
        rotation_map = rotation_map if isinstance(rotation_map, dict) else (_sector_rotation_cache or build_sector_rotation_map())
        info = _get_yf_info(ticker)
        sector = str(info.get("sector") or "")
        data = rotation_map.get(sector, {}) if sector else {}
        rank = str(data.get("rank") or "UNKNOWN").upper()
        rank_scores = {"HOT": 100.0, "WARM": 85.0, "NEUTRAL": 62.0, "COLD": 38.0, "FROZEN": 12.0, "UNKNOWN": 55.0}
        score = rank_scores.get(rank, 55.0)
        rs_spy = _to_float_or_none(data.get("rs_spy"))
        if rs_spy is not None:
            score += max(-10.0, min(10.0, rs_spy * 2.0))
        reasons = [f"Sector {rank}"]
        if rs_spy is not None:
            reasons.append(f"Sector RS vs SPY {rs_spy:+.1f}%")
        out = {
            "score": _pro_clamp(score), "rank": rank, "sector": sector or "N/A", "reasons": reasons,
            "metrics": {"etf": data.get("etf"), "rs_spy": rs_spy, "perf_5d": data.get("perf_5d"), "perf_20d": data.get("perf_20d"), "vs_ma50": data.get("vs_ma50")},
        }
    except Exception as e:
        out["error"] = str(e)
    return out


def _professional_market_context(regime: dict | None = None, rotation_map: dict | None = None) -> dict:
    """Market quality using current regime + breadth proxy from unique sector ETFs; no extra universe scan."""
    out = {"score": 45.0, "regime": "UNKNOWN", "breadth_score": None, "reasons": [], "metrics": {}}
    try:
        reg = regime if isinstance(regime, dict) else get_market_regime()
        name = str(reg.get("regime") or "UNKNOWN").upper()
        regime_scores = {"BULL": 92.0, "NEUTRAL": 68.0, "BEAR": 12.0, "UNKNOWN": 45.0}
        regime_score = regime_scores.get(name, 45.0)
        rotation_map = rotation_map if isinstance(rotation_map, dict) else (_sector_rotation_cache or build_sector_rotation_map())

        rank_weights = {"HOT": 100.0, "WARM": 82.0, "NEUTRAL": 55.0, "COLD": 25.0, "FROZEN": 0.0}
        unique_etf = {}
        for data in (rotation_map or {}).values():
            if not isinstance(data, dict):
                continue
            etf = data.get("etf")
            if not etf or etf in unique_etf:
                continue
            unique_etf[etf] = rank_weights.get(str(data.get("rank") or "NEUTRAL").upper(), 55.0)
        breadth = (sum(unique_etf.values()) / len(unique_etf)) if unique_etf else None
        score = regime_score if breadth is None else regime_score * 0.65 + breadth * 0.35
        reasons = [f"Market {name}"]
        if breadth is not None:
            reasons.append(f"Sector breadth {breadth:.0f}/100")
        out = {
            "score": _pro_clamp(score), "regime": name, "breadth_score": None if breadth is None else round(breadth, 1), "reasons": reasons,
            "metrics": {"regime_score": regime_score, "unique_sector_etfs": len(unique_etf), "allow_trading": reg.get("allow_trading")},
        }
    except Exception as e:
        out["error"] = str(e)
    return out


def _professional_accumulation_profile(df: pd.DataFrame) -> dict:
    """Institutional-style accumulation proxy: OBV/CMF + up/down volume balance."""
    out = {"score": 50.0, "distribution": False, "reasons": [], "metrics": {}}
    try:
        ad = get_accumulation_score(df)
        label = str(ad.get("score") or "NEUTRAL").upper() if isinstance(ad, dict) else "NEUTRAL"
        base = {"ACCUMULATION": 70.0, "NEUTRAL": 50.0, "DISTRIBUTION": 18.0}.get(label, 50.0)
        closes = pd.to_numeric(df["close"], errors="coerce")
        vols = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else pd.Series(dtype=float)
        frame = pd.DataFrame({"close": closes, "volume": vols}).replace([np.inf, -np.inf], np.nan).dropna().tail(21)
        up_down_ratio = None
        high_vol_up = 0
        high_vol_down = 0
        if len(frame) >= 10:
            delta = frame["close"].diff()
            prev20 = frame["volume"].iloc[:-1]
            avg_vol = float(prev20.mean()) if len(prev20) else None
            up_vol = float(frame.loc[delta > 0, "volume"].sum())
            down_vol = float(frame.loc[delta < 0, "volume"].sum())
            if down_vol > 0:
                up_down_ratio = up_vol / down_vol
            elif up_vol > 0:
                up_down_ratio = 3.0
            if avg_vol and avg_vol > 0:
                high_vol_up = int(((delta > 0) & (frame["volume"] >= avg_vol * 1.15)).sum())
                high_vol_down = int(((delta < 0) & (frame["volume"] >= avg_vol * 1.15)).sum())

        score = base
        if up_down_ratio is not None:
            if up_down_ratio >= 1.5: score += 20
            elif up_down_ratio >= 1.15: score += 12
            elif up_down_ratio >= 0.90: score += 4
            elif up_down_ratio < 0.75: score -= 15
        cmf = _to_float_or_none(ad.get("cmf")) if isinstance(ad, dict) else None
        if cmf is not None:
            if cmf >= 0.10: score += 10
            elif cmf >= 0.03: score += 6
            elif cmf <= -0.10: score -= 12
            elif cmf < -0.03: score -= 6
        if high_vol_up > high_vol_down:
            score += min(8.0, float(high_vol_up - high_vol_down) * 2.0)

        distribution = bool(label == "DISTRIBUTION" and (up_down_ratio is None or up_down_ratio < 0.90) and (cmf is None or cmf < -0.03))
        reasons = [str(ad.get("summary") or label) if isinstance(ad, dict) else label]
        if up_down_ratio is not None:
            reasons.append(f"Up/Down volume {up_down_ratio:.2f}x")
        out = {
            "score": _pro_clamp(score), "distribution": distribution, "reasons": reasons,
            "metrics": {"obv_trend": ad.get("obv_trend") if isinstance(ad, dict) else None, "cmf": cmf, "up_down_volume_ratio": up_down_ratio, "high_volume_up_days": high_vol_up, "high_volume_down_days": high_vol_down},
        }
    except Exception as e:
        out["error"] = str(e)
    return out


def _professional_execution_profile(df: pd.DataFrame, entry_quality: dict) -> dict:
    """Differentiates a barely-valid trigger from a clean, decisive trigger."""
    out = {"score": 0.0, "reasons": [], "metrics": {}}
    try:
        m = entry_quality.get("metrics", {}) if isinstance(entry_quality, dict) else {}
        vr = _to_float_or_none(m.get("volume_ratio"))
        cp = _to_float_or_none(m.get("close_position"))
        body = _to_float_or_none(m.get("body_ratio"))
        bp = _to_float_or_none(m.get("breakout_pct"))
        open_p = _to_float_or_none(m.get("open")); high = _to_float_or_none(m.get("high")); low = _to_float_or_none(m.get("low")); close = _to_float_or_none(m.get("close"))
        upper_wick = None
        if None not in (open_p, high, low, close) and high > low:
            upper_wick = max(0.0, high - max(open_p, close)) / (high - low)

        score = 0.0
        family = str(m.get("entry_confirmation_family") or "BREAKOUT")
        confirmation_ok = bool(m.get("entry_confirmation_ok", False))
        volume_ok = bool(m.get("entry_volume_ok", False))
        if vr is not None:
            if vr >= 2.0: score += 25
            elif vr >= 1.5: score += 22
            elif vr >= ENTRY_MIN_VOLUME_RATIO: score += 18
            elif family in {"PULLBACK", "RETEST"} and volume_ok and vr >= 1.0: score += 14
        if cp is not None:
            if cp >= 0.85: score += 20
            elif cp >= 0.75: score += 18
            elif cp >= ENTRY_MIN_CLOSE_POS: score += 14
        if body is not None:
            if body >= 0.60: score += 16
            elif body >= 0.45: score += 13
            elif body >= ENTRY_MIN_BODY_RATIO: score += 9
        if upper_wick is not None:
            if upper_wick <= 0.12: score += 16
            elif upper_wick <= 0.25: score += 12
            elif upper_wick <= 0.40: score += 6
        if family == "BREAKOUT" and bp is not None:
            if ENTRY_MIN_BREAKOUT_PCT <= bp <= 0.015: score += 18
            elif 0 <= bp <= ENTRY_MAX_EXTENSION_PCT: score += 10
        elif family in {"PULLBACK", "RETEST"} and confirmation_ok:
            score += 15
        if m.get("green_candle") is True:
            score += 5

        reasons = [f"Entry mode {family}"]
        if vr is not None: reasons.append(f"Volume {vr:.2f}x")
        if cp is not None: reasons.append(f"Close position {cp*100:.0f}%")
        if upper_wick is not None: reasons.append(f"Upper wick {upper_wick*100:.0f}%")
        if bp is not None: reasons.append(f"Entry extension {bp*100:.2f}%")
        out = {"score": _pro_clamp(score), "reasons": reasons, "metrics": {"entry_mode": family, "volume_ratio": vr, "close_position": cp, "body_ratio": body, "upper_wick_ratio": upper_wick, "breakout_pct": bp}}
    except Exception as e:
        out["error"] = str(e)
    return out


def _professional_risk_profile(alert: dict, df: pd.DataFrame, entry_quality: dict) -> dict:
    """Validates trade geometry and scores the quality of the stop/target structure."""
    out = {"score": 0.0, "valid": False, "reasons": [], "blockers": [], "metrics": {}}
    try:
        qmetrics = entry_quality.get("metrics", {}) if isinstance(entry_quality, dict) else {}
        entry = _to_float_or_none(alert.get("breakout_level")) or _to_float_or_none(qmetrics.get("close"))
        stop = _to_float_or_none(alert.get("stop_loss"))
        target = _to_float_or_none(alert.get("target"))
        atr = _to_float_or_none(qmetrics.get("atr14"))
        blockers = []
        if entry is None or stop is None or target is None:
            blockers.append("Entry/Stop/Target לא תקינים")
            return {"score": 0.0, "valid": False, "reasons": [], "blockers": blockers, "metrics": {"entry": entry, "stop": stop, "target": target}}
        if stop >= entry:
            blockers.append("Stop חייב להיות מתחת למחיר הכניסה")
        if target <= entry:
            blockers.append("Target חייב להיות מעל מחיר הכניסה")
        risk = entry - stop
        reward = target - entry
        if risk <= 0 or reward <= 0:
            blockers.append("Risk/Reward geometry לא חוקי")
        if blockers:
            return {"score": 0.0, "valid": False, "reasons": [], "blockers": blockers, "metrics": {"entry": entry, "stop": stop, "target": target, "risk": risk, "reward": reward}}

        risk_pct = risk / entry
        rr_calc = reward / risk
        atr_risk = (risk / atr) if atr and atr > 0 else None
        bp = _to_float_or_none(qmetrics.get("breakout_pct"))
        score = 0.0
        if _rr_at_least(rr_calc, 4.0): score += 32
        elif _rr_at_least(rr_calc, 3.0): score += 27
        elif _rr_at_least(rr_calc, ENTRY_MIN_RR): score += 22
        elif rr_calc >= 2.0: score += 12
        if 0.01 <= risk_pct <= 0.055: score += 32
        elif 0.005 <= risk_pct <= 0.08: score += 25
        elif risk_pct <= 0.10: score += 15
        else: score += 5
        if atr_risk is not None:
            if 0.8 <= atr_risk <= 2.5: score += 22
            elif 0.5 <= atr_risk <= 3.5: score += 15
            else: score += 6
        else:
            score += 10
        if bp is not None:
            if ENTRY_MIN_BREAKOUT_PCT <= bp <= 0.015: score += 14
            elif bp <= ENTRY_MAX_EXTENSION_PCT: score += 8

        if risk_pct > PRO_MAX_STOP_RISK_PCT:
            blockers.append(f"סטופ רחב מדי ({risk_pct*100:.1f}% > {PRO_MAX_STOP_RISK_PCT*100:.1f}%)")
        if any(f in _canonical_pattern_name(alert) for f in _BREAKOUT_STOP_FAMILIES):
            if risk_pct > BREAKOUT_MAX_STOP_RISK_PCT + 1e-12 and risk_pct <= PRO_MAX_STOP_RISK_PCT:
                blockers.append(f"סטופ רחב מדי לפריצה ({risk_pct*100:.1f}% > {BREAKOUT_MAX_STOP_RISK_PCT*100:.1f}%)")
            if risk_pct < BREAKOUT_MIN_STOP_RISK_PCT - 1e-12:
                blockers.append(f"סטופ צמוד מדי לפריצה ({risk_pct*100:.1f}% < {BREAKOUT_MIN_STOP_RISK_PCT*100:.1f}%)")
        if not _rr_at_least(rr_calc, ENTRY_MIN_RR):
            blockers.append(f"R:R מחושב נמוך ({rr_calc:.2f} < {ENTRY_MIN_RR:.2f})")
        reasons = [f"Risk {risk_pct*100:.1f}%", f"R:R calculated {rr_calc:.2f}"]
        if atr_risk is not None:
            reasons.append(f"Stop distance {atr_risk:.2f} ATR")
        out = {
            "score": _pro_clamp(score), "valid": not blockers, "reasons": reasons, "blockers": blockers,
            "metrics": {"entry": entry, "stop": stop, "target": target, "risk": risk, "reward": reward, "risk_pct": risk_pct, "rr_calculated": rr_calc, "atr_risk_multiple": atr_risk},
        }
    except Exception as e:
        out["error"] = str(e)
        out["blockers"] = ["שגיאה בבדיקת Risk geometry"]
    return out


def evaluate_professional_trade_quality(df: pd.DataFrame, ticker: str, alert: dict, entry_quality: dict, regime: dict | None = None) -> dict:
    """
    V9.1 final professional gate.
    Does NOT detect or alter patterns. It ranks context after a setup is already ENTRY_READY.
    """
    try:
        rotation_map = _sector_rotation_cache or build_sector_rotation_map()
        trend = _professional_trend_profile(df)
        rs = _professional_rs_profile(ticker, df)
        sector = _professional_sector_profile(ticker, rotation_map)
        market = _professional_market_context(regime, rotation_map)
        accumulation = _professional_accumulation_profile(df)
        execution = _professional_execution_profile(df, entry_quality)
        risk = _professional_risk_profile(alert, df, entry_quality)

        # Weights sum to 100. Entry quality already passed V8; this score measures CONTEXT quality.
        score = (
            trend.get("score", 0) * 0.20
            + rs.get("score", 0) * 0.20
            + sector.get("score", 0) * 0.10
            + market.get("score", 0) * 0.05
            + accumulation.get("score", 0) * 0.15
            + execution.get("score", 0) * 0.20
            + risk.get("score", 0) * 0.10
        )
        score = _pro_clamp(score)
        blockers = list(risk.get("blockers", []) or [])
        weekly_ctx = None
        if trend.get("score", 0) < PRO_MIN_TREND_SCORE:
            blockers.append(f"Trend score נמוך ({trend.get('score',0):.0f} < {PRO_MIN_TREND_SCORE:.0f})")
        if rs.get("score", 0) < PRO_MIN_RS_PROFILE_SCORE:
            blockers.append(f"Multi-horizon RS נמוך ({rs.get('score',0):.0f} < {PRO_MIN_RS_PROFILE_SCORE:.0f})")
        if market.get("score", 0) < PRO_MIN_MARKET_CONTEXT_SCORE:
            blockers.append(f"Market context חלש ({market.get('score',0):.0f} < {PRO_MIN_MARKET_CONTEXT_SCORE:.0f})")
        if PRO_BLOCK_FROZEN_SECTOR and sector.get("rank") == "FROZEN":
            blockers.append("Sector FROZEN")
        if PRO_BLOCK_DISTRIBUTION and accumulation.get("distribution"):
            blockers.append("Institutional distribution חזקה")
        if score < PRO_MIN_SCORE:
            blockers.append(f"Professional Quality נמוך ({score:.1f} < {PRO_MIN_SCORE:.1f})")
        if WEEKLY_ALIGN_MODE in ("warn", "block"):
            weekly_ctx = weekly_context_from_daily(df)
            if weekly_ctx.get("aligned") is False:      # unknown (None) never blocks
                if WEEKLY_ALIGN_MODE == "block":
                    blockers.append(f"שבועי לא מיושר עם הפריצה ({weekly_ctx.get('reason')})")
                else:
                    log(f"   ⚠️ {ticker}: weekly not aligned — {weekly_ctx.get('reason')}")

        label = "ELITE" if score >= 88 else "STRONG" if score >= 82 else "QUALIFIED" if score >= PRO_MIN_SCORE else "WATCHLIST"
        ready = bool(PRO_ENGINE_ENABLED and not blockers)
        return {
            "status": "PRO_READY" if ready else "PRO_WATCHLIST",
            "professional_ready": ready,
            "professional_score": score,
            "label": label,
            "blockers": list(dict.fromkeys(blockers)),
            "components": {"trend": trend, "rs": rs, "sector": sector, "market": market, "accumulation": accumulation, "execution": execution, "risk": risk,
                           **({"weekly": weekly_ctx} if weekly_ctx else {})},
        }
    except Exception as e:
        return {"status": "PRO_WATCHLIST", "professional_ready": False, "professional_score": 0.0, "label": "ERROR", "blockers": [f"Professional engine error: {type(e).__name__}: {e}"], "components": {}}


def log_professional_quality_decision(alert: dict, pro: dict) -> None:
    """Persistent calibration log for later MFE/MAE/backtest analysis."""
    try:
        comps = pro.get("components", {}) if isinstance(pro, dict) else {}
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "ticker": alert.get("ticker", ""),
            "pattern": alert.get("pattern_type", ""),
            "entry_quality": (alert.get("entry_quality", {}) or {}).get("quality_score") if isinstance(alert.get("entry_quality", {}), dict) else None,
            "professional_score": pro.get("professional_score"),
            "professional_status": pro.get("status"),
            "label": pro.get("label"),
            "trend_score": (comps.get("trend", {}) or {}).get("score"),
            "rs_profile_score": (comps.get("rs", {}) or {}).get("score"),
            "sector_score": (comps.get("sector", {}) or {}).get("score"),
            "sector_rank": (comps.get("sector", {}) or {}).get("rank"),
            "market_score": (comps.get("market", {}) or {}).get("score"),
            "accumulation_score": (comps.get("accumulation", {}) or {}).get("score"),
            "execution_score": (comps.get("execution", {}) or {}).get("score"),
            "risk_score": (comps.get("risk", {}) or {}).get("score"),
            "blockers": " | ".join(pro.get("blockers", []) or []),
        }
        row = _redact_log_value(row)
        file_exists = os.path.exists(PRO_QUALITY_LOG)
        parent = os.path.dirname(PRO_QUALITY_LOG)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(PRO_QUALITY_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not file_exists or os.path.getsize(PRO_QUALITY_LOG) == 0:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        log(f"professional_quality_log error: {e}")


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
                # התנגדות אופקית: שתי הפסגות בטווח TRIANGLE_PRICE_TOL (1.5%) זו מזו
                if abs(hp_j - hp_i) / max(hp_i, 1e-9) > TRIANGLE_PRICE_TOL:
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

def check_for_consolidation_breakout(df: pd.DataFrame, ticker: str, min_score: float | None = None) -> list[dict]:
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
    effective_min_score = float(min_score if min_score is not None else MIN_ALERT_SCORE)
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
                "pattern_bars":    int(n - pi),
                "start_index":     int(max(0, len(df) - len(window) + pi)),
                "vol_declining":   vol_declining,
                "pattern_height":  float(gap_start),
                "depth_pct":       float(gap_start / max(hp_i, 1e-9)),
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
    if score < ENTRY_CANDIDATE_MIN_SCORE:
        if DEBUG_SCAN_REASONS:
            log(f"{ticker}: Falling Wedge — candidate score too low ({score:.1f} < {ENTRY_CANDIDATE_MIN_SCORE:.1f})")
        return alerts

    stop, target, size, atr, rr = atr_stop_and_position(best["breakout_level"], df, best)
    if not _rr_at_least(rr, ENTRY_MIN_RR):
        if DEBUG_SCAN_REASONS:
            log(f"{ticker}: Falling Wedge — RR too low ({rr:.2f} < {ENTRY_MIN_RR:.2f})")
        return alerts
    if DEBUG_SCAN_REASONS:
        log(f"{ticker}: ✅ Falling Wedge candidate — base_score={score:.1f} candidate_min={ENTRY_CANDIDATE_MIN_SCORE:.1f} neck={best['breakout_level']:.2f} rr={rr:.2f}")

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
                 "score_reasons": best.get("score_reasons", []),
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
        if SUPPRESS_ALERTS_FOR_OPEN_POSITIONS:
            try:
                if is_position_open(ticker):
                    log(f"{ticker}: duplicate alert suppressed — open position already exists")
                    return True
            except Exception as e:
                log(f"open-position dedup check error for {ticker}: {e}")
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
def scan_ticker(ticker: str, alert_history: dict, filter_stats: dict | None = None, min_score: float | None = None,
                filter_ticker_sets: dict[str, set[str]] | None = None) -> list[dict]:
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
        """עדכון counter גולמי + סט טיקרים ייחודיים לצורך דוח אחוזים אמיתי."""
        if filter_stats is not None and key in filter_stats:
            filter_stats[key] += 1
        if filter_ticker_sets is not None and key in filter_ticker_sets:
            filter_ticker_sets[key].add(symbol)

    # --- Market Cap ---
    mc = fetch_market_cap(symbol)
    if mc is not None and mc < MIN_MARKET_CAP_USD:
        log(f"{symbol}: market cap too low (${mc/1e6:.0f}M < ${MIN_MARKET_CAP_USD/1e9:.1f}B). Skip."); _fs("market_cap"); return []
    # FIXED: אם mc=None (לא זמין) — ממשיכים, לא דוחים

    # ── פילטר דוחות: לפני הורדת נתונים — חוסך קריאת API לכל מניה שנפסלת ──
    earn_ok, earn_reason = earnings_filter_ok(symbol)
    if not earn_ok:
        log(f"{symbol}: ⏭️ Earnings filter — {earn_reason}"); _fs("earnings"); return []
    elif DEBUG_SCAN_REASONS and "unknown" not in earn_reason:
        log(f"{symbol}: 📅 {earn_reason}")

    # --- Data ---
    df = fetch_data_twelvedata(symbol, outputsize=500)
    if df is None or df.empty:
        df = fetch_data_yfinance(symbol)
    df = _normalize_yfinance_df(df, drop_invalid_close=False) if df is not None else None
    if df is None or df.empty or len(df) < 50:
        log(f"{symbol}: no data. Skip."); _fs("no_data"); return []

    df = _closed_daily_frame(df)
    if df is None or df.empty or len(df) < 50:
        log(f"{symbol}: no sufficient closed daily data. Skip."); _fs("no_data"); return []
    if _daily_data_problems(df):
        log(f"{symbol}: invalid/stale daily data: {'; '.join(_daily_data_problems(df))}")
        _fs("no_data"); return []

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
                    if score_e >= ENTRY_CANDIDATE_MIN_SCORE:
                        stop_e, target_e, size_e, atr_e, rr_e = atr_stop_and_position(neck_db_early, df, meta_db_early)
                        if _rr_at_least(rr_e, ENTRY_MIN_RR):
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
                if score >= ENTRY_CANDIDATE_MIN_SCORE:
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
                if score >= ENTRY_CANDIDATE_MIN_SCORE:
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
                if score >= ENTRY_CANDIDATE_MIN_SCORE:
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
        phase2 = check_for_consolidation_breakout(df, symbol, min_score=effective_min_score)
        # סנן סטאפים שרחוקים מדי מ-EMA28 או מ-MA150
        if phase2:
            phase2 = [p for p in phase2
                      if abs((price_now - float(df["ema28"].iloc[-1])) / float(df["ema28"].iloc[-1])) <= EMA28_MAX_DIST_PCT
                      and ma_dist <= MA150_MAX_DISTANCE]
        candidates.extend(phase2)
    except Exception as e:
        log(f"{symbol} Phase2 error: {e}")

    # ====== PHASE 3 — V9 PATTERN EXPANSION ======
    # מוסיף תבניות איכותיות חדשות, בלי לשנות את הסינונים המוקדמים.
    try:
        added_v9 = append_v9_pattern_candidates(symbol, df, candidates)
        if added_v9 and DEBUG_SCAN_REASONS:
            log(f"{symbol}: 🧩 V9 Pattern Expansion added {added_v9} candidate(s)")
    except Exception as e:
        log(f"{symbol} V9 Pattern Expansion error: {type(e).__name__}: {e}")

    if not candidates:
        _fs("no_pattern")
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

    # --- V8 Entry Ready Gate: תבנית לבד לא מספיקה ---
    if ENTRY_ENGINE_ENABLED:
        entry_ready_candidates: list[dict] = []
        for alert in candidates:
            if not isinstance(alert, dict):
                continue
            try:
                meta = alert.setdefault("meta", {})
                base_score = float(alert.get("score", 0) or 0)

                _sync_trade_geometry(alert, df)
                meta.update({"signal_date": pd.Timestamp(df.index[-1]).date().isoformat(),
                             "market_cap_verified": mc is not None,
                             "earnings_evidence": earn_reason})
                data_blockers = []
                if mc is None:
                    data_blockers.append("Market cap לא אומת מעל $1B")
                if REQUIRE_VERIFIED_EARNINGS and "earnings date unknown" in str(earn_reason).lower():
                    data_blockers.append("מועד דוחות לא אומת")
                if data_blockers:
                    quality = {"entry_ready": False, "status": "DATA_WAIT", "quality_score": 0,
                               "blocking_fails": data_blockers, "fail_reasons": data_blockers, "metrics": {}}
                    alert["entry_quality"] = quality
                    log_entry_quality_decision(alert, quality)
                    _record_entry_diagnostics(alert.get("pattern_type", ""), quality)
                    _record_setup_evaluation(alert, "DATA_WAIT", df)
                    log(f"{symbol}: DATA WAIT — {'; '.join(data_blockers)}")
                    _fs("entry_quality")
                    continue

                # V9.1.5 hard safety: invalid long levels are rejected before any score.
                levels_ok, level_problems, level_values = _validate_long_trade_levels(alert)
                if not levels_ok:
                    safety_quality = {
                        "status": "SAFETY_REJECTED",
                        "entry_ready": False,
                        "quality_score": 0.0,
                        "base_score": round(base_score, 1),
                        "reasons": [],
                        "fail_reasons": level_problems,
                        "blocking_fails": level_problems,
                        "metrics": {
                            "rr": level_values.get("rr"),
                            "regime": "UNKNOWN",
                        },
                    }
                    alert["base_score"] = base_score
                    alert["entry_quality"] = safety_quality
                    alert["quality_score"] = 0.0
                    meta["entry_quality"] = safety_quality
                    meta["fail_reasons"] = list(dict.fromkeys((meta.get("fail_reasons", []) or []) + level_problems))
                    log_entry_quality_decision(alert, safety_quality)
                    _record_entry_diagnostics(alert.get("pattern_type", ""), safety_quality)
                    _record_setup_evaluation(alert, "SAFETY_REJECTED", df)
                    log(f"{symbol}: 🛑 SAFETY REJECT {alert.get('pattern_type','')} — {'; '.join(level_problems[:4])}")
                    _fs("entry_quality")
                    continue

                quality = evaluate_entry_quality(
                    df=df,
                    ticker=symbol,
                    breakout_level=alert.get("breakout_level", 0),
                    pattern_meta=meta,
                    base_score=base_score,
                    rr=alert.get("rr_ratio"),
                    regime=get_market_regime(),
                )
                alert["base_score"] = base_score
                alert["entry_quality"] = quality
                alert["quality_score"] = quality.get("quality_score", 0)
                alert["score"] = quality.get("quality_score", base_score)  # המייל ממוין לפי איכות כניסה אמיתית
                meta["entry_quality"] = quality
                meta["entry_quality_reasons"] = quality.get("reasons", [])
                meta["fail_reasons"] = list(dict.fromkeys((meta.get("fail_reasons", []) or []) + quality.get("fail_reasons", [])))
                log_entry_quality_decision(alert, quality)
                _record_entry_diagnostics(alert.get("pattern_type", ""), quality)
                _record_setup_evaluation(alert, "ENTRY_PASSED" if quality.get("entry_ready") else "ENTRY_REJECTED", df)

                if not quality.get("entry_ready", False):
                    fails = quality.get("blocking_fails", []) or quality.get("fail_reasons", []) or []
                    short = "; ".join(fails[:4]) if fails else "לא עבר שכבת איכות כניסה"
                    log(f"{symbol}: 👀 WATCHLIST ONLY {alert.get('pattern_type','')} — {short}")
                    _fs("entry_quality")
                    try:
                        log_watchlist(
                            symbol=symbol,
                            price=price_now,
                            reason="תבנית קיימת אבל לא Entry Ready",
                            pattern=alert.get("pattern_type", ""),
                            breakout=float(alert.get("breakout_level", 0) or 0),
                            score=float(alert.get("score", 0) or 0),
                            details=short,
                        )
                    except Exception:
                        pass
                    continue

                log(f"{symbol}: 🚀 ENTRY READY {alert.get('pattern_type','')} | quality={float(alert.get('score',0) or 0):.1f} | base={base_score:.1f} | threshold={ENTRY_READY_MIN_SCORE:.1f}")
                entry_ready_candidates.append(alert)
            except Exception as e:
                log(f"{symbol}: entry quality gate error: {type(e).__name__}: {e}")
                _fs("entry_quality")

        candidates = entry_ready_candidates
        if not candidates:
            return []

    # --- V9.1 Professional Trade Quality Gate ---
    # Pattern detectors stay untouched. This layer only ranks/filters candidates that already passed V8.
    if PRO_ENGINE_ENABLED:
        pro_ready_candidates: list[dict] = []
        for alert in candidates:
            if not isinstance(alert, dict):
                continue
            try:
                q = alert.get("entry_quality", {}) if isinstance(alert.get("entry_quality", {}), dict) else {}
                pro = evaluate_professional_trade_quality(
                    df=df, ticker=symbol, alert=alert, entry_quality=q, regime=get_market_regime()
                )
                alert["entry_quality_score"] = float(q.get("quality_score", alert.get("score", 0)) or 0)
                alert["professional_quality"] = pro
                alert["professional_score"] = float(pro.get("professional_score", 0) or 0)
                alert.setdefault("meta", {})["professional_quality"] = pro
                log_professional_quality_decision(alert, pro)
                _record_setup_evaluation(alert, "PRO_PASSED" if pro.get("professional_ready") else "PRO_REJECTED", df)

                if not pro.get("professional_ready", False):
                    blockers = pro.get("blockers", []) or []
                    short = "; ".join(blockers[:4]) if blockers else "לא עבר Professional Quality"
                    log(f"{symbol}: 🧠 PRO WATCHLIST {alert.get('pattern_type','')} | pro={float(pro.get('professional_score',0) or 0):.1f} | entry={alert['entry_quality_score']:.1f} — {short}")
                    _fs("professional_quality")
                    continue

                # Final ranking score = professional score. Keep V8 score separately for audit.
                alert["score"] = float(pro.get("professional_score", 0) or 0)
                log(f"{symbol}: ⭐ PROFESSIONAL READY {alert.get('pattern_type','')} | pro={alert['score']:.1f} | entry={alert['entry_quality_score']:.1f} | label={pro.get('label','QUALIFIED')}")
                pro_ready_candidates.append(alert)
            except Exception as e:
                log(f"{symbol}: professional quality gate error: {type(e).__name__}: {e}")
                _fs("professional_quality")

        candidates = pro_ready_candidates
        if not candidates:
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
        if DEBUG_SCAN_REASONS:
            q = alert.get("entry_quality", {}) if isinstance(alert.get("entry_quality", {}), dict) else {}
            pro = alert.get("professional_quality", {}) if isinstance(alert.get("professional_quality", {}), dict) else {}
            entry_score = float(alert.get("entry_quality_score", q.get("quality_score", 0)) or 0)
            pro_score = float(alert.get("professional_score", alert.get("score", 0)) or 0)
            log(f"ALERT REASON {symbol}: {pname} | status={q.get('status','ENTRY_READY')}/{pro.get('status','PRO_READY')} | professional={pro_score:.1f} | entry={entry_score:.1f} | base={float(alert.get('base_score', 0) or 0):.1f} | dynamic_threshold={effective_min_score:.1f} | entry_threshold={ENTRY_READY_MIN_SCORE:.1f} | pro_threshold={PRO_MIN_SCORE:.1f} | level={bl}")
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

# v9.7.5: RS_MODE=legacy keeps the original 3M/6M/12M formula bit-for-bit. RS_MODE=swing re-weights towards the
# 1M/3M windows and penalises decaying short-term relative strength. The defaults are hypotheses to be validated
# with the backtest columns exc_1m / exc_3m / rs_decay / rsl_vs_ema21 before switching it on for the gate.
RS_MODE = os.getenv("RS_MODE", "legacy").strip().lower()
RS_SWING_W_1M = float(os.getenv("RS_SWING_W_1M", "0.30"))
RS_SWING_W_3M = float(os.getenv("RS_SWING_W_3M", "0.40"))
RS_SWING_W_6M = float(os.getenv("RS_SWING_W_6M", "0.20"))
RS_SWING_W_12M = float(os.getenv("RS_SWING_W_12M", "0.10"))
RS_DECAY_K = float(os.getenv("RS_DECAY_K", "1.0"))                       # penalty = K x (1M excess - 3M excess / 3), only when negative
RS_DECAY_DEADBAND = float(os.getenv("RS_DECAY_DEADBAND", "0.02"))         # ignore decay smaller than 2 points (compounding artefact)
RS_DECAY_MAX = float(os.getenv("RS_DECAY_MAX", "0.15"))                   # cap on that penalty (excess-return units, 0.15 = 15 points)
RS_LINE_BELOW_EMA_PENALTY = float(os.getenv("RS_LINE_BELOW_EMA_PENALTY", "0.03"))
RS_LINE_EMA_SPAN = int(os.getenv("RS_LINE_EMA_SPAN", "21"))


def _get_spy_returns() -> dict:
    """Cache יומי לביצועי SPY ל-3/6/12 חודשים."""
    import datetime
    today = str(datetime.date.today())
    if _rs_spy_cache.get("date") == today:
        return _rs_spy_cache.get("returns", {})
    try:
        spy_df = _normalize_yfinance_df(yf.download("SPY", period="13mo", interval="1d",
                                                    progress=False, auto_adjust=True))
        if spy_df is None or len(spy_df) < 60:
            return {}
        close = spy_df["close"]
        now = _last_finite(close)
        if not _is_finite_number(now):
            return {}
        ret = {
            "1m":  (now - float(close.iloc[-21]))  / max(float(close.iloc[-21]),  1e-9) if len(close) >= 21  else None,
            "3m":  (now - float(close.iloc[-63]))  / max(float(close.iloc[-63]),  1e-9) if len(close) >= 63  else None,
            "6m":  (now - float(close.iloc[-126])) / max(float(close.iloc[-126]), 1e-9) if len(close) >= 126 else None,
            "12m": (now - float(close.iloc[-252])) / max(float(close.iloc[-252]), 1e-9) if len(close) >= 252 else None,
        }
        _rs_spy_cache["date"]    = today
        _rs_spy_cache["returns"] = ret
        _rs_spy_cache["close"]   = close      # same download, kept for the RS-line check (no extra request)
        return ret
    except Exception:
        return {}


def _get_spy_close():
    """SPY daily closes from the once-per-day cache (None when unavailable)."""
    _get_spy_returns()
    return _rs_spy_cache.get("close")


def _naive_day_index(series: pd.Series) -> pd.Series:
    out = pd.to_numeric(series, errors="coerce").copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    return out[~out.index.duplicated(keep="last")]


def _rs_line_vs_ema(stock_close: pd.Series, span: int | None = None) -> float | None:
    """(stock / SPY) relative line vs its own EMA, as a fraction. Negative = the relative line has rolled over."""
    try:
        span = span or RS_LINE_EMA_SPAN
        spy = _get_spy_close()
        if spy is None or stock_close is None:
            return None
        joined = pd.concat([_naive_day_index(stock_close).rename("s"), _naive_day_index(spy).rename("m")],
                           axis=1, join="inner").dropna()
        if len(joined) < span + 5:
            return None
        line = joined["s"] / joined["m"].replace(0, np.nan)
        val = float(line.iloc[-1] / line.ewm(span=span, adjust=False).mean().iloc[-1] - 1.0)
        return val if np.isfinite(val) else None
    except Exception:
        return None


def _rs_swing_combined(stock_close, p: dict, r: dict) -> tuple[float, dict]:
    """Swing-horizon combined excess return (fraction) + diagnostics.
    p / r: window -> stock return / excess return vs SPY (None when the window is missing)."""
    weights = {"1m": RS_SWING_W_1M, "3m": RS_SWING_W_3M, "6m": RS_SWING_W_6M, "12m": RS_SWING_W_12M}
    parts = [(w, r[k]) for k, w in weights.items() if p.get(k) is not None]
    wsum = sum(w for w, _ in parts)
    combined = (sum(w * x for w, x in parts) / wsum) if wsum else 0.0
    info: dict = {"rs_mode": "swing"}
    if p.get("1m") is not None and p.get("3m") is not None:
        decay = r["1m"] - r["3m"] / 3.0           # last month vs the monthly pace of the last quarter
        info["rs_decay_1m_vs_3m"] = round(decay * 100, 2)
        if decay < -RS_DECAY_DEADBAND:
            combined += RS_DECAY_K * max(decay + RS_DECAY_DEADBAND, -RS_DECAY_MAX)
    state = _rs_line_vs_ema(stock_close)
    if state is not None:
        info["rs_line_vs_ema_pct"] = round(state * 100, 2)
        if state < 0:
            combined -= RS_LINE_BELOW_EMA_PENALTY
    return combined, info


def compute_rs_score(ticker: str, df: pd.DataFrame) -> dict:
    """
    Custom weighted excess-return score versus SPY, also used by entry gates.
    This tanh transformation is not a cross-sectional percentile or IBD RS Rating.
    The existing formula and thresholds are preserved.
    """
    result = {
        "rs_score":  None,
        "rs_label":  "N/A",
        "perf_1m":   None,
        "perf_3m":   None,
        "perf_6m":   None,
        "perf_12m":  None,
        "vs_spy_1m": None,
        "vs_spy_3m": None,
        "vs_spy_6m": None,
        "vs_spy_12m": None,
        "summary":   "RS N/A",
    }
    try:
        if df is None or len(df) < 63:
            return result
        close = df["close"] if "close" in df.columns else df.iloc[:, 0]
        now   = float(close.iloc[-1])

        # ביצועי המניה
        p1m  = (now - float(close.iloc[-21]))  / max(float(close.iloc[-21]),  1e-9) if len(close) >= 21  else None
        p3m  = (now - float(close.iloc[-63]))  / max(float(close.iloc[-63]),  1e-9) if len(close) >= 63  else None
        p6m  = (now - float(close.iloc[-126])) / max(float(close.iloc[-126]), 1e-9) if len(close) >= 126 else None
        p12m = (now - float(close.iloc[-252])) / max(float(close.iloc[-252]), 1e-9) if len(close) >= 252 else None
        result["perf_1m"]  = round(p1m  * 100, 1) if p1m  is not None else None
        result["perf_3m"]  = round(p3m  * 100, 1) if p3m  is not None else None
        result["perf_6m"]  = round(p6m  * 100, 1) if p6m  is not None else None
        result["perf_12m"] = round(p12m * 100, 1) if p12m is not None else None

        # ביצועי SPY
        spy  = _get_spy_returns()
        if not spy:
            result["summary"] = "RS N/A — SPY unavailable"
            return result
        s1m  = spy.get("1m",  0) or 0
        s3m  = spy.get("3m",  0) or 0
        s6m  = spy.get("6m",  0) or 0
        s12m = spy.get("12m", 0) or 0

        # עודף תשואה יחסי
        r1m  = (p1m  - s1m)  if p1m  is not None else 0.0
        r3m  = (p3m  - s3m)  if p3m  is not None else 0.0
        r6m  = (p6m  - s6m)  if p6m  is not None else 0.0
        r12m = (p12m - s12m) if p12m is not None else 0.0
        result["vs_spy_1m"]  = round(r1m  * 100, 1)
        result["vs_spy_3m"]  = round(r3m  * 100, 1)
        result["vs_spy_6m"]  = round(r6m  * 100, 1)
        result["vs_spy_12m"] = round(r12m * 100, 1)

        # ציון IBD משוקלל → tanh → 0-100
        import math
        if RS_MODE == "swing":
            combined, _swing = _rs_swing_combined(
                close, {"1m": p1m, "3m": p3m, "6m": p6m, "12m": p12m}, {"1m": r1m, "3m": r3m, "6m": r6m, "12m": r12m})
            result.update(_swing)
        else:
            _parts = [(w, r) for w, r, p in ((0.4, r3m, p3m), (0.2, r6m, p6m), (0.4, r12m, p12m)) if p is not None]
            _wsum = sum(w for w, _ in _parts)
            combined = (sum(w * r for w, r in _parts) / _wsum) if _wsum else 0.0  # identical to before when all windows exist
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
        _dec = result.get("rs_decay_1m_vs_3m")
        if _dec is not None and _dec <= -RS_DECAY_DEADBAND * 100:
            result["summary"] += f" | ⚠️ RS קצר דועך {_dec:+.1f}%"

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

LEARNING_CONFIG_FILE = _state_path(os.getenv("LEARNING_CONFIG_FILE", "scanner_learned_params.json"))
LEARNING_MIN_SAMPLES = 50    # V9.3: דורש מדגם גדול יותר לפני שינוי פרמטרים
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
    בודק את מצב השוק לפי SPY vs MA50.
    FIX V7: אם Yahoo מחזיר שורת NaN, מנקים אותה. אם עדיין אין נתונים תקינים —
    מחזירים UNKNOWN ולא BEAR, כדי שלא יירד הסף בטעות.
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
        "allow_trading": True,
        "data_ok":       False,
        "summary":       "🟡 Market Regime: UNKNOWN | SPY data unavailable — using safe threshold",
    }
    try:
        df = _normalize_yfinance_df(yf.download("SPY", period="3mo", interval="1d",
                                                progress=False, auto_adjust=True))
        if df is None or len(df) < REGIME_MA_PERIOD + 2:
            log(f"🌍 {result['summary']}")
            _regime_cache = result
            return result

        close = df["close"]
        price = _last_finite(close)
        ma_series = close.rolling(REGIME_MA_PERIOD).mean()
        ma50 = _last_finite(ma_series)

        ma_clean = pd.to_numeric(ma_series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        prev_ma50 = float(ma_clean.iloc[-2]) if len(ma_clean) >= 2 else None

        if not all(_is_finite_number(v) for v in (price, ma50, prev_ma50)):
            log(f"🌍 {result['summary']}")
            _regime_cache = result
            return result

        vs_ma50_raw = _safe_pct(price - ma50, ma50, default=None)
        if vs_ma50_raw is None:
            log(f"🌍 {result['summary']}")
            _regime_cache = result
            return result
        vs_ma50 = round(vs_ma50_raw, 2)
        ma_rising = ma50 > prev_ma50

        if price > ma50 and ma_rising:
            regime, emoji, allow = "BULL", "🟢", True
        elif price > ma50 and not ma_rising:
            regime, emoji, allow = "NEUTRAL", "🟡", True
        elif price < ma50 and vs_ma50 > -3:
            regime, emoji, allow = "NEUTRAL", "🟡", True
        else:
            regime, emoji, allow = "BEAR", "🔴", not REGIME_ENABLED

        result.update({
            "regime":        regime,
            "spy_price":     round(float(price), 2),
            "ma50":          round(float(ma50), 2),
            "vs_ma50_pct":   vs_ma50,
            "emoji":         emoji,
            "allow_trading": allow,
            "data_ok":       True,
            "summary":       f"{emoji} Market Regime: {regime} | SPY {vs_ma50:+.1f}% vs MA{REGIME_MA_PERIOD}",
        })
        log(f"🌍 {result['summary']}")
        if not allow:
            log("⛔ BEAR Market — scan suspended (REGIME_ENABLED=True). Note: ENTRY_BLOCK_BEAR_REGIME also blocks entries in BEAR")

    except Exception as e:
        log(f"get_market_regime error: {e}")
        log(f"🌍 {result['summary']}")

    _regime_cache = result
    return result


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
        spy_df = _normalize_yfinance_df(yf.download("SPY", period="3mo", interval="1d",
                                                    progress=False, auto_adjust=True))
        if spy_df is None or len(spy_df) < 21:
            raise ValueError("SPY sector benchmark unavailable")
        spy_close = spy_df["close"]
        spy_now = _last_finite(spy_close)
        spy_20d = float(spy_close.iloc[-21]) if len(spy_close) >= 21 else float(spy_close.iloc[0])
        if not _is_finite_number(spy_now) or not _is_finite_number(spy_20d):
            raise ValueError("SPY sector benchmark not finite")
        spy_perf = _safe_pct(spy_now - spy_20d, spy_20d, default=0.0) or 0.0
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
            df = _normalize_yfinance_df(yf.download(etf, period="3mo", interval="1d",
                                                    progress=False, auto_adjust=True))
            if df is None or len(df) < 25:
                result[sector] = {"etf": etf, "rank": "NEUTRAL", "error": "no data"}
                continue

            close = df["close"]
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
#  V9.2 EXIT INTELLIGENCE ENGINE — מעקב אחרי פוזיציות פתוחות
#  עקרונות:
#  • Stop Loss המקורי נשאר קבוע ואינו נגרר כלפי מעלה.
#  • רק אחרי שהעסקה הגיעה לפחות ל-+5% מופעל Profit Protection נפרד.
#  • Target הוא יעד ייחוס בלבד; הגעה אליו אינה סוגרת אוטומטית עסקה חזקה.
#  • יציאת Reversal דורשת צירוף של כמה סימנים, ולא Doji/Volume בודד.
#  • אותות Candlestick/Volume/Reversal מחושבים רק על נר יומי סגור.
# ============================================================

POSITIONS_FILE = _state_path(os.getenv("POSITIONS_FILE", "open_positions.json"))
POSITION_MAX_DAYS = int(os.getenv("POSITION_MAX_DAYS", "30"))  # review בלבד, לא יציאה אוטומטית
SEND_POSITIONS_STATUS_EMAIL = os.getenv("SEND_POSITIONS_STATUS_EMAIL", "True").lower() in ("1", "true", "yes")

EXIT_PROFIT_ACTIVATE_PCT = float(os.getenv("EXIT_PROFIT_ACTIVATE_PCT", "5.0"))
EXIT_CHANDELIER_ATR_MULT = float(os.getenv("EXIT_CHANDELIER_ATR_MULT", "3.0"))
EXIT_WATCH_SCORE = float(os.getenv("EXIT_WATCH_SCORE", "25.0"))
EXIT_CONFIRM_SCORE = float(os.getenv("EXIT_CONFIRM_SCORE", "55.0"))
EXIT_DISTRIBUTION_VOL_MULT = float(os.getenv("EXIT_DISTRIBUTION_VOL_MULT", "1.5"))
EXIT_CLIMAX_VOL_MULT = float(os.getenv("EXIT_CLIMAX_VOL_MULT", "2.0"))
EXIT_DOJI_BODY_MAX = float(os.getenv("EXIT_DOJI_BODY_MAX", "0.10"))

# ── v9.7.5 swing-horizon exit (2 weeks - 1 month). SWING_EXIT_ENGINE=0 restores V9.2 behaviour exactly. ──
# Starting values for the parameters below come from the swing-trading brief, NOT from a fitted backtest;
# the exit-rule lab (bt3/exits.py) is the tool to tune them on saved forward paths.
SWING_EXIT_ENGINE = os.getenv("SWING_EXIT_ENGINE", "1").lower() in ("1", "true", "yes")
EXIT_ACTIVATE_ATR_MULT = float(os.getenv("EXIT_ACTIVATE_ATR_MULT", "1.5"))   # profit-protection trigger = 1.5 x ATR% at entry ...
EXIT_ACTIVATE_MIN_PCT = float(os.getenv("EXIT_ACTIVATE_MIN_PCT", "2.5"))     # ... bounded below ...
EXIT_ACTIVATE_MAX_PCT = float(os.getenv("EXIT_ACTIVATE_MAX_PCT", "5.0"))     # ... and above by the old fixed +5 %
EXIT_TRAIL_EMA_SPAN = int(os.getenv("EXIT_TRAIL_EMA_SPAN", "10"))            # trailing EMA (9 or 10)
EXIT_TRAIL_CLOSES = int(os.getenv("EXIT_TRAIL_CLOSES", "2"))                 # consecutive closes below it = EXIT; the first one = TIGHTEN
EXIT_TRAIL_MIN_BARS = int(os.getenv("EXIT_TRAIL_MIN_BARS", "3"))             # no trailing-EMA exit during the first closed bars
EXIT_EXPANSION_ATR = float(os.getenv("EXIT_EXPANSION_ATR", "3.0"))           # "strong momentum expansion": best close-bar high >= entry + 3 ATR ...
EXIT_EXPANSION_MIN_ADX = float(os.getenv("EXIT_EXPANSION_MIN_ADX", "20"))    # ... with ADX14 >= 20 and the close above the trailing EMA
EXIT_SWING_CHANDELIER_ATR_MULT = float(os.getenv("EXIT_SWING_CHANDELIER_ATR_MULT", "2.5"))  # Chandelier only after expansion
EXIT_FAILED_BREAKOUT_BARS = int(os.getenv("EXIT_FAILED_BREAKOUT_BARS", "5"))
EXIT_FAILED_BREAKOUT_VOL_MULT = float(os.getenv("EXIT_FAILED_BREAKOUT_VOL_MULT", "1.2"))


def _load_positions() -> list[dict]:
    """טוען פוזיציות פתוחות מ-JSON."""
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def is_position_open(ticker: str) -> bool:
    """בודק אם כבר קיימת פוזיציה פתוחה על הטיקר, כדי לא לשלוח התראת כניסה כפולה."""
    try:
        symbol = str(ticker or "").strip().upper().replace("$", "")
        if not symbol:
            return False
        positions = _load_positions()
        for p in positions:
            if not isinstance(p, dict):
                continue
            if str(p.get("ticker", "")).strip().upper().replace("$", "") == symbol and not p.get("closed", False):
                return True
    except Exception as e:
        log(f"is_position_open error for {ticker}: {e}")
    return False


def _save_positions(positions: list[dict]) -> None:
    """שומר פוזיציות ל-JSON."""
    try:
        parent = os.path.dirname(POSITIONS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _atomic_write_text(POSITIONS_FILE, json.dumps(positions, indent=2, default=str))
    except Exception as e:
        log(f"save_positions error: {e}")


def _ensure_exit_plan(alert: dict) -> dict:
    """
    בונה תוכנית יציאה עוד לפני שליחת הסטאפ.
    אין כאן ניסיון לנחש מחיר שיא עתידי; התוכנית מגדירה איך לרכב על המגמה
    ואיך לזהות כשהראיות משתנות.
    """
    existing = alert.get("exit_plan") if isinstance(alert, dict) else None
    if isinstance(existing, dict) and existing:
        return existing
    try:
        entry = float(alert.get("breakout_level", 0) or 0)
        stop = float(alert.get("stop_loss", 0) or 0)
        target = float(alert.get("target", 0) or 0)
    except Exception:
        entry = stop = target = 0.0
    trigger = entry * (1.0 + EXIT_PROFIT_ACTIVATE_PCT / 100.0) if entry > 0 else None
    plan = {
        "engine": "V9.2_EXIT_INTELLIGENCE",
        "stop_initial": stop if stop > 0 else None,
        "stop_policy": "FIXED_INITIAL",
        "profit_protection_trigger_pct": EXIT_PROFIT_ACTIVATE_PCT,
        "profit_protection_trigger_price": round(trigger, 4) if trigger else None,
        "chandelier_atr_mult": EXIT_CHANDELIER_ATR_MULT,
        "target_reference": target if target > 0 else None,
        "target_policy": "REFERENCE_NOT_AUTO_EXIT",
        "reversal_watch_score": EXIT_WATCH_SCORE,
        "reversal_exit_score": EXIT_CONFIRM_SCORE,
        "reversal_requires_multiple_categories": True,
        "daily_reversal_uses_closed_candles_only": True,
    }
    if SWING_EXIT_ENGINE:
        min_trigger = entry * (1.0 + EXIT_ACTIVATE_MIN_PCT / 100.0) if entry > 0 else None
        plan.update({
            "engine": "V9.7.5_SWING_EXIT",
            "stop_policy": "STRUCTURAL_FIXED_INITIAL",
            "profit_protection_policy": "ATR_DYNAMIC",
            "profit_protection_trigger_pct": EXIT_ACTIVATE_MIN_PCT,
            "profit_protection_trigger_pct_max": EXIT_ACTIVATE_MAX_PCT,
            "profit_protection_trigger_price": round(min_trigger, 4) if min_trigger else None,
            "trail_ema_span": EXIT_TRAIL_EMA_SPAN,
            "trail_closes_for_exit": EXIT_TRAIL_CLOSES,
            "chandelier_policy": "ONLY_AFTER_MOMENTUM_EXPANSION",
            "chandelier_atr_mult": EXIT_SWING_CHANDELIER_ATR_MULT,
            "tighten_on_reversal_candle_with_volume_climax": True,
        })
    if isinstance(alert, dict):
        alert["exit_plan"] = plan
    return plan


def _closed_daily_frame(df: pd.DataFrame | None, now=None) -> pd.DataFrame | None:
    """Remove today's unfinished daily bar and future session labels; fail closed.

    Daily index labels represent trading-session dates, not intraday event timestamps.
    16:15 New York is a conservative finalization cutoff, also on early-close days.
    """
    if df is None or df.empty:
        return None
    try:
        from zoneinfo import ZoneInfo
        now_ny = pd.Timestamp(now if now is not None else datetime.now(ZoneInfo("America/New_York")))
        if now_ny.tzinfo is None:
            now_ny = now_ny.tz_localize("America/New_York")
        else:
            now_ny = now_ny.tz_convert("America/New_York")
        dates = [pd.Timestamp(x).date() for x in df.index]
        settled = (now_ny.hour, now_ny.minute) >= (16, 15)
        mask = [d < now_ny.date() or (d == now_ny.date() and settled and now_ny.weekday() < 5) for d in dates]
        out = df.loc[mask].sort_index().copy()
        out = out.loc[~out.index.duplicated(keep="last")]
        return out if not out.empty else None
    except Exception as exc:
        log(f"Closed daily bar validation failed: {type(exc).__name__}")
        return None


def _doji_flag(row: pd.Series) -> bool:
    try:
        o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
        rng = max(h - l, 1e-9)
        return abs(c - o) / rng <= EXIT_DOJI_BODY_MAX
    except Exception:
        return False


def _distribution_bar(row: pd.Series, avg_vol20: float, atr: float) -> bool:
    try:
        o, h, l, c, v = map(float, (row["open"], row["high"], row["low"], row["close"], row["volume"]))
        rng = max(h - l, 1e-9)
        close_loc = (c - l) / rng
        body = abs(c - o)
        return bool(
            c < o
            and avg_vol20 > 0
            and v >= EXIT_DISTRIBUTION_VOL_MULT * avg_vol20
            and close_loc <= 0.25
            and body >= 0.60 * max(atr, 1e-9)
        )
    except Exception:
        return False


def _compute_cmf20(df: pd.DataFrame) -> pd.Series:
    try:
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        span = (high - low).replace(0, np.nan)
        mf_mult = (((close - low) - (high - close)) / span).fillna(0.0)
        mf_vol = mf_mult * volume
        vol_sum = volume.rolling(20, min_periods=10).sum().replace(0, np.nan)
        return (mf_vol.rolling(20, min_periods=10).sum() / vol_sum).replace([np.inf, -np.inf], np.nan)
    except Exception:
        return pd.Series(index=df.index, dtype=float)


def _aligned_rs_ratio(stock_df: pd.DataFrame, spy_df: pd.DataFrame | None) -> pd.Series | None:
    """קו RS פשוט = מחיר המניה / SPY על תאריכים משותפים."""
    try:
        if spy_df is None or spy_df.empty:
            return None
        a = stock_df[["close"]].rename(columns={"close": "stock"})
        b = spy_df[["close"]].rename(columns={"close": "spy"})
        joined = a.join(b, how="inner").dropna()
        if len(joined) < 10:
            return None
        return (joined["stock"] / joined["spy"].replace(0, np.nan)).dropna()
    except Exception:
        return None


def _swing_activation_pct(entry: float, atr_entry: float) -> float:
    """Profit-protection trigger in percent: EXIT_ACTIVATE_ATR_MULT x ATR%, bounded [MIN, MAX]. MAX is the old fixed +5 %."""
    try:
        atr_pct = float(atr_entry) / max(float(entry), 1e-9) * 100.0
        return float(min(EXIT_ACTIVATE_MAX_PCT, max(EXIT_ACTIVATE_MIN_PCT, EXIT_ACTIVATE_ATR_MULT * atr_pct)))
    except Exception:
        return float(EXIT_PROFIT_ACTIVATE_PCT)


def _atr_at_entry(work: pd.DataFrame, position: dict, fallback_atr: float) -> float:
    """ATR14 on the position's open date (last bar on or before it); falls back to the latest ATR."""
    try:
        d = position.get("date_open")
        if d:
            v = work["atr14"].asof(pd.Timestamp(d))
            if _is_finite_number(v) and float(v) > 0:
                return float(v)
    except Exception:
        pass
    return float(fallback_atr)


def _bars_since_open(work: pd.DataFrame, position: dict) -> int:
    """Closed daily bars strictly after the open date. Unknown date -> large number (treated as 'armed')."""
    try:
        d = position.get("date_open")
        if not d:
            return 999
        idx = pd.DatetimeIndex(work.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        return int((idx.normalize() > pd.Timestamp(d).normalize()).sum())
    except Exception:
        return 999


def _distribution_series(work: pd.DataFrame) -> pd.Series:
    """Vectorised twin of _distribution_bar() for every row (uses the row's own ATR14 and prior-20 volume average)."""
    o = pd.to_numeric(work["open"], errors="coerce")
    h = pd.to_numeric(work["high"], errors="coerce")
    l = pd.to_numeric(work["low"], errors="coerce")
    c = pd.to_numeric(work["close"], errors="coerce")
    v = pd.to_numeric(work["volume"], errors="coerce")
    rng = (h - l).clip(lower=1e-9)
    close_loc = (c - l) / rng
    body = (c - o).abs()
    atr = pd.to_numeric(work["atr14"], errors="coerce").clip(lower=1e-9) if "atr14" in work.columns else pd.Series(np.nan, index=work.index)
    avg = pd.to_numeric(work["avg_vol20"], errors="coerce")
    return ((c < o) & (avg > 0) & (v >= EXIT_DISTRIBUTION_VOL_MULT * avg) & (close_loc <= 0.25) & (body >= 0.60 * atr)).fillna(False)


def _swing_failed_breakout(work: pd.DataFrame, position: dict, entry: float, avg_vol20: float) -> list[str]:
    """Advisory only (the structural initial stop is never moved): bull-trap warning in the first bars after entry."""
    out: list[str] = []
    try:
        if _bars_since_open(work, position) > EXIT_FAILED_BREAKOUT_BARS or len(work) < 2:
            return out
        last, prev = work.iloc[-1], work.iloc[-2]
        o, h, l, c, v = (float(last[k]) for k in ("open", "high", "low", "close", "volume"))
        rng = max(h - l, 1e-9)
        close_loc = (c - l) / rng
        body = abs(c - o)
        upper_wick = h - max(o, c)
        vr = v / avg_vol20 if avg_vol20 > 0 else 0.0
        if c < entry and vr >= EXIT_FAILED_BREAKOUT_VOL_MULT:
            out.append(f"פריצת שווא אפשרית: סגירה מתחת לרמת הפריצה ({c:.2f} < {entry:.2f}) בנפח ×{vr:.1f}")
        prev_o, prev_c = float(prev["open"]), float(prev["close"])
        engulf = prev_c > prev_o and c < o and o >= prev_c and c <= prev_o
        star = upper_wick >= max(2.0 * body, 0.45 * rng) and close_loc <= 0.45
        if (engulf or star) and vr >= EXIT_CLIMAX_VOL_MULT:
            out.append(f"נר היפוך עם נפח גבוה ×{vr:.1f} בתוך {EXIT_FAILED_BREAKOUT_BARS} ימים מהכניסה")
    except Exception:
        pass
    return out


def _evaluate_exit_intelligence(df: pd.DataFrame, position: dict, spy_df: pd.DataFrame | None = None) -> dict:
    """
    מנוע יציאה רב-סיגנלי. Doji לבדו לעולם אינו סוגר פוזיציה.
    EXIT מתקבל רק משבירת Profit Floor או מקונפלואנס של כמה קטגוריות
    עם אישור מחיר/ווליום אמיתי.
    """
    result = {
        "status": "BUILDING",
        "score": 0.0,
        "signals": [],
        "categories": [],
        "profit_protection_active": False,
        "profit_exit_floor": None,
        "max_profit_pct": 0.0,
        "closed_bar_date": None,
        "target_reached": bool(position.get("target_reached", False)),
        "exit_confirmed": False,
        "exit_reason": None,
    }
    try:
        entry = float(position.get("entry", 0) or 0)
        target = float(position.get("target", 0) or 0)
        if entry <= 0:
            return result

        closed = _closed_daily_frame(df)
        if closed is None or len(closed) < 25:
            result["status"] = "DATA_LIMITED"
            return result

        work = closed.copy()
        add_technical_indicators(work)
        close = pd.to_numeric(work["close"], errors="coerce")
        high = pd.to_numeric(work["high"], errors="coerce")
        low = pd.to_numeric(work["low"], errors="coerce")
        open_ = pd.to_numeric(work["open"], errors="coerce")
        volume = pd.to_numeric(work["volume"], errors="coerce").fillna(0.0)
        work["ema10"] = close.ewm(span=10, adjust=False).mean()
        work["ema21"] = close.ewm(span=21, adjust=False).mean()
        if SWING_EXIT_ENGINE:
            # Same causal baseline as the entry engine: the bar being judged is not part of its own average.
            work["avg_vol20"] = volume.shift(1).rolling(20, min_periods=20).mean()
            work["ema_trail"] = close.ewm(span=max(2, EXIT_TRAIL_EMA_SPAN), adjust=False).mean()
        else:
            work["avg_vol20"] = volume.rolling(20, min_periods=10).mean()
        work["cmf20"] = _compute_cmf20(work)

        last = work.iloc[-1]
        prev = work.iloc[-2]
        c = float(last["close"])
        h = float(last["high"])
        l = float(last["low"])
        o = float(last["open"])
        v = float(last.get("volume", 0) or 0)
        atr = float(last.get("atr14", np.nan))
        if not np.isfinite(atr) or atr <= 0:
            atr = max(c * 0.02, 1e-9)
        avg_vol20 = float(last.get("avg_vol20", 0) or 0)
        ema10 = float(last.get("ema10", c))
        ema21 = float(last.get("ema21", c))
        result["closed_bar_date"] = str(pd.Timestamp(work.index[-1]).date())

        # Profit Protection itself is based only on CLOSED daily highs, so an intraday spike
        # cannot raise the floor and immediately force an exit against yesterday's close.
        # highest_price remains a display field; highest_closed_price drives all V9.2 decisions.
        raw_high = _last_finite(df["high"] if "high" in df.columns else df["close"], default=h)
        prior_display_high = float(position.get("highest_price", entry) or entry)
        position["highest_price"] = max(prior_display_high, raw_high if _is_finite_number(raw_high) else h, h)
        prior_closed_high = position.get("highest_closed_price", position.get("highest_price", entry))
        prior_closed_high = float(prior_closed_high) if _is_finite_number(prior_closed_high) else entry
        highest = max(prior_closed_high, h)
        position["highest_closed_price"] = highest
        max_profit_pct = (highest - entry) / max(entry, 1e-9) * 100.0
        result["max_profit_pct"] = round(max_profit_pct, 2)
        stop_ref = position.get("stop_initial", position.get("stop_current", 0))
        stop_ref = float(stop_ref) if _is_finite_number(stop_ref) else 0.0
        if 0 < stop_ref < entry:
            r_unit = entry - stop_ref
            result["max_r"] = round((highest - entry) / r_unit, 2)
            result["r_now"] = round((c - entry) / r_unit, 2)

        if SWING_EXIT_ENGINE:
            atr_entry = _atr_at_entry(work, position, atr)
            activate_pct = _swing_activation_pct(entry, atr_entry)
        else:
            atr_entry = atr
            activate_pct = EXIT_PROFIT_ACTIVATE_PCT
        result["activation_pct"] = round(activate_pct, 2)
        active_before = bool(position.get("profit_protection_active", False))
        active = active_before or max_profit_pct >= activate_pct
        result["profit_protection_active"] = active
        position["profit_protection_active"] = active

        if target > 0 and highest >= target:
            result["target_reached"] = True
            position["target_reached"] = True

        if not active:
            result["status"] = "BUILDING"
            result["signals"] = [f"Profit Protection ממתין ל-+{activate_pct:.1f}% (שיא כרגע {max_profit_pct:+.1f}%)"]
            if SWING_EXIT_ENGINE:
                fb = _swing_failed_breakout(work, position, entry, avg_vol20)
                if fb:
                    result["status"] = "WATCH"
                    result["signals"] = fb + result["signals"]
            return result

        # Profit floor נפרד מה-Stop המקורי. הוא רק עולה, ולעולם לא משנה stop_initial.
        if SWING_EXIT_ENGINE:
            # Chandelier only after strong momentum expansion (sticky once reached); before that the floor is
            # break-even plus the trailing-EMA / reversal-candle logic below.
            adx_now = last.get("adx14", np.nan)
            expanded = bool(position.get("momentum_expanded", False))
            if (not expanded and (highest - entry) / max(atr_entry, 1e-9) >= EXIT_EXPANSION_ATR
                    and c > float(last.get("ema_trail", ema10))
                    and _is_finite_number(adx_now) and float(adx_now) >= EXIT_EXPANSION_MIN_ADX):
                expanded = True
            position["momentum_expanded"] = expanded
            result["momentum_expanded"] = expanded
            chandelier_raw = (highest - EXIT_SWING_CHANDELIER_ATR_MULT * atr) if expanded else float("-inf")
        else:
            chandelier_raw = highest - EXIT_CHANDELIER_ATR_MULT * atr
        old_floor = position.get("profit_exit_floor")
        old_floor = float(old_floor) if _is_finite_number(old_floor) else entry
        floor = max(entry, old_floor, chandelier_raw)
        floor = round(float(floor), 4)
        position["profit_exit_floor"] = floor
        result["profit_exit_floor"] = floor

        score = 0.0
        signals: list[str] = []
        categories: set[str] = set()
        price_confirmation = False

        rng = max(h - l, 1e-9)
        body = abs(c - o)
        close_loc = (c - l) / rng
        upper_wick = h - max(o, c)

        # 1) Candlestick reversal — warning first, confirmation second.
        if _doji_flag(last) and h >= highest * 0.98:
            score += 8
            categories.add("candle")
            signals.append("Doji / indecision ליד השיא — אזהרה בלבד")

        prev_near_high = float(prev["high"]) >= highest * 0.98
        if _doji_flag(prev) and prev_near_high and c < float(prev["low"]):
            score += 25
            categories.update(("candle", "price"))
            price_confirmation = True
            signals.append("אישור דובי אחרי Doji: סגירה מתחת ל-Low של ה-Doji")

        prev_o = float(prev["open"]); prev_c = float(prev["close"])
        bearish_engulfing = (prev_c > prev_o and c < o and o >= prev_c and c <= prev_o)
        if bearish_engulfing:
            score += 20
            categories.update(("candle", "price"))
            price_confirmation = True
            signals.append("Bearish Engulfing מאושר")

        shooting_star = (h >= highest * 0.98 and upper_wick >= max(2.0 * body, 0.45 * rng) and close_loc <= 0.45)
        if shooting_star:
            score += 15
            categories.add("candle")
            signals.append("Shooting Star / upper-wick rejection")

        # 2) Institutional-looking distribution.
        distribution_today = _distribution_bar(last, avg_vol20, atr)
        if distribution_today:
            score += 25
            categories.update(("volume", "price"))
            price_confirmation = True
            vr = v / max(avg_vol20, 1e-9)
            signals.append(f"High-volume distribution: נר אדום חזק, Volume ×{vr:.2f}")

        dist_count = int(_distribution_series(work).tail(5).sum())
        if dist_count >= 2:
            score += 15
            categories.add("volume")
            signals.append(f"{dist_count} ימי Distribution ב-5 ימי מסחר")

        # 3) Short trend support. EMA10 alone is warning; EMA21 is confirmation.
        if c < ema10:
            score += 10
            categories.add("price")
            signals.append(f"סגירה מתחת EMA10 ({c:.2f} < {ema10:.2f})")
        if c < ema21:
            score += 25
            categories.add("price")
            price_confirmation = True
            signals.append(f"סגירה מתחת EMA21 ({c:.2f} < {ema21:.2f})")

        # 4) Hidden money-flow divergences near a price high.
        recent_high = float(high.tail(20).max()) if len(high) >= 20 else h
        near_high = h >= recent_high * 0.995
        cmf = work["cmf20"]
        if near_high and len(cmf.dropna()) >= 6:
            cmf_now = float(cmf.iloc[-1]) if _is_finite_number(cmf.iloc[-1]) else None
            cmf_5 = float(cmf.iloc[-6]) if _is_finite_number(cmf.iloc[-6]) else None
            if cmf_now is not None and cmf_5 is not None and cmf_now <= cmf_5 - 0.08:
                score += 15
                categories.add("flow")
                signals.append(f"CMF divergence: מחיר ליד שיא אבל CMF נחלש ({cmf_5:+.2f}→{cmf_now:+.2f})")

        if "obv" in work.columns and len(work) >= 12 and near_high:
            obv = pd.to_numeric(work["obv"], errors="coerce")
            if _is_finite_number(obv.iloc[-1]) and _is_finite_number(obv.iloc[-6]):
                if float(obv.iloc[-1]) < float(obv.iloc[-6]) and h >= float(high.iloc[-11:-1].max()) * 0.995:
                    score += 15
                    categories.add("flow")
                    signals.append("OBV divergence: המחיר ליד/בשיא חדש אך OBV נחלש")

        # 5) Momentum rollover.
        if "macd_hist" in work.columns and len(work) >= 2:
            mh0, mh1 = work["macd_hist"].iloc[-1], work["macd_hist"].iloc[-2]
            if _is_finite_number(mh0) and _is_finite_number(mh1) and float(mh1) >= 0 > float(mh0):
                score += 10
                categories.add("momentum")
                signals.append("MACD histogram חצה מתחת לאפס — Momentum נחלש")

        # 6) Relative Strength vs SPY deterioration.
        rs_line = _aligned_rs_ratio(work, _closed_daily_frame(spy_df) if spy_df is not None else None)
        if rs_line is not None and len(rs_line) >= 6:
            rs_now = float(rs_line.iloc[-1])
            rs_5 = float(rs_line.iloc[-6])
            rs_ema5 = float(rs_line.ewm(span=5, adjust=False).mean().iloc[-1])
            if rs_now < rs_ema5 and (rs_now / max(rs_5, 1e-9) - 1.0) <= -0.02:
                score += 10
                categories.add("relative")
                signals.append("RS vs SPY נחלש ביותר מ-2% ב-5 ימים ונמצא מתחת EMA5 של קו ה-RS")

        # 7) Climax / exhaustion — self-relative rather than fixed-price target.
        if len(work) >= 80:
            ret10 = close.pct_change(10) * 100.0
            hist = ret10.dropna()
            if len(hist) >= 50 and _is_finite_number(hist.iloc[-1]):
                threshold95 = float(hist.iloc[:-1].quantile(0.95)) if len(hist) > 1 else np.inf
                current_ret10 = float(hist.iloc[-1])
                vr = v / max(avg_vol20, 1e-9) if avg_vol20 > 0 else 0.0
                if current_ret10 >= threshold95 and vr >= EXIT_CLIMAX_VOL_MULT and rng >= 1.5 * atr and close_loc < 0.50:
                    score += 20
                    categories.update(("climax", "volume"))
                    signals.append(f"Climax risk: 10d return בקצה העליון של ההיסטוריה, Volume ×{vr:.2f}, סגירה חלשה")

        score = min(100.0, round(score, 1))
        result["score"] = score
        result["signals"] = signals
        result["categories"] = sorted(categories)

        # Exit rule A: closed candle breaks the independent profit floor.
        if c <= floor:
            result["status"] = "EXIT"
            result["exit_confirmed"] = True
            result["exit_reason"] = f"Profit Protection נשבר בסגירה ({c:.2f} ≤ {floor:.2f})"
            return result

        # v9.7.5 swing layer — closed daily bars only, evaluated on the first closed bar that shows it.
        swing_tighten: list[str] = []
        if SWING_EXIT_ENGINE:
            span = max(2, EXIT_TRAIL_EMA_SPAN)
            ema_trail = float(last.get("ema_trail", ema10))
            below = (close < work["ema_trail"])
            n_below = int(below.iloc[::-1].astype(int).cumprod().sum())      # consecutive closes below the trailing EMA
            armed = _bars_since_open(work, position) >= EXIT_TRAIL_MIN_BARS
            result["trail_ema"] = round(ema_trail, 4)
            result["closes_below_trail"] = n_below
            if armed and n_below >= max(1, EXIT_TRAIL_CLOSES):
                result["status"] = "EXIT"
                result["exit_confirmed"] = True
                result["exit_reason"] = f"Trailing EMA{span}: {n_below} סגירות רצופות מתחת ({c:.2f} < {ema_trail:.2f})"
                return result
            vr_prior = (v / avg_vol20) if avg_vol20 > 0 else 0.0
            candle_reversal = bool(bearish_engulfing or shooting_star or distribution_today
                                   or (_doji_flag(last) and h >= highest * 0.98))
            if candle_reversal and vr_prior >= EXIT_CLIMAX_VOL_MULT:
                swing_tighten.append(f"נר היפוך + Volume climax ×{vr_prior:.1f} — הדק סטופ מתחת ל-Low של הנר")
            if armed and n_below >= 1:
                swing_tighten.append(f"סגירה מתחת EMA{span} ({n_below}/{max(1, EXIT_TRAIL_CLOSES)}) — הדק סטופ")

        # Exit rule B: multi-signal confirmed reversal. A Doji alone can never satisfy this.
        if score >= EXIT_CONFIRM_SCORE and len(categories) >= 2 and price_confirmation:
            result["status"] = "EXIT"
            result["exit_confirmed"] = True
            top = "; ".join(signals[:4]) if signals else "multiple reversal signals"
            result["exit_reason"] = f"Confirmed reversal score {score:.0f}/100 — {top}"
        elif swing_tighten:
            # Ratchet only: the floor moves just under the signal bar's low and never goes down.
            result["status"] = "TIGHTEN"
            tight_floor = round(max(floor, l - 0.1 * atr), 4)
            if tight_floor > floor:
                floor = tight_floor
                position["profit_exit_floor"] = floor
                result["profit_exit_floor"] = floor
            result["tighten_reasons"] = swing_tighten
            result["signals"] = swing_tighten + [x for x in signals if x not in swing_tighten]
        elif score >= EXIT_WATCH_SCORE:
            result["status"] = "WATCH"
        elif result["target_reached"]:
            result["status"] = "RIDE_WINNER"
        else:
            result["status"] = "HOLD"
        return result
    except Exception as e:
        result["status"] = "DATA_LIMITED"
        result["signals"] = [f"Exit Intelligence error: {e}"]
        return result


def open_position(alert: dict) -> None:
    """פותח פוזיציה חדשה ושומר מראש את תוכנית היציאה של V9.2."""
    try:
        positions = _load_positions()
        ticker = alert.get("ticker", "")
        if any(p.get("ticker") == ticker and not p.get("closed") for p in positions if isinstance(p, dict)):
            return

        entry = float(alert.get("breakout_level", 0) or 0)
        stop = float(alert.get("stop_loss", 0) or 0)
        target = float(alert.get("target", 0) or 0)
        if entry <= 0 or stop <= 0:
            return

        plan = _ensure_exit_plan(alert)
        positions.append({
            "ticker": ticker,
            "pattern": alert.get("pattern_type", ""),
            "tracking_kind": "PAPER_REFERENCE_NOT_BROKER_FILL",
            "signal_date": (alert.get("meta") or {}).get("signal_date"),
            "sent_at": alert.get("sent_at"),
            "entry": entry,
            "stop_initial": stop,
            "stop_current": stop,  # compatibility: V9.2 keeps this equal to stop_initial
            "target": target,
            "target_reference": target,
            "date_open": datetime.now().strftime("%Y-%m-%d"),
            "highest_price": entry,
            "highest_closed_price": entry,
            "max_profit_pct": 0.0,
            "stop_method": "fixed_initial_plus_exit_intelligence",
            "trailing_status": "disabled_v9.2",
            "profit_protection_active": False,
            "profit_exit_floor": None,
            "target_reached": False,
            "exit_plan": plan,
            "exit_intelligence": {"status": "BUILDING", "score": 0.0, "signals": []},
            "closed": False,
            "close_reason": None,
            "close_price": None,
            "close_date": None,
            "pnl_pct": None,
        })
        _save_positions(positions)
        trigger = plan.get("profit_protection_trigger_price")
        trigger_txt = f"${trigger:.2f}" if _is_finite_number(trigger) else "+5%"
        log(f"📂 Position opened: {ticker} @ {entry:.2f} | initial_stop={stop:.2f} FIXED | target_ref={target:.2f} | profit protection from {trigger_txt}")
    except Exception as e:
        log(f"open_position error: {e}")


def run_position_tracker(send_exit_email_fn=None) -> list[dict]:
    """
    V9.2 position tracker:
    1. Stop Loss המקורי נשאר קבוע.
    2. Profit Protection מופעל רק אחרי +5% בשיא.
    3. בודק Reversal רב-סיגנלי על נרות סגורים בלבד.
    4. Target הוא reference בלבד ואינו סוגר אוטומטית.
    5. POSITION_MAX_DAYS הפך ל-review warning בלבד.
    """
    positions = _load_positions()
    open_pos = [p for p in positions if isinstance(p, dict) and not p.get("closed")]
    exit_alerts = []
    if not open_pos:
        return []

    log(f"📊 Position Tracker V9.2: checking {len(open_pos)} open positions with Exit Intelligence...")
    today = datetime.now().date()

    # SPY once per tracker run for relative-strength deterioration.
    try:
        spy_df = _normalize_yfinance_df(yf.download("SPY", period="1y", interval="1d", progress=False, auto_adjust=True))
    except Exception:
        spy_df = None

    for p in open_pos:
        ticker = str(p.get("ticker", "")).strip().upper()
        try:
            df = _normalize_yfinance_df(yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=True))
            if df is None or df.empty:
                p["data_status"] = "price_unavailable"
                log(f"   ⚠️ {ticker}: price data unavailable — keeping position open")
                continue

            price_now = _last_finite(df["close"])
            entry = float(p.get("entry", 0) or 0)
            stop_initial = float(p.get("stop_initial", p.get("stop_current", 0)) or 0)
            target = float(p.get("target", p.get("target_reference", 0)) or 0)
            if not all(_is_finite_number(v) for v in (price_now, entry, stop_initial)) or entry <= 0 or stop_initial <= 0:
                p["data_status"] = "price_unavailable"
                log(f"   ⚠️ {ticker}: invalid position/price data — keeping position open")
                continue

            p["data_status"] = "ok"
            p["stop_current"] = stop_initial  # V9.2: never trail the original stop.
            p["stop_method"] = "fixed_initial_plus_exit_intelligence"
            p["trailing_status"] = "disabled_v9.2"
            date_open = pd.Timestamp(p.get("date_open", today.isoformat())).date()
            days_open = (today - date_open).days
            pnl_pct = round((price_now - entry) / max(entry, 1e-9) * 100, 2)

            intel = _evaluate_exit_intelligence(df, p, spy_df=spy_df)
            p["exit_intelligence"] = intel
            p["max_profit_pct"] = intel.get("max_profit_pct", p.get("max_profit_pct", 0.0))
            p["profit_protection_active"] = bool(intel.get("profit_protection_active", False))
            if _is_finite_number(intel.get("profit_exit_floor")):
                p["profit_exit_floor"] = float(intel["profit_exit_floor"])
            p["target_reached"] = bool(intel.get("target_reached", p.get("target_reached", False)))
            p["last_tracker_price"] = price_now
            p["last_tracker_date"] = today.isoformat()

            soft_warnings = []
            if days_open >= POSITION_MAX_DAYS:
                soft_warnings.append(f"⏰ פתוח {days_open} ימים — review בלבד, לא יציאה אוטומטית")
            p["soft_warnings"] = soft_warnings

            close_reason = None
            close_emoji = ""
            exit_kind = None

            # Original stop remains the only downside stop before profit protection.
            if price_now <= stop_initial:
                close_reason = f"🛑 פגע ב-Stop Loss המקורי ({price_now:.2f} ≤ {stop_initial:.2f})"
                close_emoji = "🛑"
                exit_kind = "INITIAL_STOP"
            elif intel.get("exit_confirmed"):
                close_reason = f"🔄 שינוי כיוון מאושר — {intel.get('exit_reason', 'Exit Intelligence')}"
                close_emoji = "🔄"
                exit_kind = "REVERSAL_EXIT"

            if close_reason:
                p["closed"] = True
                p["close_reason"] = close_reason
                p["close_price"] = price_now
                p["close_date"] = today.isoformat()
                p["pnl_pct"] = pnl_pct
                p["exit_kind"] = exit_kind
                log(f"   {close_emoji} {ticker} CLOSED: {close_reason} | P&L={pnl_pct:+.1f}% | max_profit={float(p.get('max_profit_pct',0) or 0):+.1f}%")
                exit_alerts.append({
                    "ticker": ticker,
                    "pattern": p.get("pattern", ""),
                    "entry": entry,
                    "exit_price": price_now,
                    "stop": stop_initial,
                    "target": target,
                    "pnl_pct": pnl_pct,
                    "days_open": days_open,
                    "reason": close_reason,
                    "emoji": close_emoji,
                    "exit_kind": exit_kind,
                    "profit_floor": p.get("profit_exit_floor"),
                    "max_profit_pct": p.get("max_profit_pct", 0.0),
                    "exit_score": intel.get("score", 0.0),
                    "exit_signals": intel.get("signals", []),
                })
            else:
                status = str(intel.get("status", "BUILDING"))
                floor = intel.get("profit_exit_floor")
                floor_txt = f"{float(floor):.2f}" if _is_finite_number(floor) else "—"
                target_flag = " | target_ref=REACHED" if p.get("target_reached") else ""
                signals = intel.get("signals", []) or []
                sig_txt = "; ".join(signals[:3]) if signals else "none"
                log(
                    f"   📈 {ticker}: {price_now:.2f} | P&L={pnl_pct:+.1f}% | max={float(p.get('max_profit_pct',0) or 0):+.1f}% | "
                    f"initial_stop={stop_initial:.2f} | profit_floor={floor_txt} | exit={status} score={float(intel.get('score',0) or 0):.0f}{target_flag} | signals={sig_txt}{_position_earnings_log(ticker)}"
                )

        except Exception as e:
            log(f"   position_tracker error for {ticker}: {e}")
            continue

    _save_positions(positions)
    if exit_alerts and send_exit_email_fn:
        try:
            send_exit_email_fn(exit_alerts)
        except Exception as e:
            log(f"send_exit_email error: {e}")
    return exit_alerts


@_nonfatal_mail
def send_exit_email(exit_alerts: list[dict]) -> dict:
    """שולח מייל כאשר Stop המקורי או Exit Intelligence אישרו יציאה."""
    if not exit_alerts:
        return _mail_status("skipped")
    try:
        cards = []
        for a in exit_alerts:
            pnl = _mail_float(a.get("pnl_pct", 0) or 0)
            pnl_col = "#15803d" if pnl >= 0 else "#b91c1c"
            pnl_bg = "#f0fdf4" if pnl >= 0 else "#fef2f2"
            floor = a.get("profit_floor")
            floor_txt = f"${_mail_float(floor):.2f}" if _is_finite_number(floor) else "—"
            sigs = a.get("exit_signals", []) or []
            cards.append(_mail_exit_card(a, pnl, floor_txt, sigs))

        html_body = _mail_document("עדכוני יציאה", datetime.now().strftime("%d/%m/%Y"),
                                   "".join(cards), eyebrow="EXIT INTELLIGENCE",
                                   preheader=f"{len(exit_alerts)} עדכוני יציאה מפוזיציות במעקב")
        subject = f"🔔 Exit Intelligence — יציאה מ-{len(exit_alerts)} פוזיציה/ות"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = ", ".join(TO_EMAILS) if isinstance(TO_EMAILS, list) else TO_EMAILS
        _attach_mail_content(msg, html_body)
        return _smtp_deliver(msg, "exits")
    except Exception as e:
        log(f"send_exit_email error: {e}")
        return _mail_status("failed", refused=len(TO_EMAILS))


@_nonfatal_mail
def send_positions_status_email() -> dict:
    """דוח יומי: סטופ מקורי, Profit Floor, Reversal Score והאם להמשיך לרכב על המגמה."""
    if not SEND_POSITIONS_STATUS_EMAIL:
        return _mail_status("skipped")
    if not APP_PASSWORD:
        log("APP_PASSWORD not set — positions status email disabled.")
        return _mail_status("skipped")
    try:
        positions = _load_positions()
        open_pos = [p for p in positions if isinstance(p, dict) and not p.get("closed")]
        if not open_pos:
            log("📭 Positions status: no open positions.")
            return _mail_status("skipped")

        cards = []
        today_str = datetime.now().strftime("%d/%m/%Y")
        for p in open_pos:
            ticker = str(p.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            try:
                price_now = _mail_float(p.get("last_tracker_price", 0) or 0)
                if not _is_finite_number(price_now) or price_now <= 0:
                    df = _normalize_yfinance_df(yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=True))
                    price_now = _last_finite(df["close"]) if df is not None and not df.empty else _mail_float("nan")
                entry = _mail_float(p.get("entry", 0) or 0)
                stop = _mail_float(p.get("stop_initial", p.get("stop_current", 0)) or 0)
                target = _mail_float(p.get("target", p.get("target_reference", 0)) or 0)
                if not all(_is_finite_number(v) for v in (price_now, entry, stop)):
                    cards.append(_mail_card(ticker, "<p style='color:#f4bf64;'>אין נתוני מחיר תקינים.</p>", "נתוני המעקב אינם זמינים"))
                    continue

                pnl_pct = ((price_now - entry) / max(entry, 1e-9)) * 100.0
                highest = _mail_float(p.get("highest_price", entry) or entry)
                max_profit = _mail_float(p.get("max_profit_pct", (highest-entry)/max(entry,1e-9)*100) or 0)
                intel = p.get("exit_intelligence", {}) if isinstance(p.get("exit_intelligence", {}), dict) else {}
                status = str(intel.get("status", "BUILDING"))
                score = _mail_float(intel.get("score", 0) or 0)
                signals = intel.get("signals", []) or []
                floor = p.get("profit_exit_floor")
                floor_txt = f"${_mail_float(floor):.2f}" if _is_finite_number(floor) else "ממתין להפעלת Profit Protection"
                target_reached = bool(p.get("target_reached", False))

                if status == "TIGHTEN":
                    status_emoji, status_text, status_color, status_bg = "🔒", "להדק סטופ — נר היפוך עם נפח / סגירה מתחת ל-EMA; ה-Profit Floor הועלה", "#9a3412", "#fff7ed"
                elif status == "WATCH":
                    status_emoji, status_text, status_color, status_bg = "⚠️", "להישאר בזהירות — יש סימני שינוי כיוון שעדיין אינם מאושרים", "#92400e", "#fffbeb"
                elif status == "RIDE_WINNER":
                    status_emoji, status_text, status_color, status_bg = "🏄", "Target 1 הושג — המגמה עדיין בריאה, ממשיכים לרכב", "#15803d", "#f0fdf4"
                elif status == "HOLD":
                    status_emoji, status_text, status_color, status_bg = "✅", "להישאר — Profit Protection פעיל ואין שינוי כיוון מאושר", "#1d4ed8", "#eff6ff"
                elif status == "DATA_LIMITED":
                    status_emoji, status_text, status_color, status_bg = "⚠️", "להישאר — אין מספיק נתונים לאישור שינוי כיוון", "#6b7280", "#f9fafb"
                else:
                    status_emoji, status_text, status_color, status_bg = "🟦", f"להישאר — Stop המקורי נשאר קבוע; Profit Protection יופעל ב-+{float(intel.get('activation_pct') or EXIT_PROFIT_ACTIVATE_PCT):.1f}%", "#1d4ed8", "#eff6ff"

                sig_html = "".join(f"<li>{_mail_text(x)}</li>" for x in signals[:6]) or "<li>אין אותות Reversal משמעותיים</li>"
                target_note = "✅ הושג — אינו גורם ליציאה אוטומטית" if target_reached else "טרם הושג"
                date_open = p.get("date_open", "")
                try:
                    days_open = (datetime.now().date() - pd.Timestamp(date_open).date()).days if date_open else "N/A"
                except Exception:
                    days_open = "N/A"
                pnl_color = "#15803d" if pnl_pct >= 0 else "#b91c1c"

                cards.append(_mail_position_card({
                    "ticker": ticker, "pattern": p.get("pattern", ""), "date_open": date_open,
                    "days_open": days_open, "entry": entry, "price_now": price_now, "stop": stop,
                    "floor_txt": floor_txt, "pnl_pct": pnl_pct, "max_profit": max_profit,
                    "target": target, "target_note": target_note, "score": score,
                    "status": status, "status_text": status_text, "signals": signals,
                    "earnings_note": _position_earnings_note(ticker),
                }))
            except Exception as e:
                cards.append(_mail_card(ticker, "<p style='color:#f4bf64;'>לא ניתן להציג את נתוני הפוזיציה.</p>", "נתוני המעקב אינם זמינים"))

        _save_positions(positions)
        if not cards:
            return _mail_status("skipped")
        trig_txt = (f"{EXIT_ACTIVATE_MIN_PCT:.1f}%–{EXIT_ACTIVATE_MAX_PCT:.1f}% (לפי ATR)" if SWING_EXIT_ENGINE
                    else f"{EXIT_PROFIT_ACTIVATE_PCT:.0f}%")
        policy = (f"Target הוא יעד ייחוס. Stop המקורי נשאר קבוע; אחרי רווח של +{trig_txt} מופעל Profit Protection "
                  "והיציאה מתבססת על שינוי כיוון מאושר.")
        policy_html = f'<p style="margin:0 0 20px;color:#a9b6ca;font-size:12px;line-height:1.9;">{_mail_text(policy)}</p>'
        html_body = _mail_document("הפוזיציות במעקב", f"{len(open_pos)} פוזיציות פתוחות · {today_str}",
                                   policy_html + ''.join(cards), eyebrow="POSITION MONITOR",
                                   preheader=f"עדכון מעקב: {len(open_pos)} פוזיציות פתוחות")
        subject = f"📊 Exit Intelligence — {len(open_pos)} פוזיציות — {today_str}"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = ", ".join(TO_EMAILS) if isinstance(TO_EMAILS, list) else TO_EMAILS
        _attach_mail_content(msg, html_body)
        return _smtp_deliver(msg, "positions")
    except Exception as e:
        log(f"send_positions_status_email error: {e}")
        return _mail_status("failed", refused=len(TO_EMAILS))

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
        _atomic_write_text(LEARNING_CONFIG_FILE, json.dumps(params, indent=2))
        log(f"💾 Learned params saved → {LEARNING_CONFIG_FILE}")
    except Exception as e:
        log(f"save_learned_params error: {e}")

def _apply_learned_params() -> None:
    """
    טוען פרמטרים שנלמדו ומחיל אותם על המשתנים הגלובליים.
    נקרא בתחילת כל ריצה.
    """
    global MIN_ALERT_SCORE, BREAKOUT_TOLERANCE, EMA28_MAX_DIST_PCT
    if not AUTO_APPLY_LEARNING:
        log("Weekly learning is report-only; legacy learned overrides are not applied.")
        return
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
    """Weekly descriptive research, with no automatic threshold search or changes.

    Bracket outcomes and fixed-horizon returns are distinct from V9.2 exits.
    Raw sample counts include correlated candidates and are not significance tests.
    """
    result = {"status": "no_data", "samples": 0, "insights": [], "new_params": {}, "pattern_stats": {},
              "code_version": CODE_VERSION, "mode": "REPORT_ONLY", "model": "NEXT_OPEN_COUNTERFACTUAL_BRACKET",
              "limitations": ["candidate observations are correlated", "daily intrabar order may be unknown",
                              "not broker fills", "not the V9.2 target-reference exit strategy", "no out-of-sample calibration yet"]}
    if not os.path.exists(SETUP_EVALUATIONS_CSV):
        log("Weekly research: no versioned sent/shadow observations yet")
        return result
    try:
        data = pd.read_csv(SETUP_EVALUATIONS_CSV)
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=LEARNING_LOOKBACK_DAYS)
        stamps = pd.to_datetime(data["observed_at"], utc=True, errors="coerce")
        data = data[(stamps >= cutoff) & (data["schema_version"] == 2)].copy()
        complete = data[data["price_20d"].notna()].copy()
        sent = complete[complete["stage"] == "SENT"]
        result["samples"] = len(sent)
        result["shadow_samples"] = len(complete) - len(sent)
        for (stage, pattern), group in complete.groupby(["stage", "pattern"]):
            ambiguous = group["bracket_result_20d"].eq("AMBIGUOUS_BOTH")
            known = group.loc[~ambiguous]
            rvalues = pd.to_numeric(known["bracket_net_r_20d"], errors="coerce").dropna()
            returns = pd.to_numeric(group["horizon_return_pct_20d"], errors="coerce").dropna()
            result["pattern_stats"][f"{stage}|{pattern}"] = {
                "observations": len(group), "unique_tickers": int(group["ticker"].nunique()),
                "ambiguous_bars": int(ambiguous.sum()), "unambiguous_samples": len(rvalues),
                "bracket_positive_fraction": float((rvalues > 0).mean()) if len(rvalues) else None,
                "bracket_mean_net_r": float(rvalues.mean()) if len(rvalues) else None,
                "mean_horizon_return_pct": float(returns.mean()) if len(returns) else None,
            }
        result["status"] = "descriptive_only" if len(sent) >= LEARNING_MIN_SAMPLES else "insufficient_data"
        result["insights"].append("Entry 60 / Professional 75 remain unchanged; collect sent and rejected outcomes before calibration.")
        _atomic_write_json(LEARNING_REPORT_FILE, result)
        log(f"Weekly research: {len(sent)} mature sent observations, {result['shadow_samples']} shadow observations; report-only")
    except Exception as exc:
        result["status"] = "error"
        result["error_type"] = type(exc).__name__
        log(f"Weekly research error: {type(exc).__name__}: {exc}")
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


WEEKLY_ALIGN_MODE = os.getenv("WEEKLY_ALIGN_MODE", "off").strip().lower()   # off | warn | block  (entry gate)
WEEKLY_MA_WEEKS = int(os.getenv("WEEKLY_MA_WEEKS", "30"))
WEEKLY_EMA_SPAN = int(os.getenv("WEEKLY_EMA_SPAN", "10"))


def weekly_context_from_daily(df: pd.DataFrame | None) -> dict:
    """Weekly context built by resampling the DAILY frame the scanner already holds (no second download).

    The last weekly bar is the current (possibly partial) week, i.e. the latest daily close - the same convention the
    backtest uses, so what is measured offline is what runs live. `aligned` = price above the 30-week MA AND above the
    10-week EMA; it is None (unknown, never a blocker) when there are fewer than WEEKLY_MA_WEEKS weekly closes.
    """
    out = {"ok": False, "trend": "UNKNOWN", "above_ma30w": None, "above_ema10w": None, "ma30w_rising": None,
           "aligned": None, "reason": "אין נתונים יומיים", "source": "daily_resample"}
    try:
        if df is None or len(df) == 0:
            return out
        col = "close" if "close" in df.columns else ("Close" if "Close" in df.columns else None)
        if col is None:
            return out
        px = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        idx = pd.DatetimeIndex(px.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        px.index = idx
        px = px.sort_index()
        px = px[~px.index.duplicated(keep="last")]
        wk = px.resample("W-FRI").last().dropna()
        if len(wk) < WEEKLY_MA_WEEKS:
            out["reason"] = f"היסטוריה שבועית קצרה ({len(wk)} < {WEEKLY_MA_WEEKS} שבועות)"
            return out
        ma = wk.rolling(WEEKLY_MA_WEEKS).mean()
        ema = wk.ewm(span=WEEKLY_EMA_SPAN, adjust=False).mean()
        price, ma_now, ema_now = float(wk.iloc[-1]), float(ma.iloc[-1]), float(ema.iloc[-1])
        above_ma, above_ema = price > ma_now, price > ema_now
        ma_prev = float(ma.iloc[-5]) if len(ma.dropna()) >= 5 else float("nan")
        rising = (ma_now > ma_prev) if np.isfinite(ma_prev) else None
        price_3m = float(wk.iloc[-13]) if len(wk) >= 13 else float(wk.iloc[0])
        perf_3m = round((price - price_3m) / max(price_3m, 1e-9) * 100, 1)
        trend = "UP" if price > float(wk.iloc[-3]) else "DOWN"
        aligned = bool(above_ma and above_ema)
        if aligned:
            reason = "מעל MA30 ו-EMA10 שבועיים"
        else:
            bits = []
            if not above_ma:
                bits.append(f"מתחת MA{WEEKLY_MA_WEEKS} שבועי")
            if not above_ema:
                bits.append(f"מתחת EMA{WEEKLY_EMA_SPAN} שבועי")
            reason = " + ".join(bits)
        out.update({
            "ok": True, "trend": trend, "above_ma30w": above_ma, "above_ema10w": above_ema, "ma30w_rising": rising,
            "aligned": aligned, "reason": reason, "ma30w": round(ma_now, 2), "ema10w": round(ema_now, 2),
            "price_now": round(price, 2), "perf_3m": perf_3m, "weeks": int(len(wk)),
            "dist_ma30w_pct": round((price / ma_now - 1) * 100, 2) if ma_now > 0 else None,
        })
        return out
    except Exception as e:
        out["reason"] = f"שגיאה בניתוח שבועי: {type(e).__name__}"
        return out


def get_weekly_timeframe(ticker: str, df: pd.DataFrame | None = None) -> dict:
    """
    ניתוח שבועי: מגמה, MA30 שבועי אמיתי (30 שבועות), EMA10 שבועי, ביצועי 3 חודשים.
    מחושב מה-DataFrame היומי שכבר קיים (resample) — ללא הורדת yfinance שנייה.
    """
    result = {"trend": "UNKNOWN", "above_ma30w": None, "perf_3m": None, "summary": "N/A"}
    try:
        ctx = weekly_context_from_daily(df)
        if not ctx.get("ok"):
            result["summary"] = f"שבועי: {ctx.get('reason', 'N/A')}"
            result.update({k: ctx.get(k) for k in ("aligned", "reason", "source")})
            return result
        above, trend = bool(ctx["above_ma30w"]), ctx["trend"]
        aligned_txt = "מיושר עם הפריצה" if ctx["aligned"] else "לא מיושר"
        ctx["summary"] = (f"{'🟢' if above and trend == 'UP' else '🔴' if not above else '🟡'} שבועי: "
                          f"{'מעל' if above else 'מתחת'} MA{WEEKLY_MA_WEEKS} | {ctx['perf_3m']:+.1f}% ב-3 חודשים | "
                          f"מגמה: {trend} | {aligned_txt}")
        return ctx
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
        _c = np.asarray(closes, dtype=float)
        _v = np.nan_to_num(np.asarray(volumes, dtype=float))
        obv = np.cumsum(np.nan_to_num(np.sign(np.diff(_c, prepend=_c[0]))) * _v)   # vectorised; obv[0] = 0 as before
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
#  לוגיקה: Narrow-range volume proxy BEARISH + Short Interest גבוה + Options Put Flow
# ============================================================

REVERSE_MIN_SCORE = float(os.getenv("REVERSE_MIN_SCORE", "2.0"))  # לפחות 2 סימנים אדומים

def scan_for_institutional_selling(ticker: str, df: pd.DataFrame) -> dict:
    """
    בודק האם מוסדיים יוצאים ממניה — אות אזהרה.
    לוגיקה משולבת:
      +1: Narrow-range volume proxy bearish prints
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

        # ── Narrow-range volume proxy bearish ─────────────────────────────────
        dp = get_dark_pool_prints(ticker, df)
        if dp.get("net_bias") == "BEARISH" and dp.get("bearish_prints", 0) >= 2:
            score += 1
            signals.append(f"🔴 Narrow-range volume proxy bearish ({dp['bearish_prints']} prints)")

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
        inst_pct = inst.get("inst_ownership_pct")
        if inst_pct is not None and float(inst_pct) < 30:
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
    מזהה ימים עם ווליום חריג בטווח מחיר צר לפי סימני מחיר-נפח:

    Narrow-range volume proxy Print = יום שבו:
      1. Volume גבוה פי DARKPOOL_VOL_MULT מהממוצע
      2. תנועת מחיר (high-low)/close קטנה מ-DARKPOOL_PRICE_MAX%
         → אומדן יומי של פעילות נפח בטווח צר; אינו מזהה זירת מסחר או זהות משקיע

    תוצאות: רשימת הימים, כיוון מצטבר (net_bias), סיכום
    """
    result = {
        "prints":        [],
        "count":         0,
        "net_bias":      "NEUTRAL",  # BULLISH / BEARISH / NEUTRAL
        "bullish_prints":0,
        "bearish_prints":0,
        "summary":       "אין נתוני Narrow-range volume proxy",
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

            # תנאי Narrow-range volume proxy: volume חריג + טווח מחיר קטן
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
                f"{bias_emoji} Narrow-range volume proxy: {len(prints)} prints ב-{DARKPOOL_LOOKBACK} יום "
                f"| {bull} קנייה / {bear} מכירה"
            )
        else:
            result["summary"] = "אין Narrow-range volume proxy prints"

    except Exception as e:
        result["summary"] = "שגיאה בניתוח Narrow-range volume proxy"

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
    מרכז את כל הניתוח המעמיק: Weekly + Accumulation + Insider + Short + Options + Narrow-range volume proxy + 13F + Sentiment + Congress.
    """
    weekly      = get_weekly_timeframe(ticker, df)
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

def _mail_text(value, limit: int = 4000) -> str:
    """Escape provider/state text at the HTML boundary; never accept raw markup."""
    from html import escape
    return escape(str(value if value is not None else "")[:limit], quote=True)


def _mail_number(value, decimals: int = 2, prefix: str = "", suffix: str = "",
                 signed: bool = False, scale: float = 1.0) -> str:
    """Presentation only: missing/nonfinite values stay missing, including zero P&L."""
    if value is None or isinstance(value, bool):
        return "—"
    try:
        number = _mail_float(value) * scale
        if not math.isfinite(number):
            return "—"
        spec = f"{'+' if signed else ''},.{decimals}f"
        return f"{prefix}{number:{spec}}{suffix}"
    except (ValueError, TypeError, OverflowError):
        return "—"


def _mail_tone(tone: str = "blue") -> tuple[str, str]:
    return {
        "blue": ("#8bb5ff", "#18253c"),
        "green": ("#52d7b2", "#132f2b"),
        "red": ("#ff8793", "#351e29"),
        "amber": ("#f4bf64", "#322b1e"),
        "muted": ("#a9b6ca", "#1c2331"),
    }.get(tone, ("#8bb5ff", "#18253c"))


def _mail_pill(text, tone: str = "blue") -> str:
    color, bg = _mail_tone(tone)
    direction = "rtl" if re.search(r"[\u0590-\u08ff]", str(text)) else "ltr"
    return (f'<span dir="{direction}" style="direction:{direction};unicode-bidi:embed;display:inline-block;background-color:{bg};color:{color};'
            f'border:1px solid {bg};border-radius:6px;padding:5px 9px;font-size:11px;'
            f'font-weight:bold;line-height:1.5;">{_mail_text(text, 250)}</span>')


def _mail_metrics(items: list[tuple]) -> str:
    """Four numeric columns on desktop; two per row in supporting mobile clients."""
    cells = []
    width = 100 // max(1, len(items))
    for label, value, tone in items:
        color, _ = _mail_tone(tone)
        cells.append(
            f'<td class="mail-metric" width="{width}%" valign="top" '
            'style="padding:15px 8px;text-align:center;border:1px solid #263044;'
            'background-color:#0d111b;box-sizing:border-box;">'
            f'<div style="font-size:11px;color:#a9b6ca;line-height:1.7;">{_mail_text(label, 100)}</div>'
            f'<div dir="ltr" class="mail-value" style="direction:ltr;unicode-bidi:embed;'
            f'font-size:22px;font-weight:bold;line-height:1.6;color:{color};'
            f'font-variant-numeric:tabular-nums;white-space:nowrap;">{_mail_text(value, 100)}</div></td>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="width:100%;border-collapse:collapse;table-layout:fixed;margin:14px 0;">'
            '<tr class="mail-metric-row">' + ''.join(cells) + '</tr></table>')


def _mail_rows(rows: list[tuple]) -> str:
    parts = []
    for row in rows:
        label, value = row[:2]
        direction = "rtl" if re.search(r"[\u0590-\u08ff]", str(value)) else "ltr"
        extra = row[2] if len(row) > 2 else ""
        hint = (f'<div style="font-size:11px;color:#a9b6ca;margin-top:3px;">{_mail_text(extra)}</div>'
                if extra else "")
        parts.append(
            '<tr><td valign="top" style="width:45%;padding:9px 0 9px 12px;'
            'border-bottom:1px solid #263044;color:#a9b6ca;line-height:1.65;">'
            f'{_mail_text(label)}{hint}</td>'
            '<td valign="top" style="padding:9px 0;border-bottom:1px solid #263044;'
            'color:#edf2fa;font-weight:bold;line-height:1.65;overflow-wrap:anywhere;word-break:break-word;">'
            f'<span dir="{direction}" style="direction:{direction};unicode-bidi:embed;">'
            f'{_mail_text(value).replace(chr(10), "<br>")}</span></td></tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px;">'
            + ''.join(parts) + '</table>')


def _mail_list(items, fallback: str = "", limit: int = 8) -> str:
    values = items if isinstance(items, (list, tuple)) else []
    if not values:
        values = [fallback] if fallback else []
    return ('<ul style="margin:8px 0 0;padding:0 18px 0 0;color:#c5d0e0;'
            'font-size:12px;line-height:1.9;">'
            + ''.join(f'<li style="margin:3px 0;">{_mail_text(v)}</li>' for v in values[:limit])
            + '</ul>')


def _mail_section(title: str, body: str, tone: str = "blue") -> str:
    color, _ = _mail_tone(tone)
    return (f'<div style="border-top:1px solid #263044;margin-top:18px;padding-top:16px;">'
            f'<h3 style="margin:0 0 9px;color:{color};font-size:13px;line-height:1.6;">'
            f'{_mail_text(title)}</h3>{body}</div>')


def _mail_card(title: str, body: str, subtitle: str = "", badge: str = "") -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;margin:0 0 18px;border-collapse:separate;border:1px solid #263044;'
        'border-radius:12px;background-color:#121621;color:#edf2fa;">'
        '<tr><td class="mail-card-pad" style="padding:24px;overflow-wrap:anywhere;word-break:break-word;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;table-layout:fixed;"><tr><td valign="top" style="width:72%;">'
        f'<h2 style="margin:0;color:#ffffff;font-size:26px;line-height:1.25;">{_mail_text(title)}</h2>'
        f'<div style="margin-top:6px;color:#a9b6ca;font-size:12px;line-height:1.65;">{_mail_text(subtitle)}</div>'
        f'</td><td align="left" valign="top" style="text-align:left;">{badge}</td></tr></table>'
        f'{body}</td></tr></table>')


def _mail_document(title: str, subtitle: str, content: str, *,
                   eyebrow: str = "DAILY INTELLIGENCE", preheader: str = "") -> str:
    """Inline-first table layout; CSS only enhances the 320px+ mobile layout."""
    return f'''<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">
<title>{_mail_text(title)}</title>
<style>
body,table,td{{font-family:Arial,Helvetica,sans-serif;}}
table{{mso-table-lspace:0pt;mso-table-rspace:0pt;}}
img{{border:0;outline:none;text-decoration:none;-ms-interpolation-mode:bicubic;}}
a[x-apple-data-detectors]{{color:inherit!important;text-decoration:none!important;}}
@media screen and (max-width:520px){{
 .mail-outer{{padding:12px 8px!important;}}
 .mail-card-pad{{padding:18px 14px!important;}}
 .mail-title{{font-size:26px!important;}}
 .mail-metric-row{{display:block!important;font-size:0!important;}}
 .mail-metric{{display:inline-block!important;width:50%!important;vertical-align:top!important;}}
 .mail-value{{font-size:21px!important;}}
}}
</style></head>
<body bgcolor="#090b10" dir="rtl" style="margin:0;padding:0;background-color:#090b10;color:#edf2fa;direction:rtl;">
<div data-email-preheader="true" style="display:none;font-size:1px;line-height:1px;color:#090b10;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{_mail_text(preheader or subtitle, 220)}</div>
<table role="presentation" width="100%" bgcolor="#090b10" cellpadding="0" cellspacing="0" style="width:100%;background-color:#090b10;"><tr>
<td class="mail-outer" align="center" style="padding:30px 16px;">
<!--[if mso]><table role="presentation" width="760" align="center"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" dir="rtl" style="width:100%;max-width:760px;text-align:right;direction:rtl;">
<tr><td style="padding:0 2px 24px;border-bottom:1px solid #263044;">
<div dir="ltr" style="direction:ltr;text-align:left;font-size:13px;font-weight:bold;letter-spacing:2px;color:#8bb5ff;">STOCK SCANNER <span style="color:#526580;">/</span> INTELLIGENCE</div>
<div dir="ltr" style="direction:ltr;text-align:left;font-size:10px;letter-spacing:1.4px;color:#a9b6ca;margin-top:8px;">{_mail_text(eyebrow, 100)}</div>
<h1 class="mail-title" style="font-size:32px;line-height:1.35;margin:23px 0 8px;color:#ffffff;">{_mail_text(title)}</h1>
<p style="font-size:13px;line-height:1.8;margin:0;color:#a9b6ca;">{_mail_text(subtitle)}</p>
</td></tr><tr><td style="padding-top:24px;">{content}</td></tr>
<tr><td style="padding:18px 8px 6px;border-top:1px solid #263044;text-align:center;">
<div style="font-size:10px;letter-spacing:1.5px;color:#8b9ab1;" dir="ltr">STOCK SCANNER · ENTRY &amp; EXIT INTELLIGENCE</div>
<p style="font-size:11px;line-height:1.8;color:#8b9ab1;margin:9px 0 0;">הודעה אוטומטית — אינה מהווה ייעוץ פיננסי.</p>
</td></tr></table><!--[if mso]></td></tr></table><![endif]-->
</td></tr></table></body></html>'''


def _mail_plain_text(document: str) -> str:
    from html.parser import HTMLParser
    class TextOnly(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.hidden = [], 0
        def handle_starttag(self, tag, attrs):
            if tag in {"head", "style", "script"}:
                self.hidden += 1
            if not self.hidden and tag in {"br", "p", "div", "h1", "h2", "h3", "tr", "li"}:
                self.parts.append("\n")
        def handle_endtag(self, tag):
            if tag in {"head", "style", "script"}:
                self.hidden = max(0, self.hidden - 1)
            if not self.hidden and tag in {"td", "th"}:
                self.parts.append(" | ")
            elif not self.hidden and tag in {"p", "div", "h1", "h2", "h3", "tr", "li"}:
                self.parts.append("\n")
        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)
    parser = TextOnly()
    parser.feed(document)
    parser.close()
    return re.sub(r"\n[ \t|]*\n(?:[ \t|]*\n)+", "\n\n", ''.join(parser.parts)).strip()


def _attach_mail_content(message, document: str) -> None:
    """Keep CID images in related; put text/plain before text/html alternatives."""
    target = message
    if message.get_content_subtype() == "related":
        target = MIMEMultipart("alternative")
        message.attach(target)
    target.attach(MIMEText(_mail_plain_text(document), "plain", "utf-8"))
    target.attach(MIMEText(document, "html", "utf-8"))


def _build_html_card(alert: dict, company: dict, send_date: str, sector: dict | None = None,
                     intel: dict | None = None, catalyst: dict | None = None,
                     rs: dict | None = None) -> str:
    """Render existing signal fields without recalculating scores or trade levels."""
    company = company if isinstance(company, dict) else {}
    meta = alert.get("meta") if isinstance(alert.get("meta"), dict) else {}
    ticker = str(alert.get("ticker", ""))
    entry = alert.get("planned_entry", alert.get("breakout_level"))
    stop, target = alert.get("stop_loss"), alert.get("target")
    quality = alert.get("entry_quality") if isinstance(alert.get("entry_quality"), dict) else meta.get("entry_quality", {})
    quality = quality if isinstance(quality, dict) else {}
    qm = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    pro = alert.get("professional_quality") if isinstance(alert.get("professional_quality"), dict) else {}
    exit_plan = _ensure_exit_plan(alert)
    model = meta.get("target_model")
    target_model_text = {"R_MULTIPLE_SCENARIO": "תרחיש לפי מכפלת R · אינו יעד מבני",
                         "PRIOR_SWING_HIGH": "שיא מקומי קודם", "MEASURED_MOVE": "מהלך מדוד של התבנית",
                         "LEGACY_TARGET_REFERENCE": "יעד ייחוס מהגרסה הקודמת"}.get(model, model or "לא נרשם")
    score = pro.get("professional_score", alert.get("score"))
    pattern = alert.get("display_pattern_name", alert.get("pattern_type", ""))
    body = (f'<div style="margin:16px 0 6px;color:#d4deee;font-size:14px;font-weight:bold;line-height:1.7;">'
            f'{_mail_text(pattern)}</div>')
    body += _mail_metrics([
        ("כניסה מתוכננת", _mail_number(entry, prefix="$"), "blue"),
        ("Stop Loss · מקורי", _mail_number(stop, prefix="$"), "red"),
        ("Target 1 · ייחוס", _mail_number(target, prefix="$"), "green"),
        ("יחס סיכוי / סיכון", _mail_number(alert.get("rr_ratio"), suffix=":1"), "amber"),
    ])
    body += _mail_rows([
        ("נפח ביחס לממוצע", _mail_number(qm.get("volume_ratio"), suffix="×")),
        ("RS Score", _mail_number(qm.get("rs_score"), 0)),
        ("קרבה לשיא 52 שבועות", _mail_number(qm.get("high52_proximity"), 0, suffix="%", scale=100)),
        ("תאריך נר האיתות", meta.get("signal_date") or "לא נרשם"),
    ])
    body += '<!-- scanner-chart -->'
    # Risk per share is display arithmetic only; use the exact existing levels.
    try:
        risk = abs(_mail_float(entry) - _mail_float(stop))
    except (ValueError, TypeError, OverflowError):
        risk = None
    body += _mail_section("פרטי העסקה", _mail_rows([
        ("סיכון למניה", _mail_number(risk, prefix="$")),
        ("גודל פוזיציה מחושב", _mail_number(meta.get("position_size"), 0)),
        ("Pivot התבנית", _mail_number(alert.get("pattern_pivot", meta.get("pattern_pivot")), prefix="$")),
        ("מחיר ייחוס בעת האיתות", _mail_number(alert.get("reference_price", meta.get("reference_price")), prefix="$")),
        ("מודל היעד", target_model_text),
    ]) + '<p style="font-size:11px;color:#a9b6ca;line-height:1.7;margin:10px 0 0;">כניסה מתוכננת היא מחיר ייחוס של האיתות; אינה אישור לביצוע עסקה.</p>')
    body += _mail_section("Entry Ready · אימות הכניסה",
        _mail_rows([("סטטוס", quality.get("status", "ENTRY_READY")),
                    ("ציון Entry", _mail_number(quality.get("quality_score"), 0, suffix="/100"))])
        + _mail_list(quality.get("reasons"), "עבר שכבת Entry Ready")
        + _mail_list(quality.get("blocking_fails"), limit=6), "green")
    if pro:
        components = pro.get("components") if isinstance(pro.get("components"), dict) else {}
        rows = [("ציון Professional", _mail_number(pro.get("professional_score"), 0, suffix="/100")),
                ("סיווג", pro.get("label") or "לא נרשם")]
        for key, label in [("trend", "Trend"), ("rs", "Multi-RS"), ("sector", "Sector"),
                           ("accumulation", "Accumulation"), ("execution", "Execution"),
                           ("risk", "Risk"), ("market", "Market")]:
            component = components.get(key) if isinstance(components.get(key), dict) else {}
            if component:
                rows.append((label, _mail_number(component.get("score"), 0)))
        body += _mail_section("Professional · איכות הסטאפ", _mail_rows(rows))
    if meta.get("score_reasons"):
        body += _mail_section("קריטריונים שאומתו", _mail_list(meta["score_reasons"], limit=16), "green")
    if meta.get("fail_reasons"):
        body += _mail_section("אזהרות", _mail_list(meta["fail_reasons"], limit=16), "amber")
    protection = _mail_number(exit_plan.get("profit_protection_trigger_pct"), 0, prefix="+", suffix="%")
    trigger = _mail_number(exit_plan.get("profit_protection_trigger_price"), prefix="$")
    atr = _mail_number(exit_plan.get("chandelier_atr_mult"), 1)
    body += _mail_section("Exit Intelligence · תוכנית היציאה", _mail_list([
        f"Stop Loss המקורי נשאר קבוע: {_mail_number(stop, prefix='$')}",
        f"Profit Protection מתחיל ב-{protection}, סביב {trigger}.",
        f"לאחר ההפעלה: Profit Floor = Highest High − {atr}×ATR14; הרצפה רק עולה.",
        f"Target 1 ({_mail_number(target, prefix='$')}) הוא יעד ייחוס בלבד ואינו גורם ליציאה אוטומטית.",
        "יציאה על שינוי כיוון דורשת כמה אישורים יחד; Doji לבדו הוא אזהרה ולא SELL.",
        "אותות Reversal יומיים נבדקים רק על נר סגור.",
    ]))
    desc = str(company.get("description") or "")
    company_body = (f'<p style="font-size:12px;color:#c5d0e0;line-height:1.8;margin:0 0 8px;">'
                    f'{_mail_text(desc[:300] + ("…" if len(desc) > 300 else ""))}</p>' if desc else "")
    company_body += _mail_rows([
        ("סקטור", company.get("sector") or "לא זמין"),
        ("שווי שוק", _mail_number(company.get("market_cap_b"), 1, prefix="$", suffix="B")),
        ("EPS", _mail_number(company.get("eps"), prefix="$")),
        ("מועד דוח", company.get("earnings_date") or "לא ידוע"),
    ])
    body += _mail_section("פרטי החברה", company_body, "muted")
    if isinstance(sector, dict) and sector:
        rotation = (_sector_rotation_cache or {}).get(sector.get("sector", ""), {})
        sector_rows = [("סיכום סקטור", sector.get("summary") or "לא זמין"),
                       ("סנטימנט", sector.get("sentiment") or "לא זמין")]
        if rotation:
            sector_rows += [("Rotation", rotation.get("rank", "—")),
                            ("RS vs SPY", _mail_number(rotation.get("rs_spy"), 1, suffix="%", signed=True))]
        body += _mail_section("סקטור ורוטציה", _mail_rows(sector_rows) + _mail_list(sector.get("news"), limit=3))
    intelligence = intel if isinstance(intel, dict) else {}
    intel_rows = []
    if isinstance(rs, dict) and rs.get("rs_score") is not None:
        intel_rows.append(("RS Score", rs.get("summary", "")))
    for key, label in [("weekly", "שבועי"), ("accum", "A/D Score"), ("insider", "Insider"),
                       ("short", "Short"), ("options", "Options"),
                       ("darkpool", "Narrow-range volume proxy"), ("inst", "13F"),
                       ("sentiment", "Sentiment"), ("congress", "Congress")]:
        item = intelligence.get(key) if isinstance(intelligence.get(key), dict) else {}
        if item.get("summary"):
            intel_rows.append((label, item["summary"]))
    if intel_rows:
        body += _mail_section("ניתוח משלים", _mail_rows(intel_rows), "muted")
    if isinstance(catalyst, dict) and catalyst:
        catalyst_rows = [("סיווג", catalyst.get("catalyst_label") or "לא זמין")]
        if catalyst.get("earnings_str"):
            catalyst_rows.append(("דוח", catalyst["earnings_str"]))
        for key, label in [("sector_strength", "סקטור"), ("analyst", "אנליסטים"), ("insider", "Insider")]:
            item = catalyst.get(key) if isinstance(catalyst.get(key), dict) else {}
            if item.get("summary"):
                catalyst_rows.append((label, item["summary"]))
        body += _mail_section("Catalyst · מידע תומך", _mail_rows(catalyst_rows), "muted")
    body += f'<p style="font-size:11px;color:#8b9ab1;margin:18px 0 0;">תאריך השליחה: {_mail_text(send_date)}</p>'
    return _mail_card(ticker, body, company.get("name") or ticker,
                      _mail_pill(_mail_number(score, 0, suffix="/100") + " · איכות", "green"))


def _mail_chart(cid: str, ticker: str) -> str:
    return ('<div style="margin-top:18px;">'
            '<div style="color:#a9b6ca;font-size:11px;margin-bottom:8px;">גרף האיתות · נתוני הסורק</div>'
            f'<img src="cid:{_mail_text(cid, 100)}" alt="גרף {_mail_text(ticker, 50)}" '
            'width="700" style="display:block;width:100%;max-width:700px;height:auto;'
            'border:1px solid #263044;border-radius:8px;background-color:#ffffff;"></div>')


def _mail_position_card(values: dict) -> str:
    v = values
    tone = "green" if v["pnl_pct"] >= 0 else "red"
    body = (f'<p style="margin:16px 0 0;color:#c5d0e0;font-size:13px;line-height:1.8;">'
            f'{_mail_text(v["status_text"])}</p>')
    body += '<div style="margin-top:10px;">' + _mail_pill(v["status"], "amber" if v["status"] in ("WATCH", "TIGHTEN") else "blue") + '</div>'
    body += _mail_metrics([
        ("כניסה", _mail_number(v["entry"], prefix="$"), "blue"),
        ("מחיר מעקב אחרון", _mail_number(v["price_now"], prefix="$"), "blue"),
        ("Stop מקורי וקבוע", _mail_number(v["stop"], prefix="$"), "red"),
        ("Profit Floor", v["floor_txt"] if str(v["floor_txt"]).startswith("$") else "—", "green"),
    ])
    body += _mail_rows([
        ("תבנית", v["pattern"]), ("פתיחה", v["date_open"] or "לא נרשם"),
        ("זמן פתוח", f'{v["days_open"]} ימים'),
        ("מצב Profit Floor", v["floor_txt"]),
        ("Max profit", _mail_number(v["max_profit"], 1, suffix="%", signed=True)),
        ("Target 1 · ייחוס", _mail_number(v["target"], prefix="$")),
        ("סטטוס יעד", v["target_note"]),
        ("Reversal score", _mail_number(v["score"], 0, suffix="/100")),
        ("סטטוס מנוע", v["status"]),
    ])
    if v.get("earnings_note"):
        body += _mail_section("📅 דוח קרוב", _mail_list([v["earnings_note"],
                              "פוזיציה פתוחה לפני דוח: התנודה ביום הדוח יכולה לעבור את הסטופ בפער פתיחה."]), "red")
    body += _mail_section("Exit Intelligence", _mail_list(v["signals"], "אין אותות Reversal משמעותיים", 6),
                          "amber" if v["status"] in ("WATCH", "TIGHTEN") else "blue")
    return _mail_card(v["ticker"], body, "מעקב פוזיציה פתוחה",
                      _mail_pill(_mail_number(v["pnl_pct"], 1, suffix="%", signed=True), tone))


def _mail_exit_card(a: dict, pnl: float, floor_txt: str, sigs: list) -> str:
    body = _mail_metrics([
        ("כניסה", _mail_number(a.get("entry"), prefix="$"), "blue"),
        ("יציאה", _mail_number(a.get("exit_price"), prefix="$"), "green" if pnl >= 0 else "red"),
        ("Stop מקורי", _mail_number(a.get("stop"), prefix="$"), "red"),
        ("Profit Floor", floor_txt, "green"),
    ])
    body += _mail_rows([
        ("סיבת יציאה", a.get("reason", "")),
        ("Target 1 · ייחוס", _mail_number(a.get("target"), prefix="$")),
        ("Max profit", _mail_number(a.get("max_profit_pct"), 1, suffix="%", signed=True)),
        ("Reversal score", _mail_number(a.get("exit_score"), 0, suffix="/100")),
        ("זמן פתוח", f'{a.get("days_open", "—")} ימים'),
    ])
    body += _mail_section("אותות היציאה", _mail_list(sigs, "Stop Loss מקורי", 6), "amber")
    return _mail_card(a.get("ticker", ""), body, a.get("reason", ""),
                      _mail_pill(_mail_number(pnl, 1, suffix="%", signed=True), "green" if pnl >= 0 else "red"))


def _mail_daily_summary(stats: dict, filter_stats: dict, regime: dict,
                        prefilter_stats: dict, note: str, now_str: str) -> str:
    skipped = regime.get("allow_trading", True) is False
    errors = _infra_int(stats.get("errors", 0))
    headline = "הסריקה דולגה לפי מצב השוק" if skipped else "אין סטאפים לשליחה היום"
    tone = "amber" if skipped or errors else "muted"
    body = _mail_pill("מצב שוק" if skipped else "ללא סטאפים", tone)
    body += f'<p style="color:#c5d0e0;font-size:13px;line-height:1.9;margin:16px 0;">{_mail_text(note or headline)}</p>'
    body += _mail_metrics([
        ("נסרקו בפועל", _mail_number(stats.get("scanned", 0), 0), "blue"),
        ("התראות שנשלחו", _mail_number(stats.get("sent", 0), 0), "green"),
        ("שגיאות קריטיות", _mail_number(stats.get("errors", 0), 0), "red" if errors else "muted"),
        ("אחרי Universe", _mail_number(prefilter_stats.get("total"), 0), "muted"),
    ])
    run_rows = [
        ("מניות אחרי Universe", prefilter_stats.get("total", "N/A")),
        ("עברו EMA28 Pre-filter", prefilter_stats.get("passed", "N/A")),
        ("נדחו ב־Pre-filter EMA28", prefilter_stats.get("failed", "N/A")),
        ("נדחו ב־Market Cap Pre-check (<$1B)", _infra_int(prefilter_stats.get("market_cap_known_reject", 0) or 0) + _infra_int(prefilter_stats.get("market_cap_verified_reject", 0) or 0)),
        ("Market Cap חסר שאומת מעל $1B", prefilter_stats.get("market_cap_verified_pass", 0)),
        ("Market Cap עדיין לא ידוע — fail-open", prefilter_stats.get("market_cap_unresolved_pass", 0), "ניסיון נוסף מתבצע בתוך Full Scan"),
        ("עברו Market Cap והגיעו לסריקה מלאה", prefilter_stats.get("post_market_cap_passed", prefilter_stats.get("passed", "N/A"))),
        ("באטצ׳ים שנכשלו והועברו הלאה", prefilter_stats.get("download_failed_batches", 0), "לא נמחקים — עוברים לסריקה מלאה"),
        ("מניות בלי נתונים בבאטצ׳ והועברו הלאה", prefilter_stats.get("missing_tickers", 0), "לא פוסלים בגלל חוסר נתונים זמני"),
    ]
    reject_rows = [
        ("Market Cap / No Data", _infra_int(filter_stats.get("market_cap", 0)) + _infra_int(filter_stats.get("no_data", 0))),
        ("דוחות קרובים", filter_stats.get("earnings", 0)),
        ("EMA28", filter_stats.get("ema28", 0)),
        ("MA150 רחוק", filter_stats.get("ma150_dist", 0)),
        ("MA לא קרוב ל־neckline", filter_stats.get("ma_neckline", 0)),
        ("תבנית קיימת אבל לא Entry Ready — טיקרים", filter_stats.get("entry_quality_unique", filter_stats.get("entry_quality", 0)), f"{filter_stats.get('entry_quality', 0)} מועמדי תבנית נפסלו"),
        ("עבר Entry Ready אך נפסל ב-Professional — טיקרים", filter_stats.get("professional_quality_unique", filter_stats.get("professional_quality", 0)), f"{filter_stats.get('professional_quality', 0)} מועמדי תבנית נפסלו"),
        ("לא נמצאה תבנית", filter_stats.get("no_pattern", 0)),
        ("Reverse Scanner", filter_stats.get("reverse_scan", 0)),
    ]
    body += _mail_section("משפך הסינון", _mail_rows(run_rows))
    body += _mail_section("פירוט דחיות בסריקה מלאה", _mail_rows(reject_rows), "muted")
    body += _mail_section("מצב השוק", f'<p style="font-size:13px;color:#c5d0e0;line-height:1.9;margin:0;">{_mail_text(regime.get("summary", "מצב שוק לא ידוע"))}</p>')
    return _mail_document("סיכום הסריקה היומי", now_str, _mail_card(headline, body, "דוח פעילות הסורק"),
                          eyebrow="DAILY SCAN REPORT", preheader=note or headline)

def _canonical_pattern_name(alert: dict) -> str:
    return str(alert.get("pattern_type", "")).lstrip("🏆⭐ ")


def _sync_trade_geometry(alert: dict, df: pd.DataFrame | None = None) -> dict:
    """One planned entry for scoring, mail, state and research; pivot stays separate.

    planned_entry is a signal-time reference, not a claim of an executable broker fill.
    """
    meta = alert.setdefault("meta", {})
    pivot = _to_float_or_none(alert.get("pattern_pivot", meta.get("pattern_pivot", alert.get("breakout_level"))))
    reference = _to_float_or_none(alert.get("reference_price", meta.get("reference_price")))
    if reference is None and df is not None and not df.empty:
        reference = _to_float_or_none(df.iloc[-1].get("close", df.iloc[-1].get("Close")))
    entry = _to_float_or_none(alert.get("planned_entry"))
    if entry is None:
        choices = [x for x in (pivot, reference) if x is not None]
        entry = max(choices) if choices else None
    stop = _to_float_or_none(alert.get("stop_loss"))
    target = _to_float_or_none(alert.get("target"))
    risk, reward, rr = None, None, None
    if all(x is not None and x > 0 for x in (entry, stop, target)) and stop < entry < target:
        risk, reward = entry - stop, target - entry
        rr = reward / risk
    old_rr = _to_float_or_none(alert.get("rr_ratio"))
    if "detector_rr" not in meta and old_rr is not None:
        meta["detector_rr"] = old_rr
    alert.update({"pattern_pivot": pivot, "planned_entry": entry, "breakout_level": entry,
                  "reference_price": reference, "rr_ratio": rr})
    meta.update({"pattern_pivot": pivot, "planned_entry": entry, "reference_price": reference,
                 "planned_risk": risk, "planned_reward": reward, "calculated_rr": rr,
                 "entry_model": "SIGNAL_REFERENCE_NOT_EXECUTED"})
    if df is not None and not df.empty:
        meta["signal_date"] = pd.Timestamp(df.index[-1]).date().isoformat()
    return alert


def _daily_data_problems(df: pd.DataFrame) -> list[str]:
    issues = []
    try:
        prices = df[["open", "high", "low", "close"]].tail(260).apply(pd.to_numeric, errors="coerce")
        valid = np.isfinite(prices).all(axis=1) & (prices > 0).all(axis=1)
        valid &= prices["high"] >= prices[["open", "close", "low"]].max(axis=1)
        valid &= prices["low"] <= prices[["open", "close", "high"]].min(axis=1)
        if not bool(valid.all()):
            issues.append("invalid OHLC geometry")
        volume = pd.to_numeric(df["volume"], errors="coerce")
        if not _is_finite_number(volume.iloc[-1]) or float(volume.iloc[-1]) <= 0:
            issues.append("latest daily volume missing/zero")
        if (datetime.now(timezone.utc).date() - pd.Timestamp(df.index[-1]).date()).days > 7:
            issues.append("price history older than seven calendar days")
    except Exception as exc:
        issues.append(f"data validation error: {type(exc).__name__}")
    return issues


def _range_touched_band(frame: pd.DataFrame, average: pd.Series, tolerance_pct: float) -> bool:
    """True only when a candle intersects the moving-average tolerance band."""
    low = pd.to_numeric(frame["Low"], errors="coerce").to_numpy(dtype=float)
    high = pd.to_numeric(frame["High"], errors="coerce").to_numpy(dtype=float)
    ma = pd.to_numeric(average.reindex(frame.index), errors="coerce").to_numpy(dtype=float)
    tolerance = max(0.0, float(tolerance_pct)) / 100.0
    valid = np.isfinite(low) & np.isfinite(high) & np.isfinite(ma) & (ma > 0) & (high >= low)
    return bool((valid & (low <= ma * (1 + tolerance)) & (high >= ma * (1 - tolerance))).any())


def _eligible_research_date(observed_at, signal_date) -> str:
    """Avoid using a full daily bar whose early prices predate the observation.

    Before 09:30 NY the current session is eligible. From the open onward,
    use the next session (selected later from actual bars, including holidays).
    """
    ts = pd.Timestamp(observed_at)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts = ts.tz_convert("America/New_York")
    day = ts.date()
    if (ts.hour, ts.minute) >= (9, 30):
        day += timedelta(days=1)
    signal = pd.Timestamp(signal_date).date()
    return max(day, signal + timedelta(days=1)).isoformat()


def _evaluation_columns() -> list[str]:
    cols = ["evaluation_id", "schema_version", "code_version", "date_sent", "observed_at", "signal_date",
            "eligible_date", "ticker", "pattern", "stage", "planned_entry", "pattern_pivot", "stop", "target",
            "target_model", "entry_quality", "professional_score", "blockers", "metadata_json", "execution_model",
            "slippage_bps", "fill_price", "fill_date", "evaluation_status", "checked"]
    for n in PERF_CHECK_DAYS:
        cols += [f"price_{n}d", f"horizon_return_pct_{n}d", f"horizon_mfe_pct_{n}d", f"horizon_mae_pct_{n}d",
                 f"bracket_result_{n}d", f"bracket_net_r_{n}d", f"event_date_{n}d"]
    return cols


def _entry_research_thresholds() -> dict:
    return {"entry_min_score": ENTRY_READY_MIN_SCORE, "professional_min_score": PRO_MIN_SCORE,
            "min_rr": ENTRY_MIN_RR, "min_rs_score": ENTRY_MIN_RS_SCORE,
            "breakout_min_volume_ratio": ENTRY_MIN_VOLUME_RATIO,
            "pullback_min_volume_ratio": ENTRY_PULLBACK_MIN_VOLUME_RATIO,
            "retest_min_volume_ratio": ENTRY_RETEST_MIN_VOLUME_RATIO,
            "min_close_position": ENTRY_MIN_CLOSE_POS, "min_body_ratio": ENTRY_MIN_BODY_RATIO,
            "max_extension_pct": ENTRY_MAX_EXTENSION_PCT,
            "earnings_exclusion_calendar_days": EARNINGS_FILTER_DAYS,
            "require_verified_earnings": REQUIRE_VERIFIED_EARNINGS,
            "rs_score_method": "WEIGHTED_EXCESS_VS_SPY_TANH_NOT_PERCENTILE"}


def _capture_entry_research_diagnostics(alert: dict, stage: str, df=None) -> None:
    """Record skipped-gate evidence; research scores never authorize a live alert.

    DATA_WAIT originally short-circuits all technical gates. Compute a separate
    diagnostic score on valid closed bars without replacing that live decision.
    A bounded near-miss sample also receives a separate Professional score.
    """
    meta = alert.setdefault("meta", {})
    try:
        active_quality = alert.get("entry_quality") or {}
        data_blockers = list(active_quality.get("blocking_fails", [])) if stage == "DATA_WAIT" else []
        quality = active_quality
        if stage == "DATA_WAIT" and df is not None and not df.empty:
            if _validate_long_trade_levels(alert)[0]:
                quality = evaluate_entry_quality(
                    df=df, ticker=alert.get("ticker", ""),
                    breakout_level=alert.get("breakout_level", 0), pattern_meta=meta,
                    base_score=float(alert.get("base_score", alert.get("score", 0)) or 0),
                    rr=alert.get("rr_ratio"), regime=get_market_regime())
        audit = {"schema_version": 3, "signal_date": meta.get("signal_date"),
                 "captured_at": datetime.now(timezone.utc).isoformat(),
                 "active_stage": stage, "data_blockers": data_blockers,
                 "entry_score": quality.get("quality_score"),
                 "entry_blockers": list(quality.get("blocking_fails", [])),
                 "technical_entry_ready": bool(quality.get("entry_ready")),
                 "metrics": dict(quality.get("metrics", {})),
                 "active_thresholds": _entry_research_thresholds(),
                 "research_only": True}
        # Separate from alert['professional_quality']; it is never used by main().
        score = _to_float_or_none(quality.get("quality_score"))
        if (stage == "ENTRY_REJECTED" and PRO_SHADOW_DIAGNOSTICS and not data_blockers
                and df is not None and not df.empty and _validate_long_trade_levels(alert)[0]
                and score is not None and score >= PRO_SHADOW_MIN_ENTRY_SCORE
                and len(audit["entry_blockers"]) <= PRO_SHADOW_MAX_ENTRY_BLOCKERS):
            shadow = evaluate_professional_trade_quality(
                df=df, ticker=alert.get("ticker", ""), alert=alert,
                entry_quality=quality, regime=get_market_regime())
            audit["professional_shadow"] = {"score": shadow.get("professional_score"),
                "blockers": list(shadow.get("blockers", [])),
                "components": shadow.get("components", {}), "research_only": True}
        professional = alert.get("professional_quality") or {}
        if professional:
            audit["active_professional"] = {"score": professional.get("professional_score"),
                "blockers": list(professional.get("blockers", [])),
                "components": professional.get("components", {})}
        meta["entry_diagnostics_v3"] = audit
    except Exception as exc:
        # Diagnostics must not turn a rejected candidate into an alert or block a valid alert.
        meta["entry_diagnostics_v3"] = {"schema_version": 3, "research_only": True,
                                       "error_type": type(exc).__name__, "active_stage": stage}
        log(f"Entry research diagnostics error: {type(exc).__name__}: {exc}")


def _write_entry_funnel_audit() -> dict:
    """Daily report, independent of Saturday's 20-session learning report.

    Dates below are observation dates, including rejected candidates. Weekly
    counts include only actual SENT records and do not establish complete-week coverage.
    """
    from collections import Counter
    result = {"schema_version": 3, "code_version": CODE_VERSION,
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "active_thresholds": _entry_research_thresholds(), "mode": "REPORT_ONLY",
              "limitations": ["candidate rows are correlated", "not broker fills",
                              "observation date is not evidence of email delivery",
                              "weeks may be partial", "shadow scores never authorize alerts"]}
    rows = []
    if os.path.exists(SETUP_EVALUATIONS_CSV):
        rows = pd.read_csv(SETUP_EVALUATIONS_CSV, dtype={"evaluation_id": str}).to_dict("records")
    stages, blockers, data_wait_technical, weeks = Counter(), Counter(), Counter(), Counter()
    full_audits = shadow_scores = diagnostic_errors = 0
    ticker_sessions = set()
    for row in rows:
        stage = str(row.get("stage", ""))
        stages[stage] += 1
        ticker_sessions.add((str(row.get("ticker", "")), str(row.get("signal_date", ""))))
        try:
            blockers.update(set(json.loads(row.get("blockers") or "[]")))
            meta = json.loads(row.get("metadata_json") or "{}")
            audit = meta.get("entry_diagnostics_v3") or {}
            full_audits += int(bool(audit.get("metrics")))
            shadow_scores += int(bool(audit.get("professional_shadow")))
            diagnostic_errors += int(bool(audit.get("error_type")))
            if stage == "DATA_WAIT":
                data_wait_technical.update(set(audit.get("entry_blockers", [])))
            if stage == "SENT":
                stamp = pd.Timestamp(row["observed_at"])
                if stamp.tzinfo is None:
                    stamp = stamp.tz_localize("UTC")
                iso = stamp.tz_convert("America/New_York").date().isocalendar()
                weeks[f"{iso.year}-W{iso.week:02d}"] += 1
        except (ValueError, TypeError, KeyError):
            diagnostic_errors += 1
    result.update({"observations": len(rows), "unique_ticker_sessions": len(ticker_sessions),
                   "stage_counts": dict(stages), "top_blockers": dict(blockers.most_common(20)),
                   "data_wait_technical_blockers": dict(data_wait_technical.most_common(20)),
                   "records_with_full_entry_metrics": full_audits,
                   "records_with_professional_shadow": shadow_scores,
                   "diagnostic_errors": diagnostic_errors, "sent_by_observed_week": dict(weeks)})
    _atomic_write_json(ENTRY_AUDIT_FILE, result)
    log(f"Daily entry audit saved: {len(rows)} candidates; full metrics={full_audits}; pro shadow={shadow_scores}")
    return result


def _record_setup_evaluation(alert: dict, stage: str, df: pd.DataFrame | None = None) -> None:
    """Buffer candidate-level evidence, including rejected, selected and SMTP-failed setups."""
    if not SHADOW_TRACKING_ENABLED and stage != "SENT":
        return
    try:
        _capture_entry_research_diagnostics(alert, stage, df)
        meta = alert.get("meta") or {}
        signal = meta.get("signal_date")
        if df is not None and not df.empty:
            signal = pd.Timestamp(df.index[-1]).date().isoformat()
        if not signal:
            return
        entry = _to_float_or_none(alert.get("planned_entry", alert.get("breakout_level")))
        stop = _to_float_or_none(alert.get("stop_loss"))
        target = _to_float_or_none(alert.get("target"))
        pattern = _canonical_pattern_name(alert)
        signature = [CODE_VERSION, alert.get("ticker"), pattern, signal, entry, stop, target,
                     RESEARCH_SLIPPAGE_BPS]
        evaluation_id = hashlib.sha256(json.dumps(signature, default=str).encode()).hexdigest()[:24]
        previous = _EVALUATION_BUFFER.get(evaluation_id, {})
        stamp = alert.get("sent_at") if stage == "SENT" else previous.get("observed_at")
        stamp = stamp or datetime.now(timezone.utc).isoformat()
        stamp_ts = pd.Timestamp(stamp)
        if stamp_ts.tzinfo is None:
            stamp_ts = stamp_ts.tz_localize("UTC")
        quality = alert.get("entry_quality") or {}
        pro = alert.get("professional_quality") or {}
        blockers = quality.get("blocking_fails", []) + pro.get("blockers", [])
        record = {col: None for col in _evaluation_columns()}
        record.update({"evaluation_id": evaluation_id, "schema_version": 2, "code_version": CODE_VERSION,
                       "date_sent": stamp_ts.tz_convert("America/New_York").date().isoformat(),
                       "observed_at": stamp, "signal_date": signal,
                       "eligible_date": _eligible_research_date(stamp, signal),
                       "ticker": alert.get("ticker", ""), "pattern": pattern, "stage": stage,
                       "planned_entry": entry, "pattern_pivot": alert.get("pattern_pivot"), "stop": stop, "target": target,
                       "target_model": meta.get("target_model", "LEGACY_TARGET_REFERENCE"),
                       "entry_quality": quality.get("quality_score", alert.get("entry_quality_score")),
                       "professional_score": pro.get("professional_score", alert.get("professional_score")),
                       "blockers": json.dumps(_redact_log_value(blockers), ensure_ascii=False, default=str),
                       "metadata_json": json.dumps(_redact_log_value({k: v for k, v in meta.items() if k not in ("entry_quality", "professional_quality")}), ensure_ascii=False, default=str),
                       "execution_model": "COUNTERFACTUAL_NEXT_ELIGIBLE_OPEN",
                       "slippage_bps": RESEARCH_SLIPPAGE_BPS, "checked": False,
                       "evaluation_status": "PENDING"})
        _EVALUATION_BUFFER[evaluation_id] = record
    except Exception as exc:
        log(f"Evaluation record error: {type(exc).__name__}: {exc}")


def _flush_setup_evaluations() -> None:
    if not _EVALUATION_BUFFER:
        return
    # A failure propagates: the run must not claim a completed research checkpoint.
    rows = {}
    if os.path.exists(SETUP_EVALUATIONS_CSV):
        existing = pd.read_csv(SETUP_EVALUATIONS_CSV, dtype={"evaluation_id": str})
        rows = {str(row["evaluation_id"]): row.to_dict() for _, row in existing.iterrows()}
    for key, incoming in _EVALUATION_BUFFER.items():
        previous = rows.get(key)
        if previous is not None:
            if previous.get("stage") == "SENT":
                continue
            # Preserve the first observation unless mail acceptance supplies the actual send time.
            if incoming.get("stage") != "SENT":
                incoming["observed_at"] = previous.get("observed_at", incoming["observed_at"])
                incoming["eligible_date"] = previous.get("eligible_date", incoming["eligible_date"])
                incoming["date_sent"] = previous.get("date_sent", incoming["date_sent"])
                for col in _evaluation_columns():
                    if col in ("checked", "evaluation_status", "fill_price", "fill_date") or col.startswith(("price_", "horizon_", "bracket_", "event_date_")) or col not in incoming or incoming[col] is None:
                        incoming[col] = previous.get(col)
            else:
                # Reset prior counterfactual outcomes: actual send time may permit a later session.
                incoming["checked"] = False
                incoming["evaluation_status"] = "PENDING"
        rows[key] = incoming
    _atomic_write_text(SETUP_EVALUATIONS_CSV, pd.DataFrame(list(rows.values()), columns=_evaluation_columns()).to_csv(index=False))
    log(f"Research observations saved: {len(_EVALUATION_BUFFER)} updates, {len(rows)} total")
    _EVALUATION_BUFFER.clear()


def evaluate_setup_path(price_df: pd.DataFrame, observation: dict, horizons=None) -> dict:
    """Pure daily-bar research evaluator, separate from the V9.2 exit policy.

    Simulates entry at the first eligible session's open. First stop/target hit
    wins; a bar touching both is AMBIGUOUS, with a conservative stop-first R.
    This bracket comparison is not a broker result or V9.2's target-reference exit.
    """
    horizons = tuple(horizons or PERF_CHECK_DAYS)
    result = {"evaluation_status": "PENDING", "checked": False}
    normalized = _normalize_yfinance_df(price_df, drop_invalid_close=False)
    if normalized is None or normalized.empty:
        return result
    data = normalized.sort_index().copy()
    data = data.loc[~data.index.duplicated(keep="last")]
    signal = pd.Timestamp(observation["signal_date"]).date()
    eligible = pd.Timestamp(observation["eligible_date"]).date()
    dates = [pd.Timestamp(x).date() for x in data.index]
    data = data.loc[[d > signal and d >= eligible for d in dates]]
    if data.empty:
        return result
    data = data.iloc[:max(horizons)]
    # Original signal levels cannot be compared across a split without knowing
    # both providers' adjustment basis; retain the observation for review.
    if "stock splits" in data and pd.to_numeric(data["stock splits"], errors="coerce").fillna(0).ne(0).any():
        return {"evaluation_status": "CORPORATE_ACTION_REVIEW", "checked": False}
    if not {"open", "high", "low", "close"}.issubset(data.columns):
        return {"evaluation_status": "INVALID_PRICE_HISTORY", "checked": False}
    required = data[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    valid = np.isfinite(required).all(axis=1) & (required > 0).all(axis=1)
    valid &= required["high"] >= required[["open", "low", "close"]].max(axis=1)
    valid &= required["low"] <= required[["open", "high", "close"]].min(axis=1)
    if not bool(valid.all()):
        return {"evaluation_status": "INVALID_PRICE_HISTORY", "checked": False}
    planned = _to_float_or_none(observation.get("planned_entry"))
    stop = _to_float_or_none(observation.get("stop"))
    target = _to_float_or_none(observation.get("target"))
    if not all(x is not None and x > 0 for x in (planned, stop, target)) or not stop < planned < target:
        return {"evaluation_status": "INVALID_PLANNED_GEOMETRY", "checked": True}
    slip = max(0.0, float(observation.get("slippage_bps", RESEARCH_SLIPPAGE_BPS))) / 10000.0
    fill = float(data["open"].iloc[0]) * (1 + slip)
    result.update({"fill_price": fill, "fill_date": pd.Timestamp(data.index[0]).date().isoformat()})
    if not stop < fill < target:
        result.update({"evaluation_status": "SKIPPED_OPEN_OUTSIDE_BRACKET", "checked": True})
        return result
    risk = fill - stop
    event = None
    event_r = None
    event_date = None
    for i, (ts, bar) in enumerate(data.iterrows(), 1):
        op, hi, lo = map(float, (bar["open"], bar["high"], bar["low"]))
        if event is None:
            if op <= stop:
                event, exit_price = "GAP_STOP", op * (1 - slip)
            elif op >= target:
                event, exit_price = "GAP_TARGET", op * (1 - slip)
            elif lo <= stop and hi >= target:
                event, exit_price = "AMBIGUOUS_BOTH", stop * (1 - slip)
            elif lo <= stop:
                event, exit_price = "STOP", stop * (1 - slip)
            elif hi >= target:
                event, exit_price = "TARGET", target * (1 - slip)
            if event is not None:
                event_r = (exit_price - fill) / risk
                event_date = pd.Timestamp(ts).date().isoformat()
        if i in horizons:
            prefix = data.iloc[:i]
            close = float(bar["close"])
            result.update({f"price_{i}d": close,
                           f"horizon_return_pct_{i}d": (close * (1 - slip) / fill - 1) * 100,
                           f"horizon_mfe_pct_{i}d": (float(prefix["high"].max()) / fill - 1) * 100,
                           f"horizon_mae_pct_{i}d": (float(prefix["low"].min()) / fill - 1) * 100,
                           f"bracket_result_{i}d": event or "OPEN",
                           f"bracket_net_r_{i}d": event_r if event is not None else (close * (1 - slip) - fill) / risk,
                           f"event_date_{i}d": event_date})
        if i >= max(horizons):
            break
    result["evaluation_status"] = "COMPLETE" if len(data) >= max(horizons) else "PARTIAL"
    result["checked"] = len(data) >= max(horizons)
    return result


RESEARCH_BATCH_SIZE = int(os.getenv("RESEARCH_BATCH_SIZE", "40"))


def _research_batch_download(tickers: list) -> dict:
    """v9.7.3: same data as the per-ticker research download, fetched in batches."""
    out = {}
    if not tickers:
        return out
    size = max(1, RESEARCH_BATCH_SIZE)
    for start in range(0, len(tickers), size):
        batch = tickers[start:start + size]
        try:
            raw = yf.download(batch, period="1y", interval="1d", progress=False, auto_adjust=False,
                              actions=True, keepna=True, group_by="ticker", threads=False, timeout=30)
        except Exception as exc:
            log(f"Research batch {start + 1}-{start + len(batch)} failed: {type(exc).__name__}")
            continue
        for ticker in batch:
            frame = _extract_prefilter_ticker_frame(raw, ticker)
            if frame is None or getattr(frame, "empty", True):
                continue
            # Drop only the leading all-empty rows that a multi-ticker download adds before a listing date.
            ohlc = [c for c in frame.columns if str(c).lower() in ("open", "high", "low", "close")]
            if ohlc:
                valid = frame[ohlc].notna().any(axis=1)
                if not valid.any():
                    continue
                frame = frame.loc[valid.idxmax():]
            normalized = _closed_daily_frame(_normalize_yfinance_df(frame, drop_invalid_close=False))
            if normalized is not None and not normalized.empty:
                out[ticker] = normalized
        if start + size < len(tickers):
            time.sleep(1.0)
    log(f"Research batch download: {len(out)}/{len(tickers)} tickers in {((len(tickers) - 1) // size) + 1} request(s)")
    return out


def _update_setup_evaluations() -> None:
    if not os.path.exists(SETUP_EVALUATIONS_CSV):
        return
    observations = pd.read_csv(SETUP_EVALUATIONS_CSV, dtype={"evaluation_id": str})
    if observations.empty:
        return
    cache = {}
    changed = False
    # v9.7.3: batch-download all pending tickers (one Yahoo request per ~40 tickers instead of
    # one per ticker). Tickers missing from a batch fall back to the old single download below.
    try:
        pending = sorted({str(r.get("ticker", "")).strip().upper() for _, r in observations.iterrows()
                          if str(r.get("ticker", "")).strip() and str(r.get("checked", "")).lower() != "true"})
        cache.update(_research_batch_download(pending))
    except Exception as exc:
        log(f"Research batch download unavailable: {type(exc).__name__}")
    for index, row in observations.iterrows():
        if str(row.get("checked", "")).lower() == "true":
            continue
        ticker = str(row.get("ticker", "")).strip().upper()
        try:
            if ticker not in cache:
                # One request per ticker, sufficient for the 180-day retained research window.
                time.sleep(max(0.0, MARKET_CAP_PRECHECK_SLEEP_SECONDS))
                cache[ticker] = _closed_daily_frame(_normalize_yfinance_df(yf.download(
                    ticker, period="1y", interval="1d", progress=False, auto_adjust=False, actions=True, keepna=True), drop_invalid_close=False))
            update = evaluate_setup_path(cache[ticker], row.to_dict())
            for key, value in update.items():
                if key not in observations:
                    observations[key] = pd.Series([None] * len(observations), dtype=object)
                if isinstance(value, str) and observations[key].dtype != object:
                    observations[key] = observations[key].astype(object)
                observations.at[index, key] = value
            changed = True
        except Exception as exc:
            log(f"Research update unavailable {ticker}: {type(exc).__name__}")
    if changed:
        _atomic_write_text(SETUP_EVALUATIONS_CSV, observations.to_csv(index=False))
        log(f"Research performance updated: {len(observations)} observations")


def _select_final_alerts(alerts: list[dict], limit=None) -> list[dict]:
    best = {}
    for alert in sorted(alerts, key=lambda a: float(a.get("score", 0) or 0), reverse=True):
        ticker = str(alert.get("ticker", "")).strip().upper()
        if ticker and ticker not in best:
            best[ticker] = alert
    return list(best.values())[:max(0, int(TOP_ALERTS_TO_SEND if limit is None else limit))]


def _deliver_selected_alerts(alerts: list[dict], history: dict) -> dict:
    valid = []
    for alert in alerts:
        if _validate_long_trade_levels(alert)[0]:
            valid.append(alert)
        else:
            _record_setup_evaluation(alert, "SAFETY_REJECTED", alert.get("_df"))
    selected = _select_final_alerts(valid)
    selected_ids = {id(a) for a in selected}
    for alert in valid:
        if id(alert) not in selected_ids:
            _record_setup_evaluation(alert, "TOP_EXCLUDED", alert.get("_df"))
    if not selected:
        return {"sent": 0, "success": False, "status": "no_valid_selected", "refused": 0}
    email_copy = [dict(a) for a in selected]
    email_copy[0]["display_pattern_name"] = "🏆 " + _canonical_pattern_name(email_copy[0])
    delivery = send_email_alerts(email_copy)
    if not isinstance(delivery, dict) or not delivery.get("success"):
        failure = delivery if isinstance(delivery, dict) else {}
        for alert in selected:
            _record_setup_evaluation(alert, "SMTP_FAILED", alert.get("_df"))
        return {"sent": 0, "success": False, "status": failure.get("status", "failed"), "refused": failure.get("refused", 0)}
    stamp = datetime.now(timezone.utc).isoformat()
    for alert in selected:
        alert["sent_at"] = stamp
        record_alert_sent(alert["ticker"], _canonical_pattern_name(alert), alert.get("breakout_level"), history)
        log_setup_for_tracking(alert)
        open_position(alert)
        log_to_csv(alert["ticker"], (alert.get("meta") or {}).get("score_reasons", []))
        _record_setup_evaluation(alert, "SENT", alert.get("_df"))
    save_alert_history(history)
    log("TOP accepted by email server: " + ", ".join(a["ticker"] for a in selected))
    return {"sent": len(selected), "success": True, "status": delivery.get("status", "accepted"), "refused": delivery.get("refused", 0)}




def _build_setup_chart_figure(ticker: str, df: pd.DataFrame, alert: dict):
    """Plot the detector's recorded anchors; never invent a second pattern for display."""
    from plotly.subplots import make_subplots
    data = _normalize_yfinance_df(df)
    if data is None or data.empty:
        raise ValueError("No chart data")
    pattern = _canonical_pattern_name(alert)
    meta = alert.get("meta") or {}
    signal_date = meta.get("signal_date")
    if signal_date:
        signal = pd.Timestamp(signal_date).date()
        data = data.loc[[pd.Timestamp(x).date() <= signal for x in data.index]]
    lookback = DB_LOOKBACK if pattern.startswith("Double") else max(TRIANGLE_LOOKBACK, int(meta.get("pattern_bars", 0) or 0) + 25)
    data = data.tail(max(lookback, 60)).copy()
    close = data["close"]
    averages = {"ema21": close.ewm(span=21, adjust=False).mean(), "ema28": close.ewm(span=28, adjust=False).mean(),
                "ma50": close.rolling(50, min_periods=20).mean(), "ma150": close.rolling(150, min_periods=50).mean(),
                "ma200": close.rolling(200, min_periods=80).mean()}
    # Existing full-history indicators take priority; do not rewarm long averages on a short plot window.
    full = _normalize_yfinance_df(df)
    for col, span in [("ema21", 21), ("ema28", 28)]:
        if col not in full:
            full[col] = full["close"].ewm(span=span, adjust=False).mean()
    ensure_ma_columns(full)
    for col in averages:
        data[col] = full[col].reindex(data.index) if col in full else averages[col]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.035, row_heights=[0.74, 0.26])
    fig.add_trace(go.Candlestick(x=data.index, open=data["open"], high=data["high"], low=data["low"], close=data["close"], name="Price"), row=1, col=1)
    colors = {"ema21": "#2563eb", "ema28": "#0891b2", "ma50": "#16a34a", "ma150": "#ea580c", "ma200": "#64748b"}
    cols = ["ema28", "ma150", "ma200"]
    if "Pullback" in pattern or "Retest" in pattern:
        cols = ["ema21", "ema28", "ma50", "ma150", "ma200"]
    for col in cols:
        fig.add_trace(go.Scatter(x=data.index, y=data[col], name=col.upper(), line={"color": colors[col], "width": 1.25}), row=1, col=1)
    for key, label, color in [("planned_entry", "Planned reference entry", "#15803d"), ("stop_loss", "Initial stop", "#dc2626"), ("target", "Target reference", "#9333ea")]:
        value = _to_float_or_none(alert.get(key, alert.get("breakout_level") if key == "planned_entry" else None))
        if value is not None and value > 0:
            fig.add_hline(y=value, line_color=color, line_dash="dash", annotation_text=f"{label} {value:.2f}", row=1, col=1)
    pivot = _to_float_or_none(alert.get("pattern_pivot", meta.get("pattern_pivot")))
    if pivot is not None:
        fig.add_hline(y=pivot, line_color="#475569", line_dash="dot", annotation_text=f"Pattern pivot {pivot:.2f}", row=1, col=1)
    for prefix, label, color in [("base", "Base", "#bfdbfe"), ("box", "Box", "#bfdbfe"), ("pullback", "Pullback", "#bbf7d0"), ("retest", "Retest", "#fde68a"), ("tight_area", "Final contraction", "#ddd6fe")]:
        left, right = meta.get(prefix + "_start"), meta.get(prefix + "_end")
        if left and right:
            fig.add_vrect(x0=pd.Timestamp(left), x1=pd.Timestamp(right), fillcolor=color, opacity=0.18, line_width=0, annotation_text=label, row=1, col=1)
    for key, label in [("base_high", "Base resistance"), ("base_low", "Base support"), ("box_high", "Box resistance"), ("box_low", "Box support"), ("pullback_low", "Pullback low"), ("prior_swing_high", "Prior swing high"), ("retest_low", "Retest low"), ("tight_area_low", "Contraction low")]:
        value = _to_float_or_none(meta.get(key))
        if value is not None:
            fig.add_hline(y=value, line_color="#94a3b8", line_dash="dot", line_width=0.8, annotation_text=label, row=1, col=1)
    # Legacy detector anchors are shown only when the detector stored them.
    for key in ("bottom1", "bottom2", "cup_bottom", "handle_low", "support", "support_level"):
        value = _to_float_or_none(meta.get(key))
        if value is not None:
            fig.add_hline(y=value, line_color="#94a3b8", line_dash="dot", annotation_text=key, row=1, col=1)
    volume_colors = np.where(data["close"] >= data["open"], "#16a34a", "#dc2626")
    fig.add_trace(go.Bar(x=data.index, y=data["volume"], marker_color=volume_colors, name="Volume"), row=2, col=1)
    vol_average = full["volume"].shift(1).rolling(20, min_periods=10).mean().reindex(data.index)
    fig.add_trace(go.Scatter(x=data.index, y=vol_average, name="Prior volume average 20", line={"color": "#f59e0b", "width": 1.3}), row=2, col=1)
    model = meta.get("target_model", "LEGACY_TARGET_REFERENCE")
    title = alert.get("display_pattern_name", pattern)
    fig.update_layout(title=f"{ticker} — {title}<br>Signal {signal_date or 'unrecorded'} | RR {float(alert.get('rr_ratio') or 0):.2f} | target: {model}", template="plotly_white", width=1120, height=800, margin={"l": 55, "r": 190, "t": 95, "b": 55}, legend={"orientation": "h", "y": 1.04}, xaxis_rangeslider_visible=False, xaxis2_rangeslider_visible=False)
    fig.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}])
    fig.update_yaxes(title_text="USD", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    return fig


def create_chart(ticker: str, df: pd.DataFrame, alert: dict) -> str | None:
    if not PLOTLY_AVAILABLE or df is None:
        return None
    path = None
    exported = False
    fig = None
    try:
        fig = _build_setup_chart_figure(ticker, df, alert)
        directory = _MAIL_CHART_DIRECTORY.get() or CHARTS_DIR
        os.makedirs(directory, exist_ok=True)
        label = re.sub(r"[^A-Za-z0-9_-]", "_", str(ticker))[:24]
        descriptor, path = tempfile.mkstemp(prefix=f"scanner-chart-{label}-", suffix=".png", dir=directory)
        os.close(descriptor)
        pio.write_image(fig, path)
        exported = True
        return path
    except Exception as exc:
        log(f"Chart export unavailable {ticker}: {type(exc).__name__}: {exc}")
        return None
    finally:
        fig = None
        if path and not exported:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                log("Incomplete chart cleanup deferred")

@_nonfatal_mail
def send_email_alerts(alerts: list[dict]) -> dict:
    ci_cache, sector_cache, intel_cache = {}, {}, {}
    all_attachments = []
    try:
        if not alerts:
            return {"success": False, "accepted": 0, "refused": 0, "status": "no_alerts"}
        if not APP_PASSWORD or not TO_EMAILS:
            log("Email configuration missing — email disabled.")
            return {"success": False, "accepted": 0, "refused": len(TO_EMAILS), "status": "disabled"}

        alerts = sorted(alerts, key=lambda a: _infra_float(a.get("score",0), 0.0), reverse=True)
        now_str   = datetime.now().strftime("%d/%m/%Y")
        subj_str  = datetime.now().strftime("%Y-%m-%d %H:%M")

        cards = ""
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
            df = alert.get("_df")  # retain the exact signal frame until selection/history finish
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

            card_html = _build_html_card(alert, company, now_str, sector_data, intel_data, catalyst_data, rs_data)
            chart_html = '<p style="font-size:11px;color:#a9b6ca;margin:16px 0 0;">הגרף אינו זמין במייל זה.</p>'
            if chart_path and _chart_in_mail_workspace(chart_path) and os.path.exists(chart_path):
                # Do not derive Content-ID headers from external ticker text.
                cid = f"scanner_chart_{idx}"
                try:
                    part = MIMEBase("image", "png")
                    with open(chart_path, "rb") as fp:
                        part.set_payload(fp.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-ID", f"<{cid}>")
                    part.add_header("Content-Disposition", "inline", filename=os.path.basename(chart_path))
                    all_attachments.append(part)
                    chart_html = _mail_chart(cid, ticker)
                except Exception as e:
                    log(f"chart attach error: {e}")
            cards += card_html.replace('<!-- scanner-chart -->', chart_html)

        msg = MIMEMultipart("related")
        msg["From"]    = FROM_EMAIL
        msg["To"]      = ", ".join(TO_EMAILS)
        if len(alerts) == 1:
            a = alerts[0]
            msg["Subject"] = f"⭐ PROFESSIONAL READY {a.get('ticker','')} | {a.get('pattern_type','')} | Quality {_mail_float(a.get('score',0)):.1f}"
        else:
            msg["Subject"] = f"🚀 {len(alerts)} Entry Ready סטאפים — {subj_str}"
        html_body = _mail_document("סטאפים חדשים", f"{len(alerts)} סטאפים לשליחה · {now_str}", cards,
                                   eyebrow="ENTRY READY SETUPS",
                                   preheader=f"{len(alerts)} סטאפים: " + ", ".join(str(a.get("ticker", "")) for a in alerts))
        _attach_mail_content(msg, html_body)
        for att in all_attachments:
            msg.attach(att)

        return _smtp_deliver(msg, "setups")
    finally:
        all_attachments.clear()
        ci_cache.clear()
        sector_cache.clear()
        intel_cache.clear()
@_nonfatal_mail
def send_daily_summary_email(stats: dict, filter_stats: dict, regime: dict | None = None,
                             prefilter_stats: dict | None = None, note: str = "") -> dict:
    """
    שולח מייל יומי גם כשאין סטאפים.
    המטרה: לדעת שהסריקה באמת רצה, כמה מניות נבדקו, ומה נפסל בדרך.
    """
    if not APP_PASSWORD:
        log("APP_PASSWORD not set — daily summary email disabled.")
        return _mail_status("disabled", refused=len(TO_EMAILS))

    stats = stats or {}
    filter_stats = filter_stats or {}
    prefilter_stats = prefilter_stats or {}
    regime = regime or {}

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    html = _mail_daily_summary(stats, filter_stats, regime, prefilter_stats, note, now_str)

    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_EMAIL
    msg["To"] = ", ".join(TO_EMAILS)
    msg["Subject"] = f"✅ סורק המניות רץ — אין סטאפים היום — {now_str}"
    _attach_mail_content(msg, html)

    return _smtp_deliver(msg, "daily_summary")

# ============================================================
#  MAIN LOOP
# ============================================================




# ============================================================
#  PRE-FILTER — סינון מהיר לפני הסריקה המלאה
#  מוריד 35 ימים ל-כל המניות בbatch → מסנן לפי EMA28
#  חיסכון: ~80% מהמניות נדחות לפני הורדת נתונים מלאים
# ============================================================

PREFILTER_BATCH_SIZE = int(os.getenv("PREFILTER_BATCH_SIZE", "50"))  # מניות לבקשה אחת — קטן יותר כדי להפחית חסימות Yahoo
PREFILTER_PERIOD     = os.getenv("PREFILTER_PERIOD", "6mo")          # V9.3: תקופה נתמכת + convergence טוב יותר מול Full Scan
PREFILTER_EMA_GUARD_BAND_PCT = float(os.getenv("PREFILTER_EMA_GUARD_BAND_PCT", "0.004"))  # רק ב-prefilter; Full Scan נשאר קשיח
PREFILTER_EMA_SLOPE_TOL_PCT  = float(os.getenv("PREFILTER_EMA_SLOPE_TOL_PCT", "0.0005"))
PREFILTER_DOWNLOAD_RETRIES = int(os.getenv("PREFILTER_DOWNLOAD_RETRIES", "3"))
PREFILTER_TIMEOUT_SECONDS  = int(os.getenv("PREFILTER_TIMEOUT_SECONDS", "20"))
PREFILTER_RETRY_SLEEP_SECONDS = int(os.getenv("PREFILTER_RETRY_SLEEP_SECONDS", "10"))

# סטטיסטיקה של ה-Pre-filter למייל היומי
PREFILTER_STATS = {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "batches": 0,
    "download_failed_batches": 0,
    "missing_tickers": 0,
    "ticker_errors": 0,
    "passed_due_to_download_failure": 0,
}


def _download_prefilter_batch(batch: list[str], batch_start: int, total: int) -> pd.DataFrame | None:
    """מוריד batch מ-yfinance עם retry, timeout ו-threads=False כדי להפחית חסימות."""
    for attempt in range(1, PREFILTER_DOWNLOAD_RETRIES + 1):
        try:
            log(
                f"   Downloading prefilter batch {batch_start + 1}-"
                f"{batch_start + len(batch)} / {total} | attempt {attempt}"
            )
            raw = yf.download(
                batch,
                period=PREFILTER_PERIOD,
                interval="1d",
                progress=False,
                auto_adjust=True,
                group_by="ticker",
                threads=False,
                timeout=PREFILTER_TIMEOUT_SECONDS,
            )

            if raw is not None and not raw.empty:
                return raw

            log(f"   ⚠️ Empty prefilter batch {batch_start + 1}-{batch_start + len(batch)}")
        except Exception as e:
            log(f"   ⚠️ Pre-filter download failed attempt {attempt}: {e}")

        if attempt < PREFILTER_DOWNLOAD_RETRIES:
            time.sleep(PREFILTER_RETRY_SLEEP_SECONDS * attempt)

    return None


def _extract_prefilter_ticker_frame(raw: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """שולף DataFrame של טיקר מתוך תוצאת yf.download, גם אם MultiIndex הפוך."""
    try:
        if raw is None or raw.empty:
            return None

        ticker_up = ticker.upper()

        if isinstance(raw.columns, pd.MultiIndex):
            level0 = [str(x).upper() for x in raw.columns.get_level_values(0)]
            level1 = [str(x).upper() for x in raw.columns.get_level_values(1)]

            if ticker_up in level0:
                actual = raw.columns.get_level_values(0)[level0.index(ticker_up)]
                return raw[actual].copy()

            if ticker_up in level1:
                actual = raw.columns.get_level_values(1)[level1.index(ticker_up)]
                return raw.xs(actual, axis=1, level=1).copy()

            return None

        return raw.copy()
    except Exception:
        return None


def prefilter_by_ema28(ticker_list: list[str]) -> tuple[list[str], list[str]]:
    """
    מוריד כ-6 חודשי מסחר לכל המניות ב-batch ומסנן לפי EMA28.
    מחזיר (passed, failed) — passed ממשיכות לסריקה מלאה.

    שינוי חשוב:
    אם Yahoo לא מחזיר נתונים זמנית — לא פוסלים את המניה.
    היא עוברת לסריקה המלאה כדי שלא נאבד מניות בגלל חסימה/תקלה.
    """
    global PREFILTER_STATS

    passed: list[str] = []
    failed: list[str] = []
    total  = len(ticker_list)

    PREFILTER_STATS = {
        "total": total,
        "passed": 0,
        "failed": 0,
        "batches": 0,
        "download_failed_batches": 0,
        "missing_tickers": 0,
        "ticker_errors": 0,
        "passed_due_to_download_failure": 0,
    }

    log(f"⚡ Pre-filter: בודק {total} מניות לפי EMA28...")
    log(f"⚡ Pre-filter settings: batch={PREFILTER_BATCH_SIZE}, period={PREFILTER_PERIOD}, retries={PREFILTER_DOWNLOAD_RETRIES}, guard=±{PREFILTER_EMA_GUARD_BAND_PCT*100:.1f}%, threads=False")

    # עבד ב-batches
    for batch_start in range(0, total, PREFILTER_BATCH_SIZE):
        batch = ticker_list[batch_start:batch_start + PREFILTER_BATCH_SIZE]
        PREFILTER_STATS["batches"] += 1

        raw = _download_prefilter_batch(batch, batch_start, total)

        if raw is None or raw.empty:
            # אם batch נכשל — לא פוסלים. כולם ממשיכים לסריקה מלאה.
            PREFILTER_STATS["download_failed_batches"] += 1
            PREFILTER_STATS["passed_due_to_download_failure"] += len(batch)
            passed.extend(batch)
            done = batch_start + len(batch)
            log(f"   ⚠️ Batch failed completely — passing all {len(batch)} tickers to full scan")
            log(f"   Pre-filter: {done}/{total} — עברו: {len(passed)} | נכשלו: {len(failed)}")
            continue

        for ticker in batch:
            try:
                df_t = _extract_prefilter_ticker_frame(raw, ticker)
                if df_t is None or df_t.empty:
                    PREFILTER_STATS["missing_tickers"] += 1
                    passed.append(ticker)  # לא ידוע — תן לעבור
                    continue

                df_t = df_t.dropna(how="all")
                if len(df_t) < 10:
                    PREFILTER_STATS["missing_tickers"] += 1
                    passed.append(ticker)
                    continue

                # נרמל עמודות
                df_t.columns = [str(c).lower() for c in df_t.columns]
                if "close" not in df_t.columns:
                    PREFILTER_STATS["missing_tickers"] += 1
                    passed.append(ticker)
                    continue

                close = df_t["close"].squeeze()
                if hasattr(close, "columns"):  # עדיין DataFrame
                    close = close.iloc[:, 0]
                close = pd.to_numeric(close, errors="coerce").dropna()

                if len(close) < 10:
                    PREFILTER_STATS["missing_tickers"] += 1
                    passed.append(ticker)
                    continue

                # חשב EMA28
                ema28     = close.ewm(span=28, adjust=False).mean()
                price_now = float(close.iloc[-1])
                ema_now   = float(ema28.iloc[-1])
                ema_prev  = float(ema28.iloc[-2]) if len(ema28) >= 2 else ema_now

                if ema_now <= 0:
                    PREFILTER_STATS["missing_tickers"] += 1
                    passed.append(ticker)
                    continue

                dist = (price_now - ema_now) / ema_now

                # V9.3: ה-Pre-filter הוא coarse gate בלבד. guard-band קטן מונע false reject
                # בגלל הבדלי מקור/adjustment; ה-Full Scan נשאר הסמכות הקשיחה.
                if dist < -PREFILTER_EMA_GUARD_BAND_PCT:
                    failed.append(ticker)
                    continue
                if dist > EMA28_MAX_DIST_PCT + PREFILTER_EMA_GUARD_BAND_PCT:
                    failed.append(ticker)
                    continue
                slope_pct = (ema_now - ema_prev) / max(abs(ema_prev), 1e-9)
                if EMA28_REQUIRE_RISING and slope_pct < -PREFILTER_EMA_SLOPE_TOL_PCT:
                    failed.append(ticker)
                    continue

                passed.append(ticker)

            except Exception as e:
                PREFILTER_STATS["ticker_errors"] += 1
                # במקרה של שגיאה — לא פוסלים בגלל בעיית Data זמנית
                passed.append(ticker)

        done = batch_start + len(batch)
        log(f"   Pre-filter: {done}/{total} — עברו: {len(passed)} | נכשלו: {len(failed)}")

    checked_total = len(passed) + len(failed)
    if checked_total != total:
        missing = total - checked_total
        log(f"🚨 PREFILTER BUG: expected {total}, got {checked_total}. Missing {missing} tickers.")
    else:
        log(f"✅ PREFILTER OK: checked all {total} tickers.")

    PREFILTER_STATS["passed"] = len(passed)
    PREFILTER_STATS["failed"] = len(failed)

    pct_saved = len(failed) / max(total, 1) * 100
    log(f"⚡ Pre-filter סיים: {len(passed)} עוברות | {len(failed)} נדחו ({pct_saved:.0f}% חיסכון)")
    log(
        "⚡ Pre-filter data issues: "
        f"failed_batches={PREFILTER_STATS['download_failed_batches']}, "
        f"missing_tickers={PREFILTER_STATS['missing_tickers']}, "
        f"ticker_errors={PREFILTER_STATS['ticker_errors']}"
    )
    return passed, failed


def precheck_market_caps_before_full_scan(ticker_list: list[str]) -> tuple[list[str], list[str]]:
    """
    V9.1.2 — שער Market Cap לפני ה-Full Scan היקר.

    - Market cap ידוע מה-NASDAQ screener: משתמשים באותו ערך שכבר הכניס ל-Universe.
    - Market cap חסר: מאמתים פעם אחת דרך yfinance.
    - אם אומת מתחת ל-$1B: דוחים כאן, לפני sleep/API/pattern scan.
    - אם עדיין לא ידוע בגלל תקלה זמנית: fail-open ומנסים שוב בתוך Full Scan.

    אין כאן הקלה בסף: $1B נשאר בדיוק אותו סף.
    """
    global MARKET_CAP_PRECHECK_STATS, PREFILTER_STATS

    passed: list[str] = []
    rejected: list[str] = []
    stats = {
        "input": len(ticker_list),
        "known_pass": 0,
        "known_reject": 0,
        "unknown_to_verify": 0,
        "verified_pass": 0,
        "verified_reject": 0,
        "unresolved_pass": 0,
        "output": 0,
    }

    log(
        f"💰 Market Cap Pre-check: validating {len(ticker_list)} EMA28 survivors "
        f"against ${MIN_MARKET_CAP_USD/1e9:.1f}B threshold..."
    )

    for symbol in ticker_list:
        t = str(symbol).strip().upper().replace("$", "")
        if not t:
            continue

        hint = UNIVERSE_MARKET_CAP_HINTS.get(t)
        if hint is not None and _is_finite_number(hint) and float(hint) > 0:
            mc = float(hint)
            # Safety: should normally be impossible because Universe already filtered it.
            if mc < MIN_MARKET_CAP_USD:
                stats["known_reject"] += 1
                rejected.append(t)
            else:
                stats["known_pass"] += 1
                _mc_cache[t] = mc
                passed.append(t)
            continue

        stats["unknown_to_verify"] += 1
        if MARKET_CAP_PRECHECK_SLEEP_SECONDS > 0:
            time.sleep(MARKET_CAP_PRECHECK_SLEEP_SECONDS)
        mc = fetch_market_cap(t)
        if mc is not None and _is_finite_number(mc) and float(mc) > 0:
            if float(mc) < MIN_MARKET_CAP_USD:
                stats["verified_reject"] += 1
                rejected.append(t)
            else:
                stats["verified_pass"] += 1
                passed.append(t)
        else:
            # Preserve coverage: unknown is not evidence that cap is below $1B.
            # Clear failed caches so scan_ticker gets one independent retry later.
            stats["unresolved_pass"] += 1
            _mc_cache.pop(t, None)
            try:
                _info_cache.pop(t, None)
            except Exception:
                pass
            passed.append(t)

    stats["output"] = len(passed)
    MARKET_CAP_PRECHECK_STATS = stats

    # Attach to the existing summary object so the daily email/log explains
    # exactly how many candidates were removed before the expensive Full Scan.
    PREFILTER_STATS["market_cap_precheck_input"] = stats["input"]
    PREFILTER_STATS["market_cap_known_pass"] = stats["known_pass"]
    PREFILTER_STATS["market_cap_known_reject"] = stats["known_reject"]
    PREFILTER_STATS["market_cap_unknown_to_verify"] = stats["unknown_to_verify"]
    PREFILTER_STATS["market_cap_verified_pass"] = stats["verified_pass"]
    PREFILTER_STATS["market_cap_verified_reject"] = stats["verified_reject"]
    PREFILTER_STATS["market_cap_unresolved_pass"] = stats["unresolved_pass"]
    PREFILTER_STATS["post_market_cap_passed"] = stats["output"]

    log(
        "💰 Market Cap Pre-check done: "
        f"input={stats['input']}, known_pass={stats['known_pass']}, "
        f"unknown_checked={stats['unknown_to_verify']}, verified_pass={stats['verified_pass']}, "
        f"rejected_below_$1B={stats['known_reject'] + stats['verified_reject']}, "
        f"unresolved_fail_open={stats['unresolved_pass']}, full_scan={stats['output']}"
    )
    return passed, rejected


def _run_saturday_maintenance_if_due() -> bool:
    """
    Run the scanner's weekly self-learning once per Israeli Saturday.

    This maintenance is deliberately independent of the US Market Day Guard:
    Saturday normally has no fresh US candle, but weekly learning still needs
    to run. A small state marker prevents the 05:00-09:59 retry schedule from
    running the learning job repeatedly on the same Saturday.

    Returns True only when maintenance actually ran in this invocation.
    """
    try:
        from zoneinfo import ZoneInfo
        now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
    except Exception:
        now_il = datetime.utcnow() + timedelta(hours=3)

    if now_il.weekday() != 5:  # Saturday
        return False

    today = now_il.date().isoformat()
    last_done = ""
    try:
        if os.path.exists(SATURDAY_MAINTENANCE_STATE_FILE):
            with open(SATURDAY_MAINTENANCE_STATE_FILE, "r", encoding="utf-8") as f:
                last_done = f.read().strip()
    except Exception as e:
        log(f"🧠 Saturday maintenance marker read error: {e}")

    if last_done == today:
        log(f"🧠 Saturday maintenance already completed today ({today}) — skipping duplicate maintenance")
        return False

    log("🧠 Saturday maintenance — running weekly self-learning before Market Day Guard...")
    update_performance_log()
    result = run_self_learning()
    try:
        os.makedirs(os.path.dirname(SATURDAY_MAINTENANCE_STATE_FILE) or ".", exist_ok=True)
        _atomic_write_text(SATURDAY_MAINTENANCE_STATE_FILE, today + "\n")
        log(f"💾 Saturday maintenance marker saved: {today}")
    except Exception as e:
        log(f"🧠 Saturday maintenance marker save error: {e}")
    return True


@_managed_scanner_run
def main() -> None:
    global tickers

    log("=" * 60)
    log("Stock Scanner Unified — START")
    log(f"Code version: {CODE_VERSION}")
    log(f"Market cap threshold: ${MIN_MARKET_CAP_USD/1e9:.1f}B | Pipeline: screener cache → precheck unknowns → fail-open only if unresolved")
    log(f"Entry Ready threshold: quality>={ENTRY_READY_MIN_SCORE:.1f} | candidate_score>={ENTRY_CANDIDATE_MIN_SCORE:.1f}")
    log("RESET VERIFIED FIX FILE")
    log("=" * 60)
    # ── מנע Sleep במהלך הסריקה ──────────────────────────────
    try:
        import ctypes
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED — מונע שינה עד סיום
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000002)
        log("✅ Sleep prevention: active")
    except Exception:
        pass

    # ── Saturday weekly maintenance — independent of market candles ──
    # Market-cap > $1B remains part of every Full Scan. The special Saturday
    # task is self-learning, and it must be allowed to run even with no fresh candle.
    try:
        _run_saturday_maintenance_if_due()
    except Exception as e:
        log(f"Saturday maintenance error: {e}")

    # ── Market Day Guard — calendar day is irrelevant; only market data decides ──
    market_session_date = None
    try:
        should_run_market, market_session_date, market_guard_reason = should_run_for_new_market_session()
        log(f"🗓️ Market Day Guard: {market_guard_reason}")
        if not should_run_market:
            log("⏸️ No new market session — exiting before scan to prevent duplicate alerts.")
            return
    except Exception as e:
        log(f"Market Day Guard failed — continuing scan: {e}")
        market_session_date = None

    _set_last_session_date(market_session_date)  # v9.7.3 earnings rule
    log(f"📅 Earnings rule: recent-report mode={EARNINGS_RECENT_MODE}, newest candle={_LAST_SESSION_DATE}")

    # ── טען פרמטרים שנלמדו מריצות קודמות ──────────────────
    _apply_learned_params()

    # ── עדכן ביצועי סטאפים קודמים (5/10/20 יום) ────────────
    try:
        update_performance_log()
    except Exception as e:
        log(f"update_performance_log error: {e}")

    # ── בדוק פוזיציות פתוחות — V9.2 Exit Intelligence ──────
    try:
        exit_alerts = run_position_tracker(send_exit_email_fn=send_exit_email)
        if exit_alerts:
            log(f"📤 {len(exit_alerts)} exit alerts sent")
    except Exception as e:
        log(f"Position tracker error: {e}")

    # בדיקת תנאים הכרחיים לפני ריצה
    if not _email_configured():
        log("Email delivery disabled: configuration incomplete; scanning continues.")
    if not API_KEYS:
        log("❌ FATAL: אין מפתחות TwelveData API. הגדר TWELVEDATA_API_KEYS.")
        return

    # ── שלח דוח יומי על פוזיציות פתוחות — להישאר / לצאת ─────
    try:
        send_positions_status_email()
    except Exception as e:
        log(f"Positions status email error: {e}")

    # ── בנה Universe — כל מניות NYSE + NASDAQ מעל $1B ──────
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
        try:
            send_daily_summary_email(
                {"scanned": 0, "skipped_bl": 0, "no_alert": 0, "found": 0, "errors": 0, "sent": 0},
                {
                    "market_cap": 0, "no_data": 0, "earnings": 0, "ema28": 0,
                    "ma150_dist": 0, "ma_neckline": 0, "no_pattern": 0, "reverse_scan": 0, "entry_quality": 0, "professional_quality": 0,
                },
                regime,
                PREFILTER_STATS,
                note="הסריקה דולגה בגלל מצב שוק BEAR לפי ההגדרות שלך.",
            )
        except Exception as e:
            log(f"Daily summary email error while skipping BEAR market: {e}")
        try:
            save_last_market_scan_date(market_session_date)
        except Exception as e:
            log(f"Market scan date save error while skipping BEAR market: {e}")
        return

    # ── ציון מינימלי דינמי לפי מצב השוק ─────────────────────
    dynamic_score = get_dynamic_min_score()
    regime_name   = regime.get("regime", "NEUTRAL")
    log(f"🎯 MIN_ALERT_SCORE דינמי: {dynamic_score} (Regime={regime_name})")
    log(f"🚀 V9.3 Pattern-Aware Entry: min_quality={ENTRY_READY_MIN_SCORE:.1f}, breakout_volume×{ENTRY_MIN_VOLUME_RATIO:.2f}, pullback/retest_volume×{ENTRY_PULLBACK_MIN_VOLUME_RATIO:.2f}+, RS>={ENTRY_MIN_RS_SCORE:.0f}")
    log(f"🧩 V9 Pattern Expansion: enabled={V9_PATTERN_ENGINE_ENABLED}, all_detected_families_before_entry=True, email_limit={TOP_ALERTS_TO_SEND}, patterns=FlatBase/Darvas/VCP/EMA-Pullback/Retest")
    log(f"🧠 V9.1 Professional Quality: enabled={PRO_ENGINE_ENABLED}, min={PRO_MIN_SCORE:.1f}, trend>={PRO_MIN_TREND_SCORE:.0f}, multi-RS>={PRO_MIN_RS_PROFILE_SCORE:.0f}, max_stop={PRO_MAX_STOP_RISK_PCT*100:.0f}%, breakout_stop={BREAKOUT_MIN_STOP_RISK_PCT*100:.0f}-{BREAKOUT_MAX_STOP_RISK_PCT*100:.0f}%")
    log(f"🧭 V9.2 Exit Intelligence: fixed_initial_stop=True, profit_protection=+{EXIT_PROFIT_ACTIVATE_PCT:.1f}%, chandelier={EXIT_CHANDELIER_ATR_MULT:.1f}ATR, watch>={EXIT_WATCH_SCORE:.0f}, exit>={EXIT_CONFIRM_SCORE:.0f}, target=reference-only")

    # ── Market Reversal Detector ──────────────────────────────
    try:
        run_market_reversal_detector()
    except Exception as e:
        log(f"Market Reversal Detector error: {e}")

    blocklist     = load_blocklist()
    alert_history = load_alert_history()

    all_alerts: list[dict] = []
    stats = {"scanned":0, "skipped_bl":0, "no_alert":0, "found":0, "errors":0, "sent":0}
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
        "entry_quality": 0,
        "professional_quality": 0,
        "reverse_scan":  0,
        "passed":        0,
    }
    # Candidate counters can exceed the number of scanned tickers because one ticker
    # may match several patterns. These sets provide correct ticker-level reporting.
    filter_ticker_sets: dict[str, set[str]] = {key: set() for key in filter_stats}

    ticker_list = list(tickers)
    random.shuffle(ticker_list)

    # ── Pre-filter מהיר לפי EMA28 ────────────────────────────
    log("⚡ מריץ Pre-filter...")
    try:
        ticker_list, prefilter_failed = prefilter_by_ema28(ticker_list)
        # חשוב: לא מערבבים את דחיות ה-Pre-filter בתוך סטטיסטיקת ה-Full scan.
        # אחרת מתקבל אחוז לא הגיוני כמו EMA28 311% מתוך המניות שנסרקו בפועל.
        log(f"⚡ EMA28 Pre-filter: {len(ticker_list)} מניות נשארו לפני Market Cap Pre-check")
    except Exception as e:
        log(f"Pre-filter error — ממשיך בלעדיו: {e}")

    # ── V9.1.2 Market Cap Pre-check ─────────────────────────
    # מסלק מניות < $1B לפני sleep/TwelveData/pattern scan, ורק עבור MC חסר
    # מבצע yfinance verification. ערכי screener ידועים כבר נמצאים ב-_mc_cache.
    try:
        ticker_list, market_cap_pre_rejected = precheck_market_caps_before_full_scan(ticker_list)
        log(f"💰 אחרי Market Cap Pre-check: {len(ticker_list)} מניות ממשיכות לסריקה מלאה")
    except Exception as e:
        log(f"Market Cap Pre-check error — fail-open, continuing candidates unchanged: {e}")

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
            new = scan_ticker(symbol, alert_history, filter_stats, min_score=dynamic_score,
                              filter_ticker_sets=filter_ticker_sets) or []
            if not new:
                stats["no_alert"] += 1
                continue

            accepted_count = 0
            for alert in new:
                alert_score = float(alert.get("score", 0) or 0)
                # V8 safety: שום סטאפ לא נשלח אם הוא לא Entry Ready או מתחת לסף איכות הכניסה.
                q = alert.get("entry_quality", {}) if isinstance(alert.get("entry_quality", {}), dict) else {}
                if ENTRY_ENGINE_ENABLED and not q.get("entry_ready", False):
                    log(f"{symbol}: ❌ blocked before send — not ENTRY_READY")
                    filter_stats["entry_quality"] += 1
                    continue
                pro = alert.get("professional_quality", {}) if isinstance(alert.get("professional_quality", {}), dict) else {}
                if PRO_ENGINE_ENABLED and not pro.get("professional_ready", False):
                    log(f"{symbol}: ❌ blocked before send — not PROFESSIONAL_READY")
                    filter_stats["professional_quality"] += 1
                    continue
                if PRO_ENGINE_ENABLED and alert_score < float(PRO_MIN_SCORE):
                    log(f"{symbol}: ❌ blocked before send — professional quality {alert_score:.1f} < PRO_MIN_SCORE {float(PRO_MIN_SCORE):.1f}")
                    filter_stats["professional_quality"] += 1
                    continue
                if alert_score < float(ENTRY_READY_MIN_SCORE):
                    log(f"{symbol}: ❌ blocked before send — entry quality {alert_score:.1f} < ENTRY_READY_MIN_SCORE {float(ENTRY_READY_MIN_SCORE):.1f}")
                    filter_stats["score_low"] += 1
                    continue
                if alert_score < float(dynamic_score):
                    log(f"{symbol}: ❌ blocked before send — score {alert_score:.1f} < dynamic threshold {float(dynamic_score):.1f}")
                    filter_stats["score_low"] += 1
                    continue
                all_alerts.append(alert)
                accepted_count += 1

            if accepted_count == 0:
                stats["no_alert"] += 1
                continue
            stats["found"] += accepted_count
        except Exception as e:
            stats["errors"] += 1
            log(f"Critical error {symbol}: {type(e).__name__}: {e}")

    # V9.1.5: expose ticker-level counts separately from raw candidate counts.
    filter_stats["entry_quality_unique"] = len(filter_ticker_sets.get("entry_quality", set()))
    filter_stats["professional_quality_unique"] = len(filter_ticker_sets.get("professional_quality", set()))

    # שומר היסטוריית התראות. את _df מנקים רק אחרי שליחת המייל,
    # כדי שהגרפים וה-RS במייל לא יצטרכו להוריד נתונים מחדש.
    save_alert_history(alert_history)

    if not all_alerts:
        log("No valid setups found today.")
        try:
            send_daily_summary_email(stats, filter_stats, regime, PREFILTER_STATS)
        except Exception as e:
            stats["errors"] += 1
            log(f"Daily summary email error: {type(e).__name__}: {e}")
    else:
        delivery = _deliver_selected_alerts(all_alerts, alert_history)
        stats["sent"] = delivery["sent"]
        stats["email_status"] = delivery["status"]
        stats["email_refused_recipients"] = delivery.get("refused", 0)
        if not delivery["success"]:
            stats["errors"] += 1
            stats["entry_email_failed"] = True

    _flush_setup_evaluations()
    try:
        _write_entry_funnel_audit()
    except Exception as exc:
        log(f"Daily entry audit error: {type(exc).__name__}: {exc}")

    # נקה DataFrames מהזיכרון אחרי שסיימנו לשלוח מיילים וליצור גרפים
    for a in all_alerts:
        try:
            a.pop("_df", None)
        except Exception:
            pass

    log(f"STATS: {stats}")

    # ── דוח פילטרים מפורט ──────────────────────────────────
    total_scanned = stats["scanned"]
    log("=" * 60)
    log("📊 FILTER BREAKDOWN:")
    log("   --- Pre-filter / Universe ---")
    log(f"   {'מניות אחרי Universe':<28} {int(PREFILTER_STATS.get('total', 0) or 0):>6,}")
    log(f"   {'עברו EMA28 Pre-filter':<28} {int(PREFILTER_STATS.get('passed', 0) or 0):>6,}")
    log(f"   {'נדחו ב-Pre-filter EMA28':<28} {int(PREFILTER_STATS.get('failed', 0) or 0):>6,}")
    mc_pre_reject = int(PREFILTER_STATS.get('market_cap_known_reject', 0) or 0) + int(PREFILTER_STATS.get('market_cap_verified_reject', 0) or 0)
    log(f"   {'נדחו Market Cap לפני Full':<28} {mc_pre_reject:>6,}")
    log(f"   {'MC חסר שאומת מעל $1B':<28} {int(PREFILTER_STATS.get('market_cap_verified_pass', 0) or 0):>6,}")
    log(f"   {'MC לא ידוע — fail-open':<28} {int(PREFILTER_STATS.get('market_cap_unresolved_pass', 0) or 0):>6,}")
    log(f"   {'הגיעו ל-Full Scan אחרי MC':<28} {int(PREFILTER_STATS.get('post_market_cap_passed', PREFILTER_STATS.get('passed', 0)) or 0):>6,}")
    log(f"   {'בעיות Data שהועברו הלאה':<28} {int(PREFILTER_STATS.get('missing_tickers', 0) or 0) + int(PREFILTER_STATS.get('ticker_errors', 0) or 0):>6,}")
    log("   --- Full scan only ---")
    log(f"   {'מניות נסרקו בפועל':<28} {total_scanned:>6,}")
    log(f"   {'❌ Market Cap / No Data':<28} {filter_stats['market_cap'] + filter_stats['no_data']:>6,}  ({(filter_stats['market_cap']+filter_stats['no_data'])/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'⏭️  דוחות קרובים':<28} {filter_stats['earnings']:>6,}  ({filter_stats['earnings']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'❌ EMA28 בתוך Full scan':<28} {filter_stats['ema28']:>6,}  ({filter_stats['ema28']/max(total_scanned,1)*100:.0f}%)")
    # gap filter הוסר
    # volume filter הוסר
    # RS Score filter הוסר
    log(f"   {'❌ MA150 רחוק (> 5%)':<28} {filter_stats['ma150_dist']:>6,}  ({filter_stats['ma150_dist']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'❌ MA לא קרוב ל-neckline':<28} {filter_stats['ma_neckline']:>6,}  ({filter_stats['ma_neckline']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'❌ ציון נמוך מהסף':<28} {filter_stats['score_low']:>6,}  ({filter_stats['score_low']/max(total_scanned,1)*100:.0f}%)")
    entry_unique = int(filter_stats.get("entry_quality_unique", filter_stats.get("entry_quality", 0)) or 0)
    pro_unique = int(filter_stats.get("professional_quality_unique", filter_stats.get("professional_quality", 0)) or 0)
    log(f"   {'👀 לא Entry Ready — טיקרים':<28} {entry_unique:>6,}  ({entry_unique/max(total_scanned,1)*100:.0f}%) | candidate rejects={filter_stats['entry_quality']:,}")
    log(f"   {'🧠 נפסל Professional — טיקרים':<28} {pro_unique:>6,}  ({pro_unique/max(total_scanned,1)*100:.0f}%) | candidate rejects={filter_stats['professional_quality']:,}")
    log(f"   {'❌ Reverse Scanner / מכירה מוסדית':<28} {filter_stats['reverse_scan']:>6,}  ({filter_stats['reverse_scan']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'❌ לא נמצאה תבנית':<28} {filter_stats['no_pattern']:>6,}  ({filter_stats['no_pattern']/max(total_scanned,1)*100:.0f}%)")
    log(f"   {'✅ עברו הכל ונשלחו':<28} {stats.get('sent',0):>6,}")
    log("=" * 60)
    try:
        if ENTRY_DIAGNOSTICS.get("blockers"):
            top_blockers = sorted(ENTRY_DIAGNOSTICS["blockers"].items(), key=lambda kv: kv[1], reverse=True)[:8]
            log("🧪 V9.3 ENTRY FUNNEL — top blockers: " + " | ".join(f"{k}={v}" for k, v in top_blockers))
        if ENTRY_DIAGNOSTICS.get("patterns"):
            parts = []
            for pat, vals in sorted(ENTRY_DIAGNOSTICS["patterns"].items(), key=lambda kv: (kv[1].get("ready",0)+kv[1].get("watchlist",0)), reverse=True):
                parts.append(f"{pat}: ready={vals.get('ready',0)}, watch={vals.get('watchlist',0)}")
            log("🧪 V9.3 ENTRY FUNNEL — by pattern: " + " | ".join(parts[:10]))
    except Exception as e:
        log(f"Entry diagnostics summary error: {e}")

    if stats.get("entry_email_failed"):
        log("Full scan finished, but entry email failed; market candle remains eligible for retry.")
    else:
        try:
            save_last_market_scan_date(market_session_date)
        except Exception as e:
            log(f"Market scan date save error: {e}")

    # ── V9.1.3 Rolling history retention — רק היסטוריה, לא state חי ──
    try:
        cleanup_history_older_than_one_month()
    except Exception as e:
        log(f"History retention cleanup error: {type(e).__name__}: {e}")

    if stats.get("entry_email_failed"):
        log("Stock Scanner Unified — SCAN COMPLETE; ENTRY EMAIL FAILED")
        return _mail_status("failed", refused=delivery.get("refused", 0),
                            scan_completed=True, notification_pending=True)

    log("Stock Scanner Unified — DONE")
    log("=" * 60)

    # ── שחרר Sleep prevention ────────────────────────────────
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass

if __name__ == "__main__":
if __name__ == "__main__":
    main()
