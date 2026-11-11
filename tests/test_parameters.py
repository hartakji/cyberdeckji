import json
from pathlib import Path
import tempfile
import unittest

from deckfw import Choice, Form, Number, Text, Toggle
from deckfw.parameters import load_form, parameter_env


class ParameterTests(unittest.TestCase):
    def test_typed_defaults_validation_and_fresh_values(self):
        form = Form([
            Text("host", "Host", default="::1", format="host"),
            Choice("mode", "Mode", choices=[("One", 1), ("Two", 2)], default=1),
            Toggle("enabled", "Enabled"),
            Number("count", "Count", default=3, minimum=1, maximum=9),
        ])
        values = form.defaults()
        self.assertEqual(form.validate(values),
                         {"host": "::1", "mode": 1, "enabled": False, "count": 3})
        values["host"] = "changed"
        self.assertEqual(form.defaults()["host"], "::1")
        for field in form.fields:
            with self.subTest(field=field.key):
                with self.assertRaises(ValueError):
                    field.validate(None)

    def test_host_formats(self):
        host = Text("target", "Target", format="host")
        for value in ("192.168.1.1", "::1", "host", "example.com.", "a-b.local"):
            host.validate(value)
        for value in ("-x", "999.1.1.1", "1.2.3.4-255", "a..b", "x/y", "a b", "a;cmd"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                host.validate(value)
        with self.assertRaises(ValueError):
            Text("target", "Target", format="ip").validate("hostname")
        Text("target", "Target", required=False, format="ip").validate("")

    def test_text_validation_and_custom_validator(self):
        field = Text("name", "Name", default="abc", max_length=4,
                     validator=lambda value: "No digits" if any(c.isdigit() for c in value) else None)
        for value in ("", "abcde", "a\0", "a\n", "a\x7f", "a1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                field.validate(value)
        self.assertEqual(field.validate("word"), "word")

    def test_invalid_declarations_and_defaults(self):
        factories = [
            lambda: Text("bad-key", "Label"),
            lambda: Text("x", "", default="ok"),
            lambda: Text("x", "X", format="mystery"),
            lambda: Text("x", "X", max_length=0),
            lambda: Choice("x", "X", choices=[], default="a"),
            lambda: Choice("x", "X", choices=["abc"], default="a"),
            lambda: Choice("x", "X", choices=[("A", [])], default=[]),
            lambda: Choice("x", "X", choices=[("A", 1), ("B", 1)], default=1),
            lambda: Number("x", "X", default=1, minimum=10, maximum=1),
            lambda: Number("x", "X", default=1, minimum=0, maximum=10, step=0),
            lambda: Number("x", "X", default=1, minimum=0, maximum=10, step=3),
            lambda: Number("x", "X", default=float("nan"), minimum=0, maximum=10),
            lambda: Form([Toggle("a", "A"), Toggle("A", "B")]),
            lambda: Form([Text("x", "X")]),
            lambda: Form([Toggle("x", "X", default="yes")]),
            lambda: Form([Number("x", "X", default=11, minimum=0, maximum=10)]),
            lambda: Form([Choice("x", "X", [("A", "a")], default="b")]),
            lambda: Form(["not a field"]),
        ]
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_decimal_steps_and_numeric_bounds(self):
        field = Number("delay", "Delay", default=0.1, minimum=0.0, maximum=1.0, step=0.1)
        value = field.default
        for _ in range(2):
            value = field.adjust(value, 1)
        self.assertEqual(value, 0.3)
        field.validate(value)
        self.assertEqual(field.adjust(1.0, 1), 1.0)
        self.assertEqual(field.adjust(0.0, -1), 0.0)
        for value in (0.25, -0.1, 1.1, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                field.validate(value)
        integer = Number("count", "Count", 1, minimum=0, maximum=2)
        self.assertIs(type(integer.adjust(1, 1)), int)
        with self.assertRaises(ValueError):
            integer.validate(1.0)

    def test_choice_display_wrap_and_toggle(self):
        choice = Choice("x", "X", [("First", 1), ("Second", 2)], default=1)
        self.assertEqual(choice.display(2), "Second")
        self.assertEqual(choice.adjust(1, -1), 2)
        self.assertEqual(choice.adjust(2, 1), 1)
        with self.assertRaises(ValueError):
            choice.validate(True)
        self.assertTrue(Toggle("x", "X").adjust(False, -1))

    def test_json_and_environment_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "script.py.params.json"
            definition = {"fields": [
                {"type": "text", "key": "message", "label": "Message", "default": "a ' $; b"},
                {"type": "toggle", "key": "verbose", "label": "Verbose", "default": True},
                {"type": "number", "key": "count", "label": "Count",
                 "default": 2, "minimum": 1, "maximum": 3},
                {"type": "choice", "key": "mode", "label": "Mode",
                 "choices": [["Fast", "fast"]], "default": "fast"},
            ]}
            path.write_text(json.dumps(definition))
            values = load_form(path).defaults()
            self.assertEqual(parameter_env(values), {
                "DECK_PARAM_MESSAGE": "a ' $; b", "DECK_PARAM_VERBOSE": "true",
                "DECK_PARAM_COUNT": "2", "DECK_PARAM_MODE": "fast",
            })
            for bad in ([], {}, {"fields": {}}, {"fields": [{"type": "oops"}]},
                        {"fields": [], "typo": 1},
                        {"fields": [{"type": "text", "key": "a", "label": "A",
                                     "validator": "ignored"}]}):
                path.write_text(json.dumps(bad))
                with self.subTest(definition=bad), self.assertRaises(ValueError):
                    load_form(path)


if __name__ == "__main__":
    unittest.main()
