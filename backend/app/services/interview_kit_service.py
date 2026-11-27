from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    Application,
    CandidateProfile,
    CandidateSkill,
    InterviewKit,
    InterviewMemory,
    Job,
    JobRequirement,
    Project,
    Resume,
    User,
)
from app.schemas.career import InterviewKitProgressRequest, InterviewKitResponse
from app.services.audit_service import record_audit_event

KIT_MODULE_KEYS = (
    "company_role_research",
    "cs_fundamentals",
    "dsa",
    "role_specific_technical",
    "resume_project_deep_dive",
    "system_design",
    "behavioral_star",
    "mock_interview_prep",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _build_modules(
    *,
    job: Job,
    req: JobRequirement | None,
    resume: Resume,
    skills: list[str],
    projects: list[Project],
    weak_topics: list[str],
) -> dict[str, Any]:
    now_iso = _utc_now().isoformat()
    required_skills = list(req.required_skills) if req and isinstance(req.required_skills, list) else []
    preferred_skills = list(req.preferred_skills) if req and isinstance(req.preferred_skills, list) else []
    focus_skills = required_skills[:6] or skills[:6] or ["Python", "SQL", "REST APIs"]
    top_project = projects[0] if projects else None
    project_name = top_project.name if top_project else "Verified Resume Project"
    project_stack = ", ".join(top_project.technologies[:4]) if top_project and top_project.technologies else ", ".join(focus_skills[:3])

    return {
        "company_role_research": {
            "title": "1. Company & Role Research",
            "verified_facts": [
                {
                    "fact": f"Company: {job.company}; Role: {job.role_title}",
                    "source_url": job.canonical_url,
                    "retrieved_at": now_iso,
                },
                {
                    "fact": f"Required stack from JD: {', '.join(focus_skills)}",
                    "source_url": job.canonical_url,
                    "retrieved_at": now_iso,
                },
            ],
            "reasonable_inferences": [
                f"Interviewers for {job.role_title} at {job.company} will likely probe production reliability, testing, and clear trade-off reasoning."
            ],
            "topics_to_study": [job.company, job.role_title, *preferred_skills[:3]],
            "questions": [
                {
                    "id": "cr-1",
                    "question": f"Why are you interested in the {job.role_title} role at {job.company}?",
                    "suggested_answer": f"Connect your verified work on {project_name} ({project_stack}) directly to {job.company}'s posted requirements.",
                    "explanation": "Ground motivation in specific engineering problems from the JD rather than generic praise.",
                    "example": f"Reference {project_name} and its real deliverables.",
                    "difficulty": "easy",
                    "evidence_source": f"job:{job.id}",
                    "grounding_type": "verified_job_and_resume",
                }
            ],
            "practice_tasks": [f"Review {job.company} posting at {job.canonical_url} and list 3 role priorities."],
        },
        "cs_fundamentals": {
            "title": "2. Computer Science Fundamentals",
            "topics_to_study": ["Concurrency & Async I/O", "Database Indexing & Transactions", "HTTP/REST & Caching", *weak_topics[:2]],
            "questions": [
                {
                    "id": "cs-1",
                    "question": "Explain database transaction isolation levels and when you would use row-level locking (`SELECT ... FOR UPDATE`).",
                    "suggested_answer": "Discuss Read Committed vs Serializable, lost-update prevention, and locking target rows during quota/state transitions.",
                    "explanation": "Tests practical backend correctness under concurrent requests.",
                    "example": "Idempotent quota reservation or status transition locking.",
                    "difficulty": "medium",
                    "evidence_source": "cs_fundamentals",
                    "grounding_type": "technical_reference",
                }
            ],
            "practice_tasks": ["Write out ACID properties and explain index B-tree trade-offs."],
        },
        "dsa": {
            "title": "3. Data Structures & Algorithms",
            "topics_to_study": ["Hash Maps & Sliding Window", "Trees & Graphs (BFS/DFS)", "Heaps & Intervals", "Time/Space Complexity"],
            "questions": [
                {
                    "id": "dsa-1",
                    "question": "Given a stream of job postings with canonical keys, how do you deduplicate and retrieve the top-K highest-scoring jobs in O(N log K)?",
                    "suggested_answer": "Use a hash set/map keyed by canonical ID for O(1) deduplication and a min-heap of size K for top-K selection.",
                    "explanation": "Balances streaming memory bounds with deterministic complexity.",
                    "example": "heapq.heappushpop in Python for O(N log K) time and O(K) space.",
                    "difficulty": "medium",
                    "evidence_source": "dsa_core",
                    "grounding_type": "technical_reference",
                }
            ],
            "practice_tasks": ["Solve 2 medium array/graph problems and state time/space complexity aloud."],
        },
        "role_specific_technical": {
            "title": f"4. Role-Specific Technical Prep ({job.role_title})",
            "topics_to_study": focus_skills,
            "questions": [
                {
                    "id": "rs-1",
                    "question": f"How do you structure validation, error handling, and idempotency in services built with {', '.join(focus_skills[:3])}?",
                    "suggested_answer": "Validate inputs at boundaries with strict schemas, isolate external calls with timeouts/retries, and use DB uniqueness keys.",
                    "explanation": "Directly addresses reliability expectations for backend/AI engineering roles.",
                    "example": "Schema validation + transactional outbox + bounded retries.",
                    "difficulty": "medium",
                    "evidence_source": f"job_requirements:{job.id}",
                    "grounding_type": "verified_job_requirements",
                }
            ],
            "practice_tasks": [f"Review core patterns for: {', '.join(focus_skills)}."],
        },
        "resume_project_deep_dive": {
            "title": "5. Resume & Project Deep Dive (Strictly Grounded)",
            "resume_checksum": resume.sha256_checksum,
            "topics_to_study": [project_name, *skills[:4]],
            "questions": [
                {
                    "id": "rp-1",
                    "question": f"Walk through the architecture and toughest engineering trade-off in {project_name}.",
                    "suggested_answer": f"Describe only verified implementation details from your approved resume (SHA-256 {resume.sha256_checksum[:12]}...) using {project_stack}.",
                    "explanation": "Never extrapolate unverified metrics; explain real design choices and failure modes.",
                    "example": top_project.summary[:220] if top_project and top_project.summary else f"Project: {project_name}",
                    "difficulty": "medium",
                    "evidence_source": f"resume:{resume.id}:sha256:{resume.sha256_checksum}",
                    "grounding_type": "verified_resume_evidence",
                }
            ],
            "practice_tasks": [f"Prepare a 90-second architecture walkthrough of {project_name}."],
        },
        "system_design": {
            "title": "6. System Design",
            "topics_to_study": ["Asynchronous Worker Queues", "Rate Limiting & Circuit Breakers", "PostgreSQL + Redis Coordination"],
            "questions": [
                {
                    "id": "sd-1",
                    "question": "Design a reliable background job pipeline that handles worker crashes without duplicate external side effects.",
                    "suggested_answer": "Use a transactional outbox, worker leases with heartbeats, idempotency keys, and dead-letter handling.",
                    "explanation": "Demonstrates production systems thinking.",
                    "example": "PostgreSQL outbox + Celery acks_late + Redis coordination.",
                    "difficulty": "hard",
                    "evidence_source": "system_design_core",
                    "grounding_type": "technical_reference",
                }
            ],
            "practice_tasks": ["Sketch data flow, failure modes, and recovery for a queue-backed service."],
        },
        "behavioral_star": {
            "title": "7. STAR Behavioral Preparation",
            "topics_to_study": ["Debugging Under Ambiguity", "Handling Disagreement", "Ownership & Delivery"],
            "questions": [
                {
                    "id": "bh-1",
                    "question": "Tell me about a time you caught a subtle bug or design flaw before it reached production.",
                    "suggested_answer": "Structure with Situation, Task, Action (root-cause diagnosis + tests), and Result grounded in your actual work.",
                    "explanation": "Interviewers look for ownership, systematic debugging, and measurable follow-through.",
                    "example": "Use a verified experience from your Master Profile.",
                    "difficulty": "medium",
                    "evidence_source": f"resume:{resume.id}",
                    "grounding_type": "verified_resume_evidence",
                }
            ],
            "practice_tasks": ["Write 3 STAR stories using only verified experiences."],
        },
        "mock_interview_prep": {
            "title": "8. Adaptive Mock Interview Preparation",
            "topics_to_study": ["Timed Problem Solving", "Structured Explanations", *weak_topics[:3]],
            "questions": [
                {
                    "id": "mk-1",
                    "question": "Ready to run an interactive mock round targeting your weak areas and this role's core skills.",
                    "suggested_answer": "Start a mock session from the Mock Interview tab to practice one question at a time.",
                    "explanation": "Adaptive rounds adjust difficulty and feed weak topics back into Interview Memory.",
                    "example": "Select 'mixed' or 'technical' round.",
                    "difficulty": "medium",
                    "evidence_source": "interview_memory",
                    "grounding_type": "adaptive_practice",
                }
            ],
            "practice_tasks": ["Complete one 5-question adaptive mock session and review weak topics."],
        },
    }


class InterviewKitService:
    """Generates and persists 8-module Interview Kits grounded in approved resume SHA-256 and verified evidence."""

    def generate_kit(
        self,
        db: Session,
        *,
        user: User,
        application_id: uuid.UUID,
        module_key: str | None = None,
    ) -> InterviewKitResponse:
        row = db.execute(
            select(Application, Job, Resume)
            .join(Job, Job.id == Application.job_id)
            .join(Resume, Resume.id == Application.resume_id)
            .where(
                Application.id == application_id,
                Application.user_id == user.id,
                Application.deleted_at.is_(None),
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
        app, job, resume = row

        req = db.scalar(select(JobRequirement).where(JobRequirement.job_id == job.id))
        profile = db.scalar(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
        skills: list[str] = []
        projects: list[Project] = []
        if profile is not None:
            skills = [
                s.skill_name
                for s in db.scalars(
                    select(CandidateSkill).where(
                        CandidateSkill.profile_id == profile.id,
                        CandidateSkill.verified.is_(True),
                    )
                )
            ]
            projects = list(db.scalars(select(Project).where(Project.profile_id == profile.id)))

        weak_topics = [
            m.topic
            for m in db.scalars(
                select(InterviewMemory)
                .where(
                    InterviewMemory.user_id == user.id,
                    InterviewMemory.memory_type.in_(["weak_topic", "recurring_mistake"]),
                )
                .order_by(InterviewMemory.updated_at.desc())
                .limit(5)
            )
        ]

        fresh_modules = _build_modules(
            job=job,
            req=req,
            resume=resume,
            skills=skills,
            projects=projects,
            weak_topics=weak_topics,
        )

        active_kit = db.scalar(
            select(InterviewKit)
            .where(
                InterviewKit.user_id == user.id,
                InterviewKit.application_id == app.id,
                InterviewKit.is_active.is_(True),
            )
            .order_by(InterviewKit.version.desc())
        )

        if module_key:
            if module_key not in KIT_MODULE_KEYS:
                raise HTTPException(status_code=400, detail=f"Unknown module_key: {module_key}")
            if active_kit is not None:
                merged = dict(active_kit.modules_json or {})
                merged[module_key] = fresh_modules[module_key]
                active_kit.modules_json = merged
                db.commit()
                db.refresh(active_kit)
                return InterviewKitResponse.model_validate(active_kit)

        next_version = 1
        if active_kit is not None:
            next_version = active_kit.version + 1
            active_kit.is_active = False

        kit = InterviewKit(
            user_id=user.id,
            application_id=app.id,
            job_id=job.id,
            resume_id=resume.id,
            resume_checksum=resume.sha256_checksum,
            version=next_version,
            is_active=True,
            modules_json=fresh_modules,
            progress_json={"completed_topics": [], "revision_notes": {}},
            readiness_estimate=35.0,
        )
        db.add(kit)
        record_audit_event(
            db,
            user_id=user.id,
            actor=f"user:{user.id}",
            action="interview_kit.generated",
            resource_type="interview_kit",
            resource_id=str(kit.id),
            metadata={"application_id": str(app.id), "version": next_version, "resume_checksum": resume.sha256_checksum},
        )
        db.commit()
        db.refresh(kit)
        return InterviewKitResponse.model_validate(kit)

    def list_kits(self, db: Session, *, user: User, application_id: uuid.UUID | None = None) -> list[InterviewKitResponse]:
        query = select(InterviewKit).where(InterviewKit.user_id == user.id)
        if application_id is not None:
            query = query.where(InterviewKit.application_id == application_id)
        rows = list(db.scalars(query.order_by(InterviewKit.created_at.desc())))
        return [InterviewKitResponse.model_validate(r) for r in rows]

    def get_kit(self, db: Session, *, user: User, kit_id: uuid.UUID) -> InterviewKitResponse:
        kit = db.scalar(select(InterviewKit).where(InterviewKit.id == kit_id, InterviewKit.user_id == user.id))
        if kit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview kit not found")
        return InterviewKitResponse.model_validate(kit)

    def update_progress(
        self,
        db: Session,
        *,
        user: User,
        kit_id: uuid.UUID,
        payload: InterviewKitProgressRequest,
    ) -> InterviewKitResponse:
        kit = db.scalar(select(InterviewKit).where(InterviewKit.id == kit_id, InterviewKit.user_id == user.id))
        if kit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview kit not found")

        progress = dict(kit.progress_json or {})
        if payload.completed_topics is not None:
            progress["completed_topics"] = payload.completed_topics
        if payload.revision_notes is not None:
            notes = dict(progress.get("revision_notes") or {})
            notes.update(payload.revision_notes)
            progress["revision_notes"] = notes
        kit.progress_json = progress
        if payload.user_feedback is not None:
            kit.user_feedback = payload.user_feedback

        completed_count = len(progress.get("completed_topics") or [])
        kit.readiness_estimate = min(95.0, round(35.0 + completed_count * 7.5, 1))
        db.commit()
        db.refresh(kit)
        return InterviewKitResponse.model_validate(kit)

    def export_kit_markdown(self, db: Session, *, user: User, kit_id: uuid.UUID) -> str:
        kit = db.scalar(select(InterviewKit).where(InterviewKit.id == kit_id, InterviewKit.user_id == user.id))
        if kit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview kit not found")
        lines = [
            f"# ApplyPilot Interview Kit (v{kit.version})",
            f"- Application ID: `{kit.application_id}`",
            f"- Resume SHA-256: `{kit.resume_checksum}`",
            f"- Readiness Estimate: {kit.readiness_estimate}% (estimate only)",
            "",
        ]
        for key in KIT_MODULE_KEYS:
            mod = (kit.modules_json or {}).get(key) or {}
            lines.append(f"## {mod.get('title', key)}")
            for q in mod.get("questions") or []:
                lines.append(f"- **Q:** {q.get('question')}")
                lines.append(f"  - **Guidance:** {q.get('suggested_answer')}")
            lines.append("")
        return "\n".join(lines)
