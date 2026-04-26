import pytest

from autora.experiment_runner.recruitment_manager import prolific as prolific_api


def test_start_study_uses_start_transition(monkeypatch):
    captured = {}

    def fake_update(study_id, action, token):
        captured["study_id"] = study_id
        captured["action"] = action
        captured["token"] = token
        return {"ok": True}

    monkeypatch.setattr(prolific_api, "_update_study_status", fake_update)
    out = prolific_api.start_study("study-123", "TOKEN")

    assert out == {"ok": True}
    assert captured["study_id"] == "study-123"
    assert captured["action"] == "START"
    assert captured["token"] == "TOKEN"


def test_setup_study_uses_new_prolific_schema(monkeypatch):
    captured = {}

    def fake_post(url, headers, _json):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = _json
        return {"id": "study-123", "maximum_allowed_time": 45}

    monkeypatch.setattr(prolific_api, "__save_post", fake_post)
    monkeypatch.setattr(prolific_api, "_is_study_uncompleted", lambda *_, **__: False)
    monkeypatch.setattr(prolific_api, "_list_studies", lambda *_, **__: [])

    out = prolific_api.setup_study(
        name="autora-test",
        description="smoke",
        external_study_url="https://example.org/task",
        estimated_completion_time=3,
        prolific_token="TOKEN",
        total_available_places=2,
        completion_code="ABC123",
        check_prev=True,
    )

    assert out["id"] == "study-123"
    assert out["maximum_allowed_time"] == 45
    assert captured["url"].endswith("/api/v1/studies/")
    assert captured["headers"]["Authorization"] == "Token TOKEN"

    payload = captured["json"]
    assert "completion_codes" in payload
    assert "filters" in payload
    assert "completion_code" not in payload
    assert "completion_option" not in payload
    assert payload["completion_codes"][0]["code"] == "ABC123"
    assert payload["completion_codes"][0]["actions"][0]["action"] == "AUTOMATICALLY_APPROVE"
    assert any(f["filter_id"] == "age" for f in payload["filters"])
    assert any(
        f["filter_id"] == "current-country-of-residence" and f["selected_values"] == ["1"]
        for f in payload["filters"]
    )


@pytest.mark.parametrize(
    "studies, expected_blocked",
    [
        # PAUSED study (e.g. researcher manually stopped via the dashboard)
        # must NOT block a new same-named run. This was the regression: a
        # PAUSED study with 0 participants can never auto-flip to COMPLETED,
        # so the prior `status != 'COMPLETED'` predicate trapped setup_study
        # in its 10-cycle wait loop forever.
        ([{"id": "s1", "name": "autora", "status": "PAUSED"}], False),
        # AWAITING REVIEW: study filled, no longer recruiting -> non-blocking.
        ([{"id": "s1", "name": "autora", "status": "AWAITING REVIEW"}], False),
        # COMPLETED: settled -> non-blocking (historic happy path).
        ([{"id": "s1", "name": "autora", "status": "COMPLETED"}], False),
        # ACTIVE / STARTED: actively recruiting -> blocking.
        ([{"id": "s1", "name": "autora", "status": "ACTIVE"}], True),
        ([{"id": "s1", "name": "autora", "status": "STARTED"}], True),
        # UNPUBLISHED: a draft is not recruiting -> non-blocking. Critically,
        # orphaned drafts left over from a previous failed run (e.g. one
        # whose PUBLISH transition 400'd because the token's scope didn't
        # match the study's project, and which the current token can't
        # even DELETE) would otherwise block every subsequent run forever.
        ([{"id": "s1", "name": "autora", "status": "UNPUBLISHED"}], False),
        # Mix: a finished study + a paused one + nothing recruiting -> non-blocking.
        (
            [
                {"id": "s1", "name": "autora", "status": "COMPLETED"},
                {"id": "s1", "name": "autora", "status": "PAUSED"},
            ],
            False,
        ),
        # Mix: anything ACTIVE in the list still blocks.
        (
            [
                {"id": "s1", "name": "autora", "status": "COMPLETED"},
                {"id": "s1", "name": "autora", "status": "PAUSED"},
                {"id": "s1", "name": "autora", "status": "ACTIVE"},
            ],
            True,
        ),
        # Different name -> never blocks (isolation regression guard).
        ([{"id": "x", "name": "other-study", "status": "ACTIVE"}], False),
        # No studies at all -> obviously non-blocking.
        ([], False),
    ],
)
def test_is_study_uncompleted_only_blocks_on_active_or_draft(monkeypatch, studies, expected_blocked):
    """``_is_study_uncompleted`` should only block on states that would
    actively conflict with publishing a new same-named study.

    This pins the fix for a runner that hung indefinitely after the
    researcher manually stopped a Prolific study (which transitions to
    PAUSED, never COMPLETED) and tried to launch a new run with the
    same name.
    """
    monkeypatch.setattr(prolific_api, "_list_studies", lambda *_, **__: studies)
    assert prolific_api._is_study_uncompleted("autora", "TOKEN") is expected_blocked


