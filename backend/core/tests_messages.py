"""Messages are kept for good (Nov 2026): who sent what to whom, when, when each person saw it; the alert that rings
and speaks it is only the news and expires. Inbox / sent / with one person / a period / words, by screen and by voice;
nobody reads a message between two others."""
import json
from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from core import services
from core.models import Alert, Message, MessageReceipt
from core.tests import BaseCase, _cmd


class MessagesKeptTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "username": "nikki", "password": "nikki1"})
        self.nikki_client = APIClient()
        self.nikki_client.force_authenticate(self.nikki)

    def ask(self, client, text, **fields):
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="query", **fields)):
            r = client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_a_message_is_kept_with_its_receipts_and_survives_the_alert_cleanup(self):
        alerts = services.send_message_to(self.md, [self.pa, self.nikki], "Client aa rahe hain 4 baje, conference room ready rakhein")
        self.assertEqual(len(alerts), 2)
        m = Message.objects.get()
        self.assertEqual(m.sender_id, self.md.id)
        self.assertEqual(m.text, "Client aa rahe hain 4 baje, conference room ready rakhein")
        self.assertEqual({r.recipient_id for r in m.receipts.all()}, {self.pa.id, self.nikki.id})
        self.assertTrue(all(a.ref_type == "message" and a.ref_id == str(m.id) for a in alerts))
        # The PA's phone tells it (delivered), then she taps Seen: the kept record says so; Nikki's copy stays new.
        services.mark_alerts_told(self.pa, [alerts[0].id])
        services.mark_seen(self.pa, Alert.objects.get(id=alerts[0].id))
        pa_copy = MessageReceipt.objects.get(message=m, recipient=self.pa)
        self.assertIsNotNone(pa_copy.delivered_at)
        self.assertIsNotNone(pa_copy.seen_at)
        self.assertIsNone(MessageReceipt.objects.get(message=m, recipient=self.nikki).seen_at)
        # The same words moments later (heard twice) are not sent twice.
        self.assertEqual(services.send_message_to(self.md, [self.pa], "Client aa rahe hain 4 baje, conference room ready rakhein"), [])
        self.assertEqual(Message.objects.count(), 1)
        # A week passes, the alerts are cleaned up: the message and the receipts remain exactly as they were.
        Alert.objects.filter(ref_type="message").update(created_at=timezone.now() - timedelta(days=8))
        services.cleanup_alerts()
        self.assertFalse(Alert.objects.filter(ref_type="message").exists())
        self.assertTrue(Message.objects.filter(id=m.id).exists())
        self.assertIsNotNone(MessageReceipt.objects.get(message=m, recipient=self.pa).seen_at)

    def test_inbox_sent_one_person_period_and_words(self):
        services.send_message_to(self.md, [self.pa], "Kal subah 10 baje office aa jaana")
        services.send_message_to(self.pa, [self.md], "Sir, vendor ka payment ho gaya")
        services.send_message_to(self.pa, [self.nikki], "Nikki, GST file kar dena")
        old = services.send_message_to(self.md, [self.pa], "Purana message pichhle mahine ka")[0]
        Message.objects.filter(id=old.ref_id).update(sent_at=timezone.now() - timedelta(days=40))
        # The PA: both boxes.
        d = self.ask(self.pa_client, "mere messages dikhao", view_kind="messages")
        rows = d["view"]["rows"]
        self.assertEqual(d["view"]["kind"], "messages")
        self.assertEqual({r["box"] for r in rows}, {"inbox", "sent"})
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["status"], "Not seen yet")        # her latest: sent to Nikki, Nikki has not seen it
        # Only what she sent, only what Sir sent her, a period, words.
        d = self.ask(self.pa_client, "maine kya bheja", view_kind="messages", view_filter="sent")
        self.assertEqual([r["box"] for r in d["view"]["rows"]], ["sent", "sent"])
        d = self.ask(self.pa_client, "Sir ne kya message bheja tha", view_kind="messages", view_who="Sir", view_filter="inbox")
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Kal subah 10 baje office aa jaana", "Purana message pichhle mahine ka"])
        self.assertIn("2 messages with Sir", d["spoken_reply"])
        self.assertIn("not seen yet", d["spoken_reply"])
        today = timezone.localdate()
        d = self.ask(self.pa_client, "aaj ke messages", view_kind="messages", view_from=today.isoformat(), view_to=today.isoformat())
        self.assertNotIn("Purana message", json.dumps(d))
        d = self.ask(self.pa_client, "payment wala message", view_kind="messages", view_words="payment")
        self.assertEqual([r["title"] for r in d["view"]["rows"]], ["Sir, vendor ka payment ho gaya"])
        # Downloadable like any card.
        self.assertTrue(d["view"]["file_name"].startswith("jarvis-messages-"))
        self.assertIn("vendor ka payment", d["view"]["text"])
        # The screen's own page.
        r = self.pa_client.get("/api/v1/views/messages/?filter=inbox")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(row["box"] == "inbox" for row in r.json()["rows"]))

    def test_nobody_reads_a_message_between_two_others(self):
        services.send_message_to(self.md, [self.pa], "Sir to PA: confidential bonus figures")
        services.send_message_to(self.pa, [self.md], "PA to Sir: the figures are ready")
        for text, fields in (("Sir ke messages dikhao", dict(view_kind="messages", view_who="Sir")),
                             ("Priya ke messages", dict(view_kind="messages", view_who="Priya")),
                             ("sab messages", dict(view_kind="messages"))):
            d = self.ask(self.nikki_client, text, **fields)
            self.assertNotIn("bonus", json.dumps(d))
            self.assertNotIn("figures", json.dumps(d))
            self.assertEqual(d["view"]["rows"], [])
        r = self.nikki_client.get("/api/v1/views/messages/?who=Priya")
        self.assertEqual(r.json()["rows"], [])
        # The MD sees only his own messages too: never Priya's to Nikki.
        services.send_message_to(self.pa, [self.nikki], "PA to Nikki: private word")
        d = self.ask(self.md_client, "sab messages", view_kind="messages")
        self.assertNotIn("private word", json.dumps(d))
