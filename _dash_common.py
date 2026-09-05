"""Shared config, auth, and API helpers for every Streamlit page (dashboard + chat).

Not in pages/ — Streamlit only auto-registers scripts inside pages/ as
navigable pages, so this stays a plain importable module.
"""

import os
from pathlib import Path

import httpx
import streamlit as st

# Load .env so os.environ picks up API_KEY / API_URL etc. on local dev.
# On Render/Streamlit Cloud, real env vars take precedence (dotenv skips them).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass


def _cfg(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val is not None:
        return val
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


API_URL      = _cfg("API_URL",      "http://localhost:8000")
API_KEY      = _cfg("API_KEY",      "")
APP_PASSWORD = _cfg("APP_PASSWORD", "")

# Fixed categorical hue order, assigned by entity (ticker) — never by rank —
# so a supplier keeps the same color on every chart regardless of sort order
# or which suppliers a filter happens to include. Hexes are the dataviz
# skill's validated default categorical palette (light mode).
SUPPLIER_COLORS = {
    "ADI":  "#2a78d6",  # blue
    "APH":  "#1baf7a",  # aqua
    "AVGO": "#eda100",  # yellow
    "CRUS": "#008300",  # green
    "JBL":  "#4a3aa7",  # violet
    "QCOM": "#e34948",  # red
    "QRVO": "#e87ba4",  # magenta
    "SWKS": "#eb6834",  # orange
}
DEFAULT_COLOR = "#898781"  # muted ink — fallback for any ticker outside the fixed set


def inject_css() -> None:
    """Cosmetic polish on top of .streamlit/config.toml's theme. Kept to a single
    rule — tabular figures on numeric text — so percentages and dollar amounts in
    expander titles don't jitter in width as digits change. Call once per run."""
    st.markdown(
        """
        <style>
        [data-testid="stMarkdownContainer"], [data-testid="stExpanderDetails"] {
            font-variant-numeric: tabular-nums;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def require_password() -> None:
    """Gate the page behind APP_PASSWORD if one is configured. Call before any other output."""
    if not APP_PASSWORD:
        return
    if not st.session_state.get("authenticated"):
        st.title("📊 Supply-Chain Risk Copilot")
        pwd = st.text_input("Access password", type="password")
        if st.button("Login", type="primary"):
            if pwd == APP_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password")
        st.stop()


@st.cache_resource
def _server_warm_state() -> dict:
    """Mutable dict shared across ALL sessions and ALL pages in this process."""
    return {"warm": False}


def warm_badge() -> None:
    """Informational API-status badge. Purely cosmetic — get_json() below waits out a cold start regardless."""
    if _server_warm_state()["warm"]:
        st.success("API ready", icon="🟢")
    else:
        st.warning("API may be cold — first request can take ~60s (Render free-tier wake-up)", icon="🟡")


@st.cache_data(ttl=300, show_spinner="Loading… (server may be waking up, up to ~60s on first request)")
def _fetch_json(path: str, params_items: tuple, timeout: float) -> dict:
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    resp = httpx.get(f"{API_URL}{path}", params=dict(params_items), headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_json(path: str, params: dict | None = None, timeout: float = 90.0) -> dict | None:
    """GET against the API, cached per (path, params) for 5 minutes so switching back to an
    already-seen selection (e.g. a fiscal year) is instant instead of re-fetching. A spinner
    only shows on an actual cache miss — including the (rare) cold-start wait. Returns None on failure."""
    params_items = tuple(sorted((params or {}).items()))
    try:
        data = _fetch_json(path, params_items, timeout)
        _server_warm_state()["warm"] = True
        return data
    except Exception as e:
        st.error(f"Could not reach API: {e}")
        return None
