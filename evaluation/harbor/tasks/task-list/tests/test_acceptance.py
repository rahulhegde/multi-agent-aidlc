import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

APP = Path(os.getenv("AIDLC_TASK_APP", "/app"))


def commands(lines):
    result = subprocess.run(
        [sys.executable, str(APP / "todo.py")],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        timeout=5,
        cwd=APP,
    )
    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(output) == len(lines), "Exactly one JSON output per command is required"
    return output


class TaskListAcceptance(unittest.TestCase):
    def test_add_list_complete_and_repeat(self):
        output = commands(
            [
                '{"action":"list"}',
                '{"action":"add","title":" Homework "}',
                '{"action":"complete","id":1}',
                '{"action":"complete","id":1}',
                '{"action":"list"}',
            ]
        )
        self.assertEqual(output[0], [])
        self.assertEqual(output[1], {"id": 1, "title": "Homework", "completed": False})
        self.assertEqual(output[2], {"id": 1, "title": "Homework", "completed": True})
        self.assertEqual(output[3], output[2])
        self.assertEqual(output[4], [output[2]])

    def test_bad_input_does_not_mutate_state_or_consume_ids(self):
        bad = [
            "not json",
            "[]",
            "{}",
            '{"action":"delete"}',
            '{"action":"add","title":" "}',
            '{"action":"add","title":7}',
            '{"action":"complete","id":true}',
            '{"action":"complete","id":0}',
            '{"action":"complete","id":99}',
            '{"action":"complete"}',
            '{"action":"add"}',
        ]
        output = commands(
            [
                '{"action":"add","title":"First"}',
                *bad,
                '{"action":"add","title":"Second"}',
                '{"action":"list"}',
            ]
        )
        for value in output[1 : 1 + len(bad)]:
            self.assertIsInstance(value.get("error"), str)
            self.assertTrue(value["error"].strip())
        self.assertEqual(output[-2]["id"], 2)
        self.assertEqual(output[-1], [output[0], output[-2]])

    def test_state_is_process_local(self):
        commands(['{"action":"add","title":"Ephemeral"}'])
        self.assertEqual(commands(['{"action":"list"}']), [[]])

    def test_source_uses_only_standard_library_and_no_dynamic_execution(self):
        tree = ast.parse((APP / "todo.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            if isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or "").split(".")[0], sys.stdlib_module_names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"eval", "exec", "__import__"})


if __name__ == "__main__":
    unittest.main()
