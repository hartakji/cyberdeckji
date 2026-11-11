"""Declarative, validated pre-launch parameters shared by apps and scripts."""

import copy
from decimal import Decimal
import ipaddress
import json
import math
import re


class Field:
    def __init__(self, key, label, default, help="", validator=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Parameter keys must be identifiers")
        if not isinstance(label, str) or not label:
            raise ValueError(f"{key}: label must be nonempty text")
        if not isinstance(help, str) or (validator is not None and not callable(validator)):
            raise ValueError(f"{key}: invalid help or validator")
        self.key, self.label, self.default = key, label, default
        self.help, self.validator = help, validator

    def validate(self, value):
        self.check(value)
        if self.validator:
            error = self.validator(value)
            if error is not None:
                raise ValueError(str(error))
        return value

    def check(self, value):
        raise NotImplementedError

    def display(self, value):
        return str(value)

    def adjust(self, value, direction):
        return value


class Text(Field):
    def __init__(self, key, label, default="", *, required=True, max_length=256,
                 format="text", help="", validator=None):
        super().__init__(key, label, default, help, validator)
        if format not in ("text", "host", "ip"):
            raise ValueError(f"{key}: unknown text format")
        if type(required) is not bool or type(max_length) is not int or max_length < 1:
            raise ValueError(f"{key}: invalid text limits")
        self.required, self.max_length, self.format = required, max_length, format

    def check(self, value):
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Enter text without control characters")
        if len(value) > self.max_length:
            raise ValueError(f"Maximum {self.max_length} characters")
        if not value:
            if self.required:
                raise ValueError("A value is required")
            return
        if self.format == "text":
            return
        try:
            ipaddress.ip_address(value)
            return
        except ValueError:
            if self.format == "ip":
                raise ValueError("Enter a valid IPv4 or IPv6 address") from None
        hostname = value.rstrip(".")
        if (len(hostname) > 253 or re.fullmatch(r"[0-9.]+(?:-[0-9]+)?", hostname)
                or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                                        part) for part in hostname.split("."))):
            raise ValueError("Enter a valid IP address or hostname")


class Choice(Field):
    def __init__(self, key, label, choices, default, *, help="", validator=None):
        super().__init__(key, label, default, help, validator)
        if not choices:
            raise ValueError(f"{key}: at least one choice is required")
        self.choices = []
        for pair in choices:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(f"{key}: choices must be (label, value) pairs")
            name, value = pair
            if not isinstance(name, str) or not name:
                raise ValueError(f"{key}: choice labels must be text")
            serialize(value)
            if any(type(value) is type(v) and value == v for _, v in self.choices):
                raise ValueError(f"{key}: duplicate choice value")
            self.choices.append((name, value))

    def _index(self, value):
        for index, (_, candidate) in enumerate(self.choices):
            if type(candidate) is type(value) and candidate == value:
                return index
        raise ValueError("Choose one of the listed values")

    def check(self, value):
        self._index(value)

    def display(self, value):
        return self.choices[self._index(value)][0]

    def adjust(self, value, direction):
        return self.choices[(self._index(value) + direction) % len(self.choices)][1]


class Toggle(Field):
    def __init__(self, key, label, default=False, *, help="", validator=None):
        super().__init__(key, label, default, help, validator)

    def check(self, value):
        if type(value) is not bool:
            raise ValueError("Expected yes or no")

    def display(self, value):
        return "Yes" if value else "No"

    def adjust(self, value, direction):
        return not value


class Number(Field):
    def __init__(self, key, label, default, *, minimum, maximum, step=1,
                 help="", validator=None):
        super().__init__(key, label, default, help, validator)
        for value in (default, minimum, maximum, step):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{key}: numbers must be finite")
        if minimum > maximum or step <= 0:
            raise ValueError(f"{key}: invalid numeric range or step")
        self.minimum, self.maximum, self.step = minimum, maximum, step
        self.integer = all(type(v) is int for v in (default, minimum, maximum, step))
        if (Decimal(str(maximum)) - Decimal(str(minimum))) % Decimal(str(step)):
            raise ValueError(f"{key}: maximum must align with step")

    def check(self, value):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (self.integer and type(value) is not int)):
            raise ValueError("Enter a finite number of the declared type")
        if not self.minimum <= value <= self.maximum:
            raise ValueError(f"Must be between {self.minimum} and {self.maximum}")
        if (Decimal(str(value)) - Decimal(str(self.minimum))) % Decimal(str(self.step)):
            raise ValueError(f"Must advance by {self.step} from {self.minimum}")

    def adjust(self, value, direction):
        result = Decimal(str(value)) + direction * Decimal(str(self.step))
        result = min(Decimal(str(self.maximum)), max(Decimal(str(self.minimum)), result))
        return int(result) if self.integer else float(result)


class Form:
    def __init__(self, fields, submit_label="Run"):
        self.fields = list(fields)
        if not isinstance(submit_label, str) or not submit_label:
            raise ValueError("Submit label must be nonempty text")
        self.submit_label = submit_label
        keys = set()
        for field in self.fields:
            if not isinstance(field, Field):
                raise ValueError("Form entries must be parameter fields")
            if field.key.upper() in keys:
                raise ValueError(f"Duplicate parameter key: {field.key}")
            keys.add(field.key.upper())
            try:
                field.validate(field.default)
            except ValueError as exc:
                raise ValueError(f"{field.key}: invalid default: {exc}") from exc

    def defaults(self):
        return {field.key: copy.deepcopy(field.default) for field in self.fields}

    def validate(self, values):
        if set(values) != {field.key for field in self.fields}:
            raise ValueError("Parameter keys do not match the form")
        return {field.key: copy.deepcopy(field.validate(values[field.key]))
                for field in self.fields}


def load_form(path):
    with open(path, encoding="utf-8") as stream:
        definition = json.load(stream)
    if not isinstance(definition, dict) or set(definition) - {"fields", "submit_label"}:
        raise ValueError("Expected a form with fields and optional submit_label")
    if not isinstance(definition.get("fields"), list):
        raise ValueError("Form fields must be a list")
    types = {"text": Text, "choice": Choice, "toggle": Toggle, "number": Number}
    fields = []
    for entry in definition["fields"]:
        if not isinstance(entry, dict):
            raise ValueError("Each field must be an object")
        options = dict(entry)
        kind = options.pop("type", None)
        if kind not in types:
            raise ValueError(f"Unknown field type: {kind}")
        if "validator" in options:
            raise ValueError("JSON fields cannot declare Python validators")
        fields.append(types[kind](**options))
    return Form(fields, definition.get("submit_label", "Run"))


def serialize(value):
    if isinstance(value, str) and "\0" not in value:
        return value
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float) and math.isfinite(value):
        return str(value)
    raise ValueError("Parameters must be strings without NUL, booleans or finite numbers")


def parameter_env(values):
    return {"DECK_PARAM_" + key.upper(): serialize(value) for key, value in values.items()}
