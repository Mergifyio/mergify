# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2020 Mergify SAS
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
import pyparsing


git_branch = pyparsing.CharsNotIn("~^: []\\")
regexp = pyparsing.CharsNotIn("")
integer = pyparsing.Word(pyparsing.nums).setParseAction(lambda toks: int(toks[0]))
github_login = pyparsing.Word(pyparsing.alphanums + "-[]")
github_team = pyparsing.Combine(
    pyparsing.Literal("@") + github_login + pyparsing.Literal("/") + github_login
) | pyparsing.Combine(pyparsing.Literal("@") + github_login)
text = (
    pyparsing.QuotedString('"') | pyparsing.QuotedString("'") | pyparsing.CharsNotIn("")
)
milestone = pyparsing.CharsNotIn(" ")

regex_operators = pyparsing.Literal("~=")

simple_operators = (
    pyparsing.Literal(":").setParseAction(pyparsing.replaceWith("="))
    | pyparsing.Literal("=")
    | pyparsing.Literal("==")
    | pyparsing.Literal("!=")
    | pyparsing.Literal("≠")
    | pyparsing.Literal(">=")
    | pyparsing.Literal("≥")
    | pyparsing.Literal("<=")
    | pyparsing.Literal("≤")
    | pyparsing.Literal("<")
    | pyparsing.Literal(">")
)


def _match_boolean(literal):
    return (
        literal
        + pyparsing.Empty().setParseAction(pyparsing.replaceWith("="))
        + pyparsing.Empty().setParseAction(pyparsing.replaceWith(True))
    )


match_integer = simple_operators + integer


def _match_with_operator(token):
    return (simple_operators + token) | (regex_operators + regexp)


def _token_to_dict(s, loc, toks):
    not_, key_op, key, op, value = toks
    if key_op == "#":
        value = int(value)
    d = {op: (key_op + key, value)}
    if not_:
        return {"-": d}
    return d


_match_login_or_teams = _match_with_operator(github_login) | (
    simple_operators + github_team
)


head = "head" + _match_with_operator(git_branch)
base = "base" + _match_with_operator(git_branch)
author = "author" + _match_login_or_teams
merged = _match_boolean("merged")
closed = _match_boolean("closed")
conflict = _match_boolean("conflict")
draft = _match_boolean("draft")
merged_by = "merged-by" + _match_login_or_teams
body = "body" + _match_with_operator(text)
assignee = "assignee" + _match_login_or_teams
label = "label" + _match_with_operator(text)
locked = _match_boolean("locked")
title = "title" + _match_with_operator(text)
files = "files" + _match_with_operator(text)
milestone = "milestone" + _match_with_operator(milestone)
number = "number" + match_integer
review_requests = "review-requested" + _match_login_or_teams
review_approved_by = "approved-reviews-by" + _match_login_or_teams
review_dismissed_by = "dismissed-reviews-by" + _match_login_or_teams
review_changes_requested_by = "changes-requested-reviews-by" + _match_login_or_teams
review_commented_by = "commented-reviews-by" + _match_login_or_teams
status_success = "status-success" + _match_with_operator(text)
status_failure = "status-failure" + _match_with_operator(text)
status_neutral = "status-neutral" + _match_with_operator(text)
check_success = "check-success" + _match_with_operator(text)
check_failure = "check-failure" + _match_with_operator(text)
check_neutral = "check-neutral" + _match_with_operator(text)

search = (
    pyparsing.Optional(
        (
            pyparsing.Literal("-").setParseAction(pyparsing.replaceWith(True))
            | pyparsing.Literal("¬").setParseAction(pyparsing.replaceWith(True))
            | pyparsing.Literal("+").setParseAction(pyparsing.replaceWith(False))
        ),
        default=False,
    )
    + pyparsing.Optional("#", default="")
    + (
        head
        | base
        | author
        | merged_by
        | body
        | assignee
        | label
        | locked
        | closed
        | conflict
        | draft
        | merged
        | title
        | files
        | milestone
        | number
        | review_requests
        | review_approved_by
        | review_dismissed_by
        | review_changes_requested_by
        | review_commented_by
        | status_success
        | status_neutral
        | status_failure
        | check_success
        | check_neutral
        | check_failure
    )
).setParseAction(_token_to_dict)
