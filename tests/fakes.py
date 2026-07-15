from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from metadata_qa.models import JobRecord, JobStatus, PodRecord


class FakeGateway:
    def __init__(self) -> None:
        self.jobs: list[JobRecord] = []
        self.pods: dict[str, list[PodRecord]] = {}
        self.logs: dict[str, str] = {}
        self.stream_lines: dict[str, list[bytes | str]] = {}
        self.list_jobs_calls = 0
        self.created_names: list[str] = []
        self.deleted: list[tuple[str, bool]] = []
        self.ready = True

    def list_jobs(self) -> list[JobRecord]:
        self.list_jobs_calls += 1
        return list(self.jobs)

    def read_job(self, name: str) -> JobRecord | None:
        return next((job for job in self.jobs if job.name == name), None)

    def create_job_from_cronjob(self, name: str) -> JobRecord:
        self.created_names.append(name)
        job = JobRecord(name=name, status=JobStatus.PENDING, is_managed=True)
        self.jobs.append(job)
        return job

    def delete_job(self, name: str, *, foreground: bool) -> bool:
        self.deleted.append((name, foreground))
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.name != name]
        return len(self.jobs) != before

    def list_pods(self, job_name: str) -> list[PodRecord]:
        return list(self.pods.get(job_name, []))

    def read_logs(self, pod_name: str, *, tail_lines: int) -> str:
        lines = self.logs.get(pod_name, "").splitlines()
        return "\n".join(lines[-tail_lines:])

    def stream_logs(self, pod_name: str) -> Iterator[bytes | str]:
        yield from self.stream_lines.get(pod_name, [])

    def check_ready(self) -> None:
        if not self.ready:
            from metadata_qa.kubernetes_gateway import KubernetesError

            raise KubernetesError("fake readiness")

    def set_status(self, job_name: str, status: JobStatus) -> None:
        self.jobs = [
            replace(job, status=status) if job.name == job_name else job for job in self.jobs
        ]


class FakeClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds
