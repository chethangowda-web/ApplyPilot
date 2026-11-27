from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    InterviewAttempt,
    InterviewKit,
    InterviewMemory,
    InterviewSession,
    User,
)
from app.schemas.career import (
    InterviewAttemptResponse,
    InterviewSessionResponse,
    MockSessionStartRequest,
)
from app.services.audit_service import record_audit_event

ROUND_TO_MODULES: dict[str, tuple[str, ...]] = {
    "technical": ("cs_fundamentals", "role_specific_technical", "system_design"),
    "dsa": ("dsa",),
    "backend_swe": ("cs_fundamentals", "role_specific_technical", "system_design"),
    "ai_ml": ("role_specific_technical", "resume_project_deep_dive", "system_design"),
    "resume_deep_dive": ("resume_project_deep_dive",),
    "behavioral": ("behavioral_star",),
    "mixed": ("role_specific_technical", "dsa", "resume_project_deep_dive", "system_design", "behavioral_star"),
}

MAX_QUESTIONS_PER_SESSION = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _pick_question(kit: InterviewKit, round_type: str, difficulty: str, index: int, weak_topics: list[str]) -> dict[str, Any]:
    modules = ROUND_TO_MODULES.get(round_type, ROUND_TO_MODULES["mixed"])
    mod_key = modules[index % len(modules)]
    mod = (kit.modules_json or {}).get(mod_key) or {}
    questions = mod.get("questions") or []
    base_q = questions[index % len(questions)] if questions else {}
    topic = (mod.get("topics_to_study") or [mod_key])[0]
    if weak_topics and index == 1:
        topic = weak_topics[0]

    prompt_text = str(
        base_q.get("question")
        or f"[{difficulty.upper()}] Explain your approach to {topic} in the context of this role."
    )
    if index > 0 and base_q:
        prompt_text = f"[{difficulty.upper()} · Follow-up #{index + 1}] {prompt_text}"

    return {
        "question_index": index,
        "module_key": mod_key,
        "topic": str(topic),
        "difficulty": difficulty,
        "question_text": prompt_text,
        "expected_structure": [
            "Core concept & direct answer",
            "Concrete trade-offs or complexity",
            "Real example or edge-case handling",
        ],
        "hints": [
            f"Focus on {topic} fundamentals and concrete trade-offs.",
            "Keep claims grounded in verified experience.",
        ],
    }


def _evaluate_answer(question: dict[str, Any], answer: str) -> tuple[float, dict[str, Any], list[str], list[str]]:
    text = answer.strip()
    words = text.split()
    lowered = text.lower()

    clarity = min(25.0, round(len(words) / 2.5, 1))
    reasoning_markers = ("because", "trade-off", "tradeoff", "however", "therefore", "complexity", "latency", "edge case")
    reasoning_hits = sum(1 for m in reasoning_markers if m in lowered)
    reasoning = min(25.0, round(8.0 + reasoning_hits * 5.5, 1))

    depth_markers = ("index", "transaction", "async", "cache", "o(", "sha", "idempoten", "retry", "situation", "result", "test")
    depth_hits = sum(1 for m in depth_markers if m in lowered)
    depth = min(25.0, round(6.0 + depth_hits * 5.0, 1))

    accuracy = 20.0 if len(words) >= 20 else round(max(5.0, len(words) * 0.8), 1)
    total = round(min(100.0, clarity + reasoning + depth + accuracy), 1)

    topic = str(question.get("topic") or question.get("module_key") or "general")
    weak_topics: list[str] = []
    strong_topics: list[str] = []
    if total < 65.0:
        weak_topics.append(topic)
    else:
        strong_topics.append(topic)

    feedback = {
        "rubric": {
            "accuracy": accuracy,
            "depth": depth,
            "clarity": clarity,
            "reasoning": reasoning,
        },
        "disclaimer": "Transparent heuristic/AI rubric estimate; does not guarantee live interviewer scoring.",
        "strengths": ["Clear structure" if clarity >= 15 else "Concise response"],
        "improvements": (
            ["Add concrete trade-offs, complexity, or edge-case handling."]
            if total < 75.0
            else ["Strong coverage; tighten conciseness for timed rounds."]
        ),
    }
    return total, feedback, weak_topics, strong_topics


