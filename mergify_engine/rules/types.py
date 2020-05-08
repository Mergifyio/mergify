# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
import dataclasses

import jinja2.exceptions
import jinja2.sandbox
import voluptuous

from mergify_engine import context


_JINJA_ENV = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)


@dataclasses.dataclass
class LineColumnPath(object):
    line: int
    column: int

    def __repr__(self):
        if self.column is None:
            return f"line {self.line}"
        return f"line {self.line}, column {self.column}"


class DummyContext(context.Context):

    # This is only used to check Jinja2 syntax validity
    @staticmethod
    def _get_consolidated_data(key):
        if key not in context.PullRequest.ATTRIBUTES:
            raise context.PullRequestAttributeError(key)

    @staticmethod
    def _ensure_complete():
        pass


_DUMMY_PR = context.PullRequest(DummyContext(None, {}, {}))


def Jinja2(value):
    """A Jinja2 type for voluptuous Schemas."""
    if value is None:
        raise voluptuous.Invalid("Template cannot be null")
    try:
        # TODO: optimize this by returning, storing and using the parsed Jinja2 AST
        _DUMMY_PR.render_template(value)
    except context.RenderTemplateFailure as rtf:
        if rtf.lineno is None:
            path = None
        else:
            path = [LineColumnPath(rtf.lineno, None)]
        raise voluptuous.Invalid(
            "Template syntax error", error_message=str(rtf), path=path
        )
    return value
