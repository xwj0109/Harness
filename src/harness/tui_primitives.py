from __future__ import annotations

import re


STATUS_SYMBOLS = {
    "running": "●",
    "leased": "●",
    "responding": "●",
    "completed": "✓",
    "succeeded": "✓",
    "done": "✓",
    "ready": "○",
    "waiting": "○",
    "idle": "○",
    "active": "○",
    "blocked": "■",
    "failed": "!",
    "approval_required": "◆",
    "waiting_approval": "◆",
    "approval": "◆",
    "artifact": "⬡",
}

STATUS_LABELS = {
    "setup_needed": "needs setup",
    "approval_required": "approval needed",
    "waiting_approval": "needs approval",
    "pending_contract": "needs confirmation",
    "ui_action": "UI updated",
    "in_flight": "running",
    "succeeded": "completed",
    "leased": "running",
}


def status_symbol(value: object) -> str:
    return STATUS_SYMBOLS.get(str(value or "idle").strip().casefold(), "○")


def status_label(value: object) -> str:
    raw = str(value or "idle").strip()
    return STATUS_LABELS.get(raw, humanize_identifier(raw))


def humanize_identifier(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    text = re.sub(r"[_./-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def title_case_identifier(value: object) -> str:
    words = []
    for word in humanize_identifier(value).split():
        lowered = word.casefold()
        if lowered == "ui":
            words.append("UI")
        elif lowered == "tui":
            words.append("TUI")
        else:
            words.append(word.title())
    return " ".join(words)


def first_line(value: object, *, limit: int = 96) -> str:
    text = str(value or "").splitlines()[0].strip()
    return short_text(text, limit=limit)


def short_text(value: object, *, limit: int = 88) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def clean_event_summary(value: object) -> str:
    text = first_line(value, limit=88)
    text = re.sub(r"^\d+\s+", "", text).strip()
    text = re.sub(
        r"\s*\([^)]*\b(?:artifact|lease|msg|perm|run|sess|task|todo)_[^)]+\)",
        "",
        text,
    )
    text = re.sub(
        r"\b(?:artifact|lease|msg|perm|run|sess|task|todo)_[A-Za-z0-9_./-]+",
        "item",
        text,
    )
    return text or "no events"
