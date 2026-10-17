"""End-to-end acceptance test for Architecture Studio (run: pytest test_end_to_end.py)."""

import importlib
import math
import os
import sqlite3

import pytest

from animation_gen import render_request_flow
from diagram_gen import render_diagram
from model import Connection, Service, SystemDesign
from testdata_gen import generate_test_data

ROWS = 25


@pytest.fixture(scope="module")
def design() -> SystemDesign:
    return SystemDesign(
        title="Acceptance Test System",
        services=[
            Service(name="gateway", kind="api", max_throughput_rps=100),
            Service(name="worker", kind="worker"),
            Service(name="db", kind="db"),
            Service(name="cache", kind="cache"),
        ],
        connections=[
            Connection(source="gateway", target="worker"),
            Connection(source="worker", target="db"),
            Connection(source="gateway", target="cache"),
        ],
    )


def test_yaml_roundtrip(design, tmp_path):
    path = tmp_path / "design.yaml"
    design.save_yaml(str(path))
    assert path.is_file()
    assert SystemDesign.load_yaml(str(path)) == design


def test_validation_rejects_bad_references():
    with pytest.raises(ValueError, match="nope"):
        SystemDesign(services=[Service(name="a")], connections=[Connection(source="a", target="nope")])
    with pytest.raises(ValueError, match="Duplicate"):
        SystemDesign(services=[Service(name="a"), Service(name="a")])


def test_render_diagram(design, tmp_path):
    out = render_diagram(design, str(tmp_path / "diagram.png"))
    assert os.path.isfile(out)
    assert os.path.getsize(out) > 0


def test_generate_test_data(design, tmp_path):
    db_path = generate_test_data(design, str(tmp_path / "data.db"), rows_per_service=ROWS)
    assert os.path.isfile(db_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'svc_%'")
        ]
        assert sorted(tables) == sorted(["svc_gateway", "svc_worker", "svc_db", "svc_cache"])
        for table in tables:
            (count,) = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            assert count == ROWS, f"{table} has {count} rows, expected {ROWS}"
    finally:
        conn.close()
    # Re-runnable: a second run replaces the tables instead of appending.
    generate_test_data(design, db_path, rows_per_service=ROWS)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM "svc_gateway"').fetchone()[0] == ROWS
    finally:
        conn.close()


def test_render_request_flow(design, tmp_path):
    out = render_request_flow(design, str(tmp_path / "flow.mp4"), quality="low")
    assert os.path.isfile(out)
    assert os.path.getsize(out) > 0


def test_capacity(design):
    notebook = importlib.import_module("capacity_notebook")
    result = notebook.compute_capacity(design, concurrent_users=1000, requests_per_min=15)
    # Worked by hand: total_rps = 1000 * (15 / 60) = 250 rps, all into the single
    # entry point "gateway"; ceil(250 / 100) = 3 instances.
    assert result["gateway"]["incoming_rps"] == 250.0
    assert result["gateway"]["instances_needed"] == 3
    assert result["gateway"]["instances_needed"] == math.ceil(1000 * (15 / 60) / 100)
    # 3 instances * $0.05/h * 730 h = $109.50
    assert result["gateway"]["monthly_cost_usd"] == pytest.approx(109.5)
    # Each outgoing edge carries the full upstream RPS.
    assert result["worker"]["incoming_rps"] == 250.0
    assert result["cache"]["incoming_rps"] == 250.0
    assert result["db"]["incoming_rps"] == 250.0
