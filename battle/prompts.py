"""
battle/prompts.py

Prompts for the battle-mode question bank. Mirrors the fidelity rules in
core/prompt.py's SYSTEM_PROMPT — a question built from outside knowledge is
a worse failure here than in a lecture, because it's scored competitively
against a classmate with real XP on the line and no lecturer in the loop
to catch it.
"""

BATTLE_QUESTION_GENERATION_PROMPT = r"""
You are generating multiple-choice quiz questions for Rovea's competitive quiz-battle
mode, for Petroleum and Gas Engineering students at the University of Lagos (Unilag).

Course: {course_code}
Topic: {topic_name}

## FIDELITY HARD LIMIT — READ THIS FIRST
Every question, its correct answer, and every distractor must be answerable using
ONLY the LECTURER SLIDES content below. Do not introduce a technical term,
sub-technique, named method, formula, classification, real-world case, or fact that
is not present in the slide excerpt — even if it is accurate, standard, commonly
taught industry knowledge. If the slide content is thin, generate fewer questions
rather than padding with outside knowledge. A short, fully faithful set of questions
is correct; a richer-seeming question built from outside knowledge is a failure,
always — these questions are scored head-to-head between students for real
competitive stakes, so a wrong or ungrounded answer key is worse here than anywhere
else in the app.

BARE LISTS ARE THE HIGHEST-RISK CASE FOR THIS RULE. If the slide only names or lists
items without elaboration, you may test whether a student understands what the term
means and why it matters — never a specific number, named sub-type, or detail the
slide never gave.

## DISTRACTOR RULES
Each question needs exactly 7 options: 1 correct answer and 6 distractors, so the
serving system can rotate through a random subset each time the question is shown
without the same wrong options appearing every time. Distractors must be:
- Plausible to someone who half-understands the topic — not obviously wrong.
- Each WRONG for a DIFFERENT reason where possible (a common misconception, a
  swapped term, an adjacent-but-different concept from the SAME slide content) —
  never six near-duplicates of the same wrong idea.
- Grounded in the same fidelity limit as the question itself — never invent a
  technical-sounding distractor using a term or fact not in the slide excerpt.
If the slide content is too thin to support 6 distinct, plausible distractors
without fabricating detail, generate fewer questions rather than weakening this
rule — do not fall back to filler distractors like "None of the above" or trivially
wrong options.

## QUESTION STYLE
- MCQ only, no theory/calculation questions requiring worked steps.
- One clear concept tested per question — not a compound question testing two facts.
- Plain text only in question/options — no LaTeX, no markdown formatting, since
  this is parsed as JSON and rendered with no math renderer.
- Difficulty: medium — a student who attended the lecture should get it right,
  a student who skimmed should not.

Return ONLY a JSON array, no explanation, no markdown fences, no preamble. Each
element in this exact format:

[
  {{
    "question": "Question text here",
    "correct_answer": "The correct option text",
    "distractors": ["Wrong option 1", "Wrong option 2", "Wrong option 3", "Wrong option 4", "Wrong option 5", "Wrong option 6"]
  }}
]

Generate up to {target_count} questions. Generate fewer if the slide content cannot
honestly support that many without breaking the fidelity rule above.

LECTURER SLIDES:
{slide_context}
"""