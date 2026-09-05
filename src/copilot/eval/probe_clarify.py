"""Does it ask when it should, and stay quiet when it should not.

Deterministic, zero LLM, zero cost -- clarification is decided before any model
is called, so measuring it never needs one either.

The second half matters more than the first. Asking is cheap for the system and
expensive for the reader, so a clarifier that fires often is worse than one that
never existed, and "it asks good questions" is not a finding until "it does not
ask unnecessary ones" is also measured. The over-asking corpus is every question
in every eval set: ~80 real questions, each written to be answered, none of which
may produce a menu.

    uv run --active python -m copilot.eval.probe_clarify
"""

import glob
import json
from pathlib import Path

from copilot.agent.clarify import clarification_for
from copilot.agent.conversation import carried_slots
from copilot.agent.slots import extract_slots

# (question, prior turns, should it ask, why this case exists)
CASES: list[tuple[str, list[str], bool, str]] = [
    # ── should ask: more than one reading, every reading answerable ──────────
    ("What was Apple's profit in fiscal 2024?", [], True,
     "gross, operating and net are three stored figures and nothing chooses"),
    ("What was the margin of Cirrus Logic in 2024?", [], True,
     "same family, reached through the of-phrase rather than the possessive"),
    ("What was Apple's EPS in 2024?", [], True,
     "basic and diluted are both stored and differ"),
    ("What was Qorvo's income in FY2024?", [], True,
     "operating and net"),
    ("How dependent is it on Apple?",
     ["Compare Cirrus Logic and Qorvo in fiscal 2024."], True,
     "two companies still open in the thread, both readings are real questions"),

    # ── must not ask: one reading, or none ──────────────────────────────────
    ("What was Apple's gross profit in fiscal 2024?", [], False,
     "the family word is inside a phrase that already names a member"),
    ("What was Apple's net margin in fiscal 2024?", [], False, "resolved"),
    ("What was Apple's diluted EPS in 2024?", [], False, "resolved"),
    ("What does Skyworks say about profit margins?", [], False,
     "wants a passage, not a figure -- no company possesses the family word"),
    ("How dependent is it on Apple?",
     ["What was Cirrus Logic's revenue in fiscal 2024?"], False,
     "one candidate, so the pronoun resolves and nothing is asked"),
    ("How dependent is Cirrus Logic on Apple?", [], False, "no pronoun, no family word"),
    ("What was Apple's revenue in fiscal 2024?", [], False, "revenue is one figure"),
    ("Which companies supply Apple in FY2024?", [], False, "not a metric question"),
    ("What share of Apple's procurement spend goes to Qorvo?", [], False,
     "structurally undisclosed -- answered by saying so, never by a menu"),
    ("What was Apple's Americas segment revenue in 2024?", [], False,
     "NO reading is answerable; a menu here would imply segment data exists"),
    ("What was Skyworks' SKWS revenue in 2024?", [], False,
     "a typo is the system's to fix, not the reader's to confirm"),
]


def _ask(question: str, prior: list[str]) -> dict | None:
    carry = carried_slots([{"question": q, "answer": "(answered)"} for q in prior])
    return clarification_for(question, extract_slots(question, carry=carry), carry)


def _sweep_datasets() -> list[tuple[str, str]]:
    """Every eval question, which by construction must never be handed back."""
    unexpected: list[tuple[str, str]] = []
    for path in sorted(glob.glob("data/datasets/eval_set*.json")):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if "conversations" in data:
            for conv in data["conversations"]:
                prior: list[str] = []
                for turn in conv["turns"]:
                    if _ask(turn["question"], prior):
                        unexpected.append((Path(path).name, turn["question"]))
                    prior.append(turn["question"])
            continue
        for item in data["items"]:
            if item.get("retired"):
                continue
            if _ask(item["question"], []):
                unexpected.append((Path(path).name, item["question"]))
    return unexpected


def main() -> None:
    right = 0
    for question, prior, should, why in CASES:
        got = _ask(question, prior) is not None
        ok = got == should
        right += ok
        mark = "ok " if ok else "BAD"
        want = "ask" if should else "quiet"
        was = "ask" if got else "quiet"
        print(f"  {mark} want={want:<5} got={was:<5}  {question}")
        if not ok:
            print(f"        ({why})")

    print(f"\nclarification accuracy {right}/{len(CASES)} = "
          f"{right / len(CASES) * 100:.1f}%")

    over = _sweep_datasets()
    print("\nover-asking sweep: every eval question in data/datasets/")
    print(f"  unexpected clarifications: {len(over)}")
    for name, question in over:
        print(f"    {name}: {question}")

    Path("data/results").mkdir(parents=True, exist_ok=True)
    Path("data/results/probe_clarify.json").write_text(json.dumps({
        "cases": len(CASES), "correct": right,
        "accuracy": round(right / len(CASES) * 100, 1),
        "over_asked": [{"dataset": n, "question": q} for n, q in over],
    }, indent=2), encoding="utf-8")
    print("\nwrote data/results/probe_clarify.json")


if __name__ == "__main__":
    main()
