from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import Mock
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def extracted_functions(names: set[str], namespace: dict[str, Any]) -> dict[str, Any]:
    module = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names
    isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    ast.fix_missing_locations(isolated)
    exec(compile(isolated, "public-service-functions", "exec"), namespace)
    return namespace


def service_namespace() -> dict[str, Any]:
    reservation = SimpleNamespace(stop=Mock())
    namespace = {
        "MACHINE": {"features": {"mail": False, "research": False}},
        "Path": Path, "Any": Any, "Callable": Callable, "subprocess": subprocess,
        "run_bridge": Mock(), "RESERVATION_PATH": Path("synthetic/reservations.json"),
        "MAIL_SNAPSHOT": Path("synthetic/snapshot.json"),
        "MAIL_JOBS_PATH": Path("synthetic/mail.json"),
        "LAUNCHES_PATH": Path("synthetic/launches.json"),
        "RESEARCH_JOBS_PATH": Path("synthetic/research.json"),
        "_run_hidden": Mock(side_effect=AssertionError("process must not start")),
        "process_mail_draft": Mock(side_effect=AssertionError("mail must not start")),
        "ReservationService": Mock(return_value=reservation),
        "MailJobService": Mock(side_effect=AssertionError("mail service must not be constructed")),
        "MailSnapshotStore": Mock(side_effect=AssertionError("mail store must not be read")),
        "ResearchJobService": Mock(side_effect=AssertionError("research service must not be constructed")),
        "LaunchService": Mock(return_value=object()), "PocketHandler": object,
        "PocketHTTPServer": Mock(side_effect=lambda *args, **kwargs: SimpleNamespace(**kwargs)),
    }
    return extracted_functions({"public_features", "make_server"}, namespace)


def test_disabled_services_do_not_construct_or_read_saved_jobs(tmp_path: Path) -> None:
    files = [tmp_path / "mail.json", tmp_path / "research.json"]
    for path in files:
        path.write_text('{"jobs":[{"id":"synthetic-job","status":"running"}]}', encoding="utf-8")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    namespace = service_namespace()
    server = namespace["make_server"](mail_jobs_path=files[0], research_jobs_path=files[1])
    assert server.mail_service is None
    assert server.research_service is None
    for name in ("MailJobService", "MailSnapshotStore", "ResearchJobService", "_run_hidden", "process_mail_draft"):
        namespace[name].assert_not_called()
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}


@pytest.mark.parametrize("feature", ["mail", "research"])
def test_unbundled_integration_is_rejected_before_service_creation(feature: str) -> None:
    namespace = service_namespace()
    with pytest.raises(ValueError, match="not included"):
        namespace["make_server"](features={feature: True})
    namespace["ReservationService"].assert_not_called()
    namespace["PocketHTTPServer"].assert_not_called()


def test_bind_failure_stops_only_created_services() -> None:
    namespace = service_namespace()
    namespace["PocketHTTPServer"].side_effect = OSError("synthetic bind failure")
    with pytest.raises(OSError, match="synthetic"):
        namespace["make_server"]()
    namespace["ReservationService"].return_value.stop.assert_called_once()
    namespace["MailJobService"].assert_not_called()
    namespace["ResearchJobService"].assert_not_called()


def test_unconfigured_ask_bridge_has_no_process_or_file_access() -> None:
    namespace = extracted_functions({"ask_support", "run_answer_ask"}, {"ASK_BRIDGE_DIR": None})
    assert namespace["ask_support"]() is None
    assert namespace["run_answer_ask"]("synthetic-session", {}) == ("notStarted", "")


def test_constructor_validates_features_before_constructing_services() -> None:
    module = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    server = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "PocketHTTPServer")
    constructor = next(node for node in server.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    first = constructor.body[0]
    assert isinstance(first, ast.Assign)
    assert isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "public_features"
