import unittest

from job_search.security import StartupSecurityError, authorized, csrf_valid, load_request_security, trusted_proxy_peer


class SecurityPolicyTests(unittest.TestCase):
    def test_loopback_development_needs_no_credentials(self):
        policy = load_request_security({"JOB_SEARCH_HOST": "127.0.0.1"})
        self.assertFalse(policy.enabled)

    def test_non_loopback_requires_complete_production_controls(self):
        with self.assertRaisesRegex(StartupSecurityError, "STARTUP_UNSAFE_BINDING"):
            load_request_security({"JOB_SEARCH_HOST": "0.0.0.0"})

    def test_external_policy_checks_bearer_and_csrf_tokens(self):
        policy = load_request_security(
            {
                "JOB_SEARCH_HOST": "0.0.0.0",
                "JOB_SEARCH_AUTH_TOKEN": "auth-secret",
                "JOB_SEARCH_CSRF_TOKEN": "csrf-secret",
                "JOB_SEARCH_TRUSTED_PROXY": "1",
                "JOB_SEARCH_TLS_TERMINATED": "1",
                "JOB_SEARCH_TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 2001:db8::/32",
            }
        )
        self.assertTrue(authorized("Bearer auth-secret", policy.auth_token))
        self.assertFalse(authorized(None, policy.auth_token))
        self.assertTrue(csrf_valid("csrf-secret", policy.csrf_token))
        self.assertFalse(csrf_valid("incorrect", policy.csrf_token))
        events = []

        def observe(name, **fields):
            events.append((name, fields))

        self.assertTrue(trusted_proxy_peer("10.2.3.4", policy, observe))
        self.assertTrue(trusted_proxy_peer("2001:db8::1", policy, observe))
        self.assertFalse(trusted_proxy_peer("192.168.1.10", policy, observe))
        self.assertEqual(events, [], "Well-formed peers must not create failure events.")
        self.assertFalse(trusted_proxy_peer("not-an-address", policy, observe))
        self.assertEqual(len(events), 1, "One malformed peer must emit one denial event.")
        self.assertEqual(events[0][0], "proxy_address_invalid")
        self.assertEqual(events[0][1]["error_code"], "PROXY_ADDRESS_INVALID")
        self.assertEqual(events[0][1]["cause"], "ValueError")

    def test_external_policy_requires_valid_proxy_cidrs(self):
        environment = {
            "JOB_SEARCH_HOST": "0.0.0.0",
            "JOB_SEARCH_AUTH_TOKEN": "auth-secret",
            "JOB_SEARCH_CSRF_TOKEN": "csrf-secret",
            "JOB_SEARCH_TRUSTED_PROXY": "1",
            "JOB_SEARCH_TLS_TERMINATED": "1",
            "JOB_SEARCH_TRUSTED_PROXY_CIDRS": "not-a-network",
        }

        with self.assertRaisesRegex(StartupSecurityError, "STARTUP_UNSAFE_PROXY_CONFIGURATION"):
            load_request_security(environment)

    def test_malformed_ip_shaped_binding_fails_closed(self):
        with self.assertRaisesRegex(StartupSecurityError, "STARTUP_INVALID_HOST"):
            load_request_security({"JOB_SEARCH_HOST": "127.0.0.999"})


if __name__ == "__main__":
    unittest.main()
