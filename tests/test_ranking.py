import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_watch  # noqa: E402
import marks  # noqa: E402
import ranker  # noqa: E402
import scorer  # noqa: E402


def job(i, company="Acme", title="Customer Success Manager", text="Pay: $70,000 - $80,000. Remote US.", pay=""):
    return {"id": i, "company": company, "title": title, "url": f"https://x/{i}", "where": "Remote", "pay": pay, "text": text}


def score(a, b, q, loc="yes", **kw):
    return {"they_pick_him": a, "he_wants_it": b, "job_quality": q, "location_ok": loc, "why": "w", **kw}


class ScorerTests(unittest.TestCase):
    def test_parses_array_out_of_chatty_reply(self):
        self.assertEqual(scorer.parse_reply('Sure! [{"id": "a"}] Hope that helps.'), [{"id": "a"}])

    def test_unverified_pay_is_blanked(self):
        rec = scorer.audit_pay({"pay": "$90,000 - $99,000"}, job("a"))
        self.assertEqual(rec["pay"], "not listed")
        kept = scorer.audit_pay({"pay": "$70,000 - $80,000"}, job("a"))
        self.assertEqual(kept["pay"], "$70,000 - $80,000")

    def test_keeps_valid_records_and_drops_bad_ones(self):
        reply = json.dumps([
            {"id": "a", "pay": "$70,000", **score(4, 5, 3)},
            {"id": "b", "pay": "not listed", **score(9, 5, 3)},        # score out of range
            {"id": "c", "pay": "not listed", **score(3, 3, 3, loc="maybe")},  # bad location value
            {"id": "zzz", "pay": "not listed", **score(3, 3, 3)},       # unknown id
        ])
        got = scorer.score_jobs([job("a"), job("b"), job("c")], "profile", {}, run=lambda p: reply, log=lambda *_: None)
        self.assertEqual(list(got), ["a"])
        self.assertEqual(got["a"]["source"], "claude")

    def test_cli_failure_returns_empty(self):
        def boom(prompt):
            raise RuntimeError("signed out")
        self.assertEqual(scorer.score_jobs([job("a")], "p", {}, run=boom, log=lambda *_: None), {})

    def test_prompt_marks_postings_untrusted_and_includes_health(self):
        seen = {}
        scorer.score_jobs([job("a", company="Pushpay")], "PROFILE", {"Pushpay": "Glassdoor 3.9"},
                          run=lambda p: seen.setdefault("p", p) and "[]", log=lambda *_: None)
        self.assertIn("untrusted", seen["p"])
        self.assertIn("Glassdoor 3.9", seen["p"])
        self.assertIn('<posting id="a">', seen["p"])


class RankerTests(unittest.TestCase):
    def test_order_exclusions_and_tiebreak(self):
        jobs = [job("old"), job("new"), job("weak"), job("faraway"), job("done", title="Done Role"), job("unscored")]
        scores = {"old": score(4, 4, 4), "new": score(4, 4, 4), "weak": score(1, 5, 5),
                  "faraway": score(5, 5, 5, loc="no"), "done": score(5, 5, 5)}
        m = {"ids": {ranker.sid("done"): {"mark": "applied"}}, "keys": {}}
        first_seen = {"old": "2026-10-10", "new": "2026-10-19"}
        order = [j["id"] for j, _ in ranker.rank(jobs, scores, m, first_seen)]
        self.assertEqual(order, ["new", "old"])

    def test_marked_title_also_hides_reposts(self):
        jobs = [job("repost", company="Pushpay", title="CSM, Nurture")]
        m = {"ids": {}, "keys": {ranker.key("Pushpay", "CSM, Nurture"): "applied"}}
        self.assertEqual(ranker.rank(jobs, {"repost": score(5, 5, 5)}, m, {}), [])

    def test_composite_penalizes_one_sided_fit(self):
        self.assertGreater(ranker.composite(score(4, 4, 3)), ranker.composite(score(2, 5, 5)))


class MarksTests(unittest.TestCase):
    def test_parses_taps_and_records_title_key(self):
        s = ranker.sid("gh:pushpay:1")
        m = {"ids": {}, "keys": {}, "since": 0}
        info = {s: {"company": "Pushpay", "title": "CSM", "url": "https://x/1"}}
        ups = marks.apply_marks([(1760000000, f"applied {s}"), (1760000001, "hello"), (1760000002, "skip 0123456789")], m, info)
        self.assertEqual(m["ids"][s]["mark"], "applied")
        self.assertEqual(m["ids"]["0123456789"]["mark"], "skip")
        self.assertIn(ranker.key("Pushpay", "CSM"), m["keys"])
        self.assertEqual(m["since"], 1760000003)
        self.assertEqual(list(ups), ["https://x/1"])

    def test_fills_applied_column_in_job_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.csv"
            with p.open("w", newline="") as f:
                csv.writer(f).writerows([["date_found", "link", "applied"], ["2026-10-17", "https://x/1", ""], ["2026-10-17", "https://x/2", ""]])
            marks.mark_csv(p, {"https://x/1": "2026-10-19"})
            with p.open() as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[1][2], "2026-10-19")
            self.assertEqual(rows[2][2], "")


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.real = job_watch.ntfy
        job_watch.ntfy = lambda title, message, click=None, tags=(), actions=None: self.sent.append((title, click, actions))

    def tearDown(self):
        job_watch.ntfy = self.real

    def test_morning_sends_five_to_one_then_header_with_buttons(self):
        ranked = [(job(f"j{n}", company=f"C{n}"), score(5, 5, 5)) for n in range(1, 8)]
        job_watch.send_morning(ranked, new_ids={"j2"})
        titles = [t for t, _, _ in self.sent]
        self.assertEqual([t.split(" ")[0] for t in titles[:5]], ["#5", "#4", "#3", "#2", "#1"])
        self.assertTrue(titles[3].endswith("(new)"))
        self.assertTrue(titles[5].startswith("Apply today: your top 5 of 7"))
        actions = self.sent[4][2]
        self.assertEqual([a["label"] for a in actions], ["Applied", "Skip"])
        self.assertEqual(actions[0]["body"], f"applied {ranker.sid('j1')}")
        self.assertTrue(actions[0]["url"].endswith(job_watch.CFG["marks_topic"]))

    def test_afternoon_only_alerts_new_jobs_in_top_ten(self):
        ranked = [(job(f"j{n}"), score(5, 5, 5)) for n in range(1, 13)]
        job_watch.send_new_top(ranked, new_ids={"j3", "j11"})
        self.assertEqual([t.split(":")[0] for t, _, _ in self.sent], ["New #3"])

    def test_morning_skips_new_jobs_already_in_top_five(self):
        ranked = [(job(f"j{n}"), score(5, 5, 5)) for n in range(1, 13)]
        job_watch.send_new_top(ranked, new_ids={"j2", "j7"}, skip_top=5)
        self.assertEqual([t.split(":")[0] for t, _, _ in self.sent], ["New #7"])


