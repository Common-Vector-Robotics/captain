#!/usr/bin/env python3
"""Read Captain's ClickUp workspace structure and apply its write rules.

The structure lives in a private config file named by
``CAPTAIN_CLICKUP_WORKSPACE_CONFIG``: the Inbox list, the subsystem folder
lists (``lists``) Captain may file into directly when the destination is
certain, the folder keyword table, the archived lists Captain must never
write, the status sets, the allowed tags, the folder owners, and the identity
map used to turn a person's name into exactly one ClickUp assignee. The public repo ships
only the schema, ``data/clickup-workspace.example.json``, with placeholder
people; real names and user ids never belong in the repo.

This module has no command-line interface and makes no network requests.
``clickup_write.py`` enforces the write rules; ``daily_context.py``,
``daily_wrap.py``, ``critical_paths.py``, and ``personal_top2.py`` use the
status and task-kind helpers.

Environment:

- ``CAPTAIN_CLICKUP_WORKSPACE_CONFIG``: required path to the private config.
  Without it the writer refuses to run; readers fall back to generic rules.
- ``CAPTAIN_INBOX_LIST_ID``: the Inbox list id (overrides ``inbox_list_id``).
- ``CAPTAIN_ARCHIVED_LIST_IDS``: comma-separated ids added to the blocklist.
"""

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG_PATH = ROOT / "data" / "clickup-workspace.example.json"
DEFAULT_CONFIG_PATH = ROOT / "data" / "clickup-workspace.json"
CONFIG_ENV = "CAPTAIN_CLICKUP_WORKSPACE_CONFIG"
INBOX_ENV = "CAPTAIN_INBOX_LIST_ID"
ARCHIVED_ENV = "CAPTAIN_ARCHIVED_LIST_IDS"

# Every created task description must start with this line.
DONE_WHEN_PREFIX = "done when:"
PROPOSED_FOLDER_PREFIX = "proposed folder:"
FILED_DIRECTLY_PREFIX = "filed directly:"

# ClickUp status types that mean the work is finished.
FINISHED_TYPES = {"done", "closed"}


class WorkspaceConfigError(ValueError):
    """The workspace config is missing, unreadable, or inconsistent."""


class IdentityResolutionError(ValueError):
    """A person could not be resolved to exactly one assignable member."""


def config_path():
    """Return the private config path.

    ``CAPTAIN_CLICKUP_WORKSPACE_CONFIG`` wins when set. Otherwise the host's
    private copy at ``data/clickup-workspace.json`` is used, the same place the
    other private data files live next to their ``.example.json``. That file is
    gitignored. With neither present this is a configuration error, so no
    ClickUp write runs without the host's real identity map and rules.
    """
    value = os.environ.get(CONFIG_ENV, "").strip()
    if value:
        return Path(value).expanduser()
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    raise WorkspaceConfigError(
        "no ClickUp workspace config: set {} or install the private file at {} "
        "(schema: data/clickup-workspace.example.json)".format(CONFIG_ENV, DEFAULT_CONFIG_PATH)
    )


def load_workspace(path=None):
    """Read the workspace config as a mapping, failing closed on any problem."""
    path = Path(path) if path else config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkspaceConfigError(
            "cannot read ClickUp workspace config {}: {}".format(path, error)
        ) from error
    if not isinstance(data, dict):
        raise WorkspaceConfigError("ClickUp workspace config {} must be an object".format(path))
    return data


# Destinations


def archived_list_ids(config):
    """Return every list id Captain must never write (config plus env)."""
    ids = {str(item).strip() for item in config.get("archived_list_ids") or [] if str(item).strip()}
    ids.update(
        item.strip() for item in os.environ.get(ARCHIVED_ENV, "").split(",") if item.strip()
    )
    return ids


