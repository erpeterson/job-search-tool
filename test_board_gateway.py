import unittest

from job_search.data_access.board_gateway import CallableBoardGateway


class CallableBoardGatewayTests(unittest.TestCase):
    def test_dispatches_to_selected_adapter_without_framework_dependency(self):
        calls = []

        def fetch_linkedin(keywords, location, force_refresh):
            calls.append((keywords, location, force_refresh))
            return [{"title": "Architect"}]

        gateway = CallableBoardGateway(fetch_linkedin, lambda *_args: [])

        result = gateway.fetch("linkedin", "architect", "Remote", force_refresh=True)

        self.assertEqual(result[0]["title"], "Architect")
        self.assertEqual(calls, [("architect", "Remote", True)])

    def test_rejects_unsupported_board_at_adapter_boundary(self):
        gateway = CallableBoardGateway(lambda *_args: [], lambda *_args: [])

        with self.assertRaisesRegex(ValueError, "Unsupported board"):
            gateway.fetch("unknown", "architect", "Remote")


if __name__ == "__main__":
    unittest.main()
