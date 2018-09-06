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


def _convert_merge_rule(rule, branch_name=None):
    default_merge_strategy_method = rule.get(
        'merge_strategy', {}).get('method')
    default_merge_rebase_fallback = rule.get(
        'merge_strategy', {}).get('rebase_fallback')
    default_required_reviews = (
        rule['protection'][
            'required_pull_request_reviews'][
                'required_approving_review_count']
    )
    default_strict = rule.get(
        'protection', {}).get('required_status_checks', {}).get('strict')
    default_contexts = rule.get(
        'protection', {}).get('required_status_checks', {}).get('contexts', [])

    merge_params = {}
    if default_merge_strategy_method:
        merge_params['method'] = default_merge_strategy_method
    if default_merge_rebase_fallback:
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

    rules = [{
        "name": rule_name,
        "conditions": default_conditions + [
            "#review-approved-by>=%d" % default_required_reviews,
        ] + [
            "status-success=%s" % context for context in default_contexts
        ],
        "merge": merge_params,
    }]

    for bp_label, bp_branch_name in rule.get(
            "automated_backport_labels", {}).items():
        if branch_name is None:
            bp_suffix = ""
        else:
            bp_suffix = " from %s" % branch_name
        rules.append({
            "name": "backport %s%s" % (bp_branch_name, bp_suffix),
            "conditions": (
                default_conditions + ["label=%s" % bp_label, "merged"]
            ),
            "backport": [bp_branch_name],
        })

    return rules


def convert_config(rules):
    new_rules = _convert_merge_rule(
        mrules.merge_branch_rule_with_default(rules["default"]))

    for branch_name in rules['branches']:
        new_rules.extend(_convert_merge_rule(
            mrules.get_merged_branch_rule(rules, branch_name),
            branch_name=branch_name))

    return new_rules