def inbox_list_id(config):
    """Return the one list id task creation may target.

    ``CAPTAIN_INBOX_LIST_ID`` wins over the config. A missing value or an
    Inbox id that is also archived is a configuration error.
    """
    value = os.environ.get(INBOX_ENV, "").strip() or str(config.get("inbox_list_id") or "").strip()
    if not value:
        raise WorkspaceConfigError(
            "no Inbox list configured: set inbox_list_id in the workspace config or {}".format(
                INBOX_ENV
            )
        )
    if value in archived_list_ids(config):
        raise WorkspaceConfigError("configured Inbox list {} is archived".format(value))
    return value


def optional_inbox_list_id(config):
    """Return the Inbox id for readers, or ``None`` when it is not configured."""
    try:
        return inbox_list_id(config)
    except WorkspaceConfigError:
        return None


# Direct filing: subsystem folder lists and the certainty rule

# A product is known only when the text names it, or names a project that
# belongs to exactly one product.
SPACE_PATTERNS = (
    ("GL-1", r"gl-?1"),
    ("Ghostrunner", r"ghost ?runner"),
    ("GL-1", r"afs"),
    ("Ghostrunner", r"newlab"),
)

# Words that name a subsystem folder outright.
FOLDER_NAME_PATTERNS = (
    ("Structures", r"structures"),
    ("Chassis", r"chassis"),
    ("Power", r"power"),
    ("Battery", r"battery"),
    ("Harness", r"harness"),
    ("Sensors & Compute", r"sensors (?:&|and) compute|sensors|compute"),
    ("Safety", r"safety"),
    ("Autonomy", r"autonomy"),
    ("Firmware", r"firmware"),
    ("HMI", r"hmi"),
    ("Integration", r"integration"),
)

# Folders Captain never files into, even when a list id is configured.
NEVER_DIRECT_FOLDERS = {"projects"}

ROUTE_NAMED = "named-folder"
ROUTE_KEYWORD_OWNER = "keyword-owner"
ROUTE_INBOX = "inbox"


def _word(pattern):
    return re.compile(r"(?<![a-z0-9])(?:{})(?![a-z0-9])".format(pattern))


def _hits(text, patterns):
    """Return the distinct names whose pattern occurs in ``text``, in table order."""
    found = []
    for name, pattern in patterns:
        if name not in found and _word(pattern).search(text):
            found.append(name)
    return found


def product_space_names(config):
    """Return the names of the product spaces (never OPS)."""
    names = [
        str(space.get("name")) for space in config.get("spaces") or []
        if isinstance(space, dict) and space.get("kind") == "product" and space.get("name")
    ]
    return names or ["GL-1", "Ghostrunner"]


def folder_in_space(folder, space, config):
    """Return whether ``folder`` exists in ``space`` (``folders_only_in`` limits some)."""
    only_in = (config.get("folders_only_in") or {}).get(folder)
    return not only_in or space in only_in


def direct_lists(config):
    """Return ``{list_id: (space, folder)}`` for every subsystem list Captain may file into.

    Only product spaces count; OPS lists, the Projects folders, and archived
    lists are never direct destinations.
    """
    products = set(product_space_names(config))
    archived = archived_list_ids(config)
    result = {}
    for space, folders in (config.get("lists") or {}).items():
        if space not in products or not isinstance(folders, dict):
            continue
        for folder, list_id in folders.items():
            list_id = str(list_id or "").strip()
            if (
                list_id
                and list_id not in archived
                and folder.casefold() not in NEVER_DIRECT_FOLDERS
                and folder_in_space(folder, space, config)
            ):
                result[list_id] = (space, folder)
    return result


def direct_list_id(space, folder, config):
    """Return the configured list id for ``space``/``folder``, or ``None``."""
    for list_id, location in direct_lists(config).items():
        if location == (space, folder):
            return list_id
    return None


