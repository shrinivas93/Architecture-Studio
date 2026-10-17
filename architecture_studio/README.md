# Architecture Studio

Software architects usually design and communicate a system with several separate, disconnected tools: one to draw the architecture, another to generate test data, a spreadsheet for capacity and cost math, and often nothing at all to walk a reviewer through how a request flows through the system. Because each one is maintained by hand, they drift out of sync. Architecture Studio fixes this. You define the system **once**, in a single shared Pydantic model (`model.py`). The architecture diagram (Diagrams), the test data (Faker → SQLite), the capacity and cost numbers (Marimo) and the request-flow animation (Manim) are all generated from that one definition, inside one Gradio UI, so they can never disagree with each other.

## Repository layout

```
architecture_studio/
├── app.py                 # Gradio entrypoint: 5 tabs, shared SystemDesign in gr.State
├── model.py               # Pydantic SystemDesign / Service / Connection, YAML load/save
├── diagram_gen.py         # Diagrams  -> output/diagrams/*.png
├── testdata_gen.py        # Faker     -> output/testdata/*.db (SQLite)
├── capacity_notebook.py   # Marimo notebook + importable compute_capacity()
├── animation_gen.py       # Manim     -> output/animations/*.mp4
├── test_end_to_end.py     # pytest acceptance test
├── designs/               # saved *.yaml designs
├── output/{diagrams,testdata,animations}/
├── requirements.txt, Dockerfile, docker-compose.yml
```

## Local setup

Requires Python 3.11 or newer.

```bash
cd architecture_studio
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then install the system packages: Graphviz (for Diagrams), ffmpeg (for Manim's video encoding), and Manim's Cairo/Pango/LaTeX build dependencies.

**Linux (Debian/Ubuntu):**

```bash
sudo apt-get update
sudo apt-get install -y graphviz ffmpeg \
    texlive texlive-latex-extra \
    libcairo2-dev libpango1.0-dev pkg-config build-essential python3-dev
