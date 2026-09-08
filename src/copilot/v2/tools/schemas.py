"""Pydantic arg schemas for the v2 tools.

One schema per tool. Field descriptions are what the model reads, so v1's
load-bearing hints (the exact metric-label list, the relation-direction note)
live here rather than concatenated onto the tool description. ``metric`` is
validated against the live DB label set -- a written-down list that drifts from
the data is this project's most repeated defect.

``retrieve_text`` has no ``query`` field on purpose: v1 overwrites it with the
raw user question every turn (a compressed rewrite measurably hurt recall), so it
comes from resolved state, not the model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from copilot.agent.agent import advertised_metrics
from copilot.v2.tools.base import ToolError, ToolErrorKind

_METRICS = sorted(advertised_metrics())
_METRIC_LIST = ", ".join(_METRICS)


class QueryFinancialsArgs(BaseModel):
    ticker: str = Field(description="Company ticker, e.g. AAPL, CRUS, AVGO.")
    metric: str = Field(
        description=f"Exact stored label. One of: {_METRIC_LIST}.",
        json_schema_extra={"enum": _METRICS},
    )
    fiscal_year: int | None = Field(
        default=None,
        description="Fiscal year, e.g. 2024. Omit for the most recent.",
    )

    @field_validator("metric")
    @classmethod
    def _known_metric(cls, v: str) -> str:
        if v in _METRICS:
            return v
        import difflib
        near = difflib.get_close_matches(v, _METRICS, n=3, cutoff=0.5)
        raise ToolError(
            ToolErrorKind.BAD_ARGUMENT,
            f"{v!r} is not a stored metric label. Use one of: {_METRIC_LIST}.",
            data={"asked_for": v, "did_you_mean": near},
        )


class ListMetricsArgs(BaseModel):
    ticker: str = Field(description="Company ticker to enumerate available metrics/years for.")


class RetrieveTextArgs(BaseModel):
    ticker: str | None = Field(
        default=None, description="Restrict the search to this company's filings."
    )
    k: int = Field(default=5, ge=1, le=20, description="Number of passages to return.")
    fiscal_year: int | None = Field(
        default=None,
        description="Restrict to this filing year. Omit to let the tool scope to "
        "the most recent filing (it reports what it scoped to).",
    )


class GraphQueryArgs(BaseModel):
    customer: str | None = Field(
        default=None,
        description="Ticker of the CUSTOMER side. 'Who supplies Apple?' -> customer=AAPL.",
    )
    supplier: str | None = Field(
        default=None,
        description="Ticker of the SUPPLIER side. 'Who does Qorvo sell to?' -> supplier=QRVO. "
        "Concentration edges are supplier-reported: a company that files 10-K "
        "customer-concentration disclosures is the supplier.",
    )
    fiscal_year: str = Field(
        default="latest",
        description="'latest', 'trend' (all years), a year like '2024', or a range '2022-2025'.",
    )
    depth: int = Field(default=1, ge=1, le=3, description="1 = direct edges; 2+ = multi-hop.")


class ComputeArgs(BaseModel):
    expression: str = Field(
        description="Arithmetic over the named variables, e.g. 'gross_profit / revenue * 100'. "
        "Numbers come from query_financials -- never inline literals."
    )
    variables: dict[str, float] = Field(
        description="Name -> value map for every name in the expression."
    )
