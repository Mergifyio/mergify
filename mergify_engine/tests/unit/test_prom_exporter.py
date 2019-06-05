from unittest import mock

import requests.exceptions

from mergify_engine import prom_exporter


def test_exception_need_retry():
    retry_state = mock.Mock()
    retry_state.outcome.failed = True
    retry_state.outcome.exception.return_value = e = (
        requests.exceptions.HTTPError()
    )
    e.response = mock.Mock()
    e.response.status_code = 500
    assert prom_exporter._exception_need_retry(retry_state) is True


def test_exception_wait_time():
    retry_state = mock.Mock()
    retry_state.outcome.failed = True
    retry_state.outcome.exception.return_value = e = (
        requests.exceptions.HTTPError()
    )
    e.response = mock.Mock()
    e.response.status_code = 500
    assert prom_exporter._wait_time_for_exception(retry_state) == 30
