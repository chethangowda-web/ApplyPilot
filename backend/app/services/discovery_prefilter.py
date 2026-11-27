from dataclasses import dataclass, field
import re
from typing import Any

TARGET_ROLE_PATTERNS = (
    r"\bsoftware\s+(?:engineer|developer|development\s+engineer)\b",
    r"\bsde\b",
    r"\bswe\b",
    r"\bbackend\s+(?:engineer|developer)\b",
    r"\bai\s+engineer\b",
    r"\bapplied\s+ai\b",
    r"\bmachine\s+learning\s+engineer\b",
    r"\bml\s+engineer\b",
    r"\b(?:nlp|llm)\s+engineer\b",
    r"\bdata\s+engineer\b",
    r"\b(?:software|ai|ml|backend|engineering)\s+intern(?:ship)?\b",
    r"\b(?:graduate|junior|associate|entry[\s-]level|fresher)\s+(?:software|backend|ai|ml|data)?\s*(?:engineer|developer)\b",
    r"\b(?:full[\s-]?stack|platform|systems|research)\s+engineer\b",
)

EXCLUDED_ROLE_PATTERNS = (
    r"\b(?:account\s+executive|sales|business\s+development|bdr|sdr)\b",
    r"\b(?:recruiter|recruiting|talent\s+acquisition|people\s+partner|human\s+resources|\bhr\b)\b",
    r"\b(?:legal|counsel|attorney|paralegal|compliance|policy)\b",
    r"\b(?:accounting|accountant|finance|financial|payroll|tax|audit)\b",
    r"\b(?:marketing|brand|communications|public\s+relations|\bpr\b|copywriter)\b",
    r"\b(?:investigator|fraud\s+analyst|trust\s*&\s*safety|customer\s+support|solutions\s+architect)\b",
    r"\b(?:product\s+manager|program\s+manager|project\s+manager|product\s+designer|ux\s+designer)\b",
    r"\b(?:fellowship|demand\s+planning|office\s+manager|executive\s+assistant)\b",
)

SENIOR_TITLE_PATTERNS = (
    r"\b(?:senior|sr\.?|staff|principal|distinguished|fellow|director|vp|vice\s+president|head\s+of|chief)\b",
    r"\b(?:engineering\s+manager|tech\s+lead|team\s+lead)\b",
)

INDIA_LOCATION_TOKENS = (
    "india",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "pune",
    "chennai",
    "mumbai",
    "delhi",
    "ncr",
    "gurgaon",
    "gurugram",
    "noida",
    "kolkata",
    "ahmedabad",
    "kochi",
    "chandigarh",
)

COUNTRY_RESTRICTED_REMOTE_PATTERNS = (
    r"\b(?:us|u\.s\.|usa|united\s+states|north\s+america|uk|united\s+kingdom|europe|eu|emea|canada|australia|apac\s+ex-india)\s*(?:only|remote)\b",
    r"\bremote\s*[-–—(]\s*(?:us|u\.s\.|usa|united\s+states|uk|eu|emea|canada|ny|ca|sf)\b",
)

_TARGET_ROLE_RE = re.compile("|".join(TARGET_ROLE_PATTERNS), re.IGNORECASE)
_EXCLUDED_ROLE_RE = re.compile("|".join(EXCLUDED_ROLE_PATTERNS), re.IGNORECASE)
_SENIOR_TITLE_RE = re.compile("|".join(SENIOR_TITLE_PATTERNS), re.IGNORECASE)
_COUNTRY_RESTRICTED_RE = re.compile("|".join(COUNTRY_RESTRICTED_REMOTE_PATTERNS), re.IGNORECASE)
_YEARS_RE = re.compile(r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:-\s*\d{1,2}\s*)?years?\s+(?:of\s+)?(?:experience|relevant|software|industry)\b", re.IGNORECASE)


