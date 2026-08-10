"""Agent state — deliberately thin: the DB owns artifacts, ledger and jobs;
state carries only what the model needs to reason about THIS turn."""
from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    credits_spent_turn: int   # seamless credits this turn (M3)
    budget: dict              # {"org_today": int, "org_cap": int, "convo_cap": int}
