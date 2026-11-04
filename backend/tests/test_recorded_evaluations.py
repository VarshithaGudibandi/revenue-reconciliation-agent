"""The model results in the README, reproduced from the answers recorded in ``cassettes/``.

These replay real answers from ``gemini-3.1-flash-lite`` without a key or the network. A change to
a prompt, a schema or the generator changes the questions, so the recording no longer covers
them and the replay fails: record again, and update the README with what comes out.
"""

from pathlib import Path

from recon.ai.adjudication.llm import LlmAdjudicator
from recon.ai.explanation.llm import LlmExplainer
from recon.ai.llm.client import ReplayJson
from recon.ai.llm.tools import ReplaySteps
from recon.ai.mapping.agent import AgentMapper
from recon.evaluation.adjudication_eval import run_evaluation as adjudication_run
from recon.evaluation.explanation_eval import explain_inputs
from recon.evaluation.mapping_eval import all_cases, run_evaluation, sample_cases, summarize

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


def test_the_recorded_investigations_give_the_documented_table() -> None:
    from recon.ai.investigation.agent import investigate
    from recon.evaluation.investigation_eval import sample

    model = ReplaySteps(CASSETTES / f"investigation-{MODEL}.jsonl")
    done = cause = found = checked = 0
    for ledger, item in sample(range(2), 200, 10):
        result = investigate(model, ledger, item.case)
        if result.finding is None:
            continue  # not recorded: the free quota ran out before these cases
        done += 1
        checked += result.verified
        cause += result.verified and result.finding["cause"] == item.cause
        real = {ledger.real(str(a)) for a in result.finding["evidence"]}
        found += item.counterparts <= {r[1] for r in real if r is not None}
    assert (done, cause, found, checked) == (26, 25, 26, 26)
