"""Diagrams integration: render a SystemDesign as an architecture PNG."""

from __future__ import annotations

import os
import shutil
from collections import Counter

from diagrams import Cluster, Diagram
from diagrams.onprem.compute import Server
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.queue import Kafka
from diagrams.programming.framework import Flask

from model import SystemDesign

KIND_ICONS = {
    "api": Flask,
    "worker": Server,
    "db": PostgreSQL,
    "cache": Redis,
    "queue": Kafka,
}

CLUSTER_THRESHOLD = 6


class GraphvizNotInstalledError(RuntimeError):
    """Raised when the Graphviz ``dot`` executable cannot be found."""


def _require_graphviz() -> None:
    if shutil.which("dot") is None:
        raise GraphvizNotInstalledError(
            "Graphviz is not installed (the 'dot' executable was not found on PATH). "
            "Install it with 'sudo apt-get install graphviz' (Debian/Ubuntu) or "
            "'brew install graphviz' (macOS), then try again."
        )


def render_diagram(design: SystemDesign, out_path: str) -> str:
    """Render ``design`` to a PNG at ``out_path`` and return the PNG's path."""
    _require_graphviz()
    if not design.services:
        raise ValueError("Cannot render a diagram for a design with no services")

    out_path = os.path.abspath(out_path)
    if not out_path.lower().endswith(".png"):
        out_path += ".png"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    filename_no_ext = out_path[: -len(".png")]

    kind_counts = Counter(s.kind for s in design.services)
    clustered_kinds = {k for k, n in kind_counts.items() if n > CLUSTER_THRESHOLD}

    nodes = {}
    with Diagram(
        design.title,
        filename=filename_no_ext,
        outformat="png",
        show=False,
        direction="LR",
    ):
        # Unclustered services in design order (left to right).
        for service in design.services:
            if service.kind not in clustered_kinds:
                nodes[service.name] = KIND_ICONS[service.kind](service.name)
        # One cluster per kind that has more than CLUSTER_THRESHOLD services.
        for kind in [k for k in KIND_ICONS if k in clustered_kinds]:
            with Cluster(f"{kind} services"):
                for service in design.services:
                    if service.kind == kind:
                        nodes[service.name] = KIND_ICONS[kind](service.name)
        for conn in design.connections:
            nodes[conn.source] >> nodes[conn.target]

    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"Diagram rendering failed: {out_path} was not produced")
    return out_path
