import requests

from mergify_engine import config
from mergify_engine.clients import http


real_session_init = requests.sessions.Session.__init__


def retring_session_init(self, *args, **kwargs):
    real_session_init(self, *args, **kwargs)

    adapter = requests.adapters.HTTPAdapter(max_retries=http.RETRY)

    self.mount(f"https://api.{config.GITHUB_DOMAIN}", adapter)


requests.sessions.Session.__init__ = retring_session_init
