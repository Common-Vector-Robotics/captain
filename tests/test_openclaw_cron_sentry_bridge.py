# tests/test_openclaw_cron_sentry_bridge.py
import json
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import openclaw_cron_sentry_bridge as bridge

FIXTURE = json.loads(
    (ROOT / "fixtures" / "openclaw_cron_list_sample.json").read_text(encoding="utf-8")
)


class JobViewTests(unittest.TestCase):
    def test_nested_state_errors(self):
        """Confirm failure details can be read from a job's nested state."""
        view = bridge.job_view(FIXTURE["jobs"][0])
        self.assertEqual(view["name"], "Nightly Cognee cognify")
        self.assertEqual(view["errors"], 46)
        self.assertEqual(view["last_error"], "cognify timeout after 300s")

    def test_flat_error_count(self):
        """Confirm a top-level failure count is read correctly."""
        view = bridge.job_view(FIXTURE["jobs"][1])
        self.assertEqual(view["errors"], 0)

    def test_consecutive_errors(self):
        """Confirm OpenClaw's consecutive-failure field is supported."""
        view = bridge.job_view(FIXTURE["jobs"][2])
        self.assertEqual(view["errors"], 2)

    def test_no_counter_fields_yields_none(self):
        """Confirm a job without failure counters is marked as unknown."""
        view = bridge.job_view({"id": "x", "name": "bare job"})
        self.assertIsNone(view["errors"])


class DiffTests(unittest.TestCase):
    def _views(self):
        """Return the simplified monitoring view of every example job."""
        return [bridge.job_view(job) for job in FIXTURE["jobs"]]

    def test_first_run_seeds_without_failures(self):
        """Confirm the first run creates a baseline without raising old alerts."""
        failures = bridge.diff_failures({}, self._views())
        self.assertEqual(failures, [])

    def test_error_increase_reports_failure(self):
        """Confirm a rising failure count creates exactly one new alert."""
        prev = bridge.build_state(self._views())
        bumped = json.loads(json.dumps(FIXTURE))
        bumped["jobs"][0]["state"]["errors"] = 47
        views = [bridge.job_view(job) for job in bumped["jobs"]]
        failures = bridge.diff_failures(prev, views)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["name"], "Nightly Cognee cognify")
        self.assertEqual(failures[0]["previous"], 46)
        self.assertEqual(failures[0]["current"], 47)

    def test_unchanged_and_recovered_report_nothing(self):
        """Confirm unchanged or improved jobs do not create alerts."""
        prev = bridge.build_state(self._views())
        failures = bridge.diff_failures(prev, self._views())
        self.assertEqual(failures, [])

    def test_counter_reset_reports_nothing_then_resumes_tracking(self):
        """Confirm a reset is harmless and later failures are still detected."""
        # Prior state has the job at 46 errors.
        prev = bridge.build_state(self._views())

        # OpenClaw restarts and the counter resets to 0 (also try a lower,
        # non-zero value to cover a partial reset).
        for reset_value in (0, 12):
            with self.subTest(reset_value=reset_value):
                reset_fixture = json.loads(json.dumps(FIXTURE))
                reset_fixture["jobs"][0]["state"]["errors"] = reset_value
                reset_views = [
                    bridge.job_view(job) for job in reset_fixture["jobs"]
                ]
                failures = bridge.diff_failures(prev, reset_views)
                self.assertEqual(failures, [])

                # The reset value becomes the new baseline...
                new_state = bridge.build_state(reset_views)

                # ...and a genuine increase from that new baseline IS
                # reported, proving the reset doesn't permanently blind
                # the bridge to future failures.
                bumped_fixture = json.loads(json.dumps(reset_fixture))
                bumped_fixture["jobs"][0]["state"]["errors"] = reset_value + 1
                bumped_views = [
                    bridge.job_view(job) for job in bumped_fixture["jobs"]
                ]
                resumed_failures = bridge.diff_failures(new_state, bumped_views)
                self.assertEqual(len(resumed_failures), 1)
                self.assertEqual(
                    resumed_failures[0]["name"], "Nightly Cognee cognify"
                )
                self.assertEqual(resumed_failures[0]["previous"], reset_value)
                self.assertEqual(resumed_failures[0]["current"], reset_value + 1)

    def test_vanished_job_reports_nothing_and_drops_from_state(self):
        """Confirm a removed job quietly leaves the saved monitoring state."""
        prev = bridge.build_state(self._views())

        # New poll only returns two of the three jobs; "job-stale" vanishes.
        remaining_fixture = json.loads(json.dumps(FIXTURE))
        remaining_fixture["jobs"] = [
            job for job in remaining_fixture["jobs"] if job["id"] != "job-stale"
        ]
        remaining_views = [
            bridge.job_view(job) for job in remaining_fixture["jobs"]
        ]

        failures = bridge.diff_failures(prev, remaining_views)
        self.assertFalse(
            any(f["name"] == "Friday stale-task briefing" for f in failures)
        )

        new_state = bridge.build_state(remaining_views)
        self.assertNotIn("job-stale", new_state["jobs"])

    def test_new_job_with_nonzero_counter_reports_immediately(self):
        """Confirm a newly discovered failing job is reported immediately."""
        # Prior state is non-empty (not the first-run seeding path) but
        # does not contain the new job.
        prev = bridge.build_state(self._views())

        new_job_fixture = json.loads(json.dumps(FIXTURE))
        new_job_fixture["jobs"].append({
            "id": "job-brand-new",
            "name": "Brand new backfill job",
            "state": {"errors": 5, "lastError": "backfill failed"},
        })
        views = [bridge.job_view(job) for job in new_job_fixture["jobs"]]

        failures = bridge.diff_failures(prev, views)
        new_job_failures = [
            f for f in failures if f["name"] == "Brand new backfill job"
        ]
        self.assertEqual(len(new_job_failures), 1)
        self.assertEqual(new_job_failures[0]["previous"], 0)
        self.assertEqual(new_job_failures[0]["current"], 5)


