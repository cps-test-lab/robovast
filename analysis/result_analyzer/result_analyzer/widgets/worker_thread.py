import queue
import threading
import time
from abc import ABC, abstractmethod

from PySide2.QtCore import QObject
from PySide2.QtCore import Signal as pyqtSignal
from PySide2.QtCore import Slot as pyqtSlot


class CancellableWorkload(ABC):
    """Abstract base class for cancellable workload"""

    def __init__(self, name=''):
        self.name = name
        self.cancel_event = threading.Event()
        self.progress_callback = None

    def set_progress_callback(self, callback):
        """Set callback for progress updates: callback(percentage)"""
        self.progress_callback = callback

    def cancel(self):
        """Cancel the calculation"""
        self.cancel_event.set()

    def is_cancelled(self):
        """Check if calculation was cancelled"""
        return self.cancel_event.is_set()

    def reset_cancellation(self):
        """Reset cancellation flag for new calculation"""
        self.cancel_event.clear()

    @abstractmethod
    def run(self, data, run_type):
        """Perform the actual calculation. Must check is_cancelled() regularly!"""


class LatestOnlyWorker(QObject):
    """Worker that processes only the latest task set, discarding older ones"""

    # Signals
    finished = pyqtSignal(bool, object, str, str)  # result, data, task_id, workload_id
    progress = pyqtSignal(int, str, str)  # progress percentage, workload_id, status
    error = pyqtSignal(str, str)  # error_message, workload_id

    def __init__(self, workloads):
        super().__init__()
        self.task_queue = queue.Queue()
        self.running = True
        self._current_task_id = None
        self._task_counter = 0
        # Support both single workload (for backward compatibility) and list of workloads
        if isinstance(workloads, list):
            self._workloads = workloads
        else:
            self._workloads = [workloads]

    def add_task(self, data, run_type):
        """Add a new task, clearing any pending tasks and cancelling current workload set"""
        # Cancel all currently running calculations immediately
        for workload in self._workloads:
            if workload:
                workload.cancel()

        # Generate unique task ID
        self._task_counter += 1
        task_id = self._task_counter

        # Clear the queue of old tasks
        discarded_count = 0
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
                discarded_count += 1
            except queue.Empty:
                break

        # Add the new task
        task_item = {
            'data': data,
            'run_type': run_type,
            'task_id': task_id,
            'timestamp': time.time()
        }
        self.task_queue.put(task_item)

        print(f"Added task {task_id}, cancelled all workloads, discarded {discarded_count} old tasks")
        return task_id

    @pyqtSlot()
    def run_worker_loop(self):
        """Main worker loop - processes tasks continuously"""
        print("Worker loop started")

        while self.running:
            try:
                # Wait for a task (blocks until one is available)
                try:
                    task_item = self.task_queue.get(timeout=0.1)  # Short timeout to allow checking self.running
                except queue.Empty:
                    continue

                # Check if we're still supposed to be running
                if not self.running:
                    break

                self._current_task_id = task_item['task_id']
                print(f"Processing task: {self._current_task_id}")

                # Get the very latest task if multiple arrived while we were setting up
                latest_task = task_item
                discarded_here = 0
                while True:
                    try:
                        newer_task = self.task_queue.get_nowait()
                        latest_task = newer_task
                        discarded_here += 1
                    except queue.Empty:
                        break

                if discarded_here > 0:
                    self._current_task_id = latest_task['task_id']
                    print(f"Found {discarded_here} newer tasks, processing latest: {self._current_task_id}")

                # Process the latest task with all workloads sequentially
                try:
                    # Reset all workloads for new task
                    for workload in self._workloads:
                        workload.reset_cancellation()
                        # Create a progress callback for this specific workload
                        workload.set_progress_callback(lambda percentage, status="",
                                                       wl=workload: self._on_progress_update(percentage, wl, status))

                    # Run all workloads sequentially
                    for workload in self._workloads:
                        if not self.running or workload.is_cancelled():
                            break

                        try:
                            print(f"Starting workload: {workload.name}")
                            start_time = time.time()
                            result, data = workload.run(latest_task['data'], latest_task['run_type'])
                            end_time = time.time()
                            duration = end_time - start_time

                            # Only emit result if task wasn't cancelled and we're still running
                            if self.running and not workload.is_cancelled():
                                self.finished.emit(result, data, self._current_task_id, workload.name)
                                print(f"Task {self._current_task_id} completed successfully with {
                                      workload.name} (duration {duration:.3f}s)")
                            else:
                                print(f"Task {self._current_task_id} was cancelled for {workload.name}")

                        except Exception as e:
                            end_time = time.time()
                            duration = end_time - start_time if 'start_time' in locals() else 0
                            if self.running and not workload.is_cancelled():
                                self.error.emit(f"Error in task {self._current_task_id}: {str(e)}", workload.name)
                                print(f"Task {self._current_task_id} failed with {workload.name}: {str(e)} (duration {duration:.3f}s)")
                            else:
                                print(f"Task {self._current_task_id} cancelled due to error in {workload.name}: {str(e)}")

                except Exception as e:
                    if self.running:
                        # Emit error for all workloads if there's a general error
                        for workload in self._workloads:
                            self.error.emit(f"Error in task {self._current_task_id}: {str(e)}", workload.name)
                            print(f"Task {self._current_task_id} failed with {workload.name}: {str(e)}")

                self._current_task_id = None

            except Exception as e:
                print(f"Unexpected error in worker loop: {e}")
                if self.running:
                    self.error.emit(f"Worker loop error: {str(e)}")

        print("Worker loop ended")

    def _on_progress_update(self, percentage, workload, status=""):
        """Internal progress callback that forwards to signal with workload identification"""
        if self.running:
            self.progress.emit(percentage, workload.name, status)

    def stop(self):
        """Stop the worker gracefully and cancel all workloads immediately"""
        print("Stopping worker...")
        self.running = False
        for workload in self._workloads:
            if workload:
                workload.cancel()  # Cancel all calculations immediately

        # Clear any remaining tasks
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
            except queue.Empty:
                break

    def cancel_current_task(self):
        """Cancel only the current task set (but keep worker running)"""
        print(f"Cancelling current task: {self._current_task_id}")
        for workload in self._workloads:
            if workload:
                workload.cancel()
