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
import asyncio
import datetime
import itertools
import pprint
import typing
import urllib

import daiquiri

from mergify_engine import config
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine import redis_utils
from mergify_engine import rules
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens
from mergify_engine.engine import actions_runner
from mergify_engine.queue import merge_train


LOG = daiquiri.getLogger(__name__)


async def get_repositories_setuped(
    token: str, install_id: int
) -> typing.List[github_types.GitHubRepository]:  # pragma: no cover
    repositories = []
    url = f"{config.GITHUB_REST_API_URL}/user/installations/{install_id}/repositories"
    token = f"token {token}"
    async with http.AsyncClient(
        headers={
            "Authorization": token,
            "Accept": "application/vnd.github.machine-man-preview+json",
        },
    ) as session:
        while True:
            response = await session.get(url)
            if response.status_code == 200:
                repositories.extend(response.json()["repositories"])
                if "next" in response.links:
                    url = response.links["next"]["url"]
                    continue
                else:
                    return repositories
            else:
                response.raise_for_status()


async def report_dashboard_synchro(
    install_id: int,
    sub: subscription.Subscription,
    uts: user_tokens.UserTokens,
    title: str,
    slug: typing.Optional[str] = None,
) -> None:
    print(f"* {title} SUB DETAIL: {sub.reason}")
    print(
        f"* {title} SUB NUMBER OF TOKENS: {len(uts.users)} ({', '.join(u['login'] for u in uts.users)})"
    )


async def report_worker_status(
    redis_links: redis_utils.RedisLinks, owner: github_types.GitHubLogin
) -> None:
    stream_name = f"stream~{owner}".encode()
    streams: typing.List[
        typing.Tuple[bytes, float]
    ] = await redis_links.stream.zrangebyscore(
        "streams", min=0, max="+inf", withscores=True
    )

    for pos, item in enumerate(streams):  # noqa: B007
        if item[0] == stream_name:
            break
    else:
        print("* WORKER: Installation not queued to process")
        return

    planned = datetime.datetime.utcfromtimestamp(streams[pos][1]).isoformat()

    attempts_raw = await redis_links.stream.hget("attempts", stream_name)
    if attempts_raw is None:
        attempts = 0
    else:
        attempts = int(attempts)
    print(
        "* WORKER: Installation queued, "
        f" pos: {pos}/{len(streams)},"
        f" next_run: {planned},"
        f" attempts: {attempts}"
    )

    size = await redis_links.stream.xlen(stream_name)
    print(f"* WORKER PENDING EVENTS for this installation: {size}")


async def report_queue(title: str, q: merge_train.Train) -> None:
    pulls = await q.get_pulls()
    if not pulls:
        return

    print(f"* {title} {q.ref}")

    async def _get_config(
        p: github_types.GitHubPullRequestNumber,
    ) -> typing.Tuple[github_types.GitHubPullRequestNumber, int]:
        return p, (await q.get_config(p))["priority"]

    pulls_priorities: typing.Dict[github_types.GitHubPullRequestNumber, int] = dict(
        await asyncio.gather(*(_get_config(p) for p in pulls))
    )

    for priority, grouped_pulls in itertools.groupby(
        pulls, key=lambda p: pulls_priorities[p]
    ):
        try:
            fancy_priority = queue.PriorityAliases(priority).name
        except ValueError:
            fancy_priority = str(priority)
        formatted_pulls = ", ".join((f"#{p}" for p in grouped_pulls))
        print(f"** {formatted_pulls} (priority: {fancy_priority})")


def _url_parser(
    url: str,
) -> typing.Tuple[
    github_types.GitHubLogin,
    typing.Optional[github_types.GitHubRepositoryName],
    typing.Optional[github_types.GitHubPullRequestNumber],
]:

    path = [el for el in urllib.parse.urlparse(url).path.split("/") if el != ""]

    pull_number: typing.Optional[str]
    repo: typing.Optional[str]

    try:
        owner, repo, _, pull_number = path
    except ValueError:
        pull_number = None
        try:
            owner, repo = path
        except ValueError:
            if len(path) == 1:
                owner = path[0]
                repo = None
            else:
                raise ValueError

    return (
        github_types.GitHubLogin(owner),
        None if repo is None else github_types.GitHubRepositoryName(repo),
        None
        if pull_number is None
        else github_types.GitHubPullRequestNumber(int(pull_number)),
    )


