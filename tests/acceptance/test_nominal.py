import signal
import subprocess
import time

import pytest


@pytest.fixture
def worker(running_worker):
    def func(*queues):
        process = running_worker(*queues)
        time.sleep(1)
        process.send_signal(signal.SIGINT)
        return process.communicate()

    return func


@pytest.fixture
def running_worker(process_env):
    def func(*queues, name="worker"):
        return subprocess.Popen(
            [
                "procrastinate",
                "-vvv",
                "worker",
                f"--name={name}",
                "--queues",
                ",".join(queues),
            ],
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )

    return func


def test_nominal(defer, worker):
    from .param import Param

    defer("sum_task", a=5, b=7)
    defer("sum_task_param", p1=Param(3), p2=Param(4))
    defer("increment_task", a=3)

    stdout, stderr = worker()

    assert stdout.splitlines() == ["Launching a worker on all queues", "12", "7", "4"]
    assert stderr.startswith("INFO:procrastinate.")

    defer("product_task", a=5, b=4)

    stdout, stderr = worker("default")
    assert "20" not in stdout

    stdout, stderr = worker("product_queue")
    assert stdout.splitlines() == ["Launching a worker on product_queue", "20"]

    defer("two_fails")
    stdout, stderr = worker()
    assert "Print something to stdout" in stdout
    assert stderr.count("Exception: This should fail") == 2

    defer("multiple_exception_failures")
    stdout, stderr = worker()
    assert (
        stdout
        == """Launching a worker on all queues
Try 0
Try 1
Try 2
"""
    )

    assert stderr.count("Traceback (most recent call last)") == 3
    assert stderr.count("Job error, to retry") == 2
    waited_log = "Job error - Job tests.acceptance.app.multiple_exception_failures[6]()"
    assert stderr.count(waited_log) == 1


def test_lock(defer, running_worker):
    """
    In this test, we launch 2 workers in two parallel threads, and ask them
    both to process tasks with the same lock. We check that the second task is
    not started before the first one was finished.
    """

    defer(
        "sleep_and_write",
        ["--lock", "a"],
        sleep=0.5,
        write_before="before-1",
        write_after="after-1",
    )
    defer(
        "sleep_and_write",
        ["--lock", "a"],
        sleep=0.001,
        write_before="before-2",
        write_after="after-2",
    )
    # Run the 2 workers concurrently
    process1 = running_worker(name="worker1")
    process2 = running_worker(name="worker2")
    time.sleep(2)
    # And stop them
    process1.send_signal(signal.SIGINT)
    process2.send_signal(signal.SIGINT)

    # Gather their stdout
    stdout1, stderr1 = process1.communicate()
    print(stderr1)
    stdout2, stderr2 = process2.communicate()
    print(stderr2)
    stdout = stdout1 + stdout2

    # Sort the interesting lines by timestamp to reconstitute a consistent view
    lines = dict(
        line.split()[1:] for line in stdout.splitlines() if line.startswith("->")
    )
    lines = sorted(lines, key=lines.get)

    # Check that it all happened in order
    assert lines == ["before-1", "after-1", "before-2", "after-2"]
    # If locks didnt work, we would have
    # ["before-1", "before-2", "after-2", "after-1"]


def test_queueing_lock(defer, running_worker):
    defer("sometask", ["--queueing-lock", "a"])
    defer("sometask", ["--queueing-lock", "b"])

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        defer("sometask", ["--queueing-lock", "a"])

    assert excinfo.value.returncode == 1

    # This one doesn't raise
    defer("sometask", ["--queueing-lock", "a", "--ignore-already-enqueued"])
