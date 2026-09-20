"""HTTP contract tests: authentication, authorization, tenancy and job flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


@pytest.fixture(scope="module")
def client(request):
    from app.main import create_app
    from app.seed.demo import seed_if_empty

    # Seeding is disabled in the test environment, so the demo tenant the HTTP
    # tests authenticate against is created explicitly here.
    seed_if_empty()
    application = create_app()
    # The lifespan starts background workers; tests drive the queue directly so
    # the assertions stay deterministic.
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def tokens(client):
    out = {}
    for email in (
        "engineer@example.com",
        "manufacturing@example.com",
        "cam@example.com",
        "estimator@example.com",
        "approver@example.com",
        "admin@example.com",
    ):
        response = client.post("/v1/auth/token", json={"email": email, "password": "Pilot2026!"})
        assert response.status_code == 200, response.text
        out[email.split("@")[0]] = response.json()["access_token"]
    return out


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_and_root_are_public(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "openapi" in client.get("/").json()


def test_openapi_documents_every_router(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for route in (
        "/v1/projects",
        "/v1/geometry-jobs",
        "/v1/plans:generate",
        "/v1/simulations",
        "/v1/nc-programs:postprocess",
        "/v1/releases",
        "/v1/machine-runs",
        "/v1/events/stream",
    ):
        assert route in paths, f"{route} is missing from the published contract"
    tags = {t["name"] for t in schema["openapi_tags"]} if "openapi_tags" in schema else {t["name"] for t in schema.get("tags", [])}
    assert {"auth", "projects", "geometry", "planning", "verification", "production"} <= tags


def test_a_request_without_a_token_is_rejected(client):
    response = client.get("/v1/projects")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_forged_token_is_rejected(client):
    response = client.get("/v1/projects", headers=auth("not-a-real-token"))
    assert response.status_code == 401


def test_every_response_carries_a_trace_id(client, tokens):
    response = client.get("/v1/projects", headers=auth(tokens["engineer"]))
    assert response.status_code == 200
    assert response.headers.get("X-Trace-Id")


def test_permissions_are_enforced_per_action(client, tokens):
    """An estimator may read a project but not create one."""
    response = client.post(
        "/v1/projects",
        headers=auth(tokens["estimator"]),
        json={"part_number": "DENIED-1", "name": "Should not be created"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "permission_denied"
    assert "project:create" in body["detail"]["missing"]

    assert client.get("/v1/projects", headers=auth(tokens["estimator"])).status_code == 200


def test_release_signing_requires_the_release_role(client, tokens):
    response = client.post(
        "/v1/releases/does-not-exist:approve",
        headers=auth(tokens["cam"]),
        json={"totp_code": "123456", "statement": "attempting to sign without authority", "checklist": {"a": True}},
    )
    assert response.status_code == 403
    assert "nc:release" in response.json()["detail"]["missing"]


def test_project_lifecycle_and_state_machine(client, tokens):
    created = client.post(
        "/v1/projects",
        headers=auth(tokens["engineer"]),
        json={"part_number": "API-1001", "name": "API test bracket", "quantity": 5, "target_material_code": "AL6082-T6"},
    )
    assert created.status_code == 201
    project = created.json()
    assert project["state"] == "Draft"
    assert project["version"] == 1

    # A skipped transition is refused with the allowed set.
    bad = client.post(
        f"/v1/projects/{project['id']}/transitions",
        headers=auth(tokens["engineer"]),
        json={"target_state": "Released"},
    )
    assert bad.status_code == 409
    assert bad.json()["code"] == "invalid_transition"
    assert "Evidence collection" in bad.json()["detail"]["allowed"]

    good = client.post(
        f"/v1/projects/{project['id']}/transitions",
        headers=auth(tokens["engineer"]),
        json={"target_state": "Evidence collection", "reason": "Starting evidence collection"},
    )
    assert good.status_code == 200
    assert good.json()["state"] == "Evidence collection"


def test_optimistic_concurrency_rejects_a_stale_write(client, tokens):
    project = client.post(
        "/v1/projects",
        headers=auth(tokens["engineer"]),
        json={"part_number": "API-1002", "name": "Concurrency test"},
    ).json()

    first = client.patch(
        f"/v1/projects/{project['id']}",
        headers=auth(tokens["engineer"]),
        json={"name": "Renamed once", "expected_version": project["version"]},
    )
    assert first.status_code == 200

    stale = client.patch(
        f"/v1/projects/{project['id']}",
        headers=auth(tokens["engineer"]),
        json={"name": "Renamed from a stale copy", "expected_version": project["version"]},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"


def test_upload_parses_the_artifact_and_queues_a_job(client, tokens):
    project = client.post(
        "/v1/projects",
        headers=auth(tokens["engineer"]),
        json={"part_number": "API-1003", "name": "Upload test"},
    ).json()

    initiate = client.post(
        f"/v1/projects/{project['id']}/artifacts:initiate",
        headers=auth(tokens["engineer"]),
        json={"filename": "BRK-1042.step", "byte_size": 4096},
    )
    assert initiate.status_code == 200
    assert initiate.json()["upload_url"].endswith(f"/projects/{project['id']}/artifacts")

    with (SAMPLES / "drawings" / "BRK-1042.pdf").open("rb") as handle:
        upload = client.post(
            f"/v1/projects/{project['id']}/artifacts",
            headers=auth(tokens["engineer"]),
            files={"file": ("BRK-1042.pdf", handle, "application/pdf")},
        )
    assert upload.status_code == 201
    artifact = upload.json()
    assert artifact["kind"] == "drawing_pdf"
    assert artifact["authority"] == "Drawing"
    assert artifact["status"] == "Processed"
    assert len(artifact["content_hash"]) == 64
    assert artifact["extracted"]["general_tolerance"] == "ISO 2768-m"

    jobs = client.get(f"/v1/jobs?project_id={project['id']}", headers=auth(tokens["engineer"])).json()
    assert any(j["kind"] == "artifact_parse" for j in jobs)

    content = client.get(
        f"/v1/projects/{project['id']}/artifacts/{artifact['id']}/content", headers=auth(tokens["engineer"])
    )
    assert content.status_code == 200
    assert content.headers["X-Content-Hash"] == artifact["content_hash"]


def test_authority_override_requires_a_reason(client, tokens):
    project = client.post(
        "/v1/projects", headers=auth(tokens["engineer"]), json={"part_number": "API-1004", "name": "Authority test"}
    ).json()
    with (SAMPLES / "cad" / "BRK-1042.step").open("rb") as handle:
        artifact = client.post(
            f"/v1/projects/{project['id']}/artifacts",
            headers=auth(tokens["engineer"]),
            files={"file": ("BRK-1042.step", handle, "application/step")},
        ).json()

    assert artifact["authority"] == "Scan"
    blank = client.patch(
        f"/v1/projects/{project['id']}/artifacts/{artifact['id']}/authority",
        headers=auth(tokens["engineer"]),
        json={"authority": "Verified CAD and PMI", "reason": ""},
    )
    assert blank.status_code == 422

    promoted = client.patch(
        f"/v1/projects/{project['id']}/artifacts/{artifact['id']}/authority",
        headers=auth(tokens["engineer"]),
        json={"authority": "Verified CAD and PMI", "reason": "Released model confirmed against the PDM record"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["authority"] == "Verified CAD and PMI"
    assert promoted.json()["authority_overridden"] is True


def test_conflicting_observations_surface_through_the_api(client, tokens):
    project = client.post(
        "/v1/projects", headers=auth(tokens["engineer"]), json={"part_number": "API-1005", "name": "Conflict test"}
    ).json()
    headers = auth(tokens["engineer"])

    client.post(
        f"/v1/projects/{project['id']}/observations",
        headers=headers,
        json={"attribute": "diameter", "feature_key": "H1", "value": 12.0, "authority": "Drawing",
              "confidence": 0.9, "uncertainty": 0.02, "status": "Verification Required", "criticality": "Function critical"},
    )
    client.post(
        f"/v1/projects/{project['id']}/observations",
        headers=headers,
        json={"attribute": "diameter", "feature_key": "H1", "value": 11.6, "authority": "Ordinary photograph",
              "confidence": 0.6, "uncertainty": 0.15, "status": "Inferred", "criticality": "Function critical"},
    )

    conflicts = client.get(f"/v1/projects/{project['id']}/conflicts", headers=headers).json()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["resolved"] is False
    assert conflict["severity"] == "S2"

    resolved = client.post(
        f"/v1/projects/{project['id']}/conflicts/{conflict['id']}/resolve",
        headers=headers,
        json={"chosen_observation_id": conflict["authoritative_observation_id"], "reason": "Drawing is the controlled source"},
    )
    assert resolved.status_code == 200
    assert client.get(f"/v1/projects/{project['id']}/conflicts", headers=headers).json() == []


def test_master_data_is_versioned_not_edited_in_place(client, tokens):
    headers = auth(tokens["admin"])
    before = client.get("/v1/factory/fixtures", headers=headers).json()
    code = "API-FIXTURE"

    first = client.post(
        "/v1/factory/fixtures",
        headers=headers,
        json={"code": code, "name": "First revision", "jaw_opening_mm": 150.0, "verified": True},
    )
    assert first.status_code == 201
    assert first.json()["revision"] == 1

    second = client.post(
        "/v1/factory/fixtures",
        headers=headers,
        json={"code": code, "name": "Second revision with taller jaws", "jaw_opening_mm": 180.0, "verified": True},
    )
    assert second.status_code == 201
    assert second.json()["revision"] == 2

    current = client.get("/v1/factory/fixtures", headers=headers).json()
    matching = [f for f in current if f["code"] == code]
    assert len(matching) == 1  # revision 1 is superseded, not deleted
    assert matching[0]["revision"] == 2

    with_history = client.get("/v1/factory/fixtures?include_superseded=true", headers=headers).json()
    assert len([f for f in with_history if f["code"] == code]) == 2
    assert len(before) < len(current)


def test_master_data_changes_need_the_permission(client, tokens):
    response = client.post(
        "/v1/factory/machines", headers=auth(tokens["estimator"]), json={"code": "NOPE", "name": "Unauthorised"}
    )
    assert response.status_code == 403


def test_event_topics_match_the_published_contract(client, tokens):
    topics = {t["topic"] for t in client.get("/v1/events/topics", headers=auth(tokens["engineer"])).json()}
    assert {
        "artifact.processed",
        "geometry.version.created",
        "engineering.conflict.detected",
        "plan.candidate.created",
        "simulation.completed",
        "release.status.changed",
        "machine.run.completed",
        "learning.rule.proposed",
    } <= topics


def test_events_are_recorded_for_the_tenant(client, tokens):
    events = client.get("/v1/events?limit=200", headers=auth(tokens["engineer"])).json()
    assert events
    assert {e["topic"] for e in events} & {"artifact.processed", "engineering.conflict.detected"}
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)


def test_a_missing_object_reports_not_found_without_confirming_it_exists(client, tokens):
    response = client.get("/v1/plans/00000000000000000000000000000000", headers=auth(tokens["engineer"]))
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_mfa_enrolment_returns_a_usable_factor(client, tokens):
    response = client.post("/v1/auth/mfa/enrol", headers=auth(tokens["approver"]))
    assert response.status_code == 200
    body = response.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/")

    from app.core.security import totp_now, verify_totp

    assert verify_totp(body["secret"], totp_now(body["secret"])) is True
    assert verify_totp(body["secret"], "000000") is False


def test_cors_exposes_the_download_headers(client):
    # A browser on another origin (GitHub Pages, a static site) can only read a
    # response header the API exposes; without Content-Disposition every
    # controlled download would save under a generic name.
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})
    exposed = {h.strip().lower() for h in response.headers.get("access-control-expose-headers", "").split(",")}
    assert {"content-disposition", "x-controlled", "x-package-hash", "x-trace-id"} <= exposed
