"""
Trade Republic Stock Dashboard
==============================

A lightweight, high-performance Streamlit dashboard for analyzing stocks that
are tradable on Trade Republic — WITHOUT ever connecting to the Trade
Republic API.

Data architecture (dual-API, zero cost by default):
    1. Historical candles  -> yfinance (free, no key required)
    2. "Live" price ticks   -> pluggable LiveDataProvider:
         - Alpaca Markets IEX free-tier REST endpoint (optional, needs free
           API keys from https://alpaca.markets) -> true near-real-time US
           quotes
         - yfinance fast_info polling (default, no key needed, works for
           both US tickers AND most European Trade Republic tickers such as
           SAP.DE, ADS.DE, etc.)

Persistence: watchlist.json (local file, survives restarts)

IMPORTANT HONEST ENGINEERING NOTE
----------------------------------
Streamlit is a script-rerun framework, not a persistent WebSocket server.
True "tick-by-tick" WebSocket streaming (e.g. Alpaca's streaming API) cannot
be cleanly rendered inside a normal Streamlit script without a background
thread + a fair amount of extra plumbing that is fragile across Streamlit
versions. The robust, production-safe way to get "second-by-second" updates
in Streamlit is: a lightweight auto-refresh timer (via the
`streamlit-autorefresh` component) that triggers a full rerun every N
seconds, combined with a fast REST/snapshot price lookup on every rerun.
That is what this app does. It is smooth and reliable down to ~2-3 second
intervals, which is more than adequate for a personal dashboard and avoids
exchange rate limits.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

try:
    from streamlit_autorefresh import st_autorefresh

    AUTOREFRESH_AVAILABLE = True
except ImportError:  # graceful fallback if the optional package isn't installed
    AUTOREFRESH_AVAILABLE = False

# yfinance logs every failed request straight to the console (even when we
# already catch the exception and show a clean message in the UI). This
# silences that duplicate noise; our own try/except blocks below handle and
# surface every failure properly inside Streamlit instead.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


# ======================================================================
# CONFIG & CONSTANTS
# ======================================================================

st.set_page_config(
    page_title="TR Stock Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Substrings that reliably indicate a network/DNS-level block rather than
# "ticker doesn't exist" or a genuine Yahoo-side rate limit. Most common
# real-world cause: DNS/ad-blockers (e.g. AdGuard's AdAway list) block
# fc.yahoo.com, which yfinance needs for its cookie/crumb auth handshake.
CONNECTION_ERROR_HINTS = (
    "fc.yahoo.com", "failed to connect", "connection refused",
    "getaddrinfo failed", "name or service not known", "could not connect",
    "max retries exceeded", "newconnectionerror", "nameresolutionerror",
    "read timed out", "connection timed out",
)

WATCHLIST_FILE = Path("watchlist.json")
DEFAULT_REFRESH_SECONDS = 5
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets/v2"

# Popular European stocks tradable on Trade Republic mapped to the
# equivalent yfinance/Yahoo Finance ticker (Yahoo already tracks most
# European exchanges natively, so no third-party mapping service is
# required — the exchange suffix does the job).
EU_US_MAPPING: dict[str, dict] = {
    "SAP": {"name": "SAP SE", "yfinance": "SAP.DE", "note": "Xetra. ADR also trades as SAP on the NYSE."},
    "Adidas": {"name": "Adidas AG", "yfinance": "ADS.DE"},
    "Siemens": {"name": "Siemens AG", "yfinance": "SIE.DE"},
    "Allianz": {"name": "Allianz SE", "yfinance": "ALV.DE"},
    "BASF": {"name": "BASF SE", "yfinance": "BAS.DE"},
    "Volkswagen (Vz)": {"name": "Volkswagen AG", "yfinance": "VOW3.DE"},
    "BMW": {"name": "Bayerische Motoren Werke AG", "yfinance": "BMW.DE"},
    "Deutsche Bank": {"name": "Deutsche Bank AG", "yfinance": "DBK.DE"},
    "Deutsche Telekom": {"name": "Deutsche Telekom AG", "yfinance": "DTE.DE"},
    "Mercedes-Benz": {"name": "Mercedes-Benz Group AG", "yfinance": "MBG.DE"},
    "Airbus": {"name": "Airbus SE", "yfinance": "AIR.PA"},
    "LVMH": {"name": "LVMH Moët Hennessy", "yfinance": "MC.PA"},
    "ASML": {"name": "ASML Holding N.V.", "yfinance": "ASML.AS"},
    "Nestlé": {"name": "Nestlé S.A.", "yfinance": "NESN.SW"},
    "Novo Nordisk": {"name": "Novo Nordisk A/S", "yfinance": "NOVO-B.CO"},
    "Apple": {"name": "Apple Inc.", "yfinance": "AAPL"},
    "Tesla": {"name": "Tesla Inc.", "yfinance": "TSLA"},
    "Nvidia": {"name": "NVIDIA Corp.", "yfinance": "NVDA"},
    "Microsoft": {"name": "Microsoft Corp.", "yfinance": "MSFT"},
    "Amazon": {"name": "Amazon.com Inc.", "yfinance": "AMZN"},
    "Alphabet (Google)": {"name": "Alphabet Inc.", "yfinance": "GOOGL"},
}


# ======================================================================
# DATA MODELS
# ======================================================================

@dataclass
class PriceQuote:
    ticker: str
    price: Optional[float]
    previous_close: Optional[float]
    source: str
    error: Optional[str] = None

    @property
    def change_abs(self) -> Optional[float]:
        if self.price is None or self.previous_close is None:
            return None
        return self.price - self.previous_close

    @property
    def change_pct(self) -> Optional[float]:
        if self.price is None or not self.previous_close:
            return None
        return (self.price - self.previous_close) / self.previous_close * 100

    @property
    def is_up(self) -> bool:
        return (self.change_abs or 0) >= 0


# ======================================================================
# WATCHLIST PERSISTENCE
# ======================================================================

def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except (json.JSONDecodeError, OSError) as e:
        st.warning(f"⚠️ watchlist.json konnte nicht gelesen werden ({e}). Starte mit leerer Liste.")
        return []


def save_watchlist(tickers: list[str]) -> None:
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(tickers, f, indent=2, ensure_ascii=False)
    except OSError as e:
        st.error(f"❌ Watchlist konnte nicht gespeichert werden: {e}")


def add_to_watchlist(ticker: str) -> None:
    ticker = ticker.strip().upper()
    if not ticker:
        return
    if ticker not in st.session_state.watchlist:
        st.session_state.watchlist.append(ticker)
        save_watchlist(st.session_state.watchlist)
        st.toast(f"✅ {ticker} zur Watchlist hinzugefügt")
    else:
        st.toast(f"ℹ️ {ticker} ist bereits in der Watchlist")


def remove_from_watchlist(ticker: str) -> None:
    if ticker in st.session_state.watchlist:
        st.session_state.watchlist.remove(ticker)
        save_watchlist(st.session_state.watchlist)
        st.toast(f"🗑️ {ticker} entfernt")


# ======================================================================
# DATA FETCHING — HISTORICAL (yfinance, cached)
# ======================================================================

TIMEFRAME_OPTIONS = {
    "1 Tag": {"period": "1d", "interval": "2m"},
    "5 Tage": {"period": "5d", "interval": "15m"},
    "1 Monat": {"period": "1mo", "interval": "1h"},
    "3 Monate": {"period": "3mo", "interval": "1d"},
    "6 Monate": {"period": "6mo", "interval": "1d"},
    "1 Jahr": {"period": "1y", "interval": "1d"},
    "2 Jahre": {"period": "2y", "interval": "1wk"},
}


def is_connection_error(msg: str) -> bool:
    m = msg.lower()
    return any(hint in m for hint in CONNECTION_ERROR_HINTS)


def record_yf_error(e: Exception) -> None:
    st.session_state["last_yf_error"] = str(e)
    st.session_state["last_yf_error_is_connection"] = is_connection_error(str(e))
    st.session_state["last_yf_error_ambiguous"] = False


@st.cache_data(ttl=60, show_spinner=False)
def fetch_historical(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV history via yfinance. Cached for 60s to respect rate limits."""
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        return df
    except Exception as e:
        record_yf_error(e)
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_previous_close(ticker: str) -> Optional[float]:
    try:
        info = yf.Ticker(ticker).fast_info
        pc = info.get("previous_close") if hasattr(info, "get") else info.previous_close
        if pc:
            return float(pc)
        # empty-but-no-exception case (silent Yahoo rate limit) -> fall through to history
    except Exception as e:
        record_yf_error(e)
    try:
        df = yf.Ticker(ticker).history(period="5d", interval="1d")
        if df is not None and len(df) >= 2:
            return float(df["Close"].iloc[-2])
    except Exception as e2:
        record_yf_error(e2)
    return None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_company_name(ticker: str) -> str:
    try:
        info = yf.Ticker(ticker).info
        return info.get("longName") or info.get("shortName") or ticker
    except Exception:
        return ticker


