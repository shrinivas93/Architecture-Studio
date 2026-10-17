"""Architecture Studio — Gradio entrypoint.

One shared SystemDesign (held in gr.State) drives five tabs:
Design (Pydantic + YAML), Diagram (Diagrams), Test Data (Faker + SQLite),
Capacity Planner (Marimo) and Animation (Manim).
"""

from __future__ import annotations

import atexit
import html
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import quote

import gradio as gr
import pandas as pd
from pydantic import ValidationError

from animation_gen import render_request_flow
from diagram_gen import render_diagram
from model import SERVICE_KINDS, Connection, Service, SystemDesign, example_design, list_saved_designs
from testdata_gen import generate_test_data, list_tables, preview_table

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESIGNS_DIR = os.path.join(BASE_DIR, "designs")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DIAGRAMS_DIR = os.path.join(OUTPUT_DIR, "diagrams")
TESTDATA_DIR = os.path.join(OUTPUT_DIR, "testdata")
ANIMATIONS_DIR = os.path.join(OUTPUT_DIR, "animations")
NOTEBOOK_PATH = os.path.join(BASE_DIR, "capacity_notebook.py")

EXAMPLE_CHOICE = "(example design)"
EMPTY_CHOICE = "(new empty design)"

# Hugging Face Spaces sets SPACE_ID; there the app must listen on all interfaces.
ON_SPACES = bool(os.environ.get("SPACE_ID"))
GRADIO_HOST = os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0" if ON_SPACES else "127.0.0.1")
GRADIO_PORT = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
MARIMO_HOST = os.environ.get("MARIMO_HOST", "127.0.0.1")
MARIMO_PORT = int(os.environ.get("MARIMO_PORT", "2718"))
# URL the *browser* uses to reach Marimo (differs from MARIMO_HOST behind Docker/proxies).
MARIMO_PUBLIC_URL = os.environ.get("MARIMO_PUBLIC_URL", f"http://localhost:{MARIMO_PORT}")
# Single-port mode serves the Marimo notebook in-process at CAPACITY_MOUNT on the
# Gradio port, for hosts that expose only one port. It is on automatically on
# Hugging Face Spaces (which sets SPACE_ID).
SINGLE_PORT = os.environ.get("SINGLE_PORT", "").lower() in ("1", "true", "yes") or ON_SPACES
CAPACITY_MOUNT = "/capacity"

# Manim configures root logging at INFO; keep Gradio's HTTP client quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)

