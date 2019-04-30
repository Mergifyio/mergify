# -*- encoding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import itertools
import operator
import pprint

import github

import requests

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import sub_utils
from mergify_engine import utils


def get_repositories_setuped(token, install_id):  # pragma: no cover
    repositories = []
    url = ("https://api.%s/user/installations/%s/repositories" %
           (config.GITHUB_DOMAIN, install_id))
    token = "token {}".format(token)
    session = requests.Session()
    while True:
        response = session.get(url, headers={
            "Authorization": token,
            "Accept": "application/vnd.github.machine-man-preview+json",
            "User-Agent": "PyGithub/Python"
        })
        if response.status_code == 200:
            repositories.extend(response.json()["repositories"])
            if "next" in response.links:
                url = response.links["next"]["url"]
                continue
            else:
                return repositories
        elif response.status_code == 403:
            raise github.BadCredentialsException(
                status=response.status_code,
                data=response.text
            )
        elif response.status_code == 404:
            raise github.UnknownObjectException(
                status=response.status_code,
                data=response.text
            )
        raise github.GithubException(
            status=response.status_code,
            data=response.text
        )


def create_jwt():
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    return integration.create_jwt()


def report(url):
    redis = utils.get_redis_for_cache()
    path = url.replace("https://github.com/", "")
    owner, repo, _, pull_number = path.split("/")

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    install_id = utils.get_installation_id(integration, owner)

    print("* INSTALLATION ID: %s" % install_id)

    cached_sub = sub_utils.get_subscription(redis, install_id)
    db_sub = sub_utils._retrieve_subscription_from_db(install_id)
    print("* SUBSCRIBED (cache/db): %s / %s" % (
        cached_sub["subscription_active"], db_sub["subscription_active"]))
    print("* SUB DETAIL: %s" % db_sub["subscription_reason"])

    print("* NUMBER OF CACHED TOKENS: %d" % len(cached_sub["tokens"]))

    try:
        exception = None
        for token in cached_sub["tokens"].items():
            try:
                repos = get_repositories_setuped(token, install_id)
            except github.BadCredentialsException as e:
                exception = e
                continue
            except github.GithubException.GithubException as e:
                if e.status == 401:
                    exception = e
                else:
                    raise
            else:
                break
        if exception:
            print("* MERGIFY DOESN'T HAVE ANY VALID OAUTH TOKENS")
    except github.UnknownObjectException:
        print("* MERGIFY SEEMS NOT INSTALLED")
    else:
        repos = [r for r in repos if r["full_name"] == owner + "/" + repo]
        if repos:
            print("* MERGIFY INSTALLED AND ENABLED ON THIS REPOSITORY")
        else:
            print("* MERGIFY INSTALLED AND DISABLED ON THIS REPOSITORY !!!")

    installation_token = integration.get_access_token(install_id).token

    g = github.Github(installation_token,
                      base_url="https://api.%s" % config.GITHUB_DOMAIN)
    r = g.get_repo(owner + "/" + repo)
    print("* REPOSITORY IS %s" % "PRIVATE" if r.private else "PUBLIC")
    p = r.get_pull(int(pull_number))

    print("* CONFIGURATION:")
    print(r.get_contents(".mergify.yml").decoded_content.decode())

    mp = mergify_pull.MergifyPull(g, p, install_id)
    print("* PULL REQUEST:")
    pprint.pprint(mp.to_dict(), width=160)
    try:
        print("is_behind: %s" % mp.is_behind())
    except github.GithubException as e:
        print("Unable to know if pull request branch is behind: %s" % e)

    print("mergeable_state: %s" % mp.g_pull.mergeable_state)

    print("* MERGIFY STATUSES:")
    commit = p.base.repo.get_commit(p.head.sha)
    for s in commit.get_combined_status().statuses:
        if s.context.startswith("mergify"):
            print("[%s]: %s" % (s.context, s.state))

    print("* MERGIFY CHECKS:")
    checks = list(check_api.get_checks(p))
    for c in checks:
        if c.name.startswith("Mergify"):
            print("[%s]: %s | %s" % (c.name, c.conclusion,
                                     c.output.get("title")))
            print("> " + "\n> ".join(c.output.get("summary").split("\n")))
    return g, p


def main():
    parser = argparse.ArgumentParser(
        description='Debugger for mergify'
    )
    parser.add_argument("url", help="Pull request url")
    args = parser.parse_args()
    report(args.url)


def stargazer():
    parser = argparse.ArgumentParser(
        description='Stargazers counter for mergify'
    )
    parser.add_argument("-n", "--number",
                        type=int, default=20,
                        help="Number of repo to show")
    args = parser.parse_args()

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    stars = []
    for installation in utils.get_installations(integration):
        try:
            _id = installation["id"]
            token = integration.get_access_token(_id).token
            g = github.Github(token, base_url="https://api.%s" %
                              config.GITHUB_DOMAIN)

            repositories = sorted(g.get_installation(_id).get_repos(),
                                  key=operator.attrgetter("private"))
            for private, repos in itertools.groupby(
                    repositories, key=operator.attrgetter("private")):

                for repo in repos:
                    try:
                        repo.get_contents(".mergify.yml")
                        stars.append((repo.stargazers_count, repo.full_name))
                    except github.GithubException as e:
                        if e.status >= 500:  # pragma: no cover
                            raise
        except github.GithubException as e:
            # Ignore rate limit/abuse
            if e.status != 403:
                raise

    for stars_count, repo in sorted(stars, reverse=True)[:args.number]:
        print("%s: %s", repo, stars_count)
