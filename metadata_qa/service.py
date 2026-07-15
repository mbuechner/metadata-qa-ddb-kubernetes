"""Application service for the lifecycle of manually triggered Kubernetes jobs."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from metadata_qa.config import Settings
from metadata_qa.kubernetes_gateway import JobGateway, KubernetesError
from metadata_qa.models import (
    DeletePayload,
    JobListPayload,
    JobLogsPayload,
    JobRecord,
    JobStatus,
    PodRecord,
)

LOGGER = logging.getLogger(__name__)

type EventPayload = dict[str, object]
type EventPublisher = Callable[[str, EventPayload], None]
type BackgroundSpawner = Callable[[Callable[[], None]], object]


class JobNotFoundError(LookupError):
    """The requested job does not exist or is not managed by this application."""


def _thread_spawner(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


@dataclass(slots=True)
class _Lifecycle:
    job_name: str
    cancel: threading.Event
    starting: bool = True


@dataclass(slots=True)
class _LogStream:
    job_name: str
    cancel: threading.Event


class RuntimeState:
    """Synchronizes process-local lifecycle and log-stream state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lifecycle: _Lifecycle | None = None
        self._log_stream: _LogStream | None = None
        self._termination_monitors: set[str] = set()

    def reserve(self, job_name: str) -> tuple[bool, _Lifecycle]:
        with self._lock:
            if self._lifecycle is not None:
                return False, self._lifecycle
            lifecycle = _Lifecycle(job_name=job_name, cancel=threading.Event())
            self._lifecycle = lifecycle
            return True, lifecycle

    def owns(self, lifecycle: _Lifecycle) -> bool:
        with self._lock:
            return self._lifecycle is lifecycle

    def mark_created(self, lifecycle: _Lifecycle) -> None:
        with self._lock:
            if self._lifecycle is lifecycle:
                lifecycle.starting = False

    def current_job(self) -> str | None:
        with self._lock:
            return self._lifecycle.job_name if self._lifecycle else None

    def adopt(self, job_name: str) -> _Lifecycle:
        with self._lock:
            if self._lifecycle is not None and self._lifecycle.job_name == job_name:
                self._lifecycle.starting = False
                return self._lifecycle
            if self._lifecycle is not None:
                self._lifecycle.cancel.set()
            lifecycle = _Lifecycle(job_name=job_name, cancel=threading.Event(), starting=False)
            self._lifecycle = lifecycle
            return lifecycle

    def synchronize(self, active_job: str | None) -> None:
        with self._lock:
            if active_job is not None:
                if self._lifecycle is None or self._lifecycle.job_name != active_job:
                    if self._lifecycle is not None:
                        self._lifecycle.cancel.set()
                    self._lifecycle = _Lifecycle(
                        job_name=active_job, cancel=threading.Event(), starting=False
                    )
                return
            if self._lifecycle is not None and not self._lifecycle.starting:
                self._lifecycle.cancel.set()
                self._lifecycle = None

    def request_cancel(self, job_name: str) -> None:
        with self._lock:
            if self._lifecycle is not None and self._lifecycle.job_name == job_name:
                self._lifecycle.cancel.set()
            if self._log_stream is not None and self._log_stream.job_name == job_name:
                self._log_stream.cancel.set()

    def clear(self, job_name: str, lifecycle: _Lifecycle | None = None) -> None:
        with self._lock:
            if self._lifecycle is None or self._lifecycle.job_name != job_name:
                return
            if lifecycle is not None and self._lifecycle is not lifecycle:
                return
            self._lifecycle.cancel.set()
            self._lifecycle = None

    def begin_log_stream(self, job_name: str) -> _LogStream:
        with self._lock:
            if self._log_stream is not None:
                self._log_stream.cancel.set()
            stream = _LogStream(job_name=job_name, cancel=threading.Event())
            self._log_stream = stream
            return stream

    def finish_log_stream(self, stream: _LogStream) -> None:
        with self._lock:
            if self._log_stream is stream:
                self._log_stream = None

    def begin_termination_monitor(self, job_name: str) -> bool:
        with self._lock:
            if job_name in self._termination_monitors:
                return False
            self._termination_monitors.add(job_name)
            return True

    def finish_termination_monitor(self, job_name: str) -> None:
        with self._lock:
            self._termination_monitors.discard(job_name)


