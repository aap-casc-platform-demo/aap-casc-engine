#!/usr/bin/env python3
"""Shared CasC CI runtime helpers for GitHub and GitLab pipelines.

Token-only credential model. No username/password or fixed AAP_<ENV>_* paths.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml

TENANT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Removed topology fields — fail closed with migration guidance (allowed grep hits).
LEGACY_TENANT_FIELDS = frozenset(
    {
        "repo_pattern",
        "repo_names",
        "repositories",
        "repo_by_folder",
        "resource_type",
    }
)
LEGACY_CONFIG_FIELDS = frozenset(
    {
        "platform_repo_pattern",
        "platform_repo_names",
        "platform_repos",
        "repo_mode",  # Genesis launch-time only; must not persist in config.yml
    }
)

DEFAULT_JT = {
    "genesis": "jt-platform-genesis",
    "bootstrap": "jt-platform-bootstrap_tenant",
    "dispatcher": "jt-platform-casc_dispatcher",
    "drift_detection": "jt-platform-drift_detection",
}


class UnsafeString(str):
    """Preserve Ansible !unsafe scalars through load/merge/dump."""


def _unsafe_constructor(loader: yaml.Loader, node: yaml.Node) -> UnsafeString:
    return UnsafeString(loader.construct_scalar(node))


def _unsafe_representer(dumper: yaml.Dumper, data: UnsafeString) -> yaml.Node:
    return dumper.represent_scalar("!unsafe", str(data))


class CascLoader(yaml.SafeLoader):
    pass


class CascDumper(yaml.SafeDumper):
    pass


CascLoader.add_constructor("!unsafe", _unsafe_constructor)
CascDumper.add_representer(UnsafeString, _unsafe_representer)


def load_yaml_document(path: str) -> Any:
    """Load YAML with CascLoader (!unsafe-aware). Any document root is allowed."""
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=CascLoader)


def load_yaml_file(path: str) -> dict[str, Any]:
    data = load_yaml_document(path) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def dump_yaml(data: Any) -> str:
    """Consumer-facing dump — preserve author key order."""
    return yaml.dump(
        data,
        Dumper=CascDumper,
        sort_keys=False,
        default_flow_style=False,
    )


def dump_yaml_identity(data: Any) -> str:
    """Canonical marker for exact-dict uniqueness (key-order insensitive).

    Uses CascDumper so !unsafe scalars remain distinct from plain strings,
    with sort_keys=True so semantically identical mappings dedupe.
    """
    return yaml.dump(
        data,
        Dumper=CascDumper,
        sort_keys=True,
        default_flow_style=False,
    )


def json_ready(value: Any) -> Any:
    """Convert Casc YAML values (including UnsafeString) into JSON-serializable form."""
    if isinstance(value, UnsafeString):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


EXCLUDED_RESOURCE_DIRS = {
    ".git",
    ".github",
    ".schemas",
    ".engine",
    ".engine-runtime",
    ".scripts",
    ".control",
    ".aap-casc-engine",
}
EXCLUDED_RESOURCE_FILES = {"config.yml", "tenants.yml", "naming-rules.yml"}
ENV_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def resolve_control_config_path(root: str, control_config: str = "") -> str:
    """Resolve pinned control config used for env_branch_map directory scope.

    An explicitly supplied --control-config path is authoritative and must exist.
    With no explicit argument, only <root>/.control/config.yml is accepted.
    """
    if control_config:
        if not os.path.isfile(control_config):
            raise ValueError(
                f"Pinned control config not found: {control_config}"
            )
        return control_config

    pinned = os.path.join(root, ".control", "config.yml")
    if os.path.isfile(pinned):
        return pinned
    return ""


def load_env_names(root: str, control_config: str = "") -> list[str]:
    """Return env_branch_map keys (environment directory names) from pinned control config."""
    path = resolve_control_config_path(root, control_config)
    if not path:
        raise ValueError(
            "Pinned control config with env_branch_map is required to resolve "
            "desired-state directories (.control/config.yml)"
        )
    cfg = load_yaml_file(path)
    ebm = cfg.get("env_branch_map")
    if not isinstance(ebm, dict) or not ebm:
        raise ValueError(f"{path}: env_branch_map must be a non-empty mapping")
    names: list[str] = []
    for key in ebm:
        if not isinstance(key, str) or not ENV_NAME_RE.fullmatch(key):
            raise ValueError(
                f"{path}: env_branch_map key {key!r} must match ^[a-z][a-z0-9_]*$"
            )
        names.append(key)
    return names


def require_base_directory(root: str, caller_role: str = "tenant") -> None:
    """Fail closed when platform/tenant repos lack base/ (control exempt)."""
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        return
    if not os.path.isdir(os.path.join(root, "base")):
        raise ValueError(
            "Platform/tenant repositories require a base/ directory; "
            "flat-root desired state is not supported"
        )


def desired_state_search_dirs(
    root: str, control_config: str = "", caller_role: str = "tenant"
) -> list[str]:
    """Return only base/ plus env_branch_map environment directories that exist.

    Unrelated top-level directories (docs/, governance/, etc.) are never scanned.
    Platform/tenant callers require base/; control callers are exempt.
    """
    require_base_directory(root, caller_role=caller_role)
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        return []
    dirs: list[str] = ["base"]
    for env_name in load_env_names(root, control_config):
        if os.path.isdir(os.path.join(root, env_name)):
            dirs.append(env_name)
    return dirs


def iter_resource_yaml_files(
    root: str, caller_role: str = "tenant", control_config: str = ""
) -> list[str]:
    """Return desired-state resource YAML for platform/tenant callers.

    Control repositories hold control metadata (config.yml, tenants.yml, optional
    naming-rules.yml), not AAP desired state. Arbitrary root YAML in a control
    repo must not be treated as CasC resources.

    Platform/tenant scans are limited to base/ and env_branch_map environment
    directories from the pinned control config.
    """
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        return []

    require_base_directory(root, caller_role=role)
    paths: list[str] = []
    for search_dir in desired_state_search_dirs(
        root, control_config=control_config, caller_role=role
    ):
        start = os.path.join(root, search_dir)
        for current, dirs, files in os.walk(start):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_RESOURCE_DIRS]
            for name in files:
                if name in EXCLUDED_RESOURCE_FILES or name.endswith(".sample"):
                    continue
                if not name.endswith((".yml", ".yaml")):
                    continue
                paths.append(os.path.join(current, name))
    return sorted(paths)


def validate_catalog_schema(resource_types: dict[str, Any]) -> None:
    """Fail closed on catalog shape errors shared by CI and runtime."""
    defaults = resource_types.get("defaults") or {}
    exceptions = resource_types.get("exceptions") or {}
    unsupported = resource_types.get("unsupported") or {}
    if not isinstance(defaults, dict) or not isinstance(exceptions, dict):
        raise ValueError("resource-types.yml defaults/exceptions must be mappings")
    if not isinstance(unsupported, dict):
        raise ValueError("resource-types.yml unsupported must be a mapping")

    for key, meta in exceptions.items():
        if not isinstance(meta, dict):
            raise ValueError(f'resource-types.yml exceptions["{key}"] must be a mapping')
        merge_mode = meta.get("merge_mode", defaults.get("merge_mode"))
        value_type = meta.get("value_type", defaults.get("value_type", "list"))
        if merge_mode == "raw":
            if value_type != "raw":
                raise ValueError(
                    f'resource-types.yml exceptions["{key}"] merge_mode "raw" '
                    f'requires value_type: raw (got {value_type!r})'
                )
        elif merge_mode == "keyed":
            if value_type != "list":
                raise ValueError(
                    f'resource-types.yml exceptions["{key}"] merge_mode "keyed" '
                    f'requires value_type: list (got {value_type!r})'
                )
            if meta.get("identity_scalar", defaults.get("identity_scalar", True)) is not True:
                raise ValueError(
                    f'resource-types.yml exceptions["{key}"] keyed types require '
                    f"identity_scalar: true"
                )
            id_field = meta.get("identity_field", defaults.get("identity_field", "name"))
            if not isinstance(id_field, str) or not id_field:
                raise ValueError(
                    f'resource-types.yml exceptions["{key}"] keyed types require '
                    f"identity_field"
                )
        elif merge_mode == "atomic":
            if value_type != "list":
                raise ValueError(
                    f'resource-types.yml exceptions["{key}"] merge_mode "atomic" '
                    f'requires value_type: list (got {value_type!r})'
                )
        else:
            raise ValueError(
                f'resource-types.yml exceptions["{key}"] merge_mode must be '
                f'"keyed", "raw", or "atomic" (got {merge_mode!r})'
            )

    for key, meta in unsupported.items():
        if not isinstance(meta, dict) or not str(meta.get("reason") or "").strip():
            raise ValueError(
                f'resource-types.yml unsupported["{key}"] requires a non-empty reason'
            )
        if meta.get("merge_mode") in {"keyed", "raw", "atomic"}:
            raise ValueError(
                f'resource-types.yml unsupported["{key}"] must not assign a supported '
                f"merge_mode"
            )
        if key in exceptions:
            raise ValueError(
                f'resource-types.yml key "{key}" cannot be both supported and unsupported'
            )


def resolve_allowed_keys(
    resource_types: dict[str, Any],
    root: str,
    allowed_keys_path: str = "",
) -> set[str]:
    exceptions = resource_types.get("exceptions") or {}
    catalog_keys = set(exceptions)
    key_candidates = []
    if allowed_keys_path:
        key_candidates.append(allowed_keys_path)
    key_candidates.extend(
        [
            os.path.join(root, ".schemas", "engine_defaults.yml"),
            os.path.join(
                root, ".engine", "roles", "process_casc_config", "defaults", "main.yml"
            ),
        ]
    )
    allowed_keys = None
    for candidate in key_candidates:
        if candidate and os.path.exists(candidate):
            allowed_keys = set(
                (load_yaml_file(candidate).get("casc_allowed_resource_keys") or [])
            )
            break
    if allowed_keys is None:
        return catalog_keys
    if allowed_keys != catalog_keys:
        missing = sorted(catalog_keys - allowed_keys)
        extra = sorted(allowed_keys - catalog_keys)
        raise ValueError(
            "casc_allowed_resource_keys must match resource-types.yml exceptions "
            f"exactly. missing_from_allowlist={missing} "
            f"extra_in_allowlist={extra}"
        )
    return allowed_keys


def _resource_meta(resource_types: dict[str, Any], key: str) -> dict[str, Any]:
    defaults = resource_types.get("defaults") or {}
    exceptions = resource_types.get("exceptions") or {}
    meta = dict(defaults)
    meta.update(exceptions.get(key) or {})
    return meta


def _exact_unique(items: list[Any]) -> list[Any]:
    """Preserve order; drop exact duplicate mappings/scalars."""
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        marker = dump_yaml_identity(item)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def _iter_layer_files(layer_root: str) -> list[tuple[str, str]]:
    """Return (relpath, abspath) for YAML under layer_root, excluding samples."""
    if not os.path.isdir(layer_root):
        return []
    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(layer_root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in EXCLUDED_RESOURCE_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.endswith((".yml", ".yaml")):
                continue
            if name.endswith(".sample") or name in EXCLUDED_RESOURCE_FILES:
                continue
            abspath = os.path.join(dirpath, name)
            relpath = os.path.relpath(abspath, layer_root).replace("\\", "/")
            found.append((relpath, abspath))
    return found


def _load_layer_documents(
    layer_root: str,
    allowed_keys: set[str],
    unsupported: dict[str, Any],
) -> dict[str, tuple[str, str, Any]]:
    """Map relative path -> (abspath, resource_key, value)."""
    docs: dict[str, tuple[str, str, Any]] = {}
    for relpath, abspath in _iter_layer_files(layer_root):
        data = load_yaml_file(abspath)
        if len(data) != 1:
            raise ValueError(
                f"{abspath}: Expected exactly 1 top-level key, got {list(data)}"
            )
        key, value = next(iter(data.items()))
        if key in unsupported:
            raise ValueError(
                f'{abspath}: Unsupported resource key "{key}" — '
                f'{unsupported[key].get("reason", "unsupported")}'
            )
        if key not in allowed_keys:
            raise ValueError(f'{abspath}: Unknown resource key "{key}"')
        docs[relpath] = (abspath, key, value)
    return docs


def _validate_list_items(
    key: str,
    value: Any,
    *,
    abspath: str,
    merge_mode: str,
    id_field: str,
    naming_supported: bool,
) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(
            f'{abspath}: Key "{key}" expected list, got {type(value).__name__}'
        )
    require_identity = merge_mode == "keyed" or (
        merge_mode == "atomic" and naming_supported
    )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(
                f'{abspath}: Item {index} in "{key}" must be a mapping, '
                f"got {type(item).__name__}"
            )
        if require_identity and id_field not in item:
            raise ValueError(
                f'{abspath}: Item {index} in "{key}" missing identity field '
                f'"{id_field}"'
            )
    return value


def merge_desired_state(
    root: str,
    env_filter: str,
    resource_types_path: str,
    allowed_keys_path: str = "",
) -> dict[str, Any]:
    """Merge base/ + <env>/ using the shared catalog contract (CI + runtime).

    Path ownership: a relative path may contribute only one resource key across
    base and env. Env replaces base at the same relative path.

    keyed: identity overlay with hard uniqueness across the complete layer
    raw: recursive dict combine (env wins)
    atomic: path replace + concat + exact-dict unique; every item is a mapping
    """
    require_base_directory(root, caller_role="tenant")
    resource_types = load_yaml_file(resource_types_path)
    validate_catalog_schema(resource_types)
    defaults = resource_types.get("defaults") or {}
    exceptions = resource_types.get("exceptions") or {}
    unsupported = resource_types.get("unsupported") or {}
    allowed_keys = resolve_allowed_keys(resource_types, root, allowed_keys_path)

    base_docs = _load_layer_documents(
        os.path.join(root, "base"), allowed_keys, unsupported
    )
    env_root = os.path.join(root, env_filter) if env_filter else ""
    env_docs = (
        _load_layer_documents(env_root, allowed_keys, unsupported)
        if env_root and os.path.isdir(env_root)
        else {}
    )

    # Same relative path must keep the same resource key (replacement semantics).
    for relpath, (env_path, env_key, _) in env_docs.items():
        if relpath not in base_docs:
            continue
        base_path, base_key, _ = base_docs[relpath]
        if base_key != env_key:
            raise ValueError(
                f'Path replace conflict at "{relpath}": base key "{base_key}" '
                f'({base_path}) vs env key "{env_key}" ({env_path})'
            )

    def _group(
        docs: dict[str, tuple[str, str, Any]],
        *,
        skip_paths: set[str] | None = None,
    ) -> dict[str, list[tuple[str, Any, str]]]:
        grouped: dict[str, list[tuple[str, Any, str]]] = {}
        for relpath in sorted(docs):
            if skip_paths and relpath in skip_paths:
                continue
            abspath, key, value = docs[relpath]
            grouped.setdefault(key, []).append((relpath, value, abspath))
        return grouped

    # Atomic uses path replace (skip base paths present in env).
    # Keyed/raw aggregate every file in each layer, then overlay/combine.
    atomic_base = _group(base_docs, skip_paths=set(env_docs))
    atomic_env = _group(env_docs)
    keyed_base = _group(base_docs)
    keyed_env = _group(env_docs)

    all_keys = (
        set(atomic_base)
        | set(atomic_env)
        | set(keyed_base)
        | set(keyed_env)
    )
    merged: dict[str, Any] = {}
    for key in sorted(all_keys):
        if key not in exceptions:
            raise ValueError(f'Unknown resource key "{key}"')
        meta = _resource_meta(resource_types, key)
        merge_mode = meta.get("merge_mode", defaults.get("merge_mode", "keyed"))
        id_field = meta.get("identity_field", defaults.get("identity_field", "name"))
        naming_supported = meta.get("naming_supported", True) is True

        if merge_mode == "raw":
            result: dict[str, Any] = {}
            for relpath, value, abspath in keyed_base.get(key, []) + keyed_env.get(
                key, []
            ):
                if not isinstance(value, dict):
                    raise ValueError(
                        f'{abspath}: Key "{key}" expected mapping (raw settings), '
                        f"got {type(value).__name__}"
                    )
                result = _deep_combine(result, value)
            merged[key] = result
            continue

        if merge_mode == "atomic":
            ordered: list[Any] = []
            for relpath, value, abspath in atomic_base.get(key, []) + atomic_env.get(
                key, []
            ):
                items = _validate_list_items(
                    key,
                    value,
                    abspath=abspath,
                    merge_mode=merge_mode,
                    id_field=id_field if isinstance(id_field, str) else "name",
                    naming_supported=naming_supported,
                )
                ordered.extend(items)
            merged[key] = _exact_unique(ordered)
            continue

        # keyed — hard uniqueness across the complete base layer and env layer
        def _flatten(
            layer_contrib: list[tuple[str, Any, str]], label: str
        ) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            seen: dict[Any, str] = {}
            for relpath, value, abspath in layer_contrib:
                validated = _validate_list_items(
                    key,
                    value,
                    abspath=abspath,
                    merge_mode="keyed",
                    id_field=id_field,
                    naming_supported=True,
                )
                for item in validated:
                    ident = item[id_field]
                    if ident in seen:
                        raise ValueError(
                            f'Key "{key}" duplicate keyed identity "{id_field}"='
                            f"{ident!r} across {label} layer "
                            f"({seen[ident]} and {abspath})"
                        )
                    seen[ident] = abspath
                    items.append(item)
            return items

        base_list = _flatten(keyed_base.get(key, []), "base")
        env_list = _flatten(keyed_env.get(key, []), "env")
        env_by_id = {item[id_field]: item for item in env_list}
        out_list: list[dict[str, Any]] = []
        for item in base_list:
            ident = item[id_field]
            if ident in env_by_id:
                out_list.append(_deep_combine(item, env_by_id[ident]))
            else:
                out_list.append(item)
        base_ids = {item[id_field] for item in base_list}
        for item in env_list:
            if item[id_field] not in base_ids:
                out_list.append(item)
        merged[key] = out_list

    return merged


def validate_structure(
    root: str,
    resource_types_path: str,
    allowed_keys_path: str = "",
    caller_role: str = "tenant",
    control_config: str = "",
) -> None:
    """Validate desired-state using the same merge contract as Dispatcher/Drift."""
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        print("Control repo: skipping desired-state structural validation")
        return

    require_base_directory(root, caller_role=role)
    resource_types = load_yaml_file(resource_types_path)
    validate_catalog_schema(resource_types)
    resolve_allowed_keys(resource_types, root, allowed_keys_path)

    # Exercise every base + mapped-environment combination (empty env = base only).
    env_names = load_env_names(root, control_config=control_config)
    targets = [""] + list(env_names)
    for env_filter in targets:
        merge_desired_state(
            root=root,
            env_filter=env_filter,
            resource_types_path=resource_types_path,
            allowed_keys_path=allowed_keys_path,
        )
    print("=== ALL YAML FILES PASSED STRUCTURAL VALIDATION ===")


def _deep_combine(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_combine(result[key], value)
        else:
            result[key] = value
    return result


def write_merged_resources(
    merged: dict[str, Any], output_dir: str, repo_scope: str
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    for key, value in merged.items():
        payload = {f"{key}_{repo_scope}": value}
        dest = os.path.join(output_dir, f"{key}_{repo_scope}.yml")
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(dump_yaml(payload))


def cmd_merge_desired_state(args: argparse.Namespace) -> int:
    merged = merge_desired_state(
        root=args.root,
        env_filter=args.env,
        resource_types_path=args.resource_types,
        allowed_keys_path=args.allowed_keys,
    )
    write_merged_resources(merged, args.output_dir, args.repo_scope)
    print(
        f"=== MERGED {len(merged)} resource types → {args.output_dir} "
        f"(scope={args.repo_scope}, env={args.env}) ==="
    )
    return 0



def validate_explicit_deletions(
    root: str,
    resource_types_path: str,
    caller_role: str = "tenant",
    control_config: str = "",
) -> None:
    """Fail closed when YAML requests deletion without audited schema support."""
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        print("Control repo: skipping desired-state deletion validation")
        return

    schema = load_yaml_file(resource_types_path)
    defaults = schema.get("defaults") or {}
    exceptions = schema.get("exceptions") or {}
    errors: list[str] = []

    for path in iter_resource_yaml_files(
        root, caller_role=role, control_config=control_config
    ):
        document = load_yaml_file(path)
        if len(document) != 1:
            continue  # Structural validation reports this with better context.
        resource_key, value = next(iter(document.items()))
        metadata = dict(defaults)
        metadata.update(exceptions.get(resource_key) or {})
        field = metadata.get("deletion_field", "state")
        values = metadata.get("deletion_values", ["absent"])
        if not isinstance(field, str) or not field:
            raise ValueError(f"{resource_key}: deletion_field must be a non-empty string")
        if not isinstance(values, list) or not values:
            raise ValueError(f"{resource_key}: deletion_values must be a non-empty list")

        candidates = value if isinstance(value, list) else [value]
        for index, item in enumerate(candidates):
            if not isinstance(item, dict) or item.get(field) not in values:
                continue
            if not metadata.get("deletion_supported", False):
                errors.append(
                    f'{path}: item {index} in "{resource_key}" requests '
                    f'{field}={item.get(field)!r}, but deletion is not audited'
                )
                continue
            if not str(metadata.get("deletion_evidence") or "").strip():
                errors.append(
                    f'{path}: "{resource_key}" enables deletion without deletion_evidence'
                )

    if errors:
        raise ValueError("Unsupported explicit deletion:\n  " + "\n  ".join(errors))

def resolve_jt_names(cfg: dict[str, Any]) -> dict[str, str]:
    reject_legacy_config_fields(cfg)
    configured = cfg.get("job_templates") or {}
    if configured and not isinstance(configured, dict):
        raise ValueError("config.yml job_templates must be a mapping")
    names = dict(DEFAULT_JT)
    for key, default in DEFAULT_JT.items():
        value = configured.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"job_templates.{key} must be a non-empty string")
        names[key] = value.strip()
    return names


def github_raw(org: str, repo: str, path: str, ref: str, token: str) -> bytes:
    url = (
        f"https://api.github.com/repos/{org}/{repo}/contents/"
        f"{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.raw",
            "User-Agent": "aap-casc-engine",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def github_commit_sha(org: str, repo: str, ref: str, token: str) -> str:
    url = f"https://api.github.com/repos/{org}/{repo}/commits/{urllib.parse.quote(ref)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "aap-casc-engine",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    sha = payload.get("sha")
    if not sha:
        raise RuntimeError(f"Could not resolve control revision for {org}/{repo}@{ref}")
    return sha


def gitlab_raw(api_url: str, project: str, path: str, ref: str, token: str) -> bytes:
    project_enc = urllib.parse.quote(project, safe="")
    path_enc = urllib.parse.quote(path, safe="")
    ref_enc = urllib.parse.quote(ref, safe="")
    url = f"{api_url.rstrip('/')}/projects/{project_enc}/repository/files/{path_enc}/raw?ref={ref_enc}"
    req = urllib.request.Request(
        url,
        headers={"PRIVATE-TOKEN": token, "User-Agent": "aap-casc-engine"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def gitlab_commit_sha(api_url: str, project: str, ref: str, token: str) -> str:
    project_enc = urllib.parse.quote(project, safe="")
    ref_enc = urllib.parse.quote(ref, safe="")
    url = f"{api_url.rstrip('/')}/projects/{project_enc}/repository/commits/{ref_enc}"
    req = urllib.request.Request(
        url,
        headers={"PRIVATE-TOKEN": token, "User-Agent": "aap-casc-engine"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    sha = payload.get("id")
    if not sha:
        raise RuntimeError(f"Could not resolve control revision for {project}@{ref}")
    return sha


def fetch_control_text(
    *,
    provider: str,
    org: str,
    repo: str,
    path: str,
    revision: str,
    token: str,
    gitlab_api: str | None = None,
) -> str:
    if provider == "github":
        return github_raw(org, repo, path, revision, token).decode("utf-8")
    if provider == "gitlab":
        project = f"{org}/{repo}"
        return gitlab_raw(gitlab_api or os.environ["CI_API_V4_URL"], project, path, revision, token).decode(
            "utf-8"
        )
    raise ValueError(f"Unsupported provider: {provider}")


def ensure_control_files(
    *,
    provider: str,
    org: str,
    repo: str,
    branch: str,
    token: str,
    revision: str | None = None,
    gitlab_api: str | None = None,
    dest_dir: str = ".",
) -> str:
    if not org or not repo or not branch:
        raise ValueError("control_scm_org, control_repo, and control_branch are required")
    if not token:
        raise ValueError("CONTROL_REPO_TOKEN is required to fetch authoritative control metadata")
    if revision:
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision) is None:
            raise ValueError("control_revision must be a full hexadecimal commit SHA")
        control_revision = revision.lower()
    elif provider == "github":
        control_revision = github_commit_sha(org, repo, branch, token)
    else:
        control_revision = gitlab_commit_sha(
            gitlab_api or os.environ["CI_API_V4_URL"], f"{org}/{repo}", branch, token
        )

    os.makedirs(dest_dir, exist_ok=True)
    for relative in ("config.yml", "tenants.yml"):
        try:
            content = fetch_control_text(
                provider=provider,
                org=org,
                repo=repo,
                path=relative,
                revision=control_revision,
                token=token,
                gitlab_api=gitlab_api,
            )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Failed to fetch {org}/{repo}:{relative}@{control_revision}: HTTP {exc.code}"
            ) from exc
        out = os.path.join(dest_dir, relative)
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(content)
    naming_path = os.path.join(dest_dir, "naming-rules.yml")
    if os.path.exists(naming_path):
        os.remove(naming_path)
    try:
        naming_rules = fetch_control_text(
            provider=provider,
            org=org,
            repo=repo,
            path="naming-rules.yml",
            revision=control_revision,
            token=token,
            gitlab_api=gitlab_api,
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(
                f"Failed to fetch {org}/{repo}:naming-rules.yml@{control_revision}: "
                f"HTTP {exc.code}"
            ) from exc
    else:
        with open(naming_path, "w", encoding="utf-8") as handle:
            handle.write(naming_rules)
    return control_revision


def validate_tenant_id(value: Any) -> str:
    """Return the canonical safe tenant key or fail closed."""
    if not isinstance(value, str) or not value:
        raise ValueError("tenant_id must be a non-empty string")
    if not TENANT_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "tenant_id must match ^[a-z][a-z0-9_]*$ and contain at most 64 characters"
        )
    return value


def _required_string(record: dict[str, Any], field: str, tenant_id: str = "") -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        prefix = f"Tenant {tenant_id} " if tenant_id else "Tenant "
        raise ValueError(f"{prefix}{field} must be a non-empty string")
    return value.strip()


def _reject_legacy_fields(record: dict[str, Any], legacy: frozenset[str], label: str) -> None:
    present = sorted(set(record) & legacy)
    if present:
        raise ValueError(
            f"{label} contains removed topology fields: {', '.join(present)}. "
            "Use combined-only scalars: config.yml platform_repo and tenant repo_name "
            "(resolved as repository). Per-resource patterns and maps are no longer supported."
        )


def reject_legacy_config_fields(cfg: dict[str, Any]) -> None:
    """Fail closed when control config.yml still carries removed topology keys."""
    if not isinstance(cfg, dict):
        raise ValueError("config.yml must contain a mapping")
    _reject_legacy_fields(cfg, LEGACY_CONFIG_FIELDS, "config.yml")


def resolve_tenant_repository(tenant_id: str, repo_name: Any = "") -> str:
    """Return the single combined tenant repository name."""
    tenant = validate_tenant_id(tenant_id)
    if repo_name in (None, ""):
        return f"casc-tenant-{tenant}"
    if not isinstance(repo_name, str) or not repo_name.strip():
        raise ValueError(f"Tenant {tenant} repo_name must be a non-empty string")
    return repo_name.strip()


def platform_repo_name(cfg: dict[str, Any]) -> str:
    """Return the single combined platform desired-state repository name."""
    reject_legacy_config_fields(cfg)
    value = cfg.get("platform_repo", "casc-platform-global")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("config.yml platform_repo must be a non-empty string")
    return value.strip()


TENANT_RECORD_FIELDS = {
    "tenant_id",
    "aap_organization",
    "team_name",
    "tenant_scm_org",
    "repo_mode",
    "repo_visibility",
    "repo_name",
    "onboarding_mode",
    "status",
    "dispatch_enabled",
    "dispatcher_job_template",
}

TENANT_DISPATCHER_DEFAULT_FIELDS = {
    "project",
    "inventory",
    "execution_environment",
    "credentials",
    "launcher_user",
}


def normalize_tenant_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one customer-facing tenant record and derive runtime-only fields."""
    if not isinstance(record, dict):
        raise ValueError("Each tenants.yml entry must be a mapping")
    _reject_legacy_fields(record, LEGACY_TENANT_FIELDS, "Tenant record")
    unknown = sorted(set(record) - TENANT_RECORD_FIELDS)
    if unknown:
        raise ValueError("Unsupported tenant fields: " + ", ".join(unknown))

    tenant_id = validate_tenant_id(record.get("tenant_id"))
    onboarding_mode = record.get("onboarding_mode", "greenfield")
    if onboarding_mode not in ("greenfield", "brownfield"):
        raise ValueError(
            f"Tenant {tenant_id} onboarding_mode must be greenfield or brownfield"
        )

    explicit_org = record.get("aap_organization")
    if explicit_org is not None and (
        not isinstance(explicit_org, str) or not explicit_org.strip()
    ):
        raise ValueError(f"Tenant {tenant_id} aap_organization must be a non-empty string")
    if onboarding_mode == "brownfield" and not explicit_org:
        raise ValueError(
            f"Tenant {tenant_id} brownfield onboarding requires explicit aap_organization"
        )
    aap_organization = explicit_org.strip() if explicit_org else tenant_id

    team_name = _required_string(record, "team_name", tenant_id)

    dispatcher_job_template = record.get("dispatcher_job_template")
    if dispatcher_job_template is not None:
        if not isinstance(dispatcher_job_template, str) or not dispatcher_job_template.strip():
            raise ValueError(
                f"Tenant {tenant_id} dispatcher_job_template must be a non-empty string"
            )
        dispatcher_job_template = dispatcher_job_template.strip()

    tenant_scm_org = _required_string(record, "tenant_scm_org", tenant_id)
    repo_mode = record.get("repo_mode", "create")
    status = record.get("status", "active")
    repo_visibility = record.get("repo_visibility", "private")
    if repo_mode not in ("create", "existing"):
        raise ValueError(f"Tenant {tenant_id} repo_mode must be create or existing")
    if status not in ("active", "inactive"):
        raise ValueError(f"Tenant {tenant_id} status must be active or inactive")
    if repo_visibility not in ("private", "public"):
        raise ValueError(f"Tenant {tenant_id} repo_visibility must be private or public")
    dispatch_enabled = record.get("dispatch_enabled", True)
    if not isinstance(dispatch_enabled, bool):
        raise ValueError(f"Tenant {tenant_id} dispatch_enabled must be a boolean")

    repository = resolve_tenant_repository(tenant_id, record.get("repo_name", ""))
    normalized = dict(record)
    normalized.update(
        {
            "tenant_id": tenant_id,
            "aap_organization": aap_organization,
            "tenant_scm_org": tenant_scm_org,
            "repo_mode": repo_mode,
            "onboarding_mode": onboarding_mode,
            "repo_visibility": repo_visibility,
            "status": status,
            "dispatch_enabled": dispatch_enabled,
            "repository": repository,
            **(
                {"dispatcher_job_template": dispatcher_job_template}
                if dispatcher_job_template
                else {}
            ),
        }
    )
    normalized.pop("repo_name", None)
    normalized["team_name"] = team_name
    return normalized


