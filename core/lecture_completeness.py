"""
core/lecture_completeness.py

Detects truncated pregenerated lectures and safely continues them.

Key insight: PreGeneratedLesson only ever stores the LECTURE section
(never the quiz — that's generated separately, later, from the chat
transcript). So the only thing that matters for "was this truncated?"
is whether the model actually finished teaching before running out of
tokens — and the model always signals that by reaching ---QUIZ---.
If ---QUIZ--- is present at all, the lecture body is complete, full
stop, regardless of whether the quiz JSON itself got cut off.
"""

import re
import time

from django.conf import settings
from google.genai import types

from .prompt import SYSTEM_PROMPT

CODE_FENCE = "```"
BOLD_MARKER = "**"

SENTENCE_END_RE = re.compile(r'(?<=[.?!])\s+(?=[A-Z*>#\d])')
PARAGRAPH_BREAK_RE = re.compile(r'\n\s*\n')
TABLE_ROW_RE = re.compile(r'^\|.*\|$')

WORKED_EXAMPLE_RE = re.compile(r'worked example\s*\d*', re.IGNORECASE)
GOVERNING_EQ_RE = re.compile(r'\bgoverning equation\b', re.IGNORECASE)
FINAL_COMP_RE = re.compile(r'\bfinal computation\b', re.IGNORECASE)
TRANSITION_PROMISE_RE = re.compile(
    r"^(let'?s|let us)\s*(now\s+)?"
    r"(work through|examine|demonstrate|explore|solve|calculate|work out|"
    r"cover|look at|go through|tackle|walk through|dive into)\b",
    re.IGNORECASE,
)


def _ends_on_dangling_promise(text):
    """True if the very last paragraph is a transition sentence promising
    upcoming examples/content that never actually arrives — e.g. 'Let us
    now solve three detailed examples...' with nothing after it. This is
    independent of _has_dangling_worked_example: it catches the case
    where a PRIOR worked example was completed properly, but a NEW
    promise was made afterward and the response ran out before starting it."""
    paragraphs = [p.strip() for p in PARAGRAPH_BREAK_RE.split(text) if p.strip()]
    if not paragraphs:
        return False
    return bool(TRANSITION_PROMISE_RE.match(paragraphs[-1]))


MAX_CONTINUATION_ATTEMPTS = 4


def _looks_like_complete_formula(line):
    """A '> formula' line is safe to keep only if it isn't hanging on an
    operator or an open bracket — i.e. it wasn't cut off mid-equation."""
    stripped = line.strip().lstrip(">").strip()
    if not stripped:
        return False
    if stripped[-1] in "+-*/×÷=([{":
        return False
    return True


def _has_dangling_worked_example(text):
    """True if the text opens a worked example (or reaches its GOVERNING
    EQUATION step) but never reaches FINAL COMPUTATION for it. Catches
    truncation that happens to land on a grammatically complete sentence
    — e.g. a problem statement ending in a period with no arithmetic
    following it — which pure punctuation checks can't detect."""
    matches = list(WORKED_EXAMPLE_RE.finditer(text))
    if not matches:
        matches = list(GOVERNING_EQ_RE.finditer(text))
    if not matches:
        return False
    last_open = matches[-1].start()
    return not FINAL_COMP_RE.search(text[last_open:])


def is_lecture_truncated(full_text):
    if not full_text or not full_text.strip():
        return True

    if "---QUIZ---" in full_text:
        return False

    stripped = full_text.rstrip()

    if stripped.count(CODE_FENCE) % 2 != 0:
        return True
    if stripped.count(BOLD_MARKER) % 2 != 0:
        return True
    if (stripped.count("{") - stripped.count("}")) != 0:
        return True
    if (stripped.count("[") - stripped.count("]")) != 0:
        return True

    last_line = stripped.split("\n")[-1].strip()
    if last_line.startswith(">") and not _looks_like_complete_formula(last_line):
        return True

    if _has_dangling_worked_example(stripped):
        return True

    # NEW — catches a clean-sounding closer that's actually a promise
    # for content that was never delivered, even when earlier worked
    # examples in the same document were completed correctly.
    if _ends_on_dangling_promise(stripped):
        return True

    last_char = stripped[-1]
    if last_char in ".?!" or last_char in ")]}":
        return False
    if stripped.endswith(CODE_FENCE):
        return False
    if TABLE_ROW_RE.match(last_line):
        return False

    return True