class MainTests(unittest.TestCase):
    def setUp(self):
        """Start each command test with empty captured messages and check-ins."""
        self.messages = []
        self.checkins = []

    def _run(self, tmp_path, run_list, dry_run=False):
        """Run the monitoring command with temporary files and captured outputs."""
        state = tmp_path / "state.json"
        argv = ["--state", str(state)]
        if dry_run:
            argv.append("--dry-run")
        code = bridge.main(
            argv,
            run_list=run_list,
            capture_message_fn=lambda message, **kw: self.messages.append(
                (message, kw)
            ),
            capture_exception_fn=lambda exc, **kw: self.messages.append(
                ("EXC:" + type(exc).__name__, kw)
            ),
            checkin_fn=lambda slug, status, monitor_config=None: self.checkins.append(
                (slug, status)
            ),
            init_fn=lambda component: True,
        )
        return code, state

    def test_two_runs_report_new_failure_and_checkin_ok(self):
        """Confirm a later failure is reported while healthy monitor check-ins continue."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            code, state = self._run(tmp, run_list=lambda bin_: FIXTURE["jobs"])
            self.assertEqual(code, 0)
            self.assertEqual(self.messages, [])  # first run seeds
            bumped = json.loads(json.dumps(FIXTURE))
            bumped["jobs"][2]["consecutiveErrors"] = 3
            code, _ = self._run(tmp, run_list=lambda bin_: bumped["jobs"])
            self.assertEqual(code, 0)
            self.assertEqual(len(self.messages), 1)
            self.assertIn("Friday stale-task briefing", self.messages[0][0])
            self.assertEqual(
                self.messages[0][1]["fingerprint"],
                ["openclaw-cron", "Friday stale-task briefing"],
            )
        self.assertEqual(
            self.checkins,
            [(bridge.MONITOR_SLUG, "ok"), (bridge.MONITOR_SLUG, "ok")],
        )

    def test_cron_list_failure_sends_error_checkin(self):
        """Confirm a failed job-list request produces an error check-in."""
        import tempfile

        def broken(bin_):
            """Simulate OpenClaw being unable to return its scheduled jobs."""
            raise bridge.OpenClawCronListError("openclaw exited 1: not running")

        with tempfile.TemporaryDirectory() as d:
            code, _ = self._run(Path(d), run_list=broken)
        self.assertEqual(code, 1)
        self.assertEqual(self.checkins, [(bridge.MONITOR_SLUG, "error")])
        self.assertEqual(self.messages[0][0], "EXC:OpenClawCronListError")

    def test_dry_run_sends_nothing_and_writes_no_state(self):
        """Confirm preview mode sends no alerts and saves no state."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            code, state = self._run(
                Path(d), run_list=lambda bin_: FIXTURE["jobs"], dry_run=True
            )
            self.assertEqual(code, 0)
            self.assertFalse(state.exists())
        self.assertEqual(self.checkins, [])

    def test_no_counters_warning_fires_even_with_prior_state(self):
        """Confirm missing failure counters remain visible on every run."""
        # Regression: the "no job exposes any counter field" warning used to
        # be gated on `not prev_state`, making it effectively one-shot. If
        # `openclaw cron list --json` field names never match our candidates,
        # that meant zero cron-failure events forever after the first run,
        # while the dead-man's-switch still reported the bridge healthy. The
        # warning must fire on every run where every job is counter-blind,
        # regardless of prior state.
        import tempfile

        counterless_jobs = [
            {"id": "job-a", "name": "Job A"},
            {"id": "job-b", "name": "Job B"},
        ]
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            code, state = self._run(tmp, run_list=lambda bin_: counterless_jobs)
            self.assertEqual(code, 0)
            no_counter_msgs = [
                m for m in self.messages
                if isinstance(m[0], str) and "no error counters" in m[0]
            ]
            self.assertEqual(len(no_counter_msgs), 1)

            self.messages.clear()
            # Second run: prior state is now non-empty, but jobs are still
            # counter-blind -- the warning must fire again, not just once.
            code, state = self._run(tmp, run_list=lambda bin_: counterless_jobs)
            self.assertEqual(code, 0)
            no_counter_msgs = [
                m for m in self.messages
                if isinstance(m[0], str) and "no error counters" in m[0]
            ]
            self.assertEqual(len(no_counter_msgs), 1)
            self.assertEqual(
                no_counter_msgs[0][1]["fingerprint"],
                ["openclaw-cron-bridge", "no-counters"],
            )

    def test_state_write_is_atomic_no_tmp_file_left_behind(self):
        """Confirm the saved state is complete and leaves no temporary file."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            code, state = self._run(tmp, run_list=lambda bin_: FIXTURE["jobs"])
            self.assertEqual(code, 0)
            self.assertTrue(state.exists())
            leftover_tmp = state.with_suffix(state.suffix + ".tmp")
            self.assertFalse(leftover_tmp.exists())
            # State file itself must contain valid, complete JSON.
            json.loads(state.read_text(encoding="utf-8"))



# Envelope shape confirmed against the live Captain host (OpenClaw 2026.7.1):
# `openclaw cron list --json` returns a paginated dict whose jobs carry the
# error counter at state.consecutiveErrors and the message at state.lastError.
# Job names here are generic; only the structure is load-bearing.
REAL_SHAPE_LISTING = {
    "total": 2,
    "limit": 2,
    "offset": 0,
    "hasMore": False,
    "nextOffset": None,
    "jobs": [
        {
            "id": "job-a",
            "name": "Nightly cognify",
            "enabled": True,
            "status": "error",
            "lastRunStatus": "error",
            "lastRunError": "Agent couldn't generate a response.",
            "schedule": {"expr": "30 1 * * *", "kind": "cron", "tz": "America/Detroit"},
            "state": {
                "consecutiveErrors": 46,
                "consecutiveSkipped": 0,
                "lastError": "Agent couldn't generate a response.",
                "lastRunStatus": "error",
                "lastStatus": "error",
            },
        },
        {
            "id": "job-b",
            "name": "Healthy job",
            "enabled": True,
            "status": "ok",
            "lastRunStatus": "ok",
            "schedule": {"expr": "0 9 * * 4", "kind": "cron"},
            "state": {
                "consecutiveErrors": 0,
                "consecutiveSkipped": 0,
                "lastRunStatus": "ok",
                "lastStatus": "ok",
            },
        },
    ],
}


class RealHostShapeTests(unittest.TestCase):
    """The production envelope must parse; a silent mismatch here would mean
    the bridge reports zero cron failures forever."""

    def test_job_view_parses_real_host_shape(self):
        """Confirm the real OpenClaw response shape produces the expected job view."""
        views = [bridge.job_view(j) for j in REAL_SHAPE_LISTING["jobs"]]
        self.assertEqual(views[0]["key"], "job-a")
        self.assertEqual(views[0]["name"], "Nightly cognify")
        self.assertEqual(views[0]["errors"], 46)
        self.assertEqual(views[0]["last_error"], "Agent couldn't generate a response.")
        self.assertEqual(views[1]["errors"], 0)

    def test_no_job_is_counter_blind_on_real_shape(self):
        """Confirm every real-shaped job exposes a readable failure counter."""
        views = [bridge.job_view(j) for j in REAL_SHAPE_LISTING["jobs"]]
        self.assertEqual([v["name"] for v in views if v["errors"] is None], [])


class ExtractJobsTests(unittest.TestCase):
    def test_envelope_not_truncated(self):
        """Confirm a complete job-list response is not marked as cut off."""
        jobs, truncated = bridge.extract_jobs(REAL_SHAPE_LISTING)
        self.assertEqual(len(jobs), 2)
        self.assertFalse(truncated)

    def test_envelope_truncated_when_has_more(self):
        """Confirm OpenClaw's has-more signal marks a response as cut off."""
        listing = dict(REAL_SHAPE_LISTING)
        listing["hasMore"] = True
        jobs, truncated = bridge.extract_jobs(listing)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(truncated)

    def test_bare_list_is_never_truncated(self):
        """Confirm a plain job list is treated as complete."""
        jobs, truncated = bridge.extract_jobs(FIXTURE["jobs"])
        self.assertEqual(len(jobs), len(FIXTURE["jobs"]))
        self.assertFalse(truncated)

    def test_missing_or_empty_payloads(self):
        """Confirm empty job-list responses safely produce no jobs."""
        self.assertEqual(bridge.extract_jobs({}), ([], False))
        self.assertEqual(bridge.extract_jobs([]), ([], False))


