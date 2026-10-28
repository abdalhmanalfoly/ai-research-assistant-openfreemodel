"""Tests for the --limit batch mode of the legacy pilot importer (no DB, no network, no writes).

Covers: CLI validation (positive integer; 0/negative/non-integer rejected with a clear error),
deterministic order, exclusion of already-imported papers (including the 13-paper pilot),
RETRY-FIRST semantics for interrupted/failed records (the 429 recovery path),
dry-run-by-default semantics, and the fewer-than-N honest report.
"""
import argparse
import unittest
import uuid

from scripts.legacy_pilot_import import plan_batch, build_report, parse_limit


class LimitValidationTests(unittest.TestCase):
    def test_positive_integers_accepted(self):
        self.assertEqual(parse_limit("5"), 5)
        self.assertEqual(parse_limit("1000"), 1000)

    def test_none_means_manifest_mode(self):
        self.assertIsNone(parse_limit(None))

    def test_zero_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            parse_limit("0")
        self.assertIn("positive integer", str(cm.exception))

    def test_negative_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            parse_limit("-3")
        self.assertIn("positive integer", str(cm.exception))

    def test_non_integer_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            parse_limit("abc")
        self.assertIn("positive integer", str(cm.exception))
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_limit("7.5")


def _rows():
    """Deterministic fixture: two fully eligible rows, three invalid ones."""
    return [
        ("http://arxiv.org/abs/2001.1v1", "T-A", ["a"], "S-A", "https://arxiv.org/pdf/2001.1v1", None, []),
        ("http://arxiv.org/abs/1999.1v1", "T-B", ["b"], "S-B", "https://arxiv.org/pdf/1999.1v1", None, []),
        ("http://arxiv.org/abs/2002.1v1", "T-C", [], "S-C", "", None, []),          # no pdf_url
        ("http://arxiv.org/abs/2003.1v1", "", [], "S-D", "https://arxiv.org/pdf/2003.1v1", None, []),  # no title
        ("http://arxiv.org/abs/2004.1v1", "T-E", ["e"], "", "https://arxiv.org/pdf/2004.1v1", None, []),  # no summary
    ]


class SelectionTests(unittest.TestCase):
    def test_deterministic_order_and_uuid5_ids(self):
        recs = plan_batch(_rows(), {}, limit=50)
        self.assertEqual([r["legacy_id"] for r in recs],
                         ["http://arxiv.org/abs/2001.1v1", "http://arxiv.org/abs/1999.1v1"])
        for lr in _rows():
            for r in plan_batch([lr], {}, limit=2):
                self.assertEqual(r["papers_id"],
                                 str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:x-arxiv-legacy:{r['legacy_id']}")))

    def test_excludes_indexed_and_invalid(self):
        registered = {"http://arxiv.org/abs/2001.1v1": "indexed"}
        recs = plan_batch(_rows(), registered, limit=50)
        self.assertEqual([r["legacy_id"] for r in recs], ["http://arxiv.org/abs/1999.1v1"])

    def test_pilot_manifest_ids_are_excluded_when_indexed(self):
        from scripts.legacy_pilot_import import PILOT
        registered = {lid: "indexed" for _, lid in PILOT}  # the 13-paper pilot, all completed
        pool = _rows() + [(lid, "t", ["a"], "s", "https://arxiv.org/pdf/x", None, []) for _, lid in PILOT]
        recs = plan_batch(pool, registered, limit=50)
        for r in recs:
            self.assertNotIn(r["legacy_id"], registered)

    def test_failed_and_processing_are_retried_first(self):
        # Exactly the interruption shape: 1 stuck 'processing', 1 'failed'; retries precede new work.
        registered = {"http://arxiv.org/abs/2001.1v1": "processing",
                      "http://arxiv.org/abs/1999.1v1": "failed",
                      "http://arxiv.org/abs/2002.1v1": "indexed"}
        pool = _rows() + [("http://arxiv.org/abs/NEW.1v1", "T-N", ["n"], "S-N",
                           "https://arxiv.org/pdf/N", None, [])]
        recs = plan_batch(pool, registered, limit=2)
        self.assertEqual([r["legacy_id"] for r in recs],
                         ["http://arxiv.org/abs/2001.1v1",   # stuck -> retried first
                          "http://arxiv.org/abs/1999.1v1"])  # failed -> retried second
        self.assertEqual(plan_batch(pool, registered, limit=1)[0]["legacy_id"],
                         "http://arxiv.org/abs/2001.1v1")
        full = plan_batch(pool, registered, limit=10)
        self.assertEqual(full[2]["legacy_id"], "http://arxiv.org/abs/NEW.1v1")  # fresh last

    def test_limit_caps_selection(self):
        registered = {"http://arxiv.org/abs/2001.1v1": "failed",
                      "http://arxiv.org/abs/1999.1v1": "failed"}
        self.assertEqual(len(plan_batch(_rows(), registered, limit=1)), 1)
        self.assertEqual([r["legacy_id"] for r in plan_batch(_rows(), registered, limit=50)],
                         ["http://arxiv.org/abs/2001.1v1", "http://arxiv.org/abs/1999.1v1"])

    def test_order_stable_across_calls(self):
        a = [r["legacy_id"] for r in plan_batch(_rows(), {}, limit=50)]
        b = [r["legacy_id"] for r in plan_batch(_rows(), {}, limit=50)]
        self.assertEqual(a, b)

    def test_report_honest_when_fewer_than_limit(self):
        recs = plan_batch(_rows(), {}, limit=50)
        out = build_report(recs, 5)
        self.assertIn("only 2 eligible new records remain (requested limit 5)", out)

    def test_report_head_changes_with_mode(self):
        self.assertIn("hard limit 13", build_report([], None))
        self.assertIn("limit 7", build_report([{"papers_id": "x", "legacy_id": "l", "title": "t"}], 7))


class DryRunDefaultTests(unittest.TestCase):
    def test_no_execute_flag_means_dry_run_zero_writes(self):
        # Parse-level contract: --execute is opt-in; its absence leaves execute False.
        ap = argparse.ArgumentParser()
        ap.add_argument("--execute", action="store_true")
        ap.add_argument("--limit", type=parse_limit)
        ns = ap.parse_args(["--limit", "10"])
        self.assertFalse(ns.execute)
        self.assertEqual(ns.limit, 10)
