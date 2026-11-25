import io
from pathlib import Path
import docx
import fitz
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.resume_intelligence.evidence_retriever import EvidenceRetriever
from app.agents.resume_intelligence.resume_analyzer import ResumeAnalyzerAgent
from app.database.base import Base
from app.database.models import (
    CandidateProfile,
    CandidateSkill,
    Certification,
    Experience,
    Job,
    JobRequirement,
    JobSource,
    Project,
    Resume,
    User,
)
from app.schemas.resume_intelligence import (
    AttributedTextItem,
    TailoredCertificationItem,
    TailoredEducationItem,
    TailoredExperienceItem,
    TailoredProjectItem,
    TailoredResumeContent,
    TailoredSkillGroup,
)
from app.services.fingerprint_service import (
    compute_evidence_fingerprint,
    compute_generation_idempotency_key,
)
from app.services.pdf_resume_service import PDFOverflowError, render_tailored_resume_pdf
from app.services.resume_vault_service import compute_sha256, store_resume_bytes


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


def _make_docx_bytes(lines: list[str]) -> bytes:
    d = docx.Document()
    for line in lines:
        d.add_paragraph(line)
    bio = io.BytesIO()
    d.save(bio)
    return bio.getvalue()


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with SessionLocal() as db:
        yield db
    engine.dispose()


def _seed_full_candidate_and_job(db: Session, storage_root: Path):
    user = User(email="stage2@example.com", hashed_password="hashed-password")
    db.add(user)
    db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        full_name="Chethan Gowda",
        email="chethan@example.com",
        phone="+91-9000000001",
        location="Bengaluru, India",
        github_url="https://github.com/chethan",
        linkedin_url="https://linkedin.com/in/chethan",
        portfolio_url="https://chethan.dev",
        summary="Backend & AI Engineer experienced in Python, FastAPI, and LangGraph.",
        education=[{"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"}],
        achievements=["SIH Finalist 2025"],
    )
    db.add(profile)
    db.flush()

    sk1 = CandidateSkill(profile_id=profile.id, skill_name="Python", category="Languages", verified=True)
    sk2 = CandidateSkill(profile_id=profile.id, skill_name="LangGraph", category="AI", verified=True)
    sk3 = CandidateSkill(profile_id=profile.id, skill_name="FastAPI", category="Backend", verified=True)
    sk_unverified = CandidateSkill(
        profile_id=profile.id, skill_name="Rust", category="Languages", verified=False
    )
    db.add_all([sk1, sk2, sk3, sk_unverified])

    proj = Project(
        profile_id=profile.id,
        name="ORBIT",
        summary="Multi-agent workflow orchestrator",
        technologies=["Python", "LangGraph", "FastAPI"],
        implemented_features=["Built checkpointed state graph", "Added FastAPI REST endpoints"],
        planned_features=["Deploy Kubernetes autoscaler"],
        verified_metrics=["Processed 500 workflows with 99% deterministic replay"],
        repo_url="https://github.com/chethan/orbit",
    )
    exp = Experience(
        profile_id=profile.id,
        company="Acme AI",
        role="Backend Intern",
        location="Bengaluru",
        start_date="2025-05",
        end_date="2025-08",
        verified_responsibilities=["Implemented FastAPI services and PostgreSQL migrations"],
    )
    cert = Certification(
        profile_id=profile.id,
        name="AWS Certified Cloud Practitioner",
        issuer="AWS",
        issued_date="2025-09",
        credential_url="https://aws.amazon.com/verify/123",
        verified=True,
    )
    db.add_all([proj, exp, cert])

    master_bytes = _make_pdf_bytes(
        "PROFESSIONAL SUMMARY\nPython developer.\n"
        "TECHNICAL SKILLS\nPython, LangGraph, FastAPI\n"
        "PROJECTS\nORBIT https://github.com/chethan/orbit\n"
    )
    master_id = __import__("uuid").uuid4()
    master_path = store_resume_bytes(
        storage_root=storage_root,
        user_id=user.id,
        resume_id=master_id,
        safe_filename="master.pdf",
        is_master=True,
        data=master_bytes,
    )
    master = Resume(
        id=master_id,
        profile_id=profile.id,
        is_master=True,
        original_filename="master.pdf",
        content_type="application/pdf",
        file_size_bytes=len(master_bytes),
        version_label="master-v1",
        pdf_path=str(master_path),
        sha256_checksum=compute_sha256(master_bytes),
    )
    db.add(master)

    src = JobSource(source_name="gh-s2", connector_type="greenhouse")
    db.add(src)
    db.flush()

    job = Job(
        source_id=src.id,
        source_job_id="202",
        canonical_url="https://boards.greenhouse.io/s2/jobs/202",
        dedup_key="a" * 64,
        content_checksum="b" * 64,
        company="DiscoverCo",
        role_title="AI Engineer Intern",
        raw_jd="Looking for Python, LangGraph, FastAPI.",
    )
    db.add(job)
    db.flush()

    req = JobRequirement(
        job_id=job.id,
        role_category="AI Engineer",
        required_skills=["Python", "LangGraph"],
        preferred_skills=["FastAPI"],
        technical_stack=["Python", "LangGraph", "FastAPI"],
        responsibilities=["Build multi-agent AI workflows"],
    )
    db.add(req)
    db.commit()
    return profile, master, job, req, proj


