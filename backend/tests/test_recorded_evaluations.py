"""The model results in the README, reproduced from the answers recorded in ``cassettes/``.

These replay real answers from ``gemini-3.1-flash-lite`` without a key or the network. A change to
a prompt, a schema or the generator changes the questions, so the recording no longer covers
them and the replay fails: record again, and update the README with what comes out.
"""

from pathlib import Path

from recon.adjudication.llm import LlmAdjudicator
from recon.evaluation.adjudication_eval import run_evaluation as adjudication_run
from recon.evaluation.explanation_eval import explain_inputs
from recon.evaluation.mapping_eval import all_cases, run_evaluation, sample_cases, summarize
from recon.explanation.llm import LlmExplainer
from recon.llm.client import ReplayJson
from recon.llm.tools import ReplaySteps
from recon.mapping.agent import AgentMapper

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes"
MODEL = "gemini-3.1-flash-lite"


def test_the_recorded_adjudications_give_the_readme_s_table() -> None:
    llm = LlmAdjudicator(ReplayJson(CASSETTES / f"adjudication-{MODEL}.jsonl"))

    results = adjudication_run(llm.adjudicate, seeds=range(5), size=200)

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


def test_the_recorded_mapping_agent_gives_the_readme_s_table() -> None:
    agent = AgentMapper(ReplaySteps(CASSETTES / f"mapping-agent-{MODEL}.jsonl"))
    cases = sample_cases(all_cases(range(1, 2)), 15)

    summary = summarize(run_evaluation(agent, seeds=range(1, 2), cases=cases))

    exact = {group: counts["files_exact"] for group, counts in summary.items()}
    wrong = sum(counts["files_wrong"] for counts in summary.values())
    assert exact == {"development": 23, "held_out": 18, "stress": 19}
    assert wrong == 3
    # The one case the provider was too busy to answer was never recorded.
    assert len(agent.errors) == 1
