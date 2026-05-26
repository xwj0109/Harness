from __future__ import annotations

ALLOWED_MODEL_PROTOCOLS = frozenset(
    {
        "codex_cli",
        "openai_chat",
        "openai_responses",
        "openai_codex_responses",
        "anthropic_messages",
        "bedrock_converse",
        "google_generative",
    }
)


def is_allowed_model_protocol(protocol: object) -> bool:
    return isinstance(protocol, str) and protocol.strip() in ALLOWED_MODEL_PROTOCOLS
