import socket
import unittest
from unittest.mock import patch

from job_search.http_client import (
    OutboundRequestError,
    PinnedAddressTransport,
    ResolvedDestination,
    SafeHttpClient,
    _PinnedHTTPSConnection,
)


def resolver_for(*addresses):
    return lambda *_args, **_kwargs: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
        for address in addresses
    ]


class FakeResponse:
    def __init__(self, status_code=200, location=None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}


class RecordingTransport:
    def __init__(self, *responses):
        self.responses = list(responses or [FakeResponse()])
        self.calls = []

    def get(self, destination, *, headers, timeout):
        self.calls.append((destination, headers, timeout))
        return self.responses.pop(0)


class SafeHttpClientTests(unittest.TestCase):
    def test_rejects_hostname_resolving_to_private_address(self):
        client = SafeHttpClient(resolver=resolver_for("127.0.0.1"))

        with self.assertRaisesRegex(OutboundRequestError, "private or reserved"):
            client.get("manual_posting", "https://jobs.example.test/1", headers={}, timeout=1)

    def test_pins_validated_address_despite_rebinding_connection_answer(self):
        transport = RecordingTransport()
        validation_dns = resolver_for("8.8.8.8")
        simulated_connection_dns = resolver_for("127.0.0.1")
        client = SafeHttpClient(resolver=validation_dns, transport=transport)

        client.get("manual_posting", "https://jobs.example.test/1", headers={}, timeout=1)

        destination = transport.calls[0][0]
        self.assertEqual(destination.address, "8.8.8.8")
        self.assertNotEqual(destination.address, simulated_connection_dns("jobs.example.test", None)[0][4][0])
        self.assertEqual(destination.hostname, "jobs.example.test")

    def test_validates_every_redirect_hop_before_transport_connection(self):
        transport = RecordingTransport(FakeResponse(302, "http://127.0.0.1/internal"))
        client = SafeHttpClient(resolver=resolver_for("8.8.8.8"), transport=transport)

        with self.assertRaisesRegex(OutboundRequestError, "private or reserved"):
            client.get("manual_posting", "https://jobs.example.test/1", headers={}, timeout=1)
        self.assertEqual(len(transport.calls), 1)

    def test_rejects_board_host_outside_allowlist(self):
        client = SafeHttpClient(resolver=resolver_for("8.8.8.8"))

        with self.assertRaisesRegex(OutboundRequestError, "approved job-board host"):
            client.get("indeed", "https://jobs.example.test/1", headers={}, timeout=1)

    def test_pinned_transport_preserves_https_hostname_and_host_header(self):
        destination = ResolvedDestination("https://jobs.example.test:8443/path?q=1", "jobs.example.test", "8.8.8.8")
        observed = {}

        class FakeConnection:
            def __init__(self, address, port, hostname, timeout):
                observed.update(address=address, port=port, hostname=hostname, timeout=timeout)

            def request(self, method, path, headers):
                observed.update(method=method, path=path, headers=headers)

            def getresponse(self):
                class RawResponse:
                    status = 200
                    reason = "OK"

                    @staticmethod
                    def getheaders():
                        return []

                    @staticmethod
                    def read():
                        return b"ok"

                return RawResponse()

            def close(self):
                observed["closed"] = True

        with patch("job_search.http_client._PinnedHTTPSConnection", FakeConnection):
            response = PinnedAddressTransport().get(
                destination, headers={"Host": "attacker", "Accept": "text/html"}, timeout=7
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["address"], "8.8.8.8")
        self.assertEqual(observed["hostname"], "jobs.example.test")
        self.assertEqual(observed["headers"]["Host"], "jobs.example.test:8443")
        self.assertEqual(observed["path"], "/path?q=1")
        self.assertTrue(observed["closed"])

    def test_pinned_transport_supports_ipv6_numeric_destinations(self):
        destination = ResolvedDestination("http://[2001:4860:4860::8888]/", "example.test", "2001:4860:4860::8888")
        observed = {}

        class FakeConnection:
            def __init__(self, address, port, timeout):
                observed.update(address=address, port=port, timeout=timeout)

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                class RawResponse:
                    status = 200
                    reason = "OK"

                    @staticmethod
                    def getheaders():
                        return []

                    @staticmethod
                    def read():
                        return b""

                return RawResponse()

            def close(self):
                pass

        with patch("job_search.http_client.http.client.HTTPConnection", FakeConnection):
            PinnedAddressTransport().get(destination, headers={}, timeout=3)

        self.assertEqual(observed["address"], "2001:4860:4860::8888")
        self.assertEqual(observed["port"], 80)
        self.assertEqual(observed["timeout"], 3)

    def test_https_connection_uses_pinned_address_with_hostname_for_tls_verification(self):
        created_sockets = []
        observed = {}

        class FakeTlsContext:
            check_hostname = True

            def wrap_socket(self, sock, *, server_hostname):
                observed.update(wrapped_socket=sock, server_hostname=server_hostname)
                return "tls-socket"

        fake_context = FakeTlsContext()
        with (
            patch("job_search.http_client.ssl.create_default_context", return_value=fake_context),
            patch(
                "job_search.http_client.socket.create_connection",
                side_effect=lambda *args: (created_sockets.append(args) or "tcp-socket"),
            ),
        ):
            connection = _PinnedHTTPSConnection("8.8.8.8", 443, "jobs.example.test", 9)
            connection.connect()

        self.assertTrue(fake_context.check_hostname)
        self.assertEqual(created_sockets[0][0], ("8.8.8.8", 443))
        self.assertEqual(observed["server_hostname"], "jobs.example.test")
        self.assertEqual(connection.sock, "tls-socket")

    def test_pinned_transport_converts_connection_timeout_to_safe_error(self):
        destination = ResolvedDestination("http://8.8.8.8/", "example.test", "8.8.8.8")

        class TimeoutConnection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                raise socket.timeout("timed out")

            def close(self):
                pass

        with patch("job_search.http_client.http.client.HTTPConnection", TimeoutConnection):
            with self.assertRaisesRegex(OutboundRequestError, "Pinned outbound connection failed"):
                PinnedAddressTransport().get(destination, headers={}, timeout=1)


if __name__ == "__main__":
    unittest.main()