@dataclass
class ConnectorScanMetrics:
    raw_fetched: int = 0
    pages_scanned: int = 0
    removed_by_role: int = 0
    removed_by_location: int = 0
    removed_by_experience: int = 0
    shortlisted: int = 0
    next_cursor: str | None = None
    cache_hit: bool = False
    duration_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_fetched": self.raw_fetched,
            "pages_scanned": self.pages_scanned,
            "removed_by_role": self.removed_by_role,
            "removed_by_location": self.removed_by_location,
            "removed_by_experience": self.removed_by_experience,
            "shortlisted": self.shortlisted,
            "next_cursor": self.next_cursor,
            "cache_hit": self.cache_hit,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass(frozen=True)
class PrefilterDecision:
    keep: bool
    reason: str  # kept | role_excluded | role_non_target | location_outside_target | experience_senior
    location_uncertain: bool = False


def _location_matches_india_or_global_remote(location: str | None, *, is_remote_flag: bool = False) -> tuple[bool, bool]:
    """Return (matches_target_geography, location_uncertain)."""
    loc = (location or "").strip()
    if not loc:
        return True, not is_remote_flag

    segments = [seg.strip() for seg in re.split(r"[|;/]", loc) if seg.strip()]
    for seg in segments:
        low = seg.lower()
        if any(tok in low for tok in INDIA_LOCATION_TOKENS):
            return True, False
        if "remote" in low and not _COUNTRY_RESTRICTED_RE.search(low):
            if any(g in low for g in ("worldwide", "global", "anywhere", "international", "distributed")) or low in {
                "remote",
                "fully remote",
                "remote friendly",
            }:
                return True, False
    if is_remote_flag and not _COUNTRY_RESTRICTED_RE.search(loc.lower()):
        # Remote flag without India or explicit global remote: keep only if no non-India US/EU city/country lock
        non_india_markers = ("san francisco", "new york", "seattle", "london", "paris", "berlin", "austin", "boston", "united states", "usa", "uk", "canada")
        if not any(m in loc.lower() for m in non_india_markers):
            return True, False
    return False, False


def evaluate_job_prefilter(
    *,
    title: str,
    location: str | None,
    description: str = "",
    is_remote_flag: bool = False,
    experience_min: float | None = None,
    query: str = "",
    requested_location: str | None = None,
    smart_filter: bool = True,
) -> PrefilterDecision:
    """Fast deterministic pre-filter before expensive JD analysis & embedding generation."""
    title_clean = (title or "").strip()
    if not smart_filter:
        return PrefilterDecision(keep=True, reason="kept")

    if _EXCLUDED_ROLE_RE.search(title_clean) and not _TARGET_ROLE_RE.search(title_clean):
        return PrefilterDecision(keep=False, reason="role_excluded")

    if not _TARGET_ROLE_RE.search(title_clean):
        return PrefilterDecision(keep=False, reason="role_non_target")

    if query:
        q_tokens = [t for t in query.lower().split() if len(t) >= 2]
        hay = f"{title_clean} {description[:1200]}".lower()
        if q_tokens and not any(t in hay for t in q_tokens):
            return PrefilterDecision(keep=False, reason="role_non_target")

    # Experience pre-filter (reject obvious senior/staff/5+ yr roles, keep intern/entry/0-2yr/unknown)
    if _SENIOR_TITLE_RE.search(title_clean) and "intern" not in title_clean.lower():
        return PrefilterDecision(keep=False, reason="experience_senior")
    if experience_min is not None and experience_min > 3.0:
        return PrefilterDecision(keep=False, reason="experience_senior")
    for m in _YEARS_RE.finditer(description[:2500]):
        yrs = int(m.group(1))
        if yrs >= 4:
            return PrefilterDecision(keep=False, reason="experience_senior")

    # Location pre-filter (India + Global Remote; never reject solely when metadata is empty)
    if requested_location:
        req_low = requested_location.lower()
        loc_low = (location or "").lower()
        if location and req_low not in loc_low and not (is_remote_flag and "remote" in req_low):
            return PrefilterDecision(keep=False, reason="location_outside_target")

    geo_ok, loc_uncertain = _location_matches_india_or_global_remote(location, is_remote_flag=is_remote_flag)
    if not geo_ok:
        return PrefilterDecision(keep=False, reason="location_outside_target")

    return PrefilterDecision(keep=True, reason="kept", location_uncertain=loc_uncertain)
