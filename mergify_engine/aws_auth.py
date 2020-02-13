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

# Inspired from botocore.auth.SigV4Auth

import datetime
from hashlib import sha256
import hmac
import http.client
from urllib.parse import urlsplit, quote

import daiquiri
import httpx

from mergify_engine import config

LOG = daiquiri.getLogger(__name__)

EMPTY_SHA256_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SIGV4_TIMESTAMP = "%Y%m%dT%H%M%SZ"
SIGNED_HEADERS_BLACKLIST = [
    "expect",
    "user-agent",
    "x-amzn-trace-id",
]


class AWSV4SignAuth(httpx.Auth):
    def __init__(self, service_name):
        self._service_name = service_name
        self._region_name = config.AWS_REGION_NAME
        self._access_key = config.AWS_ACCESS_KEY_ID
        self._secret_key = config.AWS_SECRET_ACCESS_KEY

    def auth_flow(self, request):
        # NOTE(sileht): Logging here, is the same format as AWS signing error, so it's
        # easy to compare what wrong.

        datetime_now = datetime.datetime.utcnow()
        timestamp = datetime_now.strftime(SIGV4_TIMESTAMP)
        request.headers["X-Amz-Date"] = timestamp

        canonical_request = self.canonical_request(request)
        LOG.debug("Calculating signature using v4 auth.")
        LOG.debug("CanonicalRequest:\n%s", canonical_request)

        string_to_sign = self.string_to_sign(request, canonical_request)
        LOG.debug("StringToSign:\n%s", string_to_sign)

        signature = self.signature(request, string_to_sign)
        LOG.debug("Signature:\n%s", signature)

        self._inject_signature_to_request(request, signature)

        yield request

    def _sign(self, key, msg, hex=False):
        if hex:
            sig = hmac.new(key, msg.encode("utf-8"), sha256).hexdigest()
        else:
            sig = hmac.new(key, msg.encode("utf-8"), sha256).digest()
        return sig

    def headers_to_sign(self, request):
        """
        Select the headers from the request that need to be included
        in the StringToSign.
        """
        header_map = http.client.HTTPMessage()
        for name, value in request.headers.items():
            lname = name.lower()
            if lname not in SIGNED_HEADERS_BLACKLIST:
                header_map[lname] = value
        if "host" not in header_map:
            # Ensure we sign the lowercased version of the host, as that
            # is what will ultimately be sent on the wire.
            # TODO: We should set the host ourselves, instead of relying on our
            # HTTP client to set it for us.
            header_map["host"] = self._canonical_host(request.url).lower()
        return header_map

    def _canonical_host(self, url):
        url_parts = urlsplit(url)
        default_ports = {"http": 80, "https": 443}
        if any(
            url_parts.scheme == scheme and url_parts.port == port
            for scheme, port in default_ports.items()
        ):
            # No need to include the port if it's the default port.
            return url_parts.hostname
        # Strip out auth if it's present in the netloc.
        return url_parts.netloc.rsplit("@", 1)[-1]

    def canonical_headers(self, headers_to_sign):
        """
        Return the headers that need to be included in the StringToSign
        in their canonical form by converting all header keys to lower
        case, sorting them in alphabetical order and then joining
        them into a string, separated by newlines.
        """
        headers = []
        sorted_header_names = sorted(set(headers_to_sign))
        for key in sorted_header_names:
            value = ",".join(
                self._header_value(v) for v in sorted(headers_to_sign.get_all(key))
            )
            headers.append("%s:%s" % (key, value))
        return "\n".join(headers)

    def _header_value(self, value):
        # From the sigv4 docs:
        # Lowercase(HeaderName) + ':' + Trimall(HeaderValue)
        #
        # The Trimall function removes excess white space before and after
        # values, and converts sequential spaces to a single space.
        return " ".join(value.split())

    def signed_headers(self, headers_to_sign):
        headers = ["%s" % n.lower().strip() for n in set(headers_to_sign)]
        headers = sorted(headers)
        return ";".join(headers)

    def canonical_request(self, request):
        cr = [request.method.upper()]
        cr.append(self._normalize_url_path(request.url.path))
        cr.append(self._normalize_qs(request.url.query))
        headers_to_sign = self.headers_to_sign(request)
        cr.append(self.canonical_headers(headers_to_sign) + "\n")
        cr.append(self.signed_headers(headers_to_sign))
        body_checksum = self.payload(request)
        cr.append(body_checksum)
        return "\n".join(cr)

    def payload(self, request):
        request_body = request.read()
        if request_body:
            return sha256(request_body).hexdigest()
        else:
            return EMPTY_SHA256_HASH

    def remove_dot_segments(self, url):
        # RFC 3986, section 5.2.4 "Remove Dot Segments"
        # Also, AWS services require consecutive slashes to be removed,
        # so that's done here as well
        if not url:
            return "/"
        input_url = url.split("/")
        output_list = []
        for x in input_url:
            if x and x != ".":
                if x == "..":
                    if output_list:
                        output_list.pop()
                else:
                    output_list.append(x)

        if url[0] == "/":
            first = "/"
        else:
            first = ""
        if url[-1] == "/" and output_list:
            last = "/"
        else:
            last = ""

        return first + "/".join(output_list) + last

    def _normalize_qs(self, qs):
        # Must be ordered...
        return "&".join(sorted(qs.split("&")))

    def _normalize_url_path(self, path):
        normalized_path = quote(self.remove_dot_segments(path), safe="/~")
        return normalized_path

    def scope(self, request):
        scope = [self._access_key]
        scope.append(request.headers["X-Amz-Date"][0:8])
        scope.append(self._region_name)
        scope.append(self._service_name)
        scope.append("aws4_request")
        return "/".join(scope)

    def credential_scope(self, request):
        scope = []
        scope.append(request.headers["X-Amz-Date"][0:8])
        scope.append(self._region_name)
        scope.append(self._service_name)
        scope.append("aws4_request")
        return "/".join(scope)

    def string_to_sign(self, request, canonical_request):
        """
        Return the canonical StringToSign as well as a dict
        containing the original version of all headers that
        were included in the StringToSign.
        """
        sts = ["AWS4-HMAC-SHA256"]
        sts.append(request.headers["X-Amz-Date"])
        sts.append(self.credential_scope(request))
        sts.append(sha256(canonical_request.encode("utf-8")).hexdigest())
        return "\n".join(sts)

    def signature(self, request, string_to_sign):
        key = self._secret_key
        k_date = self._sign(
            ("AWS4" + key).encode("utf-8"), request.headers["X-Amz-Date"][0:8]
        )
        k_region = self._sign(k_date, self._region_name)
        k_service = self._sign(k_region, self._service_name)
        k_signing = self._sign(k_service, "aws4_request")
        return self._sign(k_signing, string_to_sign, hex=True)

    def _inject_signature_to_request(self, request, signature):
        headers = ["AWS4-HMAC-SHA256 Credential=%s" % self.scope(request)]
        headers_to_sign = self.headers_to_sign(request)
        headers.append("SignedHeaders=%s" % self.signed_headers(headers_to_sign))
        headers.append("Signature=%s" % signature)
        request.headers["Authorization"] = ", ".join(headers)
        return request