# ======================================================================
# DATA FETCHING — LIVE PRICE (Alpaca optional, yfinance fallback)
# ======================================================================

@st.cache_data(ttl=3, show_spinner=False)
def get_live_price_alpaca(ticker: str, api_key: str, api_secret: str) -> PriceQuote:
    """Alpaca free IEX-tier latest trade snapshot. Requires free API keys."""
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    try:
        url = f"{ALPACA_DATA_BASE_URL}/stocks/{ticker}/trades/latest"
        resp = requests.get(url, headers=headers, params={"feed": "iex"}, timeout=4)
        if resp.status_code == 200:
            price = resp.json().get("trade", {}).get("p")
            prev_close = fetch_previous_close(ticker)
            return PriceQuote(ticker, price, prev_close, source="alpaca")
        elif resp.status_code in (401, 403):
            return PriceQuote(ticker, None, None, source="alpaca", error="Ungültige Alpaca API-Keys")
        elif resp.status_code == 404:
            return PriceQuote(ticker, None, None, source="alpaca", error="Symbol bei Alpaca nicht gefunden (nur US-Ticker)")
        else:
            return PriceQuote(ticker, None, None, source="alpaca", error=f"Alpaca HTTP {resp.status_code}")
    except requests.exceptions.RequestException as e:
        return PriceQuote(ticker, None, None, source="alpaca", error=f"Verbindungsfehler: {e}")


@st.cache_data(ttl=3, show_spinner=False)
def get_live_price_yfinance(ticker: str) -> PriceQuote:
    """Fallback / default: no-key polling via yfinance. Works for EU + US tickers."""
    try:
        fi = yf.Ticker(ticker).fast_info
        price = fi.get("last_price") if hasattr(fi, "get") else fi.last_price
        prev_close = fi.get("previous_close") if hasattr(fi, "get") else fi.previous_close
        if price is not None:
            return PriceQuote(ticker, float(price), float(prev_close) if prev_close else None, source="yfinance")

        # fast_info "succeeded" but came back empty. Cause is ambiguous: could be
        # a genuine short-lived Yahoo rate limit, OR a network/DNS block — we
        # don't know without an actual connectivity probe, so we don't guess.
        # Fall back to the plain daily-history endpoint (different, more
        # tolerant code path) before giving up.
        st.session_state["last_yf_error"] = (
            f"fast_info für {ticker} kam ohne Preis zurück (leere Antwort von Yahoo)."
        )
        st.session_state["last_yf_error_is_connection"] = False
        st.session_state["last_yf_error_ambiguous"] = True
        hist = yf.Ticker(ticker).history(period="2d", interval="1d")
        if hist is not None and not hist.empty:
            last_close = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
            return PriceQuote(ticker, last_close, prev, source="yfinance (verzögert, Fallback)")

        return PriceQuote(
            ticker, None, None, source="yfinance",
            error="Yahoo Finance liefert aktuell keine Daten (leere Antwort, auch im Fallback). "
                  "Meist vorübergehend — siehe Verbindungsdiagnose in der Sidebar.",
        )
    except Exception as e:
        record_yf_error(e)
        if is_connection_error(str(e)):
            return PriceQuote(
                ticker, None, None, source="yfinance",
                error="Verbindung zu Yahoo Finance blockiert (fc.yahoo.com nicht erreichbar) — "
                      "siehe Hinweis unten in der Sidebar.",
            )
        return PriceQuote(ticker, None, None, source="yfinance", error=str(e))