def tenant_dispatcher_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate the optional shared references used by tenant-bound Dispatchers."""
    raw = cfg.get("tenant_dispatcher_defaults")
    if not isinstance(raw, dict):
        raise ValueError(
            "config.yml tenant_dispatcher_defaults must be a mapping when a tenant "
            "uses dispatcher_job_template"
        )
    unknown = sorted(set(raw) - TENANT_DISPATCHER_DEFAULT_FIELDS)
    if unknown:
        raise ValueError(
            "config.yml tenant_dispatcher_defaults contains unsupported fields: "
            + ", ".join(unknown)
        )
    normalized = {
        field: _required_string(raw, field)
        for field in TENANT_DISPATCHER_DEFAULT_FIELDS - {"credentials"}
    }
    credentials = raw.get("credentials")
    if not isinstance(credentials, list) or not credentials:
        raise ValueError(
            "config.yml tenant_dispatcher_defaults credentials must be a non-empty list"
        )
    if any(not isinstance(item, str) or not item.strip() for item in credentials):
        raise ValueError(
            "config.yml tenant_dispatcher_defaults credentials must contain non-empty strings"
        )
    normalized["credentials"] = [item.strip() for item in credentials]
    if len(normalized["credentials"]) != len(set(normalized["credentials"])):
        raise ValueError(
            "config.yml tenant_dispatcher_defaults credentials must be unique"
        )
    return normalized


def normalize_runtime_tenant(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw or engine-produced tenant data without widening tenants.yml."""
    if not isinstance(record, dict):
        raise ValueError("Tenant runtime data must be a mapping")
    _reject_legacy_fields(record, LEGACY_TENANT_FIELDS, "Tenant runtime data")
    payload = {
        key: value
        for key, value in record.items()
        if key in TENANT_RECORD_FIELDS or key == "repository"
    }
    if "repository" in payload and "repo_name" not in payload:
        payload = dict(payload)
        payload["repo_name"] = payload.pop("repository")
    normalized = normalize_tenant_record(
        {key: value for key, value in payload.items() if key in TENANT_RECORD_FIELDS}
    )
    if "repository" in record and record["repository"] != normalized["repository"]:
        raise ValueError("Derived repository does not match the canonical tenant resolver")
    return normalized


