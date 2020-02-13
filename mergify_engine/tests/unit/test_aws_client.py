from unittest import mock
import json

import httpx
from freezegun import freeze_time

from mergify_engine import aws_auth

SQS_VERSION = "2012-11-05"
SQS_BASE_URL = f"https://sqs.whatever.amazonaws.com/1234567"
SQS_AUTH = aws_auth.AWSV4SignAuth("sqs")


def app_send(request, timeout):
    return httpx.models.Response(
        status_code=200, headers={}, request=request, http_version="HTTP/1.1"
    )


@freeze_time("2020-02-13T13:39:40")
def test_aws_authv4_post():
    app = mock.Mock()
    app.send.side_effect = app_send

    with httpx.Client(base_url=SQS_BASE_URL, auth=SQS_AUTH, dispatch=app) as client:
        request = client.post(
            "all-events",
            data={
                "Action": "SendMessage",
                "Version": SQS_VERSION,
                "MessageBody": json.dumps({"my": "body"}),
                "DelaySeconds": 30,
            },
        ).request
        assert (
            request.read()
            == b"Action=SendMessage&Version=2012-11-05&MessageBody=%7B%22my%22%3A+%22body%22%7D&DelaySeconds=30"
        )
        assert dict(request.headers) == {
            "host": "sqs.whatever.amazonaws.com",
            "user-agent": "python-httpx/0.11.1",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
            "content-length": "94",
            "content-type": "application/x-www-form-urlencoded",
            "x-amz-date": "20200213T133940Z",
            "authorization": (
                "AWS4-HMAC-SHA256 "
                "Credential=X/20200213/us-east-2/sqs/aws4_request, "
                "SignedHeaders=accept;accept-encoding;connection;content-length;content-type;host;x-amz-date, "
                "Signature=66e83d69a8ac3062c21a147efa8ed560b3d1bd97326067f985539c335c8339a0"
            ),
        }


@freeze_time("2020-02-13T13:39:40")
def test_aws_authv4_get():
    app = mock.Mock()
    app.send.side_effect = app_send

    with httpx.Client(base_url=SQS_BASE_URL, auth=SQS_AUTH, dispatch=app) as client:
        request = client.get(
            "all-events",
            params={
                "Action": "ReceivedMessage",
                "Version": SQS_VERSION,
                "MaxNumberOfMessages": 10,
                "VisibilityTimeout": 1,
                "WaitTimeSeconds": 1,
            },
        ).request
        assert request.read() == b""
        assert (
            request.url
            == "https://sqs.whatever.amazonaws.com/all-events?Action=ReceivedMessage&Version=2012-11-05&MaxNumberOfMessages=10&VisibilityTimeout=1&WaitTimeSeconds=1"
        )
        assert dict(request.headers) == {
            "host": "sqs.whatever.amazonaws.com",
            "user-agent": "python-httpx/0.11.1",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
            "x-amz-date": "20200213T133940Z",
            "authorization": (
                "AWS4-HMAC-SHA256 "
                "Credential=X/20200213/us-east-2/sqs/aws4_request, "
                "SignedHeaders=accept;accept-encoding;connection;host;x-amz-date, "
                "Signature=d4d811efe0e2b3a3fa5307ca104b62ce043cf074518d12fcfe79572508c1a71a"
            ),
        }
