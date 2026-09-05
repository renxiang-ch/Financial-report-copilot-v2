"""
Company cluster definitions for ingestion pipelines.

CLUSTER_V1       — original 6-company Apple supplier prototype
CLUSTER_RESEARCH — 15-company SC-DisclosureQA research cluster

Research cluster tiers (ASC 280 customer concentration framework):
  Tier 0  Hub          : AAPL
  Tier 1  Positive     : CRUS, QRVO, SWKS, AVGO, QCOM  (explicit Apple >10% disclosed)
  Tier 1  Negative     : GLW, ADI, TXN, MCHP, ON, LRCX (no named customer >10%)
  Tier 2  EMS/connector: APH, JBL, SANM               (structural inference, no text GT)

All companies are US-listed 10-K filers (EDGAR/XBRL available).
NXPI and FLEX excluded — foreign private issuers filing 20-F.
"""

CLUSTER_V1: dict[str, str] = {
    "AAPL": "Apple Inc.",
    "SWKS": "Skyworks Solutions Inc.",
    "QRVO": "Qorvo Inc.",
    "CRUS": "Cirrus Logic Inc.",
    "GLW":  "Corning Inc.",
    "AVGO": "Broadcom Inc.",
}

CLUSTER_RESEARCH: dict[str, str] = {
    # Tier 0 — Hub
    "AAPL": "Apple Inc.",
    # Tier 1 positive — explicit Apple concentration (named, ASC 280)
    "CRUS": "Cirrus Logic Inc.",
    "QRVO": "Qorvo Inc.",
    "SWKS": "Skyworks Solutions Inc.",
    "AVGO": "Broadcom Inc.",
    "QCOM": "Qualcomm Inc.",
    # Tier 1 negative — diversified, no single customer >10% disclosed
    "GLW":  "Corning Inc.",
    "ADI":  "Analog Devices Inc.",
    "TXN":  "Texas Instruments Inc.",
    "MCHP": "Microchip Technology Inc.",
    "ON":   "onsemi",
    "LRCX": "Lam Research Corp.",
    # Tier 2 — EMS / connector, structural inference only
    "APH":  "Amphenol Corp.",
    "JBL":  "Jabil Inc.",
    "SANM": "Sanmina Corp.",
}

# Manual CIK overrides for tickers the EDGAR search can't resolve automatically.
# Format: { "TICKER": "0000000000" } (10-digit zero-padded CIK string)
CIK_OVERRIDES: dict[str, str] = {}

CLUSTER_ALL: dict[str, str] = {**CLUSTER_V1, **CLUSTER_RESEARCH}
