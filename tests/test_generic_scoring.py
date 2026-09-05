"""Regression tests for the generic-agent baseline scorer.

Every case here is a real Codex answer from a baseline run that the first
scorer mis-scored (docs/devlog/002). The fix: prefer the number after "=" in
the shown calculation, and take the sign from prose ("loss", "decreased").
"""

import copilot.eval.generic_scoring as gs
from copilot.eval.generic_scoring import _extract_answer_number, score_numeric, score_retrieval

_YOY = {"expected_unit": "%", "tolerance_pct": 0.5}


def _num(text: str, expected: float, **item) -> bool:
    return score_numeric({"expected_value": expected, **item}, text)["correct"]


# ── _extract_answer_number ───────────────────────────────────────────────────

def test_prefers_number_after_equals_over_prose_headline():
    # headline rounded to 2.0, calc shows 2.02 -- take the calc value
    assert _extract_answer_number(
        "grew by approximately 2.0%.\nGrowth: (391 - 383) / 383 * 100 = 2.02%"
    ) == 2.02


def test_calc_line_keeps_its_explicit_sign():
    assert _extract_answer_number(
        "revenue decreased by 2.8%.\n(383.285 - 394.328) / 394.328 * 100 = -2.8%"
    ) == -2.8


def test_prose_only_number_is_a_magnitude():
    # sign-agnostic; score_numeric tries both signs against expected_value
    assert _extract_answer_number("Qorvo reported a net loss of $70.322 million.") == 70.322


def test_form_type_digits_are_not_the_answer():
    # "10-K" must not be read as the number 10
    assert _extract_answer_number(
        "Using Cirrus Logic's FY2024 10-K:\nImpact: $1.556B * 20% = $311.3 million"
    ) == 311.3


def test_plain_positive_number_still_works():
    assert _extract_answer_number("Apple's total revenue was $391.035 billion.") == 391.035


def test_markdown_bold_calc_result_and_link_path_ignored():
    # calc result wrapped in **bold**, and a markdown link whose URL contains
    # "Financial Report Copilot v2" (the "v2" was being read as 2.0)
    answer = ("Using [FY2024 10-K](</Users/x/Financial Report Copilot v2/CRUS/2024_10-K_x.htm>):\n"
              "- rev: **$1.78889B**\n- $1.78889B * 87% = **$1.556 billion**\n"
              "- $1.556B * 20% = **$311.3 million**")
    assert _extract_answer_number(answer) == 311.3


def test_currency_and_thousands_separator_after_equals():
    assert _extract_answer_number("... = $1,556 million") == 1556.0


# ── score_numeric end-to-end (the items devlog 002 flagged) ──────────────────

def test_qrvo_net_loss_scores_correct():
    assert _num(
        "Qorvo reported a net loss of $70.322 million in fiscal year 2024.",
        -70322000.0, expected_unit="USD", tolerance_pct=0.5,
    )


def test_aapl_yoy_2022_2023_decline_scores_correct():
    assert _num(
        "Apple's revenue decreased by 2.8%.\n(383.285 - 394.328) / 394.328 * 100 = -2.8%",
        -2.8005, **_YOY,
    )


def test_swks_yoy_decline_scores_correct():
    assert _num(
        "Skyworks' revenue decreased by 12.5%.\n(4178.0 - 4772.4) / 4772.4 = -12.45%",
        -12.4549, **_YOY,
    )


def test_crus_dollar_impact_scores_correct():
    answer = ("Using Cirrus Logic's FY2024 10-K:\n"
              "$1.78889B * 87% = $1.556B\n$1.556B * 20% = $311.3 million")
    assert _num(answer, 311266860.0, expected_unit="USD", tolerance_pct=1.0)


def test_dollar_impact_magnitude_scores_correct_despite_decrease_wording():
    # expected value is a positive magnitude; "decrease of ~$347M" must not
    # be negated into a miss (regression from an earlier prose-sign heuristic)
    assert _num(
        "Estimated revenue impact: a decrease of approximately $347 million.",
        346994552.0, expected_unit="USD", tolerance_pct=1.0,
    )


def test_wrong_answer_still_fails():
    assert not _num("Apple's revenue grew by 15%.\n... = 15%", 2.022, **_YOY)


# ── score_retrieval: judge is the authority, verbatim phrase is not a gate ────

def test_retrieval_follows_judge_not_verbatim_phrase(monkeypatch):
    monkeypatch.setattr(gs, "_llm_judge", lambda *a: {"score": 3, "reason": "all facts present"})
    item = {"question": "q", "golden_answer": "ref",
            "golden_citations": [{"key_phrase": "an exact phrase the answer paraphrases"}]}
    r = score_retrieval(item, "a correct but paraphrased answer")
    assert r["correct"] is True
    assert r["passage_hit"] is False  # reported, not gating


def test_retrieval_needs_review_when_judge_unavailable(monkeypatch):
    def _boom(*a):
        raise RuntimeError("bad key")
    monkeypatch.setattr(gs, "_llm_judge", _boom)
    r = score_retrieval({"question": "q", "golden_answer": "r"}, "ans")
    assert r["correct"] is None and r["needs_review"] is True
