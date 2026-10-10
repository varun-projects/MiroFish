from datetime import datetime
from types import SimpleNamespace

from app import create_app
from app.api import graph as graph_api
from app.models.project import Project, ProjectStatus


class ProviderError(RuntimeError):
    """A provider-side failure shaped like the SDK errors MiroFish calls."""

    def __init__(self, status_code=None, request_id=None):
        super().__init__("SECRET-PROVIDER-BODY")
        self.status_code = status_code
        self.request_id = request_id
        self.body = {
            "type": "about:blank",
            "title": "Gone",
            "status": status_code,
            "detail": "SECRET-PROVIDER-BODY",
        }


def _client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def _project(status=ProjectStatus.ONTOLOGY_GENERATED):
    now = datetime.now().isoformat()
    return Project(
        project_id="proj-1",
        name="Project",
        status=status,
        created_at=now,
        updated_at=now,
        ontology={"entity_types": [], "edge_types": []},
        graph_id="graph-1",
        graph_build_task_id="task-1",
        zep_batch_id="batch-1",
        zep_batch_operation_id="operation-1",
    )


def _install_failing_builder(monkeypatch, error):
    class Builder:
        def __init__(self, **_kwargs):
            pass

        def validate_batch_chunks(self, _chunks, batch_size=350):
            return None

        def get_graph_data(self, _graph_id):
            raise error

        def delete_graph(self, _graph_id):
            raise error

        def get_batch_summary(self, _batch_id):
            raise error

        def create_graph(self, **_kwargs):
            raise error

    monkeypatch.setattr(graph_api.Config, "ZEP_API_KEY", "test-key")
    monkeypatch.setattr(graph_api, "GraphBuilderService", Builder)


def test_graph_data_maps_a_retired_model_to_502(monkeypatch):
    _install_failing_builder(
        monkeypatch, ProviderError(status_code=410, request_id="req-gone")
    )

    response = _client().get("/api/graph/data/graph-1")

    assert response.status_code == 502
    assert response.json["success"] is False
    assert "HTTP 410" in response.json["error"]
    assert "req-gone" in response.json["error"]
    assert "traceback" not in response.json
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "SECRET-PROVIDER-BODY" not in body


def test_graph_data_maps_a_rate_limit_to_502(monkeypatch):
    _install_failing_builder(monkeypatch, ProviderError(status_code=429))

    response = _client().get("/api/graph/data/graph-1")

    assert response.status_code == 502
    assert "HTTP 429" in response.json["error"]
    assert "traceback" not in response.json
    assert "Traceback" not in response.get_data(as_text=True)


def test_graph_data_maps_an_unexpected_error_to_500(monkeypatch):
    _install_failing_builder(monkeypatch, RuntimeError("postgres://user:hunter2@db"))

    response = _client().get("/api/graph/data/graph-1")

    assert response.status_code == 500
    assert response.json["error"] == "Graph data request failed; check the server logs"
    assert "traceback" not in response.json
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "hunter2" not in body


def test_provider_request_id_is_sanitized_and_truncated(monkeypatch):
    hostile = 'req-1\n<script>alert("x")</script>'
    _install_failing_builder(
        monkeypatch, ProviderError(status_code=410, request_id=hostile)
    )
    client = _client()

    response = client.get("/api/graph/data/graph-1")

    assert response.status_code == 502
    error = response.json["error"]
    assert "request_id: req-1scriptalertxscript)" in error
    assert "<" not in error
    assert ">" not in error
    assert "\n" not in error

    _install_failing_builder(
        monkeypatch, ProviderError(status_code=410, request_id="a" * 200)
    )
    response = client.get("/api/graph/data/graph-1")

    assert "a" * 128 in response.json["error"]
    assert "a" * 129 not in response.json["error"]


def test_graph_delete_maps_a_provider_failure_to_502(monkeypatch):
    project = _project(ProjectStatus.GRAPH_COMPLETED)
    _install_failing_builder(
        monkeypatch, ProviderError(status_code=503, request_id="req-delete")
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "find_projects_by_graph_id",
        classmethod(lambda _cls, _graph_id: [project]),
    )
    monkeypatch.setattr(
        graph_api.ZepGraphMemoryManager,
        "get_simulation_ids_for_graph",
        classmethod(lambda _cls, _graph_id: []),
    )
    monkeypatch.setattr(
        graph_api,
        "SimulationManager",
        lambda: SimpleNamespace(list_simulations=lambda: []),
    )

    response = _client().delete("/api/graph/delete/graph-1")

    assert response.status_code == 502
    assert "HTTP 503" in response.json["error"]
    assert "req-delete" in response.json["error"]
    assert "traceback" not in response.json
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "SECRET-PROVIDER-BODY" not in body


