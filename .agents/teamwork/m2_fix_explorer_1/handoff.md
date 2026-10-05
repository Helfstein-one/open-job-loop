# Handoff Report — Milestone M2 LLM Fix Formulation

## 1. Observation
- Target files inspected:
  - `src/llm/prompts.py` (128 lines)
  - `src/llm/evaluator.py` (204 lines)
  - `tests/test_adversarial_m2_llm.py` (548 lines)
  - `tests/test_llm.py` (356 lines)
- Baseline full test suite execution (`.venv/bin/pytest -v`) resulted in `182 passed, 2 xfailed in 19.30s`.
  The two xfailed tests in `tests/test_adversarial_m2_llm.py` were:
  1. `test_candidate_profile_with_prompt_injection` (un-sanitized XML tag leakage).
  2. `test_live_ollama_prompt_injection_resistance` (Ollama 3B susceptibility to prompt injection without contradiction clamp).
- In `strip_job_posting_tags`, the regex `rf"<{tag}>\s*(.*?)\s*</{tag}>"` was unanchored, causing truncation of surrounding text when inner tags appeared.
- In `wrap_job_posting`, only closing `</job_posting>` tags were sanitized; opening tags `<job_posting>` remained untouched.
- In `format_candidate_profile`, no XML delimiter sanitization was performed.
- In `enforce_threshold_consistency`, an evaluation with `fit_score=100`, `matched_skills=[]`, and `missing_skills=['C', 'Linux', 'Kernel']` was automatically promoted to `SHORTLIST`.

## 2. Logic Chain
1. Anchoring `strip_job_posting_tags` with `rf"^\s*<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>\s*$"` ensures that stripping only occurs when the entire input is enclosed in XML tags. Text containing inner tags is left intact.
2. Sanitizing both opening and closing tags in `wrap_job_posting` using `re.compile(rf"<\s*(/?)\s*{re.escape(tag)}\s*>", re.IGNORECASE)` substitutes opening `<job_posting>` with `&lt;job_posting&gt;` and closing `</job_posting>` with `&lt;/job_posting&gt;`, preventing nested delimiter spoofing.
3. Sanitizing XML delimiters in `format_candidate_profile` ensures candidate resume data cannot break out of candidate context or fake job boundary tags.
4. Adding contradiction detection to `enforce_threshold_consistency`:
   `if len(matched_skills) == 0 and len(missing_skills) > 0 and fit_score >= 70: fit_score = 0; recommendation = Recommendation.DISCARD`
   provides deterministic post-evaluation defense against model hallucinations and injection overrides.
5. Updating `tests/test_adversarial_m2_llm.py` and `tests/test_llm.py` removes `xfail` annotations, validates context preservation, and tests contradiction clamping.
6. Empirical verification of the combined proposed modules across 80 tests resulted in 80 passed, 0 failed, 0 xfailed.

## 3. Caveats
- Contradiction clamping specifically triggers when `len(matched_skills) == 0` and `len(missing_skills) > 0` and `fit_score >= 70`. If a model hallucinates matched skills (e.g. inventing skills not in the candidate profile), additional candidate-skill set-intersection verification would be required for full defense.
- Changes were verified against Python 3.12, local Ollama with `llama3.2:3b`, and ruff linter.

## 4. Conclusion
Complete code fixes have been formulated, verified, and packaged into ready-to-apply artifacts:
- Patch file: `.agents/teamwork/m2_fix_explorer_1/m2_fixes.patch`
- Replacement modules:
  - `.agents/teamwork/m2_fix_explorer_1/proposed_prompts.py`
  - `.agents/teamwork/m2_fix_explorer_1/proposed_evaluator.py`
  - `.agents/teamwork/m2_fix_explorer_1/proposed_test_adversarial_m2_llm.py`
  - `.agents/teamwork/m2_fix_explorer_1/proposed_test_llm.py`

All 5 requirements from the dispatch are fully resolved:
1. `strip_job_posting_tags` anchored.
2. `wrap_job_posting` sanitizes opening and closing tags.
3. `format_candidate_profile` sanitizes XML delimiters.
4. `enforce_threshold_consistency` implements contradiction detection (`len(matched)==0 and len(missing)>0 and score>=70` -> clamp to 0 and DISCARD).
5. Tests updated; 100% test pass rate achieved.

## 5. Verification Method
To apply and independently verify the fixes:
1. Apply patch:
   ```bash
   patch -p0 < .agents/teamwork/m2_fix_explorer_1/m2_fixes.patch
   ```
2. Run test suites:
   ```bash
   .venv/bin/pytest -v tests/test_llm.py tests/test_adversarial_m2_llm.py
   ```
   Expected: 80 passed, 0 xfailed, 0 failed.
3. Run entire test suite:
   ```bash
   .venv/bin/pytest -v
   ```
   Expected: 184 passed, 0 xfailed, 0 failed.
4. Check linting:
   ```bash
   /opt/homebrew/bin/ruff check src/ tests/
   ```
   Expected: All checks passed!