def route_details(text, assignee_id, config):
    """Apply the certainty rule and explain it; ``route_destination`` wraps this.

    The result maps ``list_id``, ``rule``, ``reason``, ``space``, ``folder``
    (both ``None`` for the Inbox), and ``proposal``: a ``Space/Folder`` guess
    (``A or B`` when several) for an Inbox task's ``Proposed folder:`` line, or
    ``None`` when the text points at no folder.
    """
    lowered = str(text or "").casefold()
    products = product_space_names(config)
    spaces = [name for name in _hits(lowered, SPACE_PATTERNS) if name in products]
    named = _hits(lowered, FOLDER_NAME_PATTERNS)
    keyword_table = [
        (folder, "|".join(re.escape(str(word).casefold()) for word in words if str(word).strip()))
        for folder, words in (config.get("folder_keywords") or {}).items()
        if any(str(word).strip() for word in words or [])
    ]
    keyword_hits = [] if named else _hits(lowered, keyword_table)

    def inbox(reason):
        candidates = named or keyword_hits
        pairs = [
            "{}/{}".format(space, folder)
            for space in (spaces if len(spaces) == 1 else products)
            for folder in candidates
            if folder_in_space(folder, space, config)
        ]
        return {
            "list_id": inbox_list_id(config),
            "rule": ROUTE_INBOX,
            "reason": reason,
            "space": None,
            "folder": None,
            "proposal": " or ".join(pairs) or None,
        }

    # 1. The product.
    if not spaces:
        return inbox("space unknown: the text names no product (GL-1, Ghostrunner, AFS, Newlab)")
    if len(spaces) > 1:
        return inbox("space unknown: the text names several products ({})".format(", ".join(spaces)))
    space = spaces[0]

    # 2. The folder: named outright, or one keyword hit that the assignee owns.
    if len(named) > 1:
        return inbox("folder unknown: the text names several folders ({})".format(", ".join(named)))
    if named:
        folder, rule = named[0], ROUTE_NAMED
        why = "the text names {} and {}".format(space, folder)
    elif not keyword_hits:
        return inbox("folder unknown: the text names no folder and no folder keyword matches")
    elif len(keyword_hits) > 1:
        return inbox("folder unknown: keywords match several folders ({})".format(
            ", ".join(keyword_hits)))
    else:
        folder, rule = keyword_hits[0], ROUTE_KEYWORD_OWNER
        owner = str((config.get("folder_owners") or {}).get(folder) or "")
        if assignee_id is None or not owner or str(assignee_id) != owner:
            return inbox("folder unknown: keywords point at {} but the assignee is not its "
                         "owner".format(folder))
        why = "the text names {}; keywords point only at {} and the assignee owns it".format(
            space, folder)

    if not folder_in_space(folder, space, config):
        return inbox("folder unknown: {} exists only in {}, not {}".format(
            folder, ", ".join((config.get("folders_only_in") or {}).get(folder) or []), space))

    # 3. Certain: both known, so file into that folder's list.
    list_id = direct_list_id(space, folder, config)
    if not list_id:
        return inbox("no list configured for {}/{} in the workspace config".format(space, folder))
    return {
        "list_id": list_id,
        "rule": rule,
        "reason": why,
        "space": space,
        "folder": folder,
        "proposal": "{}/{}".format(space, folder),
    }


def route_destination(text, assignee_id, config):
    """Return ``(list_id, rule, reason)`` for a new task described by ``text``.

    The destination is a subsystem folder's list only when it is certain:

    1. The space is known only if the text names the product (``GL-1``/``GL1``,
       ``Ghostrunner``/``ghost runner``) or a project that implies one
       (``AFS`` means GL-1, ``Newlab`` means Ghostrunner).
    2. The folder is known only if the text names exactly one subsystem
       folder (rule ``named-folder``), or names none while the config's
       ``folder_keywords`` match exactly one folder and ``assignee_id`` is
       that folder's owner in ``folder_owners`` (rule ``keyword-owner``).
       HMI exists only in GL-1.
    3. Anything less is the Inbox (rule ``inbox``).

    Example input: ``("check the DCDC grounding on GL-1", <Power owner id>, config)``
    Example output: ``("<GL-1 Power list id>", "keyword-owner", "...")``
    """
    route = route_details(text, assignee_id, config)
    return route["list_id"], route["rule"], route["reason"]


