"""
Thin Anthropic wrapper for the evaluation passes.

`anthropic` and `pydantic` are imported lazily inside functions so Django system
checks and unit tests (which patch `parse`) don't require the SDK or an API key.

`parse()` is the single choke point every pass calls; tests monkeypatch it to
return canned schema instances, so no network or key is needed to exercise the
orchestration logic.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Type, TypeVar

from django.conf import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


def get_client():
    import anthropic

    # Resolves ANTHROPIC_API_KEY (or an `ant auth login` profile) from the env.
    if settings.ANTHROPIC_API_KEY:
        return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return anthropic.Anthropic()


def image_block(path: str, *, media_type: str = "image/jpeg") -> dict:
    """Build a base64 image content block from a downsampled photo on disk."""
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def cached_system(text: str) -> list[dict]:
    """A single cache-controlled system block (stable prefix -> cache hits)."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def parse(
    *,
    model: str,
    system: str,
    content: list[dict],
    schema: Type[T],
    max_tokens: int | None = None,
) -> T:
    """
    Call the Messages API with a forced structured output and return the parsed,
    validated schema instance. The shared `system` prompt is prompt-cached.
    """
    client = get_client()
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens or settings.EVAL_MAX_TOKENS,
        system=cached_system(system),
        messages=[{"role": "user", "content": content}],
        output_format=schema,
    )
    return response.parsed_output
