import socket
import unittest

from job_search.http_client import OutboundRequestError, SafeHttpClient


def resolver_for(*addresses):
    return lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]


class FakeResponse:
    def __init__(self, status_code=200, location=None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}


class SafeHttpClientTests(unittest.TestCase):
    def test_rejects_hostname_resolving_to_private_address(self):
        client = SafeHttpClient(resolver=resolver_for("127.0.0.1"))

        with self.assertRaisesRegex(OutboundRequestError, "private or reserved"):
            client.get("manual_posting", "https://jobs.example.test/1", headers={}, timeout=1)

    def test_rejects_redirect_to_private_address(self):
        calls = []

        def request(url, **_kwargs):
            calls.append(url)
            return FakeResponse(302, "http://127.0.0.1/internal")

        client = SafeHttpClient(resolver=resolver_for("8.8.8.8"), request=request)
        with self.assertRaisesRegex(OutboundRequestError, "private or reserved"):
            client.get("manual_posting", "https://jobs.example.test/1", headers={}, timeout=1)
        self.assertEqual(calls, ["https://jobs.example.test/1"])

    def test_allows_public_url_and_disables_automatic_redirects(self):
        observed = {}

        def request(url, **kwargs):
            observed.update(kwargs)
            return FakeResponse()

        client = SafeHttpClient(resolver=resolver_for("8.8.8.8"), request=request)
        response = client.get("manual_posting", "https://jobs.example.test/1", headers={"Accept": "text/html"}, timeout=3)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(observed["allow_redirects"])

    def test_rejects_board_host_outside_allowlist(self):
        client = SafeHttpClient(resolver=resolver_for("8.8.8.8"))

        with self.assertRaisesRegex(OutboundRequestError, "approved job-board host"):
            client.get("indeed", "https://jobs.example.test/1", headers={}, timeout=1)


if __name__ == "__main__":
    unittest.main()
