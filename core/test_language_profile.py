from django.test import SimpleTestCase

from core.language_profile import (
    COURSE_PROFILES,
    LANGUAGE_EXAMPLE_PROFILE,
    LANGUAGE_VERIFIER_NOTE,
    lecture_system_prompt,
    profile_for,
    scope_verifier_prompt,
)
from core.prompt import OUTLINE_LECTURE_PROMPT, OUTLINE_SCOPE_VERIFIER_PROMPT

ANCHOR = "OTHER TOPICS IN THIS COURSE:"


class UnprofiledCoursesAreUntouched(SimpleTestCase):
    def test_chm101_lecture_prompt_is_the_same_object(self):
        self.assertIs(lecture_system_prompt("CHM101"), OUTLINE_LECTURE_PROMPT)

    def test_chm101_verifier_prompt_is_the_same_object(self):
        self.assertIs(scope_verifier_prompt("CHM101"), OUTLINE_SCOPE_VERIFIER_PROMPT)

    def test_missing_or_unknown_codes_are_untouched(self):
        for code in (None, "", "PGG434", "GST112", "GST1111"):
            self.assertIs(lecture_system_prompt(code), OUTLINE_LECTURE_PROMPT)
            self.assertIs(scope_verifier_prompt(code), OUTLINE_SCOPE_VERIFIER_PROMPT)

    def test_chm101_has_no_profile(self):
        self.assertNotIn("CHM101", COURSE_PROFILES)
        self.assertIsNone(profile_for("CHM101"))


class Gst111GetsTheLanguageProfile(SimpleTestCase):
    def test_code_variants_all_match(self):
        for code in ("GST111", "gst111", "GST 111", " gst 111 "):
            self.assertEqual(profile_for(code), "language", code)

    def test_lecture_prompt_is_base_plus_block(self):
        got = lecture_system_prompt("GST111")
        self.assertTrue(got.startswith(OUTLINE_LECTURE_PROMPT))
        self.assertTrue(got.endswith(LANGUAGE_EXAMPLE_PROFILE))
        self.assertGreater(len(got), len(OUTLINE_LECTURE_PROMPT))

    def test_verifier_note_inserted_once_above_anchor(self):
        got = scope_verifier_prompt("GST111")
        self.assertEqual(got.count(LANGUAGE_VERIFIER_NOTE.strip()), 1)
        self.assertEqual(got.count(ANCHOR), 1)
        self.assertLess(got.index(LANGUAGE_VERIFIER_NOTE.strip()), got.index(ANCHOR))

    def test_verifier_placeholders_survive(self):
        got = scope_verifier_prompt("GST111")
        for token in ("__LEVEL__", "__TOPIC__", "__OTHER_TOPICS__", "__OUTLINE__", "__LECTURE__"):
            self.assertIn(token, got)

    def test_removing_the_note_restores_the_base_verifier(self):
        got = scope_verifier_prompt("GST111")
        restored = got.replace(LANGUAGE_VERIFIER_NOTE.strip() + "\n\n", "", 1)
        self.assertEqual(restored, OUTLINE_SCOPE_VERIFIER_PROMPT)


class BasePromptsStillHaveWhatTheGateNeeds(SimpleTestCase):
    def test_anchor_appears_exactly_once_in_base_verifier(self):
        self.assertEqual(OUTLINE_SCOPE_VERIFIER_PROMPT.count(ANCHOR), 1)

    def test_profile_text_has_no_placeholder_tokens(self):
        for text in (LANGUAGE_EXAMPLE_PROFILE, LANGUAGE_VERIFIER_NOTE):
            self.assertNotIn("__", text)