async def report(
    url: str,
) -> typing.Union[context.Context, github.AsyncGithubInstallationClient, None]:
    redis_links = redis_utils.RedisLinks(name="debug")

    try:
        owner_login, repo, pull_number = _url_parser(url)
    except ValueError:
        print(f"{url} is not valid")
        return None

    try:
        installation_json = await github.get_installation_from_login(owner_login)
        client = github.aget_client(installation_json)
    except exceptions.MergifyNotInstalled:
        print(f"* Mergify is not installed on account {owner_login}")
        return None

    # Do a dumb request just to authenticate
    await client.get("/")

    print(f"* INSTALLATION ID: {installation_json['id']}")

    if repo is None:
        slug = None
    else:
        slug = owner_login + "/" + repo

    owner_id = installation_json["account"]["id"]
    cached_sub = await subscription.Subscription.get_subscription(
        redis_links.cache, owner_id
    )
    db_sub = await subscription.Subscription._retrieve_subscription_from_db(
        redis_links.cache, owner_id
    )

    cached_tokens = await user_tokens.UserTokens.get(redis_links.cache, owner_id)
    if config.SAAS_MODE:
        db_tokens = typing.cast(
            user_tokens.UserTokens,
            (
                await user_tokens.UserTokensSaas._retrieve_from_db(
                    redis_links.cache, owner_id
                )
            ),
        )
    else:
        db_tokens = cached_tokens

    print("* Features (db):")
    for v in sorted(f.value for f in db_sub.features):
        print(f"  - {v}")
    print("* Features (cache):")
    for v in sorted(f.value for f in cached_sub.features):
        print(f"  - {v}")

    installation = context.Installation(
        installation_json, cached_sub, client, redis_links
    )

    await report_dashboard_synchro(
        installation.installation["id"], cached_sub, cached_tokens, "ENGINE-CACHE", slug
    )
    await report_dashboard_synchro(
        installation.installation["id"], db_sub, db_tokens, "DASHBOARD", slug
    )

    await report_worker_status(redis_links, owner_login)

    if repo is not None:
        repository = await installation.get_repository_by_name(repo)

        print(
            f"* REPOSITORY IS {'PRIVATE' if repository.repo['private'] else 'PUBLIC'}"
        )

        print(f"* DEFAULT BRANCH: {repository.repo['default_branch']}")

        print("* CONFIGURATION:")
        mergify_config = None
        config_file = await repository.get_mergify_config_file()
        if not config_file:
            print(".mergify.yml is missing")
        else:
            print(f"Config filename: {config_file['path']}")
            print(config_file["decoded_content"])
            try:
                mergify_config = await repository.get_mergify_config()
            except rules.InvalidRules as e:  # pragma: no cover
                print(f"configuration is invalid {str(e)}")

        if pull_number is None:
            async for branch in typing.cast(
                typing.AsyncGenerator[github_types.GitHubBranch, None],
                client.items(
                    f"/repos/{owner_login}/{repo}/branches",
                    resource_name="branches",
                    page_limit=100,
                ),
            ):
                q = merge_train.Train(repository, branch["name"])
                await q.load()
                await report_queue("TRAIN", q)

        else:
            repository = await installation.get_repository_by_name(
                github_types.GitHubRepositoryName(repo)
            )
            try:
                ctxt = await repository.get_pull_request_context(
                    github_types.GitHubPullRequestNumber(int(pull_number))
                )
            except http.HTTPNotFound:
                print(f"Pull request `{url}` does not exist")
                await redis_links.shutdown_all()
                return client

            # FIXME queues could also be printed if no pull number given
            # TODO(sileht): display train if any
            q = await merge_train.Train.from_context(ctxt)
            print(f"* TRAIN: {', '.join([f'#{p}' for p in await q.get_pulls()])}")
            print("* PULL REQUEST:")
            pr_data = await ctxt.pull_request.items()
            pprint.pprint(pr_data, width=160)

            is_behind = await ctxt.is_behind
            print(f"is_behind: {is_behind}")

            print(f"mergeable_state: {ctxt.pull['mergeable_state']}")

            print("* MERGIFY LAST CHECKS:")
            for c in await ctxt.pull_engine_check_runs:
                print(
                    f"[{c['name']}]: {c['conclusion']} | {c['output'].get('title')} | {c['html_url']}"
                )
                print(
                    "> "
                    + "\n> ".join(
                        ("No Summary",)
                        if c["output"]["summary"] is None
                        else c["output"]["summary"].split("\n")
                    )
                )

            if mergify_config is not None:
                print("* MERGIFY LIVE MATCHES:")
                pull_request_rules = mergify_config["pull_request_rules"]
                match = await pull_request_rules.get_pull_request_rule(ctxt)
                summary_title, summary = await actions_runner.gen_summary(
                    ctxt, pull_request_rules, match
                )
                print(f"[Summary]: success | {summary_title}")
                print("> " + "\n> ".join(summary.strip().split("\n")))
            await redis_links.shutdown_all()
            return ctxt

    await redis_links.shutdown_all()
    return client


def main() -> None:
    parser = argparse.ArgumentParser(description="Debugger for mergify")
    parser.add_argument("url", help="Pull request url")
    args = parser.parse_args()
    try:
        asyncio.run(report(args.url))
    except KeyboardInterrupt:
        print("Interruped...")
    except BrokenPipeError:
        pass
