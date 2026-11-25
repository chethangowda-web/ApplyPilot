import html
import io
from typing import Any
import fitz
from pydantic import BaseModel, Field

from app.schemas.resume_intelligence import TailoredResumeContent
from app.services.fingerprint_service import TEMPLATE_VERSION
from app.services.resume_parser import parse_resume_bytes
from app.services.resume_vault_service import compute_sha256


class PDFOverflowError(ValueError):
    """Raised when tailored resume content exceeds the allowed ATS page budget."""


class PDFRenderResult(BaseModel):
    pdf_bytes: bytes
    sha256_checksum: str
    file_size_bytes: int
    page_count: int
    template_version: str = TEMPLATE_VERSION
    links_preserved: list[str] = Field(default_factory=list)
    ats_report: dict[str, Any] = Field(default_factory=dict)
    parsed_metadata: dict[str, Any] = Field(default_factory=dict)
    quality_passed: bool = True


_CSS = """
body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 9.8pt;
    line-height: 1.32;
    color: #111827;
    margin: 0;
    padding: 0;
}
h1 {
    font-size: 16pt;
    margin: 0 0 2pt 0;
    color: #0f172a;
    letter-spacing: 0.3pt;
}
.contact {
    font-size: 8.8pt;
    color: #334155;
    margin: 0 0 8pt 0;
    border-bottom: 1pt solid #cbd5e1;
    padding-bottom: 5pt;
}
.contact a, .item-links a {
    color: #1d4ed8;
    text-decoration: underline;
}
h2 {
    font-size: 10.5pt;
    text-transform: uppercase;
    color: #0f172a;
    border-bottom: 0.75pt solid #94a3b8;
    margin: 8pt 0 4pt 0;
    padding-bottom: 1.5pt;
    letter-spacing: 0.4pt;
}
p {
    margin: 0 0 4pt 0;
}
.item-header {
    font-weight: bold;
    font-size: 9.8pt;
    margin: 4pt 0 1pt 0;
}
.item-meta {
    font-size: 8.8pt;
    color: #475569;
    margin: 0 0 2pt 0;
}
ul {
    margin: 1pt 0 5pt 0;
    padding-left: 14pt;
}
li {
    margin-bottom: 1.5pt;
}
"""


def _esc(val: str | None) -> str:
    return html.escape((val or "").strip(), quote=True)


def _safe_http_url(url: str | None) -> str | None:
    cleaned = (url or "").strip()
    if cleaned.lower().startswith(("https://", "http://")):
        return cleaned
    return None


def _build_resume_html_and_links(
    content: TailoredResumeContent,
) -> tuple[str, list[tuple[str, str]]]:
    """Build single-column ATS XHTML and collect clickable URI annotations."""
    links_to_bind: list[tuple[str, str]] = []
    contact_parts: list[str] = []
    if content.location:
        contact_parts.append(_esc(content.location))
    if content.email:
        contact_parts.append(_esc(content.email))
        links_to_bind.append((content.email.strip(), f"mailto:{content.email.strip()}"))
    if content.phone:
        contact_parts.append(_esc(content.phone))

    for label, raw_url in (
        ("GitHub", content.github_url),
        ("LinkedIn", content.linkedin_url),
        ("Portfolio", content.portfolio_url),
    ):
        safe_url = _safe_http_url(raw_url)
        if safe_url:
            contact_parts.append(f'<a href="{_esc(safe_url)}">{_esc(safe_url)}</a>')
            links_to_bind.append((safe_url, safe_url))

    sections_html: list[str] = [
        f"<h1>{_esc(content.full_name)}</h1>",
        f'<p class="contact">{" | ".join(contact_parts)}</p>',
    ]

    def _render_summary() -> str:
        if not content.summary.text.strip():
            return ""
        return f"<h2>PROFESSIONAL SUMMARY</h2><p>{_esc(content.summary.text)}</p>"

    def _render_skills() -> str:
        if not content.skill_groups:
            return ""
        lines = ["<h2>TECHNICAL SKILLS</h2>"]
        for grp in content.skill_groups:
            if grp.skills:
                skill_csv = ", ".join(_esc(s) for s in grp.skills)
                lines.append(f"<p><b>{_esc(grp.category)}:</b> {skill_csv}</p>")
        return "".join(lines) if len(lines) > 1 else ""

    def _render_experience() -> str:
        if not content.experiences:
            return ""
        lines = ["<h2>EXPERIENCE</h2>"]
        for exp in content.experiences:
            end_str = "Present" if exp.is_current else (exp.end_date or "Present")
            loc_str = f" | {exp.location}" if exp.location else ""
            lines.append(
                f'<p class="item-header">{_esc(exp.role)} — {_esc(exp.company)}</p>'
                f'<p class="item-meta">{_esc(exp.start_date)} – {_esc(end_str)}{_esc(loc_str)}</p>'
            )
            if exp.bullets:
                lines.append("<ul>")
                for b in exp.bullets:
                    lines.append(f"<li>{_esc(b.text)}</li>")
                lines.append("</ul>")
        return "".join(lines)

    def _render_projects() -> str:
        if not content.projects:
            return ""
        lines = ["<h2>PROJECTS</h2>"]
        for proj in content.projects:
            tech_csv = ", ".join(proj.technologies)
            header = f"{proj.name} ({tech_csv})" if tech_csv else proj.name
            lines.append(f'<p class="item-header">{_esc(header)}</p>')
            meta_links: list[str] = []
            for url in (proj.repo_url, proj.live_url):
                safe = _safe_http_url(url)
                if safe:
                    meta_links.append(f'<a href="{_esc(safe)}">{_esc(safe)}</a>')
                    links_to_bind.append((safe, safe))
            if meta_links:
                lines.append(f'<p class="item-meta">{" | ".join(meta_links)}</p>')
            if proj.summary:
                lines.append(f"<p>{_esc(proj.summary)}</p>")
            if proj.bullets:
                lines.append("<ul>")
                for b in proj.bullets:
                    lines.append(f"<li>{_esc(b.text)}</li>")
                lines.append("</ul>")
        return "".join(lines)

    def _render_education() -> str:
        if not content.education:
            return ""
        lines = ["<h2>EDUCATION</h2>"]
        for edu in content.education:
            degree_line = edu.degree
            if edu.field_of_study:
                degree_line = f"{edu.degree} in {edu.field_of_study}"
            years = " – ".join(p for p in (edu.start_year, edu.end_year) if p)
            meta_parts = [p for p in (years, edu.grade_or_cgpa) if p]
            meta = f" ({' | '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"<p><b>{_esc(edu.institution)}</b> — {_esc(degree_line)}{_esc(meta)}</p>")
        return "".join(lines)

    def _render_certifications() -> str:
        if not content.certifications:
            return ""
        lines = ["<h2>CERTIFICATIONS</h2><ul>"]
        for cert in content.certifications:
            date_str = f" ({cert.issued_date})" if cert.issued_date else ""
            safe_url = _safe_http_url(cert.credential_url)
            url_html = ""
            if safe_url:
                url_html = f' — <a href="{_esc(safe_url)}">{_esc(safe_url)}</a>'
                links_to_bind.append((safe_url, safe_url))
            lines.append(f"<li><b>{_esc(cert.name)}</b> — {_esc(cert.issuer)}{_esc(date_str)}{url_html}</li>")
        lines.append("</ul>")
        return "".join(lines)

    def _render_achievements() -> str:
        if not content.achievements:
            return ""
        lines = ["<h2>ACHIEVEMENTS</h2><ul>"]
        for ach in content.achievements:
            lines.append(f"<li>{_esc(ach.text)}</li>")
        lines.append("</ul>")
        return "".join(lines)

    renderers = {
        "summary": _render_summary,
        "skills": _render_skills,
        "experience": _render_experience,
        "projects": _render_projects,
        "education": _render_education,
        "certifications": _render_certifications,
        "achievements": _render_achievements,
    }
    seen_sections: set[str] = set()
    for sec in content.section_order:
        key = sec.strip().lower()
        if key in renderers and key not in seen_sections:
            seen_sections.add(key)
            rendered = renderers[key]()
            if rendered:
                sections_html.append(rendered)
    for key, fn in renderers.items():
        if key not in seen_sections:
            rendered = fn()
            if rendered:
                sections_html.append(rendered)

    full_html = f"<html><body>{''.join(sections_html)}</body></html>"
    return full_html, links_to_bind