def find_safe_cutoff(text, min_keep_ratio=0.5):
    text = text.rstrip()
    if not text:
        return text

    if text.count(CODE_FENCE) % 2 != 0:
        text = text[:text.rfind(CODE_FENCE)].rstrip()

    if text.count(BOLD_MARKER) % 2 != 0:
        text = text[:text.rfind(BOLD_MARKER)].rstrip()

    lines = text.split("\n")
    if lines and lines[-1].strip().startswith(">") and not _looks_like_complete_formula(lines[-1]):
        text = "\n".join(lines[:-1]).rstrip()

    if _has_dangling_worked_example(text):
        matches = list(WORKED_EXAMPLE_RE.finditer(text)) or list(GOVERNING_EQ_RE.finditer(text))
        if matches:
            cut_at = matches[-1].start()
            prior_breaks = [m for m in PARAGRAPH_BREAK_RE.finditer(text) if m.start() < cut_at]
            if prior_breaks:
                cut_at = prior_breaks[-1].start()
            text = text[:cut_at].rstrip()

    # NEW — strip a trailing dangling-promise paragraph unconditionally,
    # independent of the worked-example branch above.
    if _ends_on_dangling_promise(text):
        parts = [p for p in PARAGRAPH_BREAK_RE.split(text) if p.strip()]
        text = "\n\n".join(parts[:-1]).rstrip()

    if not text:
        return text

    paragraph_breaks = list(PARAGRAPH_BREAK_RE.finditer(text))
    if paragraph_breaks:
        cut_at = paragraph_breaks[-1].start()
        if cut_at >= len(text) * min_keep_ratio:
            return text[:cut_at].rstrip()

    sentence_ends = list(SENTENCE_END_RE.finditer(text))
    if sentence_ends:
        cut_at = sentence_ends[-1].start() + 1
        return text[:cut_at].rstrip()

    return text


# Broadened: catches "Sub-topic N: X", bare "**Bold Heading**" lines,
# and "### Heading" markdown — not just literal "Sub-topic" prefixes,
# since continuation passes don't reliably use that exact phrasing.
SUBTOPIC_HEADING_RE = re.compile(
    r'(?:^|\n)\s*(?:#{1,4}\s*)?(?:\*\*)?'
    r'(?:Sub-?topic\s*\d*[:.]?\s*)?'
    r'([A-Z][A-Za-z0-9\s\-\(\)/]{3,70}(?:\([A-Za-z_]+\))?)'
    r'\s*(?:\*\*)?\s*(?=\n)',
)

# Key technical terms/variable names that, if repeated as the FOCUS of a
# new section heading, strongly signal re-teaching rather than new content.
KEY_CONCEPT_RE = re.compile(
    r'\b(Bo|Bt|Bg|Rs|Rsi|co|cw|cg|cf|Z-?factor|API gravity|'
    r'solution gas-?oil ratio|formation volume factor|compressibility)\b',
    re.IGNORECASE,
)


def _extract_covered_subtopics(full_previous_content):
    """Pull out sub-topic/section titles already taught across the ENTIRE
    accumulated lecture so far — not just the tail — so continuation
    passes know what NOT to re-teach."""
    titles = SUBTOPIC_HEADING_RE.findall(full_previous_content)
    seen, deduped = set(), []
    for t in titles:
        clean = t.strip().rstrip(':').strip()
        # Skip generic prose fragments that aren't really headings
        if len(clean.split()) > 12 or len(clean.split()) < 1:
            continue
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            deduped.append(clean)
    return deduped


def _extract_key_concepts(text):
    """Which core variables/concepts (Bo, Rs, Z-factor, etc.) a chunk of
    text is centrally about — used to catch re-teaching even when the
    heading itself is phrased completely differently."""
    return {m.group(1).lower() for m in KEY_CONCEPT_RE.finditer(text)}


