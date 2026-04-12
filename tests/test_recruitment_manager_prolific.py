from autora.experiment_runner.recruitment_manager import prolific as prolific_api


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
