"""Read the constraints out of a question, deterministically.

Three separate defects turned out to have the same missing piece: nothing in
this system ever parsed a question into what it is actually asking about.

    Routing decided answerable-or-not by looking for words near other words,
    without ever asking which side of a supplier/customer relationship the
    question was on. It scores 38.9% on phrasings drawn from real failures --
    four answerable questions hard-refused, four undisclosed quantities let
    through -- while the harness written against its own patterns reports 100%.

    Follow-up turns lost the fiscal year. Turn one establishes FY2024, turn
    three asks "and Cirrus?", and nothing carries the year forward, so the model
    picks whichever concentration it likes and binds it to FY2024 revenue.

    Retrieval searched every year at once. Every company has ten filings and a
    quarter of the corpus is a near-duplicate of another chunk, so the top five
    fills up with the same boilerplate from different years. Scoping to the
    year asked about takes hit@5 from 4/8 to 6/8 -- but only if something knows
    which year was asked about.

One parser, three consumers. It is regex and a company index, not a model: the
whole point is that the same question always yields the same slots, so a routing
decision can be argued with rather than re-rolled.

The company index is read from the `companies` table rather than written down
here. A hardcoded inventory of company names would be the fifth instance of this
project's most persistent defect -- a list that drifts from the data it claims to
describe -- and the router's own `_KNOWN_COMPANIES` is already the fourth.
"""

import re
from functools import lru_cache

from copilot.storage.db import get_conn

# ── Company recognition ──────────────────────────────────────────────────────

# Informal names no derivation from the registered name will produce.
_EXTRA_ALIASES = {
    "ti": "TXN",
    "texas instruments": "TXN",
    "on semiconductor": "ON",
    "onsemi": "ON",
    "analog devices": "ADI",
    "lam": "LRCX",
    "microchip": "MCHP",
    "qualcomm": "QCOM",
    "broadcom": "AVGO",
}

_SUFFIXES = re.compile(
    r"\b(inc|corp|corporation|company|co|ltd|limited|plc|holdings|group)\b\.?",
    re.IGNORECASE,
)

# Tickers that are also ordinary English words. Matched only in upper case, and
# never from a lowercase surface form.
_WORDLIKE_TICKERS = {"ON"}


