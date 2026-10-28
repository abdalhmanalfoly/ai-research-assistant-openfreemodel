"""Migration-chain integrity: the reconstructed Alembic history must be complete, correctly linked, and
idempotency-guarded — WITHOUT executing anything against a database (source-level inspection only)."""
import ast
import os
import unittest

VERSIONS = os.path.join(os.path.dirname(__file__), "..", "..", "migrations", "versions")


class ChainIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files = sorted(f for f in os.listdir(VERSIONS) if f.endswith(".py") and not f.startswith("__"))
        cls.revisions = {}
        for f in files:
            with open(os.path.join(VERSIONS, f), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            rev = down = None
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    t = node.targets[0]
                    if isinstance(t, ast.Name) and t.id == "revision" and isinstance(node.value, ast.Constant):
                        rev = node.value.value
                    if isinstance(t, ast.Name) and t.id == "down_revision" and isinstance(node.value, ast.Constant):
                        down = node.value.value
                    if isinstance(t, ast.Name) and t.id == "down_revision" and isinstance(node.value, ast.Name):
                        down = node.value.id  # e.g. down_revision = None
            cls.revisions[rev] = (down, f, ast.get_source_segment or tree)

    def test_chain_is_complete_0001_to_0009(self):
        self.assertEqual(set(self.revisions),
                         {"0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"})

    def test_every_link_points_to_its_real_predecessor(self):
        chain = {"0002": "0001", "0003": "0002", "0004": "0003", "0005": "0004",
                 "0006": "0005", "0007": "0006", "0008": "0007", "0009": "0008"}
        for rev, expected_down in chain.items():
            self.assertEqual(self.revisions[rev][0], expected_down,
                             f"{rev} must chain to {expected_down} (never bypass a revision by relinking)")

    def test_exactly_one_head(self):
        heads = {rev for rev in self.revisions
                 if rev not in {down for down, _, _ in self.revisions.values() if down}}
        self.assertEqual(heads, {"0009"})

    def test_0006_and_0007_are_explicit_reconciliations(self):
        for f in ("0006_ownership_schema.py", "0007_aux_tables.py"):
            src = open(os.path.join(VERSIONS, f), encoding="utf-8").read()
            self.assertIn("RECONCILIATION", src, f"{f} must state what it is, not masquerade as recovered history")

    def test_revisions_are_idempotency_guarded_and_fail_safe(self):
        src6 = open(os.path.join(VERSIONS, "0006_ownership_schema.py"), encoding="utf-8").read()
        src7 = open(os.path.join(VERSIONS, "0007_aux_tables.py"), encoding="utf-8").read()
        src8 = open(os.path.join(VERSIONS, "0008_nemotron_2048.py"), encoding="utf-8").read()
        # every schema-changing step in 0006/0007 must check the catalog first
        self.assertIn("_has_table", src6)
        self.assertIn("_has_constraint", src6)
        self.assertIn("_has_table", src7)
        self.assertIn("diverges from the audited live schema", src7)   # refuse unexpected shapes, never "repair"
        self.assertIn("diverges from the audited live schema", src6)
        # 0008 must refuse the anomalous double-preservation state instead of overwriting a generation
        self.assertNotRegex(src8, r'else:\s*\n[^#]*.Override', msg="no silent overwrite branch")
        self.assertIn("BOTH preservation slots are occupied", src8)
        self.assertIn("already records branch", src8)  # field-record clobber guard

    def test_0008_never_touches_user_data_and_keeps_texts(self):
        src8 = open(os.path.join(VERSIONS, "0008_nemotron_2048.py"), encoding="utf-8").read()
        self.assertNotIn("DROP TABLE chunks", src8)
        self.assertNotIn("TRUNCATE", src8)
        self.assertNotIn("DELETE FROM chunks WHERE embedding", src8)  # vectors preserved via rename/copy, not deleted
