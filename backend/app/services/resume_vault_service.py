from contextlib import contextmanager
import hashlib
import io
import os
import re
import threading
import time
import uuid
from pathlib import Path
import docx
import fitz
from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(filename: str) -> tuple[str, str]:
    """Return (safe_filename, lowercase_extension) stripped of directory traversal."""
    base = os.path.basename(filename or "").strip()
    if not base:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filename is required")
    _, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file format; only PDF and DOCX resumes are accepted",
        )
    safe = _SAFE_NAME_RE.sub("_", base).strip("._")
    if not safe:
        safe = f"resume{ext}"
    if not safe.lower().endswith(ext):
        safe = f"{safe}{ext}"
    return safe[:180], ext


def validate_file_contents(data: bytes, ext: str, max_bytes: int) -> None:
    """Verify file size and magic bytes/container structure match the declared extension."""
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds maximum allowed size of {max_bytes} bytes",
        )

    if ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid PDF file header")
        try:
            with fitz.open(stream=data, filetype="pdf") as pdf_doc:
                if len(pdf_doc) < 1:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF has no pages")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Corrupted PDF file") from exc

    elif ext == ".docx":
        if not data.startswith(b"PK\x03\x04"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid DOCX archive header")
        try:
            docx.Document(io.BytesIO(data))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Corrupted DOCX file") from exc


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_resume_bytes(
    *,
    storage_root: Path,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    safe_filename: str,
    is_master: bool,
    data: bytes,
) -> Path:
    """Persist immutable resume bytes under private user-scoped storage."""
    bucket = "master_resume" if is_master else "tailored_resumes"
    root_resolved = storage_root.resolve()
    target_dir = (root_resolved / bucket / str(user_id)).resolve()
    if root_resolved not in target_dir.parents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid storage path")
    target_dir.mkdir(parents=True, exist_ok=True)

    target_path = (target_dir / f"{resume_id}_{safe_filename}").resolve()
    if target_dir not in target_path.parents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid target filename")

    with open(target_path, "xb") as fh:
        fh.write(data)
    return target_path


def resolve_owned_resume_path(storage_root: Path, stored_path: str | None) -> Path:
    """Validate stored path stays inside storage_root and exists on disk."""
    if not stored_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume file path not available")
    root_resolved = storage_root.resolve()
    candidate = Path(stored_path).resolve()
    if root_resolved not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume file not found")
    return candidate


def _extract_uuid_prefix(filename: str) -> uuid.UUID | None:
    prefix = filename.split("_", 1)[0]
    try:
        return uuid.UUID(prefix)
    except ValueError:
        return None


_LOCAL_ARTIFACT_LOCKS_GUARD = threading.Lock()
_LOCAL_ARTIFACT_LOCKS: dict[uuid.UUID, threading.Lock] = {}


def _get_local_artifact_lock(resume_id: uuid.UUID) -> threading.Lock:
    with _LOCAL_ARTIFACT_LOCKS_GUARD:
        lock = _LOCAL_ARTIFACT_LOCKS.get(resume_id)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_ARTIFACT_LOCKS[resume_id] = lock
        return lock


@contextmanager
def artifact_persistence_lock(
    db: Session,
    resume_id: uuid.UUID,
    *,
    release_xact_on_exit: bool = False,
):
    """Coordinate PDF artifact writes/commits with orphan cleanup across threads and Postgres processes."""
    local_lock = _get_local_artifact_lock(resume_id)
    local_lock.acquire()
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    try:
        if dialect == "postgresql":
            digest = hashlib.sha256(f"resume-artifact:{resume_id}".encode("utf-8")).digest()
            signed_i64 = int.from_bytes(digest[:8], byteorder="big", signed=True)
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": signed_i64})
        yield
        if release_xact_on_exit and dialect == "postgresql" and db.in_transaction():
            db.commit()
    except Exception:
        if release_xact_on_exit and dialect == "postgresql" and db.in_transaction():
            db.rollback()
        raise
    finally:
        local_lock.release()


def _parse_user_dir_uuid(dirname: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(dirname)
    except ValueError:
        return None


def reconcile_orphaned_tailored_pdfs(
    db: Session,
    *,
    storage_root: Path,
    min_age_seconds: int = 300,
) -> dict[str, int]:
    """Idempotently remove aged orphaned PDFs in `tailored_resumes/` that lack a committed `Resume` row.

    Synchronization & safety invariants:
    - Never touches `master_resume/` files or non-UUID filenames.
    - Respects `min_age_seconds` grace period.
    - Acquires `artifact_persistence_lock(db, file_resume_id)` shared with worker persistence.
    - Inside the lock, skips files when a `ResumeGenerationJob` for that user holds an active valid
      worker lease (`status == 'running'` and `lease_expires_at > now`) or explicitly references
      `resume_id == file_resume_id` in `'queued'`/`'running'`/`'completed'`.
    - Rechecks committed `Resume` references immediately before `unlink(missing_ok=True)`.
    """
    from app.database.models import Resume, ResumeGenerationJob, utc_now

    tailored_root = (storage_root.resolve() / "tailored_resumes").resolve()
    if not tailored_root.is_dir():
        return {"scanned": 0, "removed": 0, "skipped_grace": 0, "skipped_active_job": 0}

    cutoff_epoch = time.time() - max(0, min_age_seconds)
    scanned = 0
    removed = 0
    skipped_grace = 0
    skipped_active_job = 0

    for user_dir in tailored_root.iterdir():
        if not user_dir.is_dir():
            continue
        dir_user_id = _parse_user_dir_uuid(user_dir.name)
        for file_path in user_dir.iterdir():
            if not file_path.is_file() or file_path.suffix.lower() != ".pdf":
                continue
            scanned += 1
            file_resume_id = _extract_uuid_prefix(file_path.name)
            if file_resume_id is None:
                continue

            try:
                stat = file_path.stat()
            except FileNotFoundError:
                continue

            if stat.st_mtime > cutoff_epoch:
                skipped_grace += 1
                continue

            with artifact_persistence_lock(db, file_resume_id, release_xact_on_exit=True):
                now = utc_now()
                # Check if an active valid-lease worker or queued/running job references this artifact
                if dir_user_id is not None:
                    active_lease_job = db.scalar(
                        select(ResumeGenerationJob.id).where(
                            ResumeGenerationJob.user_id == dir_user_id,
                            ResumeGenerationJob.status == "running",
                            (ResumeGenerationJob.lease_expires_at.is_(None))
                            | (ResumeGenerationJob.lease_expires_at > now),
                        )
                    )
                    if active_lease_job is not None:
                        skipped_active_job += 1
                        continue

                referenced_by_gen = db.scalar(
                    select(ResumeGenerationJob.id).where(
                        ResumeGenerationJob.resume_id == file_resume_id,
                        ResumeGenerationJob.status.in_(("queued", "running", "completed")),
                    )
                )
                if referenced_by_gen is not None:
                    skipped_active_job += 1
                    continue

                # Recheck committed Resume references immediately before deletion
                committed = db.scalar(
                    select(Resume.id).where(
                        (Resume.id == file_resume_id) | (Resume.pdf_path == str(file_path.resolve()))
                    )
                )
                if committed is not None:
                    continue

                try:
                    file_path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue

    return {
        "scanned": scanned,
        "removed": removed,
        "skipped_grace": skipped_grace,
        "skipped_active_job": skipped_active_job,
    }
