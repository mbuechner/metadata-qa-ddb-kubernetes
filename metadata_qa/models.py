"""Small domain models independent from Flask and the Kubernetes client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NotRequired, TypedDict


class JobStatus(StrEnum):
    UNKNOWN = "Unknown"
    PENDING = "Pending"
    STARTING = "Starting"
    RUNNING = "Running"
    STOPPING = "Stopping"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELED = "Canceled"
    IDLE = "Idle"
    ERROR = "Error"


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Normalized job data consumed by service and transport layers."""

    name: str
    status: JobStatus
    start_time: datetime | None = None
    completion_time: datetime | None = None
    is_terminating: bool = False
    is_managed: bool = False
    is_legacy: bool = False

    @property
    def is_completed(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}

    def to_api_dict(self) -> JobPayload:
        return {
            "name": self.name,
            "status": self.status.value,
            "startTime": self.start_time.isoformat() if self.start_time else None,
            "completionTime": self.completion_time.isoformat() if self.completion_time else None,
            "isTerminating": self.is_terminating,
        }


@dataclass(frozen=True, slots=True)
class PodRecord:
    name: str
    phase: str
    creation_time: datetime | None = None


class JobPayload(TypedDict):
    name: str
    status: str
    startTime: str | None
    completionTime: str | None
    isTerminating: bool


class StatusPayload(TypedDict):
    message: str
    status: str


class LogPayload(TypedDict):
    message: str


class JobListPayload(TypedDict):
    namespace: str
    cronjobName: str
    activeJob: str | None
    activeJobStatus: str | None
    activeJobTerminating: bool | None
    jobs: list[JobPayload]


class JobLogsPayload(TypedDict):
    job: str
    pod: str | None
    status: str
    logs: str


class DeletePayload(TypedDict):
    deleted: bool
    job: str
    alreadyAbsent: NotRequired[bool]
