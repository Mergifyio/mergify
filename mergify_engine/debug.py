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

from mergify_engine import config
from mergify_engine import context
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import rules
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.actions.merge import helpers
from mergify_engine.actions.merge import queue
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.engine import actions_runner


def get_repositories_setuped(token, install_id):  # pragma: no cover
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


def report_sub(install_id, slug, sub, title):
    print(f"* {title} SUB DETAIL: {sub['subscription_reason']}")
    print(
        f"* {title} SUB NUMBER OF TOKENS: {len(sub['tokens'])} ({', '.join(sub['tokens'])})"
    )

    for login, token in sub["tokens"].items():
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


async def report_worker_status(owner):
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


def report(url):
    path = url.replace("https://github.com/", "")
    try:
        owner, repo, _, pull_number = path.split("/")
    except ValueError:
        pull_number = None
        try:
            owner, repo = path.split("/")
        except ValueError:
            print(f"Wrong URL: {url}")
            return

    slug = owner + "/" + repo

    try:
        client = github.get_client(owner, repo)
    except exceptions.MergifyNotInstalled:
        print("* Mergify is not installed there")
        return

    print("* INSTALLATION ID: %s" % client.auth.installation["id"])

    cached_sub, db_sub = utils.async_run(
        sub_utils.get_subscription(client.auth.owner_id),
        sub_utils._retrieve_subscription_from_db(client.auth.owner_id),
    )
    print(
        "* SUBSCRIBED (cache/db): %s / %s"
        % (cached_sub["subscription_active"], db_sub["subscription_active"])
    )
    report_sub(client.auth.installation["id"], slug, cached_sub, "ENGINE-CACHE")
    report_sub(client.auth.installation["id"], slug, db_sub, "DASHBOARD")

    repo = client.item(f"/repos/{owner}/{repo}")
    print(f"* REPOSITORY IS {'PRIVATE' if repo['private'] else 'PUBLIC'}")

    utils.async_run(report_worker_status(client.auth.owner))

    if pull_number:
        pull_raw = client.item(f"pulls/{pull_number}")
        ctxt = context.Context(
            client,
            pull_raw,
            cached_sub,
            [{"event_type": "mergify-debugger", "data": {}}],
        )

        q = queue.Queue.from_context(ctxt)
        print("* QUEUES: %s" % ", ".join([f"#{p}" for p in q.get_pulls()]))

    else:
        for branch in client.items("branches"):
            q = queue.Queue(
                utils.get_redis_for_cache(),
                client.auth.installation["id"],
                client.auth.owner,
                client.auth.repo,
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

    print("* CONFIGURATION:")
    mergify_config = None
    try:
        filename, mergify_config_content = rules.get_mergify_config_content(client)
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

    if pull_number:
        print("* PULL REQUEST:")
        pr_data = dict(ctxt.pull_request.items())
        pprint.pprint(pr_data, width=160)

        print("is_behind: %s" % ctxt.is_behind)

        print("mergeable_state: %s" % ctxt.pull["mergeable_state"])

        print("* MERGIFY LAST CHECKS:")
        for c in ctxt.pull_engine_check_runs:
            print(
                "[%s]: %s | %s" % (c["name"], c["conclusion"], c["output"].get("title"))
            )
            print("> " + "\n> ".join(c["output"].get("summary").split("\n")))

        if mergify_config is not None:
            print("* MERGIFY LIVE MATCHES:")
            match = mergify_config["pull_request_rules"].get_pull_request_rule(ctxt)
            summary_title, summary = actions_runner.gen_summary(ctxt, match)
            print("> %s" % summary_title)
            print(summary)

        return ctxt
    else:
        return client


def main():
    parser = argparse.ArgumentParser(description="Debugger for mergify")
    parser.add_argument("url", help="Pull request url")
    args = parser.parse_args()
    report(args.url)
