"""Supervisor + specialist agents (Strands 'agents as tools')."""

import time
from typing import Any

from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel
from strands.types.tools import ToolContext

from csagent_agents import prompts
from csagent_agents.config import AgentConfig
from csagent_agents.context import RequestContext
from csagent_agents.tools import (
    KNOWLEDGE_TOOLS,
    ORDER_TOOLS,
    RETURNS_TOOLS,
    SUPPORT_TOOLS,
)


def make_model(cfg: AgentConfig) -> BedrockModel:
    # Low temperature: factual customer service, not creative writing.
    # Guardrails are applied at the trust boundary in service.py, not on every model call.
    return BedrockModel(
        region_name=cfg.region, model_id=cfg.model_id, temperature=0.2, max_tokens=1024
    )


def _delegate(
    name: str,
    system_prompt: str,
    tools: list[Any],
    question: str,
    tool_context: ToolContext,
) -> str:
    """Run a fresh, memory-less specialist for one request. It shares the request's identity
    (invocation_state) and its turn/token limits (invocation_context)."""
    state = tool_context.invocation_state
    request = state.get("request")
    started = time.monotonic()
    if not isinstance(request, RequestContext) or "model" not in state:
        return "Specialist unavailable: missing request context."
    specialist = Agent(
        name=name,
        model=state["model"],
        system_prompt=system_prompt,
        tools=tools,
        callback_handler=None,
    )
    result = specialist(
        (question or "").strip()[:2000],
        invocation_state=state,
        invocation_context=tool_context.invocation_context,
    )
    request.record(f"agent:{name}", str(result.stop_reason), started)
    return str(result)


@tool(context=True)
def ask_order_agent(question: str, tool_context: ToolContext) -> str:
    """Ask the Order Management specialist about orders, shipments or delivery status.

    Args:
        question: A complete, self-contained question including any order id or product named.
    """
    return _delegate("order_agent", prompts.ORDER_AGENT, ORDER_TOOLS, question, tool_context)


@tool(context=True)
def ask_returns_agent(question: str, tool_context: ToolContext) -> str:
    """Ask the Returns & Refunds specialist about eligibility or to submit a return request.

    Args:
        question: A complete request including the order id and, for submissions, the reason.
    """
    return _delegate("returns_agent", prompts.RETURNS_AGENT, RETURNS_TOOLS, question, tool_context)


@tool(context=True)
def ask_support_agent(question: str, tool_context: ToolContext) -> str:
    """Ask the Customer Support specialist to create a ticket or escalate to a human.

    Args:
        question: What happened, the order id if any, and whether to escalate to a human.
    """
    return _delegate("support_agent", prompts.SUPPORT_AGENT, SUPPORT_TOOLS, question, tool_context)


@tool(context=True)
def ask_knowledge_agent(question: str, tool_context: ToolContext) -> str:
    """Ask the Knowledge specialist about company policies, FAQs or procedures.

    Args:
        question: The policy question, e.g. "What is the return window for electronics?".
    """
    return _delegate(
        "knowledge_agent", prompts.KNOWLEDGE_AGENT, KNOWLEDGE_TOOLS, question, tool_context
    )


SUPERVISOR_TOOLS = [ask_order_agent, ask_returns_agent, ask_support_agent, ask_knowledge_agent]


def build_supervisor(model: BedrockModel, tools: list[Any] | None = None) -> Agent:
    return Agent(
        name="supervisor",
        model=model,
        system_prompt=prompts.SUPERVISOR,
        tools=list(tools) if tools is not None else list[Any](SUPERVISOR_TOOLS),
        callback_handler=None,
        conversation_manager=SlidingWindowConversationManager(window_size=20),
    )