# Task descriptions and tags


def _lines(description):
    return [line.strip() for line in (description or "").splitlines() if line.strip()]


def description_problems(description, filed_in=None):
    """Return the reasons a create-task description is not acceptable.

    The first non-blank line must start with ``Done when:`` and carry a
    condition. An Inbox task (``filed_in`` is ``None``) needs a
    ``Proposed folder:`` line that names one and no ``Filed directly:`` line.
    A task filed straight into a subsystem list (``filed_in`` is its
    ``(space, folder)``) needs a ``Filed directly: <Space>/<Folder>`` line that
    names that list.
    """
    lines = _lines(description)
    problems = []
    first = lines[0] if lines else ""
    if not first.casefold().startswith(DONE_WHEN_PREFIX) or not first[len(DONE_WHEN_PREFIX):].strip():
        problems.append("description must begin with a 'Done when: <condition>' line")
    filed_lines = [line for line in lines if line.casefold().startswith(FILED_DIRECTLY_PREFIX)]
    if filed_in is None:
        folder_lines = [line for line in lines if line.casefold().startswith(PROPOSED_FOLDER_PREFIX)]
        if not any(line[len(PROPOSED_FOLDER_PREFIX):].strip() for line in folder_lines):
            problems.append("description must include a 'Proposed folder: <Space>/<Folder>' line")
        if filed_lines:
            problems.append("an Inbox task must not carry a 'Filed directly:' line")
        return problems
    expected = "{}/{}".format(*filed_in).casefold()
    if not any(
        line[len(FILED_DIRECTLY_PREFIX):].strip().casefold().startswith(expected)
        for line in filed_lines
    ):
        problems.append(
            "a task filed directly into {}/{} must carry a 'Filed directly: {}/{} (<rule>)' "
            "line".format(filed_in[0], filed_in[1], filed_in[0], filed_in[1])
        )
    return problems


def filter_tags(tags, config):
    """Split requested tags into ``(kept, dropped)`` using ``allowed_tags``.

    Example input: ``["Safety", "R-12"]``
    Example output: ``(["safety"], ["R-12"])``
    """
    allowed = {str(tag).strip().casefold() for tag in config.get("allowed_tags") or []}
    kept, dropped = [], []
    for tag in tags or []:
        name = str(tag).strip()
        if not name:
            continue
        if name.casefold() in allowed:
            if name.casefold() not in kept:
                kept.append(name.casefold())
        elif name not in dropped:
            dropped.append(name)
    return kept, dropped


# Identity


