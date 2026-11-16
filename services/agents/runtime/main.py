"""AgentCore Runtime entrypoint (direct code deployment: this file is at the zip root)."""

from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from csagent_agents.config import AgentConfig
from csagent_agents.entrypoint import configure_logging, handle_invocation
from csagent_agents.service import AgentService

configure_logging()
app = BedrockAgentCoreApp()
_service: AgentService | None = None


def _get_service() -> AgentService:
    # Built once per microVM; sessions are routed to the same microVM by AgentCore.
    global _service
    if _service is None:
        _service = AgentService(AgentConfig.from_env())
    return _service


@app.entrypoint
def invoke(payload: Any, context: RequestContext) -> dict[str, Any]:
    return handle_invocation(
        payload, context.session_id, context.request_headers or {}, _get_service()
    )


if __name__ == "__main__":
    app.run()