```

**macOS (Homebrew):**

```bash
brew install graphviz ffmpeg cairo pango pkg-config
brew install --cask mactex-no-gui   # LaTeX for Manim (smaller alternative: brew install --cask basictex)
```

### Run the app

```bash
python app.py
```

This opens Gradio at <http://localhost:7860>.

### Run the acceptance test

```bash
pytest test_end_to_end.py
```

### Run the capacity planner on its own

```bash
marimo run capacity_notebook.py -- --design designs/<your-design>.yaml   # serves a small web app
marimo edit capacity_notebook.py                                          # open it for editing
```

If you leave out `--design`, the notebook offers a dropdown of every `designs/*.yaml`.

## Docker quick-start

```bash
cd architecture_studio
docker compose up --build
```

- Gradio: <http://localhost:7860>
- Marimo capacity planner (started from the Capacity Planner tab): <http://localhost:2718>

`designs/` and `output/` are mounted as volumes, so saved designs and generated files persist on the host.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `GRADIO_SERVER_NAME` | `127.0.0.1` (`0.0.0.0` in Docker) | Gradio bind address |
| `GRADIO_SERVER_PORT` | `7860` | Gradio port |
| `MARIMO_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Marimo bind address |
| `MARIMO_PORT` | `2718` | Marimo port |
| `MARIMO_PUBLIC_URL` | `http://localhost:2718` | URL the **browser** uses for the embedded planner |

## Implementation choices

- **Manim invocation: Python API, no CLI.** `animation_gen.render_request_flow` builds a `Scene` subclass from the design. It renders it in-process with `scene.render()` inside `manim.tempconfig({...})`, using `quality="low_quality"` (equivalent to `-ql`) by default and `"high_quality"` (`-qh`) for `quality="high"`. Rendering happens in a temporary media directory, and the mp4 is then moved to the requested path. Manim's config is process-global, so a lock serialises concurrent renders. Text is rendered with Pango (`Text`), so a working render needs only Cairo, Pango and ffmpeg. The LaTeX packages are installed because they are part of Manim's standard dependency set.
- **Capacity planner embedding: iframe *and* link.** The "Launch capacity planner" button first writes the current design to YAML: the file it was loaded from or last saved to, or `designs/<title>.yaml` if it has never been saved. It then starts `python -m marimo run capacity_notebook.py --host … --port 2718 --headless --no-token -- --design <path>` as a background subprocess. It waits for the port to open and renders a `gr.HTML` block containing an `<iframe>` of `MARIMO_PUBLIC_URL`, plus a clickable link to the same URL as a fallback. `marimo run` sends no `X-Frame-Options` header, so the iframe works. Clicking the button again restarts Marimo on the latest design. The subprocess is terminated when the app exits.
- **Diagram layout.** Services are added to a left-to-right (`direction="LR"`) Diagrams graph in `design.services` order. Clusters are created only for a kind that has more than 6 services; that kind gets one `Cluster`. A missing Graphviz `dot` executable raises `GraphvizNotInstalledError`, which tells you how to install it.
- **Animation layout.** Boxes are placed left to right in topological order. Each service sits in a column given by its longest distance from an entry point, and services in the same column are stacked vertically so arrows never pass through boxes. Edges that point backwards (only possible in a cycle) are drawn as curved arrows. The animated path is found by a DFS from an entry point to a sink, preferring entry points that have outgoing connections.
- **Capacity model** (`compute_capacity`): `total_rps = users × rpm / 60`, split equally across the entry points (services with no incoming connection). Traffic propagates in topological/BFS order. A service's incoming RPS is the sum of the RPS of every service with an edge into it, and every outgoing edge carries the full upstream RPS. Then `instances_needed = ceil(rps / max_throughput_rps)` and `monthly_cost_usd = instances × cost_per_hour_usd × 730`. In a cyclic design, services inside the cycle receive only the traffic arriving from outside it.

## Worked example: `example_design()`

The sample design has 4 services: `api-gateway` (api, 500 RPS/instance, $0.10/h) → `auth-worker` (worker, 200 RPS, $0.08/h) → `users-db` (db, 1000 RPS, $0.25/h), and `api-gateway` → `session-cache` (cache, 5000 RPS, $0.06/h). It is selected by default in the **Load a design** dropdown as `(example design)`. You can also start from `(new empty design)` and build your own.

1. **Design**: the Services table lists the 4 services and the Connections table lists the 3 edges. "Save design" writes `designs/example-login-system.yaml`, and that file then appears in the load dropdown.
2. **Diagram**: "Generate diagram" shows a left-to-right PNG. A Flask icon for `api-gateway` points to a server icon (`auth-worker`) and a Redis icon (`session-cache`), and the server icon points to a PostgreSQL icon (`users-db`). The PNG is saved to `output/diagrams/example-login-system.png`.
3. **Test Data**: with 50 rows per service, the status line reports 4 tables × 50 rows in `output/testdata/example-login-system.db`, followed by a 5-row preview of each table. `svc_api_gateway` has request IDs, IPs, user agents and status codes. `svc_auth_worker` and `svc_session_cache` have job IDs, payload sentences and durations. `svc_users_db` has Indian names, emails, cities and sign-up dates (locale `en_IN`).
4. **Capacity Planner**: "Launch capacity planner" embeds the Marimo app. At the default 1,000 users × 15 requests/min, every service receives 250 RPS. The table shows `api-gateway` 1 instance ($73.00/mo), `auth-worker` 2 instances ($116.80), `users-db` 1 ($182.50) and `session-cache` 1 ($43.80), with the line "Total monthly cost across all services: $416.10". Moving the sliders updates the numbers live.
5. **Animation**: "Render animation" shows a progress bar and then plays an mp4 (about 8 seconds at low quality). Four coloured boxes appear (`api-gateway` on the left; `auth-worker` and `session-cache` in the middle column; `users-db` on the right) with all 3 arrows. A yellow dot travels `api-gateway → auth-worker → users-db`, each box flashes yellow as the dot arrives, and the caption "Request complete" appears.
