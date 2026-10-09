"""
core/language_profile.py

Extra prompt rules for courses that teach a LANGUAGE SKILL (words, sounds, sentences)
rather than calculations, e.g. GST111 Communication in English.

Design rules:
- prompt.py is never edited. The base prompts stay exactly as they are.
- A course with no profile gets back the SAME string object it got before this file
  existed (lecture_system_prompt(code) is OUTLINE_LECTURE_PROMPT), so a course such as
  CHM101 cannot change. test_language_profile.py enforces this.
- To give another course this profile, add its code to COURSE_PROFILES.
"""
import re

from .prompt import OUTLINE_LECTURE_PROMPT, OUTLINE_SCOPE_VERIFIER_PROMPT

# course code (no spaces, upper case) -> profile name
COURSE_PROFILES = {
    "GST111": "language",
}


LANGUAGE_EXAMPLE_PROFILE = r"""
LANGUAGE-COURSE EXAMPLES (THIS COURSE ONLY)
These rules ADD to everything above. They override it only where they say so.

1. WHAT CHANGES
This course teaches a language skill, so ideas are shown with real words, sounds and sentences.
The rule above that says topics with no calculation get NO worked examples forbids invented
NUMERICAL examples only. Examples made of words and sentences are expected in this course, and
they are not a scope violation.

2. LET THE LEARNING OUTCOMES CHOOSE THE KIND OF EXAMPLE
The outline lists learning outcomes. Find the outcome this topic serves and follow its verb:
- identify or classify: show real words or sentences and say which category each belongs to and
  why. Give one clear instance for every category the topic names. Where two categories are easy
  to confuse, add one contrasting pair.
- construct or write: build up in steps. Start with a short plain sentence (or the smallest
  correct piece of the writing), add one thing at a time, and say what each addition does.
- apply: state a short problem, then reason through it one small step at a time.
- demonstrate: give one short model, then point out what makes it work. Do not pretend that
  reading it is the same as practising it. Say plainly that the skill grows by practising aloud
  or on paper.
- list, define or discuss: no example is needed beyond a good analogy.

If no outcome fits this topic, choose by the nature of the topic. A grammar or usage rule gets a
correct sentence beside a wrong one, clearly labelled. Word classes and word formation get sorted
examples. A writing task gets a short model with its parts pointed out.
  Never describe a category, a property or a pattern without giving at least one real word or
  sentence that shows it. Never claim that something changes meaning or has an effect without
  showing one case of it.

3. WHEN TO GIVE ONE, AND HOW MUCH
Give an example only when the idea has a confusable neighbour, is a rule the student must apply,
is a classification, or is a mistake you have just warned about. Skip it when an analogy or a
picture already carries the idea. At most one set of examples per section, and at most five short
items in a set. Place it after the plain-words explanation and before the "Remember for exams"
line. Keep it short enough that the section still fits its pages.

4. SCOPE STAYS TIGHT
An example may show any idea or property you mention in the lesson, including standard
fundamentals of a listed item. It must not bring in a further concept, rule or technical term in
order to explain itself. Use common words and short plain sentences. Do not
use names of real people, places or brands (write "the student" or "my friend"), and do not quote
real poems, songs, speeches or books.

5. WORDS AND SOUNDS ARE NOT MATHEMATICS
Write example words and sentences as ordinary text, in italics or plain, never between dollar
signs. Show stress with capital letters on the stressed syllable (PREsent). Never use IPA or other
phonetic symbols: the lesson is read aloud by a text-to-speech voice that cannot read them.
Describe a sound in plain words and give a key word, for example "the short vowel in sit".

6. SOUND EXAMPLES NEED EXTRA CARE
The reference is standard British English and its usual count of 44 sounds (20 vowels and 24
consonants). The first time you give a count or a pronunciation, say once: "In this course we follow standard
British English, which has 44 sounds: 20 vowels and 24 consonants." Count sounds, not letters,
and say so wherever spelling and sound disagree. Before using a sound example, silently check it:
how many sounds the word has, which are vowels and which are consonants, and where the stress
falls. If you are not certain, or if the word is said very differently in common accents, use a
different and more ordinary word. A wrong example is worse than none.
"""


LANGUAGE_VERIFIER_NOTE = r"""
NOTE FOR THIS COURSE (it teaches a language skill): ordinary example words, sounds and short
generic sentences used to demonstrate an idea the lesson teaches are NOT out_of_scope and NOT
unsourced_specifics, even though the outline does not list them. DO flag an example that brings in
a further concept, rule or technical term the outline does not support, that names a real person,
place or brand, or that quotes a real text.
"""


_LECTURE_BLOCKS = {"language": LANGUAGE_EXAMPLE_PROFILE}
_VERIFIER_NOTES = {"language": LANGUAGE_VERIFIER_NOTE}

# The verifier note is placed just above this line of OUTLINE_SCOPE_VERIFIER_PROMPT.
_VERIFIER_ANCHOR = "OTHER TOPICS IN THIS COURSE:"


def _normalise(course_code):
    return re.sub(r"\s+", "", course_code or "").upper()


def profile_for(course_code):
    """Profile name for a course code, or None for a course with no profile."""
    return COURSE_PROFILES.get(_normalise(course_code))


def lecture_system_prompt(course_code=None):
    """System prompt for lesson generation. A course with no profile gets back
    OUTLINE_LECTURE_PROMPT itself, untouched."""
    profile = profile_for(course_code)
    if profile is None:
        return OUTLINE_LECTURE_PROMPT
    return OUTLINE_LECTURE_PROMPT + "\n" + _LECTURE_BLOCKS[profile]


def scope_verifier_prompt(course_code=None):
    """Scope-verifier prompt. A course with no profile gets back
    OUTLINE_SCOPE_VERIFIER_PROMPT itself, untouched."""
    profile = profile_for(course_code)
    if profile is None or _VERIFIER_ANCHOR not in OUTLINE_SCOPE_VERIFIER_PROMPT:
        return OUTLINE_SCOPE_VERIFIER_PROMPT
    note = _VERIFIER_NOTES[profile].strip()
    return OUTLINE_SCOPE_VERIFIER_PROMPT.replace(
        _VERIFIER_ANCHOR, f"{note}\n\n{_VERIFIER_ANCHOR}", 1,
    )