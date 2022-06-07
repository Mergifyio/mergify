import pytest

from mergify_engine import config


def test_legacy_api_url(
    original_environment_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://api.github.com"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://api.github.com/graphql"

    monkeypatch.setenv(
        "MERGIFYENGINE_GITHUB_API_URL", "https://onprem.example.com/api/v3/"
    )
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://onprem.example.com/api/v3"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://onprem.example.com/api/graphql"

    monkeypatch.setenv(
        "MERGIFYENGINE_GITHUB_API_URL", "https://onprem.example.com/api/v3"
    )
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://onprem.example.com/api/v3"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://onprem.example.com/api/graphql"

    # Not a valid GHES url, just ignore it...
    monkeypatch.setenv("MERGIFYENGINE_GITHUB_API_URL", "https://onprem.example.com/")
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://api.github.com"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://api.github.com/graphql"