for _d in (DESIGNS_DIR, DIAGRAMS_DIR, TESTDATA_DIR, ANIMATIONS_DIR):
    os.makedirs(_d, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-").lower()
    return slug or "design"


def service_names(design: SystemDesign) -> list[str]:
    return [s.name for s in design.services]


def connection_labels(design: SystemDesign) -> list[str]:
    return [f"{c.source} -> {c.target}" for c in design.connections]


def services_frame(design: SystemDesign) -> pd.DataFrame:
    columns = ["name", "kind", "avg_latency_ms", "cost_per_hour_usd", "max_throughput_rps"]
    return pd.DataFrame([s.model_dump() for s in design.services], columns=columns)


def connections_frame(design: SystemDesign) -> pd.DataFrame:
    return pd.DataFrame([c.model_dump() for c in design.connections], columns=["source", "target"])


def design_choices() -> list[str]:
    return [EXAMPLE_CHOICE, EMPTY_CHOICE] + [os.path.basename(p) for p in list_saved_designs(DESIGNS_DIR)]


def design_views(design: SystemDesign, message: str):
    """All Design-tab outputs that depend on the current design, in a fixed order."""
    names = service_names(design)
    conns = connection_labels(design)
    return (
        design,
        design.title,
        services_frame(design),
        connections_frame(design),
        gr.Dropdown(choices=names, value=None),
        gr.Dropdown(choices=names, value=names[0] if names else None),
        gr.Dropdown(choices=names, value=names[1] if len(names) > 1 else None),
        gr.Dropdown(choices=conns, value=None),
        message,
    )


def require_services(design: SystemDesign) -> None:
    if not design.services:
        raise gr.Error("The current design has no services. Add some in the Design tab first.")


def persist_current(design: SystemDesign, current_path: str | None) -> str:
    """Write the current design to YAML (its loaded/saved path, or designs/<title>.yaml)."""
    path = current_path or os.path.join(DESIGNS_DIR, f"{slugify(design.title)}.yaml")
    design.save_yaml(path)
    return path


# ---------------------------------------------------------------------------
# Design tab handlers
# ---------------------------------------------------------------------------

def set_title(design: SystemDesign, title: str):
    title = (title or "").strip() or "My System"
    return design.model_copy(update={"title": title})


def add_service(design, name, kind, latency, cost, max_rps):
    name = (name or "").strip()
    if not name:
        raise gr.Error("Service name is required.")
    try:
        new_service = Service(
            name=name,
            kind=kind,
            avg_latency_ms=float(latency),
            cost_per_hour_usd=float(cost),
            max_throughput_rps=float(max_rps),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise gr.Error(f"Invalid service: {exc}") from exc
    services = [s for s in design.services if s.name != name]
    replaced = len(services) != len(design.services)
    if replaced:
        services = [new_service if s.name == name else s for s in design.services]
    else:
        services.append(new_service)
    updated = SystemDesign(title=design.title, services=services, connections=design.connections)
    verb = "Updated" if replaced else "Added"
    return design_views(updated, f"{verb} service '{name}' ({kind}).")


def remove_service(design, name):
    if not name:
        raise gr.Error("Pick a service to remove.")
    updated = SystemDesign(
        title=design.title,
        services=[s for s in design.services if s.name != name],
        connections=[c for c in design.connections if name not in (c.source, c.target)],
    )
    return design_views(updated, f"Removed service '{name}' and its connections.")


def add_connection(design, source, target):
    if not source or not target:
        raise gr.Error("Pick both a source and a target service.")
    if any(c.source == source and c.target == target for c in design.connections):
        raise gr.Error(f"Connection {source} -> {target} already exists.")
    try:
        updated = SystemDesign(
            title=design.title,
            services=design.services,
            connections=design.connections + [Connection(source=source, target=target)],
        )
    except ValidationError as exc:
        raise gr.Error(str(exc)) from exc
    return design_views(updated, f"Added connection {source} -> {target}.")


def remove_connection(design, label):
    if not label:
        raise gr.Error("Pick a connection to remove.")
    source, target = label.split(" -> ", 1)
    updated = SystemDesign(
        title=design.title,
        services=design.services,
        connections=[c for c in design.connections if not (c.source == source and c.target == target)],
    )
    return design_views(updated, f"Removed connection {label}.")


def save_design(design, filename):
    stem = slugify(os.path.splitext(filename or "")[0] or design.title)
    path = os.path.join(DESIGNS_DIR, f"{stem}.yaml")
    design.save_yaml(path)
    return path, gr.Dropdown(choices=design_choices(), value=os.path.basename(path)), f"Saved to {path}"


def load_design(choice):
    if choice == EXAMPLE_CHOICE or not choice:
        design, path = example_design(), None
    elif choice == EMPTY_CHOICE:
        design, path = SystemDesign(), None
    else:
        path = os.path.join(DESIGNS_DIR, choice)
        try:
            design = SystemDesign.load_yaml(path)
        except (OSError, ValidationError, ValueError) as exc:
            raise gr.Error(f"Could not load {choice}: {exc}") from exc
    return (*design_views(design, f"Loaded {choice}."), path, slugify(design.title))


def refresh_design_list():
    return gr.Dropdown(choices=design_choices())


# ---------------------------------------------------------------------------
# Diagram / Test data / Animation handlers
# ---------------------------------------------------------------------------

def generate_diagram(design):
    require_services(design)
    out = os.path.join(DIAGRAMS_DIR, f"{slugify(design.title)}.png")
    try:
        path = render_diagram(design, out)
    except Exception as exc:  # surface Graphviz-missing and other errors in the UI
        raise gr.Error(str(exc)) from exc
    return path, f"Diagram saved to {path}"


def generate_data(design, rows):
    require_services(design)
    rows = int(rows or 0)
    if rows < 1:
        raise gr.Error("Rows per service must be at least 1.")
    db_path = os.path.join(TESTDATA_DIR, f"{slugify(design.title)}.db")
    generate_test_data(design, db_path, rows_per_service=rows)
    tables = list_tables(db_path)
    summary = f"Wrote {len(tables)} tables x {rows} rows to {db_path}: " + ", ".join(tables)
    return db_path, summary


def render_animation(design, quality, progress=gr.Progress()):
    require_services(design)
    progress(0.05, desc="Building Manim scene")
    out = os.path.join(ANIMATIONS_DIR, f"{slugify(design.title)}_{quality}.mp4")
    progress(0.2, desc=f"Rendering with Manim ({quality} quality) — this can take a while")
    try:
        path = render_request_flow(design, out, quality=quality)
    except Exception as exc:
        raise gr.Error(f"Animation failed: {exc}") from exc
    progress(1.0, desc="Done")
    return path, f"Animation saved to {path}"


# ---------------------------------------------------------------------------
# Capacity planner (Marimo subprocess)
# ---------------------------------------------------------------------------

_marimo_proc: subprocess.Popen | None = None


def _stop_marimo() -> None:
    global _marimo_proc
    if _marimo_proc and _marimo_proc.poll() is None:
        _marimo_proc.terminate()
        try:
            _marimo_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _marimo_proc.kill()
    _marimo_proc = None


atexit.register(_stop_marimo)


def _exit_on_sigterm(signum, frame):
    # Turn SIGTERM (e.g. `docker stop`) into a normal exit so atexit stops Marimo.
    sys.exit(0)


def _port_open(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe_host, port)) == 0


def launch_capacity_planner(design, current_path):
    global _marimo_proc
    require_services(design)
    design_path = persist_current(design, current_path)
    if SINGLE_PORT:
        url = f"{CAPACITY_MOUNT}/?design={quote(os.path.basename(design_path))}"
        return design_path, capacity_html(design, design_path, url)
    _stop_marimo()
    deadline = time.time() + 10
    while _port_open(MARIMO_HOST, MARIMO_PORT) and time.time() < deadline:
        time.sleep(0.2)
    if _port_open(MARIMO_HOST, MARIMO_PORT):
        raise gr.Error(f"Port {MARIMO_PORT} is already in use by another process; set MARIMO_PORT.")
    cmd = [
        sys.executable, "-m", "marimo", "run", NOTEBOOK_PATH,
        "--host", MARIMO_HOST, "--port", str(MARIMO_PORT), "--headless", "--no-token",
        "--", "--design", design_path,
    ]
    _marimo_proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while time.time() < deadline:
        if _marimo_proc.poll() is not None:
            err = _marimo_proc.stderr.read().decode(errors="replace") if _marimo_proc.stderr else ""
            raise gr.Error(f"Marimo exited early: {err[-800:]}")
        if _port_open(MARIMO_HOST, MARIMO_PORT):
            break
        time.sleep(0.3)
    else:
        _stop_marimo()
        raise gr.Error("Marimo did not start within 60 seconds.")
    return design_path, capacity_html(design, design_path, MARIMO_PUBLIC_URL)


def capacity_html(design: SystemDesign, design_path: str, url: str) -> str:
    title, path, url = html.escape(design.title), html.escape(design_path), html.escape(url, quote=True)
    return (
        f'<p>Capacity planner for <b>{title}</b> (design file <code>{path}</code>) '
        f'is running at <a href="{url}" target="_blank">{url}</a> — open it in a new tab if the frame below is blank.</p>'
        f'<iframe src="{url}" style="width:100%;height:820px;border:1px solid #ccc;border-radius:6px;"></iframe>'
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def build_app() -> gr.Blocks:
    initial = example_design()
    with gr.Blocks(title="Architecture Studio") as demo:
        design_state = gr.State(initial)
        path_state = gr.State(None)
        db_state = gr.State(None)

        gr.Markdown(
            "# Architecture Studio\nDefine your system once — the diagram, test data, "
            "capacity plan and request-flow animation are all generated from the same design."
        )

        with gr.Tab("Design"):
            with gr.Row():
                load_dd = gr.Dropdown(design_choices(), value=EXAMPLE_CHOICE, label="Load a design", scale=3)
                load_btn = gr.Button("Load", scale=1)
                refresh_btn = gr.Button("Refresh list", scale=1)
            title_box = gr.Textbox(value=initial.title, label="System title")
            with gr.Row():
                with gr.Column():
                    gr.Markdown("### Add / update a service")
                    svc_name = gr.Textbox(label="Name (existing name = update)")
                    svc_kind = gr.Dropdown(SERVICE_KINDS, value="api", label="Kind")
                    with gr.Row():
                        svc_latency = gr.Number(value=50.0, label="Avg latency (ms)")
                        svc_cost = gr.Number(value=0.05, label="Cost per hour (USD)")
                        svc_rps = gr.Number(value=100.0, label="Max RPS per instance")
                    add_svc_btn = gr.Button("Add / update service", variant="primary")
                    with gr.Row():
                        remove_svc_dd = gr.Dropdown(service_names(initial), label="Service to remove")
                        remove_svc_btn = gr.Button("Remove service")
                with gr.Column():
                    gr.Markdown("### Connections")
                    names = service_names(initial)
                    with gr.Row():
                        conn_src = gr.Dropdown(names, value=names[0], label="Source")
                        conn_dst = gr.Dropdown(names, value=names[1], label="Target")
                    add_conn_btn = gr.Button("Add connection", variant="primary")
                    with gr.Row():
                        remove_conn_dd = gr.Dropdown(connection_labels(initial), label="Connection to remove")
                        remove_conn_btn = gr.Button("Remove connection")
            services_df = gr.Dataframe(services_frame(initial), label="Services", interactive=False)
            connections_df = gr.Dataframe(connections_frame(initial), label="Connections", interactive=False)
            with gr.Row():
                save_name = gr.Textbox(value=slugify(initial.title), label="File name (saved to designs/)")
                save_btn = gr.Button("Save design", variant="primary")
            design_status = gr.Markdown()

        with gr.Tab("Diagram"):
            diagram_btn = gr.Button("Generate diagram", variant="primary")
            diagram_img = gr.Image(label="Architecture diagram", type="filepath")
            diagram_status = gr.Markdown()

        with gr.Tab("Test Data"):
            with gr.Row():
                rows_input = gr.Number(value=50, precision=0, minimum=1, label="Rows per service")
                data_btn = gr.Button("Generate test data", variant="primary")
            data_status = gr.Markdown()

            # Own container + explicit trigger: a re-render must never reset the
            # rows input above it.
            with gr.Column():
                @gr.render(inputs=db_state, triggers=[db_state.change])
                def show_tables(db_path):
                    if not db_path or not os.path.isfile(db_path):
                        gr.Markdown("_No test data generated yet._")
                        return
                    for table in list_tables(db_path):
                        headers, rows = preview_table(db_path, table, limit=5)
                        gr.Dataframe(
                            pd.DataFrame(rows, columns=headers),
                            label=f"{table} (first 5 rows)",
                            interactive=False,
                        )

        with gr.Tab("Capacity Planner"):
            gr.Markdown(
                "Opens the Marimo notebook `capacity_notebook.py` "
                + (f"(served at `{CAPACITY_MOUNT}/` on this server)" if SINGLE_PORT
                   else f"as a background server on port {MARIMO_PORT}")
                + ", pointed at the current design (saved to YAML first)."
            )
            capacity_btn = gr.Button("Launch capacity planner", variant="primary")
            capacity_frame = gr.HTML()

        with gr.Tab("Animation"):
            with gr.Row():
                quality_radio = gr.Radio(["low", "high"], value="low", label="Quality (-ql / -qh)")
                anim_btn = gr.Button("Render animation", variant="primary")
            anim_video = gr.Video(label="Request flow")
            anim_status = gr.Markdown()

        design_outputs = [
            design_state, title_box, services_df, connections_df,
            remove_svc_dd, conn_src, conn_dst, remove_conn_dd, design_status,
        ]
        title_box.change(set_title, [design_state, title_box], design_state)
        add_svc_btn.click(add_service, [design_state, svc_name, svc_kind, svc_latency, svc_cost, svc_rps], design_outputs)
        remove_svc_btn.click(remove_service, [design_state, remove_svc_dd], design_outputs)
        add_conn_btn.click(add_connection, [design_state, conn_src, conn_dst], design_outputs)
        remove_conn_btn.click(remove_connection, [design_state, remove_conn_dd], design_outputs)
        save_btn.click(save_design, [design_state, save_name], [path_state, load_dd, design_status])
        load_btn.click(load_design, load_dd, design_outputs + [path_state, save_name])
        refresh_btn.click(refresh_design_list, None, load_dd)

        diagram_btn.click(generate_diagram, design_state, [diagram_img, diagram_status])
        data_btn.click(generate_data, [design_state, rows_input], [db_state, data_status])
        capacity_btn.click(launch_capacity_planner, [design_state, path_state], [path_state, capacity_frame])
        anim_btn.click(render_animation, [design_state, quality_radio], [anim_video, anim_status])
    return demo


def serve_single_port() -> None:
    """Serve Gradio at / and the Marimo notebook at CAPACITY_MOUNT on one port."""
    import marimo
    import uvicorn
    from fastapi import FastAPI

    server = FastAPI()
    notebook_app = marimo.create_asgi_app(quiet=True).with_app(path="", root=NOTEBOOK_PATH).build()
    server.mount(CAPACITY_MOUNT, notebook_app)
    server = gr.mount_gradio_app(server, build_app(), path="/", allowed_paths=[OUTPUT_DIR])
    uvicorn.run(server, host=GRADIO_HOST, port=GRADIO_PORT)


if __name__ == "__main__":
    if SINGLE_PORT:
        serve_single_port()
    else:
        signal.signal(signal.SIGTERM, _exit_on_sigterm)
        build_app().launch(
            server_name=GRADIO_HOST,
            server_port=GRADIO_PORT,
            allowed_paths=[OUTPUT_DIR],
        )
