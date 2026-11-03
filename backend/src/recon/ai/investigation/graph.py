"""The investigation as a graph: investigate, check, ask a person, act.

LangGraph keeps the state of a run in Postgres after every step, so a run that stops to ask a
person can be resumed hours later, by another process, from where it stopped. A finding that
fails its checks ends the run without being shown; a person who rejects one ends it with nothing
done. Only an approved finding reaches ``act``, which is given by the caller, so the graph
itself never touches a case.
"""

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from recon.ai.investigation.agent import Investigation


class State(TypedDict, total=False):
    finding: dict[str, Any] | None
    problems: list[str]
    trace: list[dict[str, Any]]
    error: str | None
    decision: dict[str, Any]
    outcome: str


def build_graph(
    investigate: Callable[[], Investigation],
    act: Callable[[dict[str, Any], dict[str, Any]], str],
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[State, Any, State, State]:
    """``investigate`` runs the model over the case; ``act`` applies an approved finding and
    says what it did."""

    def run(state: State) -> State:
        done = investigate()
        return {
            "finding": done.finding,
            "problems": done.problems,
            "trace": done.trace,
            "error": done.error,
        }

    def checked(state: State) -> str:
        if state.get("error") or not state.get("finding") or state.get("problems"):
            return "unverified"
        return "verified"

    def ask(state: State) -> State:
        decision = interrupt({"finding": state["finding"], "trace": state["trace"]})
        return {"decision": decision if isinstance(decision, dict) else {"approve": False}}

    def decided(state: State) -> str:
        return "approved" if state["decision"].get("approve") else "rejected"

    def apply(state: State) -> State:
        finding = state["finding"] or {}
        return {"outcome": act(finding, state["decision"])}

    def unverified(state: State) -> State:
        return {"outcome": "not_shown"}

    def rejected(state: State) -> State:
        return {"outcome": "rejected"}

    graph = StateGraph(State)
    graph.add_node("investigate", run)
    graph.add_node("ask", ask)
    graph.add_node("apply", apply)
    graph.add_node("unverified", unverified)
    graph.add_node("rejected", rejected)
    graph.add_edge(START, "investigate")
    graph.add_conditional_edges(
        "investigate", checked, {"verified": "ask", "unverified": "unverified"}
    )
    graph.add_conditional_edges("ask", decided, {"approved": "apply", "rejected": "rejected"})
    for node in ("apply", "unverified", "rejected"):
        graph.add_edge(node, END)
    return graph.compile(checkpointer=checkpointer)
