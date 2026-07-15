from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import Mock

from metadata_qa.config import Settings
from metadata_qa.kubernetes_gateway import KubernetesError
from metadata_qa.models import JobRecord, JobStatus, PodRecord
from metadata_qa.service import JobNotFoundError, JobService
from tests.fakes import FakeClock, FakeGateway


class JobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.from_env(
            {
                "CRONJOB_NAME": "nightly",
                "START_POD_TIMEOUT_SECONDS": "2",
                "JOB_TERMINATION_TIMEOUT_SECONDS": "4",
            }
        )
        self.gateway = FakeGateway()
        self.clock = FakeClock()
        self.events: list[tuple[str, dict[str, object]]] = []
        self.spawned: list[object] = []

        def spawn(target):
            self.spawned.append(target)
            return target

        self.service = JobService(
            self.settings,
            self.gateway,
            publish=lambda event, payload: self.events.append((event, payload)),
            spawn=spawn,
            monotonic=self.clock.monotonic,
            wall_time=lambda: 1_700_000_000,
            sleep=self.clock.sleep,
        )

    def test_list_jobs_uses_one_cluster_read_and_filters_unmanaged_jobs(self) -> None:
        self.gateway.jobs = [
            JobRecord(
                name="nightly-1700000000",
                status=JobStatus.RUNNING,
                start_time=datetime(2025, 1, 1, tzinfo=UTC),
                is_managed=True,
            ),
            JobRecord(name="other-1700000000", status=JobStatus.RUNNING, is_managed=False),
        ]

        payload = self.service.list_jobs()

        self.assertEqual(self.gateway.list_jobs_calls, 1)
        self.assertEqual(payload["activeJob"], "nightly-1700000000")
        self.assertEqual([job["name"] for job in payload["jobs"]], ["nightly-1700000000"])
        self.assertEqual(self.service.current_job, "nightly-1700000000")

    def test_existing_active_job_prevents_a_second_start(self) -> None:
        self.gateway.jobs = [
            JobRecord(name="nightly-1700000000", status=JobStatus.PENDING, is_managed=True)
        ]

        self.service.start_job()

        self.assertEqual(self.gateway.created_names, [])
        self.assertEqual(self.events[-1][1]["status"], JobStatus.PENDING.value)

    def test_start_timeout_keeps_job_cancellable(self) -> None:
        self.service.start_job()

        self.assertEqual(self.gateway.created_names, ["nightly-1700000000"])
        self.assertEqual(self.service.current_job, "nightly-1700000000")
        self.assertEqual(self.events[-1][1]["status"], JobStatus.ERROR.value)
        self.assertIn("weiterhin abgebrochen", str(self.events[-1][1]["message"]))

    def test_failed_pod_clears_reserved_job(self) -> None:
        self.gateway.pods["nightly-1700000000"] = [PodRecord("pod-1", "Failed")]

        self.service.start_job()

        self.assertIsNone(self.service.current_job)
        self.assertEqual(self.events[-1][1]["status"], JobStatus.ERROR.value)

    def test_logs_hide_unmanaged_job(self) -> None:
        self.gateway.jobs = [
            JobRecord(name="nightly-1700000000", status=JobStatus.SUCCEEDED, is_managed=False)
        ]

        with self.assertRaises(JobNotFoundError):
            self.service.get_logs("nightly-1700000000", tail_lines=10)

    def test_delete_completed_job_uses_background_propagation(self) -> None:
        self.gateway.jobs = [
            JobRecord(name="nightly-1700000000", status=JobStatus.SUCCEEDED, is_managed=True)
        ]

        result = self.service.delete_job("nightly-1700000000")

        self.assertTrue(result["deleted"])
        self.assertEqual(self.gateway.deleted, [("nightly-1700000000", False)])

    def test_invalid_job_name_never_reaches_gateway(self) -> None:
        with self.assertRaises(JobNotFoundError):
            self.service.delete_job("nightly-admin")

        self.assertEqual(self.gateway.deleted, [])

    def test_cancel_recovers_cluster_state_after_restart(self) -> None:
        job_name = "nightly-1700000000"
        self.gateway.jobs = [JobRecord(name=job_name, status=JobStatus.RUNNING, is_managed=True)]

        self.service.cancel_current_job()

        self.assertEqual(self.gateway.deleted, [(job_name, True)])
        self.assertEqual(len(self.spawned), 1)
        target = self.spawned.pop()
        target()
        self.assertIsNone(self.service.current_job)
        self.assertEqual(self.events[-1][1]["status"], JobStatus.CANCELED.value)

    def test_cancel_without_active_job_reports_idle(self) -> None:
        self.service.cancel_current_job()

        self.assertEqual(self.events[-1][1]["status"], JobStatus.IDLE.value)

    def test_active_delete_uses_foreground_and_monitor(self) -> None:
        job_name = "nightly-1700000000"
        self.gateway.jobs = [JobRecord(name=job_name, status=JobStatus.RUNNING, is_managed=True)]

        result = self.service.delete_job(job_name)

        self.assertTrue(result["deleted"])
        self.assertEqual(self.gateway.deleted, [(job_name, True)])
        self.assertEqual(len(self.spawned), 1)

    def test_absent_delete_is_idempotent(self) -> None:
        result = self.service.delete_job("nightly-1700000000")

        self.assertTrue(result["deleted"])
        self.assertTrue(result["alreadyAbsent"])

    def test_logs_without_pod_return_explicit_unknown_payload(self) -> None:
        self.gateway.jobs = [
            JobRecord(name="nightly-1700000000", status=JobStatus.PENDING, is_managed=True)
        ]

        result = self.service.get_logs("nightly-1700000000", tail_lines=10)

        self.assertIsNone(result["pod"])
        self.assertEqual(result["status"], JobStatus.UNKNOWN.value)

    def test_create_failure_releases_reservation_and_hides_details(self) -> None:
        self.gateway.create_job_from_cronjob = Mock(side_effect=KubernetesError("create"))

        with self.assertLogs("metadata_qa.service", level="ERROR"):
            self.service.start_job()

        self.assertIsNone(self.service.current_job)
        self.assertEqual(
            self.events[-1][1]["message"],
            "Kubernetes-Fehler beim Starten des Jobs.",
        )

    def test_live_stream_preserves_spaces_and_replaces_invalid_utf8(self) -> None:
        job_name = "nightly-1700000000"
        self.gateway.pods[job_name] = [PodRecord("pod-1", "Running")]

        def stream_logs(_pod_name):
            yield b"  padded  \n"
            yield b"invalid: \xff\n"
            self.gateway.pods[job_name] = [PodRecord("pod-1", "Succeeded")]
            self.gateway.set_status(job_name, JobStatus.SUCCEEDED)

        self.gateway.stream_logs = stream_logs
        self.service.start_job()
        self.assertEqual(len(self.spawned), 1)

        target = self.spawned.pop()
        target()

        log_messages = [
            payload["message"] for event, payload in self.events if event == "log_update"
        ]
        self.assertEqual(log_messages, ["  padded  ", "invalid: �"])
        self.assertIsNone(self.service.current_job)


if __name__ == "__main__":
    unittest.main()
