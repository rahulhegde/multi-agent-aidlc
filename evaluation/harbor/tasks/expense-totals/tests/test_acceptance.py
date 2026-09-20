import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

APP = Path(os.getenv("AIDLC_TASK_APP", "/app"))


def requests(values):
    result = subprocess.run(
        [sys.executable, str(APP / "expenses.py")],
        input="\n".join(json.dumps(value) for value in values) + "\n",
        text=True,
        capture_output=True,
        timeout=5,
        cwd=APP,
    )
    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(output) == len(values), "Exactly one JSON output per request is required"
    return output


class ExpenseAcceptance(unittest.TestCase):
    def test_exact_totals_trimmed_categories_and_empty_list(self):
        output = requests(
            [
                {
                    "expenses": [
                        {"category": " Food ", "amount": "0.10"},
                        {"category": "Food", "amount": "0.20"},
                        {"category": "Travel", "amount": "2"},
                    ]
                },
                {"expenses": []},
            ]
        )
        self.assertEqual(
            output[0], {"total": "2.30", "by_category": {"Food": "0.30", "Travel": "2.00"}}
        )
        self.assertEqual(output[1], {"total": "0.00", "by_category": {}})

    def test_invalid_fields_and_amounts_do_not_publish_partial_totals(self):
        values = [
            [],
            {},
            {"expenses": {}},
            {"expenses": [7]},
            {"expenses": [{"category": "", "amount": "1"}]},
            {"expenses": [{"category": 1, "amount": "1"}]},
            {"expenses": [{"amount": "1"}]},
            {"expenses": [{"category": "Food"}]},
        ]
        for amount in [1, True, "NaN", "Infinity", "-1", "+1", "1e2", "1.234", "1.", ""]:
            values.append(
                {
                    "expenses": [
                        {"category": "Food", "amount": "1"},
                        {"category": "Food", "amount": amount},
                    ]
                }
            )
        for output in requests(values):
            self.assertEqual(set(output), {"error"})
            self.assertIsInstance(output["error"], str)
            self.assertTrue(output["error"].strip())

    def test_malformed_json_recovers_and_requests_are_independent(self):
        result = subprocess.run(
            [sys.executable, str(APP / "expenses.py")],
            input='not json\n{"expenses":[]}\n',
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        output = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(output[0]["error"])
        self.assertEqual(output[1], {"total": "0.00", "by_category": {}})
        self.assertEqual(requests([{"expenses": []}])[0], output[1])

    def test_source_uses_only_standard_library_and_no_dynamic_execution(self):
        tree = ast.parse((APP / "expenses.py").read_text())
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
