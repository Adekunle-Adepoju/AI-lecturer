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

This limit applies EQUALLY to distractors, not just the correct answer. A fabricated
wrong option is still a fidelity violation — a student who correctly rules out a
distractor using real slide knowledge should never be second-guessed by a distractor
that doesn't actually appear anywhere in the source, because that teaches them the
slide is less complete or reliable than it is.

BARE LISTS ARE THE HIGHEST-RISK CASE FOR THIS RULE. If the slide only names or lists
items without elaboration, you may test whether a student understands what the term
means and why it matters — never a specific number, named sub-type, or detail the
slide never gave.

## DISTRACTOR RULES
Each question needs exactly 7 options: 1 correct answer and 6 distractors, so the
serving system can rotate through a random subset each time the question is shown
without the same wrong options appearing every time.

Before writing distractors, first identify every OTHER fact, factor, term, or claim
actually present in the slide excerpt that is related to this question's topic but is
NOT the correct answer — these are your primary source for distractors. Only once
you've used what's genuinely there should you build any additional distractor, and
even then it must stay grounded in the same fidelity limit above — never a
technical-sounding option invented from outside knowledge just because the slide
didn't offer enough real material. If the slide doesn't contain enough real content to
support 6 distinct, plausible, fidelity-grounded distractors, generate FEWER
distractors or skip the question entirely rather than inventing ungrounded ones to
hit the count. A question with 4 solid distractors beats one with 6 where 2 are
fabricated.

Distractors must also be:
- Plausible to someone who half-understands the topic — not obviously wrong.
- Each WRONG for a DIFFERENT reason where possible (a common misconception, a
  swapped term, an adjacent-but-different concept from the SAME slide content) —
  never six near-duplicates of the same wrong idea.
- Never a filler option like "None of the above" or a trivially absurd choice —
  every option, right or wrong, should look like a serious candidate answer.

## QUESTION STYLE
- MCQ only, no theory/calculation questions requiring worked steps.
- One clear concept tested per question — not a compound question testing two facts.
- Never phrase a question as a reference to its source — no "according to the slides",
  "as stated in the lecture", "what does the slide say about...", "what is the formula
  given in the slides for...", or similar. Ask the question directly, the way a
  lecturer or exam would ask it cold, with no framing that reveals or references
  where the content came from.
  Wrong: "What is the formula given in the slides for calculating the life of a well?"
  Right: "What is the formula for calculating the life of a well (ta)?"
  Wrong: "According to the lecture slides, what does the formation resistivity factor F express?"
  Right: "What does the formation resistivity factor F express?"
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

Never use double-quote characters (") inside question, correct_answer, or distractor
text — if you need to quote a term, use single quotes instead. This is a strict JSON
requirement, not a style preference.

Generate up to {target_count} questions. Generate fewer if the slide content cannot
honestly support that many without breaking the fidelity rule above.

LECTURER SLIDES:
{slide_context}
"""