def get_live_price(ticker: str) -> PriceQuote:
    use_alpaca = bool(st.session_state.get("alpaca_key")) and bool(st.session_state.get("alpaca_secret"))
    if use_alpaca:
        quote = get_live_price_alpaca(ticker, st.session_state["alpaca_key"], st.session_state["alpaca_secret"])
        if quote.price is not None:
            return quote
        # silent fallback to yfinance if alpaca fails for this ticker (e.g. EU stock)
        return get_live_price_yfinance(ticker)
    return get_live_price_yfinance(ticker)


# ======================================================================
# STRATEGIES — pluggable pattern detectors (Entry / Stop-Loss / Target)
# ======================================================================
#
# Design: every strategy is a function with the signature
#     detect(df: pd.DataFrame, **params) -> StrategySignal
# registered in STRATEGY_REGISTRY under a display name. Adding a new
# strategy later (e.g. "Micro Pullback", "VWAP Reclaim") means writing one
# more function and adding one more registry entry — the UI (selector,
# chart overlay, scanner) picks it up automatically.
#
# HONEST DISCLAIMER: this is a simplified, rule-based approximation of a
# discretionary pattern Ross Cameron reads by eye on 1-5min charts with
# Level 2 order flow. It is a heuristic, not a guarantee of a valid setup,
# and it works best on fine-grained intraday data (1 Tag / 5 Tage). This is
# not financial advice.

@dataclass
class StrategySignal:
    detected: bool
    status: str  # "signal_active" | "signal_past" | "forming" | "none"
    pattern_name: str
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    risk_reward: Optional[float] = None
    pole_start_pos: Optional[int] = None
    pole_end_pos: Optional[int] = None
    flag_start_pos: Optional[int] = None
    flag_end_pos: Optional[int] = None
    breakout_pos: Optional[int] = None
    notes: str = ""


NO_SIGNAL = lambda name: StrategySignal(detected=False, status="none", pattern_name=name)


def detect_bull_flag(
    df: pd.DataFrame,
    pole_window: int = 3,
    min_pole_pct: float = 3.0,
    min_flag_bars: int = 2,
    max_flag_bars: int = 15,
    max_retrace_pct: float = 50.0,
    breakout_recency: int = 3,
    require_volume_decline: bool = True,
) -> StrategySignal:
    """
    Ross Cameron / Warrior-Trading-style bull flag:
      1. Flagpole: a strong, fast move up over `pole_window` bars (>= min_pole_pct),
         driven by a volume spike.
      2. Flag: a contained sideways/down consolidation of min..max_flag_bars bars
         that does not retrace more than `max_retrace_pct` of the pole, and shows
         volume *contraction* relative to the pole (a core Warrior-Trading rule —
         "volume dries up in the flag before the next leg"). Without this check the
         scanner can mistake an unrelated small wiggle for a flag; with it, only
         genuine pole -> quiet-pause -> breakout structures qualify.
      3. Breakout: a bar closes above the pole high -> Entry.
    Entry = pole high (breakout level) | Stop = flag low | Target = entry + pole height
    (measured-move projection). Scans from the most recent bars backwards so the
    freshest valid pattern wins.
    """
    name = "Bull Flag (Warrior Trading Style)"
    n = len(df)
    if n < pole_window + min_flag_bars + 1:
        return NO_SIGNAL(name)

    highs, lows, opens, closes = df["High"].values, df["Low"].values, df["Open"].values, df["Close"].values
    volumes = df["Volume"].values if "Volume" in df.columns else None

    for pole_end_pos in range(n - 1, pole_window - 1, -1):
        pole_start_pos = pole_end_pos - pole_window + 1
        pole_open = opens[pole_start_pos]
        pole_close = closes[pole_end_pos]
        if not pole_open:
            continue
        pole_pct = (pole_close - pole_open) / pole_open * 100
        if pole_pct < min_pole_pct:
            continue

        pole_high = float(highs[pole_start_pos:pole_end_pos + 1].max())
        pole_low = float(lows[pole_start_pos:pole_end_pos + 1].min())
        pole_height = pole_high - pole_low
        if pole_height <= 0:
            continue

        retrace_level = pole_high - (max_retrace_pct / 100.0) * pole_height
        flag_start_pos = pole_end_pos + 1
        if flag_start_pos > n - 1:
            continue
        max_flag_end_pos = min(n - 1, flag_start_pos + max_flag_bars - 1)

        breakout_pos = None
        invalid_pos = None
        for j in range(flag_start_pos, max_flag_end_pos + 1):
            if lows[j] < retrace_level:
                invalid_pos = j
                break
            bars_in_flag_so_far = j - flag_start_pos + 1
            if bars_in_flag_so_far >= min_flag_bars and closes[j] > pole_high:
                breakout_pos = j
                break

        if breakout_pos is not None:
            if require_volume_decline and volumes is not None and breakout_pos > flag_start_pos:
                pole_avg_vol = float(volumes[pole_start_pos:pole_end_pos + 1].mean())
                flag_avg_vol = float(volumes[flag_start_pos:breakout_pos].mean())
                if pole_avg_vol > 0 and flag_avg_vol >= pole_avg_vol:
                    continue  # no volume contraction in the flag -> not a genuine setup
            flag_low = float(lows[flag_start_pos:breakout_pos].min()) if breakout_pos > flag_start_pos else pole_low
            entry = pole_high
            stop_loss = min(flag_low, pole_low)
            target = entry + pole_height
            risk = entry - stop_loss
            if risk <= 0:
                continue
            rr = (target - entry) / risk
            recency = (n - 1) - breakout_pos
            status = "signal_active" if recency <= breakout_recency else "signal_past"
            return StrategySignal(
                detected=True, status=status, pattern_name=name,
                entry=entry, stop_loss=stop_loss, target=target, risk_reward=rr,
                pole_start_pos=pole_start_pos, pole_end_pos=pole_end_pos,
                flag_start_pos=flag_start_pos, flag_end_pos=breakout_pos - 1,
                breakout_pos=breakout_pos,
                notes=f"Ausbruch vor {recency} Kerze(n).",
            )

        # no breakout yet within window — is the flag still actively forming as of the latest bar?
        if invalid_pos is None and max_flag_end_pos == n - 1:
            flag_slice_low = float(lows[flag_start_pos:n].min())
            bars_so_far = n - flag_start_pos
            if bars_so_far >= min_flag_bars:
                if require_volume_decline and volumes is not None:
                    pole_avg_vol = float(volumes[pole_start_pos:pole_end_pos + 1].mean())
                    flag_avg_vol = float(volumes[flag_start_pos:n].mean())
                    if pole_avg_vol > 0 and flag_avg_vol >= pole_avg_vol:
                        continue
                entry = pole_high
                stop_loss = min(flag_slice_low, pole_low)
                target = entry + pole_height
                risk = entry - stop_loss
                if risk <= 0:
                    continue
                rr = (target - entry) / risk
                return StrategySignal(
                    detected=True, status="forming", pattern_name=name,
                    entry=entry, stop_loss=stop_loss, target=target, risk_reward=rr,
                    pole_start_pos=pole_start_pos, pole_end_pos=pole_end_pos,
                    flag_start_pos=flag_start_pos, flag_end_pos=n - 1,
                    breakout_pos=None,
                    notes="Flagge bildet sich noch — Ausbruch über Entry-Linie abwarten.",
                )
        # else: this pole candidate got invalidated — keep scanning earlier poles
        continue

    return NO_SIGNAL(name)


