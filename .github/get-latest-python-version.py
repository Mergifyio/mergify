#!/usr/bin/env python3

import json
import re
import urllib.request as request


def get_latest_version_number() -> str:
    tags_request = request.urlopen(
        "https://hub.docker.com/v2/repositories/library/python/tags/?name=-slim&page_size=50"
    )
    json_tags = json.load(tags_request)["results"]
    json_tags = [
        tag["name"].replace("-slim", "")
        for tag in json_tags
        if re.fullmatch(r"^\d+\.\d+\.\d+-slim", tag["name"])
    ]
    json_tags.sort(key=lambda s: list(map(int, s.split("."))))
    return json_tags[-1]


if __name__ == "__main__":
    print(get_latest_version_number())
