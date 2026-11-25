import io
import re
from typing import Any
import docx
import fitz

SECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "summary": ("professional summary", "summary", "profile", "objective"),
    "education": ("education", "academic"),
    "skills": ("technical skills", "skills", "technologies"),
    "projects": ("projects", "key projects"),
    "experience": ("experience", "work experience", "employment", "internships"),
    "certifications": ("certifications", "licenses"),
    "achievements": ("achievements", "awards", "honors"),
}

_URL_RE = re.compile(r"https?://[^\s<>()\"']+")


def _detect_sections(text: str) -> list[str]:
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    found: list[str] = []
    for section_key, aliases in SECTION_PATTERNS.items():
        for line in lines:
            if any(line == alias or line.startswith(f"{alias}:") for alias in aliases):
                if section_key not in found:
                    found.append(section_key)
                break
    return found


def parse_resume_bytes(data: bytes, extension: str) -> dict[str, Any]:
    """Deterministically parse PDF or DOCX resume bytes without LLM calls or fabrication."""
    ext = extension.lower()
    extracted_links: list[str] = []
    page_count: int | None = None
    paragraph_count: int | None = None

    if ext == ".pdf":
        with fitz.open(stream=data, filetype="pdf") as pdf_doc:
            page_count = len(pdf_doc)
            chunks: list[str] = []
            for page in pdf_doc:
                chunks.append(page.get_text("text"))
                for link in page.get_links():
                    uri = link.get("uri")
                    if uri and uri not in extracted_links and not uri.startswith("mailto:"):
                        extracted_links.append(uri)
            text = "\n".join(chunks)
    elif ext == ".docx":
        document = docx.Document(io.BytesIO(data))
        paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
        paragraph_count = len(paragraphs)
        text = "\n".join(paragraphs)
    else:
        raise ValueError(f"Unsupported resume extension: {ext}")

    for match in _URL_RE.findall(text):
        cleaned = match.rstrip(".,;")
        if cleaned not in extracted_links:
            extracted_links.append(cleaned)

    words = text.split()
    detected_sections = _detect_sections(text)
    text_extractable = len(words) >= 5

    # Store only structural metadata and non-PII public profile links (no raw text or emails/phones)
    public_links = [
        url for url in extracted_links if any(host in url.lower() for host in ("github.com", "linkedin.com"))
    ]

    parsed_metadata: dict[str, Any] = {
        "file_format": ext.lstrip("."),
        "page_count": page_count,
        "paragraph_count": paragraph_count,
        "word_count": len(words),
        "char_count": len(text),
        "detected_sections": detected_sections,
        "public_links": public_links,
    }
    ats_report: dict[str, Any] = {
        "text_extractable": text_extractable,
        "has_standard_headings": len(detected_sections) >= 2,
        "detected_heading_count": len(detected_sections),
    }
    return {
        "parsed_metadata": parsed_metadata,
        "ats_report": ats_report,
        "validation_passed": text_extractable,
    }
