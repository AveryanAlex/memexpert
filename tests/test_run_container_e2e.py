"""No-Docker regression tests for the container E2E orchestrator."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from scripts import run_container_e2e

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def fake_project_claim(run_id: str) -> run_container_e2e.ProjectClaim:
    return run_container_e2e.ProjectClaim(
        network_name=f"memexpert-e2e-{run_id}-claim",
        network_id=f"claim-id-{run_id}",
    )


def test_parse_consumer_queues_returns_registered_queue_names() -> None:
    payload = json.dumps(
        [
            {"queue_name": "pipeline.media_inspect", "consumer_tag": "one"},
            {"queue_name": "pipeline.ocr", "consumer_tag": "two"},
            {"queue_name": "pipeline.ocr", "consumer_tag": "three"},
        ],
    )

    assert run_container_e2e.parse_consumer_queues(payload) == {
        "pipeline.media_inspect",
        "pipeline.ocr",
    }


def test_parse_pipeline_consumer_ownership_maps_inspectable_role_arguments() -> None:
    payload = json.dumps(
        [
            {
                "queue_name": "pipeline.media_inspect",
                "consumer_tag": "ctag3.a67ebace3517446c82bcad1c2334f75d",
                "arguments": [["x-memexpert-worker-role", "longstr", "media"]],
            },
            {
                "queue_name": "pipeline.ocr",
                "consumer_tag": "ctag2.c718a740c9804c87b7d564d35ec0c54d",
                "arguments": [["x-memexpert-worker-role", "longstr", "ocr"]],
            },
            {"queue_name": "unrelated", "consumer_tag": "amq.ctag-generated", "arguments": []},
        ]
    )

    assert run_container_e2e.parse_pipeline_consumer_ownership(payload) == {
        "pipeline.media_inspect": ["media"],
        "pipeline.ocr": ["ocr"],
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ("not-a-table", "invalid arguments"),
        ([["x-memexpert-worker-role", "longstr"]], "triplet"),
        ([["x-memexpert-worker-role", "longstr", 7]], "invalid x-memexpert-worker-role"),
    ],
)
def test_parse_pipeline_consumer_ownership_rejects_malformed_arguments(
    arguments: object,
    message: str,
) -> None:
    payload = json.dumps([{"queue_name": "pipeline.ocr", "arguments": arguments}])

    with pytest.raises(run_container_e2e.RabbitMQConsumerInspectionError, match=message):
        run_container_e2e.parse_pipeline_consumer_ownership(payload)


def test_wait_for_pipeline_consumer_ownership_retries_wrong_role_then_succeeds() -> None:
    observations = [
        {"pipeline.ocr": ["media"]},
        {"pipeline.ocr": ["ocr"]},
    ]
    clock = FakeClock()

    run_container_e2e.wait_for_pipeline_consumer_ownership(
        ["docker", "compose"],
        env={},
        expected_roles={"pipeline.ocr": "ocr"},
        timeout_seconds=2.0,
        poll_interval_seconds=1.0,
        inspect_consumers=lambda _remaining: observations.pop(0),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert observations == []
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not-json", "valid JSON"),
        (json.dumps({"queue_name": "pipeline.ocr"}), "JSON array"),
        (json.dumps(["pipeline.ocr"]), "consumer row 0"),
        (json.dumps([{"queue_name": ""}]), "queue_name"),
    ],
)
def test_parse_consumer_queues_rejects_malformed_output(payload: str, message: str) -> None:
    with pytest.raises(run_container_e2e.RabbitMQConsumerInspectionError, match=message):
        run_container_e2e.parse_consumer_queues(payload)


def test_wait_for_pipeline_consumers_retries_transient_inspection_and_succeeds() -> None:
    required = frozenset({"pipeline.media_inspect", "pipeline.ocr"})
    observations: list[Exception | set[str]] = [
        run_container_e2e.RabbitMQConsumerInspectionError("node is still starting"),
        {"pipeline.media_inspect"},
        set(required),
    ]
    clock = FakeClock()

    def inspect(_: float) -> set[str]:
        observation = observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation

    run_container_e2e.wait_for_pipeline_consumers(
        ["docker", "compose"],
        env={},
        required_queues=required,
        timeout_seconds=5.0,
        poll_interval_seconds=1.0,
        inspect_consumers=inspect,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert observations == []
    assert clock.sleeps == [1.0, 1.0]


def test_wait_for_pipeline_consumers_timeout_reports_last_state() -> None:
    clock = FakeClock()

    with pytest.raises(run_container_e2e.PipelineConsumerReadinessError) as exc_info:
        run_container_e2e.wait_for_pipeline_consumers(
            ["docker", "compose"],
            env={},
            required_queues=frozenset({"pipeline.media_inspect", "pipeline.ocr"}),
            timeout_seconds=2.0,
            poll_interval_seconds=1.0,
            inspect_consumers=lambda _: {"pipeline.media_inspect"},
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    message = str(exc_info.value)
    assert "pipeline.ocr" in message
    assert "pipeline.media_inspect" in message
    assert "attempts=2" in message
    assert clock.now == 2.0


def test_long_sanitized_run_ids_keep_hash_suffixes_unique() -> None:
    shared_prefix = "feature-" + "a" * 80

    first = run_container_e2e.sanitize_run_id(f"{shared_prefix}-first")
    second = run_container_e2e.sanitize_run_id(f"{shared_prefix}-second")

    assert len(first) <= run_container_e2e.MAX_RUN_ID_LENGTH
    assert len(second) <= run_container_e2e.MAX_RUN_ID_LENGTH
    assert first != second
    assert first.startswith("feature-")
    assert second.startswith("feature-")


def test_create_artifact_dir_rejects_existing_run_directory(tmp_path: Path) -> None:
    existing = tmp_path / "same-run"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("owned by an earlier run", encoding="utf-8")

    with pytest.raises(run_container_e2e.E2ERunCollisionError, match="already exists"):
        run_container_e2e.create_artifact_dir(tmp_path, run_id="same-run")

    assert sentinel.read_text(encoding="utf-8") == "owned by an earlier run"


def test_assert_project_available_rejects_existing_compose_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        output = "volume-id\n" if command[1:3] == ["volume", "ls"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(run_container_e2e.subprocess, "run", fake_run)

    with pytest.raises(run_container_e2e.E2ERunCollisionError, match="volume-id"):
        run_container_e2e.assert_project_available("memexpert-e2e-collision", env={})


def test_claim_project_is_atomic_when_two_worktrees_request_same_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    networks: set[str] = set()

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "network", "create"]:
            network_name = command[-1]
            if network_name in networks:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="already exists")
            networks.add(network_name)
            return subprocess.CompletedProcess(command, 0, stdout="claim-network-id\n", stderr="")
        if command[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(command, 0 if command[-1] in networks else 1)
        raise AssertionError(f"Unexpected Docker command: {command}")

    monkeypatch.setattr(run_container_e2e.subprocess, "run", fake_run)

    first = run_container_e2e.claim_project("memexpert-e2e-same-run", env={})

    assert first.network_name == "memexpert-e2e-same-run-claim"
    with pytest.raises(run_container_e2e.E2ERunCollisionError, match="another worktree"):
        run_container_e2e.claim_project("memexpert-e2e-same-run", env={})


def test_capture_marks_nonzero_command_failure_visibly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 23)

    monkeypatch.setattr(run_container_e2e.subprocess, "run", fake_run)
    output_path = tmp_path / "capture.txt"

    failure = run_container_e2e.capture(["docker", "version"], env={}, output_path=output_path)

    assert failure is not None
    assert failure.returncode == 23
    assert failure.output_path == str(output_path)
    assert "ARTIFACT CAPTURE FAILED" in output_path.read_text(encoding="utf-8")


def test_capture_flushes_command_header_before_child_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output_file = cast("TextIO", kwargs["stdout"])
        os.write(output_file.fileno(), b"child output\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(run_container_e2e.subprocess, "run", fake_run)
    output_path = tmp_path / "capture.txt"

    failure = run_container_e2e.capture(["docker", "version"], env={}, output_path=output_path)

    assert failure is None
    assert output_path.read_text(encoding="utf-8") == "$ docker version\nchild output\n"


def test_main_records_unexpected_python_exception_as_nonzero_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_RUN_ID", "unexpected-error")
    monkeypatch.delenv("E2E_KEEP_STACK", raising=False)
    monkeypatch.setattr(run_container_e2e, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_container_e2e,
        "claim_project",
        lambda *_args, **_kwargs: fake_project_claim("unexpected-error"),
    )
    monkeypatch.setattr(run_container_e2e, "assert_project_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        run_container_e2e,
        "run_checked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unexpected boom")),
    )
    monkeypatch.setattr(run_container_e2e, "collect_artifacts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(run_container_e2e, "run_best_effort", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "remove_defaulted_images", lambda *_args, **_kwargs: [])

    exit_code = run_container_e2e.main()

    metadata = json.loads((tmp_path / "unexpected-error" / "run-metadata.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert metadata["exit_code"] == 1
    assert metadata["failure"] == {
        "kind": "python_exception",
        "message": "unexpected boom",
        "type": "ValueError",
    }


def test_main_records_project_collision_without_tearing_down_foreign_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_RUN_ID", "project-collision")
    monkeypatch.setattr(run_container_e2e, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_container_e2e,
        "claim_project",
        lambda *_args, **_kwargs: fake_project_claim("project-collision"),
    )
    monkeypatch.setattr(
        run_container_e2e,
        "assert_project_available",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            run_container_e2e.E2ERunCollisionError("existing volume foreign-volume"),
        ),
    )
    monkeypatch.setattr(
        run_container_e2e,
        "collect_artifacts",
        lambda *_args, **_kwargs: pytest.fail("foreign project artifacts must not be captured"),
    )
    monkeypatch.setattr(
        run_container_e2e,
        "run_best_effort",
        lambda *_args, **_kwargs: pytest.fail("foreign project must not be torn down"),
    )

    exit_code = run_container_e2e.main()

    metadata = json.loads((tmp_path / "project-collision" / "run-metadata.json").read_text(encoding="utf-8"))
    assert exit_code == 2
    assert metadata["exit_code"] == 2
    assert metadata["failure"] == {
        "kind": "collision",
        "message": "existing volume foreign-volume",
    }
    assert metadata["project_available_at_start"] is False
    assert metadata["project_claim"]["released"] is False
    assert metadata["project_claim"]["retained_reason"] == "project_preflight_failed"


def test_main_records_capture_failure_without_failing_successful_assertions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_RUN_ID", "capture-error")
    monkeypatch.delenv("E2E_KEEP_STACK", raising=False)
    monkeypatch.setattr(run_container_e2e, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_container_e2e,
        "claim_project",
        lambda *_args, **_kwargs: fake_project_claim("capture-error"),
    )
    monkeypatch.setattr(run_container_e2e, "assert_project_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "run_checked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "wait_for_pipeline_consumers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "wait_for_pipeline_consumer_ownership", lambda *_args, **_kwargs: None)
    capture_failure = run_container_e2e.CommandFailure(
        command=("docker", "compose", "logs"),
        message="command exited with status 7",
        returncode=7,
        output_path="compose-logs.txt",
    )
    monkeypatch.setattr(run_container_e2e, "collect_artifacts", lambda *_args, **_kwargs: [capture_failure])
    monkeypatch.setattr(run_container_e2e, "run_best_effort", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "remove_defaulted_images", lambda *_args, **_kwargs: [])

    exit_code = run_container_e2e.main()

    metadata = json.loads((tmp_path / "capture-error" / "run-metadata.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert metadata["exit_code"] == 0
    assert metadata["artifact_capture_failures"] == [capture_failure.as_metadata()]
    assert metadata["project_claim"]["released"] is True


def test_main_retains_claim_and_records_failed_teardown_without_failing_assertions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_RUN_ID", "teardown-error")
    monkeypatch.delenv("E2E_KEEP_STACK", raising=False)
    monkeypatch.setattr(run_container_e2e, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_container_e2e,
        "claim_project",
        lambda *_args, **_kwargs: fake_project_claim("teardown-error"),
    )
    monkeypatch.setattr(run_container_e2e, "assert_project_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "run_checked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "wait_for_pipeline_consumers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "wait_for_pipeline_consumer_ownership", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "collect_artifacts", lambda *_args, **_kwargs: [])
    teardown_failure = run_container_e2e.CommandFailure(
        command=("docker", "compose", "down"),
        message="command exited with status 9",
        returncode=9,
    )
    best_effort_commands: list[list[str]] = []

    def run_best_effort(command: list[str], **_: object) -> run_container_e2e.CommandFailure | None:
        best_effort_commands.append(command)
        if "down" in command:
            return teardown_failure
        pytest.fail("The claim must not be released after failed cleanup")

    monkeypatch.setattr(run_container_e2e, "run_best_effort", run_best_effort)
    monkeypatch.setattr(run_container_e2e, "remove_defaulted_images", lambda *_args, **_kwargs: [])

    exit_code = run_container_e2e.main()

    metadata = json.loads((tmp_path / "teardown-error" / "run-metadata.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert metadata["exit_code"] == 0
    assert metadata["cleanup_failures"] == [teardown_failure.as_metadata()]
    assert metadata["project_claim"]["released"] is False
    assert metadata["project_claim"]["retained_reason"] == "cleanup_failed"
    assert len(best_effort_commands) == 1


def test_main_preserves_core_exit_code_when_diagnostics_and_cleanup_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_RUN_ID", "core-and-cleanup-error")
    monkeypatch.delenv("E2E_KEEP_STACK", raising=False)
    monkeypatch.setattr(run_container_e2e, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_container_e2e,
        "claim_project",
        lambda *_args, **_kwargs: fake_project_claim("core-and-cleanup-error"),
    )
    monkeypatch.setattr(run_container_e2e, "assert_project_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        run_container_e2e,
        "run_checked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(7, ["core"])),
    )
    capture_failure = run_container_e2e.CommandFailure(command=("capture",), message="capture failed", returncode=3)
    cleanup_failure = run_container_e2e.CommandFailure(command=("cleanup",), message="cleanup failed", returncode=4)
    monkeypatch.setattr(run_container_e2e, "collect_artifacts", lambda *_args, **_kwargs: [capture_failure])
    monkeypatch.setattr(run_container_e2e, "run_best_effort", lambda *_args, **_kwargs: cleanup_failure)
    monkeypatch.setattr(run_container_e2e, "remove_defaulted_images", lambda *_args, **_kwargs: [])

    exit_code = run_container_e2e.main()

    metadata = json.loads(
        (tmp_path / "core-and-cleanup-error" / "run-metadata.json").read_text(encoding="utf-8"),
    )
    assert exit_code == 7
    assert metadata["exit_code"] == 7
    assert metadata["artifact_capture_failures"] == [capture_failure.as_metadata()]
    assert metadata["cleanup_failures"] == [cleanup_failure.as_metadata()]


def test_main_keep_stack_retains_claim_without_attempting_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E2E_RUN_ID", "keep-stack")
    monkeypatch.setenv("E2E_KEEP_STACK", "1")
    monkeypatch.setattr(run_container_e2e, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_container_e2e,
        "claim_project",
        lambda *_args, **_kwargs: fake_project_claim("keep-stack"),
    )
    monkeypatch.setattr(run_container_e2e, "assert_project_available", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "run_checked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "wait_for_pipeline_consumers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "wait_for_pipeline_consumer_ownership", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_container_e2e, "collect_artifacts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        run_container_e2e,
        "run_best_effort",
        lambda *_args, **_kwargs: pytest.fail("KEEP_STACK must retain both stack and claim"),
    )

    exit_code = run_container_e2e.main()

    metadata = json.loads((tmp_path / "keep-stack" / "run-metadata.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert metadata["project_claim"]["released"] is False
    assert metadata["project_claim"]["retained_reason"] == "keep_stack"