class TruncationReportingTests(unittest.TestCase):
    def setUp(self):
        """Start each reporting test with empty captured messages and check-ins."""
        self.messages = []
        self.checkins = []

    def _run_listing(self, listing):
        """Run the monitor once with a supplied job-list response."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.json"
            code = bridge.main(
                ["--state", str(state)],
                run_list=lambda bin_: listing,
                capture_message_fn=lambda message, **kw: self.messages.append(
                    (message, kw)
                ),
                capture_exception_fn=lambda exc, **kw: None,
                checkin_fn=lambda slug, status, monitor_config=None: (
                    self.checkins.append((slug, status))
                ),
                init_fn=lambda component: True,
            )
        return code

    def _truncation_messages(self):
        """Return only captured alerts about an incomplete job listing."""
        return [
            m for m in self.messages
            if m[1].get("fingerprint") == ["openclaw-cron-bridge", "truncated-listing"]
        ]

    def test_truncated_listing_is_reported(self):
        """Confirm an incomplete listing produces one clear warning."""
        listing = dict(REAL_SHAPE_LISTING)
        listing["hasMore"] = True
        self.assertEqual(self._run_listing(listing), 0)
        reported = self._truncation_messages()
        self.assertEqual(len(reported), 1)
        self.assertIn("first page", reported[0][0])
        self.assertEqual(reported[0][1]["extra"]["jobs_seen"], 2)

    def test_untruncated_listing_reports_nothing(self):
        """Confirm a complete listing produces no truncation warning."""
        self.assertEqual(self._run_listing(REAL_SHAPE_LISTING), 0)
        self.assertEqual(self._truncation_messages(), [])



if __name__ == "__main__":
    unittest.main()
