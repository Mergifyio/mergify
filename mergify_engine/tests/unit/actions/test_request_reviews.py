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
import pytest
import voluptuous

from mergify_engine.actions import request_reviews


@pytest.mark.parametrize(
    "config",
    (
        {},
        {
            "users": ["hello"],
        },
        {
            "teams": ["hello", "@foobar"],
        },
    ),
)
def test_config(config):
    request_reviews.RequestReviewsAction.get_schema()(config)


@pytest.mark.parametrize(
    "config,error",
    (
        (
            {
                "users": ["hello"]
                * (
                    request_reviews.RequestReviewsAction.GITHUB_MAXIMUM_REVIEW_REQUEST
                    + 1
                ),
            },
            "length of value must be at most 15 for dictionary value @ data['users']",
        ),
        (
            {
                "teams": ["hello"]
                * (
                    request_reviews.RequestReviewsAction.GITHUB_MAXIMUM_REVIEW_REQUEST
                    + 1
                ),
            },
            "length of value must be at most 15 for dictionary value @ data['teams']",
        ),
    ),
)
def test_config_not_ok(config, error):
    with pytest.raises(voluptuous.MultipleInvalid) as p:
        request_reviews.RequestReviewsAction.get_schema()(config)
    assert str(p.value) == error
