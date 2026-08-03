"""Constrained tool registry for external Maker actions.

Tools receive typed, tenant-scoped context. They never accept arbitrary URLs,
credentials or raw API requests from a model. Every side effect is represented
by a CockroachDB ``tool_execution`` receipt with its own idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ToolContext:
    repo: Any
    business_id: str
    task_id: str
    user_id: str | None = None
    clients: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    status: str
    tool_name: str
    execution_id: str | None = None
    external_reference: str | None = None
    message: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str

    def execute(self, context: ToolContext, payload: Mapping[str, Any]) -> ToolResult:
        ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool {name!r}") from None

    def execute(
        self, name: str, context: ToolContext, payload: Mapping[str, Any],
    ) -> ToolResult:
        return self.get(name).execute(context, payload)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


__all__ = ["Tool", "ToolContext", "ToolResult", "ToolRegistry"]
