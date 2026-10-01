#!/usr/bin/env python3
"""
pve-ansible-compare
===================

Compare the guests (QEMU virtual machines and LXC containers) running on a
Proxmox VE cluster with the hosts declared in the inventories of an Ansible
project.

The Ansible project is expected to follow this layout::

    <ansible_project>/inventories/<location>/<env_name>.yml

where ``<location>`` usually identifies a single Proxmox cluster and
``<env_name>`` is the environment. On the Proxmox side, the environment of a
guest is declared with a tag such as ``env.<env_name>``.

For every guest collected from the cluster, the script checks whether it is
declared in the inventory matching its environment and prints a report.
Optionally it also reports "orphan" hosts, i.e. hosts declared in an inventory
that do not exist on the cluster.

Run ``pve_ansible_compare.py --help`` for the full list of options.
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import json
import os
import re
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover - dependency check
    sys.stderr.write("ERROR: PyYAML is required (pip install -r requirements.txt)\n")
    sys.exit(2)

__version__ = "1.0.0"

# Exit codes
EXIT_OK = 0
EXIT_DISCREPANCIES = 1
EXIT_ERROR = 2

# Result statuses for a Proxmox guest
STATUS_OK = "ok"                # found in an inventory of its environment
STATUS_WRONG_ENV = "wrong-env"  # found only in inventories of other environments
STATUS_MISSING = "missing"      # not found in any inventory

# Directories inside a location directory that never contain inventory files
NON_INVENTORY_DIRS = {"group_vars", "host_vars"}
INVENTORY_SUFFIXES = {".yml", ".yaml"}


class CompareError(Exception):
    """Fatal error raised for invalid input (inventories, options, API data)."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class InventoryHost:
    """A host declared in an Ansible inventory file.

    Attributes:
        name: The host name as written in the inventory (after range expansion).
        direct_groups: Groups in which the host is explicitly listed.
        all_groups: Direct groups plus every ancestor group (except ``all``).
    """

    name: str
    direct_groups: set[str] = field(default_factory=set)
    all_groups: set[str] = field(default_factory=set)


@dataclass
class Inventory:
    """A parsed Ansible inventory file.

    Attributes:
        path: Path of the inventory file.
        location: Location name (name of the parent directory).
        env: Environment name (file name without extension, lowercase).
        hosts: Hosts declared in the inventory, indexed by name.
    """

    path: Path
    location: str
    env: str
    hosts: dict[str, InventoryHost] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Return a short human readable label such as ``site1/dev.yml``."""
        return f"{self.location}/{self.path.name}"


@dataclass
class Guest:
    """A Proxmox resource to be checked: a QEMU VM, an LXC container or a node.

    Attributes:
        name: Guest name (node name for cluster nodes).
        kind: ``qemu``, ``lxc`` or ``node``.
        node: Proxmox node hosting the guest.
        vmid: Guest ID (``None`` for nodes).
        status: Runtime status reported by Proxmox (``running``, ``stopped``...).
        tags: List of tags assigned to the guest.
        template: Whether the guest is a template.
        envs: Environments the guest belongs to (resolved later).
        env_source: Where the environment comes from (``tag``, ``default``, ``nodes-option``).
    """

    name: str
    kind: str
    node: str
    vmid: int | None = None
    status: str = ""
    tags: list[str] = field(default_factory=list)
    template: bool = False
    envs: list[str] = field(default_factory=list)
    env_source: str = ""


@dataclass
class Match:
    """A match between a Proxmox guest and an inventory host."""

    inventory: Inventory
    host: InventoryHost


@dataclass
class GuestResult:
    """Outcome of the comparison for a single guest.

    Attributes:
        guest: The checked guest.
        status: One of ``STATUS_OK``, ``STATUS_WRONG_ENV``, ``STATUS_MISSING``.
        expected: Matches in inventories of the guest's environment.
        others: Matches in inventories of other environments.
        notes: Additional remarks (e.g. no inventory file for the environment).
    """

    guest: Guest
    status: str
    expected: list[Match] = field(default_factory=list)
    others: list[Match] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Excluded:
    """A guest that was not checked, with the reason why."""

    guest: Guest
    reason: str


@dataclass
class Report:
    """Full result of a comparison run."""

    source: str
    inventories: list[Inventory]
    results: list[GuestResult]
    excluded: list[Excluded]
    orphans: list[Match] | None  # None when orphan detection was not requested
    warnings: list[str] = field(default_factory=list)

    def by_status(self, status: str) -> list[GuestResult]:
        """Return the results having the given status."""
        return [r for r in self.results if r.status == status]

    @property
    def has_discrepancies(self) -> bool:
        """Return True if any guest is missing/misplaced or any orphan was found."""
        if any(r.status != STATUS_OK for r in self.results):
            return True
        return bool(self.orphans)


# ---------------------------------------------------------------------------
# Ansible inventory parsing
# ---------------------------------------------------------------------------

class InventoryLoader(yaml.SafeLoader):
    """YAML SafeLoader that tolerates Ansible specific tags (``!vault``, ``!unsafe``...).

    The content of tagged nodes is loaded as plain data: we only care about
    the inventory structure, not about the values of variables.
    """


def _construct_unknown_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    """Construct a node carrying an unknown ``!tag`` as plain YAML data.

    Args:
        loader: The YAML loader instance.
        tag_suffix: The tag name without the leading ``!`` (unused).
        node: The YAML node to construct.

    Returns:
        A string, list or dict depending on the node type.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


