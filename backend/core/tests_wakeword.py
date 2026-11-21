"""The wake word (Nov 2026): one of a few phrases the phone can listen for, chosen in Settings or by voice; the
assistant's own name follows it ("Hey Billy" makes it Billy) while the app stays JARVIS."""
from unittest import mock

from core import livevoice, services
from core.tests import BaseCase, _cmd


class WakeWordTests(BaseCase):
    def test_the_wake_word_sets_the_assistants_name(self):
        self.assertEqual(services.assistant_name(self.md), "JARVIS")
        r = self.md_client.patch("/api/v1/me/prefs/", {"wakeWord": "hey billy"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["wakeWord"], "hey_billy")
        self.assertEqual(services.assistant_name(self.md), "Billy")
        self.assertEqual(services.wake_word_of(self.md)["model"], "hey_billy.onnx")
        # The brain and the live line are told the name and the call; the transcription expects the name.
        ctx = services.ai_context(self.md)
        self.assertEqual(ctx["assistant"], {"name": "Billy", "name_hi": "बिली", "wake_phrase": "Hey Billy"})
        text = livevoice.instructions_for(self.md, ctx)
        self.assertIn("You are Billy", text)
        self.assertIn("Hey Billy", text)
        self.assertIn("Billy", livevoice.keyterms_for(self.md, ctx))
        # Each person's own: the PA's assistant is still JARVIS.
        self.assertEqual(services.assistant_name(self.pa), "JARVIS")
        self.assertIn("You are JARVIS", livevoice.instructions_for(self.pa, services.ai_context(self.pa)))
        # By voice: "ab mujhe OK Boss bolke bulao" (the phrase as said, or the key).
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="set_setting", setting_key="wakeWord", setting_value="OK Boss",
                                                                     spoken_reply="Ji Sir, ab OK Boss boliye.")):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "ab mujhe OK Boss bolke bulao"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual((d["action"], d["setting_patch"]), ("set_setting", {"wakeWord": "ok_boss"}), d)
        # The app applies the patch (as it does for every shared setting) and reloads the listener for the new word.
        r = self.md_client.patch("/api/v1/me/prefs/", d["setting_patch"], format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(services.assistant_name(self.md), "Boss")
        # Only the bundled words: anything else is refused.
        self.assertEqual(self.md_client.patch("/api/v1/me/prefs/", {"wakeWord": "hey siri"}, format="json").status_code, 422)
        self.assertEqual(services.assistant_name(self.md), "Boss")
