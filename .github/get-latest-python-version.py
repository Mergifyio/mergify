import json
import os
import re
import urllib.request


TOKEN = os.getenv("UPDATECLI_GITHUB_TOKEN")


def get_version() -> str:
    regex = re.compile(r'^LATEST_.*="([^"]*)"$', re.M)

    url_tags = urllib.request.Request(
        "https://api.github.com/repos/heroku/heroku-buildpack-python/git/matching-refs/tags",
        headers={"Authorization": f"token {TOKEN}"},
    )
    f = urllib.request.urlopen(url_tags)
    last_tag = json.load(f)[-1]["ref"][len("refs/tags/") :]

    url_versions = urllib.request.Request(
        f"https://raw.githubusercontent.com/heroku/heroku-buildpack-python/{last_tag}/bin/default_pythons",
        headers={"Authorization": f"token {TOKEN}"},
    )
    f = urllib.request.urlopen(url_versions)

    m = regex.search(f.read().decode())
    if m:
        version = m.group(1)[len("python-") :].strip()
        if version:
            return version

    raise RuntimeError("Failed to find lastest python version")


print(get_version())
