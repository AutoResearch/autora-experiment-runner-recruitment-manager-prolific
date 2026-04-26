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
    monkeypatch.setattr(prolific_api, "_is_study_uncompleted", lambda *_: False)
    monkeypatch.setattr(prolific_api, "_list_studies", lambda *_: [])

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
        # UNPUBLISHED draft could be published at any moment by the
        # researcher and would race the new study -> blocking.
        ([{"id": "s1", "name": "autora", "status": "UNPUBLISHED"}], True),
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
    monkeypatch.setattr(prolific_api, "_list_studies", lambda *_: studies)
    assert prolific_api._is_study_uncompleted("autora", "TOKEN") is expected_blocked