@lru_cache(maxsize=1)
def _company_index() -> tuple[dict[str, str], frozenset[str]]:
    """(surface form -> ticker, all tickers), built from the database.

    Surface forms are derived by peeling corporate suffixes off the registered
    name: "Skyworks Solutions Inc." yields "skyworks solutions" and "skyworks",
    which is how people actually write it.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, name FROM companies")
            rows = cur.fetchall()
    finally:
        conn.close()

    index: dict[str, str] = {}
    tickers = set()
    for row in rows:
        ticker = row["ticker"].upper()
        tickers.add(ticker)
        name = _SUFFIXES.sub("", row["name"] or "").strip(" .,")
        if not name:
            continue
        index[name.lower()] = ticker
        head = name.split()[0]
        # A one-word head like "Apple" or "Corning" is how the company is named
        # in prose. Two-letter heads are too collision-prone to trust.
        if len(head) > 3:
            index.setdefault(head.lower(), ticker)
    index.update(_EXTRA_ALIASES)
    return index, frozenset(tickers)


def find_companies(text: str) -> list[str]:
    """Tickers named in `text`, in the order they appear, without duplicates."""
    index, tickers = _company_index()
    hits: list[tuple[int, str]] = []

    for surface, ticker in index.items():
        for m in re.finditer(rf"\b{re.escape(surface)}\b", text, re.IGNORECASE):
            hits.append((m.start(), ticker))

    for ticker in tickers:
        if ticker in _WORDLIKE_TICKERS:
            continue
        for m in re.finditer(rf"\b{ticker}\b", text):
            hits.append((m.start(), ticker))

    out: list[str] = []
    for _, ticker in sorted(hits):
        if ticker not in out:
            out.append(ticker)
    return out


# ── Fiscal year ──────────────────────────────────────────────────────────────

_YEAR_RE = re.compile(r"\b(?:fy\s?|fiscal\s+(?:year\s+)?)?((?:19|20)\d{2})\b",
                      re.IGNORECASE)
# A question about how something moved cannot be answered inside one year, so a
# single year found in it must not be used to scope retrieval to that year.
# "history" was in this list and should not have been. It is the only trigger
# on "How does Broadcom describe its history and the role of the VMware
# acquisition?", which is a narrative question about the current filing, not a
# numeric series -- and flagging it as a trend costs that question its
# top-ranked passage once retrieval scopes by year (measured across the
# retrieval set: rank 1 -> outside the top five, MRR 0.548 -> 0.405). Every
# real trend question in the frozen sets is caught by grow / change /
# over time / from-to.
_TREND_RE = re.compile(
    r"\b(trend|trends|over (?:time|the years|years|all)|each year|every year|year[- ]over[- ]year|yoy|"
    r"across years|by year|since|from \d{4}|through \d{4}|"
    r"grow(?:th|n)?|change[ds]?|evolv)", re.IGNORECASE)


def find_years(text: str) -> list[int]:
    return sorted({int(y) for y in _YEAR_RE.findall(text or "")
                   if 1990 <= int(y) <= 2100})


def find_fiscal_year(text: str) -> int | None:
    """The one year this question is about, or None if it is not about one year.

    None is a real answer, not a failure to parse. Scoping a trend question to a
    single year would hide the very comparison it asks for, so a range, a
    multi-year phrasing, or no year at all all return None.
    """
    years = find_years(text)
    if len(years) != 1 or _TREND_RE.search(text or ""):
        return None
    return years[0]


# ── Metric ───────────────────────────────────────────────────────────────────

# Ordered: the first match wins, so multi-word forms precede their heads.
_METRIC_TERMS: list[tuple[str, str]] = [
    (r"cost of (goods sold|revenue|sales)|cogs", "COGS"),
    (r"gross profit", "GrossProfit"),
    (r"gross margin", "GrossProfit"),
    (r"operating income|operating profit|operating margin", "OperatingIncome"),
    (r"net income|net profit|net margin|bottom line|earnings", "NetIncome"),
    (r"diluted eps|eps.{0,10}diluted", "EPS_Diluted"),
    (r"basic eps|eps.{0,10}basic", "EPS_Basic"),
    (r"total assets", "TotalAssets"),
    (r"long[- ]term debt", "LongTermDebt"),
    (r"r&d|research and development", "R&D"),
    (r"revenue|net sales|total sales|top line|turnover|sales", "Revenue"),
]


def find_metric(text: str) -> str | None:
    low = (text or "").lower()
    for pattern, label in _METRIC_TERMS:
        if re.search(pattern, low):
            return label
    return None


# ── Relationship direction ───────────────────────────────────────────────────
#
# This is the slot the router needs and never had. A 10-K concentration
# disclosure is supplier-reported: it says what share of the SUPPLIER's revenue
# a named customer accounted for. So a question about a share of a supplier's
# revenue is answerable, and the mirror image -- a share of a customer's spend --
# is not, no matter how it is phrased. Which of the two a question is asking
# depends on whose denominator it names, and that is decidable from the text.

_REVENUE_NOUN = (r"(?:net |total |annual )?(?:revenue|revenues|sales|turnover|"
                 r"top line|net sales)")
# "sourcing", "procurement" and "purchasing" are heads here as well as prefixes.
# They were prefixes only, so "Apple's component sourcing" found no spend noun
# and the question routed to auto -- caught by harness_router, which the newer
# 18-case probe missed, and which had passed that afternoon only because the
# model happened to refuse on its own.
_SPEND_NOUN = (r"(?:procurement|purchasing|supplier|supply[ -]chain|component|sourcing|vendor)?"
               r"[ -]*(?:spend|spending|budget|purchases|outlay|costs?|sourcing|procurement|purchasing|"
               r"cost of goods sold|cogs|bill of materials)")

# "how much of", "what share of", "46% of" -- the framing that makes a question
# about a proportion rather than about a quantity.
_SHARE_RE = re.compile(
    r"\b(percent|percentage|share|fraction|proportion|portion|how much of|"
    r"what part of|%)", re.IGNORECASE)

_CUSTOMER_WORD_RE = re.compile(r"\b(customers?|clients?|end[- ]customers?)\b",
                                re.IGNORECASE)

_DEPENDENCE_RE = re.compile(
    r"\b(depend(?:ent|ence|ency|s)?|reli(?:ant|ance|es)|exposure|exposed|"
    r"concentration)\b", re.IGNORECASE)

_BUY_VERB_RE = re.compile(
    r"\b(buys?|bought|purchas(?:e|es|ed|ing)|pays?|paid|procure[sd]?|"
    r"source[sd]? from|spends? on)\b", re.IGNORECASE)
# "cost of goods sold" is a line item, not an act of selling, and letting it
# match here read a procurement question as a supply question.
_SELL_VERB_RE = re.compile(
    r"\b(sells?|(?<!goods )sold|supplies|supplied|supplying|ships?|shipped|"
    r"provides?)\b",
    re.IGNORECASE)


def _possessed(text: str, noun_pattern: str) -> tuple[str | None, str]:
    """(the ticker whose `noun` this is, whatever qualified it).

    "Apple's spend" -> ("AAPL", ""). "Apple's Americas segment spend" ->
    ("AAPL", "Americas segment").

    The second value is the part of the question this parse could not
    represent, and it is returned rather than discarded on purpose. The
    possessive pattern skips up to two words between the company and the noun
    so that ordinary padding does not defeat it, and those skipped words are
    exactly where a scope qualifier hides. Silently stepping over them is how a
    question about one business segment becomes a question about the whole
    company, with nothing anywhere recording that the change was made.

    Both written forms are accepted: the possessive ("Skyworks' revenue") and
    the of-phrase ("revenue of Skyworks").
    """
    index, tickers = _company_index()
    names = sorted(list(index) + [t for t in tickers if t not in _WORDLIKE_TICKERS],
                   key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in names)

    for pattern in (rf"\b({alt})(?:'s|'|s')\s+((?:\w+\s+){{0,2}}?){noun_pattern}\b",
                    rf"{noun_pattern}\s+(?:of|for|at)\s+(?:the\s+)?({alt})\b"):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            key = m.group(1)
            skipped = (m.group(2) or "").strip() if m.re.groups > 1 else ""
            return (index.get(key.lower()) or key.upper()), skipped
    return None, ""


# Words that can sit between a company and its revenue/spend noun without
# narrowing what is being asked for. Everything else that lands there is a
# qualifier this parser cannot hold.
#
# This list is small and closed BY CONSTRUCTION -- it enumerates padding, not
# qualifiers. Enumerating qualifiers instead ("segment", "geography", "raw
# materials") is the reverse-engineered patching this project has rejected four
# times: it works for whatever the list happens to name and fails silently for
# everything it forgot, which is the same defect one level up.
_HARMLESS_MODIFIERS = {
    "total", "net", "annual", "yearly", "overall", "consolidated", "full",
    "reported", "gross", "own", "company", "companys", "entire", "whole",
    "fiscal", "the", "its", "a", "an",
}


def relation(text: str) -> dict:
    """Which side of a supplier/customer relationship this question asks about.

    Returns {"side": "supplier"|"customer"|None, "supplier": t|None,
             "customer": t|None, "basis": str}. `basis` names the rule that
    fired, so a routing decision can be explained to the person it refused.
    """
    text = text or ""
    companies = find_companies(text)
    none = {"side": None, "supplier": None, "customer": None, "basis": "no rule matched"}

    def result(side, supplier, customer, basis):
        return {"side": side, "supplier": supplier, "customer": customer, "basis": basis}

    # A. Role nouns say the direction outright. "a customer of Corning" makes
    #    Corning the supplier whatever else the sentence does.
    index, tickers = _company_index()
    names = sorted(list(index) + [t for t in tickers if t not in _WORDLIKE_TICKERS],
                   key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in names)

    def resolve(raw):
        return index.get(raw.lower()) or raw.upper()

    m = re.search(rf"customers?\s+(?:of|for)\s+(?:the\s+)?({alt})\b", text, re.IGNORECASE)
    if m:
        sup = resolve(m.group(1))
        other = next((c for c in companies if c != sup), None)
        return result("supplier", sup, other, "'customer of X' makes X the supplier")

    m = re.search(rf"\b({alt})(?:'s|'|s')\s+customers?\b", text, re.IGNORECASE)
    if m:
        sup = resolve(m.group(1))
        return result("supplier", sup, next((c for c in companies if c != sup), None),
                      "'X's customers' makes X the supplier")

    m = re.search(rf"\b({alt})(?:'s|'|s')\s+suppliers?\b|suppliers?\s+(?:of|to)\s+"
                  rf"(?:the\s+)?({alt})\b", text, re.IGNORECASE)
    if m:
        cust = resolve(m.group(1) or m.group(2))
        # Asking about the suppliers of X is a question about the suppliers, and
        # every fact it needs is disclosed on the supplier side.
        return result("supplier", None, cust,
                      "asks about the suppliers, whose own disclosures answer it")

    # B. Whose denominator is it. Requires a share framing: "Apple's revenue"
    #    in a plain lookup is not a relationship question.
    if _SHARE_RE.search(text):
        spender, _ = _possessed(text, _SPEND_NOUN)
        # A spend denominator only makes this a procurement question when there
        # is a second party for the spend to go to. "Where does Corning's raw
        # material purchasing come from" names one company and asks for a
        # disclosure, not for a share of anyone's budget.
        if spender and len(companies) >= 2:
            return result("customer", next((c for c in companies if c != spender), None),
                          spender, "asks for a share of the named company's spend")

        earner, _ = _possessed(text, _REVENUE_NOUN)
        # A share of a company's revenue is only a SUPPLY-CHAIN question when
        # there is a counterparty for the revenue to come from. Without one the
        # same words describe things this table cannot answer and must not be
        # routed as though it could: "what percentage of Apple's revenue came
        # from China", "Apple's revenue growth from 2023 to 2024". Both named a
        # company, a revenue noun, a share word and the word "from", and both
        # were classified as dependency questions until this line existed.
        if earner and (len(companies) >= 2 or _CUSTOMER_WORD_RE.search(text)):
            return result("supplier", earner,
                          next((c for c in companies if c != earner), None),
                          "asks for a share of the named company's revenue")

    # C. Directional verbs, when no denominator was named, but only with both parties named -- "Apple buys" on
    #    its own is not a question about a share of anything.
    if len(companies) >= 2:
        if _BUY_VERB_RE.search(text):
            return result("customer", companies[1], companies[0],
                          "a buying verb makes the first company the customer")
        if _SELL_VERB_RE.search(text):
            return result("supplier", companies[0], companies[1],
                          "a selling verb makes the first company the supplier")

    # D. Dependence framing with both parties named. "How dependent is Cirrus
    #    Logic on Apple" names no denominator but means one.
    if _DEPENDENCE_RE.search(text) and len(companies) >= 2:
        return result("supplier", companies[0], companies[1],
                      "dependence runs from the first company to the second")

    return none


def unaccounted(text: str) -> list[str]:
    """Parts of the question this parser recognised as present and could not hold.

    Why a parser needs a way to say "no"
        A fixed set of slots is not the danger. The danger is a fixed set of
        slots with no way to report what fell outside them, because then a
        reduced reading of the question is indistinguishable from a complete
        one, and whatever acts on it acts confidently on less than was asked.

        This project has measured that exact failure at the tool boundary:
        `query_financials(ticker, metric, fiscal_year)` has no argument for a
        segment or geography, so the qualifier never reached the tool -- 0.0% of
        the time, not rarely -- and the reduced query came back with a confident
        answer the model never doubted. Giving the tool an argument that could
        REJECT such a request fixed most of it. Giving it an argument that
        merely made the model state "consolidated", with no rejection path,
        nearly doubled fabrications: the model turned a silent reduction into a
        justified one. The mechanism that works is the rejection channel, not
        the extra field.

        So this returns residue, and every caller that takes an irreversible
        action on a parse -- refusing a question, forcing a tool, narrowing a
        search to one year -- must treat a non-empty result as a reason to fall
        back to the permissive path rather than to act. Being unsure is allowed;
        being unsure and confident is not.

    What it can and cannot see
        Only qualifiers sitting in a position the parser looks at: between a
        company and the revenue or spend noun it was matched to. That is where
        scope qualifiers actually appear in these questions, and it is found
        structurally -- whatever those words are -- rather than by matching them
        against a list of known qualifiers. A list would work for the entries
        someone remembered and fail silently for the rest, which is the defect
        this exists to avoid, not a smaller version of it.

        It cannot see a qualifier elsewhere in the sentence, and it does not
        try. An empty result means "nothing was noticed", never "the question
        was fully understood".
    """
    out: list[str] = []
    for noun in (_REVENUE_NOUN, _SPEND_NOUN):
        _, skipped = _possessed(text or "", noun)
        for word in skipped.split():
            bare = word.strip(",.;:'\u2019").lower()
            if not bare or bare in _HARMLESS_MODIFIERS or bare in out:
                continue
            # A year sitting here is not lost -- it is held in `fiscal_year`.
            # Residue means "noticed and not represented", so anything another
            # slot already carries must be excluded or the channel cries wolf
            # on questions it handled correctly.
            if _YEAR_RE.fullmatch(bare):
                continue
            out.append(bare)
    return out


# ── Anaphora ─────────────────────────────────────────────────────────────────
#
# "How dependent is it on Apple?" needs two parties for any direction rule to
# fire, and names one. The other is in the previous turn.
#
# Merging the carried companies into the list does not work, and the way it
# fails is instructive: every rule in `relation` is POSITIONAL -- "dependence
# runs from the first company to the second" -- and appending CRUS after AAPL
# produces the reading that Apple depends on Cirrus Logic. The pronoun's
# position in the sentence is the information that gets thrown away.
#
# So the carried company is substituted INTO the pronoun's position and the
# sentence is parsed again, which leaves every rule working on a normal
# sentence. Two guards keep it honest:
#
#   * the re-parse must actually produce a relation, or the substitution is
#     discarded whole. "How much of it comes from Apple?" has an "it" that means
#     revenue, and swapping a company in there yields no relation either way --
#     so nothing is kept, and the wrong reading never leaves this function.
#   * more than one candidate means the pronoun is ambiguous, and an ambiguous
#     pronoun is resolved by not resolving it.
#
# `route_question` additionally refuses to REFUSE on a company that came from
# here. A wrong forced tool costs a recoverable call; a wrong refusal returns
# nothing and cannot be argued with.

_ANAPHOR_RE = re.compile(
    r"\b(it|they|them|its|their|the company|that company|the firm)\b",
    re.IGNORECASE)
_POSSESSIVE_ANAPHORS = {"its", "their"}


def anaphor_candidates(text: str, carry_companies: list[str],
                       named: list[str]) -> list[str]:
    """Companies the thread left open that this question does not name.

    Split out from resolve_anaphor because more than one of these is not
    nothing: it is the precise reason the pronoun cannot be resolved, and
    clarify.py turns it into the options put back to the reader. Computing it
    and discarding it was the previous behaviour.
    """
    return [c for c in (carry_companies or []) if c not in (named or [])]


def resolve_anaphor(text: str, carry_companies: list[str],
                    named: list[str]) -> tuple[str, str | None]:
    """(question with the pronoun replaced, the ticker used) or (text, None).

    Only the first pronoun is replaced. A follow-up with two of them is not one
    this parser can claim to have understood.
    """
    candidates = anaphor_candidates(text, carry_companies, named)
    if len(candidates) != 1:
        return text, None
    ticker = candidates[0]

    match = _ANAPHOR_RE.search(text or "")
    if not match:
        return text, None
    word = match.group(1).lower()
    replacement = f"{ticker}'s" if word in _POSSESSIVE_ANAPHORS else ticker
    return text[:match.start()] + replacement + text[match.end():], ticker


# ── The parse ────────────────────────────────────────────────────────────────

def extract_slots(question: str, carry: dict | None = None) -> dict:
    """Everything decidable about a question without asking a model.

    `carry` is the previous turn's slots. Only constraints a follow-up leaves
    unstated are inherited, and each inherited value is named in `inherited` so
    the answer can say which year it assumed instead of quietly picking one.
    """
    question = question or ""
    slots = {
        "companies":   find_companies(question),
        "fiscal_year": find_fiscal_year(question),
        "years":       find_years(question),
        "metric":      find_metric(question),
        "is_trend":    bool(_TREND_RE.search(question)),
        "inherited":   [],
        # Set only when a pronoun was replaced by a carried company, and holding
        # the sentence that was actually parsed. A rewrite nobody can see is a
        # rewrite nobody can check -- this one reaches the trace and the answer.
        "resolved_question": None,
        # What this parse noticed and could not represent. A caller about to
        # refuse a question, force a tool, or narrow a search to one year must
        # back off to the permissive path when this is non-empty -- see
        # `unaccounted`.
        "unaccounted": unaccounted(question),
    }
    slots.update({f"relation_{k}": v for k, v in relation(question).items()})

    if carry:
        # A year is inherited only by a question that mentions no year at all and
        # is not asking about change. Both guards matter and neither is implied
        # by `fiscal_year is None`, which is also None for questions that named
        # years this parser declined to collapse into one:
        #
        #   "How has it changed since 2020?"  -> years [2020], trend  -> None
        #   "Compare 2023 and 2024"           -> years [2023, 2024]   -> None
        #
        # Inheriting the previous turn's single year into either would overwrite
        # a constraint the user did state, which is worse than carrying nothing:
        # the first would be answered for one year and the second for the wrong
        # one, both with an assumption line claiming it was inherited.
        if (slots["fiscal_year"] is None and carry.get("fiscal_year") is not None
                and not slots["years"] and not slots["is_trend"]):
            slots["fiscal_year"] = carry["fiscal_year"]
            slots["inherited"].append("fiscal_year")
        if slots["metric"] is None and carry.get("metric") is not None:
            slots["metric"] = carry["metric"]
            slots["inherited"].append("metric")
        # All-or-nothing when no company is named. A follow-up naming one company
        # has changed the subject ("and Cirrus?"), not added to it, so the
        # previous set is dropped rather than merged -- merging would answer
        # about two companies when one was asked about.
        if not slots["companies"] and carry.get("companies"):
            slots["companies"] = list(carry["companies"])
            slots["inherited"].append("companies")
        # When a company IS named and the sentence still points at another one
        # with a pronoun, the missing party is filled at the pronoun's position.
        # Only when the question does not already parse into a relation: a
        # sentence that stands on its own is never rewritten.
        elif slots["relation_side"] is None and carry.get("companies"):
            resolved, ticker = resolve_anaphor(question, carry["companies"],
                                               slots["companies"])
            if ticker:
                again = relation(resolved)
                # Kept only if the substitution bought something. Otherwise the
                # pronoun was not standing for a company and the rewrite is
                # thrown away rather than left to be read as a parse.
                if again["side"] is not None:
                    slots.update({f"relation_{k}": v for k, v in again.items()})
                    slots["companies"] = find_companies(resolved)
                    slots["resolved_question"] = resolved
                    slots["inherited"].append("companies")
    return slots