def detect_likely_duplicate_reteach(previous_content, new_continuation, overlap_threshold=0.6):
    """Safety net independent of heading text matching: if the new
    continuation's early paragraphs are dominated by the same core
    variables/concepts as an already-taught section (Bo, Rs, Z-factor,
    etc.), it's very likely re-teaching that section under a different
    heading — exactly what slipped past heading-based detection here.
    Checked against just the OPENING of the new content (first ~600
    chars), since that's where a new sub-topic announces its subject."""
    new_opening = new_continuation[:600]
    new_concepts = _extract_key_concepts(new_opening)
    if len(new_concepts) < 2:
        return False  # too generic a signal to act on

    prior_paragraphs = [p for p in PARAGRAPH_BREAK_RE.split(previous_content) if p.strip()]
    for para in prior_paragraphs:
        para_concepts = _extract_key_concepts(para)
        if len(para_concepts) < 2:
            continue
        overlap = len(new_concepts & para_concepts) / len(new_concepts)
        if overlap >= overlap_threshold and ("worked example" in para.lower() or "final computation" in para.lower()):
            return True
    return False


def continue_truncated_lecture(extraction_client, course_code, course_title, topic_name,
                                week, level, student_name, topic_index,
                                previous_content, slide_text=""):
    tail_context = previous_content[-1200:]
    word_count = len(previous_content.split())

    covered = _extract_covered_subtopics(previous_content)
    covered_block = ""
    if covered:
        covered_list = "\n".join(f"- {c}" for c in covered)
        covered_block = (
            f"\n\nSUB-TOPICS ALREADY FULLY TAUGHT IN THIS LECTURE — DO NOT "
            f"RE-TEACH THESE, DO NOT REPEAT THEIR WORKED EXAMPLES, DO NOT "
            f"INTRODUCE THEM AGAIN AS IF THEY WERE NEW:\n{covered_list}\n\n"
            f"If you are tempted to write a sub-topic heading that matches "
            f"or overlaps any of the above, STOP — that content already "
            f"exists earlier in this document. Move to a genuinely new "
            f"sub-topic, or if all reasonable sub-topics for this specific "
            f"topic are already covered, wrap up and go straight to "
            f"---QUIZ---.\n"
        )

    # Hard backstop against runaway length: if the lecture has already
    # grown well past the 3500-word target across continuation passes,
    # stop opening new sub-topics entirely and force it toward the quiz.
    length_instruction = ""
    if word_count > 6000:
        length_instruction = (
            f"\n\nThis lecture is already {word_count} words long — well "
            f"past target length. Do NOT open any new sub-topic. Finish "
            f"only whatever single point was left hanging, then go "
            f"straight to ---QUIZ---.\n"
        )

    resume_instruction = (
        "Continue writing directly from that exact point, picking up "
        "mid-flow as if no interruption happened."
    )
    if tail_context.rstrip().endswith((".", "!", "?")):
        resume_instruction = (
            "The text above ends cleanly. Continue from there — if a "
            "worked example was left unstarted or unfinished, begin (or "
            "restart) it fully using the complete GOVERNING EQUATION → "
            "KNOWN PARAMETERS → LINE-BY-LINE ARITHMETIC → FINAL "
            "COMPUTATION structure. Never state a problem without solving it."
        )

    continuation_message = (
        f"Course: {course_code} — {course_title}\n"
        f"Topic: {topic_name}\n"
        f"Level: {level}L | Week: {week}\n"
        f"Student name: {student_name}\n"
        f"Topic number: {topic_index + 1} of 3 in this session\n"
        f"{covered_block}"
        f"{length_instruction}\n"
        f"You were teaching this topic and got cut off before finishing. "
        f"Here is the END of what you already wrote — do NOT repeat any of "
        f"it, do NOT re-introduce the topic, do NOT restate ---INTRO--- or "
        f"---LECTURE--- markers:\n\n"
        f"...{tail_context}\n\n"
        f"{resume_instruction} Keep teaching until the topic is fully and "
        f"deeply covered per the length and structure rules, then output "
        f"the ---QUIZ--- section exactly as instructed."
        f"{slide_text}"
    )

    last_error = None
    for attempt in range(3):
        try:
            response = extraction_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=continuation_message,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=8000,
                ),
            )
            text = response.text
            if not text or not text.strip():
                raise ValueError("Empty response from continuation call")
            return text
        except Exception as e:
            last_error = e
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                time.sleep(20)
                continue
            if any(m in error_str for m in ["503", "UNAVAILABLE", "timeout", "Timeout"]):
                time.sleep(10)
                continue
            break

    raise RuntimeError(f"Continuation failed for {course_code} — {topic_name}: {last_error}")