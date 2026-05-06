from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: str = "read"


class ToolResult(BaseModel):
    name: str
    ok: bool
    output: str
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    error_type: str | None = None


class ToolContext(BaseModel):
    project_root: Path
    context_excludes: list[str] = Field(default_factory=list)


class Tool(Protocol):
    spec: ToolSpec

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        ...