InventoryLoader.add_multi_constructor("!", _construct_unknown_tag)


def strip_host_port(pattern: str) -> str:
    """Remove an optional ``:port`` suffix from a host entry.

    Ansible allows entries such as ``host.example.com:2222``. IPv6 addresses
    (more than one colon outside brackets) are left untouched.

    Args:
        pattern: The host entry as written in the inventory.

    Returns:
        The host entry without the port.
    """
    outside_brackets = re.sub(r"\[[^\]]*\]", "", pattern)
    if outside_brackets.count(":") == 1:
        match = re.match(r"^(.+):(\d+)$", pattern)
        if match:
            return match.group(1)
    return pattern


def expand_host_pattern(pattern: str) -> list[str]:
    """Expand Ansible host ranges such as ``web[01:10].example.com``.

    Numeric (optionally zero padded) and alphabetic ranges are supported,
    with an optional stride (``[1:10:2]``). Multiple ranges in the same
    pattern are expanded recursively. Anything that does not look like a
    valid range is returned unchanged.

    Args:
        pattern: A host entry from the inventory.

    Returns:
        The list of host names the pattern expands to.
    """
    match = re.search(r"\[([^\]]+)\]", pattern)
    if not match:
        return [pattern]

    head, tail = pattern[: match.start()], pattern[match.end():]
    spec = match.group(1).split(":")
    if len(spec) not in (2, 3):
        return [pattern]

    begin, end = spec[0], spec[1]
    try:
        stride = int(spec[2]) if len(spec) == 3 else 1
    except ValueError:
        return [pattern]
    if stride < 1:
        return [pattern]

    if begin.isdigit() and end.isdigit():
        # Zero padding is kept when the begin value has leading zeros
        width = len(begin) if begin.startswith("0") and len(begin) > 1 else 0
        sequence = [str(i).zfill(width) for i in range(int(begin), int(end) + 1, stride)]
    elif (len(begin) == 1 and len(end) == 1
          and begin in string.ascii_letters and end in string.ascii_letters):
        letters = string.ascii_letters
        sequence = list(letters[letters.index(begin): letters.index(end) + 1: stride])
    else:
        return [pattern]

    # The tail may contain further ranges
    return [head + item + rest for item in sequence for rest in expand_host_pattern(tail)]


