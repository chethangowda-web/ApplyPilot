"""Live PostgreSQL (pgvector) + Redis + FastAPI end-to-end Milestone 1 verifier.

Run against a live PostgreSQL + Redis stack:
    DATABASE_URL=postgresql+psycopg://applypilot:applypilot@localhost:5434/applypilot \
    REDIS_URL=redis://localhost:6381/0 \
    python tests/live_postgres_verify.py
"""

import io
import os
import sys
import tempfile
from pathlib import Path
from alembic import command
from alembic.config import Config
import docx
import fitz
import psycopg
import redis
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.core.config import get_settings
from app.main import app


def make_pdf() -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text(
        (72, 72),
        "PROFESSIONAL SUMMARY\nPython & LangGraph engineer.\n"
        "TECHNICAL SKILLS\nPython, FastAPI, PostgreSQL\n"
        "PROJECTS\nORBIT https://github.com/chethangowda-web/ORBIT\n",
    )
    data = doc.tobytes()
    doc.close()
    return data


def make_docx() -> bytes:
    d = docx.Document()
    d.add_paragraph("EDUCATION")
    d.add_paragraph("B.Tech Computer Science")
    d.add_paragraph("EXPERIENCE")
    d.add_paragraph("Intern at Example")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def main() -> int:
    db_url = os.environ["DATABASE_URL"]
    redis_url = os.environ["REDIS_URL"]
    psycopg_dsn = db_url.replace("postgresql+psycopg://", "postgresql://")

    # 1. Verify Redis & PostgreSQL connectivity
    r = redis.from_url(redis_url, socket_timeout=5)
    assert r.ping() is True
    with psycopg.connect(psycopg_dsn, connect_timeout=5) as conn:
        ver = conn.execute("SELECT version()").fetchone()[0]
        print("PostgreSQL:", ver[:60])

    # 2. Migration upgrade -> verify 8 tables, vector extension, VECTOR(768) -> downgrade -> re-upgrade
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")
    with psycopg.connect(psycopg_dsn) as conn:
        ext = conn.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchone()
        assert ext and ext[0] == "vector", "pgvector extension missing"
        vec_col = conn.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'candidate_skills'::regclass AND attname = 'embedding'"
        ).fetchone()
        assert vec_col and vec_col[0] == "vector(768)", f"Unexpected embedding type: {vec_col}"

    from app.database.models import CandidateProfile, CandidateSkill, Project, User
    from app.database.session import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    engine = get_engine()
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    expected = {
        "users",
        "candidate_profiles",
        "candidate_skills",
        "projects",
        "experiences",
        "certifications",
        "resumes",
        "audit_logs",
        "job_sources",
        "jobs",
        "job_requirements",
        "job_matches",
        "workflow_runs",
        "job_analysis_history",
        "auto_tailor_daily_quotas",
        "resume_generation_jobs",
        "resume_job_mappings",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"
    with psycopg.connect(psycopg_dsn) as conn:
        jd_vec_col = conn.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'jobs'::regclass AND attname = 'jd_embedding'"
        ).fetchone()
        assert jd_vec_col and jd_vec_col[0] == "vector(768)", f"Unexpected jd_embedding type: {jd_vec_col}"
    # Verify foreign keys and indexes exist
    fks = insp.get_foreign_keys("candidate_skills")
    assert any(fk["referred_table"] == "candidate_profiles" for fk in fks)
    idxs = {idx["name"] for idx in insp.get_indexes("resumes")}
    assert "ix_resumes_sha256_checksum" in idxs

    # Direct ORM CRUD round-trip including pgvector VECTOR(768)
    with get_session_factory()() as session:
        u = User(email="orm_crud_probe@example.com", hashed_password="hashed-value")
        session.add(u)
        session.flush()
        p = CandidateProfile(user_id=u.id, full_name="ORM Probe", email=u.email)
        session.add(p)
        session.flush()
        vec = [0.01] * 768
        sk = CandidateSkill(profile_id=p.id, skill_name="LangGraph", category="Agentic AI", embedding=vec)
        pr = Project(profile_id=p.id, name="ORBIT", summary="Multi-agent", technologies=["Python", "LangGraph"])
        session.add_all([sk, pr])
        session.commit()
        loaded_sk = session.get(CandidateSkill, sk.id)
        assert loaded_sk is not None and len(loaded_sk.embedding) == 768
        session.delete(u)
        session.commit()
    engine.dispose()
    print("Direct PostgreSQL ORM CRUD + VECTOR(768) round-trip PASSED")

    # Rollback and reapply
    command.downgrade(cfg, "base")
    with psycopg.connect(psycopg_dsn) as conn:
        langgraph_tables = {
            "alembic_version",
            "checkpoint_blobs",
            "checkpoint_migrations",
            "checkpoint_writes",
            "checkpoints",
        }
        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
        } - langgraph_tables
        assert not remaining, f"Tables survived downgrade: {remaining}"
    command.upgrade(cfg, "head")
    print("Alembic upgrade/downgrade/reapply on live PostgreSQL PASSED")

    # 3. End-to-end API integration against live PostgreSQL
    with tempfile.TemporaryDirectory() as tmp_storage:
        os.environ["STORAGE_DIR"] = tmp_storage
        get_settings.cache_clear()
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            email = "live_verify@example.com"
            reg = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"})
            assert reg.status_code in (201, 409)
            tok = client.post("/api/auth/login", json={"email": email, "password": "StrongPassword123!"})
            assert tok.status_code == 200
            headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}

            prof = client.post(
                "/api/profile",
                headers=headers,
                json={"full_name": "Live Candidate", "email": email, "location": "Bengaluru"},
            )
            assert prof.status_code == 200
            assert client.post(
                "/api/profile/skills",
                headers=headers,
                json={"skill_name": "LangGraph", "category": "Agentic AI"},
            ).status_code == 201

            pdf = make_pdf()
            up_pdf = client.post(
                "/api/resume/upload",
                headers=headers,
                files={"file": ("../../Master.pdf", pdf, "application/pdf")},
                data={"is_master": "true"},
            )
            assert up_pdf.status_code == 201
            assert up_pdf.json()["original_filename"] == "Master.pdf"
            rid = up_pdf.json()["id"]
            assert client.get("/api/resume/versions", headers=headers).status_code == 200
            dl = client.get(f"/api/resume/versions/{rid}/download", headers=headers)
            assert dl.status_code == 200 and dl.content == pdf

            # Duplicate checksum rejected on live Postgres
            dup = client.post(
                "/api/resume/upload",
                headers=headers,
                files={"file": ("Master_dup.pdf", pdf, "application/pdf")},
                data={"is_master": "true"},
            )
            assert dup.status_code == 409

            # Second user cannot access first user's profile or resume on live Postgres
            email2 = "live_verify_2@example.com"
            reg2 = client.post("/api/auth/register", json={"email": email2, "password": "StrongPassword123!"})
            assert reg2.status_code in (201, 409)
            tok2 = client.post("/api/auth/login", json={"email": email2, "password": "StrongPassword123!"})
            headers2 = {"Authorization": f"Bearer {tok2.json()['access_token']}"}
            assert client.get(f"/api/resume/versions/{rid}", headers=headers2).status_code == 404
            assert client.get(f"/api/resume/versions/{rid}/download", headers=headers2).status_code == 404
            assert client.get("/api/profile", headers=headers2).status_code == 404

            # Invalid JWT rejected
            assert client.get("/api/auth/me", headers={"Authorization": "Bearer invalid.tok"}).status_code == 401

            up_docx = client.post(
                "/api/resume/upload",
                headers=headers,
                files={
                    "file": (
                        "Master.docx",
                        make_docx(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                data={"is_master": "true"},
            )
            assert up_docx.status_code == 201

            # Milestone 2 live PostgreSQL Job Discovery + LangGraph + Dedup + Scoring check
            import httpx
            from app.api.routes.jobs import get_discovery_http_client

            client.post(
                "/api/profile",
                headers=headers,
                json={
                    "full_name": "Live Candidate",
                    "email": email,
                    "location": "Bengaluru, India",
                    "education": [{"institution": "Presidency", "degree": "B.Tech", "field_of_study": "CSE"}],
                },
            )
            client.post(
                "/api/profile/skills",
                headers=headers,
                json={"skill_name": "Python", "category": "Languages"},
            )
            client.post(
                "/api/profile/skills",
                headers=headers,
                json={"skill_name": "FastAPI", "category": "Backend"},
            )

            def live_mock_handler(request: httpx.Request) -> httpx.Response:
                if "boards-api.greenhouse.io" in str(request.url):
                    return httpx.Response(
                        200,
                        json={
                            "jobs": [
                                {
                                    "id": 9001,
                                    "title": "AI Engineer Intern",
                                    "location": {"name": "Bengaluru, India"},
                                    "first_published": "2026-11-25T08:00:00Z",
                                    "absolute_url": "https://boards.greenhouse.io/liveco/jobs/9001",
                                    "content": "Python and FastAPI agentic engineering. 0-1 years.",
                                }
                            ],
                            "meta": {"total_pages": 1},
                        },
                    )
                return httpx.Response(404)

            mock_http = httpx.AsyncClient(transport=httpx.MockTransport(live_mock_handler))
            app.dependency_overrides[get_discovery_http_client] = lambda: mock_http
            try:
                src_resp = client.post(
                    "/api/jobs/sources",
                    headers=headers,
                    json={
                        "source_name": "gh-liveco",
                        "connector_type": "greenhouse",
                        "config": {"board_token": "liveco", "company_name": "LiveCo"},
                    },
                )
                assert src_resp.status_code in (201, 409)
                disc = client.post("/api/jobs/discover", headers=headers, json={"query": ""})
                assert disc.status_code == 200
                jobs_list = client.get("/api/jobs?eligible_only=true", headers=headers).json()
                assert len(jobs_list) >= 1
                jid = jobs_list[0]["id"]
                assert client.get(f"/api/jobs/{jid}", headers=headers).status_code == 200
                assert client.get(f"/api/jobs/{jid}/match", headers=headers).status_code == 200
                assert client.get("/api/jobs/sources/health", headers=headers).status_code == 200

                # Milestone 3 live Job Intelligence + pgvector + Analysis History + Workflows check
                ana_resp = client.post(f"/api/jobs/{jid}/analyze?use_llm=false", headers=headers)
                assert ana_resp.status_code == 200
                assert ana_resp.json()["semantic_similarity"] is not None
                intel_resp = client.get(f"/api/jobs/{jid}/intelligence", headers=headers)
                assert intel_resp.status_code == 200
                assert intel_resp.json()["skill_coverage"]["required_skill_coverage"] >= 0.0
                ev_resp = client.get(f"/api/jobs/{jid}/evidence", headers=headers)
                assert ev_resp.status_code == 200
                elig_resp = client.get(f"/api/jobs/{jid}/eligibility", headers=headers)
                assert elig_resp.status_code == 200
                hist_resp = client.get(f"/api/jobs/{jid}/history", headers=headers)
                assert hist_resp.status_code == 200 and len(hist_resp.json()) >= 1
                run_id = hist_resp.json()[0]["run_id"]
                if run_id:
                    wf_resp = client.get(f"/api/workflows/{run_id}", headers=headers)
                    assert wf_resp.status_code == 200
                    with psycopg.connect(psycopg_dsn) as conn:
                        cp_count = conn.execute(
                            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
                            (run_id,),
                        ).fetchone()[0]
                        assert cp_count >= 1, f"Expected Postgres checkpoint rows for {run_id}, found {cp_count}"
            finally:
                app.dependency_overrides.pop(get_discovery_http_client, None)

            print("Live PostgreSQL M1 + M2 + M3 API & security integration PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
