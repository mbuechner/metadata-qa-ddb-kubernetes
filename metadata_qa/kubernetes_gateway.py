"""Narrow, timeout-aware boundary around the Kubernetes Python client."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Protocol, cast

from kubernetes import client, config
from kubernetes.client import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from metadata_qa.config import Settings
from metadata_qa.models import JobRecord, JobStatus, PodRecord

LOGGER = logging.getLogger(__name__)

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "metadata-qa-ddb-kubernetes"
CRONJOB_LABEL = "metadata-qa-ddb-kubernetes/cronjob"


class KubernetesError(RuntimeError):
    """An infrastructure failure with safe metadata for transport-layer mapping."""

    def __init__(self, operation: str, *, status: int | None = None) -> None:
        super().__init__(f"Kubernetes operation failed: {operation}")
        self.operation = operation
        self.status = status


class KubernetesUnavailableError(KubernetesError):
    """Kubernetes client configuration is unavailable."""


class JobGateway(Protocol):
    """Infrastructure operations required by the job service."""

    def list_jobs(self) -> list[JobRecord]: ...

    def read_job(self, name: str) -> JobRecord | None: ...

    def create_job_from_cronjob(self, name: str) -> JobRecord: ...

    def delete_job(self, name: str, *, foreground: bool) -> bool: ...

    def list_pods(self, job_name: str) -> list[PodRecord]: ...

    def read_logs(self, pod_name: str, *, tail_lines: int) -> str: ...

    def stream_logs(self, pod_name: str) -> Iterator[bytes | str]: ...

    def check_ready(self) -> None: ...


def _job_status(job: client.V1Job) -> JobStatus:
    status = job.status
    if status is None:
        return JobStatus.UNKNOWN
    if status.active and status.active > 0:
        return JobStatus.RUNNING
    if status.succeeded and status.succeeded > 0:
        return JobStatus.SUCCEEDED
    if status.failed and status.failed > 0:
        return JobStatus.FAILED
    return JobStatus.PENDING


def _metadata_name(value: Any) -> str:
    metadata = getattr(value, "metadata", None)
    return cast(str, getattr(metadata, "name", None) or "")


class KubernetesGateway:
    """Lazily creates Kubernetes clients and normalizes client model objects."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._initialization_lock = threading.Lock()
        self._api_client: client.ApiClient | None = None
        self._core_api: client.CoreV1Api | None = None
        self._batch_api: client.BatchV1Api | None = None
        self._legacy_name_pattern = re.compile(
            rf"^{re.escape(settings.cronjob_name)}-(?P<timestamp>[0-9]{{10,13}})$"
        )

    def _ensure_clients(self) -> tuple[client.ApiClient, client.CoreV1Api, client.BatchV1Api]:
        if (
            self._api_client is not None
            and self._core_api is not None
            and self._batch_api is not None
        ):
            return self._api_client, self._core_api, self._batch_api

        with self._initialization_lock:
            if self._api_client is None:
                loaded_in_cluster = False
                client_configuration = client.Configuration()
                try:
                    try:
                        config.load_incluster_config(client_configuration=client_configuration)
                        loaded_in_cluster = True
                        LOGGER.info("Loaded in-cluster Kubernetes configuration")
                    except config.ConfigException:
                        config.load_kube_config(client_configuration=client_configuration)
                        LOGGER.info("Loaded Kubernetes configuration from kubeconfig")
                except config.ConfigException as exc:
                    raise KubernetesUnavailableError("load configuration") from exc
                if loaded_in_cluster and client_configuration.proxy:
                    # The API service is cluster-local. Routing it through an environment proxy
                    # commonly causes timeouts when NO_PROXY does not contain the service IP.
                    # This intentionally does not alter HTTPS or certificate verification.
                    LOGGER.info(
                        "Bypassing environment proxy for in-cluster Kubernetes API",
                        extra={"kubernetes_api_host": client_configuration.host},
                    )
                    client_configuration.proxy = None
                self._api_client = client.ApiClient(configuration=client_configuration)
                self._core_api = client.CoreV1Api(self._api_client)
                self._batch_api = client.BatchV1Api(self._api_client)

        return cast(
            tuple[client.ApiClient, client.CoreV1Api, client.BatchV1Api],
            (self._api_client, self._core_api, self._batch_api),
        )

    @contextmanager
    def _operation(self, name: str) -> Iterator[None]:
        try:
            yield
        except KubernetesError:
            raise
        except ApiException as exc:
            raise KubernetesError(name, status=exc.status) from exc
        except (OSError, TimeoutError, Urllib3HTTPError) as exc:
            raise KubernetesError(name) from exc

    def _is_managed(self, job: client.V1Job) -> tuple[bool, bool]:
        metadata = job.metadata
        labels = metadata.labels if metadata and metadata.labels else {}
        explicitly_managed = (
            labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE
            and labels.get(CRONJOB_LABEL) == self._settings.cronjob_name
        )
        legacy = bool(self._legacy_name_pattern.fullmatch(_metadata_name(job)))
        return explicitly_managed or legacy, legacy and not explicitly_managed

    def _normalize_job(self, job: client.V1Job) -> JobRecord:
        metadata = job.metadata
        status = job.status
        managed, legacy = self._is_managed(job)
        return JobRecord(
            name=_metadata_name(job),
            status=_job_status(job),
            start_time=cast(datetime | None, getattr(status, "start_time", None)),
            completion_time=cast(datetime | None, getattr(status, "completion_time", None)),
            is_terminating=bool(getattr(metadata, "deletion_timestamp", None)),
            is_managed=managed,
            is_legacy=legacy,
        )

    def list_jobs(self) -> list[JobRecord]:
        _, _, batch_api = self._ensure_clients()
        with self._operation("list jobs"):
            result = batch_api.list_namespaced_job(
                namespace=self._settings.namespace,
                _request_timeout=self._settings.kubernetes_request_timeout_seconds,
            )
            return [self._normalize_job(job) for job in result.items or []]
        raise AssertionError("unreachable")

    def read_job(self, name: str) -> JobRecord | None:
        _, _, batch_api = self._ensure_clients()
        try:
            with self._operation("read job"):
                job = batch_api.read_namespaced_job(
                    name=name,
                    namespace=self._settings.namespace,
                    _request_timeout=self._settings.kubernetes_request_timeout_seconds,
                )
                return self._normalize_job(job)
        except KubernetesError as exc:
            if exc.status == 404:
                return None
            raise

    def create_job_from_cronjob(self, name: str) -> JobRecord:
        api_client, _, batch_api = self._ensure_clients()
        with self._operation("create job"):
            cronjob = batch_api.read_namespaced_cron_job(
                name=self._settings.cronjob_name,
                namespace=self._settings.namespace,
                _request_timeout=self._settings.kubernetes_request_timeout_seconds,
            )
            job_template = cronjob.spec.job_template if cronjob.spec else None
            template_spec = job_template.spec if job_template else None
            if template_spec is None:
                raise KubernetesError("read CronJob template")

            job_spec = api_client.sanitize_for_serialization(template_spec)
            if not isinstance(job_spec, dict):
                raise KubernetesError("serialize CronJob template")
            pod_spec = job_spec.setdefault("template", {}).setdefault("spec", {})
            pod_spec.setdefault("restartPolicy", "Never")
            body = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": name,
                    "namespace": self._settings.namespace,
                    "labels": {
                        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                        CRONJOB_LABEL: self._settings.cronjob_name,
                    },
                },
                "spec": job_spec,
            }
            created = batch_api.create_namespaced_job(
                namespace=self._settings.namespace,
                body=body,
                _request_timeout=self._settings.kubernetes_request_timeout_seconds,
            )
            return self._normalize_job(created)
        raise AssertionError("unreachable")

    def delete_job(self, name: str, *, foreground: bool) -> bool:
        _, _, batch_api = self._ensure_clients()
        try:
            with self._operation("delete job"):
                batch_api.delete_namespaced_job(
                    name=name,
                    namespace=self._settings.namespace,
                    body=client.V1DeleteOptions(
                        propagation_policy="Foreground" if foreground else "Background"
                    ),
                    _request_timeout=self._settings.kubernetes_request_timeout_seconds,
                )
                return True
        except KubernetesError as exc:
            if exc.status == 404:
                return False
            raise

    def list_pods(self, job_name: str) -> list[PodRecord]:
        _, core_api, _ = self._ensure_clients()
        with self._operation("list job pods"):
            result = core_api.list_namespaced_pod(
                namespace=self._settings.namespace,
                label_selector=f"job-name={job_name}",
                _request_timeout=self._settings.kubernetes_request_timeout_seconds,
            )
            return [
                PodRecord(
                    name=_metadata_name(pod),
                    phase=cast(str, getattr(pod.status, "phase", None) or JobStatus.UNKNOWN.value),
                    creation_time=cast(
                        datetime | None, getattr(pod.metadata, "creation_timestamp", None)
                    ),
                )
                for pod in result.items or []
                if _metadata_name(pod)
            ]
        raise AssertionError("unreachable")

    def read_logs(self, pod_name: str, *, tail_lines: int) -> str:
        _, core_api, _ = self._ensure_clients()
        with self._operation("read pod logs"):
            return cast(
                str,
                core_api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=self._settings.namespace,
                    tail_lines=tail_lines,
                    timestamps=True,
                    _request_timeout=self._settings.kubernetes_request_timeout_seconds,
                ),
            )
        raise AssertionError("unreachable")

    def stream_logs(self, pod_name: str) -> Iterator[bytes | str]:
        _, core_api, _ = self._ensure_clients()
        response: Any = None
        try:
            with self._operation("stream pod logs"):
                response = core_api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=self._settings.namespace,
                    follow=True,
                    _preload_content=False,
                    _request_timeout=self._settings.log_stream_request_timeout_seconds,
                )
                yield from cast(Iterator[bytes | str], response.stream())
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                release_connection = getattr(response, "release_conn", None)
                if callable(release_connection):
                    release_connection()

    def check_ready(self) -> None:
        _, _, batch_api = self._ensure_clients()
        with self._operation("read managed CronJob"):
            batch_api.read_namespaced_cron_job(
                name=self._settings.cronjob_name,
                namespace=self._settings.namespace,
                _request_timeout=self._settings.kubernetes_request_timeout_seconds,
            )
