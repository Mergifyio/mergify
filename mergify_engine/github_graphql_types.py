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
import typing


class CachedReviewThread(typing.TypedDict):
    isResolved: bool
    first_comment: str


class GraphqlComment(typing.TypedDict):
    body: str


class GraphqlCommentsEdge(typing.TypedDict):
    node: GraphqlComment


class GraphqlComments(typing.TypedDict):
    edges: typing.List[GraphqlCommentsEdge]


class GraphqlReviewThread(typing.TypedDict):
    isResolved: bool
    comments: GraphqlComments
    id: str


class GraphqlResolveReviewThread(typing.TypedDict):
    thread: GraphqlReviewThread


class GraphqlReviewThreadsEdge(typing.TypedDict):
    node: GraphqlReviewThread


class GraphqlReviewThreads(typing.TypedDict):
    edges: typing.List[GraphqlReviewThreadsEdge]


class GraphqlPullRequest(typing.TypedDict):
    reviewThreads: GraphqlReviewThreads


class GraphqlResolveThreadMutationResponse(typing.TypedDict):
    resolveReviewThread: GraphqlResolveReviewThread


class GraphqlRepository(typing.TypedDict):
    pullRequest: GraphqlPullRequest


class GraphqlReviewThreadsQuery(typing.TypedDict):
    repository: GraphqlRepository
