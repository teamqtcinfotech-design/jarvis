"""Phase 3 of the team plan (17 Nov 2026): departments and sections.

Departments (groups of people) made, changed and deleted by the MD by voice; the manager and the PA never inside
one. Sections (folders of work) filed from the words, moved by voice, learned. Answers in the shape of the question:
a department's card, all departments, a section's work. Privacy: nobody's personal items in any of it.
"""
import json
from unittest import mock

from rest_framework.test import APIClient

from core import actions, assistant, datacards, organise, sections, services
from core.models import Alert, Department, Kind, Priority, Section, SharedTask, TaskEvent
from core.tests import SECRET, BaseCase, _cmd


class SectionsBase(BaseCase):
    def setUp(self):
        super().setUp()
        self.nikki = services.team_add(self.md, {"name": "Nikki", "title": "Accountant", "department": "Accounts",
                                                 "gender": "female", "username": "nikki", "password": "nikki1"})
        self.seema = services.team_add(self.md, {"name": "Seema", "title": "Social Media Manager", "department": "Social Media",
                                                 "gender": "female", "username": "seema", "password": "seema1"})
        self.kiran = services.team_add(self.md, {"name": "Kiran", "title": "Farm Manager", "username": "kiran", "password": "kiran1"})
        self.mansi = services.get_manager()
        self.clients = {}
        for u in (self.nikki, self.seema, self.kiran):
            c = APIClient()
            c.force_authenticate(u)
            self.clients[u.username] = c

    def say(self, client, text, cmd):
        with mock.patch("core.ai_brain.interpret", return_value=cmd):
            r = client.post("/api/v1/assistant/interpret/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def organise(self, client, text, **extra):
        return self.say(client, text, _cmd(intent="organise", title="", **extra))


class DepartmentTests(SectionsBase):
    def test_defaults_and_the_team_page_put_people_in_departments(self):
        names = {d.name for d in sections.departments()}
        self.assertEqual(names, {"Accounts", "Social Media"})  # only the two defaults exist
        self.assertEqual([d.name for d in sections.member_of(self.nikki)], ["Accounts"])
        self.assertEqual([d.name for d in sections.member_of(self.seema)], ["Social Media"])
        self.assertEqual(sections.member_of(self.kiran), [])
        # The matching sections exist too, and the manager and the PA are outside every department.
        self.assertEqual({s.name for s in sections.sections(None, "WORK")}, {"Accounts", "Social Media"})
        self.assertEqual([u.username for u in sections.management()], ["manager", "pa"])  # the manager first
        with self.assertRaises(services.ServiceError):
            sections.eligible(self.mansi)
        # Typing a department on the Team page moves the person: out of Accounts, into a new Farm.
        services.team_update(self.md, self.nikki.id, {"department": "Farm"})
        self.assertEqual([d.name for d in sections.member_of(self.nikki)], ["Farm"])
        self.assertTrue(Department.objects.filter(name="Farm", active=True).exists())
        self.nikki.refresh_from_db()
        self.assertEqual(self.nikki.department, "Farm")

    def test_make_a_department_by_voice_with_people_and_undo(self):
        d = self.organise(self.md_client, "ek naya department banao Farm aur usme Kiran ko add kar do")
        self.assertTrue(d["booked"], d)
        self.assertIn("Farm department made", d["spoken_reply"])
        self.assertIn("Kiran is in it", d["spoken_reply"])
        dept = Department.objects.get(name="Farm")
        self.assertEqual([u.username for u in dept.members.all()], ["kiran"])
        self.assertEqual(d["view"]["kind"], "department")  # the department card comes with the answer
        self.assertTrue(Alert.objects.filter(recipient=self.kiran, ref_type="department").exists())  # Kiran told
        # Undo takes it back.
        r = actions.undo_last(self.md)
        self.assertEqual(r["undone"], 1)
        dept.refresh_from_db()
        self.assertFalse(dept.active)
        self.assertEqual(sections.member_of(self.kiran), [])

    def test_only_the_md_and_never_the_manager_or_pa(self):
        d = self.organise(self.clients["nikki"], "ek naya department banao Farm")
        self.assertIn("only Sir can change departments", d["spoken_reply"])
        self.assertFalse(Department.objects.filter(name="Farm").exists())
        d = self.organise(self.md_client, "Mansi ko Accounts mein daalo")
        self.assertIn("is the manager", d["spoken_reply"])
        self.assertNotIn(self.mansi, Department.objects.get(name="Accounts").members.all())
        d = self.organise(self.md_client, "Marketing department delete kar do")
        self.assertIn("no department called marketing", d["spoken_reply"].lower())

    def test_remove_and_delete_ask_first_then_happen_through_the_card(self):
        d = self.organise(self.md_client, "Nikki ko Accounts se hata do")
        self.assertEqual(d["action"], "organise")
        self.assertIn("take Nikki out of Accounts?", d["question"])
        self.assertIn(self.nikki, Department.objects.get(name="Accounts").members.all())  # nothing yet
        # The card comes back with a yes (the phone's save, or the closed-app conversation).
        r = self.md_client.post("/api/v1/assistant/execute/", {"card": {"action": "organise", "title": d["title"], "organise": d["organise"]}},
                                format="json", HTTP_IDEMPOTENCY_KEY="org-1")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotIn(self.nikki, Department.objects.get(name="Accounts").members.all())
        self.assertIn("Nikki is out of Accounts", r.json()["en"])
        # Undo puts her back.
        self.assertEqual(actions.undo_last(self.md)["undone"], 1)
        self.assertIn(self.nikki, Department.objects.get(name="Accounts").members.all())
        d = self.organise(self.md_client, "Social Media department delete kar do")
        self.assertIn("delete the Social Media department?", d["question"])
        self.assertIn("Its 1 person stays on the team", d["question"])
        organise.apply(self.md, d["organise"])
        self.assertFalse(Department.objects.get(name="Social Media").active)
        self.assertEqual(sections.member_of(self.seema), [])

    def test_rename_and_add_by_voice(self):
        d = self.organise(self.md_client, "Accounts department ka naam badal ke Finance kar do")
        self.assertTrue(d["booked"], d)
        self.assertTrue(Department.objects.filter(name="Finance").exists())
        self.assertEqual(Section.objects.get(name="Finance").department.name, "Finance")  # its section follows
        d = self.organise(self.md_client, "Kiran ko Finance mein daal do")
        self.assertIn("Kiran is in Finance now", d["spoken_reply"])
        # A misheard name still finds the person.
        d = self.organise(self.md_client, "Seema ko Finance mein daal do")
        self.assertEqual({u.username for u in Department.objects.get(name="Finance").members.all()}, {"nikki", "kiran", "seema"})
        self.assertEqual(len(sections.member_of(self.seema)), 2)  # a person can be in two departments

    def test_the_departments_api(self):
        r = self.clients["nikki"].post("/api/v1/departments/", {"name": "Farm"}, format="json")
        self.assertEqual(r.status_code, 403)
        r = self.md_client.post("/api/v1/departments/", {"name": "Farm", "people": [self.kiran.id]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        dept_id = r.json()["id"]
        self.assertEqual([m["name"] for m in r.json()["members"]], ["Kiran"])
        r = self.md_client.patch(f"/api/v1/departments/{dept_id}/", {"add": [self.nikki.id], "remove": [self.kiran.id], "name": "Farms"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["name"], [m["name"] for m in r.json()["members"]]), ("Farms", ["Nikki"]))
        listing = self.clients["kiran"].get("/api/v1/departments/").json()
        self.assertEqual([d["name"] for d in listing["results"]], ["Accounts", "Farms", "Social Media"])
        self.assertEqual([m["name"] for m in listing["management"]], ["Mansi", "Priya"])
        self.assertEqual(self.md_client.delete(f"/api/v1/departments/{dept_id}/").status_code, 200)
        self.assertNotIn("Farms", [d["name"] for d in self.clients["kiran"].get("/api/v1/departments/").json()["results"]])
        # The manager cannot be added through the API either.
        r = self.md_client.patch(f"/api/v1/departments/{Department.objects.get(name='Accounts').id}/", {"add": [self.mansi.id]}, format="json")
        self.assertEqual(r.status_code, 422)


class FilingTests(SectionsBase):
    def test_work_is_filed_from_its_words_else_by_the_persons_department(self):
        organise.apply(self.md, {"what": "section", "do": "create", "name": "GST"})
        gst = Section.objects.get(name="GST")
        t1 = services.create_person_task(self.md, self.kiran, {"title": "Send the GST File", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.assertEqual(t1.section, gst)  # the words
        t2 = services.create_person_task(self.md, self.nikki, {"title": "Send the Bank Statement", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.assertEqual(t2.section.name, "Accounts")  # Nikki's department
        t3 = services.create_person_task(self.md, self.kiran, {"title": "Count the Cattle", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.assertIsNone(t3.section)  # nothing to go on: not filed, never guessed
        # A section said outright wins and is made if new.
        t4 = services.create_person_task(self.md, self.nikki, {"title": "Call the Auditor", "kind": Kind.TASK, "priority": Priority.NORMAL,
                                                                 "section": "Audit 2026"}, compose_note=False)
        self.assertEqual(t4.section.name, "Audit 2026")
        lines = [e["text"] for e in services.task_history(SharedTask.objects.get(id=t1.id))]
        self.assertEqual(lines[0], "Given by Sir to Kiran · in GST")  # filing from the words is part of the giving
        self.assertEqual(services.shared_item(t1)["section"], "GST")
        self.assertIn("FILED", [e["kind"] for e in services.task_history(SharedTask.objects.get(id=t4.id))])  # said outright: its own step

    def test_move_by_voice_teaches_the_section_and_shows_in_the_history(self):
        task = services.create_person_task(self.md, self.kiran, {"title": "Pune Franchise Agreement", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.assertIsNone(task.section)
        screen = {"kind": "task", "title": task.title, "items": [{"id": str(task.id), "title": task.title}]}
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="organise", title="Partners")):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "ise Partners mein daal do", "screen": screen}, format="json")
        d = r.json()
        self.assertTrue(d["booked"], d)
        task.refresh_from_db()
        self.assertEqual(task.section.name, "Partners")
        self.assertIn("pune", task.section.aliases)  # learned
        self.assertIn("franchise", task.section.aliases)
        lines = [e["text"] for e in services.task_history(task)]
        self.assertIn("Put in Partners by Sir", lines)
        # The next task with those words files itself there.
        t2 = services.create_person_task(self.md, self.nikki, {"title": "Franchise Fee Invoice", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.assertEqual(t2.section.name, "Partners")
        # Kiran (it is hers) may move it; Seema (nothing to do with it) may not.
        r = self.clients["kiran"].post(f"/api/v1/shared-tasks/{task.id}/section/", {"section": "Farm Work"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["task"]["section"], "Farm Work")
        r = self.clients["seema"].post(f"/api/v1/shared-tasks/{task.id}/section/", {"section": "Partners"}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_sections_api_and_who_may_change_them(self):
        r = self.clients["kiran"].post("/api/v1/sections/", {"name": "Farm Work"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)  # anyone makes a work section
        sid = r.json()["id"]
        self.assertEqual(self.clients["kiran"].patch(f"/api/v1/sections/{sid}/", {"name": "Farm"}, format="json").status_code, 403)
        self.assertEqual(self.md_client.patch(f"/api/v1/sections/{sid}/", {"name": "Farm"}, format="json").json()["name"], "Farm")
        mine = self.clients["kiran"].post("/api/v1/sections/", {"name": "Ghar", "kind": "PERSONAL"}, format="json").json()
        self.assertEqual(mine["kind"], "PERSONAL")
        names = [s["name"] for s in self.clients["kiran"].get("/api/v1/sections/").json()["results"]]
        self.assertIn("Ghar", names)
        self.assertNotIn("Ghar", [s["name"] for s in self.md_client.get("/api/v1/sections/").json()["results"]])  # hers alone
        # Merge and delete: the MD, with the tasks following.
        gst = organise.apply(self.md, {"what": "section", "do": "create", "name": "GST"})
        gst_id = Section.objects.get(name="GST").id
        t = services.create_person_task(self.md, self.nikki, {"title": "GST Return", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.assertEqual(t.section_id, gst_id)
        self.md_client.patch(f"/api/v1/sections/{gst_id}/", {"merge_into": sid}, format="json")
        t.refresh_from_db()
        self.assertEqual(t.section_id, sid)
        self.assertFalse(Section.objects.get(id=gst_id).active)
        self.assertIn("gst", Section.objects.get(id=sid).aliases)
        del gst


class AnswerShapeTests(SectionsBase):
    def setUp(self):
        super().setUp()
        organise.apply(self.md, {"what": "department", "do": "create", "name": "Farm", "people": [self.kiran.id]})
        organise.apply(self.md, {"what": "section", "do": "create", "name": "GST"})
        self.t1 = services.create_person_task(self.md, self.nikki, {"title": "File the GST Return", "kind": Kind.TASK, "priority": Priority.HIGH}, compose_note=False)
        self.t2 = services.create_person_task(self.mansi, self.nikki, {"title": "GST Audit Papers", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.t3 = services.create_person_task(self.md, self.kiran, {"title": "Count the Cattle", "kind": Kind.TASK, "priority": Priority.NORMAL}, compose_note=False)
        self.private = services.create_private_task(self.nikki, {"title": "GST of my own flat", "kind": Kind.TASK, "priority": Priority.NORMAL})

    def test_a_departments_card_and_all_departments(self):
        view = datacards.build(self.md, "department", params={"group": "accounts team"})
        self.assertEqual(view["kind"], "department")
        self.assertEqual([r["title"] for r in view["rows"]], ["Nikki"])
        self.assertEqual(view["rows"][0]["assigned"], 2)
        self.assertIn("Accounts has 1 person: Nikki; 2 open", view["summary"])
        grid = datacards.build(self.md, "departments")
        self.assertEqual([r["title"] for r in grid["rows"]], ["Accounts", "Farm", "Social Media"])
        self.assertIn("Management: Mansi (Manager), Priya (PA)", grid["subtitle"])
        self.assertIn("3 departments", grid["summary"])
        # A department nobody has: the grid, with a word about it.
        view = datacards.build(self.md, "department", params={"group": "Marketing"})
        self.assertEqual(view["kind"], "departments")
        self.assertIn("no department called Marketing", view["summary"])
        # Mansi, who gave Nikki one task, sees only that in the card's counts.
        view = datacards.build(self.mansi, "department", params={"group": "Accounts"})
        self.assertEqual(view["rows"][0]["assigned"], 1)

    def test_a_sections_work_grouped_by_person_and_in_the_history(self):
        view = datacards.build(self.md, "section", params={"group": "GST"})
        self.assertEqual(view["kind"], "section")
        self.assertEqual(view["title"], "GST section")
        self.assertEqual(sorted(r["title"] for r in view["rows"]), ["File the GST Return", "GST Audit Papers"])
        self.assertTrue(all(r["who"] == "Nikki" for r in view["rows"]))
        self.assertIn("GST has 2", view["summary"])
        self.assertNotIn("flat", json.dumps(view))  # Nikki's personal item never
        # The same by the generic list with the section as the group, and through the API.
        view = datacards.build(self.md, "tasks", params={"group": "gst", "filter": "open"})
        self.assertEqual(view["kind"], "section")
        r = self.md_client.get("/api/v1/views/section/?group=GST")
        self.assertEqual(r.status_code, 200)
        # Mansi sees in GST only what she gave.
        view = datacards.build(self.mansi, "section", params={"group": "GST"})
        self.assertEqual([r["title"] for r in view["rows"]], ["GST Audit Papers"])
        # Kiran (nothing in GST) gets an honest empty answer, never Nikki's work.
        view = datacards.build(self.kiran, "section", params={"group": "GST"})
        self.assertEqual(view["rows"], [])
        # History narrowed to a section or a department.
        services.update_task_status(self.nikki, self.t1.id, "COMPLETED", self.t1.version)
        hist = datacards.build(self.md, "history", params={"group": "GST"})
        self.assertEqual(sorted(r["title"] for r in hist["rows"]), ["File the GST Return", "GST Audit Papers"])
        hist = datacards.build(self.md, "history", params={"group": "Farm"})
        self.assertEqual([r["title"] for r in hist["rows"]], ["Count the Cattle"])
        # A section that does not exist: an honest answer naming the ones that do.
        view = datacards.build(self.md, "section", params={"group": "Tax"})
        self.assertIn("no section called Tax", view["summary"])
        self.assertIn("GST", view["summary"])

    def test_the_departments_work_as_a_task_list_and_people_by_membership(self):
        view = datacards.build(self.md, "tasks", params={"who": "Farm"})
        self.assertEqual([r["title"] for r in view["rows"]], ["Count the Cattle"])
        people = datacards.build(self.md, "people", params={"group": "Farm"})
        self.assertEqual([r["title"] for r in people["rows"]], ["Kiran"])
        self.assertNotIn(SECRET, json.dumps(view) + json.dumps(people))

    def test_send_the_list_on_screen_to_someone(self):
        screen = {"kind": "section", "title": "GST section", "items": [{"id": str(self.t1.id), "title": self.t1.title}],
                  "filters": {"kind": "section", "group": "GST", "filter": "open"}}
        with mock.patch("core.ai_brain.interpret", return_value=_cmd(intent="screen_action", screen_action="send_list", recipients=["Nikki"], staff_name="Nikki")):
            r = self.md_client.post("/api/v1/assistant/interpret/", {"text": "ye list Nikki ko bhej do", "screen": screen}, format="json")
        d = r.json()
        self.assertIn("Sent the list to Nikki", d["spoken_reply"])
        msg = Alert.objects.get(recipient=self.nikki, ref_type="message")
        self.assertIn("GST section", msg.body)
        self.assertIn("File the GST Return", msg.body)
        self.assertNotIn("flat", msg.body)

    def test_the_ai_context_knows_departments_and_sections(self):
        ctx = services.ai_context(self.md)
        self.assertEqual([d["name"] for d in ctx["departments"]], ["Accounts", "Farm", "Social Media"])
        self.assertEqual(ctx["management"], ["Mansi (Manager)", "Priya (PA)"])
        self.assertIn("GST", ctx["sections"])
        self.assertNotIn("flat", json.dumps(ctx, default=str))
        # Rename a department by voice through the whole path, then the card for it.
        d = self.say(self.md_client, "sab departments dikhao", _cmd(intent="query", view_kind="departments"))
        self.assertEqual(d["view"]["kind"], "departments")
        d = self.say(self.md_client, "Farm department dikhao", _cmd(intent="query", view_kind="department", view_group="Farm"))
        self.assertEqual((d["view"]["kind"], [r["title"] for r in d["view"]["rows"]]), ("department", ["Kiran"]))


class OrganiseReaderTests(BaseCase):
    """The rule reading used when the AI is off."""

    def read(self, text):
        return organise._read_rules(text)

    def test_shapes(self):
        r = self.read("ek naya department banao Farm aur usme Kiran aur Sunil ko add kar do")
        self.assertEqual((r["what"], r["do"], r["name"], r["people"]), ("department", "create", "farm", ["Kiran", "Sunil"]))
        r = self.read("Nikki ko Accounts se hata do")
        self.assertEqual((r["what"], r["do"], r["name"], r["people"]), ("department", "remove", "accounts", ["nikki"]))
        r = self.read("Kiran ko Farm mein daal do")
        self.assertEqual((r["what"], r["do"], r["name"], r["people"]), ("department", "add", "farm", ["kiran"]))
        r = self.read("GST section banao")
        self.assertEqual((r["what"], r["do"], r["name"]), ("section", "create", "gst"))
        r = self.read("ise Partners mein daal do")
        self.assertEqual((r["what"], r["do"], r["name"]), ("task", "move", "partners"))
        r = self.read("Franchise section ko Partners mein merge kar do")
        self.assertEqual((r["what"], r["do"], r["name"], r["new_name"]), ("section", "merge", "franchise", "partners"))
        r = self.read("Marketing department delete kar do")
        self.assertEqual((r["what"], r["do"], r["name"]), ("department", "delete", "marketing"))
        self.assertEqual(self.read("Sunil ko bolo bank ka kaam kar de")["what"], "none")
