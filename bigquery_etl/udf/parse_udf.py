"""
Machinery for parsing UDFs and tests defined in .sql files.

This should eventually be refactored to a more general library for
parsing UDF dependencies in queries as well.
"""

from dataclasses import dataclass, astuple
import re
import os
from pathlib import Path
from typing import List
import sqlparse
import yaml

from bigquery_etl.metadata.parse_metadata import METADATA_FILE
from bigquery_etl.util.common import project_dirs


UDF_DIRS = ("udf", "udf_js")
MOZFUN_DIR = ("mozfun",)
UDF_CHAR = "[a-zA-z0-9_]"
UDF_FILE = "udf.sql"
PROCEDURE_FILE = "stored_procedure.sql"
EXAMPLE_DIR = "examples"
TEMP_UDF_RE = re.compile(f"(?:udf|assert)_{UDF_CHAR}+")
PERSISTENT_UDF_RE = re.compile(fr"((?:udf|assert){UDF_CHAR}*)\.({UDF_CHAR}+)")
MOZFUN_UDF_RE = re.compile(fr"({UDF_CHAR}+)\.({UDF_CHAR}+)")
PERSISTENT_UDF_PREFIX = re.compile(
    r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION(\s+IF\s+NOT\s+EXISTS)?", re.IGNORECASE
)
UDF_NAME_RE = re.compile(r"^([a-zA-Z0-9_]+\.)?[a-zA-Z][a-zA-Z0-9_]{0,255}$")
GENERIC_DATASET = "_generic_dataset_"

# UDFs defined in mozfun
MOZFUN_ROUTINES = [
    {
        "name": root.split("/")[-2] + "." + root.split("/")[-1],
        "is_udf": filename == UDF_FILE,
    }
    for udf_dir in MOZFUN_DIR
    for root, dirs, files in os.walk(udf_dir)
    for filename in files
    if filename in (UDF_FILE, PROCEDURE_FILE)
]


@dataclass
class RawUdf:
    """Representation of the content of a single UDF sql file."""

    name: str
    dataset: str
    filepath: str
    definitions: List[str]
    tests: List[str]
    dependencies: List[str]
    description: str  # description from metadata file
    is_stored_procedure: str

    @staticmethod
    def from_file(filepath):
        """Read in a RawUdf from a SQL file on disk."""
        filepath = Path(filepath)

        text = filepath.read_text()
        name = filepath.parent.name
        dataset = filepath.parent.parent.name

        # check if UDF has associated metadata file
        description = ""
        metadata_file = filepath.parent / METADATA_FILE
        if metadata_file.exists():
            metadata = yaml.safe_load(metadata_file.read_text())
            if "description" in metadata:
                description = metadata["description"]

        try:
            return RawUdf.from_text(text, dataset, name, str(filepath), description)
        except ValueError as e:
            raise ValueError(str(e) + f" in {filepath}")

    @staticmethod
    def from_text(text, dataset, name, filepath=None, description="", is_defined=True):
        """Create a RawUdf instance from text.

        If is_defined is False, then the UDF does not
        need to be defined in the text; it could be
        just tests.
        """
        sql = sqlparse.format(text, strip_comments=True)
        statements = [s for s in sqlparse.split(sql) if s.strip()]

        prod_name = name
        persistent_name = f"{dataset}.{name}"
        temp_name = f"{dataset}_{name}"
        internal_name = None
        is_stored_procedure = False

        definitions = []
        tests = []

        procedure_start = -1

        for i, s in enumerate(statements):
            normalized_statement = " ".join(s.lower().split())
            if normalized_statement.startswith("create or replace function"):
                definitions.append(s)
                if persistent_name in normalized_statement:
                    internal_name = persistent_name

            elif normalized_statement.startswith("create temp function"):
                definitions.append(s)
                if temp_name in normalized_statement:
                    internal_name = temp_name

            elif normalized_statement.startswith("create or replace procedure"):
                is_stored_procedure = True
                definitions.append(s)
                tests.append(s)
                if persistent_name in normalized_statement:
                    internal_name = persistent_name

            else:
                if normalized_statement.startswith("begin"):
                    procedure_start = i

                if procedure_start == -1:
                    tests.append(s)

                if procedure_start > -1 and normalized_statement.endswith("end;"):
                    tests.append(" ".join(statements[procedure_start : i + 1]))
                    procedure_start = -1

        for name in (prod_name, internal_name):
            if name is None:
                raise ValueError(
                    f"Expected a function named {persistent_name} or {temp_name} "
                    f"to be defined"
                )
            if is_defined and not UDF_NAME_RE.match(name):
                raise ValueError(
                    f"Invalid UDF name {name}: Must start with alpha char, "
                    f"limited to chars {UDF_CHAR}, be at most 256 chars long"
                )

        # find usages of both persistent and temporary UDFs
        dependencies = re.findall(PERSISTENT_UDF_RE, "\n".join(definitions))
        dependencies = [".".join(t) for t in dependencies]
        dependencies.extend(re.findall(TEMP_UDF_RE, "\n".join(definitions)))

        # for public UDFs dependencies can live in arbitrary dataset;
        # we can check if some known dependency is part of the UDF
        # definition instead
        for udf in MOZFUN_ROUTINES:
            if udf["name"] in "\n".join(definitions):
                dependencies.append(udf["name"])

        if is_defined:
            dependencies.remove(internal_name)

        return RawUdf(
            internal_name,
            dataset,
            filepath,
            definitions,
            tests,
            # We convert the list to a set to deduplicate entries,
            # but then convert back to a list for stable order.
            sorted(set(dependencies)),
            description,
            is_stored_procedure,
        )


