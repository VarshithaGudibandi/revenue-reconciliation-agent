"""The model results in the README, reproduced from the answers recorded in ``cassettes/``.

These replay real answers from ``gemini-3.1-flash-lite`` without a key or the network. A change to
a prompt, a schema or the generator changes the questions, so the recording no longer covers
them and the replay fails: record again, and update the README with what comes out.
"""

from pathlib import Path

from recon.adjudication.llm import LlmAdjudicator
from recon.evaluation.adjudication_eval import run_evaluation
from recon.evaluation.explanation_eval import explain_inputs
from recon.explanation.llm import LlmExplainer
from recon.llm.client import ReplayJson

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes"
MODEL = "gemini-3.1-flash-lite"


def test_the_recorded_adjudications_give_the_readme_s_table() -> None:
    llm = LlmAdjudicator(ReplayJson(CASSETTES / f"adjudication-{MODEL}.jsonl"))

    results = run_evaluation(llm.adjudicate, seeds=range(5), size=200)

    assert {kind: dict(counts) for kind, counts in results.items()} == {
        "explicit": {"correct": 13},
        "descriptive": {"correct": 14},
        "paraphrased": {"correct": 15, "suggested_right": 2},
        "useless": {"abstained": 16},
    }
    assert llm.errors == []


def test_every_recorded_explanation_passes_the_checks() -> None:
    explainer = LlmExplainer(ReplayJson(CASSETTES / f"explanation-{MODEL}.jsonl"))
    inputs = [inp for seed in range(2) for inp in explain_inputs(seed, 200, 3)]

    accepted = [explainer.explain(inp).text is not None for inp in inputs]

    assert len(inputs) == 46
    assert all(accepted)
    assert explainer.errors == []
