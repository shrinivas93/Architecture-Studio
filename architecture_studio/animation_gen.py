"""Manim integration: animate one request travelling through a SystemDesign.

Rendering uses Manim's Python API (``Scene.render()`` inside ``tempconfig``), so
no manual ``manim`` CLI call is needed. Manim's configuration is process-global,
so renders are serialised with a lock.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading

from manim import (
    DOWN,
    GREEN,
    UP,
    WHITE,
    YELLOW,
    ArcBetweenPoints,
    Arrow,
    Create,
    Dot,
    FadeIn,
    Line,
    MoveAlongPath,
    RoundedRectangle,
    Scene,
    Text,
    VGroup,
    Write,
    linear,
    there_and_back,
    tempconfig,
)

from model import SystemDesign, entry_points, topological_order

QUALITY_FLAGS = {"low": "low_quality", "high": "high_quality"}  # -ql / -qh
KIND_COLORS = {
    "api": "#4C8BF5",
    "worker": "#8E6CEF",
    "db": "#2E9E6B",
    "cache": "#E5533D",
    "queue": "#F2A93B",
}

_render_lock = threading.Lock()


def compute_levels(design: SystemDesign) -> tuple[list[str], dict[str, int]]:
    """Topological order plus a column index per service (longest path from an entry point).

    Edges that point backwards in the order (only possible with cycles) are
    ignored for levelling so every service gets a column.
    """
    order = topological_order(design)
    position = {name: i for i, name in enumerate(order)}
    level: dict[str, int] = {}
    for name in order:
        preds = [
            c.source
            for c in design.connections
            if c.target == name and position[c.source] < position[name]
        ]
        level[name] = max((level[p] + 1 for p in preds), default=0)
    order = sorted(order, key=lambda n: (level[n], position[n]))
    return order, level


def representative_path(design: SystemDesign) -> list[str]:
    """DFS from an entry point to a sink (service without outgoing connections).

    Falls back to the longest acyclic path found if every reachable service is
    part of a cycle and no sink is reachable.
    """
    if not design.services:
        return []
    adjacency: dict[str, list[str]] = {s.name: [] for s in design.services}
    for c in design.connections:
        if c.target not in adjacency[c.source]:
            adjacency[c.source].append(c.target)
    # Entry points with outgoing connections first, so an isolated service
    # never wins over a real multi-hop path.
    starts = sorted(entry_points(design) or [design.services[0].name], key=lambda n: not adjacency[n])
    best: list[str] = []

    def dfs(node: str, path: list[str]) -> list[str] | None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        if not adjacency[node]:
            return list(path)
        for nxt in adjacency[node]:
            if nxt in path:
                continue
            found = dfs(nxt, path + [nxt])
            if found:
                return found
        return None

    trivial: list[str] | None = None
    for start in starts:
        found = dfs(start, [start])
        if found and len(found) > 1:
            return found
        if found and trivial is None:
            trivial = found
    # No multi-hop entry->sink path: prefer the longest acyclic walk over an
    # isolated service, so the animation still shows traffic moving.
    if len(best) > 1 or trivial is None:
        return best
    return trivial


def _build_scene_class(design: SystemDesign) -> type[Scene]:
    order, level = compute_levels(design)
    path = representative_path(design)

    class RequestFlowScene(Scene):
        def construct(self) -> None:
            columns: dict[int, list[str]] = {}
            for name in order:
                columns.setdefault(level[name], []).append(name)
            n_cols = len(columns)
            max_rows = max(len(v) for v in columns.values())
            usable_w, usable_h = 13.0, 5.6
            col_w = usable_w / n_cols
            row_h = usable_h / max_rows
            box_w = min(2.6, col_w * 0.72)
            box_h = min(1.1, row_h * 0.6)

            title = Text(design.title, font_size=30).to_edge(UP, buff=0.3)
            boxes: dict[str, VGroup] = {}
            for col, names in columns.items():
                x = -usable_w / 2 + col_w * (col + 0.5)
                for row, name in enumerate(names):
                    y = (len(names) - 1) * row_h / 2 - row * row_h - 0.2
                    service = design.service(name)
                    rect = RoundedRectangle(
                        corner_radius=0.12,
                        width=box_w,
                        height=box_h,
                        color=KIND_COLORS[service.kind],
                        fill_color=KIND_COLORS[service.kind],
                        fill_opacity=0.25,
                    )
                    label = Text(name, font_size=22, color=WHITE)
                    kind = Text(service.kind, font_size=14, color=WHITE)
                    text = VGroup(label, kind).arrange(DOWN, buff=0.06)
                    text.scale_to_fit_width(min(text.width, box_w * 0.88))
                    if text.height > box_h * 0.85:
                        text.scale_to_fit_height(box_h * 0.85)
                    group = VGroup(rect, text.move_to(rect.get_center()))
                    group.move_to([x, y, 0])
                    boxes[name] = group

            arrows: dict[tuple[str, str], tuple[Arrow, object]] = {}
            for c in design.connections:
                key = (c.source, c.target)
                if key in arrows:
                    continue
                src, dst = boxes[c.source][0], boxes[c.target][0]
                forward = level[c.target] > level[c.source]
                arc = 0.0 if forward else -1.2
                if c.source == c.target:
                    start = src.get_top() + [-box_w * 0.25, 0, 0]
                    end = src.get_top() + [box_w * 0.25, 0, 0]
                    arc = -2.5
                elif forward:
                    start, end = src.get_right(), dst.get_left()
                else:
                    start, end = src.get_top(), dst.get_top()
                arrow = Arrow(start, end, buff=0.05, path_arc=arc, stroke_width=3,
                              max_tip_length_to_length_ratio=0.15)
                track = Line(start, end) if arc == 0.0 else ArcBetweenPoints(start, end, angle=arc)
                arrows[key] = (arrow, track)

            self.add(title)
            self.play(*[FadeIn(b) for b in boxes.values()], run_time=0.8)
            self.play(*[Create(a) for a, _ in arrows.values()], run_time=0.8)

            if path:
                dot = Dot(boxes[path[0]].get_center(), radius=0.12, color=YELLOW)
                self.play(FadeIn(dot, scale=2), run_time=0.3)
                self._flash(boxes[path[0]][0])
                for src, dst in zip(path, path[1:]):
                    arrow, track = arrows[(src, dst)]
                    self.play(dot.animate.move_to(track.get_start()), run_time=0.2)
                    self.play(MoveAlongPath(dot, track), run_time=0.7, rate_func=linear)
                    self.play(dot.animate.move_to(boxes[dst].get_center()), run_time=0.2)
                    self._flash(boxes[dst][0])
                self.play(dot.animate.set_opacity(0), run_time=0.2)

            caption = Text("Request complete", font_size=32, color=GREEN).to_edge(DOWN, buff=0.4)
            self.play(Write(caption), run_time=0.8)
            self.wait(0.8)

        def _flash(self, rect: RoundedRectangle) -> None:
            self.play(
                rect.animate(rate_func=there_and_back).set_fill(YELLOW, opacity=0.9),
                run_time=0.5,
            )

    return RequestFlowScene


def render_request_flow(design: SystemDesign, out_path: str, quality: str = "low") -> str:
    """Render the request-flow animation to ``out_path`` (mp4) and return the path."""
    if quality not in QUALITY_FLAGS:
        raise ValueError(f"quality must be one of {sorted(QUALITY_FLAGS)}, got '{quality}'")
    if not design.services:
        raise ValueError("Cannot animate a design with no services")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg is not installed. Install it with 'sudo apt-get install ffmpeg' "
            "(Debian/Ubuntu) or 'brew install ffmpeg' (macOS)."
        )

    out_path = os.path.abspath(out_path)
    if not out_path.lower().endswith(".mp4"):
        out_path += ".mp4"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    scene_cls = _build_scene_class(design)

    with _render_lock, tempfile.TemporaryDirectory(prefix="manim_") as media_dir:
        with tempconfig(
            {
                "quality": QUALITY_FLAGS[quality],
                "media_dir": media_dir,
                "output_file": "request_flow",
                "disable_caching": True,
                "verbosity": "WARNING",
                "progress_bar": "none",
                "write_to_movie": True,
                "format": "mp4",
                "preview": False,
            }
        ):
            scene = scene_cls()
            scene.render()
            produced = str(scene.renderer.file_writer.movie_file_path)
        if not os.path.isfile(produced):
            raise RuntimeError(f"Manim did not produce a movie file (expected {produced})")
        shutil.move(produced, out_path)

    if os.path.getsize(out_path) == 0:
        raise RuntimeError(f"Animation rendering produced an empty file: {out_path}")
    return out_path
