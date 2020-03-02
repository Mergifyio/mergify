from mergify_engine import SINGLE_PULL_API_RE


def test_simple_pull_re():
    assert not SINGLE_PULL_API_RE.match("https://foorbar:443/")
    assert not SINGLE_PULL_API_RE.match("https://api.github.com:443/orgs/foobar")
    assert not SINGLE_PULL_API_RE.match("https://api.github.com:443/repos/foo/bar")
    assert not SINGLE_PULL_API_RE.match(
        "https://api.github.com:443/repos/foo/bar/pulls"
    )
    assert (
        SINGLE_PULL_API_RE.match("https://api.github.com:443/repos/foo/bar/pulls/1")
        is not None
    )
    assert (
        SINGLE_PULL_API_RE.match("https://api.github.com:443/repos/foo/bar/pulls/123")
        is not None
    )
    assert not SINGLE_PULL_API_RE.match(
        "https://api.github.com:443/repos/foo/bar/pulls/abc"
    )
    assert not SINGLE_PULL_API_RE.match(
        "https://api.github.com:443/repos/foo/bar/pulls/123/commits"
    )
    assert not SINGLE_PULL_API_RE.match(
        "https://api.github.com:443/repos/foo/bar/pulls/123/commits/1"
    )
