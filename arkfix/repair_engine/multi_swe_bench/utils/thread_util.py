import logging
import queue
import threading
import traceback
from dataclasses import dataclass
from typing import Any


@dataclass
class Result:
    id: Any
    success: bool
    result: Any = None
    error: Any = None
    traceback: str = None


class SPMCThreadPool:
    def __init__(self, num_workers, logger: logging.Logger = None):

        self.num_workers = num_workers
        self.task_queue = queue.Queue()
        self.result_dict = {}
        self.lock = threading.Lock()
        self.workers: list[threading.Thread] = []
        self.running = False
        self.logger = logger

    def _worker(self):
        while True:
            task = self.task_queue.get()
            if task is None:
                self.task_queue.task_done()
                break

            task_id, func, args, kwargs = task

            try:
                if self.logger:
                    self.logger.info(f"Task {task_id} started.")
                result = func(*args, **kwargs)
                with self.lock:
                    self.result_dict[task_id] = Result(
                        id=task_id, success=True, result=result
                    )
            except Exception as e:
                with self.lock:
                    self.result_dict[task_id] = Result(
                        id=task_id,
                        success=False,
                        error=e,
                        traceback=traceback.format_exc(),
                    )
            finally:
                self.task_queue.task_done()

    def start(self):
        if not self.running:
            self.running = True
            self.result_dict.clear()
            for _ in range(self.num_workers):
                worker_thread = threading.Thread(target=self._worker)
                worker_thread.daemon = True
                worker_thread.start()
                self.workers.append(worker_thread)
            if self.logger:
                self.logger.info(f"ThreadPool started with {self.num_workers} workers.")

    def send(self, task_id, func, *args, **kwargs):
        if not self.running:
            raise RuntimeError("ThreadPool is not running. Call start() first.")
        with self.lock:
            self.result_dict[task_id] = None
        self.task_queue.put((task_id, func, args, kwargs))
        if self.logger:
            self.logger.debug(f"Task {task_id} sent to the queue.")

    def join(self):
        self.task_queue.join()
        with self.lock:
            results = self.result_dict
            self.result_dict = {}
            return results

    def stop(self):
        if self.running:
            self.running = False
            self.join()
            for _ in range(self.num_workers):
                self.task_queue.put(None)
            for worker_thread in self.workers:
                worker_thread.join()
            self.workers.clear()
            if self.logger:
                self.logger.info("ThreadPool stopped.")
