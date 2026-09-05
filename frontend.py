"""Entry point — routes to Chat (default) and the three dashboard views.

Uses st.navigation instead of the classic pages/ directory convention so the
sidebar label and default landing page are explicit here rather than derived
from filenames (which is what produced the un-styled "frontend" label when
this script itself was the classic-MPA default page).
"""

import streamlit as st

from _dash_common import inject_css, require_password

st.set_page_config(page_title="Supply-Chain Risk Copilot", page_icon="📊", layout="centered")
inject_css()
require_password()

pg = st.navigation([
    st.Page("views/chat.py", title="Chat", icon="💬", default=True),
    st.Page("views/exposure.py", title="Company Exposure View", icon="📊"),
    st.Page("views/supplier.py", title="Supplier Deep Dive", icon="🔍"),
    st.Page("views/whatif.py", title="What-if Scenario Tool", icon="🎯"),
])
pg.run()
