import unittest

from job_search.security import StartupSecurityError, authorized, csrf_valid, load_request_security


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
            }
        )
        self.assertTrue(authorized("Bearer auth-secret", policy.auth_token))
        self.assertFalse(authorized(None, policy.auth_token))
        self.assertTrue(csrf_valid("csrf-secret", policy.csrf_token))
        self.assertFalse(csrf_valid("incorrect", policy.csrf_token))


if __name__ == "__main__":
    unittest.main()