def _platform_repo_owners(cfg: dict[str, Any]) -> set[tuple[str, str]]:
    reject_legacy_config_fields(cfg)
    control_org = str(cfg.get("control_scm_org") or cfg.get("platform_scm_org") or "").strip()
    platform_org = str(cfg.get("platform_scm_org") or "").strip()
    owners: set[tuple[str, str]] = set()
    if control_org and cfg.get("control_repo"):
        owners.add((control_org, str(cfg["control_repo"]).strip()))
    if platform_org:
        owners.add((platform_org, platform_repo_name(cfg)))
    return owners


def validate_tenant_registry(
    tenants_doc: dict[str, Any], cfg: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Validate global tenant identity and repository ownership invariants."""
    if not isinstance(tenants_doc, dict):
        raise ValueError("tenants.yml must contain a mapping")
    tenants = tenants_doc.get("tenants", [])
    if not isinstance(tenants, list):
        raise ValueError("tenants.yml tenants must be a list")
    if cfg is not None:
        reject_legacy_config_fields(cfg)
    normalized = [normalize_tenant_record(record) for record in tenants]
    ids: dict[str, int] = {}
    orgs: dict[str, str] = {}
    repo_owners: dict[tuple[str, str], str] = {
        owner: "control/platform" for owner in _platform_repo_owners(cfg or {})
    }
    shared_dispatcher = ""
    if cfg is not None:
        job_templates = cfg.get("job_templates", {})
        if isinstance(job_templates, dict):
            shared_dispatcher = str(job_templates.get("dispatcher") or "").strip()
    dispatcher_owners: dict[str, str] = {}
    for index, tenant in enumerate(normalized):
        tenant_id = tenant["tenant_id"]
        if tenant_id in ids:
            raise ValueError(
                f"Duplicate tenant_id '{tenant_id}' at entries {ids[tenant_id]} and {index}"
            )
        ids[tenant_id] = index
        aap_org = tenant["aap_organization"]
        if aap_org in orgs:
            raise ValueError(
                f"AAP Organization '{aap_org}' is assigned to both {orgs[aap_org]} and {tenant_id}"
            )
        orgs[aap_org] = tenant_id
        owner = (tenant["tenant_scm_org"], tenant["repository"])
        if owner in repo_owners:
            raise ValueError(
                f"Repository {owner[0]}/{owner[1]} is owned by both "
                f"{repo_owners[owner]} and {tenant_id}"
            )
        repo_owners[owner] = tenant_id
        dedicated_dispatcher = tenant.get("dispatcher_job_template")
        if dedicated_dispatcher:
            if dedicated_dispatcher == shared_dispatcher:
                raise ValueError(
                    f"Tenant {tenant_id} dispatcher_job_template must differ from the shared Dispatcher JT"
                )
            if dedicated_dispatcher in dispatcher_owners:
                raise ValueError(
                    f"Dispatcher Job Template '{dedicated_dispatcher}' is assigned to both "
                    f"{dispatcher_owners[dedicated_dispatcher]} and {tenant_id}"
                )
            dispatcher_owners[dedicated_dispatcher] = tenant_id
    if dispatcher_owners and cfg is not None:
        tenant_dispatcher_defaults(cfg)
    return normalized


SCAFFOLD_VERSION = 5


def build_scaffold_marker(
    tenant: dict[str, Any], *, repository: str
) -> dict[str, Any]:
    """Build the immutable provider marker for the tenant combined repository."""
    normalized = normalize_runtime_tenant(tenant)
    if repository != normalized["repository"]:
        raise ValueError(
            f"Repository {repository} is not the combined repository for "
            f"tenant {normalized['tenant_id']} (expected {normalized['repository']})"
        )
    marker = {
        "scaffold_version": SCAFFOLD_VERSION,
        "tenant_id": normalized["tenant_id"],
        "aap_organization": normalized["aap_organization"],
        "tenant_scm_org": normalized["tenant_scm_org"],
        "repo_mode": normalized["repo_mode"],
        "repo_visibility": normalized["repo_visibility"],
        "onboarding_mode": normalized["onboarding_mode"],
        "repository": repository,
        "team_name": normalized["team_name"],
    }
    if normalized.get("dispatcher_job_template"):
        marker["dispatcher_job_template"] = normalized["dispatcher_job_template"]
    return marker


def validate_scaffold_marker(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Fail when an existing marker does not exactly own the requested scaffold."""
    if not isinstance(actual, dict):
        raise ValueError("Existing tenant scaffold marker must be a YAML mapping")
    if actual.get("scaffold_version") != SCAFFOLD_VERSION:
        raise ValueError(
            f"Unsupported scaffold marker version {actual.get('scaffold_version')}; "
            f"expected {SCAFFOLD_VERSION}"
        )
    mismatches = [key for key, value in expected.items() if actual.get(key) != value]
    mismatches.extend(sorted(set(actual) - set(expected)))
    if mismatches:
        raise ValueError(
            "Existing scaffold marker conflicts with requested tenant fields: "
            + ", ".join(sorted(set(mismatches)))
        )


def public_tenant_runtime(tenant: dict[str, Any]) -> dict[str, Any]:
    """Return normalized runtime data suitable for JSON/Ansible consumption."""
    normalized = normalize_runtime_tenant(tenant)
    return {
        key: value
        for key, value in normalized.items()
        if not key.startswith("_")
    }


def tenant_immutable_projection(tenant: dict[str, Any]) -> dict[str, Any]:
    """Return the post-scaffold immutable tenant identity/topology contract."""
    normalized = normalize_runtime_tenant(tenant)
    projection = {
        "tenant_id": normalized["tenant_id"],
        "aap_organization": normalized["aap_organization"],
        "tenant_scm_org": normalized["tenant_scm_org"],
        "repo_mode": normalized["repo_mode"],
        "repo_visibility": normalized["repo_visibility"],
        "onboarding_mode": normalized["onboarding_mode"],
        "repository": normalized["repository"],
        "team_name": normalized["team_name"],
    }
    if normalized.get("dispatcher_job_template"):
        projection["dispatcher_job_template"] = normalized["dispatcher_job_template"]
    return projection


def _load_tenant_marker(
    tenant: dict[str, Any],
    *,
    provider: str,
    token: str,
    refs: list[str],
    gitlab_api: str = "",
) -> dict[str, Any] | None:
    """Return the first readable scaffold marker for the tenant, if any."""
    normalized = normalize_runtime_tenant(tenant)
    marker_path = ".aap-casc-engine/tenant-scaffold.yml"
    repo_name = normalized["repository"]
    for ref in refs:
        try:
            if provider == "github":
                raw = github_raw(
                    normalized["tenant_scm_org"], repo_name, marker_path, ref, token
                )
            elif provider == "gitlab":
                raw = gitlab_raw(
                    gitlab_api or os.environ["CI_API_V4_URL"],
                    f"{normalized['tenant_scm_org']}/{repo_name}",
                    marker_path,
                    ref,
                    token,
                )
            else:
                raise ValueError("provider must be github or gitlab")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        loaded = yaml.safe_load(raw.decode("utf-8"))
        if isinstance(loaded, dict):
            return loaded
    return None


def _tenant_marker_exists(
    tenant: dict[str, Any],
    *,
    provider: str,
    token: str,
    refs: list[str],
    gitlab_api: str = "",
) -> bool:
    return (
        _load_tenant_marker(
            tenant,
            provider=provider,
            token=token,
            refs=refs,
            gitlab_api=gitlab_api,
        )
        is not None
    )


def _restores_marker_owned_identity(
    tenant: dict[str, Any], marker: dict[str, Any]
) -> bool:
    """True when the proposed tenant exactly matches the scaffold marker contract."""
    normalized = normalize_runtime_tenant(tenant)
    expected = build_scaffold_marker(normalized, repository=normalized["repository"])
    try:
        validate_scaffold_marker(marker, expected)
    except ValueError:
        return False
    return True


def diff_tenant_actions(
    previous_doc: dict[str, Any],
    current_doc: dict[str, Any],
    cfg: dict[str, Any],
    *,
    marker_exists: Any,
    load_marker: Any = None,
) -> list[dict[str, Any]]:
    """Return Bootstrap actions while enforcing marker-based lifecycle immutability.

    After a marker exists, identity/topology changes away from the marker-owned
    contract are rejected. Restoring the exact marker-owned identity is allowed
    (no Bootstrap action) so poisoned registry commits can be repaired without
    force-pushing a protected control branch.
    """
    previous = {
        item["tenant_id"]: item for item in validate_tenant_registry(previous_doc, cfg)
    }
    current = {
        item["tenant_id"]: item for item in validate_tenant_registry(current_doc, cfg)
    }
    actions: list[dict[str, Any]] = []

    for tenant_id in sorted(set(previous) | set(current)):
        old = previous.get(tenant_id)
        new = current.get(tenant_id)
        if new is None:
            if old is not None and marker_exists(old):
                raise ValueError(
                    f"Tenant {tenant_id} has scaffold markers and cannot be removed in place; "
                    "use an explicit retirement/migration procedure"
                )
            continue
        if old is None:
            if new["status"] == "active":
                actions.append({"action": "added", "tenant": public_tenant_runtime(new)})
            continue

        immutable_changed = tenant_immutable_projection(old) != tenant_immutable_projection(new)
        if immutable_changed:
            if marker_exists(old) or marker_exists(new):
                marker = None
                if load_marker is not None:
                    marker = load_marker(old) or load_marker(new)
                if marker is not None and _restores_marker_owned_identity(new, marker):
                    # Registry repair back to the marker-owned contract.
                    continue
                raise ValueError(
                    f"Tenant {tenant_id} scaffold identity/topology is immutable after the first marker; "
                    "use an explicit migration procedure"
                )
            if new["status"] == "active":
                actions.append(
                    {"action": "corrected", "tenant": public_tenant_runtime(new)}
                )
            continue

        if old["status"] == "inactive" and new["status"] == "active":
            if not marker_exists(new):
                actions.append(
                    {"action": "activated", "tenant": public_tenant_runtime(new)}
                )

    return actions


def resolve_bootstrap_request(
    tenants_doc: dict[str, Any], cfg: dict[str, Any], request: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Resolve a registered authoritative tenant or validate an unregistered request."""
    if not isinstance(tenants_doc, dict):
        raise ValueError("tenants.yml must contain a mapping")
    raw_tenants = tenants_doc.get("tenants", [])
    if not isinstance(raw_tenants, list):
        raise ValueError("tenants.yml tenants must be a list")

    normalized_registry = validate_tenant_registry(tenants_doc, cfg)
    requested_id = validate_tenant_id(request.get("tenant_id"))
    registered = next(
        (tenant for tenant in normalized_registry if tenant["tenant_id"] == requested_id),
        None,
    )
    if registered is None:
        # Rebuild from original tenants.yml records so derived runtime fields
        # (e.g. repository) are not fed back as unsupported registry inputs.
        candidate_doc = {"tenants": list(raw_tenants) + [request]}
        candidate = next(
            tenant
            for tenant in validate_tenant_registry(candidate_doc, cfg)
            if tenant["tenant_id"] == requested_id
        )
        return public_tenant_runtime(candidate), False

    comparable_fields = (
        "aap_organization",
        "team_name",
        "tenant_scm_org",
        "repo_mode",
        "repo_name",
        "onboarding_mode",
        "repo_visibility",
        "dispatcher_job_template",
    )
    conflicts = []
    registered_public = public_tenant_runtime(registered)
    for field in comparable_fields:
        supplied = request.get(field)
        if supplied in (None, "", {}):
            continue
        if field == "repo_name":
            authoritative = registered_public.get("repository")
        else:
            authoritative = registered_public.get(field)
        if supplied != authoritative:
            conflicts.append(field)
    if conflicts:
        raise ValueError(
            f"Tenant {requested_id} is registered; direct inputs conflict with Git for: "
            + ", ".join(conflicts)
        )
    return registered_public, True


def resolve_dispatch_route(
    tenants_doc: dict[str, Any],
    cfg: dict[str, Any],
    *,
    caller_role: str,
    target_env: str,
    triggered_repo: str,
) -> dict[str, Any]:
    """Resolve the shared or tenant-bound Dispatcher for one pipeline run."""
    if caller_role not in {"platform", "tenant"}:
        raise ValueError("caller_role must be platform or tenant")
    env_map = cfg.get("env_branch_map")
    if not isinstance(env_map, dict) or target_env not in env_map:
        raise ValueError(f"target_env '{target_env}' is not configured")
    shared_jt = (cfg.get("job_templates") or {}).get("dispatcher")
    if not isinstance(shared_jt, str) or not shared_jt.strip():
        raise ValueError("config.yml job_templates.dispatcher must be a non-empty string")
    shared_jt = shared_jt.strip()
    tenants = validate_tenant_registry(tenants_doc, cfg)

    if caller_role == "platform":
        return {
            "dedicated": False,
            "job_template": shared_jt,
            "dispatch_scope": "platform",
            "target_env": target_env,
        }

    repo = triggered_repo.strip()
    matches = [
        tenant
        for tenant in tenants
        if tenant["status"] == "active"
        and tenant["dispatch_enabled"]
        and f"{tenant['tenant_scm_org']}/{tenant['repository']}" == repo
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Triggered repository '{repo}' must resolve to exactly one active, dispatch-enabled tenant"
        )
    tenant = matches[0]
    dedicated_jt = tenant.get("dispatcher_job_template")
    if not dedicated_jt:
        return {
            "dedicated": False,
            "job_template": shared_jt,
            "dispatch_scope": "tenant",
            "target_env": target_env,
            "tenant_id": tenant["tenant_id"],
            "triggered_repo": repo,
        }

    return {
        "dedicated": True,
        "job_template": dedicated_jt,
        "dispatch_scope": "tenant",
        "target_env": target_env,
        "tenant_id": tenant["tenant_id"],
        "triggered_repo": repo,
        "fixed_extra_vars": {
            "target_env": target_env,
            "dispatch_scope": "tenant",
            "tenant_id": tenant["tenant_id"],
            "triggered_repo": repo,
            "control_scm_org": cfg.get("control_scm_org")
            or cfg.get("platform_scm_org"),
            "control_repo": cfg.get("control_repo"),
            "control_branch": cfg.get("control_branch"),
        },
    }


def cmd_ensure_control(args: argparse.Namespace) -> int:
    revision = ensure_control_files(
        provider=args.provider,
        org=args.control_scm_org,
        repo=args.control_repo,
        branch=args.control_branch,
        token=args.token,
        revision=args.control_revision or None,
        gitlab_api=args.gitlab_api,
        dest_dir=args.dest_dir,
    )
    print(f"control_revision={revision}")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"control_revision={revision}\n")
    return 0



def cmd_resolve_jt_names(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    names = resolve_jt_names(cfg)
    print(json.dumps(names))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            for key, value in names.items():
                handle.write(f"{key}_jt_name={value}\n")
    return 0


def cmd_validate_registry(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    tenants_doc = load_yaml_file(args.tenants)
    normalized = [public_tenant_runtime(item) for item in validate_tenant_registry(tenants_doc, cfg)]
    print(json.dumps({"tenants": normalized}, sort_keys=True))
    return 0


def cmd_validate_structure(args: argparse.Namespace) -> int:
    try:
        validate_structure(
            args.root,
            args.resource_types,
            allowed_keys_path=args.allowed_keys,
            caller_role=args.caller_role,
            control_config=args.control_config,
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    return 0


def cmd_validate_deletions(args: argparse.Namespace) -> int:
    try:
        validate_explicit_deletions(
            args.root,
            args.resource_types,
            caller_role=args.caller_role,
            control_config=args.control_config,
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    print("Explicit deletion validation passed")
    return 0


def cmd_list_desired_state_dirs(args: argparse.Namespace) -> int:
    role = (args.caller_role or "tenant").strip().lower()
    if role == "control":
        return 0
    try:
        for directory in desired_state_search_dirs(
            args.root,
            control_config=args.control_config,
            caller_role=role,
        ):
            print(directory)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_resolve_bootstrap(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    tenants_doc = load_yaml_file(args.tenants)
    request = json.loads(args.request_json)
    tenant, registered = resolve_bootstrap_request(tenants_doc, cfg, request)
    print(json.dumps({"tenant": tenant, "registered": registered}, sort_keys=True))
    return 0


def cmd_resolve_dispatch_route(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    tenants_doc = load_yaml_file(args.tenants)
    route = resolve_dispatch_route(
        tenants_doc,
        cfg,
        caller_role=args.caller_role,
        target_env=args.target_env,
        triggered_repo=args.triggered_repo,
    )
    payload = json.dumps(route, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    print(payload)
    return 0


def cmd_scaffold_marker(args: argparse.Namespace) -> int:
    tenant = json.loads(args.tenant_json)
    marker = build_scaffold_marker(tenant, repository=args.repository)
    if args.actual_json:
        validate_scaffold_marker(json.loads(args.actual_json), marker)
    print(json.dumps(marker, sort_keys=True))
    return 0


def cmd_diff_tenants(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    previous = load_yaml_file(args.previous)
    current = load_yaml_file(args.current)
    mapped_branches = list((cfg.get("env_branch_map") or {}).values())
    if not mapped_branches:
        raise ValueError("config.yml env_branch_map must be a non-empty mapping")
    marker_refs = [args.marker_ref] if args.marker_ref else list(dict.fromkeys(mapped_branches))

    def marker_exists(tenant: dict[str, Any]) -> bool:
        return _tenant_marker_exists(
            tenant,
            provider=args.provider,
            token=args.scm_token,
            refs=marker_refs,
            gitlab_api=args.gitlab_api,
        )

    def load_marker(tenant: dict[str, Any]) -> dict[str, Any] | None:
        return _load_tenant_marker(
            tenant,
            provider=args.provider,
            token=args.scm_token,
            refs=marker_refs,
            gitlab_api=args.gitlab_api,
        )

    actions = diff_tenant_actions(
        previous,
        current,
        cfg,
        marker_exists=marker_exists,
        load_marker=load_marker,
    )
    payload = json.dumps(actions, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    print(payload)
    return 0


def cmd_yaml_to_json(args: argparse.Namespace) -> int:
    """Convert CasC YAML (!unsafe-aware) to JSON for OPA and other consumers."""
    data = load_yaml_document(args.input)
    payload = json.dumps(json_ready(data))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
            if not payload.endswith("\n"):
                handle.write("\n")
    else:
        print(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CasC CI runtime helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    ensure = sub.add_parser("ensure-control", help="Fetch control files at pinned revision")
    ensure.add_argument("--provider", choices=["github", "gitlab"], required=True)
    ensure.add_argument("--control-scm-org", required=True)
    ensure.add_argument("--control-repo", required=True)
    ensure.add_argument("--control-branch", required=True)
    ensure.add_argument("--token", required=True)
    ensure.add_argument("--control-revision", default="")
    ensure.add_argument("--gitlab-api", default="")
    ensure.add_argument("--dest-dir", default=".")
    ensure.add_argument("--github-output", default="")
    ensure.set_defaults(func=cmd_ensure_control)

    jt = sub.add_parser("resolve-jt-names", help="Resolve JT names from config.yml")
    jt.add_argument("--config", default="config.yml")
    jt.add_argument("--github-output", default="")
    jt.set_defaults(func=cmd_resolve_jt_names)

    registry = sub.add_parser("validate-registry", help="Validate tenants.yml globally")
    registry.add_argument("--config", default="config.yml")
    registry.add_argument("--tenants", default="tenants.yml")
    registry.set_defaults(func=cmd_validate_registry)

    structure = sub.add_parser(
        "validate-structure",
        help="Validate desired-state YAML structure (role-aware)",
    )
    structure.add_argument("--root", default=".")
    structure.add_argument("--resource-types", required=True)
    structure.add_argument("--allowed-keys", default="")
    structure.add_argument("--control-config", default="")
    structure.add_argument(
        "--caller-role",
        default="tenant",
        choices=["control", "platform", "tenant"],
    )
    structure.set_defaults(func=cmd_validate_structure)

    merge = sub.add_parser(
        "merge-desired-state",
        help="Merge base/ + env desired state (keyed/raw/atomic) into output YAML",
    )
    merge.add_argument("--root", required=True)
    merge.add_argument("--env", required=True)
    merge.add_argument("--resource-types", required=True)
    merge.add_argument("--allowed-keys", default="")
    merge.add_argument("--output-dir", required=True)
    merge.add_argument("--repo-scope", required=True)
    merge.set_defaults(func=cmd_merge_desired_state)

    deletions = sub.add_parser(
        "validate-deletions", help="Reject explicit deletion without audited support"
    )
    deletions.add_argument("--root", default=".")
    deletions.add_argument("--resource-types", required=True)
    deletions.add_argument("--control-config", default="")
    deletions.add_argument(
        "--caller-role",
        default="tenant",
        choices=["control", "platform", "tenant"],
    )
    deletions.set_defaults(func=cmd_validate_deletions)

    list_dirs = sub.add_parser(
        "list-desired-state-dirs",
        help="List base/ + env_branch_map dirs for desired-state scans",
    )
    list_dirs.add_argument("--root", default=".")
    list_dirs.add_argument("--control-config", default="")
    list_dirs.add_argument(
        "--caller-role",
        default="tenant",
        choices=["control", "platform", "tenant"],
    )
    list_dirs.set_defaults(func=cmd_list_desired_state_dirs)

    bootstrap = sub.add_parser("resolve-bootstrap", help="Resolve one Bootstrap request")
    bootstrap.add_argument("--config", required=True)
    bootstrap.add_argument("--tenants", required=True)
    bootstrap.add_argument("--request-json", required=True)
    bootstrap.set_defaults(func=cmd_resolve_bootstrap)

    route = sub.add_parser(
        "resolve-dispatch-route",
        help="Resolve the shared or tenant-bound Dispatcher for one pipeline run",
    )
    route.add_argument("--config", required=True)
    route.add_argument("--tenants", required=True)
    route.add_argument("--caller-role", choices=["platform", "tenant"], required=True)
    route.add_argument("--target-env", required=True)
    route.add_argument("--triggered-repo", default="")
    route.add_argument("--output", default="")
    route.set_defaults(func=cmd_resolve_dispatch_route)

    marker = sub.add_parser("scaffold-marker", help="Render or compare a scaffold marker")
    marker.add_argument("--tenant-json", required=True)
    marker.add_argument("--repository", required=True)
    marker.add_argument("--actual-json", default="")
    marker.set_defaults(func=cmd_scaffold_marker)

    tenant_diff = sub.add_parser(
        "diff-tenants", help="Resolve actionable tenant changes and enforce lifecycle"
    )
    tenant_diff.add_argument("--provider", choices=["github", "gitlab"], required=True)
    tenant_diff.add_argument("--config", required=True)
    tenant_diff.add_argument("--previous", required=True)
    tenant_diff.add_argument("--current", required=True)
    tenant_diff.add_argument("--scm-token", required=True)
    tenant_diff.add_argument("--marker-ref", default="")
    tenant_diff.add_argument("--gitlab-api", default="")
    tenant_diff.add_argument("--output", default="tenant_actions.json")
    tenant_diff.set_defaults(func=cmd_diff_tenants)

    y2j = sub.add_parser(
        "yaml-to-json",
        help="Convert CasC YAML to JSON (!unsafe-aware; for OPA input)",
    )
    y2j.add_argument("input", help="Input YAML path")
    y2j.add_argument(
        "--output",
        "-o",
        default="",
        help="Output JSON path (default: stdout)",
    )
    y2j.set_defaults(func=cmd_yaml_to_json)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CI entrypoint must fail closed with message
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
