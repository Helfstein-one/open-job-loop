from __future__ import annotations
"""
Job description pre-processor, boilerplate stripper, and context ceiling truncator.
"""


from dataclasses import dataclass
import html
import math
import re
from typing import Optional


class DescriptionTooShortError(ValueError):
    """Raised when a job description contains 50 or fewer characters of meaningful content."""
    pass


@dataclass(frozen=True)
class TruncationResult:
    """Metadata container for text truncation operations."""
    raw_text: str
    cleaned_text: str
    final_text: str
    original_tokens: int
    final_tokens: int
    was_truncated: bool


class TextTruncator:
    """
    Pre-processes job postings for local LLM evaluation by removing legal/EEO
    boilerplate, validating length, bounding tokens to a ceiling, and wrapping
    in XML delimiters to mitigate prompt injection.
    """

    # Comprehensive regular expressions matching boilerplate sections
    EEO_AND_BOILERPLATE_PATTERNS = [
        # EEO / Affirmative Action Employer statements
        r"(?i)\b(?:we are (?:an? )?equal opportunity employer|equal (?:employment )?opportunity|eeo/aa|affirmative action employer).*?(?:\n|\Z)",
        # Standard non-discrimination legal clauses
        r"(?i)\b(?:all qualified applicants will receive consideration for employment without regard to|qualified applicants will be considered without regard to).*?(?:\n|\Z)",
        r"(?i)\b(?:we (?:do not discriminate|prohibit discrimination) (?:on the basis of|based on)).*?(?:\n|\Z)",
        r"(?i)\b(?:we celebrate diversity and are committed to creating an inclusive).*?(?:\n|\Z)",
        # Municipal / State Fair Chance Ordinances (SF, CA, LA, NYC)
        r"(?i)\b(?:pursuant to the (?:san francisco|california|los angeles|new york) fair chance|fair chance ordinance|fair chance initiative).*?(?:\n|\Z)",
        # Disability & ADA accommodation boilerplate
        r"(?i)\b(?:if you (?:require|need) (?:an? )?reasonable accommodation|accommodations? for (?:individuals|persons) with disabilities|americans with disabilities act).*?(?:\n|\Z)",
        # Third-party agency / recruiter disclaimers
        r"(?i)\b(?:notice to (?:recruitment |staffing )?(?:agencies|recruiters)|no unsolicited (?:agency )?resumes|unsolicited resumes from (?:third-party |search )?agencies).*?(?:\n|\Z)",
        # Pay transparency regulatory clauses
        r"(?i)\b(?:pay transparency nondiscrimination provision).*?(?:\n|\Z)",
        # Background check & drug screen compliance clauses
        r"(?i)\b(?:pre-employment (?:background check|drug (?:screen|test))|contingent upon successful completion of a background check).*?(?:\n|\Z)",
    ]

    TRUNCATION_MARKER = "\n\n[...Description truncated for context window ceiling...]"

    def __init__(
        self,
        max_tokens: int = 1500,
        min_chars: int = 10,
        chars_per_token: float = 4.0,
        wrap_xml: bool = True,
        tag: str = "job_posting",
    ) -> None:
        """
        Initialize the TextTruncator.

        :param max_tokens: Maximum allowed token budget (default 1,500).
        :param min_chars: Minimum required character length for meaningful content (>50).
        :param chars_per_token: Heuristic ratio of characters per token (default 4.0).
        :param wrap_xml: Whether to wrap output in XML delimiters by default.
        :param tag: XML delimiter tag name (default 'job_posting').
        """
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        if min_chars < 0:
            raise ValueError(f"min_chars cannot be negative, got {min_chars}")
        if chars_per_token <= 0:
            raise ValueError(f"chars_per_token must be positive, got {chars_per_token}")

        self.max_tokens = max_tokens
        self.min_chars = min_chars
        self.chars_per_token = chars_per_token
        self.default_wrap_xml = wrap_xml
        self.tag = tag

        # Precompile regexes for optimal high-throughput performance
        self._compiled_boilerplate = [
            re.compile(pattern) for pattern in self.EEO_AND_BOILERPLATE_PATTERNS
        ]
        self._html_breaks = re.compile(r"(?i)<(?:br|p|div|li|h[1-6])[^>]*>")
        self._html_tags = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^>]*)?>")
        self._horizontal_spaces = re.compile(r"[ \t]+")
        self._vertical_spaces = re.compile(r"\n{3,}")
        self._closing_tag_pattern = re.compile(
            rf"<\s*/\s*{re.escape(self.tag)}\s*>",
            re.IGNORECASE,
        )

    def estimate_tokens(self, text: str) -> int:
        """
        Estimate the number of tokens using heuristic character estimation.
        Formula: ceil(len(text) / chars_per_token) for non-empty text, 0 for empty.
        """
        if not text:
            return 0
        return max(1, math.ceil(len(text) / self.chars_per_token))

    def clean_boilerplate(self, text: str) -> str:
        """
        Remove HTML tags, decode HTML entities, strip legal and EEO disclosures,
        and normalize whitespace while preserving salary ranges and core requirements.
        """
        if not text:
            return ""

        # 1. Unescape HTML entities (&amp; -> &, &lt; -> <, etc.)
        cleaned = html.unescape(text)

        # 2. Convert block and break tags to explicit newlines
        cleaned = self._html_breaks.sub("\n\n", cleaned)

        # 3. Strip all remaining HTML tags
        cleaned = self._html_tags.sub(" ", cleaned)

        # 4. Remove EEO, legal, and recruiter boilerplate
        for pattern in self._compiled_boilerplate:
            cleaned = pattern.sub("\n\n", cleaned)

        # 5. Normalize whitespace line by line
        lines = [self._horizontal_spaces.sub(" ", line).strip() for line in cleaned.splitlines()]
        cleaned = "\n".join(lines)

        # 6. Collapse excessive blank lines
        cleaned = self._vertical_spaces.sub("\n\n", cleaned)
        return cleaned.strip()

    def validate_length(self, text: str) -> None:
        """
        Validate that the cleaned text has strictly more than min_chars characters.

        :raises DescriptionTooShortError: if text has len <= min_chars.
        """
        stripped_len = len(text.strip()) if text else 0
        if stripped_len <= self.min_chars:
            raise DescriptionTooShortError(
                f"Job description too short: {stripped_len} characters "
                f"(minimum >{self.min_chars} required)"
            )

    def wrap_delimiters(self, text: str) -> str:
        """
        Wrap text in XML delimiters while neutralizing adversarial closing tags.
        """
        # Escape any closing tag inside user-provided content to prevent prompt escape
        sanitized = self._closing_tag_pattern.sub(f"&lt;/{self.tag}&gt;", text)
        return f"<{self.tag}>\n{sanitized}\n</{self.tag}>"

    def strip_delimiters(self, text: str) -> str:
        """
        Unwrap XML delimiter tags if present.
        """
        pattern = rf"<{self.tag}>\s*(.*?)\s*</{self.tag}>"
        match = re.search(pattern, text, re.DOTALL)
        return match.group(1).strip() if match else text.strip()

    def _truncate_to_char_budget(self, text: str, max_chars: int) -> str:
        """
        Intelligently truncate text within max_chars preserving natural sentence
        or paragraph boundaries, appending the truncation indicator.
        """
        if len(text) <= max_chars:
            return text

        marker_len = len(self.TRUNCATION_MARKER)
        effective_budget = max(0, max_chars - marker_len)

        candidate = text[:effective_budget]

        # Attempt to cut at a paragraph boundary in the last 30% of candidate
        min_threshold = int(effective_budget * 0.7)
        last_para = candidate.rfind("\n\n")
        if last_para >= min_threshold:
            cut_idx = last_para
        else:
            # Attempt to cut at a sentence boundary (. / ! / ?)
            sentence_cuts = [
                candidate.rfind(". "),
                candidate.rfind(".\n"),
                candidate.rfind("! "),
                candidate.rfind("? "),
            ]
            last_sentence = max(sentence_cuts)
            if last_sentence >= min_threshold:
                cut_idx = last_sentence + 1
            else:
                # Attempt to cut at a word boundary
                last_space = candidate.rfind(" ")
                cut_idx = last_space if last_space > 0 else effective_budget

        return text[:cut_idx].rstrip() + self.TRUNCATION_MARKER

    def process(self, text: str, wrap_xml: Optional[bool] = None) -> TruncationResult:
        """
        Execute full pre-processing pipeline and return an auditable TruncationResult.

        :param text: Raw input job description.
        :param wrap_xml: Optional boolean to override default XML wrapping.
        :raises DescriptionTooShortError: if cleaned content has <= min_chars characters.
        :return: TruncationResult with raw, cleaned, final text, token counts, and flags.
        """
        if text is None:
            raise DescriptionTooShortError("Job description cannot be None")

        should_wrap = self.default_wrap_xml if wrap_xml is None else wrap_xml

        # 1. Clean noise, HTML, and boilerplate
        cleaned = self.clean_boilerplate(text)

        # 2. Validate length (> min_chars)
        self.validate_length(cleaned)

        original_tokens = self.estimate_tokens(cleaned)

        # 3. Calculate character and token budget
        wrapper_overhead_chars = len(f"<{self.tag}>\n\n</{self.tag}>") if should_wrap else 0
        total_max_chars = int(self.max_tokens * self.chars_per_token)
        content_max_chars = total_max_chars - wrapper_overhead_chars

        # 4. Truncate content if needed
        was_truncated = False
        if len(cleaned) > content_max_chars:
            content_text = self._truncate_to_char_budget(cleaned, content_max_chars)
            was_truncated = True
        else:
            content_text = cleaned

        # 5. Wrap with delimiters if requested
        final_text = self.wrap_delimiters(content_text) if should_wrap else content_text
        final_tokens = self.estimate_tokens(final_text)

        return TruncationResult(
            raw_text=text,
            cleaned_text=cleaned,
            final_text=final_text,
            original_tokens=original_tokens,
            final_tokens=final_tokens,
            was_truncated=was_truncated,
        )

    def truncate(self, text: str, wrap_xml: Optional[bool] = None) -> str:
        """
        Truncates, cleans, validates, and wraps text according to project contract.
        Returns the final string directly.
        """
        result = self.process(text, wrap_xml=wrap_xml)
        return result.final_text