def test_resume_analyzer_handles_pdf_docx_and_missing_master(db_session, tmp_path):
    profile, master, _job, _req, _proj = _seed_full_candidate_and_job(db_session, tmp_path)
    analyzer = ResumeAnalyzerAgent()

    # 1. PDF master
    pdf_analysis = analyzer.analyze(db_session, profile=profile, master_resume=master, storage_dir=tmp_path)
    assert pdf_analysis.has_master_file is True
    assert pdf_analysis.file_format == "pdf"
    assert "skills" in pdf_analysis.detected_sections
    assert "https://github.com/chethan/orbit" in pdf_analysis.public_links

    # 2. DOCX master
    docx_bytes = _make_docx_bytes(
        [
            "EDUCATION",
            "B.Tech CS",
            "EXPERIENCE",
            "- Built APIs at Acme AI",
        ]
    )
    docx_id = __import__("uuid").uuid4()
    docx_path = store_resume_bytes(
        storage_root=tmp_path,
        user_id=profile.user_id,
        resume_id=docx_id,
        safe_filename="master.docx",
        is_master=True,
        data=docx_bytes,
    )
    docx_master = Resume(
        id=docx_id,
        profile_id=profile.id,
        is_master=True,
        original_filename="master.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size_bytes=len(docx_bytes),
        version_label="master-v2",
        docx_path=str(docx_path),
        sha256_checksum=compute_sha256(docx_bytes),
    )
    db_session.add(docx_master)
    db_session.flush()
    docx_analysis = analyzer.analyze(
        db_session, profile=profile, master_resume=docx_master, storage_dir=tmp_path
    )
    assert docx_analysis.has_master_file is True
    assert docx_analysis.file_format == "docx"
    assert docx_analysis.bullet_count >= 1

    # 3. Missing master resume baseline
    user2 = User(email="nomaster@example.com", hashed_password="h")
    db_session.add(user2)
    db_session.flush()
    p2 = CandidateProfile(user_id=user2.id, full_name="No Master", email="nomaster@example.com", summary="Hi")
    db_session.add(p2)
    db_session.flush()
    no_master = analyzer.analyze(db_session, profile=p2, storage_dir=tmp_path)
    assert no_master.has_master_file is False
    assert no_master.uncertainties