def normalize_name(value):
    """Lowercase and collapse punctuation so aliases compare reliably."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def members(config):
    """Return the configured member records."""
    return [member for member in config.get("members") or [] if isinstance(member, dict)]


def _member_keys(member):
    name = normalize_name(member.get("name"))
    keys = {name, normalize_name(member.get("clickup_id"))}
    if name:
        keys.add(name.split()[0])
    keys.update(normalize_name(alias) for alias in member.get("aliases") or [])
    return {key for key in keys if key}


def resolve_member(person, config):
    """Resolve a name, alias, or numeric id to exactly one member record.

    Departed people, unknown names or ids, and names matching several members
    raise ``IdentityResolutionError``. Nothing is guessed.
    """
    key = normalize_name(person)
    if not key:
        raise IdentityResolutionError("empty assignee")

    departed = {normalize_name(name) for name in config.get("departed_names") or []}
    if key in departed or key.split()[0] in departed:
        raise IdentityResolutionError(
            "{!r} is no longer in the ClickUp workspace; hold the item and ask a human "
            "for the current owner".format(str(person))
        )

    matches = [member for member in members(config) if key in _member_keys(member)]
    if not matches:
        raise IdentityResolutionError(
            "{!r} is not in the ClickUp identity map; hold the item instead of "
            "guessing".format(str(person))
        )
    if len(matches) > 1:
        raise IdentityResolutionError(
            "{!r} matches several ClickUp members ({}); hold the item".format(
                str(person), ", ".join(member.get("name") or "?" for member in matches)
            )
        )
    return matches[0]


def resolve_assignee_id(person, config):
    """Return the numeric ClickUp id for one assignable person."""
    member = resolve_member(person, config)
    if not member.get("assignable", True):
        raise IdentityResolutionError(
            "{} can never be a ClickUp assignee".format(member.get("name") or person)
        )
    try:
        return int(member["clickup_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise WorkspaceConfigError(
            "member {!r} has no numeric clickup_id".format(member.get("name"))
        ) from error


# Statuses and task kinds


def status_parts(task_or_status):
    """Return ``(name, type)`` for a task, a status mapping, or a status name.

    Example input: ``{"status": {"status": "Done", "type": "closed"}}``
    Example output: ``("done", "closed")``
    """
    status = task_or_status
    # A task carries its status mapping under ``status``; unwrap it once.
    if isinstance(status, dict) and isinstance(status.get("status"), dict):
        status = status["status"]
    if isinstance(status, dict):
        return (
            str(status.get("status") or "").strip().casefold(),
            str(status.get("type") or "").strip().casefold(),
        )
    return str(status or "").strip().casefold(), ""


def is_finished(task_or_status, config=None):
    """Return whether ClickUp considers the task done or closed.

    The ClickUp status ``type`` decides. Only when the type is missing do the
    configured fallback names (``closed_status_names_fallback``) apply.
    """
    name, typ = status_parts(task_or_status)
    if typ:
        return typ in FINISHED_TYPES
    names = (config or {}).get("closed_status_names_fallback") or [
        "done", "complete", "completed", "closed", "cancelled", "canceled",
    ]
    return name in {str(item).casefold() for item in names}


def is_open(task_or_status, config=None):
    """Return whether the task still needs work."""
    return not is_finished(task_or_status, config)


def is_not_started(task_or_status, config=None):
    """Return whether an open task has not started (type ``open`` or a named set)."""
    name, typ = status_parts(task_or_status)
    if typ in FINISHED_TYPES:
        return False
    if typ == "open":
        return True
    names = (config or {}).get("not_started_statuses") or ["backlog", "ready", "intake"]
    return name in {str(item).casefold() for item in names}


def task_list_id(task):
    """Return the id of the list that holds a task, or an empty string."""
    task_list = task.get("list") or {}
    return str(task_list.get("id") or task.get("list_id") or "")


def is_milestone(task, config=None):
    """Return whether a task is a ClickUp Milestone (``custom_item_id``)."""
    config = config or {}
    ids = {str(item) for item in config.get("milestone_custom_item_ids") or [1]}
    if task.get("custom_item_id") is not None and str(task.get("custom_item_id")) in ids:
        return True
    return str(task.get("id")) in {str(item) for item in config.get("milestone_task_ids") or []}


def is_inbox_task(task, config=None):
    """Return whether a task sits in the configured Inbox list."""
    inbox = optional_inbox_list_id(config or {})
    return bool(inbox) and task_list_id(task) == inbox


def exempt_from_owner_checks(task, config=None):
    """Milestones and Inbox items are never owner gaps or blockers."""
    return is_milestone(task, config) or is_inbox_task(task, config)


_READER_CACHE = {}


def reader_config():
    """Return the config for read-only board scripts, or ``{}`` without one.

    Readers degrade to generic rules (status type first, default milestone
    type, no Inbox exemption) instead of failing. The result is cached per
    config path.
    """
    try:
        path = str(config_path())
    except WorkspaceConfigError:
        return {}
    if path not in _READER_CACHE:
        try:
            _READER_CACHE[path] = load_workspace(path)
        except WorkspaceConfigError:
            return {}
    return _READER_CACHE[path]