class ModeTests(unittest.TestCase):
    def at(self, hour):
        class Fake(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 20, hour, 5)
        job_watch.datetime, self.saved = Fake, job_watch.datetime

    def tearDown(self):
        job_watch.datetime = getattr(self, "saved", job_watch.datetime)

    def test_modes_by_hour_and_state(self):
        today = job_watch.TODAY
        self.at(4)
        self.assertEqual(job_watch.pick_mode("", {}), "marks")
        self.at(9)
        self.assertEqual(job_watch.pick_mode("", {}), "morning")
        self.assertEqual(job_watch.pick_mode("", {"last_morning": today}), "marks")
        self.at(17)
        self.assertEqual(job_watch.pick_mode("", {"last_morning": today}), "afternoon")
        self.assertEqual(job_watch.pick_mode("", {}), "morning")  # Mac slept through 8 AM: morning list first
        self.assertEqual(job_watch.pick_mode("--marks", {}), "marks")


if __name__ == "__main__":
    unittest.main()


class ClipTests(unittest.TestCase):
    def test_pay_in_the_middle_of_a_long_posting_survives(self):
        text = "A" * 5000 + "\nAnnual Salary: $90,000 - $110,000\n" + "B" * 4000
        self.assertIn("$90,000 - $110,000", scorer.clip(text))

    def test_short_posting_untouched(self):
        self.assertEqual(scorer.clip("short $1"), "short $1")


class BigBoardTests(unittest.TestCase):
    CARD = ('<li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:4472354169">'
            '<h3 class="base-search-card__title"> Implementation Specialist </h3>'
            '<h4 class="base-search-card__subtitle"><a href="x">Acme &amp; Co</a></h4>'
            '<span class="job-search-card__location"> Dallas, TX </span>'
            '<span class="job-search-card__salary-info"> $30.00/hr - $40.00/hr </span>'
            '<time class="job-search-card__listdate" datetime="2026-10-22">1 day ago</time></div></li>')

    def test_parses_linkedin_cards(self):
        c = job_watch.parse_linkedin_cards("<ul>" + self.CARD + "</ul>")[0]
        self.assertEqual((c["id"], c["title"], c["company"], c["location"], c["date"]),
                         ("4472354169", "Implementation Specialist", "Acme & Co", "Dallas, TX", "2026-10-22"))

    def test_hourly_salary_is_annualized(self):
        self.assertEqual(job_watch.pay_from_text("$30.00/hr - $40.00/hr"), (62400.0, 83200.0))

    def test_board_jobs_use_the_60k_floor(self):
        j = job_watch.job(id="in:1", company="Acme", title="Customer Success Manager", remote=True, feed=True,
                          loc_text="Remote, USA", pay_max=65000, pay_min=55000, floor=60000, url="u", source="Indeed")
        self.assertEqual(job_watch.classify(j), "other")
        j2 = dict(j, floor=None)
        self.assertIsNone(job_watch.classify(j2))  # the default $70k floor still applies elsewhere

    def test_unranked_only_when_scorer_is_down(self):
        new = [job("a"), job("b")]
        self.assertEqual(job_watch.unranked_plan(new, attempted=5, got=0), (new, set()))
        self.assertEqual(job_watch.unranked_plan(new, attempted=36, got=36), ([], {"a", "b"}))


class SmallFixTests(unittest.TestCase):
    def test_hourly_pay_shows_per_hour(self):
        self.assertEqual(ranker.pay_text({}, {"pay": "$40.87–$45.67"}), "$40.87–$45.67/hr")
        self.assertEqual(ranker.pay_text({}, {"pay": "$24.50 per hour"}), "$24.50/hr")
        self.assertEqual(ranker.pay_text({}, {"pay": "$80,000 - $100,000"}), "$80,000 - $100,000")

    def test_unclear_location_is_flagged(self):
        body = job_watch.ranked_body(job("a"), score(4, 4, 4, loc="unclear"))
        self.assertIn("location unclear", body)
        self.assertNotIn("location unclear", job_watch.ranked_body(job("a"), score(4, 4, 4)))
