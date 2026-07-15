from __future__ import annotations

import base64
import unittest

from metadata_qa import create_app
from metadata_qa.config import Settings
from metadata_qa.models import JobRecord, JobStatus, PodRecord
from tests.fakes import FakeGateway


class WebIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.from_env(
            {
                "CRONJOB_NAME": "nightly",
                "HTTPAUTH_USERNAME": "operator",
                "HTTPAUTH_PASSWORD": "secret",
                "MAX_LOG_TAIL_LINES": "100",
            }
        )
        self.gateway = FakeGateway()
        self.app = create_app(self.settings, self.gateway, async_mode="threading")
        self.app.testing = True
        self.client = self.app.test_client()
        token = base64.b64encode(b"operator:secret").decode("ascii")
        self.auth = {"Authorization": f"Basic {token}"}

    def test_health_is_available_without_credentials(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok"})
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("object-src 'none'", response.headers["Content-Security-Policy"])

    def test_api_requires_valid_basic_auth(self) -> None:
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)
        self.assertEqual(
            self.client.get("/api/jobs", headers=self.auth).status_code,
            200,
        )

    def test_malformed_basic_auth_is_rejected(self) -> None:
        response = self.client.get("/api/jobs", headers={"Authorization": "Basic !!!not-base64!!!"})

        self.assertEqual(response.status_code, 401)

    def test_tail_lines_is_bounded_before_gateway_access(self) -> None:
        response = self.client.get(
            "/api/jobs/nightly-1700000000/logs?tailLines=101", headers=self.auth
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("between 1 and 100", response.json["error"])

    def test_logs_endpoint_returns_observable_payload(self) -> None:
        self.gateway.jobs = [
            JobRecord(name="nightly-1700000000", status=JobStatus.SUCCEEDED, is_managed=True)
        ]
        self.gateway.pods["nightly-1700000000"] = [PodRecord("pod-1", "Succeeded")]
        self.gateway.logs["pod-1"] = "one\ntwo\nthree"

        response = self.client.get(
            "/api/jobs/nightly-1700000000/logs?tailLines=2", headers=self.auth
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["logs"], "two\nthree")

    def test_delete_unknown_shape_returns_404(self) -> None:
        response = self.client.delete("/api/jobs/nightly-admin", headers=self.auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.gateway.deleted, [])

    def test_readiness_reports_gateway_failure_without_details(self) -> None:
        self.gateway.ready = False

        with self.assertLogs("metadata_qa.web", level="WARNING"):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {"status": "not-ready"})

    def test_index_loads_externalized_application_script(self) -> None:
        response = self.client.get("/", headers=self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/static/app.js", response.data)
        self.assertNotIn(b"onclick=", response.data)

    def test_index_and_security_policy_support_https_frontend(self) -> None:
        response = self.client.get("/", headers=self.auth, base_url="https://ui.example")

        self.assertEqual(response.status_code, 200)
        self.assertIn("ws: wss:", response.headers["Content-Security-Policy"])

    def test_url_prefix_mounts_ui_api_assets_health_and_socketio_together(self) -> None:
        prefixed_settings = Settings.from_env(
            {
                "CRONJOB_NAME": "nightly",
                "HTTPAUTH_USERNAME": "operator",
                "HTTPAUTH_PASSWORD": "secret",
                "URL_PREFIX": "/app/manager/",
            }
        )
        prefixed_app = create_app(prefixed_settings, FakeGateway(), async_mode="threading")
        prefixed_app.testing = True
        client = prefixed_app.test_client()

        index = client.get("/app/manager", headers=self.auth)
        self.assertEqual(index.status_code, 200)
        self.assertIn(b'name="application-base-path" content="/app/manager"', index.data)
        self.assertIn(b"/app/manager/static/app.js", index.data)
        self.assertEqual(
            client.get("/app/manager/static/app.js", headers=self.auth).status_code, 200
        )
        self.assertEqual(client.get("/app/manager/api/jobs", headers=self.auth).status_code, 200)
        self.assertEqual(client.get("/app/manager/healthz").status_code, 200)

        socket_handshake = client.get(
            "/app/manager/socket.io/?EIO=4&transport=polling",
            headers=self.auth,
        )
        self.assertEqual(socket_handshake.status_code, 200)
        self.assertTrue(socket_handshake.data.startswith(b"0"))

        self.assertEqual(client.get("/", headers=self.auth).status_code, 404)
        self.assertEqual(client.get("/api/jobs", headers=self.auth).status_code, 404)
        self.assertEqual(
            client.get("/socket.io/?EIO=4&transport=polling", headers=self.auth).status_code,
            404,
        )

    def test_kubernetes_error_is_logged_but_not_exposed(self) -> None:
        from metadata_qa.kubernetes_gateway import KubernetesError

        def fail_to_list_jobs():
            raise KubernetesError("secret detail")

        self.gateway.list_jobs = fail_to_list_jobs
        with self.assertLogs("metadata_qa.web", level="ERROR"):
            response = self.client.get("/api/jobs", headers=self.auth)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json,
            {"error": "Kubernetes service is temporarily unavailable"},
        )

    def test_socket_connection_and_cancel_event(self) -> None:
        socketio = self.app.extensions["metadata_qa.socketio"]
        socket_client = socketio.test_client(self.app, headers=self.auth)

        self.assertTrue(socket_client.is_connected())
        initial = socket_client.get_received()
        self.assertEqual(initial[0]["name"], "status_update")

        socket_client.emit("cancel_job")
        received = socket_client.get_received()
        self.assertEqual(received[-1]["args"][0]["status"], JobStatus.IDLE.value)
        socket_client.disconnect()

    def test_socket_rejects_missing_basic_auth(self) -> None:
        socketio = self.app.extensions["metadata_qa.socketio"]
        socket_client = socketio.test_client(self.app)

        self.assertFalse(socket_client.is_connected())


if __name__ == "__main__":
    unittest.main()
