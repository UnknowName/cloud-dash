from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from queue import Empty, PriorityQueue
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 5
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0


@dataclass(order=True)
class _PrioritizedTask:
    priority: int
    sequence: int = field(compare=True)
    fn: Callable | None = field(compare=False, default=None)
    args: tuple = field(compare=False, default=())
    kwargs: dict = field(compare=False, default_factory=dict)
    future: Future | None = field(compare=False, default=None)
    is_sentinel: bool = field(compare=False, default=False)
    cycle_id: int = field(compare=False, default=0)


@dataclass(frozen=True)
class PoolStats:
    active_threads: int = 0
    queue_length: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    total_submitted: int = 0
    max_workers: int = 0
    current_cycle: int = 0


class PriorityThreadPool:
    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        name_prefix: str = "pool",
    ) -> None:
        self._max_workers = max_workers
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._name_prefix = name_prefix

        self._task_queue: PriorityQueue[_PrioritizedTask] = PriorityQueue()
        self._workers: list[threading.Thread] = []
        self._shutdown = False
        self._active_count = 0
        self._sequence = 0

        self._lock = threading.Lock()
        self._seq_lock = threading.Lock()

        self._completed_tasks = 0
        self._failed_tasks = 0
        self._total_submitted = 0

        # 采集周期追踪：同一轮采集的任务共享 cycle_id，用于等待整轮完成
        self._cycle_id = 0
        # 每轮采集的待完成 Future 计数
        self._cycle_pending: dict[int, list[Future]] = {}
        self._cycle_lock = threading.Lock()

        self._start_workers()
        logger.info(
            "线程池 [%s] 已创建: max_workers=%d, max_retries=%d, retry_delay=%.1fs",
            name_prefix, max_workers, max_retries, retry_delay,
        )

    def _start_workers(self) -> None:
        for i in range(self._max_workers):
            self._create_worker(i)

    def _create_worker(self, index: int) -> None:
        worker = threading.Thread(
            target=self._worker_loop,
            name=f"{self._name_prefix}-worker-{index}",
            daemon=True,
        )
        worker.start()
        self._workers.append(worker)

    def _worker_loop(self) -> None:
        while not self._shutdown:
            try:
                task = self._task_queue.get(timeout=1.0)
            except Empty:
                continue

            if task.is_sentinel:
                logger.debug("工作线程收到退出信号: %s", threading.current_thread().name)
                self._task_queue.task_done()
                break

            with self._lock:
                self._active_count += 1

            try:
                self._execute_with_retry(task)
            finally:
                with self._lock:
                    self._active_count -= 1
                self._task_queue.task_done()

    def _execute_with_retry(self, task: _PrioritizedTask) -> None:
        last_exception: Exception | None = None
        fn_name = getattr(task.fn, "__qualname__", getattr(task.fn, "__name__", str(task.fn)))

        for attempt in range(self._max_retries + 1):
            try:
                if attempt > 0:
                    delay = self._retry_delay * (2 ** (attempt - 1))
                    logger.info(
                        "线程池 [%s] 任务重试 (第%d次, 延迟%.1fs): %s",
                        self._name_prefix, attempt, delay, fn_name,
                    )
                    time.sleep(delay)

                logger.debug(
                    "线程池 [%s] 任务开始: %s (尝试 %d/%d, 周期=%d)",
                    self._name_prefix, fn_name, attempt + 1, self._max_retries + 1,
                    task.cycle_id,
                )
                result = task.fn(*task.args, **task.kwargs)

                if task.future and not task.future.done():
                    task.future.set_result(result)

                with self._lock:
                    self._completed_tasks += 1

                logger.debug("线程池 [%s] 任务完成: %s", self._name_prefix, fn_name)
                return
            except Exception as e:
                last_exception = e
                logger.warning(
                    "线程池 [%s] 任务异常: %s (尝试 %d/%d): %s",
                    self._name_prefix, fn_name, attempt + 1, self._max_retries + 1, e,
                )

        if task.future and not task.future.done():
            task.future.set_exception(last_exception)

        with self._lock:
            self._failed_tasks += 1

        logger.error(
            "线程池 [%s] 任务最终失败: %s (已重试%d次): %s",
            self._name_prefix, fn_name, self._max_retries, last_exception,
        )

    def begin_cycle(self) -> int:
        """开始一个新的采集周期，返回周期ID，后续 submit 可关联此周期"""
        with self._cycle_lock:
            self._cycle_id += 1
            self._cycle_pending[self._cycle_id] = []
            return self._cycle_id

    def submit(
        self,
        fn: Callable,
        *args: Any,
        priority: int = 5,
        cycle_id: int = 0,
        **kwargs: Any,
    ) -> Future:
        if self._shutdown:
            raise RuntimeError("线程池已关闭，无法提交新任务")

        future = Future()

        with self._seq_lock:
            self._sequence += 1
            seq = self._sequence

        task = _PrioritizedTask(
            priority=priority,
            sequence=seq,
            fn=fn,
            args=args,
            kwargs=kwargs,
            future=future,
            cycle_id=cycle_id,
        )

        self._task_queue.put(task)

        with self._lock:
            self._total_submitted += 1

        # 将 Future 注册到对应周期的待完成列表
        if cycle_id > 0:
            with self._cycle_lock:
                if cycle_id in self._cycle_pending:
                    self._cycle_pending[cycle_id].append(future)

        logger.debug(
            "线程池 [%s] 任务已提交: %s (优先级=%d, 序号=%d, 周期=%d)",
            self._name_prefix, getattr(fn, "__name__", str(fn)), priority, seq, cycle_id,
        )
        return future

    def submit_batch(
        self,
        tasks: list[tuple[Callable, tuple, dict, int]],
        cycle_id: int = 0,
    ) -> list[Future]:
        """批量提交任务，减少锁竞争。每个元素为 (fn, args, kwargs, priority)"""
        if self._shutdown:
            raise RuntimeError("线程池已关闭，无法提交新任务")

        futures: list[Future] = []

        with self._seq_lock:
            start_seq = self._sequence + 1
            self._sequence += len(tasks)

        for i, (fn, args, kwargs, priority) in enumerate(tasks):
            future = Future()
            task = _PrioritizedTask(
                priority=priority,
                sequence=start_seq + i,
                fn=fn,
                args=args,
                kwargs=kwargs,
                future=future,
                cycle_id=cycle_id,
            )
            self._task_queue.put(task)
            futures.append(future)

            if cycle_id > 0:
                with self._cycle_lock:
                    if cycle_id in self._cycle_pending:
                        self._cycle_pending[cycle_id].append(future)

        with self._lock:
            self._total_submitted += len(tasks)

        logger.debug(
            "线程池 [%s] 批量提交 %d 个任务 (周期=%d)",
            self._name_prefix, len(tasks), cycle_id,
        )
        return futures

    def wait_cycle(self, cycle_id: int, timeout: float = 300.0) -> bool:
        """等待指定采集周期的所有任务完成，返回是否在超时前全部完成"""
        with self._cycle_lock:
            futures = list(self._cycle_pending.pop(cycle_id, []))

        if not futures:
            return True

        deadline = time.monotonic() + timeout
        for future in futures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "线程池 [%s] 等待周期 %d 超时 (已耗时 %.1fs)",
                    self._name_prefix, cycle_id, timeout,
                )
                return False
            try:
                future.result(timeout=remaining)
            except Exception:
                pass

        logger.debug("线程池 [%s] 周期 %d 所有任务已完成", self._name_prefix, cycle_id)
        return True

    def adjust_workers(self, new_max: int) -> None:
        if new_max < 1:
            raise ValueError("并发数必须大于0")

        with self._lock:
            old_max = self._max_workers
            self._max_workers = new_max
            self._workers = [w for w in self._workers if w.is_alive()]

        if new_max > old_max:
            for i in range(len(self._workers), len(self._workers) + new_max - old_max):
                self._create_worker(i)
            logger.info("线程池 [%s] 扩容: %d -> %d", self._name_prefix, old_max, new_max)
        elif new_max < old_max:
            with self._seq_lock:
                self._sequence += 1
                seq = self._sequence
            for _ in range(old_max - new_max):
                sentinel = _PrioritizedTask(
                    priority=0,
                    sequence=seq,
                    is_sentinel=True,
                )
                self._task_queue.put(sentinel)
            logger.info("线程池 [%s] 缩容: %d -> %d", self._name_prefix, old_max, new_max)

    def suggest_workers(self, task_count: int, collection_interval_seconds: int) -> int:
        """根据任务量和采集间隔建议最优工作线程数

        策略：确保在采集间隔内能完成所有任务，同时不超过合理上限。
        每个任务预估耗时约2秒（含网络IO），留50%安全余量。
        """
        if task_count <= 0 or collection_interval_seconds <= 0:
            return self._max_workers

        estimated_task_seconds = 2.0
        safety_factor = 1.5
        total_estimated_time = task_count * estimated_task_seconds * safety_factor
        # 需要的并发度 = 总预估时间 / 可用时间
        needed = max(1, int(total_estimated_time / collection_interval_seconds) + 1)
        # 上限为任务数本身（再多线程也无意义）和当前配置的2倍
        upper_bound = max(self._max_workers, min(task_count, self._max_workers * 2))
        return min(needed, upper_bound)

    def shutdown(self, wait: bool = True) -> None:
        self._shutdown = True

        if wait:
            self._task_queue.join()
        else:
            while not self._task_queue.empty():
                try:
                    task = self._task_queue.get_nowait()
                    if task.future and not task.future.done():
                        task.future.cancel()
                    self._task_queue.task_done()
                except Empty:
                    break

        for worker in self._workers:
            worker.join(timeout=5.0)

        self._workers.clear()
        logger.info(
            "线程池 [%s] 已关闭: 已完成=%d, 已失败=%d, 总提交=%d",
            self._name_prefix, self._completed_tasks, self._failed_tasks, self._total_submitted,
        )

    def get_stats(self) -> PoolStats:
        with self._lock:
            return PoolStats(
                active_threads=self._active_count,
                queue_length=self._task_queue.qsize(),
                completed_tasks=self._completed_tasks,
                failed_tasks=self._failed_tasks,
                total_submitted=self._total_submitted,
                max_workers=self._max_workers,
                current_cycle=self._cycle_id,
            )

    @property
    def max_workers(self) -> int:
        return self._max_workers
