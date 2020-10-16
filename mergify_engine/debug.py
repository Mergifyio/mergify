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
import datetime
import itertools
import pprint
import typing

from mergify_engine import config
from mergify_engine import context
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.actions.merge import helpers
from mergify_engine.actions.merge import queue
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.engine import actions_runner


def get_repositories_setuped(token: str, install_id: int) -> list:  # pragma: no cover
    repositories = []
    url = f"{config.GITHUB_API_URL}/user/installations/{install_id}/repositories"
    token = f"token {token}"
    session = http.Client()
    while True:
        response = session.get(
            url,
            headers={
                "Authorization": token,
                "Accept": "application/vnd.github.machine-man-preview+json",
                "User-Agent": "PyGithub/Python",
            },
        )
        if response.status_code == 200:
            repositories.extend(response.json()["repositories"])
            if "next" in response.links:
                url = response.links["next"]["url"]
                continue
            else:
                return repositories
        else:
            response.raise_for_status()


def report_sub(
    install_id: int,
    sub: subscription.Subscription,
    title: str,
    slug: typing.Optional[str] = None,
):
    print(f"* {title} SUB DETAIL: {sub.reason}")
    print(
        f"* {title} SUB NUMBER OF TOKENS: {len(sub.tokens)} ({', '.join(sub.tokens)})"
    )

    for login, token in sub.tokens.items():
        try:
            repos = get_repositories_setuped(token, install_id)
        except http.HTTPNotFound:
            print(f"* {title} SUB: MERGIFY SEEMS NOT INSTALLED")
            return
        except http.HTTPClientSideError as e:
            print(
                f"* {title} SUB: token for {login} is invalid "
                f"({e.status_code}: {e.message})"
            )
        else:
            if slug is not None:
                if any((r["full_name"] == slug) for r in repos):
                    print(
                        f"* {title} SUB: MERGIFY INSTALLED AND ENABLED ON THIS REPOSITORY"
                    )
                else:
                    print(
                        f"* {title} SUB: MERGIFY INSTALLED BUT DISABLED ON THIS REPOSITORY"
                    )
                break
    else:
        print(f"* {title} SUB: MERGIFY DOESN'T HAVE ANY VALID OAUTH TOKENS")


async def report_worker_status(owner: str) -> None:
    stream_name = f"stream~{owner}".encode()
    r = await utils.create_aredis_for_stream()
    streams = await r.zrangebyscore("streams", min=0, max="+inf", withscores=True)

    for pos, item in enumerate(streams):
        if item[0] == stream_name:
            break
    else:
        print("* WORKER: Installation not queued to process")
        return

    planned = datetime.datetime.utcfromtimestamp(streams[pos]).isoformat()

    attempts = await r.hget("attempts", stream_name) or 0
    print(
        "* WORKER: Installation queued, "
        f" pos: {pos}/{len(streams)},"
        f" next_run: {planned},"
        f" attempts: {attempts}"
    )

    size = await r.xlen(stream_name)
    print(f"* WORKER PENDING EVENTS for this installation: {size}")


def report(
    url: str,
) -> typing.Union[context.Context, github.GithubInstallationClient, None]:
    path = url.replace("https://github.com/", "")

    pull_number: typing.Optional[str]
    repo: typing.Optional[str]

    try:
        owner, repo, _, pull_number = path.split("/")
    except ValueError:
        pull_number = None
        try:
            owner, repo = path.split("/")
        except ValueError:
            owner = path
            repo = None

    try:
        client = github.get_client(owner)
    except exceptions.MergifyNotInstalled:
        print(f"* Mergify is not installed on account {owner}")
        return None

    print("* INSTALLATION ID: %s" % client.auth.installation["id"])

    cached_sub, db_sub = utils.async_run(
        subscription.Subscription.get_subscription(client.auth.owner_id),
        subscription.Subscription._retrieve_subscription_from_db(client.auth.owner_id),
    )

    if repo is None:
        slug = None
    else:
        slug = owner + "/" + repo

    print("* SUBSCRIBED (cache/db): %s / %s" % (cached_sub.active, db_sub.active))
    print("* Features (cache):")
    for f in cached_sub.features:
        print(f"  - {f.value}")
    report_sub(client.auth.installation["id"], cached_sub, "ENGINE-CACHE", slug)
    report_sub(client.auth.installation["id"], db_sub, "DASHBOARD", slug)

    utils.async_run(report_worker_status(client.auth.owner))

    if repo is not None:

        repo_info = client.item(f"/repos/{owner}/{repo}")
        print(f"* REPOSITORY IS {'PRIVATE' if repo_info['private'] else 'PUBLIC'}")

        print("* CONFIGURATION:")
        mergify_config = None
        try:
            filename, mergify_config_content = rules.get_mergify_config_content(
                client, repo
            )
        except rules.NoRules:  # pragma: no cover
            print(".mergify.yml is missing")
        else:
            print(f"Config filename: {filename}")
            print(mergify_config_content.decode())
            try:
                mergify_config = rules.UserConfigurationSchema(mergify_config_content)
            except rules.InvalidRules as e:  # pragma: no cover
                print("configuration is invalid %s" % str(e))
            else:
                mergify_config["pull_request_rules"].rules.extend(
                    engine.DEFAULT_PULL_REQUEST_RULES.rules
                )

        if pull_number is None:
            for branch in client.items(f"/repos/{owner}/{repo}/branches"):
                q = queue.Queue(
                    utils.get_redis_for_cache(),
                    client.auth.installation["id"],
                    client.auth.owner,
                    repo,
                    branch["name"],
                )
                pulls = q.get_pulls()
                if not pulls:
                    continue

                print(f"* QUEUES {branch['name']}:")

                for priority, grouped_pulls in itertools.groupby(
                    pulls, key=lambda v: q.get_config(v)["priority"]
                ):
                    try:
                        fancy_priority = helpers.PriorityAliases(priority).name
                    except ValueError:
                        fancy_priority = priority
                    formatted_pulls = ", ".join((f"#{p}" for p in grouped_pulls))
                    print(f"** {formatted_pulls} (priority: {fancy_priority})")
        else:
            pull_raw = client.item(f"/repos/{owner}/{repo}/pulls/{pull_number}")
            ctxt = context.Context(
                client,
                pull_raw,
                cached_sub,
                [{"event_type": "mergify-debugger", "data": {}}],
            )

            # FIXME queues could also be printed if no pull number given
            q = queue.Queue.from_context(ctxt)
            print("* QUEUES: %s" % ", ".join([f"#{p}" for p in q.get_pulls()]))
            print("* PULL REQUEST:")
            pr_data = dict(ctxt.pull_request.items())
            pprint.pprint(pr_data, width=160)

            print("is_behind: %s" % ctxt.is_behind)

            print("mergeable_state: %s" % ctxt.pull["mergeable_state"])

            print("* MERGIFY LAST CHECKS:")
            for c in ctxt.pull_engine_check_runs:
                print(
                    "[%s]: %s | %s"
                    % (c["name"], c["conclusion"], c["output"].get("title"))
                )
                print("> " + "\n> ".join(c["output"].get("summary").split("\n")))

            if mergify_config is not None:
                print("* MERGIFY LIVE MATCHES:")
                match = mergify_config["pull_request_rules"].get_pull_request_rule(ctxt)
                summary_title, summary = actions_runner.gen_summary(ctxt, match)
                print("> %s" % summary_title)
                print(summary)

            return ctxt

    return client


def main() -> None:
    parser = argparse.ArgumentParser(description="Debugger for mergify")
    parser.add_argument("url", help="Pull request url")
    args = parser.parse_args()
    report(args.url)
