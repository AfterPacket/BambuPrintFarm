from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass
class Job:
    id: str
    filename: str
    filepath: str
    status: str
    created_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    printer_id: Optional[str]
    assigned_printer_id: Optional[str]
    plate: int
    auto_assign: bool
    error: Optional[str]


class JobQueue:
    def __init__(self, storage_dir: str = "jobs") -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._storage_dir = os.path.abspath(storage_dir)
        # Historic queue.json entries stored paths like "jobs\\files\\..."
        # (relative to the repo root). When running from a different CWD, those
        # paths break. We normalize relative paths against this directory's
        # parent (repo root) for backwards compatibility.
        self._root_dir = os.path.abspath(os.path.join(self._storage_dir, os.pardir))
        self._files_dir = os.path.join(self._storage_dir, "files")
        self._meta_path = os.path.join(self._storage_dir, "queue.json")
        os.makedirs(self._files_dir, exist_ok=True)
        self._load()

    def _abs_path(self, filepath: str) -> str:
        if not filepath:
            return filepath
        if os.path.isabs(filepath):
            return filepath
        normalized = filepath.replace("/", os.sep).replace("\\", os.sep)
        if normalized.startswith("jobs" + os.sep):
            return os.path.abspath(os.path.join(self._root_dir, normalized))
        return os.path.abspath(os.path.join(self._storage_dir, normalized))

    def _load(self) -> None:
        if not os.path.exists(self._meta_path):
            return
        try:
            with open(self._meta_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError:
            # Corrupt queue file; keep the instance usable (new jobs can still be enqueued).
            return
        touched = False
        for item in data.get("jobs", []):
            job = Job(**item)
            abs_path = self._abs_path(job.filepath)
            if abs_path != job.filepath:
                job.filepath = abs_path
                touched = True
            self._jobs[job.id] = job
        if touched:
            self._persist()

    def _persist(self) -> None:
        payload = {"jobs": [asdict(job) for job in self._jobs.values()]}
        tmp_path = self._meta_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_path, self._meta_path)

    def _safe_filename(self, filename: str) -> str:
        base = os.path.basename(filename)
        cleaned = "".join(c for c in base if c.isalnum() or c in "._-")
        cleaned = cleaned.strip("._")
        return cleaned or "job.gcode"

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, object]]:
        with self._lock:
            jobs = list(self._jobs.values())
            if status:
                jobs = [job for job in jobs if job.status == status]
            jobs.sort(key=lambda job: job.created_at)
            return [asdict(job) for job in jobs]

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def enqueue(
        self,
        filename: str,
        fileobj,
        plate: int = 1,
        printer_id: Optional[str] = None,
        auto_assign: bool = True,
    ) -> Job:
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            safe_name = self._safe_filename(filename)
            filepath = os.path.join(self._files_dir, f"{job_id}__{safe_name}")
            if hasattr(fileobj, "seek"):
                try:
                    fileobj.seek(0)
                except OSError:
                    pass
            with open(filepath, "wb") as out:
                shutil.copyfileobj(fileobj, out)
            job = Job(
                id=job_id,
                filename=safe_name,
                filepath=filepath,
                status="queued",
                created_at=time.time(),
                started_at=None,
                finished_at=None,
                printer_id=printer_id,
                assigned_printer_id=None,
                plate=plate,
                auto_assign=auto_assign,
                error=None,
            )
            self._jobs[job_id] = job
            self._persist()
            return job

    def mark_dispatching(self, job_id: str, printer_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "queued":
                return False
            job.status = "dispatching"
            job.assigned_printer_id = printer_id
            job.started_at = time.time()
            self._persist()
            return True

    def mark_running(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.status = "running"
            self._persist()
            return True

    def mark_failed(self, job_id: str, error: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.status = "failed"
            job.error = error
            job.finished_at = time.time()
            self._persist()
            return True

    def mark_canceled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in {"completed", "failed"}:
                return False
            job.status = "canceled"
            job.finished_at = time.time()
            self._persist()
            return True

    def mark_completed(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.status = "completed"
            job.finished_at = time.time()
            self._persist()
            return True

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if not job:
                return False
            try:
                if os.path.exists(job.filepath):
                    os.remove(job.filepath)
            except OSError:
                pass
            self._persist()
            return True
