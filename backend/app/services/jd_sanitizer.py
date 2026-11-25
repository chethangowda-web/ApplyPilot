import html
import re
from typing import Any
from bs4 import BeautifulSoup

# Patterns indicative of untrusted prompt-injection payloads inside JDs
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(system|developer)\s+(prompt|rules|instructions?)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an|in)\s+", re.IGNORECASE),
    re.compile(r"(auto[-\s]?submit|bypass\s+approval|set\s+score\s+to\s+100)", re.IGNORECASE),
    re.compile(r"(reveal|exfiltrate|print)\s+(api\s*key|secret|password|token)", re.IGNORECASE),
    re.compile(r"</?\s*system\s*>|BEGIN\s+SYSTEM\s+PROMPT", re.IGNORECASE),
)

_ANTI_BOT_WALL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"verify\s+you\s+are\s+human", re.IGNORECASE),
    re.compile(r"(recaptcha|hcaptcha|cf-turnstile|captcha)", re.IGNORECASE),
    re.compile(r"sign\s+in\s+to\s+continue|please\s+log\s+in\s+to\s+view", re.IGNORECASE),
)

_KNOWN_SKILLS: tuple[str, ...] = (
    "Python",
    "Java",
    "TypeScript",
    "JavaScript",
    "Go",
    "C++",
    "SQL",
    "FastAPI",
    "Django",
    "Flask",
    "Spring Boot",
    "Node.js",
    "Express.js",
    "React",
    "Next.js",
    "LangGraph",
    "LangChain",
    "RAG",
    "LLM",
    "LLMs",
    "Multi-Agent",
    "AI Agents",
    "Machine Learning",
    "Deep Learning",
    "PyTorch",
    "TensorFlow",
    "PostgreSQL",
    "pgvector",
    "MongoDB",
    "Redis",
    "Docker",
    "Kubernetes",
    "Git",
    "REST APIs",
    "REST API",
    "AWS",
    "GCP",
    "Azure",
    "pytest",
    "OOP",
    "MCP",
)

_EXP_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_EXP_MIN_RE = re.compile(
    r"(?:minimum|min\.?|at\s+least|requires?)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)|(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_SALARY_RE = re.compile(
    r"(?:₹|INR|USD|\$)\s*[\d,.]+(?:\s*(?:k|K|LPA|lpa|M|m))?(?:\s*(?:-|–|to)\s*(?:₹|INR|USD|\$)?\s*[\d,.]+(?:\s*(?:k|K|LPA|lpa|M|m))?)?"
)


def html_to_clean_text(raw_html: str) -> str:
    """Convert raw HTML fragments into normalized plain text while stripping scripts/styles."""
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    soup = BeautifulSoup(unescaped, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines()]
    return "\n".join(line for line in lines if line)


def detect_prompt_injection(text: str) -> list[str]:
    """Detect suspicious prompt-injection directives embedded inside untrusted external text."""
    if not text:
        return []
    matches: list[str] = []
    for pat in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            matches.append(m.group(0)[:80])
    return matches


def contains_auth_or_captcha_wall(text: str) -> bool:
    """Return True when page text indicates a login, CAPTCHA, or anti-bot gate."""
    if not text:
        return False
    return any(pat.search(text) for pat in _ANTI_BOT_WALL_PATTERNS)


def extract_deterministic_job_signals(
    *,
    title: str,
    description: str,
    location: str | None = None,
    workplace_type: str | None = None,
    commitment: str | None = None,
) -> dict[str, Any]:
    """Deterministically extract conservative structured signals without fabricating missing info."""
    combined = f"{title}\n{location or ''}\n{workplace_type or ''}\n{commitment or ''}\n{description}"
    lowered = combined.lower()
    desc_lower = description.lower()

    # Remote policy
    remote_policy = "unknown"
    is_remote: bool | None = None
    wp = (workplace_type or "").lower()
    loc_lower = (location or "").lower()
    if "remote" in wp or "remote" in loc_lower or "work from home" in lowered or "remote worldwide" in lowered:
        remote_policy = "remote"
        is_remote = True
    elif "hybrid" in wp or "hybrid" in loc_lower or "hybrid" in desc_lower:
        remote_policy = "hybrid"
        is_remote = False
    elif "on-site" in wp or "onsite" in wp or "on-site" in desc_lower or "onsite" in desc_lower:
        remote_policy = "onsite"
        is_remote = False

    # Employment type
    employment_type = "unknown"
    if "intern" in title.lower() or "internship" in (commitment or "").lower() or "internship" in desc_lower:
        employment_type = "internship"
    elif "contract" in (commitment or "").lower() or "freelance" in (commitment or "").lower():
        employment_type = "contract"
    elif "full-time" in (commitment or "").lower() or "full time" in lowered or "permanent" in lowered:
        employment_type = "full_time"

    # Experience bounds (explicit mentions only; never guess if missing)
    exp_min: float | None = None
    exp_max: float | None = None
    m_range = _EXP_RANGE_RE.search(description)
    if m_range:
        exp_min = float(m_range.group(1))
        exp_max = float(m_range.group(2))
    else:
        m_min = _EXP_MIN_RE.search(description)
        if m_min:
            val = m_min.group(1) or m_min.group(2)
            if val:
                exp_min = float(val)

    # Skills explicitly mentioned in JD text
    required_skills: list[str] = []
    preferred_skills: list[str] = []
    pref_marker = max(desc_lower.find("good to have"), desc_lower.find("preferred"), desc_lower.find("nice to have"))
    for skill in _KNOWN_SKILLS:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(skill)}(?![A-Za-z0-9])", re.IGNORECASE)
        match = pattern.search(description)
        if not match:
            continue
        canonical = "REST APIs" if skill == "REST API" else ("LLMs" if skill == "LLM" else skill)
        if pref_marker != -1 and match.start() > pref_marker:
            if canonical not in preferred_skills and canonical not in required_skills:
                preferred_skills.append(canonical)
        else:
            if canonical not in required_skills:
                required_skills.append(canonical)

    # Education mentions
    education_requirements: list[str] = []
    for edu_pat, label in (
        (r"\bb\.?\s*tech\b|\bb\.?\s*e\.?\b|\bbachelor", "B.E./B.Tech/Bachelor's"),
        (r"\bm\.?\s*tech\b|\bm\.?\s*s\.?\b|\bmaster", "M.Tech/M.S./Master's"),
        (r"\bmca\b", "MCA"),
        (r"\bcomputer\s+science\b|\bcs\b|\binformation\s+technology\b|\bai\b", "CS/IT/AI"),
    ):
        if re.search(edu_pat, desc_lower) and label not in education_requirements:
            education_requirements.append(label)

    # Salary text if explicitly present
    salary_match = _SALARY_RE.search(description)
    salary_text = salary_match.group(0).strip() if salary_match else None

    # Injection detection (data-only flagging)
    injections = detect_prompt_injection(description)

    return {
        "remote_policy": remote_policy,
        "is_remote": is_remote,
        "employment_type": employment_type,
        "experience_min": exp_min,
        "experience_max": exp_max,
        "required_skills": required_skills,
        "preferred_skills": preferred_skills,
        "education_requirements": education_requirements,
        "salary_text": salary_text,
        "prompt_injection_flags": injections,
    }