def test_evidence_retriever_and_complete_fingerprint_sensitivity(db_session, tmp_path):
    profile, master, job, req, proj = _seed_full_candidate_and_job(db_session, tmp_path)
    retriever = EvidenceRetriever()

    ev1 = retriever.retrieve(db_session, profile=profile, job=job, req=req, master_resume=master)
    assert ev1.excluded_unverified_skills == ["Rust"]
    assert all(s.skill_name != "Rust" for s in ev1.skills)
    assert proj.id.hex or ev1.projects[0].implemented_features
    assert str(proj.id) in ev1.excluded_planned_features_by_project
    assert ev1.skills[0].evidence_id.startswith("skill:")
    assert ev1.projects[0].evidence_id == f"project:{proj.id}"
    assert ev1.experiences[0].evidence_id.startswith("exp:")
    # Content-derived education and achievement IDs (not positional)
    assert ev1.education[0].evidence_id.startswith("edu:")
    assert len(ev1.education[0].evidence_id) > len("edu:")  # has hash suffix
    assert ev1.achievements[0].evidence_id.startswith("ach:")
    assert len(ev1.achievements[0].evidence_id) > len("ach:")  # has hash suffix

    fp_base = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_base == ev1.evidence_fingerprint

    # Idempotency key is shared across auto & non-forced manual
    k1 = compute_generation_idempotency_key(profile_id=profile.id, job_id=job.id, evidence_fingerprint=fp_base)
    k2 = compute_generation_idempotency_key(profile_id=profile.id, job_id=job.id, evidence_fingerprint=fp_base)
    assert k1 == k2

    # Unrendered planned_features must NOT change fingerprint
    proj.planned_features = ["Completely different future roadmap item"]
    db_session.flush()
    assert compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req) == fp_base

    # Changing ANY resume-rendered field MUST change the fingerprint
    profile.phone = "+91-9999999999"
    db_session.flush()
    fp_phone = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_phone != fp_base

    proj.verified_metrics = ["Processed 1000 workflows"]
    db_session.flush()
    fp_metric = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_metric != fp_phone

    profile.achievements = ["SIH Winner 2026"]
    db_session.flush()
    fp_ach = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_ach != fp_metric

    req.required_skills = ["Python", "LangGraph", "PostgreSQL"]
    db_session.flush()
    fp_req = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_req != fp_ach

    fp_tpl = compute_evidence_fingerprint(
        profile=profile, master_resume=master, job=job, req=req, template_version="ats-classic-v2"
    )
    assert fp_tpl != fp_req


def test_evidence_reorder_invariance_and_content_sensitivity(db_session, tmp_path):
    """Reordering education/achievements must NOT change fingerprint; content changes MUST."""
    profile, master, job, req, _proj = _seed_full_candidate_and_job(db_session, tmp_path)

    # Base fingerprint with single education + single achievement
    fp_base = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)

    # Add a second education record and second achievement
    profile.education = [
        {"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"},
        {"institution": "MIT", "degree": "M.Tech", "field_of_study": "AI", "end_year": "2028"},
    ]
    profile.achievements = ["SIH Finalist 2025", "Google Summer of Code 2026"]
    db_session.flush()
    fp_two_items = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_two_items != fp_base  # adding records changes fingerprint

    # REORDER education — fingerprint must stay the same
    profile.education = [
        {"institution": "MIT", "degree": "M.Tech", "field_of_study": "AI", "end_year": "2028"},
        {"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"},
    ]
    db_session.flush()
    fp_edu_reordered = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_edu_reordered == fp_two_items, "Education reorder must NOT change fingerprint"

    # REORDER achievements — fingerprint must stay the same
    profile.achievements = ["Google Summer of Code 2026", "SIH Finalist 2025"]
    db_session.flush()
    fp_ach_reordered = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_ach_reordered == fp_two_items, "Achievement reorder must NOT change fingerprint"

    # CONTENT CHANGE in education must change fingerprint
    profile.education = [
        {"institution": "MIT", "degree": "M.Tech", "field_of_study": "AI", "end_year": "2028"},
        {"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2027"},  # changed year
    ]
    db_session.flush()
    fp_edu_changed = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_edu_changed != fp_two_items, "Education content change must change fingerprint"

    # CONTENT CHANGE in achievement must change fingerprint
    profile.education = [
        {"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"},
        {"institution": "MIT", "degree": "M.Tech", "field_of_study": "AI", "end_year": "2028"},
    ]
    profile.achievements = ["SIH Finalist 2025", "Google Summer of Code 2027"]  # changed year
    db_session.flush()
    fp_ach_changed = compute_evidence_fingerprint(profile=profile, master_resume=master, job=job, req=req)
    assert fp_ach_changed != fp_two_items, "Achievement content change must change fingerprint"

    # Verify EvidenceRetriever uses content-derived IDs too
    retriever = EvidenceRetriever()
    profile.education = [
        {"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"},
        {"institution": "MIT", "degree": "M.Tech", "field_of_study": "AI", "end_year": "2028"},
    ]
    profile.achievements = ["SIH Finalist 2025", "Google Summer of Code 2026"]
    db_session.flush()
    ev_original = retriever.retrieve(db_session, profile=profile, job=job, req=req, master_resume=master)
    edu_ids_original = [e.evidence_id for e in ev_original.education]
    ach_ids_original = [a.evidence_id for a in ev_original.achievements]

    # Reverse order
    profile.education = [
        {"institution": "MIT", "degree": "M.Tech", "field_of_study": "AI", "end_year": "2028"},
        {"institution": "VTU", "degree": "B.Tech", "field_of_study": "CS", "end_year": "2026"},
    ]
    profile.achievements = ["Google Summer of Code 2026", "SIH Finalist 2025"]
    db_session.flush()
    ev_reordered = retriever.retrieve(db_session, profile=profile, job=job, req=req, master_resume=master)
    edu_ids_reordered = [e.evidence_id for e in ev_reordered.education]
    ach_ids_reordered = [a.evidence_id for a in ev_reordered.achievements]

    # Same IDs present (order of records themselves may differ, but IDs are the same set)
    assert set(edu_ids_original) == set(edu_ids_reordered), "Education IDs must be the same regardless of order"
    assert set(ach_ids_original) == set(ach_ids_reordered), "Achievement IDs must be the same regardless of order"
    # Fingerprints must match
    assert ev_original.evidence_fingerprint == ev_reordered.evidence_fingerprint