STRATEGY_REGISTRY = {
    "Bull Flag (Warrior Trading Style)": detect_bull_flag,
    # Zukünftige Strategien hier ergänzen, z.B.:
    # "Micro Pullback": detect_micro_pullback,
    # "VWAP Reclaim": detect_vwap_reclaim,
}

STATUS_LABELS = {
    "signal_active": ("🟢 Aktives Signal", "#26a69a"),
    "signal_past": ("🟡 Signal (nicht mehr frisch)", "#d4a017"),
    "forming": ("🔵 Setup bildet sich", "#42a5f5"),
    "none": ("⚪ Kein Signal", "#888"),
}


def compute_relative_volume(df: pd.DataFrame) -> Optional[float]:
    """
    Timezone-safe relative-volume proxy: compares today's cumulative volume
    (up to however many bars have printed so far) against the average
    cumulative volume at the same bar-count on prior days. >1.0 means more
    volume than usual has traded so far today — a core Warrior-Trading filter.
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    try:
        work = df.copy()
        work["_date"] = work.index.date
        dates = sorted(work["_date"].unique())
        if len(dates) < 2:
            return None
        today = dates[-1]
        today_df = work[work["_date"] == today]
        bars_so_far = len(today_df)
        if bars_so_far == 0:
            return None
        today_vol = float(today_df["Volume"].sum())

        prior_cum_vols = []
        for d in dates[:-1][-10:]:
            day_df = work[work["_date"] == d]
            if len(day_df) >= bars_so_far:
                prior_cum_vols.append(float(day_df["Volume"].iloc[:bars_so_far].sum()))
            elif len(day_df) > 0:
                prior_cum_vols.append(float(day_df["Volume"].sum()))
        if not prior_cum_vols:
            return None
        avg_prior = sum(prior_cum_vols) / len(prior_cum_vols)
        if avg_prior <= 0:
            return None
        return today_vol / avg_prior
    except Exception:
        return None


# ======================================================================
# CHARTING
# ======================================================================

def build_candlestick_chart(
    df: pd.DataFrame, ticker: str, display_name: str, signal: Optional[StrategySignal] = None
) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22],
        vertical_spacing=0.03,
    )

    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
            name=ticker,
            showlegend=False,
        ),
        row=1, col=1,
    )

    if "Volume" in df.columns:
        colors = ["#26a69a" if c >= o else "#ef5350" for o, c in zip(df["Open"], df["Close"])]
        fig.add_trace(
            go.Bar(x=df.index, y=df["Volume"], marker_color=colors, name="Volume", showlegend=False),
            row=2, col=1,
        )

    if signal is not None and signal.detected:
        # shade the flagpole and the flag/consolidation zone
        if signal.pole_start_pos is not None and signal.pole_end_pos is not None:
            fig.add_vrect(
                x0=df.index[signal.pole_start_pos], x1=df.index[signal.pole_end_pos],
                fillcolor="#42a5f5", opacity=0.12, line_width=0, row=1, col=1,
                annotation_text="Pole", annotation_position="top left", annotation_font_size=10,
            )
        if signal.flag_start_pos is not None and signal.flag_end_pos is not None and signal.flag_end_pos >= signal.flag_start_pos:
            fig.add_vrect(
                x0=df.index[signal.flag_start_pos], x1=df.index[signal.flag_end_pos],
                fillcolor="#ffa726", opacity=0.14, line_width=0, row=1, col=1,
                annotation_text="Flag", annotation_position="top left", annotation_font_size=10,
            )
        # entry / stop / target lines
        if signal.entry is not None:
            fig.add_hline(y=signal.entry, line_dash="dash", line_color="#26a69a", line_width=1.5,
                           annotation_text=f"Entry {signal.entry:,.2f}", annotation_font_color="#26a69a",
                           row=1, col=1)
        if signal.stop_loss is not None:
            fig.add_hline(y=signal.stop_loss, line_dash="dash", line_color="#ef5350", line_width=1.5,
                           annotation_text=f"Stop {signal.stop_loss:,.2f}", annotation_font_color="#ef5350",
                           row=1, col=1)
        if signal.target is not None:
            fig.add_hline(y=signal.target, line_dash="dash", line_color="#42a5f5", line_width=1.5,
                           annotation_text=f"Ziel {signal.target:,.2f}", annotation_font_color="#42a5f5",
                           row=1, col=1)

    fig.update_layout(
        title=f"{display_name} ({ticker})",
        template="plotly_dark",
        height=620,
        margin=dict(l=10, r=10, t=50, b=10),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        dragmode="pan",
    )
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_yaxes(title_text="Preis", row=1, col=1)
    fig.update_yaxes(title_text="Volumen", row=2, col=1)
    return fig


# ======================================================================
# SESSION STATE INIT
# ======================================================================

if "watchlist" not in st.session_state:
    st.session_state.watchlist = load_watchlist()
if "active_ticker" not in st.session_state:
    st.session_state.active_ticker = "AAPL"
if "alpaca_key" not in st.session_state:
    # Bei einem Cloud-Deployment (Streamlit Community Cloud) können die Keys
    # über die "Secrets"-Verwaltung gesetzt werden, statt sie bei jedem
    # Besuch manuell einzutippen. Lokal bleibt das Sidebar-Feld leer, falls
    # keine secrets.toml existiert — dann ganz normal manuell eintragen.
    try:
        st.session_state.alpaca_key = st.secrets.get("ALPACA_API_KEY", "")
    except Exception:
        st.session_state.alpaca_key = ""
if "alpaca_secret" not in st.session_state:
    try:
        st.session_state.alpaca_secret = st.secrets.get("ALPACA_SECRET_KEY", "")
    except Exception:
        st.session_state.alpaca_secret = ""


# ======================================================================
# SIDEBAR
# ======================================================================

with st.sidebar:
    st.title("📈 TR Stock Dashboard")
    st.caption("Live-Kursanalyse ohne Trade-Republic-API-Zugriff")

    page = st.radio("Ansicht", ["🔍 Analyse", "📊 Live-Cockpit", "📡 Scanner"], label_visibility="collapsed")

    st.divider()
    st.subheader("Strategie")
    strategy_name = st.selectbox("Trading-System", options=list(STRATEGY_REGISTRY.keys()))
    st.caption("Weitere Strategien (z.B. Micro Pullback, VWAP Reclaim) lassen sich hier später ergänzen.")
    with st.expander("⚙️ Bull-Flag-Parameter"):
        bf_pole_window = st.slider("Pole-Fenster (Kerzen)", 2, 8, 3)
        bf_min_pole_pct = st.slider("Min. Pole-Anstieg (%)", 1.0, 15.0, 3.0, 0.5)
        bf_max_flag_bars = st.slider("Max. Flaggenlänge (Kerzen)", 5, 30, 15)
        bf_max_retrace = st.slider("Max. Retracement der Flagge (%)", 20.0, 70.0, 50.0, 5.0)
        st.caption(
            "Diese Werte steuern den heuristischen Bull-Flag-Erkenner. Voreinstellungen "
            "orientieren sich an Ross Camerons typischer Beschreibung des Musters."
        )
    bull_flag_params = dict(
        pole_window=bf_pole_window, min_pole_pct=bf_min_pole_pct,
        max_flag_bars=bf_max_flag_bars, max_retrace_pct=bf_max_retrace,
    )

    st.divider()
    st.subheader("Aktien-Suche")

    mapping_choice = st.selectbox(
        "Bekannte Trade-Republic-Aktien (EU & US)",
        options=["— manuell eingeben —"] + sorted(EU_US_MAPPING.keys()),
    )
    if mapping_choice != "— manuell eingeben —":
        suggested_ticker = EU_US_MAPPING[mapping_choice]["yfinance"]
        note = EU_US_MAPPING[mapping_choice].get("note")
        st.caption(f"Yahoo/yfinance-Symbol: `{suggested_ticker}`" + (f" — {note}" if note else ""))
    else:
        suggested_ticker = st.session_state.active_ticker

    manual_ticker = st.text_input(
        "Ticker-Symbol", value=suggested_ticker,
        help="US-Ticker ohne Suffix (z.B. AAPL). Europäische Börsen mit Yahoo-Suffix, "
             "z.B. SAP.DE (Xetra), AIR.PA (Paris), ASML.AS (Amsterdam), NESN.SW (Zürich).",
    ).strip().upper()

    timeframe_label = st.selectbox("Zeitraum", options=list(TIMEFRAME_OPTIONS.keys()), index=3)

    if st.button("🔎 Analysieren", use_container_width=True, type="primary"):
        st.session_state.active_ticker = manual_ticker

    st.divider()
    st.subheader("Live-Daten-Quelle")
    with st.expander("⚙️ Alpaca Markets API (optional)"):
        st.caption(
            "Optional: kostenlose Alpaca-Keys (data.alpaca.markets, IEX-Feed) für "
            "direktere US-Echtzeitkurse. Ohne Keys wird automatisch auf yfinance-Polling "
            "zurückgegriffen — funktioniert für US- und EU-Ticker gleichermaßen. "
            "Die Keys werden nur im Browser-Sitzungsspeicher gehalten und nicht gespeichert."
        )
        st.session_state.alpaca_key = st.text_input("Alpaca API Key ID", value=st.session_state.alpaca_key, type="password")
        st.session_state.alpaca_secret = st.text_input("Alpaca Secret Key", value=st.session_state.alpaca_secret, type="password")

    refresh_seconds = st.slider(
        "🔄 Aktualisierungsintervall (Sek.)", min_value=2, max_value=30,
        value=DEFAULT_REFRESH_SECONDS, step=1,
    )
    if not AUTOREFRESH_AVAILABLE:
        st.warning(
            "Paket `streamlit-autorefresh` nicht installiert — automatische "
            "Aktualisierung ist deaktiviert. Siehe requirements.txt."
        )

    st.divider()
    with st.expander("🔌 Verbindungsdiagnose (Yahoo Finance)"):
        if st.button("Verbindung zu fc.yahoo.com testen", use_container_width=True):
            try:
                r = requests.get("https://fc.yahoo.com", timeout=5)
                st.session_state["conn_test_result"] = (
                    "success", f"✅ Erreichbar (HTTP {r.status_code}). yfinance sollte funktionieren.",
                )
            except requests.exceptions.RequestException as e:
                st.session_state["conn_test_result"] = ("error", str(e))
            st.session_state["conn_test_time"] = datetime.now().strftime("%H:%M:%S")

        # Persisted so the result survives the next auto-refresh rerun instead
        # of flashing and disappearing (that was the actual bug — the button's
        # own True/False state resets on every rerun, autorefresh included).
        result = st.session_state.get("conn_test_result")
        if result:
            kind, msg = result
            tested_at = st.session_state.get("conn_test_time", "")
            if kind == "success":
                st.success(f"{msg} (getestet um {tested_at})")
            else:
                st.error(f"❌ Nicht erreichbar: {msg}  (getestet um {tested_at})")
                st.markdown(
                    "**Häufigste Ursache:** `fc.yahoo.com` wird fälschlich von einem "
                    "Werbe-/DNS-Blocker (z. B. AdGuard, Pi-hole, NextDNS) blockiert — "
                    "diese Domain ist aber die interne Auth-Adresse von yfinance, keine Werbung.\n\n"
                    "**Fix für AdGuard:** Filter → Eigene Regeln → Zeile hinzufügen:\n"
                    "```\n@@||fc.yahoo.com^$important\n```\n"
                    "Danach `ipconfig /flushdns` und die App neu starten. Bei anderen "
                    "Blockern/Antivirus-Programmen: `fc.yahoo.com` auf die Whitelist setzen."
                )
        last_err = st.session_state.get("last_yf_error")
        if last_err:
            st.caption(f"Letzter yfinance-Fehler: `{last_err[:200]}`")
            if st.session_state.get("last_yf_error_is_connection"):
                st.warning(
                    "⚠️ Diese konkrete Fehlermeldung deutet auf eine Netzwerk-/DNS-Blockade "
                    "hin (z. B. Werbeblocker). Siehe Fix oben."
                )
            elif st.session_state.get("last_yf_error_ambiguous"):
                st.info(
                    "ℹ️ Yahoo hat ohne klaren Fehler einfach leer geantwortet. Das kann ein "
                    "kurzes Rate-Limit sein — **oder** doch eine Netzwerkblockade. Klicke oben "
                    "auf **'Verbindung zu fc.yahoo.com testen'** für eine eindeutige Antwort."
                )


# ======================================================================
# PAGE: ANALYSE
# ======================================================================

def render_analysis_page() -> None:
    ticker = st.session_state.active_ticker
    if not ticker:
        st.info("Bitte links ein Ticker-Symbol eingeben.")
        return

    if AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=refresh_seconds * 1000, key="analysis_autorefresh")

    display_name = fetch_company_name(ticker)
    quote = get_live_price(ticker)

    header_col, btn_col = st.columns([4, 1])
    with header_col:
        st.subheader(f"{display_name}  ·  `{ticker}`")
    with btn_col:
        st.write("")
        if st.button("➕ Aktie zur Watchlist hinzufügen", use_container_width=True):
            add_to_watchlist(ticker)

    if quote.error and quote.price is None:
        st.error(f"⚠️ Live-Preis nicht verfügbar: {quote.error}")
    else:
        m1, m2, m3, m4 = st.columns(4)
        price_str = f"{quote.price:,.2f}" if quote.price is not None else "—"
        delta_str = f"{quote.change_abs:+.2f} ({quote.change_pct:+.2f}%)" if quote.change_abs is not None else None
        m1.metric("Live-Kurs", price_str, delta_str)
        m2.metric("Vortagesschluss", f"{quote.previous_close:,.2f}" if quote.previous_close else "—")
        m3.metric("Datenquelle", quote.source)
        m4.metric("Letztes Update", datetime.now().strftime("%H:%M:%S"))

    st.divider()

    tf = TIMEFRAME_OPTIONS[timeframe_label]
    with st.spinner(f"Lade historische Daten für {ticker}…"):
        df = fetch_historical(ticker, tf["period"], tf["interval"])

    if df is None or df.empty:
        if st.session_state.get("last_yf_error_is_connection"):
            st.error(
                f"❌ Keine Verbindung zu Yahoo Finance für `{ticker}`. Das ist meist ein "
                f"Werbe-/DNS-Blocker, der `fc.yahoo.com` fälschlich blockiert — Details und "
                f"Fix findest du links im Expander **🔌 Verbindungsdiagnose**."
            )
        elif st.session_state.get("last_yf_error_ambiguous"):
            st.error(
                f"❌ Yahoo Finance hat für `{ticker}` leer geantwortet (Ursache unklar — "
                f"Rate-Limit oder Blockade). Klicke links im Expander **🔌 Verbindungsdiagnose** "
                f"auf den Test-Button für eine eindeutige Antwort, oder warte kurz und lade neu."
            )
        else:
            st.error(
                f"❌ Keine historischen Daten für `{ticker}` gefunden. Prüfe die Schreibweise "
                f"(z.B. `.DE` für Xetra-Werte) oder wähle einen anderen Zeitraum."
            )
        return

    is_intraday = tf["interval"] in ("1m", "2m", "5m", "15m")
    signal = None
    if is_intraday:
        detect_fn = STRATEGY_REGISTRY[strategy_name]
        signal = detect_fn(df, **bull_flag_params)
    else:
        st.info(
            "ℹ️ Pattern-Erkennung braucht feine Kerzen (Intraday). Wähle oben den "
            "Zeitraum **1 Tag** oder **5 Tage**, um Entry/Stop/Ziel angezeigt zu bekommen."
        )

    fig = build_candlestick_chart(df, ticker, display_name, signal=signal)
    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    if signal is not None:
        render_signal_panel(signal)

    with st.expander("ℹ️ Europäische Trade-Republic-Ticker ↔ Yahoo/yfinance-Symbole"):
        map_df = pd.DataFrame(
            [{"Aktie": k, "Name": v["name"], "yfinance-Symbol": v["yfinance"]} for k, v in EU_US_MAPPING.items()]
        )
        st.dataframe(map_df, use_container_width=True, hide_index=True)
        st.caption(
            "Trade Republic handelt die meisten Aktien über Xetra/gettex; Yahoo Finance (und "
            "damit yfinance) bildet dieselben Kurse über Börsen-Suffixe ab (`.DE`, `.PA`, `.AS`, "
            "`.SW`, `.CO` etc.), sodass keine direkte Trade-Republic-API nötig ist."
        )


def render_signal_panel(signal: StrategySignal) -> None:
    label, color = STATUS_LABELS[signal.status]
    st.markdown(f"### {signal.pattern_name}")
    st.markdown(f"<span style='color:{color}; font-weight:700;'>{label}</span>", unsafe_allow_html=True)

    if not signal.detected:
        st.caption("Aktuell kein gültiges Muster im gewählten Zeitraum gefunden.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entry (Ausbruch)", f"{signal.entry:,.2f}" if signal.entry else "—")
    c2.metric("Stop-Loss", f"{signal.stop_loss:,.2f}" if signal.stop_loss else "—")
    c3.metric("Kursziel", f"{signal.target:,.2f}" if signal.target else "—")
    rr_str = f"{signal.risk_reward:.2f} : 1" if signal.risk_reward else "—"
    c4.metric("Chance-Risiko", rr_str)

    if signal.risk_reward is not None and signal.risk_reward < 2.0:
        st.warning(
            f"⚠️ Chance-Risiko-Verhältnis liegt unter 2:1 ({rr_str}) — nach Ross Camerons "
            "Regel eigentlich kein idealer Trade."
        )
    if signal.notes:
        st.caption(signal.notes)
    st.caption(
        "⚠️ Heuristische Mustererkennung, keine Anlageberatung. Ross Cameron liest dieses "
        "Muster live mit Level-2-Orderbuch — dieser Algorithmus approximiert es rein aus "
        "Kerzen- und Volumendaten."
    )


# ======================================================================
# PAGE: LIVE DASHBOARD / COCKPIT
# ======================================================================

def render_dashboard_page() -> None:
    st.subheader("📊 Live-Cockpit — Watchlist")

    if AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=refresh_seconds * 1000, key="dashboard_autorefresh")

    watchlist = st.session_state.watchlist
    if not watchlist:
        st.info("Deine Watchlist ist leer. Füge auf der Analyse-Seite Aktien hinzu.")
        return

    st.caption(f"Letzte Aktualisierung: {datetime.now().strftime('%H:%M:%S')} · Intervall: {refresh_seconds}s")

    cols_per_row = 4
    rows = [watchlist[i:i + cols_per_row] for i in range(0, len(watchlist), cols_per_row)]

    for row in rows:
        cols = st.columns(cols_per_row)
        for col, ticker in zip(cols, row):
            with col:
                render_stock_card(ticker)


def render_stock_card(ticker: str) -> None:
    quote = get_live_price(ticker)

    if quote.error and quote.price is None:
        st.markdown(
            f"""
            <div style="border:1px solid #444; border-radius:10px; padding:14px; margin-bottom:10px; background:#1e1e1e;">
                <div style="font-weight:600; font-size:1.05rem;">{ticker}</div>
                <div style="color:#999; font-size:0.85rem; margin-top:6px;">⚠️ {quote.error}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("🗑️ Löschen", key=f"del_{ticker}", use_container_width=True):
            remove_from_watchlist(ticker)
            st.rerun()
        return

    is_up = quote.is_up
    accent = "#26a69a" if is_up else "#ef5350"
    arrow = "▲" if is_up else "▼"
    bg = "rgba(38,166,154,0.10)" if is_up else "rgba(239,83,80,0.10)"
    change_pct = quote.change_pct if quote.change_pct is not None else 0.0
    change_abs = quote.change_abs if quote.change_abs is not None else 0.0

    st.markdown(
        f"""
        <div style="border:1px solid {accent}; border-radius:10px; padding:14px; margin-bottom:10px; background:{bg};">
            <div style="font-weight:700; font-size:1.05rem;">{ticker}</div>
            <div style="font-size:1.6rem; font-weight:700; color:{accent}; margin-top:4px;">
                {quote.price:,.2f}
            </div>
            <div style="color:{accent}; font-size:0.95rem; font-weight:600;">
                {arrow} {change_abs:+.2f} ({change_pct:+.2f}%)
            </div>
            <div style="color:#888; font-size:0.72rem; margin-top:6px;">Quelle: {quote.source}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔍 Analyse", key=f"view_{ticker}", use_container_width=True):
            st.session_state.active_ticker = ticker
            st.rerun()
    with c2:
        if st.button("🗑️ Löschen", key=f"del_{ticker}", use_container_width=True):
            remove_from_watchlist(ticker)
            st.rerun()


# ======================================================================
# PAGE: SCANNER (Watchlist-basiert, Warrior-Trading-Kriterien)
# ======================================================================

SCANNER_PERIOD = "5d"
SCANNER_INTERVAL = "5m"


def render_scanner_page() -> None:
    st.subheader("📡 Scanner — Watchlist gegen Warrior-Trading-Kriterien")
    st.caption(
        "Durchsucht **nur deine gespeicherte Watchlist** (kein Live-Scan des Gesamtmarkts — "
        "das würde einen kostenpflichtigen Profi-Feed erfordern). Ross Camerons klassische "
        "Kandidaten sind extreme Micro-Caps mit winzigem Float, die bei Trade Republic oft gar "
        "nicht handelbar sind — die Kriterien unten sind daher als Orientierung zu verstehen, "
        "nicht als Garantie, dass eine Aktie hier tatsächlich '5x Relativvolumen' erreicht."
    )

    if AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=refresh_seconds * 1000, key="scanner_autorefresh")

    watchlist = st.session_state.watchlist
    if not watchlist:
        st.info("Deine Watchlist ist leer. Füge auf der Analyse-Seite Aktien hinzu.")
        return

    f1, f2, f3 = st.columns(3)
    min_gap = f1.slider("Min. Gap %", 0.0, 50.0, 10.0, 0.5)
    min_relvol = f2.slider("Min. Relativvolumen (x)", 1.0, 10.0, 5.0, 0.5)
    price_range = f3.slider("Preis-Spanne ($/€)", 0.0, 100.0, (2.0, 20.0), 0.5)

    detect_fn = STRATEGY_REGISTRY[strategy_name]
    rows = []
    with st.spinner("Scanne Watchlist…"):
        for ticker in watchlist:
            df = fetch_historical(ticker, SCANNER_PERIOD, SCANNER_INTERVAL)
            quote = get_live_price(ticker)
            if df is None or df.empty or quote.price is None:
                rows.append({
                    "Ticker": ticker, "Preis": None, "Gap %": None, "RelVol": None,
                    "Kriterien": "⚠️ keine Daten", "Pattern": "—", "_signal": None, "_ok": False,
                })
                continue
            rel_vol = compute_relative_volume(df)
            gap_pct = quote.change_pct
            signal = detect_fn(df, **bull_flag_params)
            price = quote.price

            meets_gap = gap_pct is not None and gap_pct >= min_gap
            meets_relvol = rel_vol is not None and rel_vol >= min_relvol
            meets_price = price_range[0] <= price <= price_range[1]
            ok = meets_gap and meets_relvol and meets_price

            label, _ = STATUS_LABELS[signal.status]
            rows.append({
                "Ticker": ticker,
                "Preis": price,
                "Gap %": gap_pct,
                "RelVol": rel_vol,
                "Kriterien": "✅ erfüllt" if ok else "—",
                "Pattern": label,
                "_signal": signal,
                "_ok": ok,
            })

    # sort: active signals first, then criteria-matches, then by rel. volume
    def sort_key(r):
        sig = r["_signal"]
        active = 1 if (sig and sig.status == "signal_active") else 0
        return (-active, -int(r["_ok"]), -(r["RelVol"] or 0))

    rows.sort(key=sort_key)

    for r in rows:
        with st.container(border=True):
            c1, c2, c3, c4, c5, c6 = st.columns([1.2, 1, 1, 1, 1.6, 1])
            c1.markdown(f"**{r['Ticker']}**")
            c2.markdown(f"{r['Preis']:,.2f}" if r["Preis"] is not None else "—")
            gap = r["Gap %"]
            gap_color = "#26a69a" if (gap or 0) >= 0 else "#ef5350"
            c3.markdown(f"<span style='color:{gap_color}'>{gap:+.1f}%</span>" if gap is not None else "—", unsafe_allow_html=True)
            c4.markdown(f"{r['RelVol']:.1f}x" if r["RelVol"] is not None else "—")
            c5.markdown(r["Pattern"])
            c6.markdown(r["Kriterien"])

            sig = r["_signal"]
            if sig is not None and sig.detected:
                st.caption(
                    f"Entry {sig.entry:,.2f} · Stop {sig.stop_loss:,.2f} · Ziel {sig.target:,.2f} · "
                    f"CRV {sig.risk_reward:.2f}:1" if sig.risk_reward else
                    f"Entry {sig.entry:,.2f} · Stop {sig.stop_loss:,.2f} · Ziel {sig.target:,.2f}"
                )
            if st.button("🔍 Im Analyse-Tab öffnen", key=f"scan_open_{r['Ticker']}"):
                st.session_state.active_ticker = r["Ticker"]
                st.rerun()


# ======================================================================
# ROUTER
# ======================================================================

if page == "🔍 Analyse":
    render_analysis_page()
elif page == "📊 Live-Cockpit":
    render_dashboard_page()
else:
    render_scanner_page()