def test_build_request_maps_a_provider_failure_to_502(monkeypatch):
    project = _project(ProjectStatus.GRAPH_BUILDING)
    _install_failing_builder(
        monkeypatch, ProviderError(status_code=410, request_id="req-batch")
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )
    monkeypatch.setattr(
        graph_api,
        "TaskManager",
        lambda: SimpleNamespace(get_task=lambda _task_id: None),
    )

    response = _client().post("/api/graph/build", json={"project_id": "proj-1"})

    assert response.status_code == 502
    assert "HTTP 410" in response.json["error"]
    assert "req-batch" in response.json["error"]
    assert "traceback" not in response.json
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "SECRET-PROVIDER-BODY" not in body


def test_build_request_maps_an_unexpected_error_to_500(monkeypatch):
    project = _project(ProjectStatus.GRAPH_BUILDING)
    _install_failing_builder(monkeypatch, RuntimeError("ZEP_API_KEY=hunter2"))
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )
    monkeypatch.setattr(
        graph_api,
        "TaskManager",
        lambda: SimpleNamespace(get_task=lambda _task_id: None),
    )

    response = _client().post("/api/graph/build", json={"project_id": "proj-1"})

    assert response.status_code == 500
    assert response.json["error"] == "Graph build request failed; check the server logs"
    assert "traceback" not in response.json
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "hunter2" not in body


def test_background_build_failure_reports_a_safe_task_error(monkeypatch):
    project = _project(ProjectStatus.ONTOLOGY_GENERATED)
    build_targets = []
    client = _client()

    class Thread:
        def __init__(self, *, target, daemon):
            build_targets.append(target)

        def start(self):
            pass

    _install_failing_builder(
        monkeypatch, ProviderError(status_code=410, request_id="req-gone")
    )
    monkeypatch.setattr(graph_api.threading, "Thread", Thread)
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_extracted_text",
        classmethod(lambda _cls, _project_id: "source text"),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )

    response = client.post("/api/graph/build", json={"project_id": "proj-1"})
    assert response.status_code == 200
    task_id = response.json["data"]["task_id"]

    assert len(build_targets) == 1
    build_targets[0]()

    task = client.get(f"/api/graph/task/{task_id}")

    assert task.status_code == 200
    assert task.json["data"]["status"] == "failed"
    assert task.json["data"]["error"] == (
        "Provider request failed (HTTP 410) (request_id: req-gone)"
    )
    assert project.status == ProjectStatus.FAILED
    assert project.error == task.json["data"]["error"]
    body = task.get_data(as_text=True)
    assert "Traceback" not in body
    assert "SECRET-PROVIDER-BODY" not in body


def test_background_build_keeps_an_unexpected_error_out_of_the_task(monkeypatch):
    project = _project(ProjectStatus.ONTOLOGY_GENERATED)
    build_targets = []
    client = _client()

    class Thread:
        def __init__(self, *, target, daemon):
            build_targets.append(target)

        def start(self):
            pass

    _install_failing_builder(monkeypatch, RuntimeError("ZEP_API_KEY=hunter2"))
    monkeypatch.setattr(graph_api.threading, "Thread", Thread)
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_project",
        classmethod(lambda _cls, _project_id: project),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "get_extracted_text",
        classmethod(lambda _cls, _project_id: "source text"),
    )
    monkeypatch.setattr(
        graph_api.ProjectManager,
        "save_project",
        classmethod(lambda _cls, _project: None),
    )

    response = client.post("/api/graph/build", json={"project_id": "proj-1"})
    task_id = response.json["data"]["task_id"]

    assert len(build_targets) == 1
    build_targets[0]()

    task = client.get(f"/api/graph/task/{task_id}")

    assert task.json["data"]["error"] == "Graph build failed; check the server logs"
    body = task.get_data(as_text=True)
    assert "Traceback" not in body
    assert "hunter2" not in body
