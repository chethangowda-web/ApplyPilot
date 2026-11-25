import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict


class ResumeMetadataResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    profile_id: uuid.UUID
    is_master: bool
    original_filename: str
    content_type: str
    file_size_bytes: int
    version_label: str
    sha256_checksum: str
    template_version: str
    ats_report: dict[str, Any]
    parsed_metadata: dict[str, Any]
    validation_passed: bool
    created_at: datetime
