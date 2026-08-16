"""LLM token metering hook — skeleton for C0."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.billing.usage_service import record_usage


def meter_llm_extraction(
    *,
    tenant_id: UUID | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    user_id: UUID | None = None,
    source: str = "llm",
) -> None:
    total = prompt_tokens + completion_tokens
    if total <= 0:
        return
    record_usage(
        tenant_id=tenant_id,
        metric="llm_extraction_tokens",
        quantity=total,
        user_id=user_id,
        source=source,
        metadata={
            "model": model,
            "prompt": prompt_tokens,
            "completion": completion_tokens,
        },
    )


def meter_llm_chat(
    *,
    tenant_id: UUID | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    user_id: UUID | None = None,
) -> None:
    total = prompt_tokens + completion_tokens
    if total <= 0:
        return
    record_usage(
        tenant_id=tenant_id,
        metric="llm_chat_tokens",
        quantity=total,
        user_id=user_id,
        source="hermes",
        metadata={"model": model, "prompt": prompt_tokens, "completion": completion_tokens},
    )


def meter_hermes_message(*, tenant_id: UUID | None, user_id: UUID | None = None, session_id: str | None = None) -> None:
    record_usage(
        tenant_id=tenant_id,
        metric="hermes_messages",
        quantity=1,
        user_id=user_id,
        source="hermes",
        metadata={"session_id": session_id} if session_id else None,
    )
