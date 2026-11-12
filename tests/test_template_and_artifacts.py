from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.game_template import render_template
from app.agents.engineer import check_js_syntax, validate_game_code
from app.artifacts import register_artifact, task_directory, contained_file


class TemplateTests(unittest.TestCase):
    def test_template_is_safe_and_complete(self) -> None:
        html = render_template(
            title='<img src=x onerror=alert(1)>',
            target=5,
            lose_limit=2,
            win_text="你赢了</script>",
            lose_text="再来一次",
        )
        self.assertNotIn("__CONFIG_JSON__", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("你赢了</script>", html)
        self.assertIn("&lt;img", html)
        self.assertIn('id="start-button"', html)
        self.assertIn("ArrowLeft", html)
        self.assertIn("playerX", html)

    def test_template_passes_js_syntax_check(self) -> None:
        html = render_template(
            title="接金币", target=10, lose_limit=3,
            win_text="你赢了！", lose_text="游戏结束",
        )
        self.assertEqual(check_js_syntax(html), [])


class GeneratedCodeGateTests(unittest.TestCase):
    """生成代码必须在写盘前拦住语法错误，否则会白烧一轮修复预算。"""

    BROKEN = (
        "<!doctype html><html><body><canvas id='game'></canvas>"
        "<script>let miss = 0; let miss = 1; window.__game={start(){},action(){},state(){}};"
        "</script></body></html>"
    )

    def test_duplicate_declaration_is_detected(self) -> None:
        problems = check_js_syntax(self.BROKEN)
        if not problems:
            self.skipTest("本机没有 Node，--check 不可用")
        self.assertIn("SyntaxError", problems[0])
        self.assertIn("already been declared", problems[0])

    def test_contract_problems_are_reported(self) -> None:
        problems = validate_game_code("<html><body>hi</body></html>", [])
        self.assertTrue(any("canvas" in p for p in problems))
        self.assertTrue(any("__game" in p for p in problems))


class ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch("app.artifacts.GENERATED_DIR", Path(self.temp.name))
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp.cleanup()

    def test_manifest_has_hash_and_version(self) -> None:
        task_dir = task_directory("task-abcdef12")
        game = task_dir / "game.html"
        game.write_text("<html></html>", encoding="utf-8")
        record = register_artifact(
            "task-abcdef12", game, "game_html", "engineer_agent", version=2
        )
        self.assertEqual(record["version"], 2)
        self.assertEqual(len(record["sha256"]), 64)
        self.assertTrue((task_dir / "manifest.json").exists())

    def test_rejects_path_traversal(self) -> None:
        task_dir = task_directory("task-abcdef12")
        outside = Path(self.temp.name) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        with self.assertRaises(ValueError):
            contained_file(task_dir, outside)
        with self.assertRaises(ValueError):
            task_directory("../task-abcdef12")


if __name__ == "__main__":
    unittest.main()
