import argparse
import json
from pathlib import Path

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from .base import Validator
from .models import CheckError, CheckResult


class TriggersJSONValidator(Validator):
    @classmethod
    def validate(cls, result: CheckResult, args: argparse.Namespace) -> None:
        if not result.options.get("path"):
            return

        module_dir: Path = result.options["path"]
        triggers_jsons = module_dir.glob("trigger_*.json")

        result.options["triggers"] = {}
        for path in triggers_jsons:
            cls.validate_trigger_json(path, result, args)

    @classmethod
    def validate_trigger_json(
        cls, path: Path, result: CheckResult, args: argparse.Namespace
    ):
        try:
            with open(path, "rt") as file:
                raw = json.load(file)

        except ValueError:
            result.errors.append(CheckError(filepath=path, error=f"can't load JSON"))
            return

        # @todo test for unique identifier
        if not isinstance(raw.get("name"), str):
            result.errors.append(
                CheckError(filepath=path, error=f"`name` is not present")
            )

        if not isinstance(raw.get("uuid"), str):
            # @todo possible to fix: generate uuid
            result.errors.append(
                CheckError(filepath=path, error=f"`uuid` is not present")
            )
        else:
            if "uuid_to_check" not in result.options:
                result.options["uuid_to_check"] = {}
            if path.name not in result.options["uuid_to_check"]:
                result.options["uuid_to_check"][path.name] = {}
            result.options["uuid_to_check"][path.name] = raw.get("uuid")

        if not isinstance(raw.get("description"), str):
            result.errors.append(
                CheckError(filepath=path, error=f"`description` is not present")
            )

        # @todo track this to main.py ?
        # @todo check that `docker_parameters` is unique within the module
        if not isinstance(raw.get("docker_parameters"), str):
            result.errors.append(
                CheckError(filepath=path, error=f"`docker_parameters` is not present")
            )
        else:
            if path.stem not in result.options["triggers"]:
                result.options["triggers"][path.stem] = {}

            result.options["triggers"][path.stem]["docker_parameters"] = raw[
                "docker_parameters"
            ]

        if not isinstance(raw.get("arguments"), dict):
            result.errors.append(
                CheckError(filepath=path, error=f"`arguments` is not present")
            )

        elif not cls.is_valid_json_schema(raw["arguments"]):
            result.errors.append(
                CheckError(filepath=path, error=f"`arguments` is not valid JSON schema")
            )

        if not isinstance(raw.get("results"), dict):
            result.errors.append(
                CheckError(filepath=path, error=f"`results` is not present")
            )

        elif not cls.is_valid_json_schema(raw["results"]):
            result.errors.append(
                CheckError(filepath=path, error=f"`results` is not valid JSON schema")
            )

    @staticmethod
    def is_valid_json_schema(schema: dict) -> bool:
        try:
            Draft7Validator.check_schema(schema)
            return True
        except SchemaError as e:
            return False
