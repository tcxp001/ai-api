"""Execute the Provider model editor helpers without a browser or live services."""

import json
import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelRowParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.context = None
        self.options = []
        self.selected = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "input" and "data-model-context" in attrs:
            self.context = attrs.get("value", "")
        elif tag == "option":
            self.options.append(attrs.get("value", ""))
            if "selected" in attrs:
                self.selected = attrs.get("value", "")


@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute editor JavaScript")
class DashboardModelEditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        source = [re.search(r"^    const esc = .+;$", html, re.MULTILINE).group()]
        for name in ("modelsToRows", "modelRowHtml", "rowsToModels"):
            source.append(re.search(
                rf"^    function {name}\(.*?^    \}}",
                html,
                re.MULTILINE | re.DOTALL,
            ).group())
        cls.source = "\n".join(source)

    def js(self, expression, setup=""):
        result = subprocess.run(
            ["node", "-e", self.source + "\n" + setup
             + "\nprocess.stdout.write(JSON.stringify(" + expression + "));"],
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        return json.loads(result.stdout)

    def row(self, **values):
        return ModelRowParser(self.js(f"modelRowHtml({json.dumps(values)}, 0)"))

    def test_blank_row_uses_explicit_defaults(self):
        row = self.row(name="m", context="", effort="")
        self.assertEqual(row.context, "100000")
        self.assertEqual(row.selected, "medium")

    def test_effort_options_remove_default_and_have_requested_order(self):
        row = self.row(name="m")
        self.assertEqual(
            row.options,
            ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
        )
        self.assertNotIn("", row.options)

    def test_unconfigured_model_formats_use_defaults(self):
        for models in ("m", ["m"], {"m": {}}):
            with self.subTest(models=models):
                html = self.js(
                    f"modelsToRows({json.dumps(models)}).map((row, i) => modelRowHtml(row, i))"
                )[0]
                row = ModelRowParser(html)
                self.assertEqual(row.context, "100000")
                self.assertEqual(row.selected, "medium")

    def test_existing_context_and_effort_are_preserved(self):
        for effort in ("none", "high", "max", "ultra"):
            with self.subTest(effort=effort):
                models = {"m": {"context_length": 262144, "reasoning_effort": effort}}
                html = self.js(
                    f"modelRowHtml(modelsToRows({json.dumps(models)})[0], 0)"
                )
                row = ModelRowParser(html)
                self.assertEqual(row.context, "262144")
                self.assertEqual(row.selected, effort)

    def test_default_none_and_ultra_save_with_unrelated_metadata_preserved(self):
        for supplied, expected in (("", "medium"), ("none", "none"), ("ultra", "ultra")):
            with self.subTest(effort=supplied):
                row = self.row(name="m", context="", effort=supplied)
                values = {
                    "[data-model-name]": "m",
                    "[data-model-context]": row.context,
                    "[data-model-effort]": row.selected,
                }
                setup = """
const state = {selectedProvider: 0, providers: [{models: {
  m: {pricing: {input: 1.5}, context_length: 262144, reasoning_effort: 'high'}
}}]};
const values = %s;
const document = {querySelectorAll: () => [{
  dataset: {modelOriginalName: 'm'},
  querySelector: selector => ({value: values[selector]})
}]};
""" % json.dumps(values)
                self.assertEqual(self.js("rowsToModels()", setup), {
                    "m": {
                        "context_length": 100000,
                        "reasoning_effort": expected,
                        "pricing": {"input": 1.5},
                    },
                })


class DashboardModelEditorSaveTest(unittest.TestCase):
    def test_backend_preserves_ultra_and_context(self):
        sys.path.insert(0, str(ROOT))
        import dashboard

        models = {"m": {"context_length": 100000, "reasoning_effort": "ultra"}}
        provider = dashboard.validate_provider({
            "name": "test", "base_url": "https://example.invalid/v1", "models": models,
        }, 1)
        self.assertEqual(dashboard.compact_provider(provider)["models"], models)


if __name__ == "__main__":
    unittest.main()
