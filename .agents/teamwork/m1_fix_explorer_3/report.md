# M1 Fix Investigation Report: MatchEvaluation Skills Nullability & Coercion

## 1. Executive Summary

- **Task**: Formulate exact code fixes for `src/models/schemas.py` so that `MatchEvaluation` accepts `None` for `matched_skills` and `missing_skills` and coerces them to `[]` via a Pydantic validator, and verify all existing tests across `tests/` pass or identify required test adjustments.
- **Root Cause Identified**: Local open-weight LLMs (e.g., Llama 3.2 via Ollama / Instructor) frequently return JSON `null` for `matched_skills` or `missing_skills` when candidate qualification matches are absent or incomplete. Currently, `MatchEvaluation` specifies `matched_skills: List[str]` without `Optional` or pre-validation coercion, raising `pydantic_core.ValidationError: Input should be a valid list [type=list_type, input_value=None]`.
- **Resolution**: 
  1. Update type annotations of `matched_skills` and `missing_skills` to `Optional[List[str]] = Field(default_factory=list, ...)`.
  2. Implement `@field_validator("matched_skills", "missing_skills", mode="before")` on `MatchEvaluation` returning `[]` whenever `v is None`.
  3. Adjust `test_match_evaluation_null_skills_validation_error` in `tests/test_adversarial_m1.py` (which previously asserted that `None` caused a `ValidationError`) to assert successful coercion to `[]`.
  4. Add `test_match_evaluation_null_skills_coercion` to `tests/test_models.py`.
- **Test Impact**: All 77 existing tests pass cleanly when the adversarial test expectation is aligned with the new resilient behavior (29/29 adversarial, 12/12 db, 7/7 models [8/8 with new test], 7/7 stress, 21/21 truncator).

---

## 2. Problem Analysis

### Current Schema Definition
In `src/models/schemas.py` lines 57-64:
```python
    matched_skills: List[str] = Field(
        default_factory=list,
        description="List of required skills possessed by the candidate."
    )
    missing_skills: List[str] = Field(
        default_factory=list,
        description="List of required skills absent from candidate profile."
    )
```

### Fragility Mechanism
When Instructor requests structured JSON output from a local LLM, or when external payloads contain null fields:
```json
{
  "fit_score": 80,
  "recommendation": "SHORTLIST",
  "matched_skills": null,
  "missing_skills": null
}
```
Pydantic v2 attempts type validation before applying default factories. Because the field type is strictly `List[str]`, Pydantic immediately throws:
```
pydantic_core._pydantic_core.ValidationError: 2 validation errors for MatchEvaluation
matched_skills
  Input should be a valid list [type=list_type, input_value=None, input_type=NoneType]
missing_skills
  Input should be a valid list [type=list_type, input_value=None, input_type=NoneType]
```

Furthermore, in `src/db/repository.py:381`, `_row_to_job` deserializes JSON strings from DuckDB:
```python
eval_raw = data.get("evaluation")
if eval_raw:
    if isinstance(eval_raw, str):
        try:
            eval_data = json.loads(eval_raw)
            data["evaluation"] = MatchEvaluation(**eval_data)
        except Exception:
            data["evaluation"] = None
```
Any job record with `null` skills causes the evaluation to silently collapse to `None`.

---

## 3. Formulated Code Changes

### Target File: `src/models/schemas.py`

#### A. Imports
Update imports on lines 10-11 to include `Any` and `field_validator`:
```python
# Before
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict, model_validator

# After
from typing import Optional, List, Any
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator
```

#### B. Field Definitions & Coercion Validator
Update lines 57-64 in `MatchEvaluation` and insert validator:
```python
# Before
    matched_skills: List[str] = Field(
        default_factory=list,
        description="List of required skills possessed by the candidate."
    )
    missing_skills: List[str] = Field(
        default_factory=list,
        description="List of required skills absent from candidate profile."
    )
    reasoning: str = Field(
        default="",
        description="Concise rationale explaining the evaluation score."
    )
    seniority_fit: Optional[str] = Field(
        default=None,
        description="Assessment of seniority level match (e.g., 'Junior', 'Mid-level', 'Senior', 'Staff')."
    )

# After
    matched_skills: Optional[List[str]] = Field(
        default_factory=list,
        description="List of required skills possessed by the candidate."
    )
    missing_skills: Optional[List[str]] = Field(
        default_factory=list,
        description="List of required skills absent from candidate profile."
    )
    reasoning: str = Field(
        default="",
        description="Concise rationale explaining the evaluation score."
    )
    seniority_fit: Optional[str] = Field(
        default=None,
        description="Assessment of seniority level match (e.g., 'Junior', 'Mid-level', 'Senior', 'Staff')."
    )

    @field_validator("matched_skills", "missing_skills", mode="before")
    @classmethod
    def coerce_none_skills(cls, v: Any) -> Any:
        """
        Coerces None or null LLM outputs for skill lists into empty lists.
        """
        if v is None:
            return []
        return v
```