class JobService:
    """Coordinates job operations without depending on Flask or Socket.IO."""

    def __init__(
        self,
        settings: Settings,
        gateway: JobGateway,
        *,
        publish: EventPublisher | None = None,
        spawn: BackgroundSpawner = _thread_spawner,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._gateway = gateway
        self._publish = publish or (lambda _event, _payload: None)
        self._spawn = spawn
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._sleep = sleep
        self._state = RuntimeState()

    @property
    def current_job(self) -> str | None:
        return self._state.current_job()

    def set_event_transport(
        self,
        *,
        publish: EventPublisher,
        spawn: BackgroundSpawner,
    ) -> None:
        """Attach the server event transport after Flask-SocketIO is initialized."""
        self._publish = publish
        self._spawn = spawn

    @staticmethod
    def _sort_key(job: JobRecord) -> tuple[bool, float, str]:
        timestamp = job.start_time.timestamp() if job.start_time is not None else 0.0
        return job.start_time is not None, timestamp, job.name

    @staticmethod
    def _select_pod(pods: list[PodRecord]) -> PodRecord | None:
        if not pods:
            return None
        phase_priority = {"Running": 3, "Pending": 2, "Succeeded": 1, "Failed": 1}
        return max(
            pods,
            key=lambda pod: (
                phase_priority.get(pod.phase, 0),
                pod.creation_time.timestamp() if pod.creation_time else 0.0,
                pod.name,
            ),
        )

    def _managed_jobs(self) -> list[JobRecord]:
        return sorted(
            (job for job in self._gateway.list_jobs() if job.is_managed),
            key=self._sort_key,
            reverse=True,
        )

    @staticmethod
    def _active_job(jobs: list[JobRecord]) -> JobRecord | None:
        return next((job for job in jobs if not job.is_completed), None)

    def list_jobs(self) -> JobListPayload:
        jobs = self._managed_jobs()
        active = self._active_job(jobs)
        self._state.synchronize(active.name if active else None)
        return {
            "namespace": self.settings.namespace,
            "cronjobName": self.settings.cronjob_name,
            "activeJob": active.name if active else self.current_job,
            "activeJobStatus": active.status.value if active else None,
            "activeJobTerminating": active.is_terminating if active else None,
            "jobs": [job.to_api_dict() for job in jobs],
        }

    def get_logs(self, job_name: str, *, tail_lines: int) -> JobLogsPayload:
        self._validate_job_name(job_name)
        job = self._gateway.read_job(job_name)
        if job is None or not job.is_managed:
            raise JobNotFoundError(job_name)
        pod = self._select_pod(self._gateway.list_pods(job_name))
        if pod is None:
            return {
                "job": job_name,
                "pod": None,
                "logs": "(no pod found for job)",
                "status": JobStatus.UNKNOWN.value,
            }
        return {
            "job": job_name,
            "pod": pod.name,
            "status": pod.phase,
            "logs": self._gateway.read_logs(pod.name, tail_lines=tail_lines),
        }

    def request_start(self) -> object:
        """Start orchestration in the configured background-task implementation."""
        return self._spawn(self.start_job)

    def start_job(self) -> None:
        created = False
        lifecycle: _Lifecycle | None = None
        job_name = ""
        try:
            existing = self._active_job(self._managed_jobs())
            if existing is not None:
                self._state.adopt(existing.name)
                status = JobStatus.STOPPING if existing.is_terminating else existing.status
                message = (
                    f"Job {existing.name} wird noch terminiert. Bitte warten."
                    if existing.is_terminating
                    else f"Job {existing.name} läuft bereits."
                )
                self._status(message, status)
                return

            job_name = f"{self.settings.cronjob_name}-{int(self._wall_time())}"
            reserved, lifecycle = self._state.reserve(job_name)
            if not reserved:
                self._status(
                    f"Job {lifecycle.job_name} läuft bereits.",
                    JobStatus.STARTING if lifecycle.starting else JobStatus.RUNNING,
                )
                return

            self._gateway.create_job_from_cronjob(job_name)
            created = True
            self._state.mark_created(lifecycle)
            self._status(f"Job {job_name} wird gestartet...", JobStatus.STARTING)

            if lifecycle.cancel.is_set():
                self._cancel_created_job(job_name)
                return

            deadline = self._monotonic() + self.settings.start_pod_timeout_seconds
            last_phase = ""
            while self._monotonic() < deadline:
                if lifecycle.cancel.is_set() or not self._state.owns(lifecycle):
                    return
                pod = self._select_pod(self._gateway.list_pods(job_name))
                if pod is not None:
                    if pod.phase != last_phase:
                        self._status(f"Job-Status: {pod.phase}", self._pod_status(pod.phase))
                        last_phase = pod.phase
                    if pod.phase == JobStatus.RUNNING.value:
                        self._start_log_stream(job_name)
                        return
                    if pod.phase == JobStatus.FAILED.value:
                        self._status("Job konnte nicht gestartet werden", JobStatus.ERROR)
                        self._state.clear(job_name, lifecycle)
                        return
                self._sleep(1)

            # Keep the job cancellable: Kubernetes may still schedule it after this UI timeout.
            self._status(
                f"Kein Pod innerhalb von {self.settings.start_pod_timeout_seconds}s für Job "
                f"{job_name} gestartet. Der Job kann weiterhin abgebrochen werden.",
                JobStatus.ERROR,
            )
        except KubernetesError as exc:
            LOGGER.error(
                "Kubernetes failure while starting job %s: operation=%s status=%s",
                job_name or "<not reserved>",
                exc.operation,
                exc.status,
            )
            self._status("Kubernetes-Fehler beim Starten des Jobs.", JobStatus.ERROR)
            if lifecycle is not None and not created:
                self._state.clear(job_name, lifecycle)

    def _cancel_created_job(self, job_name: str) -> None:
        try:
            self._gateway.delete_job(job_name, foreground=True)
        except KubernetesError as exc:
            LOGGER.error(
                "Could not delete job %s after concurrent cancellation: operation=%s status=%s",
                job_name,
                exc.operation,
                exc.status,
            )
        self._start_termination_monitor(job_name)

    def cancel_current_job(self) -> None:
        try:
            current = self.current_job
            if current is None:
                active = self._active_job(self._managed_jobs())
                current = active.name if active else None
                if current is not None:
                    self._state.adopt(current)
            if current is None:
                self._status("Kein aktiver Job zum Abbrechen gefunden.", JobStatus.IDLE)
                return

            self._status(f"Job {current} wird abgebrochen...", JobStatus.STOPPING)
            self._state.request_cancel(current)
            deleted = self._gateway.delete_job(current, foreground=True)
            if deleted:
                self._start_termination_monitor(current)
            else:
                self._state.clear(current)
                self._status(f"Job {current} ist bereits gelöscht.", JobStatus.CANCELED)
        except KubernetesError as exc:
            LOGGER.error(
                "Kubernetes failure while canceling current job: operation=%s status=%s",
                exc.operation,
                exc.status,
            )
            self._status("Kubernetes-Fehler beim Abbrechen des Jobs.", JobStatus.ERROR)

    def delete_job(self, job_name: str) -> DeletePayload:
        self._validate_job_name(job_name)
        job = self._gateway.read_job(job_name)
        if job is None:
            return {"deleted": True, "job": job_name, "alreadyAbsent": True}
        if not job.is_managed:
            raise JobNotFoundError(job_name)

        needs_monitor = not job.is_completed or job.is_terminating
        if needs_monitor:
            self._state.adopt(job_name)
            self._state.request_cancel(job_name)
        deleted = self._gateway.delete_job(job_name, foreground=needs_monitor)
        if needs_monitor and deleted:
            self._start_termination_monitor(job_name)
        elif not deleted:
            self._state.clear(job_name)
        return {"deleted": True, "job": job_name, "alreadyAbsent": not deleted}

    def check_ready(self) -> None:
        self._gateway.check_ready()

    def _start_termination_monitor(self, job_name: str) -> None:
        if self._state.begin_termination_monitor(job_name):
            self._spawn(lambda: self._wait_for_termination(job_name))

    def _wait_for_termination(self, job_name: str) -> None:
        deadline = self._monotonic() + self.settings.job_termination_timeout_seconds
        try:
            while self._monotonic() < deadline:
                try:
                    job = self._gateway.read_job(job_name)
                    if job is None or job.is_completed:
                        self._state.clear(job_name)
                        self._status(f"Job {job_name} ist terminiert.", JobStatus.CANCELED)
                        return
                except KubernetesError:
                    LOGGER.warning(
                        "Transient failure while waiting for termination of %s", job_name
                    )
                self._sleep(2)
            self._status(
                f"Zeitüberschreitung beim Warten auf die Terminierung von Job {job_name}.",
                JobStatus.ERROR,
            )
        finally:
            self._state.finish_termination_monitor(job_name)

    def _start_log_stream(self, job_name: str) -> None:
        stream = self._state.begin_log_stream(job_name)
        self._spawn(lambda: self._broadcast_logs(stream))

    def _broadcast_logs(self, stream: _LogStream) -> None:
        try:
            while not stream.cancel.is_set():
                try:
                    pod = self._select_pod(self._gateway.list_pods(stream.job_name))
                    if pod is None:
                        job = self._gateway.read_job(stream.job_name)
                        if job is None or job.is_completed:
                            if job is not None:
                                self._status(
                                    f"Job {stream.job_name} Status: {job.status.value}", job.status
                                )
                            self._state.clear(stream.job_name)
                            return
                        self._sleep(1)
                        continue

                    if pod.phase in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value}:
                        status = self._pod_status(pod.phase)
                        self._status(f"Pod {pod.name} Status: {pod.phase}", status)
                        self._state.clear(stream.job_name)
                        return
                    if pod.phase != JobStatus.RUNNING.value:
                        self._sleep(1)
                        continue

                    for raw_line in self._gateway.stream_logs(pod.name):
                        if stream.cancel.is_set():
                            return
                        line = (
                            raw_line.decode("utf-8", errors="replace")
                            if isinstance(raw_line, bytes)
                            else raw_line
                        )
                        self._publish("log_update", {"message": line.rstrip("\r\n")})
                except KubernetesError:
                    if stream.cancel.is_set():
                        return
                    LOGGER.warning(
                        "Live log stream for %s was interrupted; retrying", stream.job_name
                    )
                    self._sleep(1)
        finally:
            self._state.finish_log_stream(stream)

    def _status(self, message: str, status: JobStatus) -> None:
        self._publish("status_update", {"message": message, "status": status.value})

    def _validate_job_name(self, job_name: str) -> None:
        if not self.settings.is_managed_job_name_candidate(job_name):
            raise JobNotFoundError(job_name)

    @staticmethod
    def _pod_status(phase: str) -> JobStatus:
        try:
            return JobStatus(phase)
        except ValueError:
            return JobStatus.UNKNOWN