def test_pdf_generation_selectable_links_escaping_and_overflow():
    content = TailoredResumeContent(
        full_name="Chethan Gowda & Partners <Dev>",
        email="chethan@example.com",
        phone="+91-9000000001",
        location="Bengaluru, India",
        github_url="https://github.com/chethan",
        linkedin_url="https://linkedin.com/in/chethan",
        summary=AttributedTextItem(text="Backend & AI Engineer building LangGraph workflows.", evidence_ids=["skill:1"]),
        skill_groups=[TailoredSkillGroup(category="AI & Backend", skills=["Python", "LangGraph", "FastAPI"])],
        experiences=[
            TailoredExperienceItem(
                experience_id="exp:1",
                company="Acme AI",
                role="Backend Intern",
                location="Bengaluru",
                start_date="2025-05",
                end_date="2025-08",
                bullets=[AttributedTextItem(text="Built FastAPI services <v2>.", evidence_ids=["exp:1"])],
            )
        ],
        projects=[
            TailoredProjectItem(
                project_id="project:1",
                name="ORBIT",
                summary="Multi-agent orchestrator",
                technologies=["Python", "LangGraph"],
                repo_url="https://github.com/chethan/orbit",
                bullets=[AttributedTextItem(text="Processed 500 workflows.", evidence_ids=["project:1"])],
            )
        ],
        education=[TailoredEducationItem(evidence_id="edu:440750d17d7b0952", institution="VTU", degree="B.Tech", end_year="2026")],
        certifications=[
            TailoredCertificationItem(
                certification_id="cert:1",
                name="AWS CCP",
                issuer="AWS",
                issued_date="2025-09",
                credential_url="https://aws.amazon.com/verify/123",
            )
        ],
        achievements=[AttributedTextItem(text="SIH Finalist 2025", evidence_ids=["ach:ba1bf15d5e4d1ac4"])],
    )

    result = render_tailored_resume_pdf(content)
    assert result.quality_passed is True
    assert result.page_count == 1
    assert "https://github.com/chethan" in result.links_preserved
    assert "https://github.com/chethan/orbit" in result.links_preserved
    assert result.ats_report["text_extractable"] is True
    assert result.ats_report["has_standard_headings"] is True

    with fitz.open(stream=result.pdf_bytes, filetype="pdf") as doc:
        extracted = doc[0].get_text("text")
        assert "Chethan Gowda & Partners <Dev>" in extracted
        assert "PROFESSIONAL SUMMARY" in extracted
        assert "ORBIT" in extracted
        uris = {lnk.get("uri") for lnk in doc[0].get_links() if lnk.get("uri")}
        assert "https://github.com/chethan" in uris
        assert "https://github.com/chethan/orbit" in uris

    # Overflow check when content exceeds max_pages
    huge_bullets = [
        AttributedTextItem(
            text=f"Implemented large-scale distributed workflow pipeline component #{i} with strict validation.",
            evidence_ids=["project:1"],
        )
        for i in range(120)
    ]
    overflow_content = content.model_copy(
        update={
            "projects": [
                TailoredProjectItem(
                    project_id="project:1",
                    name="ORBIT",
                    summary="Oversized project section",
                    technologies=["Python"],
                    bullets=huge_bullets,
                )
            ]
        }
    )
    with pytest.raises(PDFOverflowError):
        render_tailored_resume_pdf(overflow_content, max_pages=2)
