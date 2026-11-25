import io
import re
import uuid
from pathlib import Path
from typing import Any
import docx
import fitz
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import CandidateProfile, Resume
from app.services.resume_parser import _detect_sections, parse_resume_bytes
from app.services.resume_vault_service import compute_sha256, resolve_owned_resume_path

logger = get_logger("applypilot.resume_analyzer")
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*\u2022\u25cf]|\d+\.)\s+")


class MasterResumeAnalysis(BaseModel):
    master_resume_id: uuid.UUID | None = None
    master_checksum: str | None = None
    has_master_file: bool = False
    file_format: str | None = None
    page_count: int | None = None
    word_count: int = 0
    bullet_count: int = 0
    detected_sections: list[str] = Field(default_factory=list)
    public_links: list[str] = Field(default_factory=list)
    raw_text_lines: list[str] = Field(default_factory=list)
    parsed_metadata: dict[str, Any] = Field(default_factory=dict)
    ats_report: dict[str, Any] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)


class ResumeAnalyzerAgent:
    """Deterministically analyzes the candidate's active immutable master resume."""

    @staticmethod
    def select_active_master_resume(
        db: Session,
        profile_id: uuid.UUID,
        *,
        master_resume_id: uuid.UUID | None = None,
    ) -> Resume | None:
        if master_resume_id is not None:
            return db.scalar(
                select(Resume).where(
                    Resume.id == master_resume_id,
                    Resume.profile_id == profile_id,
                    Resume.is_master.is_(True),
                )
            )
        return db.scalar(
            select(Resume)
            .where(Resume.profile_id == profile_id, Resume.is_master.is_(True))
            .order_by(Resume.created_at.desc())
        )

    def analyze(
        self,
        db: Session,
        *,
        profile: CandidateProfile,
        master_resume: Resume | None = None,
        storage_dir: Path | None = None,
    ) -> MasterResumeAnalysis:
        target = master_resume or self.select_active_master_resume(db, profile.id)
        if target is None:
            return MasterResumeAnalysis(
                has_master_file=False,
                detected_sections=self._sections_from_profile(profile),
                uncertainties=[
                    "No uploaded master resume file; using verified CandidateProfile records as baseline."
                ],
            )

        storage_root = Path(storage_dir) if storage_dir is not None else Path(get_settings().storage_dir)
        stored_path = target.pdf_path or target.docx_path
        ext = ".pdf" if target.pdf_path else ".docx"
        try:
            path = resolve_owned_resume_path(storage_root, stored_path)
            raw_bytes = path.read_bytes()
        except Exception as exc:
            logger.warning(
                "master_resume_file_unreadable",
                extra={"extra_fields": {"resume_id": str(target.id), "error_type": type(exc).__name__}},
            )
            return MasterResumeAnalysis(
                master_resume_id=target.id,
                master_checksum=target.sha256_checksum,
                has_master_file=False,
                detected_sections=list(target.parsed_metadata.get("detected_sections") or []),
                public_links=list(target.parsed_metadata.get("public_links") or []),
                parsed_metadata=dict(target.parsed_metadata or {}),
                ats_report=dict(target.ats_report or {}),
                uncertainties=[
                    "Master resume file unavailable on disk; relying on stored metadata and CandidateProfile."
                ],
            )

        actual_checksum = compute_sha256(raw_bytes)
        uncertainties: list[str] = []
        if actual_checksum != target.sha256_checksum:
            uncertainties.append("Master resume file checksum mismatch detected during read.")

        parsed = parse_resume_bytes(raw_bytes, ext)
        text = self._extract_text(raw_bytes, ext)
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        bullet_count = sum(1 for line in raw_lines if _BULLET_PREFIX_RE.match(line))

        return MasterResumeAnalysis(
            master_resume_id=target.id,
            master_checksum=target.sha256_checksum,
            has_master_file=True,
            file_format=ext.lstrip("."),
            page_count=parsed["parsed_metadata"].get("page_count"),
            word_count=int(parsed["parsed_metadata"].get("word_count") or 0),
            bullet_count=bullet_count,
            detected_sections=list(parsed["parsed_metadata"].get("detected_sections") or _detect_sections(text)),
            public_links=list(parsed["parsed_metadata"].get("public_links") or []),
            raw_text_lines=raw_lines[:300],
            parsed_metadata=parsed["parsed_metadata"],
            ats_report=parsed["ats_report"],
            uncertainties=uncertainties,
        )

    @staticmethod
    def _extract_text(data: bytes, ext: str) -> str:
        if ext == ".pdf":
            with fitz.open(stream=data, filetype="pdf") as doc:
                return "\n".join(page.get_text("text") for page in doc)
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    @staticmethod
    def _sections_from_profile(profile: CandidateProfile) -> list[str]:
        sections: list[str] = []
        if profile.summary:
            sections.append("summary")
        if profile.skills:
            sections.append("skills")
        if profile.experiences:
            sections.append("experience")
        if profile.projects:
            sections.append("projects")
        if profile.education:
            sections.append("education")
        if profile.certifications:
            sections.append("certifications")
        if profile.achievements:
            sections.append("achievements")
        return sections