def parse_inventory(path: Path, location: str, env: str) -> Inventory:
    """Parse an Ansible YAML inventory file.

    Groups are walked recursively through ``children`` at any depth. A group
    may be defined in one place and referenced (with an empty body) as a child
    of another group elsewhere: membership is resolved globally, as Ansible
    does, so every host gets its direct groups and all ancestor groups.

    Args:
        path: Path of the inventory file.
        location: Location name associated with the inventory.
        env: Environment name associated with the inventory.

    Returns:
        The parsed inventory.

    Raises:
        CompareError: If the file cannot be read or is not a valid inventory.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=InventoryLoader)  # noqa: S506 - SafeLoader subclass
    except (OSError, yaml.YAMLError) as exc:
        raise CompareError(f"cannot load inventory {path}: {exc}") from exc

    inventory = Inventory(path=path, location=location, env=env)
    if data is None:
        return inventory
    if not isinstance(data, dict):
        raise CompareError(f"inventory {path} is not a YAML mapping of groups")

    group_hosts: dict[str, set[str]] = {}      # group -> hosts listed directly
    group_parents: dict[str, set[str]] = {}    # group -> parent groups

    def walk(group: str, definition: Any) -> None:
        """Recursively register a group definition and its children."""
        group_hosts.setdefault(group, set())
        group_parents.setdefault(group, set())
        if not isinstance(definition, dict):
            # Empty body: reference to a group defined elsewhere
            return

        hosts = definition.get("hosts")
        if isinstance(hosts, dict):
            for entry in hosts:
                for name in expand_host_pattern(strip_host_port(str(entry))):
                    group_hosts[group].add(name)

        children = definition.get("children")
        if isinstance(children, dict):
            for child, child_definition in children.items():
                child = str(child)
                walk(child, child_definition)
                group_parents[child].add(group)

    for top_group, definition in data.items():
        walk(str(top_group), definition)

    def ancestors(group: str) -> set[str]:
        """Return all ancestor groups of ``group`` (cycle safe)."""
        seen: set[str] = set()
        stack = list(group_parents.get(group, ()))
        while stack:
            parent = stack.pop()
            if parent not in seen:
                seen.add(parent)
                stack.extend(group_parents.get(parent, ()))
        return seen

    for group, names in group_hosts.items():
        group_ancestors = ancestors(group)
        for name in names:
            host = inventory.hosts.setdefault(name, InventoryHost(name=name))
            host.direct_groups.add(group)
            host.all_groups.add(group)
            host.all_groups.update(group_ancestors)

    # "all" is implicit for every host and carries no information
    for host in inventory.hosts.values():
        host.direct_groups.discard("all")
        host.all_groups.discard("all")

    return inventory


def _inventory_files(directory: Path) -> list[Path]:
    """Return the YAML inventory files contained directly in ``directory``.

    Args:
        directory: Directory to scan.

    Returns:
        Sorted list of ``.yml``/``.yaml`` files (hidden files are skipped).
    """
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in INVENTORY_SUFFIXES and not p.name.startswith(".")
    )


def discover_inventories(path: Path, locations: list[str] | None) -> list[tuple[Path, str, str]]:
    """Find inventory files and derive location and environment for each one.

    ``path`` can be:

    * a single inventory file: location is the parent directory name;
    * an ``inventories`` root directory containing one sub directory per
      location, each with one ``<env>.yml`` file per environment;
    * a single location directory containing ``<env>.yml`` files.

    Args:
        path: Inventory file or directory.
        locations: Optional list of location names to keep (directory mode only).

    Returns:
        A list of ``(file, location, env)`` tuples.

    Raises:
        CompareError: If the path does not exist or no inventory is found.
    """
    if not path.exists():
        raise CompareError(f"inventory path {path} does not exist")

    found: list[tuple[Path, str, str]] = []
    if path.is_file():
        found.append((path, path.resolve().parent.name, path.stem.lower()))
    else:
        # Files placed directly in the given directory: it is a location dir
        for file in _inventory_files(path):
            found.append((file, path.resolve().name, file.stem.lower()))
        # Sub directories: one per location
        for sub in sorted(path.iterdir()):
            if not sub.is_dir() or sub.name.startswith(".") or sub.name in NON_INVENTORY_DIRS:
                continue
            for file in _inventory_files(sub):
                found.append((file, sub.name, file.stem.lower()))

        if locations:
            wanted = set(locations)
            found = [item for item in found if item[1] in wanted]

    if not found:
        raise CompareError(f"no inventory file found in {path}")
    return found


# ---------------------------------------------------------------------------
# Proxmox
# ---------------------------------------------------------------------------

def connect_proxmox(args: argparse.Namespace) -> Any:
    """Open a connection to the Proxmox VE API.

    Authentication uses an API token when ``--token-name`` is given,
    otherwise user and password (prompted if not provided).

    Args:
        args: Parsed command line arguments.

    Returns:
        A ``proxmoxer.ProxmoxAPI`` instance.

    Raises:
        CompareError: On missing parameters or connection/authentication errors.
    """
    try:
        from proxmoxer import ProxmoxAPI
    except ImportError as exc:
        raise CompareError("proxmoxer is required (pip install -r requirements.txt)") from exc

    if not args.host:
        raise CompareError("Proxmox host is required (--host or PVE_HOST)")
    if not args.user:
        raise CompareError("Proxmox user is required (--user or PVE_USER), e.g. root@pam")

    if not args.verify_ssl:
        # Avoid one warning per request when certificate validation is disabled
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except ImportError:
            pass

    common = {
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "verify_ssl": args.verify_ssl,
        "timeout": args.timeout,
    }
    try:
        if args.token_name:
            if not args.token_value:
                raise CompareError("--token-value (or PVE_TOKEN_VALUE) is required with --token-name")
            return ProxmoxAPI(token_name=args.token_name, token_value=args.token_value, **common)
        password = args.password or getpass.getpass(f"Password for {args.user}@{args.host}: ")
        return ProxmoxAPI(password=password, **common)
    except CompareError:
        raise
    except Exception as exc:  # proxmoxer raises several exception types
        raise CompareError(f"cannot connect to Proxmox at {args.host}: {exc}") from exc


def fetch_resources(api: Any) -> list[dict[str, Any]]:
    """Retrieve all cluster resources (guests and nodes) with a single API call.

    Args:
        api: A ``proxmoxer.ProxmoxAPI`` instance.

    Returns:
        The raw list returned by ``GET /cluster/resources``.

    Raises:
        CompareError: If the API call fails.
    """
    try:
        return list(api.cluster.resources.get())
    except Exception as exc:
        raise CompareError(f"cannot read cluster resources: {exc}") from exc


def load_resources_file(path: Path) -> list[dict[str, Any]]:
    """Load cluster resources from a JSON file instead of the API.

    The file must contain the output of
    ``pvesh get /cluster/resources --output-format json`` (a list), or an
    object with that list under the ``data`` key (raw API response).

    Args:
        path: Path of the JSON file.

    Returns:
        The list of resources.

    Raises:
        CompareError: If the file cannot be read or has an unexpected format.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CompareError(f"cannot load resources file {path}: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        raise CompareError(f"{path}: expected a list of cluster resources")
    return data


def parse_tags(raw: Any) -> list[str]:
    """Split the Proxmox ``tags`` field into a list.

    Proxmox stores tags separated by ``;`` (older versions also accepted
    ``,`` and spaces).

    Args:
        raw: The raw tags value (string, list or None).

    Returns:
        The list of non empty tags.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t for t in re.split(r"[;,\s]+", str(raw)) if t]


def resources_to_guests(resources: Iterable[dict[str, Any]]) -> tuple[list[Guest], list[Guest]]:
    """Convert raw cluster resources into guests and nodes.

    Args:
        resources: Items returned by ``/cluster/resources``.

    Returns:
        A ``(guests, nodes)`` tuple; storages, pools and other resource
        types are ignored.
    """
    guests: list[Guest] = []
    nodes: list[Guest] = []
    for item in resources:
        kind = item.get("type")
        if kind in ("qemu", "lxc"):
            vmid = item.get("vmid")
            guests.append(Guest(
                name=str(item.get("name") or f"vm{vmid}"),
                kind=kind,
                node=str(item.get("node", "")),
                vmid=int(vmid) if vmid is not None else None,
                status=str(item.get("status", "")),
                tags=parse_tags(item.get("tags")),
                template=bool(int(item.get("template") or 0)),
            ))
        elif kind == "node":
            name = str(item.get("node", ""))
            nodes.append(Guest(name=name, kind="node", node=name, status=str(item.get("status", ""))))
    return guests, nodes


# ---------------------------------------------------------------------------
# Environment resolution and name matching
# ---------------------------------------------------------------------------

def build_env_tag_regex(tag_format: str) -> re.Pattern[str]:
    """Build the regular expression used to extract the environment from a tag.

    Args:
        tag_format: Tag format containing the ``{env}`` placeholder exactly
            once, e.g. ``env.{env}``.

    Returns:
        A compiled, case-insensitive regex with an ``env`` named group.

    Raises:
        CompareError: If the placeholder is missing or repeated.
    """
    parts = tag_format.split("{env}")
    if len(parts) != 2:
        raise CompareError("--env-tag-format must contain the {env} placeholder exactly once")
    return re.compile("^" + re.escape(parts[0]) + r"(?P<env>.+?)" + re.escape(parts[1]) + "$",
                      re.IGNORECASE)


def envs_from_tags(tags: list[str], tag_regex: re.Pattern[str], aliases: dict[str, str]) -> list[str]:
    """Extract the environment names declared in a list of tags.

    Args:
        tags: Tags of a guest.
        tag_regex: Regex built by :func:`build_env_tag_regex`.
        aliases: Mapping from tag environment to inventory environment.

    Returns:
        The (lowercase, de-duplicated) list of environments, in tag order.
    """
    envs: list[str] = []
    for tag in tags:
        match = tag_regex.match(tag)
        if match:
            env = match.group("env").lower()
            env = aliases.get(env, env)
            if env not in envs:
                envs.append(env)
    return envs


def short_name(name: str) -> str:
    """Return the first DNS label of a host name, lowercase."""
    return name.split(".", 1)[0].lower()


def names_match(guest_name: str, host_name: str, mode: str) -> bool:
    """Tell whether a Proxmox guest name and an inventory host name refer to the same host.

    Modes:
        * ``exact``: case-insensitive comparison of the full names;
        * ``short``: comparison of the first DNS label only;
        * ``auto``: full comparison when both names are FQDNs, short name
          comparison when at least one of them is a short name.

    Args:
        guest_name: Name of the Proxmox guest.
        host_name: Name of the inventory host.
        mode: Matching mode.

    Returns:
        True if the names match.
    """
    a, b = guest_name.lower(), host_name.lower()
    if mode == "exact":
        return a == b
    if mode == "short":
        return short_name(a) == short_name(b)
    # auto
    if "." in a and "." in b:
        return a == b
    return short_name(a) == short_name(b)


def find_matches(name: str, inventories: list[Inventory], mode: str) -> list[Match]:
    """Find every inventory host matching ``name`` in the given inventories.

    Args:
        name: Guest name.
        inventories: Inventories to search.
        mode: Matching mode (see :func:`names_match`).

    Returns:
        The list of matches (possibly empty).
    """
    return [
        Match(inventory=inv, host=host)
        for inv in inventories
        for host in inv.hosts.values()
        if names_match(name, host.name, mode)
    ]


def matches_any(name: str, patterns: list[str]) -> bool:
    """Return True if ``name`` matches any of the shell-style glob ``patterns`` (case-insensitive)."""
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def select_guests(guests: list[Guest], nodes: list[Guest],
                  args: argparse.Namespace) -> tuple[list[Guest], list[Excluded]]:
    """Resolve environments and split guests into checked and excluded ones.

    Args:
        guests: QEMU/LXC guests from the cluster.
        nodes: Proxmox cluster nodes.
        args: Parsed command line arguments.

    Returns:
        A ``(checked, excluded)`` tuple.
    """
    tag_regex = build_env_tag_regex(args.env_tag_format)
    only_envs = {e.lower() for e in args.only_env} if args.only_env else None
    checked: list[Guest] = []
    excluded: list[Excluded] = []

    for guest in guests:
        # Order matters: the first applicable reason is reported
        if guest.template and not args.include_templates:
            excluded.append(Excluded(guest, "template"))
            continue
        if args.exclude and matches_any(guest.name, args.exclude):
            excluded.append(Excluded(guest, "excluded by name"))
            continue
        if args.exclude_tag and set(t.lower() for t in guest.tags) & {t.lower() for t in args.exclude_tag}:
            excluded.append(Excluded(guest, "excluded by tag"))
            continue
        if args.skip_stopped and guest.status != "running":
            excluded.append(Excluded(guest, f"not running ({guest.status or 'unknown'})"))
            continue

        guest.envs = envs_from_tags(guest.tags, tag_regex, args.env_aliases)
        if guest.envs:
            guest.env_source = "tag"
        elif args.default_env:
            guest.envs = [args.env_aliases.get(args.default_env.lower(), args.default_env.lower())]
            guest.env_source = "default"
        else:
            excluded.append(Excluded(guest, "no environment tag"))
            continue

        if only_envs is not None and not only_envs.intersection(guest.envs):
            excluded.append(Excluded(guest, "environment not selected"))
            continue
        checked.append(guest)

    # Proxmox nodes are checked only on request, all in the given environment
    if args.include_nodes:
        env = args.env_aliases.get(args.include_nodes.lower(), args.include_nodes.lower())
        for node in nodes:
            if args.exclude and matches_any(node.name, args.exclude):
                excluded.append(Excluded(node, "excluded by name"))
                continue
            node.envs = [env]
            node.env_source = "nodes-option"
            checked.append(node)

    return checked, excluded


def compare(checked: list[Guest], inventories: list[Inventory], mode: str) -> list[GuestResult]:
    """Check every guest against the inventories.

    Args:
        checked: Guests to check (with resolved environments).
        inventories: Parsed inventories.
        mode: Name matching mode.

    Returns:
        One result per guest.
    """
    known_envs = {inv.env for inv in inventories}
    results: list[GuestResult] = []
    for guest in checked:
        matches = find_matches(guest.name, inventories, mode)
        expected = [m for m in matches if m.inventory.env in guest.envs]
        others = [m for m in matches if m.inventory.env not in guest.envs]
        if expected:
            status = STATUS_OK
        elif others:
            status = STATUS_WRONG_ENV
        else:
            status = STATUS_MISSING

        result = GuestResult(guest=guest, status=status, expected=expected, others=others)
        for env in guest.envs:
            if env not in known_envs:
                result.notes.append(f"no inventory file for environment '{env}'")
        if len(guest.envs) > 1:
            result.notes.append("multiple environment tags: " + ", ".join(guest.envs))
        results.append(result)
    return results


def find_orphans(inventories: list[Inventory], cluster_names: list[str], mode: str,
                 exclude: list[str]) -> list[Match]:
    """Find inventory hosts that do not correspond to any cluster resource.

    Every guest and node of the cluster is considered, including the ones
    excluded from the check: a host is orphan only if nothing on the cluster
    matches it.

    Args:
        inventories: Parsed inventories.
        cluster_names: Names of every guest and node of the cluster.
        mode: Name matching mode.
        exclude: Glob patterns of inventory hosts to ignore.

    Returns:
        The orphan hosts with their inventory.
    """
    orphans: list[Match] = []
    for inv in inventories:
        for host in inv.hosts.values():
            if exclude and matches_any(host.name, exclude):
                continue
            if not any(names_match(name, host.name, mode) for name in cluster_names):
                orphans.append(Match(inventory=inv, host=host))
    return orphans


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class Colors:
    """ANSI color helper; every method returns the text unchanged when disabled."""

    def __init__(self, enabled: bool) -> None:
        """Initialize the helper.

        Args:
            enabled: Whether ANSI escape sequences should be emitted.
        """
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        """Wrap ``text`` in the given ANSI code if colors are enabled."""
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        """Return bold text."""
        return self._wrap("1", text)

    def green(self, text: str) -> str:
        """Return green text."""
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        """Return yellow text."""
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        """Return red text."""
        return self._wrap("31", text)

    def dim(self, text: str) -> str:
        """Return dimmed text."""
        return self._wrap("2", text)


def format_groups(host: InventoryHost, direct_only: bool) -> str:
    """Format the groups of an inventory host.

    Inherited groups (ancestors of the direct groups) are shown in
    parentheses, unless ``direct_only`` is set.

    Args:
        host: The inventory host.
        direct_only: Show only the groups the host is explicitly listed in.

    Returns:
        A comma separated list, or ``ungrouped``.
    """
    direct = sorted(host.direct_groups)
    inherited = [] if direct_only else sorted(host.all_groups - host.direct_groups)
    parts = direct + [f"({g})" for g in inherited]
    return ", ".join(parts) if parts else "ungrouped"


def describe_guest(guest: Guest) -> str:
    """Return a one line description of a guest (id, type, node, status, env)."""
    details = []
    if guest.vmid is not None:
        details.append(f"vmid {guest.vmid}")
    details.append(guest.kind)
    if guest.kind != "node":
        details.append(f"node {guest.node}")
    if guest.status:
        details.append(guest.status)
    if guest.envs:
        env = "/".join(guest.envs)
        details.append(f"env {env}" + (" [default]" if guest.env_source == "default" else ""))
    return f"{guest.name} ({', '.join(details)})"


def print_text_report(report: Report, args: argparse.Namespace) -> None:
    """Print the human readable report on stdout.

    Args:
        report: The comparison report.
        args: Parsed command line arguments.
    """
    c = Colors(args.color)

    def section(title: str, count: int) -> None:
        """Print a section header."""
        print()
        print(c.bold(f"=== {title} ({count}) ==="))

    def print_matches(matches: list[Match], color) -> None:
        """Print the inventories/groups in which a guest was found."""
        for m in matches:
            print(f"      {color(m.inventory.label)}: {m.host.name} "
                  f"[{format_groups(m.host, args.direct_groups_only)}]")

    def print_notes(result: GuestResult) -> None:
        """Print the notes attached to a result."""
        for note in result.notes:
            print(c.dim(f"      note: {note}"))

    print(c.bold("Proxmox source: ") + report.source)
    print(c.bold("Inventories:    ") + ", ".join(inv.label for inv in report.inventories))
    for warning in report.warnings:
        print(c.yellow(f"WARNING: {warning}"))

    ok = report.by_status(STATUS_OK)
    wrong = report.by_status(STATUS_WRONG_ENV)
    missing = report.by_status(STATUS_MISSING)

    if not args.hide_ok:
        section("Guests found in the inventory of their environment", len(ok))
        for r in ok:
            print("  " + c.green("OK   ") + describe_guest(r.guest))
            print_matches(r.expected, c.green)
            if r.others:
                print(c.dim("      also in other environments:"))
                print_matches(r.others, c.dim)
            print_notes(r)

    section("Guests found only in inventories of other environments", len(wrong))
    for r in wrong:
        print("  " + c.yellow("ENV  ") + describe_guest(r.guest))
        print_matches(r.others, c.yellow)
        print_notes(r)

    section("Guests not found in any inventory", len(missing))
    for r in missing:
        print("  " + c.red("MISS ") + describe_guest(r.guest))
        print_notes(r)

    if report.orphans is not None:
        section("Inventory hosts not found on the Proxmox cluster", len(report.orphans))
        for m in report.orphans:
            print(f"  {c.red('ORPH ')}{m.inventory.label}: {m.host.name} "
                  f"[{format_groups(m.host, args.direct_groups_only)}]")

    if report.excluded and not args.hide_excluded:
        section("Excluded guests (not checked)", len(report.excluded))
        for e in report.excluded:
            print(f"  {c.dim('SKIP ')}{describe_guest(e.guest)}: {e.reason}")

    # Final summary
    print()
    print(c.bold("=== Summary ==="))
    print(f"  Checked guests:             {len(report.results)}")
    print(f"  {c.green('Found in right inventory:')}   {len(ok)}")
    print(f"  {c.yellow('Found in other env only:')}    {len(wrong)}")
    print(f"  {c.red('Not in any inventory:')}       {len(missing)}")
    if report.orphans is not None:
        print(f"  {c.red('Orphan inventory hosts:')}     {len(report.orphans)}")
    print(f"  Excluded guests:            {len(report.excluded)}")


def report_to_dict(report: Report) -> dict[str, Any]:
    """Convert a report into a JSON serializable dictionary.

    Args:
        report: The comparison report.

    Returns:
        A dictionary suitable for ``json.dumps``.
    """

    def guest_dict(guest: Guest) -> dict[str, Any]:
        """Serialize a guest."""
        return {
            "name": guest.name, "vmid": guest.vmid, "type": guest.kind, "node": guest.node,
            "status": guest.status, "tags": guest.tags, "template": guest.template,
            "envs": guest.envs, "env_source": guest.env_source or None,
        }

    def match_dict(match: Match) -> dict[str, Any]:
        """Serialize a match."""
        return {
            "inventory": str(match.inventory.path), "location": match.inventory.location,
            "env": match.inventory.env, "host": match.host.name,
            "groups": sorted(match.host.direct_groups),
            "all_groups": sorted(match.host.all_groups),
        }

    data: dict[str, Any] = {
        "source": report.source,
        "inventories": [
            {"path": str(i.path), "location": i.location, "env": i.env, "hosts": len(i.hosts)}
            for i in report.inventories
        ],
        "warnings": report.warnings,
        "results": [
            {
                **guest_dict(r.guest), "result": r.status,
                "expected": [match_dict(m) for m in r.expected],
                "others": [match_dict(m) for m in r.others],
                "notes": r.notes,
            }
            for r in report.results
        ],
        "excluded": [{**guest_dict(e.guest), "reason": e.reason} for e in report.excluded],
        "summary": {
            "checked": len(report.results),
            STATUS_OK: len(report.by_status(STATUS_OK)),
            STATUS_WRONG_ENV: len(report.by_status(STATUS_WRONG_ENV)),
            STATUS_MISSING: len(report.by_status(STATUS_MISSING)),
            "excluded": len(report.excluded),
        },
    }
    if report.orphans is not None:
        data["orphans"] = [match_dict(m) for m in report.orphans]
        data["summary"]["orphans"] = len(report.orphans)
    return data


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_env_aliases(values: list[str]) -> dict[str, str]:
    """Parse ``--env-alias TAG_ENV=INVENTORY_ENV`` values.

    Args:
        values: Raw option values.

    Returns:
        A lowercase mapping from tag environment to inventory environment.

    Raises:
        CompareError: If a value is not in the ``a=b`` form.
    """
    aliases: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise CompareError(f"invalid --env-alias '{value}', expected TAG_ENV=INVENTORY_ENV")
        src, dst = value.split("=", 1)
        aliases[src.strip().lower()] = dst.strip().lower()
    return aliases


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean from an environment variable (1/true/yes/on are true)."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        The configured ``argparse.ArgumentParser``.
    """
    parser = argparse.ArgumentParser(
        description="Compare Proxmox VE guests (VMs and containers) with the hosts "
                    "declared in Ansible inventories.",
        epilog="Exit codes: 0 = no discrepancies, 1 = discrepancies found, 2 = error.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    conn = parser.add_argument_group("Proxmox connection")
    conn.add_argument("--host", default=os.environ.get("PVE_HOST"),
                      help="Proxmox host name or address of any cluster node (env: PVE_HOST)")
    conn.add_argument("--port", type=int, default=int(os.environ.get("PVE_PORT", "8006")),
                      help="Proxmox API port (env: PVE_PORT, default: 8006)")
    conn.add_argument("--user", default=os.environ.get("PVE_USER"),
                      help="user with realm, e.g. root@pam; also the token owner (env: PVE_USER)")
    conn.add_argument("--password", default=os.environ.get("PVE_PASSWORD"),
                      help="password; prompted if neither password nor token is given "
                           "(env: PVE_PASSWORD, preferred over the command line)")
    conn.add_argument("--token-name", default=os.environ.get("PVE_TOKEN_NAME"),
                      help="API token ID (the part after '!'), enables token auth (env: PVE_TOKEN_NAME)")
    conn.add_argument("--token-value", default=os.environ.get("PVE_TOKEN_VALUE"),
                      help="API token secret (env: PVE_TOKEN_VALUE, preferred over the command line)")
    conn.add_argument("--insecure", dest="verify_ssl", action="store_false",
                      default=env_flag("PVE_VERIFY_SSL", True),
                      help="do not verify the TLS certificate (env: PVE_VERIFY_SSL=0)")
    conn.add_argument("--timeout", type=int, default=30, help="API timeout in seconds (default: 30)")
    conn.add_argument("--resources-file", type=Path, metavar="FILE",
                      help="read cluster resources from a JSON file (output of "
                           "'pvesh get /cluster/resources --output-format json') instead of the API")

    inv = parser.add_argument_group("Ansible inventories")
    inv.add_argument("-i", "--inventory", type=Path, required=True,
                     help="inventories root directory (<root>/<location>/<env>.yml), "
                          "a location directory or a single inventory file")
    inv.add_argument("-l", "--location", action="append", default=[],
                     help="only use inventories of this location (repeatable); "
                          "recommended when the root holds several clusters")

    chk = parser.add_argument_group("Check options")
    chk.add_argument("--env-tag-format", default="env.{env}",
                     help="format of the environment tag, {env} is the placeholder "
                          "(default: env.{env})")
    chk.add_argument("--default-env", metavar="ENV",
                     help="environment for guests without an environment tag "
                          "(default: such guests are excluded and listed at the end)")
    chk.add_argument("--env-alias", action="append", default=[], metavar="TAG_ENV=INV_ENV",
                     help="map an environment from the tag to an inventory name, "
                          "e.g. production=prod (repeatable)")
    chk.add_argument("--only-env", action="append", default=[], metavar="ENV",
                     help="only check guests of this environment (repeatable)")
    chk.add_argument("--include-nodes", metavar="ENV",
                     help="also check the Proxmox nodes, expected in the inventory of ENV "
                          "(default: nodes are not checked)")
    chk.add_argument("--include-templates", action="store_true",
                     help="also check templates (default: excluded)")
    chk.add_argument("--skip-stopped", action="store_true",
                     help="exclude guests that are not running")
    chk.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                     help="exclude guests whose name matches the glob pattern (repeatable)")
    chk.add_argument("--exclude-tag", action="append", default=[], metavar="TAG",
                     help="exclude guests having this tag, e.g. no-ansible (repeatable)")
    chk.add_argument("--match", choices=("auto", "exact", "short"), default="auto",
                     help="name matching: exact = full name, short = first DNS label only, "
                          "auto = full name if both are FQDNs, else short name (default: auto)")
    chk.add_argument("--orphans", action="store_true",
                     help="also report inventory hosts that do not exist on the cluster")
    chk.add_argument("--orphans-exclude", action="append", default=[], metavar="GLOB",
                     help="ignore inventory hosts matching the glob when looking for orphans (repeatable)")

    out = parser.add_argument_group("Output")
    out.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    out.add_argument("--hide-ok", action="store_true",
                     help="do not list guests found in the right inventory")
    out.add_argument("--hide-excluded", action="store_true", help="do not list excluded guests")
    out.add_argument("--direct-groups-only", action="store_true",
                     help="show only the groups a host is directly listed in, not inherited ones")
    out.add_argument("--no-color", dest="color", action="store_false", default=None,
                     help="disable colored output (also honors NO_COLOR)")
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the comparison and print the report.

    Args:
        args: Parsed command line arguments.

    Returns:
        The process exit code.
    """
    args.env_aliases = parse_env_aliases(args.env_alias)
    if args.color is None:
        args.color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    build_env_tag_regex(args.env_tag_format)  # validate early

    # Load inventories
    inventories = [parse_inventory(path, location, env)
                   for path, location, env in discover_inventories(args.inventory, args.location)]

    # Load cluster resources
    if args.resources_file:
        resources = load_resources_file(args.resources_file)
        source = str(args.resources_file)
    else:
        resources = fetch_resources(connect_proxmox(args))
        source = f"{args.host}:{args.port}"
    guests, nodes = resources_to_guests(resources)

    warnings: list[str] = []
    locations = sorted({inv.location for inv in inventories})
    if len(locations) > 1:
        warnings.append(f"inventories of several locations are used ({', '.join(locations)}); "
                        "consider --location to restrict the check to this cluster")

    checked, excluded = select_guests(guests, nodes, args)
    results = compare(checked, inventories, args.match)

    orphans = None
    if args.orphans:
        cluster_names = [g.name for g in guests] + [n.name for n in nodes]
        orphans = find_orphans(inventories, cluster_names, args.match, args.orphans_exclude)
        orphans.sort(key=lambda m: (m.inventory.label, m.host.name))

    results.sort(key=lambda r: r.guest.name.lower())
    excluded.sort(key=lambda e: (e.reason, e.guest.name.lower()))
    report = Report(source=source, inventories=inventories, results=results,
                    excluded=excluded, orphans=orphans, warnings=warnings)

    if args.format == "json":
        print(json.dumps(report_to_dict(report), indent=2))
    else:
        print_text_report(report, args)

    return EXIT_DISCREPANCIES if report.has_discrepancies else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Program entry point.

    Args:
        argv: Command line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except CompareError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted\n")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
