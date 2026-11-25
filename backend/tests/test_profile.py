import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.session import get_db
from app.main import app


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _register_and_login(client: TestClient, email: str) -> dict[str, str]:
    reg = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"})
    assert reg.status_code == 201
    tok = client.post("/api/auth/login", json={"email": email, "password": "StrongPassword123!"})
    assert tok.status_code == 200
    return {"Authorization": f"Bearer {tok.json()['access_token']}"}


def test_profile_crud_and_subresources(client: TestClient):
    headers = _register_and_login(client, "alice@example.com")

    assert client.get("/api/profile", headers=headers).status_code == 404

    upsert = client.post(
        "/api/profile",
        headers=headers,
        json={
            "full_name": "Alice Candidate",
            "email": "alice@example.com",
            "location": "Bengaluru, India",
            "github_url": "https://github.com/alice",
            "education": [
                {
                    "institution": "Presidency University",
                    "degree": "B.Tech CSE",
                    "cgpa": "8.0",
                    "start_date": "Sep 2024",
                    "end_date": "Aug 2028",
                }
            ],
            "achievements": ["1st Place — Hackathon"],
        },
    )
    assert upsert.status_code == 200
    assert upsert.json()["full_name"] == "Alice Candidate"

    skill = client.post(
        "/api/profile/skills",
        headers=headers,
        json={"skill_name": "LangGraph", "category": "Agentic AI", "verified": True},
    )
    assert skill.status_code == 201
    skill_id = skill.json()["id"]

    proj = client.post(
        "/api/profile/projects",
        headers=headers,
        json={
            "name": "ORBIT",
            "summary": "Multi-agent system",
            "technologies": ["Python", "FastAPI", "LangGraph"],
            "implemented_features": ["LangGraph supervisor"],
            "planned_features": ["MCP integration"],
            "verified_metrics": ["400+ pytest tests"],
        },
    )
    assert proj.status_code == 201
    proj_id = proj.json()["id"]

    exp = client.post(
        "/api/profile/experiences",
        headers=headers,
        json={
            "company": "Codec Technologies",
            "role": "Web Developer Intern",
            "start_date": "Feb 2026",
            "end_date": "Apr 2026",
            "verified_responsibilities": ["Built REST APIs"],
        },
    )
    assert exp.status_code == 201

    cert = client.post(
        "/api/profile/certifications",
        headers=headers,
        json={"name": "Python Foundations", "issuer": "Coursera"},
    )
    assert cert.status_code == 201

    fetched = client.get("/api/profile", headers=headers)
    assert fetched.status_code == 200
    body = fetched.json()
    assert len(body["skills"]) == 1
    assert len(body["projects"]) == 1
    assert body["projects"][0]["planned_features"] == ["MCP integration"]
    assert len(body["experiences"]) == 1
    assert len(body["certifications"]) == 1

    assert client.delete(f"/api/profile/skills/{skill_id}", headers=headers).status_code == 200
    assert client.delete(f"/api/profile/projects/{proj_id}", headers=headers).status_code == 200


def test_profile_user_isolation(client: TestClient):
    alice_headers = _register_and_login(client, "alice2@example.com")
    bob_headers = _register_and_login(client, "bob2@example.com")

    client.post(
        "/api/profile",
        headers=alice_headers,
        json={"full_name": "Alice", "email": "alice2@example.com"},
    )
    skill = client.post(
        "/api/profile/skills",
        headers=alice_headers,
        json={"skill_name": "Python", "category": "Languages"},
    )
    alice_skill_id = skill.json()["id"]

    # Bob has no profile yet and cannot see Alice's profile or delete Alice's skill
    assert client.get("/api/profile", headers=bob_headers).status_code == 404

    client.post(
        "/api/profile",
        headers=bob_headers,
        json={"full_name": "Bob", "email": "bob2@example.com"},
    )
    bob_get = client.get("/api/profile", headers=bob_headers)
    assert bob_get.status_code == 200
    assert bob_get.json()["full_name"] == "Bob"
    assert bob_get.json()["skills"] == []

    del_attempt = client.delete(f"/api/profile/skills/{alice_skill_id}", headers=bob_headers)
    assert del_attempt.status_code == 404

    alice_get = client.get("/api/profile", headers=alice_headers)
    assert len(alice_get.json()["skills"]) == 1