def render_tailored_resume_pdf(
    content: TailoredResumeContent,
    *,
    max_pages: int = 2,
    template_version: str = TEMPLATE_VERSION,
) -> PDFRenderResult:
    """Render structured tailored resume content into a deterministic, selectable-text ATS PDF."""
    body_html, links_to_bind = _build_resume_html_and_links(content)
    story = fitz.Story(html=body_html, user_css=_CSS)
    mediabox = fitz.Rect(0, 0, 612, 792)  # US Letter portrait
    where = fitz.Rect(36, 36, 576, 756)   # 0.5-inch ATS-safe margins

    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    more = 1
    pages_drawn = 0
    while more:
        if pages_drawn >= max_pages:
            writer.close()
            raise PDFOverflowError(
                f"Tailored resume exceeds maximum allowed {max_pages} pages under template {template_version}."
            )
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
        pages_drawn += 1
    writer.close()

    # Bind real clickable PDF URI link annotations so ATS & PDF readers preserve links
    links_preserved: list[str] = []
    with fitz.open(stream=buf.getvalue(), filetype="pdf") as pdf_doc:
        for page in pdf_doc:
            for needle, uri in links_to_bind:
                rects = page.search_for(needle)
                for rect in rects:
                    page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": uri})
                    if uri not in links_preserved and not uri.startswith("mailto:"):
                        links_preserved.append(uri)
        full_text = "\n".join(page.get_text("text") for page in pdf_doc)
        final_bytes = pdf_doc.tobytes(garbage=3, deflate=True)

    if "\ufffd" in full_text or not full_text.strip():
        raise ValueError("Rendered PDF contains unreadable or empty extracted text.")

    parsed = parse_resume_bytes(final_bytes, ".pdf")
    ats_report = dict(parsed["ats_report"])
    ats_report["template_version"] = template_version
    ats_report["links_preserved_count"] = len(links_preserved)
    ats_report["within_page_budget"] = len(final_bytes) > 0 and parsed["parsed_metadata"]["page_count"] <= max_pages

    return PDFRenderResult(
        pdf_bytes=final_bytes,
        sha256_checksum=compute_sha256(final_bytes),
        file_size_bytes=len(final_bytes),
        page_count=int(parsed["parsed_metadata"].get("page_count") or pages_drawn),
        template_version=template_version,
        links_preserved=links_preserved,
        ats_report=ats_report,
        parsed_metadata=parsed["parsed_metadata"],
        quality_passed=bool(parsed["validation_passed"] and ats_report["within_page_budget"]),
    )
