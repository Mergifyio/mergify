# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Julien Danjou <jd@mergify.io>
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
from mergify_engine import rules as mrules


def _safe_getter(rule, path, default=None):
    cur = rule
    for elem in path:
        cur = cur.get(elem)
        if cur is None:
            return default
    return cur


def _convert_merge_rule(rule, branch_name=None):
    if rule is None and branch_name is None:
        return []
    default_merge_strategy_method = _safe_getter(rule, (
        'merge_strategy', 'method'))
    default_merge_rebase_fallback = _safe_getter(rule, (
        'merge_strategy', 'rebase_fallback'))
    default_required_reviews = _safe_getter(rule, (
        "protection", "required_pull_request_reviews",
        "required_approving_review_count"))
    default_strict = _safe_getter(rule, (
        "protection", "required_status_checks", "strict"))
    default_contexts = _safe_getter(rule, (
        "protection", "required_status_checks", "contexts"), [])

    merge_params = {}
    if default_merge_strategy_method:
        merge_params['method'] = default_merge_strategy_method
    if default_merge_rebase_fallback:
        if default_merge_rebase_fallback == "none":
            default_merge_rebase_fallback = None
        merge_params["rebase_fallback"] = default_merge_rebase_fallback
    if default_strict:
        merge_params["strict"] = default_strict

    if branch_name is None:
        default_conditions = []
        rule_name = "default"
    else:
        if branch_name.startswith("^"):
            operator = "~="
        else:
            operator = "="
        default_conditions = ["base%s%s" % (operator, branch_name)]
        rule_name = "%s branch" % branch_name

    if rule.get('enabling_label'):
        default_conditions.append("label=%s" % rule['enabling_label'])

    if rule.get('disabling_label'):
        default_conditions.append("label!=%s" % rule['disabling_label'])

    conditions = []
    if default_required_reviews:
        conditions.append(
            "#approved-reviews-by>=%d" % default_required_reviews
        )
    if default_contexts:
        conditions.extend(("status-success=%s" % context
                           for context in default_contexts))
    rules = [{
        "name": rule_name,
        "conditions": default_conditions + conditions,
        "actions": {
            "merge": merge_params,
        },
    }]

    automated_backport_labels = rule.get("automated_backport_labels")

    if automated_backport_labels:
        for bp_label, bp_branch_name in sorted(rule.get(
                "automated_backport_labels", {}).items()):
            if branch_name is None:
                bp_suffix = ""
            else:
                bp_suffix = " from %s" % branch_name
            rules.append({
                "name": "backport %s%s" % (bp_branch_name, bp_suffix),
                "conditions": (
                    default_conditions + ["label=%s" % bp_label]
                ),
                "actions": {
                    "backport": {
                        "branches": [bp_branch_name],
                    },
                },
            })

    return rules


def convert_config(rules):
    return (
        _convert_merge_rule(mrules.get_merged_branch_rule(rules)) +
        sum((_convert_merge_rule(
            mrules.get_merged_branch_rule(rules, branch_name),
            branch_name=branch_name)
            for branch_name in sorted(rules['branches'])), [])
    )


def main():
    import argparse

    import yaml

    parser = argparse.ArgumentParser(
        description='Convert Mergify configuration from v1 to v2'
    )
    parser.add_argument("path", type=argparse.FileType(),
                        help="Path of the .mergify.yml file")
    args = parser.parse_args()
    print(yaml.dump(convert_config(yaml.safe_load(args.path).get('rules', {})),
                    default_flow_style=False))
