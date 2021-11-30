# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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
import contextlib
import dataclasses
import functools
import re
import typing

import jinja2.exceptions
import jinja2.meta
import jinja2.sandbox
import markdownify

from mergify_engine import context


MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?\s*$", re.I)


@dataclasses.dataclass
class RenderTemplateFailure(Exception):
    message: str
    lineno: typing.Optional[int] = None

    def __str__(self) -> str:
        return self.message


@contextlib.contextmanager
def _template_exceptions_mapping() -> typing.Iterator[None]:
    try:
        yield
    except jinja2.exceptions.TemplateSyntaxError as tse:
        raise RenderTemplateFailure(tse.message or "", tse.lineno)
    except jinja2.exceptions.TemplateError as te:
        raise RenderTemplateFailure(te.message or "")
    except context.PullRequestAttributeError as e:
        raise RenderTemplateFailure(f"Unknown pull request attribute: {e.name}")


async def _filter_get_section(
    pull: "context.PullRequest",
    v: str,
    section: str,
    default: typing.Optional[str] = None,
) -> str:
    if not isinstance(section, str):
        raise TypeError("level must be a string")

    section_escaped = re.escape(section)
    level = MARKDOWN_TITLE_RE.match(section)

    if level is None:
        raise TypeError("section level not found")

    level_str = level[0].strip()

    level_re = re.compile(fr"^{level_str} +", re.I)
    section_re = re.compile(fr"^{section_escaped}\s*$", re.I)

    found = False
    section_lines = []
    for line in v.split("\n"):
        if section_re.match(line):
            found = True
        elif found and level_re.match(line):
            break
        elif found:
            section_lines.append(line.strip())

    if found:
        text = ("\n".join(section_lines)).strip()
    elif default is None:
        raise TypeError("section not found")
    else:
        text = default

    # We don't allow get_section to avoid never-ending recursion
    return await render_template(pull, text, allow_get_section=False)


async def render_template(
    pull: "context.PullRequest",
    template: str,
    extra_variables: typing.Optional[typing.Dict[str, typing.Union[str, bool]]] = None,
    allow_get_section: bool = True,
) -> str:
    """Render a template interpolating variables based on pull request attributes."""
    env = jinja2.sandbox.SandboxedEnvironment(
        undefined=jinja2.StrictUndefined, enable_async=True
    )
    env.filters["markdownify"] = lambda s: markdownify.markdownify(s)
    if allow_get_section:
        env.filters["get_section"] = functools.partial(_filter_get_section, pull)

    with _template_exceptions_mapping():
        used_variables = jinja2.meta.find_undeclared_variables(env.parse(template))
        infos = {}
        for k in used_variables:
            if extra_variables and k in extra_variables:
                infos[k] = extra_variables[k]
            else:
                infos[k] = await getattr(pull, k)
        return await env.from_string(template).render_async(**infos)


async def get_commit_message(
    pull: context.PullRequest,
    mode: typing.Literal["default", "title+body", "template"] = "default",
    template: typing.Optional[str] = None,
) -> typing.Optional[typing.Tuple[str, str]]:

    if mode == "title+body":
        body = typing.cast(str, await pull.body)
        # Include PR number to mimic default GitHub format
        return (
            f"{(await pull.title)} (#{(await pull.number)})",
            body,
        )

    if template is None:
        # No template from configuration, looks at template from body
        body = typing.cast(str, await pull.body)
        if not body:
            return None
        found = False
        message_lines = []

        for line in body.split("\n"):
            if MARKDOWN_COMMIT_MESSAGE_RE.match(line):
                found = True
            elif found and MARKDOWN_TITLE_RE.match(line):
                break
            elif found:
                message_lines.append(line)
            if found:
                pull.context.log.info("Commit message template found in body")
                template = "\n".join(line.strip() for line in message_lines)

    if template is None:
        return None

    commit_message = await render_template(pull, template.strip())
    if not commit_message:
        return None

    template_title, _, template_message = commit_message.partition("\n")
    return (template_title, template_message.lstrip())
