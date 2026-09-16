import tempfile
import unittest
from pathlib import Path

from orchestral.config import ConfigError, find_task, load_models, load_task


class LoadTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        p = self.root / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_valid_spec_loads(self) -> None:
        p = self._write("ok.yaml", "id: ok\ntype: constraint\nprompt: hi\n")
        self.assertEqual(load_task(p).id, "ok")

    def test_unknown_type_fails_closed(self) -> None:
        p = self._write("bad.yaml", "id: bad\ntype: htm\nprompt: hi\n")
        with self.assertRaises(ConfigError) as ctx:
            load_task(p)
        self.assertIn("htm", str(ctx.exception))
        self.assertIn("html", str(ctx.exception))

    def test_missing_required_field_names_file_and_field(self) -> None:
        p = self._write("noprompt.yaml", "id: x\ntype: html\n")
        with self.assertRaises(ConfigError) as ctx:
            load_task(p)
        msg = str(ctx.exception)
        self.assertIn("noprompt.yaml", msg)
        self.assertIn("prompt", msg)

    def test_malformed_yaml_names_file(self) -> None:
        p = self._write("broken.yaml", "not yaml: [unclosed")
        with self.assertRaises(ConfigError) as ctx:
            load_task(p)
        self.assertIn("broken.yaml", str(ctx.exception))

    def test_non_mapping_spec_is_rejected(self) -> None:
        p = self._write("list.yaml", "- just\n- a\n- list\n")
        with self.assertRaises(ConfigError):
            load_task(p)

    def test_find_task_raises_on_malformed_file(self) -> None:
        self._write("broken.yaml", "id: [unclosed")
        with self.assertRaises(ConfigError):
            find_task("anything", self.root)

    def test_find_task_skips_non_mapping_files(self) -> None:
        self._write("notes.yaml", "- notes\n- not a task\n")
        self.assertIsNone(find_task("anything", self.root))


class LoadModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bad_entry_names_file(self) -> None:
        (self.root / "m.yaml").write_text(
            "models:\n  - slug: x\n    name: X\n", encoding="utf-8",
        )
        with self.assertRaises(ConfigError) as ctx:
            load_models(self.root)
        self.assertIn("m.yaml", str(ctx.exception))

    def test_malformed_yaml_names_file(self) -> None:
        (self.root / "m.yaml").write_text("models: [unclosed", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_models(self.root)
        self.assertIn("m.yaml", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
