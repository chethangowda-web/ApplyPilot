import pytest
from tests.eval_benchmark import run_benchmark


@pytest.mark.asyncio
async def test_60_jd_evaluation_benchmark():
    metrics = await run_benchmark()
    assert metrics["dataset_size"] == 60
    assert metrics["required_skills_metrics"]["f1"] >= 0.90
    assert metrics["preferred_skills_metrics"]["f1"] >= 0.90
    assert metrics["experience_extraction_accuracy"] >= 0.95
    assert metrics["education_extraction_accuracy"] >= 0.95
    assert metrics["evidence_grounding_rate"] >= 0.99
    assert metrics["eligibility_accuracy"] >= 0.95
    assert metrics["scoring_consistency_rate"] == 1.0
    assert metrics["prompt_injection_detection"]["f1"] == 1.0
