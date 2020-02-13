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

import asyncio
import importlib
import sys
import xml.etree.ElementTree as XML

import daiquiri
import httpx
import ujson

from mergify_engine import aws_auth
from mergify_engine import config
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)

SQS_VERSION = "2012-11-05"
SQS_BASE_URL = (
    f"https://sqs.{config.AWS_REGION_NAME}.amazonaws.com/{config.AWS_ACCOUNT_ID}"
)
SQS_AUTH = aws_auth.AWSV4SignAuth("sqs")
SQS_NAMESPACES = {"": "http://queue.amazonaws.com/doc/2012-11-05/"}

# NOTE(sileht): connect/write/pool-acquire are 5s by default
SQS_TIMEOUT = httpx.Timeout(read_timeout=25.0)
# NOTE(sileht): between 0s and 20s, httpx read_timeout must be higher
SQS_WAIT_TIMEOUT = 20
SQS_VISIBILITY_TIMEOUT = 10
SQS_CONCURENCY_CALL = 5
SQS_MAX_NUMBER_MESSAGES = 1


async def send(method, args, countdown=30):
    if method.__module__ == "celery.local":
        # FIXME(sileht): This is a temporary hack to have method that can run within
        # celery and sqs at the same time.
        method = method.run

    body = ujson.dumps(
        {"__module__": method.__module__, "__name__": method.__name__, "args": args}
    )
    async with httpx.AsyncClient(base_url=SQS_BASE_URL, auth=SQS_AUTH) as client:
        request = client.post(
            config.AWS_SQS_QUEUE,
            data={
                "Action": "SendMessage",
                "Version": SQS_VERSION,
                "MessageBody": body,
                # "DelaySeconds": countdown,
            },
        )
        try:
            response = await request
            response.raise_for_status()
        except httpx.exceptions.HTTPError as e:
            LOG.error(
                "fail to post sqs message",
                status_code=e.response.status_code if e.reponse else None,
                text=e.response.text if e.response else None,
                exc_info=True,
            )


def _create_receive_message_task(client):
    return client.post(
        config.AWS_SQS_QUEUE,
        data={
            "Action": "ReceiveMessage",
            "Version": SQS_VERSION,
            "MaxNumberOfMessages": SQS_MAX_NUMBER_MESSAGES,
            "VisibilityTimeout": SQS_VISIBILITY_TIMEOUT,
            "WaitTimeSeconds": SQS_WAIT_TIMEOUT,
        },
    )


def _create_delete_messages_task(client, messages):
    payload = {
        "Action": "DeleteMessageBatch",
        "Version": SQS_VERSION,
    }
    for i, message in enumerate(messages):
        j = i + 1
        payload[f"DeleteMessageBatchRequestEntry.{j}.Id"] = message.find(
            "MessageId", SQS_NAMESPACES
        ).text
        payload[f"DeleteMessageBatchRequestEntry.{j}.ReceiptHandle"] = message.find(
            "ReceiptHandle", SQS_NAMESPACES
        ).text

    return client.post(config.AWS_SQS_QUEUE, data=payload)


async def _extract_job(request):
    try:
        response = await request
        response.raise_for_status()
    except httpx.exceptions.HTTPError as e:
        LOG.error(
            "fail to get sqs messages",
            status_code=e.response.status_code if e.reponse else None,
            text=e.response.text if e.response else None,
            exc_info=True,
        )
        return []

    root = XML.fromstring(response.text)
    messages = root.findall(f".//Message", SQS_NAMESPACES)
    return [
        ujson.loads(message.find("Body", SQS_NAMESPACES).text) for message in messages
    ]


async def reap_deleted_tasks(delete_tasks):
    if not delete_tasks:
        return delete_tasks

    finished, delete_tasks = await asyncio.wait(
        delete_tasks, return_when=asyncio.ALL_COMPLETED, timeout=0
    )
    for request in finished:
        try:
            response = await request
            response.raise_for_status()
        except httpx.exceptions.HTTPError as e:
            LOG.error(
                "fail to delete sqs messages",
                status_code=e.response.status_code if e.reponse else None,
                text=e.response.text if e.response else None,
                exc_info=True,
            )
    return delete_tasks


async def get_jobs():
    async with httpx.AsyncClient(
        base_url=SQS_BASE_URL, auth=SQS_AUTH, timeout=SQS_TIMEOUT
    ) as client:
        delete_tasks = set()
        while True:
            delete_tasks = await reap_deleted_tasks(delete_tasks)

            tasks = [
                _create_receive_message_task(client) for _ in range(SQS_CONCURENCY_CALL)
            ]
            requests, _ = await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)

            jobs = []
            for request in requests:
                jobs.extend(await _extract_job(request))

            LOG.debug("got %d jobs from sqs", len(jobs))
            if jobs:
                try:
                    for job in jobs:
                        yield job
                except Exception:
                    LOG.error("fail to process messages", exc_info=True)
                    continue
                else:
                    delete_tasks.add(_create_delete_messages_task(client, jobs))


async def main():
    utils.setup_logging()
    async for job in get_jobs():
        if job["__module__"] not in sys.modules:
            importlib.import_module(job["__module__"])

        # TODO(sileht): Make all job method async
        getattr(sys.modules[job["__module__"]], job["__name__"])(*job["args"])


if __name__ == "__main__":
    asyncio.run(main())