@dataclass
class ParsedUdf(RawUdf):
    """Parsed representation of a UDF including dependent UDF code."""

    tests_full_sql: List[str]

    @staticmethod
    def from_raw(raw_udf, tests_full_sql):
        """Promote a RawUdf to a ParsedUdf."""
        return ParsedUdf(*astuple(raw_udf), tests_full_sql)


def get_udf_dirs(udf_dirs, project_id=None):
    """Return paths to directories with UDFs."""
    # todo: make this more generic, I don't think we need to make the distinction
    # between mozfun and other project here since UDFs now live in project directories
    if project_id != "mozfun":
        # for non-mozfun projects, the default UDF directories are udf/ and udf_js/
        # the project needs to be pre-pended to these paths
        projects = project_dirs(project_id)

        udf_dirs = [
            os.path.join(project, d)
            for project in projects
            for d in udf_dirs
            if os.path.exists(os.path.join(project, d))
        ]

    return udf_dirs


def read_udf_dirs(*udf_dirs):
    """Read contents of udf_dirs into dict of RawUdf instances."""
    return {
        raw_udf.name: raw_udf
        for udf_dir in (udf_dirs or get_udf_dirs(UDF_DIRS) + list(MOZFUN_DIR))
        for root, dirs, files in os.walk(udf_dir)
        if os.path.basename(root) != EXAMPLE_DIR
        for filename in files
        if filename in (UDF_FILE, PROCEDURE_FILE)
        for raw_udf in (RawUdf.from_file(os.path.join(root, filename)),)
    }


def parse_udf_dirs(*udf_dirs):
    """Read contents of udf_dirs into ParsedUdf instances."""
    # collect udfs to parse
    raw_udfs = read_udf_dirs(*udf_dirs)

    # prepend udf definitions to tests
    for raw_udf in raw_udfs.values():
        tests_full_sql = udf_tests_sql(raw_udf, raw_udfs)
        yield ParsedUdf.from_raw(raw_udf, tests_full_sql)


def accumulate_dependencies(deps, raw_udfs, udf_name):
    """
    Accumulate a list of dependent UDF names.

    Given a dict of raw_udfs and a udf_name string, recurse into the
    UDF's dependencies, adding the names to deps in depth-first order.
    """
    if udf_name not in raw_udfs:
        return deps

    raw_udf = raw_udfs[udf_name]
    for dep in raw_udf.dependencies:
        deps = accumulate_dependencies(deps, raw_udfs, dep)
    if udf_name in deps:
        return deps
    else:
        return deps + [udf_name]


def udf_usages_in_file(filepath):
    """Return a list of UDF names used in the provided SQL file."""
    with open(filepath) as f:
        text = f.read()
    return udf_usages_in_text(text)


def udf_usages_in_text(text):
    """Return a list of UDF names used in the provided SQL text."""
    sql = sqlparse.format(text, strip_comments=True)
    udf_usages = PERSISTENT_UDF_RE.findall(sql)
    udf_usages = list(map(lambda t: ".".join(t), udf_usages))
    # the TEMP_UDF_RE matches udf_js, remove since it's not a valid UDF
    tmp_udfs = list(filter(lambda u: u != "udf_js", TEMP_UDF_RE.findall(sql)))
    udf_usages.extend(tmp_udfs)

    for routine in MOZFUN_ROUTINES:
        if routine["name"] in sql:
            udf_usages.append(routine["name"])

    return sorted(set(udf_usages))


def udf_usage_definitions(text, raw_udfs=None):
    """Return a list of definitions of UDFs used in provided SQL text."""
    if raw_udfs is None:
        raw_udfs = read_udf_dirs()
    deps = []
    for udf_usage in udf_usages_in_text(text):
        deps = accumulate_dependencies(deps, raw_udfs, udf_usage)
    return [
        statement
        for udf_name in deps
        for statement in raw_udfs[udf_name].definitions
        if statement not in text
    ]


def sub_local_routines(test, raw_udfs=None):
    """
    Transform persistent UDFs into temporary UDFs.

    Use generic dataset for stored procedures.
    """
    sql = prepend_udf_usage_definitions(test, raw_udfs)
    sql = sub_persistent_udf_names_as_temp(sql)

    for routine in MOZFUN_ROUTINES:
        if routine["name"] in sql:
            replace_name = routine["name"].replace(".", "_")
            if not routine["is_udf"]:
                replace_name = GENERIC_DATASET + "." + replace_name
            regex_str = r"(?:mozfun.)?" + routine["name"]
            sql = re.sub(regex_str, replace_name, sql)

    sql = PERSISTENT_UDF_PREFIX.sub("CREATE TEMP FUNCTION", sql)
    return sql


def udf_tests_sql(raw_udf, raw_udfs):
    """
    Create tests for testing persistent UDFs.

    Persistent UDFs need to be rewritten as temporary UDFs so that changes
    can be tested.
    """
    tests_full_sql = []
    for test in raw_udf.tests:
        test_sql = sub_local_routines(test, raw_udfs)
        tests_full_sql.append(test_sql)

    return tests_full_sql


def prepend_udf_usage_definitions(text, raw_udfs=None):
    """Prepend definitions of UDFs used to provided SQL text."""
    statements = udf_usage_definitions(text, raw_udfs)
    return "\n\n".join(statements + [text])


def sub_persistent_udf_names_as_temp(text):
    """Substitute persistent UDF references with temporary UDF references."""
    return PERSISTENT_UDF_RE.sub(r"\1_\2", text)
