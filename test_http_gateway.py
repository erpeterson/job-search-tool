"""Captured HTTP transport failures retain one safe blame event."""

import unittest

from job_search.data_access.http_gateway import CapturingHttpGateway
from job_search.http_client import OutboundRequestError


class CapturingHttpGatewayTests(unittest.TestCase):
    def test_outbound_failure_emits_one_sanitized_event_and_rethrows(self):
        class FailingClient:
            def get(self, *_args, **_kwargs):
                raise OutboundRequestError("private-http-token")

        events = []
        api_calls = []
        captures = []
        gateway = CapturingHttpGateway(
            FailingClient(),
            lambda: {"User-Agent": "test"},
            lambda *_args, **_kwargs: None,
            lambda *args: captures.append(args),
            lambda *args, **kwargs: api_calls.append((args, kwargs)),
            lambda name, **fields: events.append((name, fields)),
            lambda headers: headers,
        )

        with self.assertRaises(OutboundRequestError):
            gateway.get("indeed", "https://example.test/job?token=private", force_refresh=True)

        self.assertEqual(len(events), 1, "One caught transport error must emit one failure event.")
        self.assertEqual(events[0][0], "http_outbound_failed")
        self.assertEqual(events[0][1]["error_code"], "HTTP_OUTBOUND_FAILED")
        self.assertEqual(events[0][1]["component"], "data_access.http_gateway")
        self.assertEqual(events[0][1]["operation"], "get")
        self.assertEqual(events[0][1]["service"], "indeed")
        self.assertEqual(events[0][1]["cause"], "OutboundRequestError")
        self.assertEqual(len(api_calls), 1, "The normal API-attempt trace must still be recorded.")
        self.assertEqual(captures[0][3]["error_message"], "OutboundRequestError")
        self.assertNotIn("private-http-token", str(events) + str(captures))


if __name__ == "__main__":
    unittest.main()
