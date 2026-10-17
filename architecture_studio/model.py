"""Shared data model for Architecture Studio.

Every generator (diagram, test data, capacity planner, animation) reads a
``SystemDesign`` instance, so the artefacts they produce can never drift apart.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator, model_validator

ServiceKind = Literal["api", "worker", "db", "cache", "queue"]
SERVICE_KINDS: list[str] = ["api", "worker", "db", "cache", "queue"]


class Service(BaseModel):
    name: str
    kind: ServiceKind = "api"
    avg_latency_ms: float = 50.0
    cost_per_hour_usd: float = 0.05
    max_throughput_rps: float = 100.0  # requests/sec ONE instance can handle

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Service name must not be empty")
        return value

    @field_validator("max_throughput_rps")
    @classmethod
    def _throughput_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("max_throughput_rps must be greater than zero")
        return value


class Connection(BaseModel):
    source: str
    target: str


class SystemDesign(BaseModel):
    title: str = "My System"
    services: list[Service] = []
    connections: list[Connection] = []

    @field_validator("services")
    @classmethod
    def _unique_service_names(cls, services: list[Service]) -> list[Service]:
        seen: set[str] = set()
        for service in services:
            if service.name in seen:
                raise ValueError(f"Duplicate service name: '{service.name}'")
            seen.add(service.name)
        return services

    @model_validator(mode="after")
    def _connections_reference_services(self) -> "SystemDesign":
        names = {s.name for s in self.services}
        for conn in self.connections:
            if conn.source not in names:
                raise ValueError(
                    f"Connection source '{conn.source}' does not reference an existing service"
                )
            if conn.target not in names:
                raise ValueError(
                    f"Connection target '{conn.target}' does not reference an existing service"
                )
        return self

    def service(self, name: str) -> Service:
        for s in self.services:
            if s.name == name:
                return s
        raise KeyError(name)

    def save_yaml(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.model_dump(), fh, sort_keys=False)

    @classmethod
    def load_yaml(cls, path: str) -> "SystemDesign":
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.model_validate(data)


def list_saved_designs(directory: str = "designs") -> list[str]:
    """Return the paths of every saved ``*.yaml`` design in ``directory``."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    return sorted(str(p) for p in folder.glob("*.yaml"))


def example_design() -> SystemDesign:
    """A 4-service sample used for first-run convenience and the acceptance test."""
    return SystemDesign(
        title="Example Login System",
        services=[
            Service(name="api-gateway", kind="api", avg_latency_ms=20, cost_per_hour_usd=0.10, max_throughput_rps=500),
            Service(name="auth-worker", kind="worker", avg_latency_ms=80, cost_per_hour_usd=0.08, max_throughput_rps=200),
            Service(name="users-db", kind="db", avg_latency_ms=10, cost_per_hour_usd=0.25, max_throughput_rps=1000),
            Service(name="session-cache", kind="cache", avg_latency_ms=2, cost_per_hour_usd=0.06, max_throughput_rps=5000),
        ],
        connections=[
            Connection(source="api-gateway", target="auth-worker"),
            Connection(source="auth-worker", target="users-db"),
            Connection(source="api-gateway", target="session-cache"),
        ],
    )


def entry_points(design: SystemDesign) -> list[str]:
    """Services with no incoming connection, in design order."""
    targets = {c.target for c in design.connections}
    return [s.name for s in design.services if s.name not in targets]


def topological_order(design: SystemDesign) -> list[str]:
    """Kahn's algorithm over connections; entry points first, ties broken by design order.

    Services left over because of a cycle are appended in design order so every
    service is always placed.
    """
    order_index = {s.name: i for i, s in enumerate(design.services)}
    indegree = {s.name: 0 for s in design.services}
    adjacency: dict[str, list[str]] = {s.name: [] for s in design.services}
    for c in design.connections:
        adjacency[c.source].append(c.target)
        indegree[c.target] += 1
    ready = sorted([n for n, d in indegree.items() if d == 0], key=order_index.get)
    result: list[str] = []
    while ready:
        node = ready.pop(0)
        result.append(node)
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=order_index.get)
    for s in design.services:
        if s.name not in result:
            result.append(s.name)
    return result