class MockInterviewService:
    """Interactive adaptive mock interview engine backed by InterviewKit and InterviewMemory."""

    def _to_response(self, db: Session, session: InterviewSession) -> InterviewSessionResponse:
        attempts = list(
            db.scalars(
                select(InterviewAttempt)
                .where(InterviewAttempt.session_id == session.id)
                .order_by(InterviewAttempt.question_index.asc())
            )
        )
        resp = InterviewSessionResponse.model_validate(session)
        resp.attempts = [InterviewAttemptResponse.model_validate(a) for a in attempts]
        return resp

    def start_session(
        self,
        db: Session,
        *,
        user: User,
        payload: MockSessionStartRequest,
    ) -> InterviewSessionResponse:
        kit = db.scalar(
            select(InterviewKit).where(
                InterviewKit.id == payload.kit_id,
                InterviewKit.user_id == user.id,
            )
        )
        if kit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview kit not found")

        weak_topics = [
            m.topic
            for m in db.scalars(
                select(InterviewMemory)
                .where(InterviewMemory.user_id == user.id, InterviewMemory.memory_type == "weak_topic")
                .order_by(InterviewMemory.updated_at.desc())
                .limit(3)
            )
        ]
        first_q = _pick_question(kit, payload.round_type, payload.difficulty, 0, weak_topics)
        session = InterviewSession(
            user_id=user.id,
            kit_id=kit.id,
            round_type=payload.round_type,
            difficulty=payload.difficulty,
            status="active",
            current_question_index=0,
            current_question_json=first_q,
        )
        db.add(session)
        record_audit_event(
            db,
            user_id=user.id,
            actor=f"user:{user.id}",
            action="mock_interview.started",
            resource_type="interview_session",
            resource_id=str(session.id),
            metadata={"kit_id": str(kit.id), "round_type": payload.round_type},
        )
        db.commit()
        db.refresh(session)
        return self._to_response(db, session)

    def get_session(self, db: Session, *, user: User, session_id: uuid.UUID) -> InterviewSessionResponse:
        session = db.scalar(
            select(InterviewSession).where(
                InterviewSession.id == session_id,
                InterviewSession.user_id == user.id,
            )
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mock session not found")
        return self._to_response(db, session)

    def submit_answer(
        self,
        db: Session,
        *,
        user: User,
        session_id: uuid.UUID,
        answer: str,
    ) -> InterviewSessionResponse:
        session = db.scalar(
            select(InterviewSession).where(
                InterviewSession.id == session_id,
                InterviewSession.user_id == user.id,
            )
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mock session not found")
        if session.status != "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mock session is already completed")

        kit = db.scalar(select(InterviewKit).where(InterviewKit.id == session.kit_id, InterviewKit.user_id == user.id))
        if kit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview kit not found")

        q = dict(session.current_question_json or {})
        score, feedback, weak_topics, strong_topics = _evaluate_answer(q, answer)

        attempt = InterviewAttempt(
            session_id=session.id,
            kit_id=kit.id,
            user_id=user.id,
            question_index=session.current_question_index,
            module_key=str(q.get("module_key") or "mixed"),
            difficulty=session.difficulty,
            question_text=str(q.get("question_text") or ""),
            expected_structure=list(q.get("expected_structure") or []),
            hints=list(q.get("hints") or []),
            candidate_answer=answer.strip(),
            feedback_json=feedback,
            score=score,
            weak_topics=weak_topics,
            strong_topics=strong_topics,
        )
        db.add(attempt)

        # Persist adaptive memory updates (owner-scoped)
        for wt in weak_topics:
            existing_mem = db.scalar(
                select(InterviewMemory).where(
                    InterviewMemory.user_id == user.id,
                    InterviewMemory.memory_type == "weak_topic",
                    InterviewMemory.topic == wt,
                )
            )
            if existing_mem is None:
                db.add(
                    InterviewMemory(
                        user_id=user.id,
                        memory_type="weak_topic",
                        topic=wt,
                        summary=f"Needs deeper trade-off/complexity coverage on {wt} (scored {score}/100).",
                        source=f"mock_session:{session.id}",
                        confidence=0.75,
                        evidence_refs=[f"session:{session.id}"],
                        attempt_count=1,
                        last_score=score,
                    )
                )
            else:
                existing_mem.attempt_count += 1
                existing_mem.last_score = score
                existing_mem.summary = f"Needs deeper coverage on {wt} (latest score {score}/100)."

        for st in strong_topics:
            existing_strong = db.scalar(
                select(InterviewMemory).where(
                    InterviewMemory.user_id == user.id,
                    InterviewMemory.memory_type == "strong_area",
                    InterviewMemory.topic == st,
                )
            )
            if existing_strong is None:
                db.add(
                    InterviewMemory(
                        user_id=user.id,
                        memory_type="strong_area",
                        topic=st,
                        summary=f"Demonstrated solid reasoning on {st} (scored {score}/100).",
                        source=f"mock_session:{session.id}",
                        confidence=0.8,
                        evidence_refs=[f"session:{session.id}"],
                        attempt_count=1,
                        last_score=score,
                    )
                )
            else:
                existing_strong.attempt_count += 1
                existing_strong.last_score = score

        # Adapt difficulty
        next_diff = session.difficulty
        if score >= 80.0 and session.difficulty != "hard":
            next_diff = "hard" if session.difficulty == "medium" else "medium"
        elif score < 55.0 and session.difficulty != "easy":
            next_diff = "easy" if session.difficulty == "medium" else "medium"
        session.difficulty = next_diff

        next_idx = session.current_question_index + 1
        if next_idx >= MAX_QUESTIONS_PER_SESSION:
            self._finalize_session(db, session=session, kit=kit)
        else:
            session.current_question_index = next_idx
            session.current_question_json = _pick_question(
                kit, session.round_type, next_diff, next_idx, weak_topics
            )

        db.commit()
        db.refresh(session)
        return self._to_response(db, session)

    def complete_session(self, db: Session, *, user: User, session_id: uuid.UUID) -> InterviewSessionResponse:
        session = db.scalar(
            select(InterviewSession).where(
                InterviewSession.id == session_id,
                InterviewSession.user_id == user.id,
            )
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mock session not found")
        kit = db.scalar(select(InterviewKit).where(InterviewKit.id == session.kit_id, InterviewKit.user_id == user.id))
        if session.status == "active" and kit is not None:
            self._finalize_session(db, session=session, kit=kit)
            db.commit()
            db.refresh(session)
        return self._to_response(db, session)

    def _finalize_session(self, db: Session, *, session: InterviewSession, kit: InterviewKit) -> None:
        db.flush()
        attempts = list(
            db.scalars(select(InterviewAttempt).where(InterviewAttempt.session_id == session.id))
        )
        avg = round(sum(a.score for a in attempts) / len(attempts), 1) if attempts else 0.0
        weak = sorted({t for a in attempts for t in (a.weak_topics or [])})
        strong = sorted({t for a in attempts for t in (a.strong_topics or [])})
        session.status = "completed"
        session.completed_at = _utc_now()
        session.overall_score = avg
        session.report_json = {
            "overall_score": avg,
            "questions_answered": len(attempts),
            "strengths": strong,
            "weaknesses": weak,
            "study_recommendations": [f"Review {w} and practice structured trade-offs." for w in weak]
            or ["Continue timed mixed-round practice."],
            "disclaimer": "Estimated practice score based on transparent rubric.",
        }
        kit.readiness_estimate = min(98.0, round(max(kit.readiness_estimate, avg), 1))