def test_setup_study_forwards_project_id_when_provided(monkeypatch):
    """``setup_study(project_id=...)`` must include ``project_id`` in the
    create-study POST payload so Prolific routes the new study into the
    requested project. Without this, a project-scoped token (typical for
    lab accounts) lands its newly-created study in an orphan workspace
    and the runner can't publish it (Prolific returns
    ``error_code 140007`` "A Researcher is not allowed to publish a
    UNPUBLISHED study").
    """
    captured = {}

    def fake_post(url, headers, _json):
        captured["url"] = url
        captured["json"] = _json
        return {"id": "study-pid", "maximum_allowed_time": 45}

    monkeypatch.setattr(prolific_api, "__save_post", fake_post)
    monkeypatch.setattr(prolific_api, "_is_study_uncompleted", lambda *_, **__: False)
    monkeypatch.setattr(prolific_api, "_list_studies", lambda *_, **__: [])

    out = prolific_api.setup_study(
        name="autora-test",
        description="smoke",
        external_study_url="https://example.org/task",
        estimated_completion_time=3,
        prolific_token="TOKEN",
        total_available_places=2,
        completion_code="ABC123",
        check_prev=True,
        project_id="69e23f0d06df2b74aee4eff3",
    )

    assert out["id"] == "study-pid"
    # Field name is ``project`` (singular), NOT ``project_id``. Prolific's
    # create-study endpoint silently ignores ``project_id`` and routes to
    # the token's default workspace — verified empirically.
    assert captured["json"]["project"] == "69e23f0d06df2b74aee4eff3"
    assert "project_id" not in captured["json"]


def test_setup_study_omits_project_when_unset(monkeypatch):
    """Default behaviour (no ``project_id`` passed) must not put a stray
    ``project`` key in the payload — single-workspace setups should be
    completely unaffected by the new opt-in field.
    """
    captured = {}

    def fake_post(url, headers, _json):
        captured["json"] = _json
        return {"id": "study-noproj", "maximum_allowed_time": 30}

    monkeypatch.setattr(prolific_api, "__save_post", fake_post)
    monkeypatch.setattr(prolific_api, "_is_study_uncompleted", lambda *_, **__: False)
    monkeypatch.setattr(prolific_api, "_list_studies", lambda *_, **__: [])

    prolific_api.setup_study(
        name="autora-test",
        description="smoke",
        external_study_url="https://example.org/task",
        estimated_completion_time=3,
        prolific_token="TOKEN",
        total_available_places=2,
        completion_code="ABC123",
        check_prev=True,
    )

    assert "project" not in captured["json"]
    assert "project_id" not in captured["json"]


def test_list_studies_uses_project_scoped_endpoint_when_project_id_given(monkeypatch):
    """``_list_studies(project_id=...)`` must hit
    ``/api/v1/projects/<id>/studies/`` instead of the unscoped
    ``/api/v1/studies/`` endpoint. The unscoped one routinely times out
    on lab tokens (which see ~25 workspaces and thousands of studies);
    scoping to the project the runner is actually publishing into
    reduces the page size to well-under-a-second.
    """
    captured = {}

    def fake_paginate(url, headers):
        captured["url"] = url
        return [{"id": "s", "name": "autora", "status": "COMPLETED"}]

    monkeypatch.setattr(prolific_api, "__get_request_results_id", fake_paginate)

    out = prolific_api._list_studies("TOKEN", project_id="PROJ123")
    assert "/api/v1/projects/PROJ123/studies/" in captured["url"], captured["url"]
    assert out and out[0]["name"] == "autora"


def test_list_studies_falls_back_to_unscoped_endpoint_when_no_project_id(monkeypatch):
    """Default behaviour (no ``project_id``) must still use the unscoped
    ``/api/v1/studies/`` endpoint, so single-workspace tokens that don't
    pass ``project_id`` keep working unchanged.
    """
    captured = {}

    def fake_paginate(url, headers):
        captured["url"] = url
        return []

    monkeypatch.setattr(prolific_api, "__get_request_results_id", fake_paginate)

    prolific_api._list_studies("TOKEN")
    assert captured["url"].endswith("/api/v1/studies/"), captured["url"]


def test_is_study_uncompleted_forwards_project_id_to_list_studies(monkeypatch):
    """``_is_study_uncompleted(project_id=...)`` must thread the
    ``project_id`` through ``_studies_from_name`` -> ``_list_studies``
    so the same-name lookup is project-scoped (avoids the lab-token
    listing timeout).
    """
    captured = {}

    def fake_list(token, project_id=None):
        captured["project_id"] = project_id
        return []  # empty -> non-blocking

    monkeypatch.setattr(prolific_api, "_list_studies", fake_list)
    prolific_api._is_study_uncompleted("autora", "TOKEN", project_id="PROJ123")
    assert captured["project_id"] == "PROJ123"
