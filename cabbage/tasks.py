import functools
import logging
import uuid
from typing import Any, Callable, Dict, Optional, Set

from cabbage import jobs, postgres, store, types

logger = logging.getLogger(__name__)


class Task:
    def __init__(
        self,
        func: Callable,
        *,
        manager: "TaskManager",
        queue: str,
        name: Optional[str] = None,
    ):
        self.queue = queue
        self.manager = manager
        self.func: Callable = func
        self.name = name or self.func.__name__

    def __call__(self, **kwargs: types.JSONValue) -> None:
        return self.func(**kwargs)

    def defer(self, lock: str = None, **kwargs: types.JSONValue) -> int:
        lock = lock or f"{uuid.uuid4()}"
        job = jobs.Job(
            id=0, queue=self.queue, task_name=self.name, lock=lock, kwargs=kwargs
        )
        job_id = self.manager.job_store.launch_job(job=job)
        logger.info(
            "Scheduled job",
            extra={
                "action": "job_defer",
                "job": {
                    "task_name": job.task_name,
                    "queue": job.queue,
                    "kwargs": job.kwargs,
                    "id": job_id,
                },
            },
        )
        return job_id


class TaskManager:
    def __init__(self, job_store: Optional[store.JobStore] = None):
        if job_store is None:
            job_store = postgres.PostgresJobStore()

        self.job_store = job_store
        self.tasks: Dict[str, Task] = {}
        self.queues: Set[str] = set()

    def task(
        self,
        _func: Optional[Callable] = None,
        queue: str = "default",
        name: Optional[str] = None,
    ) -> Callable:
        """
        Declare a function as a task.

        Can be used as a decorator or a simple method.
        """

        def _wrap(func: Callable) -> Callable[..., Any]:
            task = Task(func, manager=self, queue=queue, name=name)
            self.register(task)

            return functools.update_wrapper(task, func)

        if _func is None:
            return _wrap

        return _wrap(_func)

    def register(self, task: Task) -> None:
        self.tasks[task.name] = task
        if task.queue not in self.queues:
            logger.info(
                "Creating queue (if not already existing)",
                extra={"action": "create_queue", "queue": task.queue},
            )
            self.job_store.register_queue(task.queue)
            self.queues.add(task.queue)