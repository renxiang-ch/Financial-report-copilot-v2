"""FastAPI application — exposes the agent as an HTTP endpoint."""

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from copilot.config import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_api_key(key: str | None = Security(_api_key_header)) -> None:
    if not settings.api_key:
        return  # no key configured → open access
    if key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")

app = FastAPI(title="Financial Report Copilot", version="0.1.0")


@app.on_event("startup")
def _preload_embedding_model() -> None:
    import os
    if os.environ.get("SKIP_PRELOAD", "").lower() in ("1", "true", "yes"):
        return
    from copilot.retrieval.hybrid import retrieve_hybrid
    retrieve_hybrid("warmup", k=1)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QuestionRequest(BaseModel):
    question: str
    model: str | None = None
    # Prior turns as [{"question", "answer"}, ...]. The client owns the thread and
    # hands it back each turn, so the API stays stateless and horizontally
    # scalable -- no session store to expire, evict, or lose on a restart, which
    # matters on a free tier that spins the instance down after 15 idle minutes.
    history: list[dict] = []


class AnswerResponse(BaseModel):
    answer: str
    steps: list[dict]
    citations: list[str]
    # Derived from the tool trace, never from the model. Declared here because
    # AnswerResponse(**result) drops anything the schema does not name, which is
    # how usage and route already fail to reach callers.
    provenance: dict = {}
    # The verdict on the answer's own figures: which of them could not be traced
    # back to something a tool returned. Separate from provenance on purpose --
    # see agent.ask.
    verification: dict = {}
    # Set when the question was handed back instead of answered. `answer` still
    # carries a readable form of it, so a client that ignores this field gets
    # something a person can act on rather than an empty string.
    needs_clarification: bool = False
    clarification: dict = {}
    # Already includes this turn, so the client can send it straight back --
    # except after a clarification, which adds no turn because nothing was
    # answered.
    history: list[dict] = []
    # What the history policy did (kept, evicted, whether the cache prefix moved).
    context: dict = {}
    usage: dict = {}
    # Which model answered, and why that one.
    model_route: dict = {}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask", response_model=AnswerResponse, dependencies=[Depends(_require_api_key)])
def ask(request: QuestionRequest):
    model = request.model or None

    if not settings.openai_api_key:
        return AnswerResponse(
            answer=(
                f"API key not configured. "
                f"Add OPENAI_API_KEY to .env to enable this model. "
                f"Your question was: '{request.question}'"
            ),
            steps=[{"tool": "mock", "input": {"question": request.question}}],
            citations=["No citations — running in mock mode"],
            history=request.history + [{"question": request.question, "answer": "mock mode"}],
            provenance={"numeric_source": "Mock mode — no data tool ran",
                        "relationship_source": None, "computation": "No computation",
                        "corroboration": "No filing cited", "accessions": [],
                        "tools_used": [], "refused": False,
                        "limitations": ["Mock mode: no API key configured, so nothing was queried."]},
        )

    from copilot.agent.agent import ask as agent_ask
    result = agent_ask(request.question, model=model, history=request.history)
    return AnswerResponse(**result)


# ── Dashboard endpoints — deterministic SQL, no LLM call, no cost ────────────

@app.get("/dashboard/years", dependencies=[Depends(_require_api_key)])
def dashboard_years(customer: str = "AAPL"):
    from copilot.dashboard import get_available_years
    return get_available_years(customer=customer)


@app.get("/dashboard/exposure", dependencies=[Depends(_require_api_key)])
def dashboard_exposure(customer: str = "AAPL", fiscal_year: str | None = None):
    from copilot.dashboard import get_exposure
    return get_exposure(customer=customer, fiscal_year=fiscal_year)


@app.get("/dashboard/supplier/{ticker}", dependencies=[Depends(_require_api_key)])
def dashboard_supplier(ticker: str, customer: str = "AAPL"):
    from copilot.dashboard import get_supplier_trend
    return get_supplier_trend(ticker=ticker, customer=customer)


@app.get("/dashboard/whatif", dependencies=[Depends(_require_api_key)])
def dashboard_whatif(customer: str = "AAPL", cut_pct: float = 20.0, fiscal_year: str | None = None):
    from copilot.dashboard import get_whatif
    return get_whatif(customer=customer, cut_pct=cut_pct, fiscal_year=fiscal_year)
