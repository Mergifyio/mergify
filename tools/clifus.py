#!/usr/bin/env python3

import argparse
import os
import pathlib
import re
import shlex
import subprocess
import sys
import typing

import yaml


def print_to_err_and_exit(msg) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def file_type(filepath: str) -> str:
    path = os.path.expanduser(filepath)
    if not os.path.isfile(path):
        raise argparse.ArgumentTypeError(
            f"The specified file path '{filepath}' is invalid"
        )
    return path


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clifus",  # CLI File Update Strategy
        description="Update files based on rules specified in a configuration file",
    )

    parser.add_argument(
        "-c",
        "--config",
        action="store",
        type=file_type,
        help="Location of the YAML configuration file",
        default=".clifus.yml",
    )

    return parser


def evaluate_source(source_id: str, source_config: dict[str, typing.Any]) -> str:
    print(f"Evaluating source {source_id}")

    if source_config["kind"] == "shell":
        command = source_config["spec"]["command"]
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use.dangerous-subprocess-use
        value = subprocess.check_output(
            shlex.split(command),
            encoding="utf-8",
        )
        value = value.strip()
        print(f"Value of source '{source_id}': {value}")
        return value


def get_replacer_value_with_transfomers(
    source_value: typing.Any, transformers: list[dict[str, typing.Any]]
) -> str:
    """
    Go through all the `transformers` indexes of the target to transform the value
    we will write to the file
    - find: Regex to modify the `source_value`.
            All the other transformers will be applied on this modified value.
    - replacer{from, to}: Look for the `from` string in the value and replace it with the `to` string
    - addPrefix: Add the string as a prefix to the value
    - addSuffix: Add the string as a suffix to the value
    """
    for transformer_dict in transformers:
        if "find" in transformer_dict:
            s = re.search(transformer_dict["find"], source_value)
            if s:
                value = s.group(0)
                break
            else:
                print_to_err_and_exit(
                    f"Regex for `find` transformer \"{transformer_dict['find']}\" could not be matched with value \"{source_value}\""
                )
    else:
        value = source_value

    for transformer_dict in transformers:
        if "replacer" in transformer_dict:
            value = value.replace(
                transformer_dict["replacer"]["from"],
                transformer_dict["replacer"]["to"],
            )

        if "addPrefix" in transformer_dict:
            value = f"{transformer_dict['addPrefix']}{value}"

        if "addSuffix" in transformer_dict:
            value = f"{value}{transformer_dict['addSuffix']}"

    print(f"Transformed value: {value}")

    return value


def get_content_regexed(source_value: str, string: str) -> str:
    """
    Replace {{ source `.+` }} with the value of the source between the ``
    """
    return re.sub(r"{{\s*source `[a-zA-Z\-]+`\s*}}", source_value, string)


def execute_file_target(
    target_name: str,
    target_config: dict,
    source_value: str,
    output_file_path: pathlib.Path,
) -> None:
    if "content" in target_config["spec"]:
        value = get_content_regexed(source_value, target_config["spec"]["content"])
        with output_file_path.open("w") as f:
            f.write(value)
    elif "lineMatchPattern" in target_config["spec"]:
        pattern = target_config["spec"]["lineMatchPattern"]
        transformers = target_config.get("transformers", {})
        replacer_value = get_replacer_value_with_transfomers(source_value, transformers)

        replaceRegexGroup = None
        for dict_ in transformers:
            if "replaceRegexGroup" in dict_:
                replaceRegexGroup = dict_["replaceRegexGroup"]
                if replaceRegexGroup <= 0:
                    print_to_err_and_exit(
                        f"Invalid regex group '{replaceRegexGroup}' in target {target_name}. The regex group must be > 0."
                    )

                break

        with output_file_path.open("r") as f:
            file_content = f.readlines()

        with output_file_path.open("w") as f:
            for line in file_content:
                m = re.match(pattern, line)
                if m:
                    if replaceRegexGroup:
                        for idx, group in enumerate(m.groups()):
                            if idx + 1 == replaceRegexGroup:
                                f.write(replacer_value)
                            else:
                                f.write(group)
                    else:
                        f.write(replacer_value)
                    f.write(os.linesep)
                else:
                    f.write(line)

    else:
        print_to_err_and_exit(
            f"No valid spec type found for target '{target_name}'. Correct values are: 'content', 'lineMatchPattern'."
        )


def execute_target(
    target_name: str, target_config: dict[str, typing.Any], source_value
) -> None:
    print("=" * 30)
    print(f"Executing target '{target_name}': {target_config['name']}")

    output_file_path = pathlib.Path(os.getcwd()) / target_config["spec"]["file"]
    try:
        output_file_path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass

    # TODO(greesb):
    # For file.lineMatchPattern and yaml, gather all the targets modifying the same
    # file to only have to rewrite the whole file once.
    if target_config["kind"] == "file":
        execute_file_target(target_name, target_config, source_value, output_file_path)
    elif target_config["kind"] == "yaml":
        # TODO(greesb):
        # Problems:
        # - PyYaml doesn't keep original format (additional line breaks, formatting of multi line values)
        # - 'on' in YAML = True, so PyYaml does the same:
        #   - Fix:
        #       def add_bool(self, node):
        #           return self.construct_scalar(node)
        #       yaml.constructor.SafeConstructor.add_constructor("tag:yaml.org,2002:bool", add_bool)
        pass
    else:
        print_to_err_and_exit(
            f"Kind value '{target_config['kind']}' not supported, in target {target_name}"
        )

    print(f"Done executing target {target_name}")


if __name__ == "__main__":
    parser = get_parser()

    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    if not len(config.get("sources", {})):
        print_to_err_and_exit("No sources found, please specify at least one.")

    sources_ids = list(config["sources"].keys())
    evaluated_sources = {}
    for target_name, target_config in config.get("targets", {}).items():
        source_id = target_config.get("sourceID")
        if not source_id:
            if len(sources_ids) > 1:
                print_to_err_and_exit(
                    f"Target {target_name} has no sourceID specified yet there are multiple sources to choose from."
                )

            source_id = sources_ids[0]
        elif source_id not in sources_ids:
            print_to_err_and_exit(
                f"SourceID {source_id}, for target {target_name}, does not exist."
            )

        if source_id not in evaluated_sources.keys():
            evaluated_sources[source_id] = evaluate_source(
                source_id, config["sources"][source_id]
            )

        execute_target(target_name, target_config, evaluated_sources[source_id])
