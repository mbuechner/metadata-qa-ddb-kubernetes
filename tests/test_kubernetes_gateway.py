from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kubernetes import client
from kubernetes.client import ApiException
from kubernetes.config import ConfigException

from metadata_qa.config import Settings
from metadata_qa.kubernetes_gateway import (
    CRONJOB_LABEL,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    KubernetesError,
    KubernetesGateway,
    KubernetesUnavailableError,
)


def make_job(
    name: str,
    *,
    labels: dict[str, str] | None = None,
    active: int | None = None,
    succeeded: int | None = None,
) -> client.V1Job:
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=name, labels=labels),
        status=client.V1JobStatus(active=active, succeeded=succeeded),
    )


class KubernetesGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.from_env(
            {
                "CRONJOB_NAME": "nightly",
                "KUBERNETES_REQUEST_TIMEOUT_SECONDS": "7",
                "LOG_STREAM_REQUEST_TIMEOUT_SECONDS": "11",
            }
        )
        self.gateway = KubernetesGateway(self.settings)
        self.api_client = Mock()
        self.core_api = Mock()
        self.batch_api = Mock()
        # Inject initialized clients so unit tests never read local cluster configuration.
        self.gateway._api_client = self.api_client
        self.gateway._core_api = self.core_api
        self.gateway._batch_api = self.batch_api

    def test_list_jobs_normalizes_labels_and_strict_legacy_names(self) -> None:
        self.batch_api.list_namespaced_job.return_value = SimpleNamespace(
            items=[
                make_job(
                    "nightly-1700000000",
                    labels={MANAGED_BY_LABEL: MANAGED_BY_VALUE, CRONJOB_LABEL: "nightly"},
                    active=1,
                ),
                make_job("nightly-1700000001", succeeded=1),
                make_job("nightly-administrator", active=1),
            ]
        )

        jobs = self.gateway.list_jobs()

        self.assertTrue(jobs[0].is_managed)
        self.assertFalse(jobs[0].is_legacy)
        self.assertTrue(jobs[1].is_managed)
        self.assertTrue(jobs[1].is_legacy)
        self.assertFalse(jobs[2].is_managed)
        self.batch_api.list_namespaced_job.assert_called_once_with(
            namespace=self.settings.namespace,
            _request_timeout=7,
        )

    def test_create_adds_ownership_labels_and_timeout(self) -> None:
        template_spec = SimpleNamespace()
        self.batch_api.read_namespaced_cron_job.return_value = SimpleNamespace(
            spec=SimpleNamespace(job_template=SimpleNamespace(spec=template_spec))
        )
        self.api_client.sanitize_for_serialization.return_value = {"template": {"spec": {}}}
        self.batch_api.create_namespaced_job.return_value = make_job(
            "nightly-1700000000",
            labels={MANAGED_BY_LABEL: MANAGED_BY_VALUE, CRONJOB_LABEL: "nightly"},
            active=1,
        )

        result = self.gateway.create_job_from_cronjob("nightly-1700000000")

        self.assertTrue(result.is_managed)
        call = self.batch_api.create_namespaced_job.call_args
        body = call.kwargs["body"]
        self.assertEqual(body["metadata"]["labels"][MANAGED_BY_LABEL], MANAGED_BY_VALUE)
        self.assertEqual(body["metadata"]["labels"][CRONJOB_LABEL], "nightly")
        self.assertEqual(body["spec"]["template"]["spec"]["restartPolicy"], "Never")
        self.assertEqual(call.kwargs["_request_timeout"], 7)

    def test_read_job_maps_404_to_none(self) -> None:
        self.batch_api.read_namespaced_job.side_effect = ApiException(status=404)

        self.assertIsNone(self.gateway.read_job("nightly-1700000000"))

    def test_non_404_api_error_keeps_status_and_cause(self) -> None:
        error = ApiException(status=403)
        self.batch_api.read_namespaced_job.side_effect = error

        with self.assertRaises(KubernetesError) as raised:
            self.gateway.read_job("nightly-1700000000")

        self.assertEqual(raised.exception.status, 403)
        self.assertIs(raised.exception.__cause__, error)

    def test_stream_closes_and_releases_response(self) -> None:
        response = Mock()
        response.stream.return_value = iter([b"first\n", b"second\n"])
        self.core_api.read_namespaced_pod_log.return_value = response

        lines = list(self.gateway.stream_logs("pod-1"))

        self.assertEqual(lines, [b"first\n", b"second\n"])
        response.close.assert_called_once_with()
        response.release_conn.assert_called_once_with()
        self.core_api.read_namespaced_pod_log.assert_called_once_with(
            name="pod-1",
            namespace=self.settings.namespace,
            follow=True,
            _preload_content=False,
            _request_timeout=11,
        )

    def test_lazy_initialization_falls_back_to_kubeconfig(self) -> None:
        gateway = KubernetesGateway(self.settings)
        api_client = Mock()
        client_configuration = Mock(proxy="http://proxy.example:8080")
        with (
            patch(
                "metadata_qa.kubernetes_gateway.client.Configuration",
                return_value=client_configuration,
            ),
            patch(
                "metadata_qa.kubernetes_gateway.config.load_incluster_config",
                side_effect=ConfigException("not in cluster"),
            ),
            patch("metadata_qa.kubernetes_gateway.config.load_kube_config") as load_kube_config,
            patch(
                "metadata_qa.kubernetes_gateway.client.ApiClient", return_value=api_client
            ) as api_type,
            patch("metadata_qa.kubernetes_gateway.client.CoreV1Api") as core_type,
            patch("metadata_qa.kubernetes_gateway.client.BatchV1Api") as batch_type,
        ):
            clients = gateway._ensure_clients()

        load_kube_config.assert_called_once_with(client_configuration=client_configuration)
        self.assertIs(clients[0], api_client)
        self.assertEqual(client_configuration.proxy, "http://proxy.example:8080")
        api_type.assert_called_once_with(configuration=client_configuration)
        core_type.assert_called_once_with(api_client)
        batch_type.assert_called_once_with(api_client)

    def test_incluster_api_bypasses_environment_proxy_without_disabling_tls(self) -> None:
        gateway = KubernetesGateway(self.settings)
        api_client = Mock()
        client_configuration = Mock(
            host="https://172.30.0.1:443",
            proxy="http://proxy.example:8080",
            verify_ssl=True,
        )
        with (
            patch(
                "metadata_qa.kubernetes_gateway.client.Configuration",
                return_value=client_configuration,
            ),
            patch("metadata_qa.kubernetes_gateway.config.load_incluster_config") as load_incluster,
            patch(
                "metadata_qa.kubernetes_gateway.client.ApiClient", return_value=api_client
            ) as api_type,
            patch("metadata_qa.kubernetes_gateway.client.CoreV1Api"),
            patch("metadata_qa.kubernetes_gateway.client.BatchV1Api"),
        ):
            gateway._ensure_clients()

        load_incluster.assert_called_once_with(client_configuration=client_configuration)
        self.assertIsNone(client_configuration.proxy)
        self.assertTrue(client_configuration.verify_ssl)
        self.assertEqual(client_configuration.host, "https://172.30.0.1:443")
        api_type.assert_called_once_with(configuration=client_configuration)

    def test_missing_cluster_configuration_has_semantic_error(self) -> None:
        gateway = KubernetesGateway(self.settings)
        client_configuration = Mock()
        with (
            patch(
                "metadata_qa.kubernetes_gateway.client.Configuration",
                return_value=client_configuration,
            ),
            patch(
                "metadata_qa.kubernetes_gateway.config.load_incluster_config",
                side_effect=ConfigException("not in cluster"),
            ),
            patch(
                "metadata_qa.kubernetes_gateway.config.load_kube_config",
                side_effect=ConfigException("no kubeconfig"),
            ),
            self.assertRaises(KubernetesUnavailableError),
        ):
            gateway._ensure_clients()

    def test_pods_and_historical_logs_are_normalized_with_timeouts(self) -> None:
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name="pod-1"),
            status=client.V1PodStatus(phase="Running"),
        )
        self.core_api.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
        self.core_api.read_namespaced_pod_log.return_value = "log text"

        pods = self.gateway.list_pods("nightly-1700000000")
        logs = self.gateway.read_logs("pod-1", tail_lines=25)

        self.assertEqual([(item.name, item.phase) for item in pods], [("pod-1", "Running")])
        self.assertEqual(logs, "log text")
        self.core_api.list_namespaced_pod.assert_called_once_with(
            namespace=self.settings.namespace,
            label_selector="job-name=nightly-1700000000",
            _request_timeout=7,
        )
        self.core_api.read_namespaced_pod_log.assert_called_once_with(
            name="pod-1",
            namespace=self.settings.namespace,
            tail_lines=25,
            timestamps=True,
            _request_timeout=7,
        )

    def test_historical_logs_decode_an_additionally_escaped_line_separator(self) -> None:
        self.core_api.read_namespaced_pod_log.return_value = (
            r"2026-07-15 first line\r\n2026-07-15 second line\n"
        )

        logs = self.gateway.read_logs("pod-1", tail_lines=25)

        self.assertEqual(logs, "2026-07-15 first line\n2026-07-15 second line\n")

    def test_historical_logs_do_not_decode_other_literal_escapes(self) -> None:
        self.core_api.read_namespaced_pod_log.return_value = r"path=C:\new\task"

        logs = self.gateway.read_logs("pod-1", tail_lines=25)

        self.assertEqual(logs, r"path=C:\new\task")

    def test_delete_404_is_idempotent_and_readiness_uses_timeout(self) -> None:
        self.batch_api.delete_namespaced_job.side_effect = ApiException(status=404)

        self.assertFalse(self.gateway.delete_job("nightly-1700000000", foreground=True))
        self.gateway.check_ready()

        self.batch_api.read_namespaced_cron_job.assert_called_once_with(
            name="nightly",
            namespace=self.settings.namespace,
            _request_timeout=7,
        )


if __name__ == "__main__":
    unittest.main()
