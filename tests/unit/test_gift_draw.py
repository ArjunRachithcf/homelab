"""wishlist/draw.py: the draw keeps its rules, a giver's mail names only their own person, and the pairs never
reach the terminal or a plaintext file."""
import contextlib
import importlib.util
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gift_draw", ROOT / "wishlist/draw.py")
draw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(draw)

PEOPLE = {u: {"username": u, "name": u.title(), "email": f"{u}@example.org", "list": f"list-{u}"}
          for u in ("alice", "bob", "carol", "dave", "erin")}
EVENT = """name = "Christmas Example"
budget = "CHF 50"
participants = ["alice", "bob", "carol", "dave", "erin"]
exclude = [["alice", "bob"], ["carol", "dave"]]
"""


class Draw(unittest.TestCase):
    def test_rules_hold(self):
        people = list(PEOPLE)
        exclude = [["alice", "bob"], ["carol", "dave"]]
        for seed in range(300):
            pairs = draw.draw(people, exclude, random.Random(seed))
            self.assertEqual(sorted(pairs), sorted(people))
            self.assertEqual(sorted(pairs.values()), sorted(people), "everyone receives exactly once")
            for g, r in pairs.items():
                self.assertNotEqual(g, r)
                self.assertNotIn(sorted([g, r]), [sorted(p) for p in exclude])

    def test_impossible(self):
        with self.assertRaises(draw.DrawError):
            draw.draw(["alice", "bob"], [["alice", "bob"]], random.Random(1))

    def test_event_checked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "e.toml")
            p.write_text('name = "X"\nparticipants = ["alice", "alice"]\n')
            with self.assertRaises(draw.DrawError):
                draw.load_event(p)
            p.write_text('name = "X"\nparticipants = ["alice", "bob"]\nexclude = [["alice", "zoe"]]\n')
            with self.assertRaises(draw.DrawError):
                draw.load_event(p)

    def test_who_hasnt_signed_in(self):
        known = {k: v for k, v in PEOPLE.items() if k != "erin"}
        with self.assertRaisesRegex(draw.DrawError, "erin"):
            draw.check_people({"participants": list(PEOPLE)}, known)

    def test_mail_names_only_their_person(self):
        body = draw.mail_body({"name": "Christmas Example", "budget": "CHF 50"}, PEOPLE["alice"], PEOPLE["carol"],
                              "https://gifts.example.org:9443", "Home")
        self.assertIn("Carol", body)
        self.assertIn("https://gifts.example.org:9443/lists/list-carol", body)
        self.assertIn("CHF 50", body)
        for other in ("Bob", "Dave", "Erin"):
            self.assertNotIn(other, body)


class Run(unittest.TestCase):
    def test_draw_mails_each_and_prints_no_pair(self):
        with tempfile.TemporaryDirectory() as d:
            ev = Path(d, "xmas.toml")
            ev.write_text(EVENT)
            sent, stored = [], {}

            def fake_store(path, recipient, record):
                stored["record"], stored["recipient"] = record, recipient
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"age-encryption.org/v1 (stub)")

            env = {"BACKUP_AGE_RECIPIENT": "age1example", "DOMAIN": "example.org", "BRAND": "Home"}
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(draw, "STATE", Path(d, "state")), \
                    mock.patch.object(draw, "people_in_wishlist", return_value=PEOPLE), \
                    mock.patch.object(draw, "env_get", side_effect=lambda k: env.get(k, "")), \
                    mock.patch.object(draw, "store", side_effect=fake_store), \
                    mock.patch.object(draw, "send", side_effect=lambda to, s, b: sent.append((to, s, b))), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(draw.main(["draw", str(ev)]), 0)
                self.assertEqual(draw.main(["draw", str(ev)]), 2, "a second draw needs --redraw")
            pairs = stored["record"]["pairs"]
            self.assertEqual(stored["recipient"], "age1example")
            self.assertEqual(len(sent), 5)
            for to, subject, body in sent:
                giver = to.split("@")[0]
                self.assertIn(PEOPLE[pairs[giver]]["name"], body)
                self.assertNotIn(PEOPLE[pairs[giver]]["name"], subject)
            printed = out.getvalue() + err.getvalue()
            for g, r in pairs.items():
                self.assertNotIn(f"{g} -> {r}", printed)
                self.assertNotIn(json.dumps(r), printed)
            for f in Path(d, "state").iterdir():
                self.assertNotIn(b"alice", f.read_bytes(), "only the encrypted draw is on disk")


if __name__ == "__main__":
    unittest.main()
