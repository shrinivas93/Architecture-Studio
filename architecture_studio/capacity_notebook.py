import marimo

__generated_with = "0.25.1"
app = marimo.App(width="medium", app_title="Capacity Planner")

with app.setup:
    import math
    import os
    import sys
    from collections import deque

    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)

    from model import SystemDesign, entry_points, list_saved_designs, topological_order

    HOURS_PER_MONTH = 730


@app.function
def compute_capacity(design: SystemDesign, concurrent_users: int, requests_per_min: float) -> dict[str, dict]:
    """Traffic model: total RPS enters at entry points and propagates along every connection.

    total_rps = concurrent_users * (requests_per_min / 60), split equally across
    entry points (services with no incoming connection). A service's incoming RPS
    is the sum of the RPS of every service with an edge into it (each outgoing
    edge carries the full upstream RPS). Then, per service:
        instances_needed = ceil(incoming_rps / max_throughput_rps)
        monthly_cost_usd = instances_needed * cost_per_hour_usd * 730
    """
    total_rps = concurrent_users * (requests_per_min / 60)
    entries = entry_points(design)
    rps: dict[str, float] = {s.name: 0.0 for s in design.services}
    for name in entries:
        rps[name] = total_rps / len(entries)

    upstream: dict[str, set[str]] = {s.name: set() for s in design.services}
    downstream: dict[str, set[str]] = {s.name: set() for s in design.services}
    for conn in design.connections:
        upstream[conn.target].add(conn.source)
        downstream[conn.source].add(conn.target)

    # Forward propagation (BFS in topological order): a node is computed once
    # all of its upstream services have been computed.
    order = topological_order(design)
    remaining = {n: len(upstream[n]) for n in rps}
    queue = deque(n for n in order if remaining[n] == 0)
    done: set[str] = set()
    while queue:
        node = queue.popleft()
        if node in done:
            continue
        if upstream[node]:
            rps[node] = sum(rps[src] for src in upstream[node])
        done.add(node)
        for nxt in sorted(downstream[node], key=order.index):
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                queue.append(nxt)
    # Services inside a cycle never reach zero remaining inputs; give them the
    # traffic arriving from already-resolved upstream services.
    for node in order:
        if node not in done:
            rps[node] = sum(rps[src] for src in upstream[node])
            done.add(node)

    result: dict[str, dict] = {}
    for service in design.services:
        incoming = rps[service.name]
        instances = math.ceil(incoming / service.max_throughput_rps)
        result[service.name] = {
            "incoming_rps": incoming,
            "instances_needed": instances,
            "monthly_cost_usd": instances * service.cost_per_hour_usd * HOURS_PER_MONTH,
        }
    return result


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _(mo):
    _designs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "designs")
    _options = {os.path.basename(p): os.path.abspath(p) for p in list_saved_designs(_designs_dir)}
    _cli_design = mo.cli_args().get("design")
    _default = None
    if _cli_design:
        _cli_design = os.path.abspath(str(_cli_design))
        _label = os.path.basename(_cli_design)
        _options.setdefault(_label, _cli_design)
        _default = _label
    elif _options:
        _default = next(iter(_options))
    design_picker = mo.ui.dropdown(options=_options, value=_default, label="System design")
    return (design_picker,)


@app.cell
def _(design_picker, mo):
    design = SystemDesign.load_yaml(design_picker.value) if design_picker.value else None
    mo.vstack(
        [
            mo.md("# Capacity Planner"),
            design_picker,
            mo.md(
                f"Loaded **{design.title}** — {len(design.services)} services, "
                f"{len(design.connections)} connections."
                if design
                else "_No saved design found. Save one from the Design tab of the Gradio app._"
            ),
        ]
    )
    return (design,)


@app.cell
def _(mo):
    users_slider = mo.ui.slider(1, 100_000, value=1000, label="Concurrent users", show_value=True)
    rpm_slider = mo.ui.slider(1, 60, value=15, label="Avg requests per user per minute", show_value=True)
    mo.vstack([users_slider, rpm_slider])
    return rpm_slider, users_slider


@app.cell
def _(design, mo, rpm_slider, users_slider):
    mo.stop(design is None, mo.md("_Pick a design to see capacity numbers._"))
    _capacity = compute_capacity(design, users_slider.value, rpm_slider.value)
    _rows = [
        {
            "service": _name,
            "incoming RPS": round(_c["incoming_rps"], 2),
            "instances needed": _c["instances_needed"],
            "monthly cost (USD)": round(_c["monthly_cost_usd"], 2),
        }
        for _name, _c in _capacity.items()
    ]
    _total = sum(_c["monthly_cost_usd"] for _c in _capacity.values())
    mo.vstack(
        [
            mo.ui.table(_rows, selection=None, label="Capacity per service"),
            mo.md(f"**Total monthly cost across all services: ${_total:,.2f}**"),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