### Why `mode="before"`?
1. **Pre-type coercion**: In Pydantic v2, `mode="before"` intercepts raw input prior to schema type enforcement. When raw input is `None`, it becomes `[]`.
2. **Schema tolerance**: Preserves Pydantic's strict checks for truly invalid non-null types (e.g., integers `123` or strings `"invalid"` will still trigger `ValidationError`).
3. **Guaranteed return type**: `instance.matched_skills` is always an initialized Python `list`, never `None`.

---

## 4. Test Suite Impact & Required Test Adjustments

### Impact Analysis Across `tests/`
An exhaustive review of all test files was conducted:

| Test File | Test Cases | Impact | Status |
|---|---|---|---|
| `tests/test_adversarial_m1.py` | 29 | 1 test affected (`test_match_evaluation_null_skills_validation_error`) | Needs adjustment |
| `tests/test_models.py` | 7 | 0 failures; benefits from added regression test | PASS |
| `tests/test_db.py` | 12 | 0 failures | PASS |
| `tests/test_stress_persistence.py` | 7 | 0 failures | PASS |
| `tests/test_truncator.py` | 21 | 0 failures | PASS |

### Adjustment in `tests/test_adversarial_m1.py`
In `tests/test_adversarial_m1.py:252-264`, the test was explicitly created as an "EMPIRICAL FRAGILITY DEMONSTRATION" asserting that `None` caused a crash:
```python
# Existing (Demonstrating Fragility):
    def test_match_evaluation_null_skills_validation_error(self):
        """
        EMPIRICAL FRAGILITY DEMONSTRATION:
        When an LLM returns null for matched_skills or missing_skills,
        MatchEvaluation raises ValidationError instead of defaulting to [].
        """
        with pytest.raises(ValidationError):
            MatchEvaluation.model_validate({
                "fit_score": 80,
                "recommendation": "SHORTLIST",
                "matched_skills": None,
                "missing_skills": None,
            })
```

Once the fix is applied, `MatchEvaluation` accepts `None` and coerces to `[]`. This test fails because `ValidationError` is no longer raised (`Failed: DID NOT RAISE ValidationError`).

**Required Adjustment**:
```python
# Updated (Verifying Resilience):
    def test_match_evaluation_null_skills_coerced_to_empty_list(self):
        """
        VERIFIED RESILIENCE:
        When an LLM returns null for matched_skills or missing_skills,
        MatchEvaluation coerces them to empty lists [] instead of raising ValidationError.
        """
        eval_obj = MatchEvaluation.model_validate({
            "fit_score": 80,
            "recommendation": "SHORTLIST",
            "matched_skills": None,
            "missing_skills": None,
        })
        assert eval_obj.matched_skills == []
        assert eval_obj.missing_skills == []
```

### Proposed Addition to `tests/test_models.py`
Add explicit positive verification of null coercion in unit test suite:
```python
def test_match_evaluation_null_skills_coercion():
    # Direct instantiation with None
    eval_direct = MatchEvaluation(
        fit_score=90,
        recommendation=Recommendation.SHORTLIST,
        matched_skills=None,
        missing_skills=None,
    )
    assert eval_direct.matched_skills == []
    assert eval_direct.missing_skills == []

    # Dict validation with None
    eval_dict = MatchEvaluation.model_validate({
        "fit_score": 75,
        "recommendation": "DISCARD",
        "matched_skills": None,
        "missing_skills": None,
    })
    assert eval_dict.matched_skills == []
    assert eval_dict.missing_skills == []

    # JSON string validation with null
    eval_json = MatchEvaluation.model_validate_json(
        '{"fit_score": 80, "recommendation": "SHORTLIST", "matched_skills": null, "missing_skills": null}'
    )
    assert eval_json.matched_skills == []
    assert eval_json.missing_skills == []
```

---

## 5. Artifact Reference

All patch files and full proposed code are located in `.agents/teamwork/m1_fix_explorer_3/`:
- `schemas_fix.patch`: Unified diff for `src/models/schemas.py`.
- `tests_fix.patch`: Unified diff for `tests/test_adversarial_m1.py` and `tests/test_models.py`.
- `proposed_schemas.py`: Full source replacement file for reference.
