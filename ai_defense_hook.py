"""
LiteLLM custom handler that inspects incoming prompts with Cisco AI Defense
before the call is forwarded to the LLM provider.

Drop this file next to your proxy config and reference it as:

    litellm_settings:
      callbacks: ai_defense_hook.proxy_handler_instance
"""

import os
import logging
from typing import Any, Literal, Optional, Union

import httpx
from fastapi import HTTPException

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import UserAPIKeyAuth, DualCache

logger = logging.getLogger("ai_defense_hook")

# US, EU, or AP regional endpoint
AI_DEFENSE_URL = os.getenv(
    "AI_DEFENSE_URL",
    "https://us.api.inspect.aidefense.security.cisco.com/api/v1/inspect/chat",
)
AI_DEFENSE_API_KEY = os.environ["AI_DEFENSE_API_KEY"]
INSPECT_TIMEOUT = float(os.getenv("AI_DEFENSE_TIMEOUT", "5.0"))

# Choose which rules to enforce. You can swap this for an integration_profile_id
# if you've configured one in the AI Defense UI.
ENABLED_RULES = [
    {"rule_name": "Prompt Injection"},
    {"rule_name": "PII"},
    {"rule_name": "PCI"},
    {"rule_name": "PHI"},
    {"rule_name": "Hate Speech"},
    {"rule_name": "Harassment"},
    {"rule_name": "Sexual Content & Exploitation"},
    {"rule_name": "Violence & Public Safety Threats"},
]


class AIDefenseHandler(CustomLogger):
    def __init__(self) -> None:
        # Reuse a single client so we don't pay TLS handshake cost per request
        self._client = httpx.AsyncClient(timeout=INSPECT_TIMEOUT)

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
        ],
    ) -> Optional[Union[dict, str, Exception]]:
        print(f"!!! AI_DEFENSE pre_call_hook FIRED  call_type={call_type!r}", flush=True)
        # Only inspect chat-style calls; let everything else through unchanged.
        if call_type not in ("completion", "acompletion", "text_completion", "atext_completion"):
            return data

        messages = data.get("messages") or []
        if not messages:
            return data

        payload = {
            "messages": messages,
            "metadata": {
                # Helpful for correlating events back in the AI Defense console
                "user": getattr(user_api_key_dict, "user_id", None) or "unknown",
                "src_app": "litellm-proxy",
            },
            "config": {"enabled_rules": []},
        }

        try:
            resp = await self._client.post(
                AI_DEFENSE_URL,
                headers={
                    "X-Cisco-AI-Defense-API-Key": AI_DEFENSE_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            verdict = resp.json()
        except Exception as e:
            # Fail-open vs. fail-closed is a policy decision. Default here is
            # fail-closed: if AI Defense is unreachable, refuse the call.
            # Switch to `return data` if you'd rather fail-open.
            logger.exception("AI Defense inspection failed: %s", e)
            raise HTTPException(
                status_code=503,
                detail="Content inspection service unavailable.",
            )

        if verdict.get("is_safe", True):
            return data

        # Unsafe — reject. Two options below; pick one.

        # Option A: return a string -> client gets a normal 200 with a refusal
        # message in the assistant slot. Good UX for end-user chat apps.
        rule_names = [r.get("rule_name") for r in verdict.get("rules", [])]
        return (
            "This request was blocked by content policy "
            f"({', '.join(rule_names) or verdict.get('severity', 'policy violation')})."
        )

        # Option B: raise -> client gets a 400. Better for programmatic callers.
        # raise HTTPException(
        #     status_code=400,
        #     detail={
        #         "error": "blocked_by_ai_defense",
        #         "classifications": verdict.get("classifications"),
        #         "severity": verdict.get("severity"),
        #         "rules": verdict.get("rules"),
        #         "event_id": verdict.get("event_id"),
        #     },
        # )


proxy_handler_instance = AIDefenseHandler()
