"""Behavioral and static contracts for the naming-policy-neutral engine baseline.

Run with: python3 -m unittest tests/test_topology_contract.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))
sys.path.insert(0, str(ROOT / "schemas"))

import casc_runtime  # noqa: E402
import generate_naming_sample  # noqa: E402
import generate_resource_catalog  # noqa: E402
import validate_naming  # noqa: E402


PIPELINES = (
    ROOT / ".github/workflows/casc-validate-and-trigger.yml",
    ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml",
    ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml",
)
PROVIDER_TASKS = (
    ROOT / "tasks/bootstrap_scm_github.yml",
    ROOT / "tasks/bootstrap_scm_gitlab.yml",
)


def base_config(**overrides):
    cfg = {
        "control_scm_org": "ww-platform",
        "control_repo": "casc-platform-control",
        "control_branch": "main",
        "platform_scm_org": "ww-platform",
        "platform_repo": "casc-platform-global",
        "env_branch_map": {"dev": "develop", "prd": "main"},
    }
    cfg.update(overrides)
    return cfg


def greenfield(tenant_id="stores", **overrides):
    record = {
        "tenant_id": tenant_id,
        "team_name": "Stores Automation",
        "tenant_scm_org": "ww-tenants",
        "onboarding_mode": "greenfield",
    }
    record.update(overrides)
    return record


def brownfield(tenant_id="legacy", **overrides):
    record = {
        "tenant_id": tenant_id,
        "aap_organization": "Legacy LDAP Organization",
        "team_name": "Legacy Automation Team",
        "tenant_scm_org": "ww-tenants",
        "onboarding_mode": "brownfield",
    }
    record.update(overrides)
    return record



class TenantIdentityTests(unittest.TestCase):
    def test_greenfield_defaults_aap_organization_to_tenant_id(self):
        runtime = casc_runtime.public_tenant_runtime(greenfield())
        self.assertEqual(runtime["tenant_id"], "stores")
        self.assertEqual(runtime["aap_organization"], "stores")
        self.assertEqual(runtime["repository"], "casc-tenant-stores")
        self.assertNotIn("repositories", runtime)
        self.assertNotIn("repo_by_folder", runtime)
        self.assertNotIn("repo_pattern", runtime)
        self.assertNotIn("org-stores", json.dumps(runtime))

    def test_greenfield_accepts_exact_customer_identities(self):
        runtime = casc_runtime.public_tenant_runtime(
            greenfield(
                aap_organization='WW Stores: Automation #1 "Primary"',
                team_name="Storekeepers' Automation",
            )
        )
        self.assertEqual(runtime["aap_organization"], 'WW Stores: Automation #1 "Primary"')
        self.assertEqual(runtime["team_name"], "Storekeepers' Automation")

    def test_brownfield_contract_references_existing_org_and_team(self):
        runtime = casc_runtime.public_tenant_runtime(brownfield())
        self.assertEqual(runtime["aap_organization"], "Legacy LDAP Organization")
        self.assertEqual(runtime["team_name"], "Legacy Automation Team")
        with self.assertRaisesRegex(ValueError, "requires explicit aap_organization"):
            casc_runtime.normalize_tenant_record(
                brownfield(aap_organization=None)
            )
        with self.assertRaisesRegex(ValueError, "team_name must be"):
            casc_runtime.normalize_tenant_record(brownfield(team_name=""))

    def test_tenant_id_safe_key_limits(self):
        for valid in ("a", "stores", "tenant_01", "a" * 64):
            self.assertEqual(casc_runtime.validate_tenant_id(valid), valid)
        for invalid in (
            "",
            "A",
            "Stores",
            "1stores",
            "stores-team",
            "stores.team",
            "stores/team",
            "../stores",
            "stores team",
            " stores",
            "stores ",
            "\tstores",
            "a" * 65,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                casc_runtime.validate_tenant_id(invalid)

    def test_derived_runtime_fields_are_not_accepted_as_registry_inputs(self):
        for field in ("derived_repositories", "repository_cache", "access_principal"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Unsupported"):
                casc_runtime.normalize_tenant_record(greenfield(**{field: "x"}))

    def test_registry_rejects_duplicate_identity_and_repo_ownership(self):
        cfg = base_config()
        with self.assertRaisesRegex(ValueError, "Duplicate tenant_id"):
            casc_runtime.validate_tenant_registry(
                {"tenants": [greenfield(), greenfield()]}, cfg
            )
        with self.assertRaisesRegex(ValueError, "AAP Organization"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("stores", aap_organization="Shared Org"),
                        greenfield("network", aap_organization="Shared Org"),
                    ]
                },
                cfg,
            )
        with self.assertRaisesRegex(ValueError, "owned by both"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("stores", repo_name="shared-casc"),
                        greenfield("network", repo_name="shared-casc"),
                    ]
                },
                cfg,
            )
        with self.assertRaisesRegex(ValueError, "owned by both"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("stores", tenant_scm_org="ww-platform", repo_name="casc-platform-global")
                    ]
                },
                cfg,
            )

    def test_inactive_records_still_reserve_identities(self):
        with self.assertRaisesRegex(ValueError, "AAP Organization"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("old", aap_organization="Reserved Org", status="inactive"),
                        greenfield("new", aap_organization="Reserved Org"),
                    ]
                },
                base_config(),
            )

    def test_custom_scalar_repository_and_legacy_rejection(self):
        runtime = casc_runtime.public_tenant_runtime(greenfield(repo_name="ww-tenant-stores"))
        self.assertEqual(runtime["repository"], "ww-tenant-stores")
        self.assertEqual(
            casc_runtime.resolve_tenant_repository("stores"), "casc-tenant-stores"
        )
        self.assertEqual(
            casc_runtime.platform_repo_name(base_config()), "casc-platform-global"
        )
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.normalize_tenant_record(greenfield(repo_pattern="combined"))
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.normalize_tenant_record(greenfield(repo_names={"projects": "x"}))
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.platform_repo_name(
                base_config(platform_repo_pattern="combined")
            )
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.reject_legacy_config_fields(
                base_config(repo_mode="create")
            )

    def test_same_short_repo_name_is_safe_across_scm_namespaces(self):
        normalized = casc_runtime.validate_tenant_registry(
            {
                "tenants": [
                    greenfield("stores", tenant_scm_org="ww-stores", repo_name="aap-casc"),
                    greenfield("network", tenant_scm_org="ww-network", repo_name="aap-casc"),
                ]
            },
            base_config(),
        )
        self.assertEqual(len(normalized), 2)
        site = (ROOT / "site.yml").read_text()
        self.assertIn("item.tenant_scm_org + '/' + item.repository", site)
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            if "gitlab" in str(pipeline):
                self.assertIn("CI_PROJECT_PATH", content)
            else:
                self.assertIn("github.repository", content)

class LifecycleTests(unittest.TestCase):
    def test_added_active_tenant_is_actionable(self):
        actions = casc_runtime.diff_tenant_actions(
            {"tenants": []},
            {"tenants": [greenfield()]},
            base_config(),
            marker_exists=lambda _tenant: False,
        )
        self.assertEqual([item["action"] for item in actions], ["added"])

    def test_pre_scaffold_correction_and_removal_are_allowed(self):
        old = greenfield(aap_organization="Typo Org")
        new = greenfield(aap_organization="Correct Org")
        actions = casc_runtime.diff_tenant_actions(
            {"tenants": [old]},
            {"tenants": [new]},
            base_config(),
            marker_exists=lambda _tenant: False,
        )
        self.assertEqual(actions[0]["action"], "corrected")
        self.assertEqual(
            casc_runtime.diff_tenant_actions(
                {"tenants": [old]},
                {"tenants": []},
                base_config(),
                marker_exists=lambda _tenant: False,
            ),
            [],
        )

    def test_post_scaffold_identity_change_and_removal_fail(self):
        old = greenfield()
        marker = casc_runtime.build_scaffold_marker(
            casc_runtime.normalize_runtime_tenant(old),
            repository="casc-tenant-stores",
        )
        with self.assertRaisesRegex(ValueError, "immutable"):
            casc_runtime.diff_tenant_actions(
                {"tenants": [old]},
                {"tenants": [greenfield(team_name="Renamed Team")]},
                base_config(),
                marker_exists=lambda _tenant: True,
                load_marker=lambda _tenant: marker,
            )
        with self.assertRaisesRegex(ValueError, "cannot be removed"):
            casc_runtime.diff_tenant_actions(
                {"tenants": [old]},
                {"tenants": []},
                base_config(),
                marker_exists=lambda _tenant: True,
                load_marker=lambda _tenant: marker,
            )

    def test_post_scaffold_restore_to_marker_owned_identity_is_allowed(self):
        good = greenfield()
        poisoned = greenfield(team_name="Renamed Stores Team")
        marker = casc_runtime.build_scaffold_marker(
            casc_runtime.normalize_runtime_tenant(good),
            repository="casc-tenant-stores",
        )
        actions = casc_runtime.diff_tenant_actions(
            {"tenants": [poisoned]},
            {"tenants": [good]},
            base_config(),
            marker_exists=lambda _tenant: True,
            load_marker=lambda _tenant: marker,
        )
        self.assertEqual(actions, [])
        # Changing away from the marker remains rejected.
        with self.assertRaisesRegex(ValueError, "immutable"):
            casc_runtime.diff_tenant_actions(
                {"tenants": [good]},
                {"tenants": [poisoned]},
                base_config(),
                marker_exists=lambda _tenant: True,
                load_marker=lambda _tenant: marker,
            )

    def test_mutable_status_and_dispatch_do_not_bootstrap(self):
        old = greenfield(status="active", dispatch_enabled=True)
        new = greenfield(status="inactive", dispatch_enabled=False)
        actions = casc_runtime.diff_tenant_actions(
            {"tenants": [old]},
            {"tenants": [new]},
            base_config(),
            marker_exists=lambda _tenant: True,
        )
        self.assertEqual(actions, [])

    def test_marker_is_strict_and_mode_specific(self):
        tenant = greenfield()
        expected = casc_runtime.build_scaffold_marker(
            tenant, repository="casc-tenant-stores"
        )
        self.assertEqual(expected["scaffold_version"], 5)
        self.assertIn("repo_mode", expected)
        self.assertIn("repo_visibility", expected)
        self.assertNotIn("tenant_scm_namespace_id", expected)
        self.assertEqual(expected["repository"], "casc-tenant-stores")
        self.assertNotIn("resource_type", expected)
        casc_runtime.validate_scaffold_marker(dict(expected), expected)
        changed = dict(expected, aap_organization="Other")
        with self.assertRaisesRegex(ValueError, "aap_organization"):
            casc_runtime.validate_scaffold_marker(changed, expected)
        extra = dict(expected, unexpected_identity="someone")
        with self.assertRaisesRegex(ValueError, "unexpected_identity"):
            casc_runtime.validate_scaffold_marker(extra, expected)

        brown = brownfield()
        marker = casc_runtime.build_scaffold_marker(
            brown, repository="casc-tenant-legacy"
        )
        self.assertEqual(marker["team_name"], "Legacy Automation Team")

    def test_survey_resolution_uses_git_as_authority(self):
        doc = {"tenants": [greenfield(aap_organization="WW Stores")]}
        resolved, registered = casc_runtime.resolve_bootstrap_request(
            doc, base_config(), {"tenant_id": "stores"}
        )
        self.assertTrue(registered)
        self.assertEqual(resolved["aap_organization"], "WW Stores")
        with self.assertRaisesRegex(ValueError, "conflict"):
            casc_runtime.resolve_bootstrap_request(
                doc,
                base_config(),
                {"tenant_id": "stores", "aap_organization": "Other"},
            )

    def test_unregistered_survey_resolution_is_lean_and_validated(self):
        resolved, registered = casc_runtime.resolve_bootstrap_request(
            {"tenants": []}, base_config(), greenfield()
        )
        self.assertFalse(registered)
        self.assertEqual(resolved["aap_organization"], "stores")
        self.assertEqual(resolved["team_name"], "Stores Automation")

    def test_second_survey_tenant_onboards_with_nonempty_registry(self):
        existing = greenfield("stores", repo_name="ww-tenant-stores")
        request = greenfield("network", team_name="Network Automation")
        resolved, registered = casc_runtime.resolve_bootstrap_request(
            {"tenants": [existing]}, base_config(), request
        )
        self.assertFalse(registered)
        self.assertEqual(resolved["tenant_id"], "network")
        self.assertEqual(resolved["repository"], "casc-tenant-network")
        self.assertNotIn("repositories", resolved)

    def test_registered_repo_name_compares_against_repository(self):
        doc = {"tenants": [greenfield(repo_name="ww-tenant-stores")]}
        cfg = base_config()
        matched, registered = casc_runtime.resolve_bootstrap_request(
            doc, cfg, {"tenant_id": "stores", "repo_name": "ww-tenant-stores"}
        )
        self.assertTrue(registered)
        self.assertEqual(matched["repository"], "ww-tenant-stores")
        omitted, registered = casc_runtime.resolve_bootstrap_request(
            doc, cfg, {"tenant_id": "stores"}
        )
        self.assertTrue(registered)
        self.assertEqual(omitted["repository"], "ww-tenant-stores")
        with self.assertRaisesRegex(ValueError, "conflict"):
            casc_runtime.resolve_bootstrap_request(
                doc, cfg, {"tenant_id": "stores", "repo_name": "other-repo"}
            )

    def test_resolve_jt_names_rejects_legacy_config_fields(self):
        names = casc_runtime.resolve_jt_names(base_config())
        self.assertEqual(names["bootstrap"], "jt-platform-bootstrap_tenant")
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.resolve_jt_names(
                base_config(platform_repo_pattern="combined")
            )


class FoundationAndTemplateTests(unittest.TestCase):
    def setUp(self):
        self.jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        self.jinja.filters["to_json"] = json.dumps
        self.jinja.filters["to_nice_yaml"] = lambda value, indent=2: yaml.safe_dump(
            value, sort_keys=False, default_flow_style=False, indent=indent
        )

    def test_two_neutral_foundation_paths(self):
        """Foundation paths come from Bootstrap provider tasks, not a dead helper."""
        path_fragments = (
            'path: "base/organizations/{{ _effective_tenant_id }}.yml"',
            'path: "base/teams/{{ _effective_tenant_id }}.yml"',
        )
        bootstrap = (ROOT / "bootstrap.yml").read_text(encoding="utf-8")
        for fragment in path_fragments:
            self.assertIn(fragment, bootstrap)
        self.assertNotIn("iter_foundation_targets", bootstrap)
        for provider in (
            ROOT / "tasks/bootstrap_scm_github.yml",
            ROOT / "tasks/bootstrap_scm_gitlab.yml",
        ):
            content = provider.read_text(encoding="utf-8")
            self.assertIn("_platform_scaffold_files", content, provider)
            self.assertIn("product(_mapped_branches)", content, provider)
        runtime = (ROOT / "scripts/pipeline/casc_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("def iter_foundation_targets", runtime)
        self.assertNotIn("FOUNDATION_RESOURCES", runtime)
        self.assertNotIn("def find_tenant", runtime)

    def test_free_form_foundation_values_round_trip_yaml(self):
        context = {
            "_effective_tenant_id": "stores",
            "_effective_aap_organization": 'WW Stores: Automation #1 "Primary"',
            "_effective_team_name": "Storekeepers' Automation",
        }
        org = yaml.safe_load(
            self.jinja.get_template("templates/org-template.yml.j2").render(**context)
        )
        team = yaml.safe_load(
            self.jinja.get_template("templates/team-template.yml.j2").render(**context)
        )
        self.assertEqual(
            org["aap_organizations"][0]["name"], context["_effective_aap_organization"]
        )
        self.assertEqual(team["aap_teams"][0]["name"], context["_effective_team_name"])
        self.assertEqual(
            team["aap_teams"][0]["organization"], context["_effective_aap_organization"]
        )

    def test_tenant_samples_use_the_exact_aap_organization(self):
        context = {
            "tenant_id": "stores",
            "_effective_aap_organization": 'WW Stores: Automation #1 "Primary"',
            "scm_base_url": "https://github.example/ww",
        }
        resource_keys = (
            ("templates/seed-controller-projects.yml.j2", "controller_projects"),
            ("templates/seed-controller-credentials.yml.j2", "controller_credentials"),
            ("templates/seed-controller-inventories.yml.j2", "controller_inventories"),
            ("templates/seed-controller-templates.yml.j2", "controller_templates"),
            ("templates/seed-controller-workflows.yml.j2", "controller_workflows"),
            ("templates/seed-controller-schedules.yml.j2", "controller_schedules"),
            ("templates/seed-controller-notifications.yml.j2", "controller_notifications"),
        )
        for template, resource_key in resource_keys:
            with self.subTest(template=template):
                rendered = self.jinja.get_template(template).render(**context)
                item = yaml.safe_load(rendered)[resource_key][0]
                self.assertEqual(item["organization"], context["_effective_aap_organization"])

    def test_bootstrap_foundation_is_org_and_team_only(self):
        bootstrap = (ROOT / "bootstrap.yml").read_text()
        for deleted in (
            "user-template.yml.j2",
            "rbac-user-template.yml.j2",
            "rbac-team-template.yml.j2",
            "default_ee",
            "Ansible Galaxy",
        ):
            self.assertNotIn(deleted, bootstrap)
        self.assertIn("team-template.yml.j2", bootstrap)
        for task in PROVIDER_TASKS:
            content = task.read_text()
            self.assertIn("_platform_scaffold_files", content)
            self.assertNotIn("rbac-user", content)
            self.assertNotIn("rbac-team", content)
            self.assertIn("Verify final platform Bootstrap scaffold content", content)

    def test_user_sample_is_password_free_and_uses_organizations_list(self):
        sample = yaml.safe_load((ROOT / "templates/seed-aap-users.yml.j2").read_text())
        user = sample["aap_user_accounts"][0]
        self.assertNotIn("password", user)
        self.assertNotIn("change_me", json.dumps(user))
        self.assertIsInstance(user["organizations"], list)

    def test_no_generic_galaxy_or_default_ee_assumptions(self):
        checked = [
            ROOT / "bootstrap.yml",
            ROOT / "templates/org-template.yml.j2",
            ROOT / "templates/seed-aap-organizations.yml.j2",
        ] + list((ROOT / "examples/v2").rglob("*.yml"))
        text = "\n".join(path.read_text() for path in checked)
        for marker in (
            "Ansible Galaxy",
            "Default execution environment",
            "default_environment",
            "galaxy_credentials",
            "DEFAULT_EE",
            "default_ee",
        ):
            self.assertNotIn(marker, text)

    def test_password_default_is_disabled_at_apply_boundaries(self):
        self.assertIn('users_default_password: ""', (ROOT / "site.yml").read_text())
        self.assertFalse((ROOT / "remediate.yml").exists())


class NamingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.resource_types = str(ROOT / "schemas/resource-types.yml")
        self.allowed = str(ROOT / "roles/process_casc_config/defaults/main.yml")

    def load(self, rules_path):
        return validate_naming.load_policy(
            str(rules_path), self.resource_types, self.allowed
        )[0]

    def test_empty_policy_is_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "naming-rules.yml"
            rules.write_text("---\n", encoding="utf-8")
            self.assertEqual(self.load(rules), {})

    def test_canonical_naming_sample_is_inert_commented_source(self):
        sample_path = ROOT / "examples/naming-rules.yml.sample"
        sample = sample_path.read_text(encoding="utf-8")
        self.assertIn("rename", sample.lower())
        self.assertIn("adapt", sample.lower())
        self.assertIn("uncomment", sample.lower())
        self.assertIn("REPLACE_ME", sample)
        self.assertNotIn("WW ", sample)
        self.assertFalse((ROOT / "examples/naming-rules-type-prefixed.yml.sample").exists())
        # Commented-only sample body loads as an inactive empty policy.
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "naming-rules.yml"
            rules.write_text(sample, encoding="utf-8")
            self.assertEqual(self.load(rules), {})

    def test_customer_policy_uses_registered_identity_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "naming-rules.yml"
            rules_path.write_text(
                "aap_user_accounts:\n  pattern: '^user_[a-z]+$'\n", encoding="utf-8"
            )
            rules = self.load(rules_path)
            good = root / "good.yml"
            good.write_text("aap_user_accounts:\n  - username: user_stores\n", encoding="utf-8")
            bad = root / "bad.yml"
            bad.write_text("aap_user_accounts:\n  - username: Stores User\n", encoding="utf-8")
            self.assertEqual(validate_naming.validate_file(str(good), rules), [])
            self.assertTrue(validate_naming.validate_file(str(bad), rules))

    def test_day_zero_policy_validates_rendered_foundation(self):
        jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        jinja.filters["to_json"] = json.dumps
        context = {
            "_effective_tenant_id": "stores",
            "_effective_aap_organization": "WW Stores Automation",
            "_effective_team_name": "Stores Automation",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "naming-rules.yml"
            rules_path.write_text(
                "aap_organizations:\n  pattern: '^WW .+ Automation$'\n"
                "aap_teams:\n  pattern: '^.+ Automation$'\n",
                encoding="utf-8",
            )
            desired = root / "desired"
            control = desired / ".control"
            control.mkdir(parents=True)
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            org_dir = desired / "base" / "organizations"
            team_dir = desired / "base" / "teams"
            org_dir.mkdir(parents=True)
            team_dir.mkdir(parents=True)
            (org_dir / "organizations.yml").write_text(
                jinja.get_template("templates/org-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            (team_dir / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            rules = self.load(rules_path)
            self.assertEqual(validate_naming.validate_tree(str(desired), rules), [])

            context["_effective_team_name"] = "Stores"
            (team_dir / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            self.assertTrue(validate_naming.validate_tree(str(desired), rules))

            # Restore a valid team, then prove unrelated docs/ YAML is ignored.
            context["_effective_team_name"] = "Stores Automation"
            (team_dir / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            docs = desired / "docs"
            docs.mkdir()
            (docs / "notes.yml").write_text(
                "aap_organizations:\n  - name: BAD NAME\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_naming.validate_tree(str(desired), rules), [])

    def test_policy_schema_fails_closed(self):
        cases = {
            "list": "- aap_organizations\n",
            "unknown": "not_a_resource:\n  pattern: x\n",
            "bad-rule": "aap_organizations: x\n",
            "missing-pattern": "aap_organizations:\n  example: x\n",
            "bad-regex": "aap_organizations:\n  pattern: '[unterminated'\n",
            "raw": "controller_settings:\n  pattern: x\n",
            "unsupported-action": "controller_launch_jobs:\n  pattern: x\n",
            "unsupported-rbac-naming": "gateway_role_user_assignments:\n  pattern: x\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, content in cases.items():
                with self.subTest(label=label):
                    path = Path(tmp) / f"{label}.yml"
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.load(path)

    def test_naming_rules_control_file_is_not_scanned_as_desired_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base").mkdir()
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            policy = Path(tmp) / "policy-source.txt"
            policy.write_text("aap_organizations:\n  pattern: '^WW .+$'\n", encoding="utf-8")
            (root / "naming-rules.yml").write_text(
                "aap_organizations:\n  pattern: invalid-as-resource-data\n", encoding="utf-8"
            )
            rules = self.load(policy)
            self.assertEqual(validate_naming.validate_tree(str(root), rules), [])

    def test_genesis_seeds_inert_sample_not_active_policy(self):
        genesis = (ROOT / "genesis.yml").read_text(encoding="utf-8")
        self.assertIn("examples/naming-rules.yml.sample", genesis)
        self.assertIn("_naming_rules_sample_content", genesis)
        self.assertIn("rstrip=false", genesis)
        self.assertNotIn("_control_sample_branches", genesis)
        # lookup must preserve trailing newline of the canonical sample bytes.
        self.assertRegex(
            genesis,
            r"lookup\('file',\s*playbook_dir\s*~\s*'/examples/naming-rules\.yml\.sample',\s*rstrip=false\)",
        )
        for task in (
            ROOT / "tasks/genesis_scm_github.yml",
            ROOT / "tasks/genesis_scm_gitlab.yml",
        ):
            content = task.read_text(encoding="utf-8")
            self.assertIn("naming-rules.yml.sample", content)
            # create_only preserves customer-modified sample content on re-run.
            self.assertIn("'path': 'naming-rules.yml.sample'", content)
            self.assertIn("'policy': 'create_only'", content)
            self.assertIn("control_branch", content)
            self.assertNotIn("_control_sample_branches", content)
            self.assertNotIn("Render naming-rules", content)
            # Never write active naming-rules.yml from Genesis.
            self.assertNotRegex(content, r"contents/naming-rules\.yml[^.]")
            self.assertNotRegex(content, r"files/naming-rules\.yml[^.]")
        sample_bytes = (ROOT / "examples/naming-rules.yml.sample").read_bytes()
        self.assertTrue(sample_bytes.endswith(b"\n"))
        self.assertFalse((ROOT / "schemas/naming-rules.yml").exists())
        self.assertFalse((ROOT / "templates/naming-rules.yml.j2").exists())
        self.assertFalse((ROOT / "templates/naming-rules.yml.sample.j2").exists())

    def test_bootstrap_naming_preflight_uses_base_layout_and_pinned_control(self):
        bootstrap = (ROOT / "bootstrap.yml").read_text(encoding="utf-8")
        self.assertIn(
            'path: "base/organizations/{{ _effective_tenant_id }}.yml"',
            bootstrap,
        )
        self.assertIn(
            'path: "base/teams/{{ _effective_tenant_id }}.yml"',
            bootstrap,
        )
        self.assertIn("--control-config", bootstrap)
        self.assertIn(
            "{{ bootstrap_clone_dir }}/{{ control_repo }}/config.yml",
            bootstrap,
        )
        # Flat root foundation files are not scanned by desired_state_search_dirs.
        self.assertNotIn(
            '{ name: organizations.yml, content: "{{ _org_foundation_content | default(\'\') }}" }',
            bootstrap,
        )


class DeletionSafetyTests(unittest.TestCase):
    @staticmethod
    def _write_pinned_control(root: Path) -> Path:
        control = root / ".control"
        control.mkdir(parents=True, exist_ok=True)
        cfg = control / "config.yml"
        cfg.write_text(
            "scm_provider: github\n"
            "control_scm_org: org\n"
            "control_repo: control\n"
            "control_branch: main\n"
            "platform_scm_org: org\n"
            "platform_repo: casc-platform-global\n"
            "env_branch_map:\n  poc: dev\n  prod: main\n",
            encoding="utf-8",
        )
        return cfg

    def test_unsupported_explicit_deletion_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pinned_control(root)
            base = root / "base" / "organizations"
            base.mkdir(parents=True)
            target = base / "org.yml"
            target.write_text(
                "aap_organizations:\n  - name: demo\n    state: absent\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "deletion is not audited"):
                casc_runtime.validate_explicit_deletions(
                    str(root), str(ROOT / "schemas/resource-types.yml")
                )

            target.write_text(
                "aap_organizations:\n  - name: demo\n    state: present\n",
                encoding="utf-8",
            )
            casc_runtime.validate_explicit_deletions(
                str(root), str(ROOT / "schemas/resource-types.yml")
            )

    def test_control_repo_ignores_unrelated_root_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pinned_control(root)
            (root / "platform-policy.yml").write_text(
                "unrelated-platform-policy:\n  owner: platform-governance\n",
                encoding="utf-8",
            )
            (root / "GOVERNANCE.md").write_text("# keep me\n", encoding="utf-8")

            # Control callers must ignore arbitrary root YAML as desired state.
            self.assertEqual(
                casc_runtime.iter_resource_yaml_files(str(root), caller_role="control"),
                [],
            )
            casc_runtime.validate_structure(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
                caller_role="control",
            )
            casc_runtime.validate_explicit_deletions(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                caller_role="control",
            )

    def test_platform_tenant_scan_only_base_and_env_branch_map_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pinned_control(root)

            base = root / "base" / "organizations"
            poc = root / "poc" / "organizations"
            prod = root / "prod" / "organizations"
            docs = root / "docs"
            governance = root / "governance"
            for path in (base, poc, prod, docs, governance):
                path.mkdir(parents=True)

            (base / "valid.yml").write_text(
                "aap_organizations:\n  - name: base-demo\n",
                encoding="utf-8",
            )
            (poc / "valid.yml").write_text(
                "aap_organizations:\n  - name: poc-demo\n",
                encoding="utf-8",
            )
            (prod / "valid.yml").write_text(
                "aap_organizations:\n  - name: prod-demo\n",
                encoding="utf-8",
            )
            (docs / "notes.yml").write_text(
                "unrelated-docs:\n  keep: true\n",
                encoding="utf-8",
            )
            (governance / "policy.yml").write_text(
                "unrelated-governance:\n  keep: true\n",
                encoding="utf-8",
            )

            for role in ("platform", "tenant"):
                with self.subTest(role=role):
                    paths = casc_runtime.iter_resource_yaml_files(
                        str(root), caller_role=role
                    )
                    names = sorted(Path(p).name for p in paths)
                    self.assertEqual(names, ["valid.yml", "valid.yml", "valid.yml"])
                    joined = "\n".join(paths)
                    self.assertIn("/base/", joined)
                    self.assertIn("/poc/", joined)
                    self.assertIn("/prod/", joined)
                    self.assertNotIn("/docs/", joined)
                    self.assertNotIn("/governance/", joined)

                    casc_runtime.validate_structure(
                        str(root),
                        str(ROOT / "schemas/resource-types.yml"),
                        allowed_keys_path=str(
                            ROOT / "roles/process_casc_config/defaults/main.yml"
                        ),
                        caller_role=role,
                    )

            # Invalid YAML under a mapped env directory must fail closed.
            (poc / "bad.yml").write_text(
                "not_a_casc_resource:\n  - name: nope\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unknown resource key"):
                casc_runtime.validate_structure(
                    str(root),
                    str(ROOT / "schemas/resource-types.yml"),
                    allowed_keys_path=str(
                        ROOT / "roles/process_casc_config/defaults/main.yml"
                    ),
                    caller_role="tenant",
                )

            # Invalid YAML under docs/ must remain ignored.
            (poc / "bad.yml").unlink()
            (docs / "also-bad.yml").write_text(
                "not_a_casc_resource:\n  - name: ignored\n",
                encoding="utf-8",
            )
            casc_runtime.validate_structure(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
                caller_role="platform",
            )

    def test_platform_tenant_ci_requires_base_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pinned_control(root)
            # Env folders alone must not satisfy CI — base/ is mandatory.
            (root / "poc" / "organizations").mkdir(parents=True)
            (root / "poc" / "organizations" / "valid.yml").write_text(
                "aap_organizations:\n  - name: poc-demo\n",
                encoding="utf-8",
            )
            for role in ("platform", "tenant"):
                with self.subTest(role=role):
                    with self.assertRaisesRegex(ValueError, "require a base/ directory"):
                        casc_runtime.validate_structure(
                            str(root),
                            str(ROOT / "schemas/resource-types.yml"),
                            allowed_keys_path=str(
                                ROOT / "roles/process_casc_config/defaults/main.yml"
                            ),
                            caller_role=role,
                        )
                    with self.assertRaisesRegex(ValueError, "require a base/ directory"):
                        casc_runtime.desired_state_search_dirs(
                            str(root), caller_role=role
                        )
            # Control remains exempt.
            casc_runtime.validate_structure(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                caller_role="control",
            )

    def test_explicit_control_config_is_authoritative_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base").mkdir()
            # Legacy root config.yml must not be used as a fallback.
            (root / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Pinned control config"):
                casc_runtime.desired_state_search_dirs(str(root))

            missing = root / "missing-control.yml"
            with self.assertRaisesRegex(ValueError, "Pinned control config not found"):
                casc_runtime.resolve_control_config_path(
                    str(root), control_config=str(missing)
                )

            pinned = self._write_pinned_control(root)
            self.assertEqual(
                casc_runtime.resolve_control_config_path(
                    str(root), control_config=str(pinned)
                ),
                str(pinned),
            )
            self.assertEqual(
                casc_runtime.resolve_control_config_path(str(root)),
                str(pinned),
            )

    def test_cli_default_control_config_resolves_under_root_not_cwd(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            cwd = outer / "cwd"
            root = outer / "desired-state"
            cwd.mkdir()
            control = root / ".control"
            orgs = root / "base" / "organizations"
            control.mkdir(parents=True)
            orgs.mkdir(parents=True)
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (orgs / "org.yml").write_text(
                "aap_organizations:\n  - name: WW Demo Org\n",
                encoding="utf-8",
            )
            policy = outer / "naming-rules.yml"
            policy.write_text(
                "aap_organizations:\n  pattern: '^WW .+$'\n",
                encoding="utf-8",
            )

            # No --control-config: must use <root>/.control/config.yml even when
            # the process cwd is elsewhere and has no .control/ directory.
            listed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/pipeline/casc_runtime.py"),
                    "list-desired-state-dirs",
                    "--root",
                    str(root),
                    "--caller-role",
                    "tenant",
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(listed.stdout.strip(), "base")

            structure = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/pipeline/casc_runtime.py"),
                    "validate-structure",
                    "--root",
                    str(root),
                    "--caller-role",
                    "tenant",
                    "--resource-types",
                    str(ROOT / "schemas/resource-types.yml"),
                    "--allowed-keys",
                    str(ROOT / "roles/process_casc_config/defaults/main.yml"),
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(structure.returncode, 0, structure.stderr + structure.stdout)

            naming = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "schemas/validate_naming.py"),
                    "--config-dir",
                    str(root),
                    "--rules",
                    str(policy),
                    "--resource-types",
                    str(ROOT / "schemas/resource-types.yml"),
                    "--allowed-keys",
                    str(ROOT / "roles/process_casc_config/defaults/main.yml"),
                    "--caller-role",
                    "tenant",
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(naming.returncode, 0, naming.stderr + naming.stdout)
            self.assertIn("All configured naming rules passed", naming.stdout)

    def test_env_branch_map_keys_reject_traversal_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            cfg = control / "config.yml"

            bad_keys = {
                "../outside": 'env_branch_map:\n  "../outside": main\n',
                "BadEnv": "env_branch_map:\n  BadEnv: main\n",
                "poc-1": "env_branch_map:\n  poc-1: main\n",
                " poc": 'env_branch_map:\n  " poc": main\n',
                "": 'env_branch_map:\n  "": main\n',
            }
            for bad_key, content in bad_keys.items():
                with self.subTest(bad_key=bad_key):
                    cfg.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, r"must match \^\[a-z\]"):
                        casc_runtime.load_env_names(str(root))
                    with self.assertRaisesRegex(ValueError, r"must match \^\[a-z\]"):
                        validate_naming.load_env_names(str(root))

            cfg.write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            self.assertEqual(
                casc_runtime.load_env_names(str(root)),
                ["poc", "prod"],
            )

    def test_every_allowed_key_resolves_fail_closed_deletion_metadata(self):
        schema = yaml.safe_load((ROOT / "schemas/resource-types.yml").read_text())
        role_defaults = yaml.safe_load(
            (ROOT / "roles/process_casc_config/defaults/main.yml").read_text()
        )
        for key in role_defaults["casc_allowed_resource_keys"]:
            metadata = dict(schema["defaults"])
            metadata.update(schema.get("exceptions", {}).get(key, {}))
            self.assertIsInstance(metadata.get("deletion_supported"), bool, key)
            self.assertEqual(metadata.get("deletion_field"), "state", key)
            self.assertIn("absent", metadata.get("deletion_values", []), key)

    def test_ci_and_dispatcher_repeat_deletion_validation(self):
        for pipeline in PIPELINES:
            self.assertIn("validate-deletions", pipeline.read_text(), pipeline)
        process_role = (ROOT / "roles/process_casc_config/tasks/main.yml").read_text()
        self.assertIn("validate-deletions", process_role)


class DeclarativeCatalogContractTests(unittest.TestCase):
    """ROADMAP-011 catalog + ROADMAP-001 atomic overlay contracts."""

    ACTION_UNSUPPORTED = {
        "controller_bulk_hosts",
        "controller_launch_jobs",
        "controller_workflow_launch_jobs",
        "hub_ee_repository_sync",
    }

    ENGINE_EXTENSIONS = {
        "hub_group_roles",
        "hub_roles",
    }

    SEED_TEMPLATES = (
        ("templates/seed-aap-organizations.yml.j2", "aap_organizations"),
        ("templates/seed-aap-teams.yml.j2", "aap_teams"),
        ("templates/seed-aap-users.yml.j2", "aap_user_accounts"),
        ("templates/seed-controller-settings.yml.j2", "controller_settings"),
        ("templates/seed-controller-credential_types.yml.j2", "controller_credential_types"),
        ("templates/seed-controller-projects.yml.j2", "controller_projects"),
        ("templates/seed-controller-credentials.yml.j2", "controller_credentials"),
        ("templates/seed-controller-inventories.yml.j2", "controller_inventories"),
        ("templates/seed-controller-templates.yml.j2", "controller_templates"),
        ("templates/seed-controller-workflows.yml.j2", "controller_workflows"),
        ("templates/seed-controller-schedules.yml.j2", "controller_schedules"),
        (
            "templates/seed-controller-schedules-platform.yml.j2",
            "controller_schedules",
        ),
        ("templates/seed-controller-notifications.yml.j2", "controller_notifications"),
        ("templates/seed-controller-execution_environments.yml.j2", "controller_execution_environments"),
        ("templates/seed-gateway-role_definitions.yml.j2", "gateway_role_definitions"),
        ("templates/seed-gateway-rbac_user_assignments.yml.j2", "gateway_role_user_assignments"),
        ("templates/seed-gateway-rbac_team_assignments.yml.j2", "gateway_role_team_assignments"),
    )

    def setUp(self):
        self.schema = yaml.safe_load((ROOT / "schemas/resource-types.yml").read_text())
        self.role_defaults = yaml.safe_load(
            (ROOT / "roles/process_casc_config/defaults/main.yml").read_text()
        )
        self.dispatch = yaml.safe_load(
            (ROOT / "schemas/collection-dispatch-4.7.0.yml").read_text()
        )
        self.identity = yaml.safe_load(
            (ROOT / "schemas/fixtures/collection-identity-4.7.0.yml").read_text()
        )
        self.jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        self.jinja.filters["to_json"] = json.dumps
        self.jinja.filters["to_nice_yaml"] = lambda value, indent=2: yaml.safe_dump(
            value, sort_keys=False, default_flow_style=False, indent=indent
        )

    def test_collection_pin_is_exact_discovered_version(self):
        collection = self.schema["collection"]
        self.assertEqual(collection["name"], "infra.aap_configuration")
        self.assertEqual(collection["version"], "4.7.0")
        req = (ROOT / "collections/requirements.yml").read_text()
        self.assertIn('version: "4.7.0"', req)
        self.assertNotIn(">=4.0.0", req)
        self.assertNotIn("job 392", req.lower())
        self.assertNotIn("job 392", yaml.dump(self.schema).lower())

    def test_allowlist_matches_supported_catalog_exactly(self):
        allowed = set(self.role_defaults["casc_allowed_resource_keys"])
        supported = set(self.schema["exceptions"])
        self.assertEqual(allowed, supported)
        unsupported = set(self.schema["unsupported"])
        self.assertFalse(allowed & unsupported)

    def test_catalog_partitions_all_active_dispatch_variables(self):
        dispatch_vars = {entry["var"] for entry in self.dispatch["dispatch_variables"]}
        self.assertEqual(len(dispatch_vars), 53)
        self.assertIn("eda_credential_input_sources", dispatch_vars)
        supported = set(self.schema["exceptions"])
        unsupported = set(self.schema["unsupported"])
        extensions = {
            key
            for key, meta in self.schema["exceptions"].items()
            if (meta or {}).get("engine_extension") is True
        }
        self.assertEqual(extensions, self.ENGINE_EXTENSIONS)
        collection_supported = supported - extensions
        self.assertEqual(collection_supported | unsupported, dispatch_vars)
        self.assertFalse(collection_supported & unsupported)
        self.assertEqual(len(collection_supported), 49)
        self.assertEqual(len(unsupported), 4)
        self.assertEqual(len(supported), 51)
        for key in self.ENGINE_EXTENSIONS:
            self.assertEqual(
                self.schema["exceptions"][key].get("merge_mode"), "atomic", key
            )
            self.assertNotIn(key, dispatch_vars)
        for key in self.identity["optional_publish_examples"]:
            self.assertNotIn(key, dispatch_vars)
            self.assertIn(
                key,
                {
                    e["var"]
                    for e in self.dispatch.get("optional_publish_variables", [])
                },
            )

    def test_supported_identities_match_collection_fixture(self):
        defaults = self.schema["defaults"]
        exceptions = self.schema["exceptions"]
        for entry in self.identity["scalar_identity_examples"]:
            key = entry["var"]
            self.assertIn(key, exceptions, key)
            meta = dict(defaults)
            meta.update(exceptions[key] or {})
            self.assertEqual(meta.get("merge_mode"), "keyed", key)
            self.assertEqual(meta.get("identity_field"), entry["identity_field"], key)
        for entry in self.identity["raw_examples"]:
            key = entry["var"]
            self.assertIn(key, exceptions, key)
            meta = dict(defaults)
            meta.update(exceptions[key] or {})
            self.assertEqual(meta.get("merge_mode"), "raw", key)
            self.assertEqual(meta.get("value_type"), "raw", key)
        for entry in self.identity["atomic_examples"]:
            key = entry["var"]
            self.assertIn(key, exceptions, key)
            meta = dict(defaults)
            meta.update(exceptions[key] or {})
            self.assertEqual(meta.get("merge_mode"), "atomic", key)
        for entry in self.identity["action_examples"]:
            self.assertIn(entry["var"], self.schema["unsupported"], entry["var"])

    def test_structure_rejects_action_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base" / "jobs").mkdir(parents=True)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (root / "base" / "jobs" / "bad.yml").write_text(
                "controller_launch_jobs:\n  - name: demo\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, r"Unsupported resource key"):
                casc_runtime.validate_structure(
                    str(root),
                    str(ROOT / "schemas/resource-types.yml"),
                    allowed_keys_path=str(
                        ROOT / "roles/process_casc_config/defaults/main.yml"
                    ),
                    control_config=str(control / "config.yml"),
                )

    def test_structure_rejects_non_mapping_list_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base" / "organizations").mkdir(parents=True)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (root / "base" / "organizations" / "bad.yml").write_text(
                "aap_organizations:\n  - just-a-string\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, r"must be a mapping"):
                casc_runtime.validate_structure(
                    str(root),
                    str(ROOT / "schemas/resource-types.yml"),
                    allowed_keys_path=str(
                        ROOT / "roles/process_casc_config/defaults/main.yml"
                    ),
                    control_config=str(control / "config.yml"),
                )

    def test_ci_and_runtime_share_merge_contract(self):
        """CI validate-structure and runtime merge must each reject the same fixtures."""
        allowed = str(ROOT / "roles/process_casc_config/defaults/main.yml")
        catalog = str(ROOT / "schemas/resource-types.yml")

        def _assert_ci_and_runtime_each_raise(root: Path, pattern: str) -> None:
            with self.assertRaisesRegex(ValueError, pattern):
                casc_runtime.validate_structure(
                    str(root),
                    catalog,
                    allowed_keys_path=allowed,
                    control_config=str(root / ".control" / "config.yml"),
                )
            with self.assertRaisesRegex(ValueError, pattern):
                casc_runtime.merge_desired_state(
                    str(root), "poc", catalog, allowed_keys_path=allowed
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (root / "base" / "organizations").mkdir(parents=True)
            (root / "base" / "organizations" / "a.yml").write_text(
                "aap_organizations:\n  - name: Dup\n",
                encoding="utf-8",
            )
            (root / "base" / "organizations" / "b.yml").write_text(
                "aap_organizations:\n  - name: Dup\n",
                encoding="utf-8",
            )
            _assert_ci_and_runtime_each_raise(root, r"duplicate keyed identity")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (root / "base" / "shared").mkdir(parents=True)
            (root / "poc" / "shared").mkdir(parents=True)
            (root / "base" / "shared" / "item.yml").write_text(
                "aap_teams:\n  - name: Team\n    organization: Org\n",
                encoding="utf-8",
            )
            (root / "poc" / "shared" / "item.yml").write_text(
                "controller_projects:\n  - name: Project\n    organization: Org\n",
                encoding="utf-8",
            )
            _assert_ci_and_runtime_each_raise(root, r"Path replace conflict")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (root / "base" / "teams").mkdir(parents=True)
            (root / "base" / "teams" / "bad.yml").write_text(
                "aap_teams:\n  - just-a-string\n",
                encoding="utf-8",
            )
            _assert_ci_and_runtime_each_raise(root, r"must be a mapping")

    def test_unsafe_tag_survives_merge_and_ansible_include_vars(self):
        import subprocess

        from ansible.parsing.dataloader import DataLoader

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = Path(tmp) / "out"
            (root / "base" / "credential_types").mkdir(parents=True)
            rendered = self.jinja.get_template(
                "templates/seed-controller-credential_types.yml.j2"
            ).render()
            src = root / "base" / "credential_types" / "example.yml"
            src.write_text(rendered, encoding="utf-8")
            loaded = casc_runtime.load_yaml_file(str(src))
            injector = loaded["controller_credential_types"][0]["injectors"][
                "extra_vars"
            ]["my_api_key"]
            self.assertIsInstance(injector, casc_runtime.UnsafeString)
            self.assertEqual(str(injector), "{{ api_key }}")

            merged = casc_runtime.merge_desired_state(
                str(root),
                "poc",
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
            )
            merged_injector = merged["controller_credential_types"][0]["injectors"][
                "extra_vars"
            ]["my_api_key"]
            self.assertIsInstance(merged_injector, casc_runtime.UnsafeString)
            casc_runtime.write_merged_resources(merged, str(out), "platform")
            dumped_path = out / "controller_credential_types_platform.yml"
            dumped = dumped_path.read_text(encoding="utf-8")
            self.assertIn("!unsafe", dumped)
            self.assertIn("{{ api_key }}", dumped)

            # Ansible DataLoader (same path as include_vars) must accept !unsafe.
            ansible_data = DataLoader().load_from_file(str(dumped_path))
            ansible_injector = ansible_data["controller_credential_types_platform"][0][
                "injectors"
            ]["extra_vars"]["my_api_key"]
            self.assertEqual(str(ansible_injector), "{{ api_key }}")

            # Also exercise include_vars via ansible-playbook with a clean config.
            cfg = Path(tmp) / "ansible.cfg"
            cfg.write_text("[defaults]\ninventory = localhost,\n", encoding="utf-8")
            playbook = Path(tmp) / "check.yml"
            playbook.write_text(
                "\n".join(
                    [
                        "---",
                        "- hosts: localhost",
                        "  connection: local",
                        "  gather_facts: false",
                        "  tasks:",
                        "    - include_vars:",
                        f"        file: {dumped_path}",
                        "    - assert:",
                        "        that:",
                        "          - controller_credential_types_platform[0].injectors.extra_vars.my_api_key == \"{{ '{{ api_key }}' }}\"",
                    ]
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["ansible-playbook", str(playbook)],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "ANSIBLE_CONFIG": str(cfg)},
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_naming_and_opa_yaml_to_json_accept_unsafe_credential_types(self):
        """CI naming + OPA conversion must not fail on seed !unsafe injectors."""
        rendered = self.jinja.get_template(
            "templates/seed-controller-credential_types.yml.j2"
        ).render()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (root / "base" / "credential_types").mkdir(parents=True)
            src = root / "base" / "credential_types" / "example.yml"
            src.write_text(rendered, encoding="utf-8")

            # Active policy that does NOT target credential types — parse must still succeed.
            rules_path = root / "naming-rules.yml"
            rules_path.write_text(
                "aap_organizations:\n"
                "  pattern: '^.+$'\n"
                "  example: Org\n"
                "  description: any org name\n",
                encoding="utf-8",
            )
            rules, _, _ = validate_naming.load_policy(
                str(rules_path),
                str(ROOT / "schemas/resource-types.yml"),
                str(ROOT / "roles/process_casc_config/defaults/main.yml"),
            )
            self.assertEqual(validate_naming.validate_file(str(src), rules), [])
            self.assertEqual(
                validate_naming.validate_tree(
                    str(root), rules, control_config=str(control / "config.yml")
                ),
                [],
            )

            out_json = root / "opa_input.json"
            rc = casc_runtime.main(
                ["yaml-to-json", str(src), "--output", str(out_json)]
            )
            self.assertEqual(rc, 0)
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["controller_credential_types"][0]["injectors"]["extra_vars"][
                    "my_api_key"
                ],
                "{{ api_key }}",
            )

        for pipeline in PIPELINES:
            content = pipeline.read_text(encoding="utf-8")
            self.assertIn("yaml-to-json", content, pipeline)
            self.assertNotIn(
                "json.dump(yaml.safe_load(open(sys.argv[1]))",
                content,
                pipeline,
            )

    def test_atomic_exact_dedup_ignores_yaml_key_order(self):
        """Semantically identical atomic mappings dedupe regardless of key order."""
        left = {"name": "Team", "organization": "Org"}
        right = {"organization": "Org", "name": "Team"}
        deduped = casc_runtime._exact_unique([left, right])
        self.assertEqual(len(deduped), 1)

        unsafe_a = {
            "name": "Type",
            "injectors": {
                "extra_vars": {"k": casc_runtime.UnsafeString("{{ api_key }}")}
            },
        }
        unsafe_b = {
            "injectors": {
                "extra_vars": {"k": casc_runtime.UnsafeString("{{ api_key }}")}
            },
            "name": "Type",
        }
        plain = {
            "name": "Type",
            "injectors": {"extra_vars": {"k": "{{ api_key }}"}},
        }
        self.assertEqual(len(casc_runtime._exact_unique([unsafe_a, unsafe_b])), 1)
        # Plain string must remain distinct from !unsafe.
        self.assertEqual(len(casc_runtime._exact_unique([unsafe_a, plain])), 2)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base" / "teams").mkdir(parents=True)
            (root / "poc" / "teams").mkdir(parents=True)
            (root / "base" / "teams" / "a.yml").write_text(
                "aap_teams:\n  - name: Team\n    organization: Org\n",
                encoding="utf-8",
            )
            (root / "poc" / "teams" / "b.yml").write_text(
                "aap_teams:\n  - organization: Org\n    name: Team\n",
                encoding="utf-8",
            )
            merged = casc_runtime.merge_desired_state(
                str(root),
                "poc",
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
            )
            self.assertEqual(len(merged["aap_teams"]), 1)

    def test_naming_sample_covers_forty_three_types(self):
        defaults = self.schema["defaults"]
        naming_keys = []
        for key, meta in self.schema["exceptions"].items():
            merged = dict(defaults)
            merged.update(meta or {})
            if (
                merged.get("naming_supported", True) is True
                and merged.get("value_type", "list") == "list"
                and merged.get("identity_scalar", True) is True
            ):
                naming_keys.append(key)
        self.assertEqual(len(naming_keys), 43)

    def test_atomic_path_replace_and_keyed_overlay_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base" / "teams").mkdir(parents=True)
            (root / "poc" / "teams").mkdir(parents=True)
            (root / "base" / "organizations").mkdir(parents=True)
            (root / "poc" / "organizations").mkdir(parents=True)
            (root / "base" / "teams" / "stores.yml").write_text(
                "aap_teams:\n  - name: Base Team\n    organization: Org\n",
                encoding="utf-8",
            )
            (root / "poc" / "teams" / "stores.yml").write_text(
                "aap_teams:\n  - name: Env Team\n    organization: Org\n",
                encoding="utf-8",
            )
            (root / "base" / "organizations" / "org.yml").write_text(
                "aap_organizations:\n  - name: Org\n    description: base\n",
                encoding="utf-8",
            )
            (root / "poc" / "organizations" / "org.yml").write_text(
                "aap_organizations:\n  - name: Org\n    description: env\n",
                encoding="utf-8",
            )
            merged = casc_runtime.merge_desired_state(
                str(root),
                "poc",
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
            )
            self.assertEqual(merged["aap_teams"][0]["name"], "Env Team")
            self.assertEqual(len(merged["aap_teams"]), 1)
            self.assertEqual(merged["aap_organizations"][0]["description"], "env")

    def test_supported_shipped_seeds_validate_and_merge(self):
        context = {
            "tenant_id": "stores",
            "_effective_tenant_id": "stores",
            "_effective_aap_organization": "WW Stores Automation",
            "_effective_team_name": "Stores Automation",
            "scm_base_url": "https://github.example/ww",
            "platform_scm_org": "example-platform",
            "engine_repo": "aap-casc-engine",
            "scm_provider": "github",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            for index, (template, key) in enumerate(self.SEED_TEMPLATES):
                folder = root / "base" / f"seed-{index}"
                folder.mkdir(parents=True)
                rendered = self.jinja.get_template(template).render(**context)
                dest = folder / f"{key}.yml"
                dest.write_text(rendered, encoding="utf-8")
                data = casc_runtime.load_yaml_file(str(dest))
                self.assertIn(key, data, template)
                self.assertIn(
                    f"docs/RESOURCE_CATALOG.md#{key}",
                    rendered,
                    template,
                )
                self.assertIn("/blob/main/docs/RESOURCE_CATALOG.md", rendered)
                gitlab_rendered = self.jinja.get_template(template).render(
                    **{**context, "scm_provider": "gitlab"}
                )
                self.assertIn(
                    "/-/blob/main/docs/RESOURCE_CATALOG.md", gitlab_rendered
                )

            casc_runtime.validate_structure(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
                control_config=str(control / "config.yml"),
            )
            merged = casc_runtime.merge_desired_state(
                str(root),
                "poc",
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
            )
            for _, key in self.SEED_TEMPLATES:
                self.assertIn(key, merged, key)
            self.assertIsInstance(merged["controller_settings"], dict)
            self.assertIn("settings", merged["controller_settings"])

    def test_resource_catalog_inputs_match_supported_catalog(self):
        examples = generate_resource_catalog.load_yaml(
            ROOT / "examples/resource-examples.yml"
        )
        parameters = yaml.safe_load(
            (ROOT / "schemas/resource-parameters-4.7.0.yml").read_text(
                encoding="utf-8"
            )
        )
        supported = set(self.schema["exceptions"])
        self.assertEqual(set(examples["resources"]), supported)
        self.assertEqual(set(parameters["resources"]), supported)
        for source in (examples, parameters):
            self.assertEqual(
                source["collection"]["name"], self.schema["collection"]["name"]
            )
            self.assertEqual(
                source["collection"]["version"], self.schema["collection"]["version"]
            )

        compatible_types = {
            "bool": {"bool", "boolean"},
            "dict": {"dict", "mapping", "obj", "object"},
            "float": {"float"},
            "int": {"int", "integer"},
            "list": {"list"},
            "str": {
                "str",
                "string",
                'choice("always", "missing", "never")',
                "str (see note below)",
            },
        }

        def _value_type(value):
            if isinstance(value, bool):
                return "bool"
            if isinstance(value, dict):
                return "dict"
            if isinstance(value, list):
                return "list"
            if isinstance(value, float):
                return "float"
            if isinstance(value, int):
                return "int"
            return "str"

        for key in supported:
            example = examples["resources"][key]["example"]
            self.assertEqual(set(example), {key}, key)
            groups = parameters["resources"][key]["parameter_groups"]
            self.assertTrue(groups, key)
            documented_fields = set()
            documented_parameters = {}
            for group in groups:
                self.assertTrue(group["name"], key)
                self.assertTrue(group["parameters"], key)
                for parameter in group["parameters"]:
                    for field in ("name", "type", "required", "description"):
                        self.assertTrue(parameter.get(field), f"{key}: {field}")
                    documented_fields.add(parameter["name"])
                    documented_parameters.setdefault(parameter["name"], parameter)
            value = example[key]
            if isinstance(value, list) and value:
                self.assertIsInstance(value[0], dict, key)
                self.assertFalse(
                    set(value[0]) - documented_fields,
                    f"{key}: example fields missing from parameter reference",
                )
                # The first group is the resource object. Later groups describe
                # nested structures that are required only when that structure
                # is selected by the resource.
                required_fields = {
                    parameter["name"]
                    for parameter in groups[0]["parameters"]
                    if str(parameter["required"]).strip().lower() == "yes"
                }
                self.assertFalse(
                    required_fields - set(value[0]),
                    f"{key}: example omits required root fields",
                )
                for field, field_value in value[0].items():
                    actual_type = _value_type(field_value)
                    documented_type = str(
                        documented_parameters[field]["type"]
                    ).lower()
                    if documented_type not in {
                        "any",
                        "not specified in role readme",
                    }:
                        self.assertIn(
                            documented_type,
                            compatible_types[actual_type],
                            f"{key}.{field}: example and catalog type differ",
                        )

    def test_resource_catalog_examples_validate_and_merge(self):
        examples = generate_resource_catalog.load_yaml(
            ROOT / "examples/resource-examples.yml"
        )["resources"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            for index, (key, entry) in enumerate(examples.items()):
                folder = root / "base" / f"catalog-{index:02d}"
                folder.mkdir(parents=True)
                (folder / f"{key}.yml").write_text(
                    "---\n"
                    + generate_resource_catalog.dump_yaml(entry["example"])
                    + "\n",
                    encoding="utf-8",
                )

            casc_runtime.validate_structure(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
                control_config=str(control / "config.yml"),
            )
            merged = casc_runtime.merge_desired_state(
                str(root),
                "poc",
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
            )
            self.assertEqual(set(merged), set(examples))

    def test_generated_resource_catalog_is_current_and_complete(self):
        self.assertIsNone(generate_resource_catalog.main(["--check"]))
        self.assertEqual(
            generate_resource_catalog.current_drift_keys(),
            {
                "aap_organizations",
                "aap_teams",
                "controller_credential_types",
                "controller_projects",
                "controller_inventories",
            },
        )
        catalog = (ROOT / "docs/RESOURCE_CATALOG.md").read_text(encoding="utf-8")
        expected_keys = set(self.schema["exceptions"])
        heading_keys = set(
            re.findall(r"(?m)^### `([a-z0-9_]+)`$", catalog)
        )
        toc_fragments = set(
            re.findall(r"\[`[a-z0-9_]+`\]\(#([a-z0-9_]+)\)", catalog)
        )
        self.assertEqual(heading_keys, expected_keys)
        self.assertEqual(toc_fragments, heading_keys)
        for key in self.schema["exceptions"]:
            self.assertIn(f"### `{key}`", catalog, key)
            self.assertIn(f"<!-- catalog-example:{key} -->", catalog, key)
        self.assertEqual(catalog.count("<!-- catalog-example:"), 51)
        self.assertIn("## Engine extensions", catalog)

    def test_resource_examples_do_not_embed_plaintext_credentials(self):
        examples = generate_resource_catalog.load_yaml(
            ROOT / "examples/resource-examples.yml"
        )

        def _check(value, path="resources"):
            if isinstance(value, dict):
                for key, item in value.items():
                    item_path = f"{path}.{key}"
                    if key in {"password", "token", "secret"}:
                        self.assertTrue(
                            isinstance(item, bool)
                            or (
                                isinstance(item, str)
                                and "lookup('env'" in item
                            ),
                            f"Plaintext credential-like value at {item_path}",
                        )
                    _check(item, item_path)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    _check(item, f"{path}[{index}]")

        _check(examples["resources"])

    def test_generated_readme_uses_provider_correct_catalog_link(self):
        template = self.jinja.get_template("templates/genesis-readme.md.j2")
        context = {
            "repo": {
                "name": "casc-platform-global",
                "description": "Platform desired state",
                "repository_class": "platform",
            },
            "scm_base_url": "https://scm.example",
            "platform_scm_org": "example-platform",
            "engine_repo": "aap-casc-engine",
        }
        github = template.render(**{**context, "scm_provider": "github"})
        gitlab = template.render(**{**context, "scm_provider": "gitlab"})
        self.assertIn(
            "/aap-casc-engine/blob/main/docs/RESOURCE_CATALOG.md", github
        )
        self.assertIn(
            "/aap-casc-engine/-/blob/main/docs/RESOURCE_CATALOG.md", gitlab
        )

    def test_naming_sample_matches_catalog(self):
        sample = (ROOT / "examples/naming-rules.yml.sample").read_text(encoding="utf-8")
        self.assertNotIn("(;)", sample)
        for line in sample.splitlines():
            if line.startswith("#   - ") and "(" in line:
                self.assertEqual(line.count("("), line.count(")"), line)
        self.assertEqual(
            generate_naming_sample.main(
                [
                    "--resource-types",
                    str(ROOT / "schemas/resource-types.yml"),
                    "--output",
                    str(ROOT / "examples/naming-rules.yml.sample"),
                    "--check",
                ]
            ),
            0,
        )


class ProviderAndPipelineParityTests(unittest.TestCase):
    def test_provider_tasks_use_two_file_all_branch_transaction(self):
        for task in PROVIDER_TASKS:
            content = task.read_text()
            self.assertIn("product(_mapped_branches)", content, task)
            self.assertIn("before content scaffolding", content, task)
            self.assertIn("Verify final scaffold marker", content, task)
            self.assertIn("Verify final thin caller", content, task)
            self.assertIn("Verify required scaffold files", content, task)
            self.assertIn("Verify final platform Bootstrap scaffold", content, task)
            self.assertIn("default-branch scaffold marker", content, task)
            self.assertIn("Validate latest survey registry candidate", content, task)
            self.assertNotIn("default('aap-organizations-global')", content, task)
            self.assertNotIn("default('aap-teams-global')", content, task)

    def test_default_branch_marker_validation_skips_missing_repos(self):
        for task in PROVIDER_TASKS:
            content = task.read_text()
            self.assertIn("selectattr('status', 'equalto', 200)", content, task)
            self.assertIn("default_branch | default('-')", content, task)

    def test_gitlab_empty_project_skips_null_default_branch_probe(self):
        """Option A existing-empty GitLab projects have default_branch: null."""
        content = (ROOT / "tasks/bootstrap_scm_gitlab.yml").read_text()
        self.assertIn("default_branch': item.json.default_branch | default('', true)", content)
        self.assertIn("not (item.empty_repo | default(false) | bool)", content)
        self.assertIn("(item.default_branch | default('', true) | length) > 0", content)
        self.assertIn("item.json.empty_repo", content)

    def test_dispatcher_selected_repos_preserves_native_lists(self):
        content = (ROOT / "site.yml").read_text()
        self.assertNotIn("selected_repos: >-", content)
        self.assertIn('selected_repos: "{{ _platform_repos if dispatch_scope == \'platform\'', content)
        self.assertIn("Build platform repos list", content)
        self.assertIn("item.repository", content)

    def test_drift_platform_repos_preserves_native_lists(self):
        content = (ROOT / "drift-detect.yml").read_text()
        self.assertIn("Build platform repos list for drift check", content)
        self.assertIn("item.repository", content)
        self.assertIn("clone_name: \"platform__{{ _platform_repo }}\"", content)
        self.assertIn("clone_depth: 0", content)
        self.assertIn("drift_compare.py", content)
        self.assertNotIn("drift_mode", content)
        self.assertNotIn("DRIFT_MODE", content)
        self.assertNotIn("remediate.yml", content)
        self.assertNotIn("extra_in_live", content)

    def test_onboarding_fanout_is_platform_only_when_scaffold_is_needed(self):
        workflows = (
            ROOT / ".github/workflows/casc-validate-and-trigger.yml",
            ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml",
        )
        for workflow_path in workflows:
            workflow = workflow_path.read_text()
            self.assertIn('onboarding_mode", "greenfield") == "greenfield"', workflow, workflow_path)
            self.assertIn('or a["tenant"].get("dispatcher_job_template")', workflow, workflow_path)
            self.assertIn("fanout_tenant_ids", workflow, workflow_path)
            self.assertIn("fanout_tenant_ids != '[]'", workflow, workflow_path)
            self.assertNotIn("dispatch_tenant_ids", workflow, workflow_path)
            self.assertNotIn("ONBOARDING_TENANTS", workflow, workflow_path)

        gitlab = (ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml").read_text()
        self.assertIn('onboarding_mode", "greenfield") == "greenfield"', gitlab)
        self.assertIn('or a["tenant"].get("dispatcher_job_template")', gitlab)
        self.assertIn("BOOTSTRAP_FANOUT_TENANT_IDS", gitlab)
        self.assertIn("No platform scaffold requires fan-out", gitlab)
        self.assertNotIn("BOOTSTRAP_DISPATCH_TENANT_IDS", gitlab)

    def test_pipelines_share_the_token_only_dispatcher_launcher(self):
        """Every pipeline delegates token-only AAP launch to one helper."""
        rejection = "username/password targets are rejected; bearer token only"
        launcher = (ROOT / "scripts/pipeline/dispatcher_launch.py").read_text()
        self.assertEqual(launcher.count(rejection), 1)
        for pipeline in PIPELINES:
            self.assertIn("dispatcher_launch.py", pipeline.read_text(), pipeline)

    def test_genesis_converges_platform_scaffold_all_branches(self):
        for task in (
            ROOT / "tasks/genesis_scm_github.yml",
            ROOT / "tasks/genesis_scm_gitlab.yml",
        ):
            content = task.read_text()
            self.assertIn("every mapped branch", content, task)
            self.assertIn("platform branch scaffold", content, task)
            self.assertIn("Converge platform", content, task)
            self.assertIn("Verify final control scaffold", content, task)
            self.assertIn("run_scaffold_commit.yml", content, task)
            # Survey tenants.yml updates are Bootstrap-only; Genesis seeds tenants.yml
            # inside the control atomic manifest (create_only), not via Contents PUT loops.
            self.assertNotIn("Push tenants.yml (first-time only)", content, task)
            self.assertNotIn("Seed CI/CD thin caller in each repo", content, task)

    def test_genesis_builds_control_repo_record_before_inventory(self):
        """Ansible cannot reference a key set in the same set_fact task."""
        content = (ROOT / "genesis.yml").read_text()
        control_idx = content.index("Build control repository inventory record")
        inventory_idx = content.index("Build complete Genesis repository inventory")
        self.assertLess(control_idx, inventory_idx)
        control_block = content[control_idx:inventory_idx]
        inventory_block = content[inventory_idx : inventory_idx + 400]
        self.assertIn("control_repo_record:", control_block)
        self.assertNotIn("genesis_repos:", control_block)
        self.assertIn("genesis_repos:", inventory_block)
        self.assertIn("control_repo_record", inventory_block)

    def test_genesis_and_bootstrap_reject_removed_topology_inputs(self):
        genesis = (ROOT / "genesis.yml").read_text()
        bootstrap = (ROOT / "bootstrap.yml").read_text()
        self.assertIn("Reject removed topology launch inputs", genesis)
        self.assertIn("PLATFORM_REPO_PATTERN", genesis)
        self.assertIn("platform_repo | length > 0", genesis)
        self.assertIn("Reject removed topology launch inputs", bootstrap)
        self.assertIn("REPO_PATTERN", bootstrap)
        self.assertIn("control_config.platform_repo_pattern is not defined", bootstrap)
        readme = (ROOT / "templates/genesis-readme.md.j2").read_text()
        self.assertIn("platform desired-state repository", readme)
        self.assertNotIn("platform desired-state repositories", readme)

    def test_precreated_empty_repositories_use_atomic_option_a(self):
        gh_genesis = (ROOT / "tasks/genesis_scm_github.yml").read_text()
        gl_genesis = (ROOT / "tasks/genesis_scm_gitlab.yml").read_text()
        gh_bootstrap = (ROOT / "tasks/bootstrap_scm_github.yml").read_text()
        gl_bootstrap = (ROOT / "tasks/bootstrap_scm_gitlab.yml").read_text()

        for content in (gh_genesis, gl_genesis):
            self.assertIn("Option A first commit", content)
            self.assertIn("Initialize empty control repository with full atomic scaffold", content)
            self.assertIn("Genesis: publish control scaffold [skip ci]", content)
            self.assertNotIn("Genesis: initialize managed repository [skip ci]", content)
        self.assertIn("auto_init: false", gh_genesis)
        self.assertIn("initialize_with_readme: false", gl_genesis)

        for content in (gh_bootstrap, gl_bootstrap):
            self.assertIn("Option A first commit", content)
            self.assertIn("Publish full tenant scaffold as first commit", content)
            self.assertIn("Bootstrap: publish tenant scaffold [skip ci]", content)
            self.assertNotIn("Bootstrap: initialize tenant scaffold identity [skip ci]", content)

        self.assertIn("auto_init: false", gh_bootstrap)
        self.assertIn("initialize_with_readme: false", gl_bootstrap)
        self.assertIn("status_code: [200, 404, 409]", gh_bootstrap)
        self.assertIn("item.json.empty_repo", gl_bootstrap)
        all_provider_tasks = gh_genesis + gl_genesis + gh_bootstrap + gl_bootstrap
        self.assertNotIn("repository-init", all_provider_tasks)
        self.assertIn("run_scaffold_commit.yml", all_provider_tasks)
        helper_task = (ROOT / "tasks/run_scaffold_commit.yml").read_text()
        self.assertIn("scaffold_commit.py", helper_task)
        self.assertIn("always:", helper_task)
        self.assertIn("Fail closed with safe (repo, branch) identity", helper_task)
        self.assertIn("Remove scaffold commit manifest", helper_task)
        # Failure/debug messages must use org/repo display identity, never the clone URL.
        self.assertIn("scaffold_repo_display", helper_task)
        self.assertIn("{{ scaffold_repo_display }}@{{ scaffold_branch }}", helper_task)
        self.assertNotIn("{{ scaffold_repo_url }}@", helper_task)
        self.assertEqual(all_provider_tasks.count("scaffold_repo_display:"), 14)

    def test_pipelines_share_registry_lifecycle_and_identity_contract(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("validate-registry", content, pipeline)
            self.assertIn("diff-tenants", content, pipeline)
            self.assertIn("tenant_id", content, pipeline)
            self.assertIn("aap_organization", content, pipeline)
            self.assertIn("team_name", content, pipeline)
            self.assertIn("repo_name", content, pipeline)
            self.assertIn("platform_repo", content, pipeline)
            self.assertNotIn("platform_repo_pattern", content, pipeline)
            self.assertNotIn("repo_name_overrides", content, pipeline)
            self.assertNotIn(".engine/schemas/naming-rules.yml", content, pipeline)

    def test_pipelines_enforce_lifecycle_on_pr_mr_validate(self):
        """Immutable tenant changes must fail PR/MR checks before merge."""
        gh_reusable = (ROOT / ".github/workflows/casc-validate-and-trigger.yml").read_text()
        gh_standalone = (
            ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml"
        ).read_text()
        gl = (ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml").read_text()

        for content, label in (
            (gh_reusable, "reusable"),
            (gh_standalone, "standalone"),
        ):
            self.assertIn("Enforce tenant lifecycle immutability", content, label)
            self.assertIn(
                "github.event_name == 'pull_request'",
                content,
                label,
            )
            self.assertIn(
                "CONTROL_REPO_TOKEN is required for tenant lifecycle validation",
                content,
                label,
            )
            # Validate-only gate: no AAP deploy secrets in the lifecycle step.
            enforce = content.split("Enforce tenant lifecycle immutability", 1)[1]
            enforce = enforce.split("- name:", 1)[0]
            self.assertIn("CONTROL_REPO_TOKEN", enforce, label)
            self.assertNotIn("AAP_ENGINE_TOKEN", enforce, label)
            self.assertNotIn("AAP_ENV_TARGETS_JSON", enforce, label)

        self.assertIn("validate:tenant-lifecycle:", gl)
        self.assertIn(
            "CONTROL_REPO_TOKEN is required for tenant lifecycle validation",
            gl,
        )
        lifecycle_job = gl.split("validate:tenant-lifecycle:", 1)[1]
        lifecycle_job = lifecycle_job.split("validate:policy-compliance:", 1)[0]
        self.assertIn("merge_request_event", lifecycle_job)
        self.assertIn("CONTROL_REPO_TOKEN", lifecycle_job)
        self.assertNotIn("AAP_ENGINE_TOKEN", lifecycle_job)
        self.assertNotIn("AAP_ENV_TARGETS_JSON", lifecycle_job)
        # Bootstrap remains push-only for JT launch (MR must not launch Bootstrap).
        bootstrap = gl.split("bootstrap:tenants:", 1)[1]
        bootstrap_script = bootstrap.split("rules:", 1)[0]
        self.assertIn('"${CI_PIPELINE_SOURCE}" != "push"', bootstrap_script)
        self.assertIn("Non-push pipeline", bootstrap_script)

    def test_pipelines_and_runtime_require_folder_layout_only(self):
        process = (ROOT / "roles/process_casc_config/tasks/main.yml").read_text()
        self.assertIn("Require folder-based layout", process)
        self.assertIn("Flat-root YAML is not supported", process)
        self.assertNotIn("Process with flat-file layout", process)
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("validate-structure", content, pipeline)
            self.assertIn("--caller-role", content, pipeline)
            self.assertIn("--control-config .control/config.yml", content, pipeline)
            self.assertIn("list-desired-state-dirs", content, pipeline)
            self.assertIn("yaml-to-json", content, pipeline)
            self.assertIn("paste -sd ' ' -", content, pipeline)
            self.assertNotIn("| tr '", content, pipeline)
            self.assertIn("Control repo: skipping desired-state", content, pipeline)
            self.assertNotIn("if os.path.isdir('base') else ['.']", content, pipeline)
            self.assertNotIn("ls -d */", content, pipeline)
            self.assertNotIn("bootstrap_dispatch_fanout", content, pipeline)
            self.assertNotIn("onboarding_dispatch", content, pipeline)
            self.assertNotIn("tenant_scm_namespace_id", content, pipeline)
            # All pipeline entrypoints must remain valid YAML.
            yaml.safe_load(content)

        naming_validator = (ROOT / "schemas/validate_naming.py").read_text()
        self.assertIn('".aap-casc-engine"', naming_validator)
        self.assertIn("caller_role", naming_validator)
        self.assertIn("desired_state_search_dirs", naming_validator)
        self.assertIn("env_branch_map", naming_validator)
        runtime = (ROOT / "scripts/pipeline/casc_runtime.py").read_text()
        self.assertIn("load_env_names", runtime)
        self.assertIn("env_branch_map", runtime)
        self.assertIn(".aap-casc-engine", runtime)
        self.assertIn("ENV_NAME_RE", runtime)

    def test_tenant_lifecycle_diff_fails_when_previous_commit_is_unavailable(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("git cat-file -e", content, pipeline)
            self.assertIn("refusing an unsafe tenant lifecycle diff", content, pipeline)

    def test_fanout_acceptance_matrix_contracts(self):
        """Onboarding fan-out applies platform state only on every provider."""
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertNotIn("onboarding_dispatch", content, pipeline)
            self.assertNotIn("bootstrap_dispatch_fanout", content, pipeline)
            self.assertNotIn("run_bounded_onboarding", content, pipeline)
            self.assertIn("dispatcher_launch.py", content, pipeline)
            self.assertIn("'dispatch_scope': 'platform'" if "gitlab" in str(pipeline) else '"dispatch_scope": "platform"', content, pipeline)
            self.assertNotIn("ONBOARDING_TENANTS", content, pipeline)
            self.assertNotIn("BOOTSTRAP_DISPATCH_TENANT_IDS", content, pipeline)
            self.assertIn('onboarding_mode", "greenfield") == "greenfield"', content, pipeline)

        # Recovery contract: docs require fanout-only retry, not full rerun.
        guide = (ROOT / "docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md").read_text()
        trigger = (ROOT / "docs/pipeline-trigger-logic.md").read_text()
        for doc in (guide, trigger):
            compact = " ".join(doc.lower().split())
            self.assertIn("retry only the failed", compact)
            self.assertIn("fanout", compact)
        self.assertIn("corrected", guide)
        self.assertIn("activated", guide)

    def test_gitlab_group_resolve_is_exact_get_fail_closed(self):
        helper = (ROOT / "tasks/gitlab_resolve_group.yml").read_text()
        self.assertIn("/api/v4/groups/", helper)
        self.assertIn("urlencode()", helper)
        self.assertIn("replace('/', '%2F')", helper)
        self.assertIn("full_path", helper)
        self.assertIn("401", helper)
        self.assertIn("403", helper)
        self.assertIn("404", helper)
        genesis = (ROOT / "tasks/genesis_scm_gitlab.yml").read_text()
        self.assertIn("gitlab_resolve_group.yml", genesis)
        # Internal facts use _gl_*_namespace_id; bare customer survey vars must not appear.
        self.assertNotRegex(genesis, r"(?<![_\w])platform_namespace_id(?![_\w])")
        self.assertNotRegex(genesis, r"(?<![_\w])control_namespace_id(?![_\w])")
        self.assertNotIn("PLATFORM_NAMESPACE_ID", genesis)
        self.assertNotIn("CONTROL_NAMESPACE_ID", genesis)
        bootstrap = (ROOT / "tasks/bootstrap_scm_gitlab.yml").read_text()
        self.assertIn("gitlab_resolve_group.yml", bootstrap)
        self.assertNotRegex(bootstrap, r"(?<![_\w])tenant_scm_namespace_id(?![_\w])")
        self.assertNotIn("TENANT_SCM_NAMESPACE_ID", bootstrap)

    def test_dispatcher_and_drift_have_no_naming_policy_dependency(self):
        for path in (ROOT / "site.yml", ROOT / "drift-detect.yml"):
            content = path.read_text()
            self.assertNotIn("naming-rules", content, path)
            self.assertNotIn("validate_naming", content, path)

    def test_generated_callers_use_control_token_without_continuation_inputs(self):
        callers = (
            ROOT / "templates/github-workflow-caller.yml.j2",
            ROOT / "templates/gitlab-ci-caller.yml.j2",
        )
        for caller in callers:
            content = caller.read_text()
            self.assertIn("CONTROL", content, caller)
            self.assertNotIn("onboarding_dispatch", content, caller)
            self.assertNotIn("CASC_OPERATION", content, caller)
            # Protected continuation inputs were removed; Bootstrap gets tenant_id
            # from control tenants.yml / JT survey, not thin callers.
            self.assertNotIn("tenant_id:", content, caller)

    def test_github_bootstrap_wires_aap_engine_host_from_vars(self):
        workflow = (ROOT / ".github/workflows/casc-validate-and-trigger.yml").read_text()
        self.assertIn("aap_engine_host:", workflow)
        self.assertIn("AAP_ENGINE_HOST: ${{ inputs.aap_engine_host }}", workflow)
        self.assertIn("aap_engine_host and AAP_ENGINE_TOKEN", workflow)
        caller = (ROOT / "templates/github-workflow-caller.yml.j2").read_text()
        self.assertIn("aap_engine_host:", caller)
        self.assertIn("vars.AAP_ENGINE_HOST", caller)
        # Genesis remains decoupled from the CI host wiring.
        genesis = (ROOT / "genesis.yml").read_text()
        self.assertNotIn("aap_engine_host", genesis)
        self.assertNotRegex(genesis, r"(?<![_\w])AAP_ENGINE_HOST(?![_\w])")

    def test_gitlab_embedded_python_compiles_with_top_level_helpers(self):
        import ast
        import re
        import textwrap

        content = (ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml").read_text()
        snippets: list[tuple[str, str]] = []
        # Multiline python3 -c blocks only (skip one-liners with trailing shell args).
        for match in re.finditer(
            r'python3\s+-c\s+"\n((?:.*\n)*?)\s*"\s*$', content, flags=re.M
        ):
            snippets.append(("python3 -c", match.group(1)))
        for match in re.finditer(
            r"python3\s+<<'([A-Z0-9_]+)'\n(.*?)^\s*\1\s*$",
            content,
            flags=re.M | re.S,
        ):
            snippets.append((match.group(1), match.group(2)))
        self.assertGreaterEqual(len(snippets), 3, "expected embedded Python blocks")
        compiled_names: set[str] = set()
        for label, raw in snippets:
            source = textwrap.dedent(raw.replace('\\"', '"'))
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                self.fail(f"{label} failed to compile: {exc}\n{source[:400]}")
            compile(source, f"<{label}>", "exec")
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    compiled_names.add(node.name)
        self.assertIn("engine_curl_args", compiled_names)
        self.assertIn("build_bootstrap_extra_vars", compiled_names)
        # Nested def after return would not appear as a top-level FunctionDef.
        bootstrap = next(src for name, src in snippets if name == "BOOTSTRAP_PY")
        bootstrap_tree = ast.parse(textwrap.dedent(bootstrap))
        top_level = {
            node.name
            for node in bootstrap_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(
            top_level,
            {"engine_curl_args", "build_bootstrap_extra_vars"},
        )

    def test_generated_callers_render_for_every_role(self):
        jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        context = {
            "platform_scm_org": "ww-platform",
            "engine_repo": "aap-casc-engine",
            "control_scm_org": "ww-platform",
            "control_repo": "casc-platform-control",
            "control_branch": "main",
        }
        for role in ("control", "platform", "tenant"):
            with self.subTest(provider="github", role=role):
                rendered = jinja.get_template(
                    "templates/github-workflow-caller.yml.j2"
                ).render(**context, caller_role=role)
                yaml.safe_load(rendered)
                self.assertIn(f"caller_role: {role}", rendered)
                self.assertEqual("AAP_ENGINE_TOKEN" in rendered, role == "control")
                self.assertEqual("aap_engine_host:" in rendered, role == "control")
                self.assertEqual("vars.AAP_ENGINE_HOST" in rendered, role == "control")
            with self.subTest(provider="gitlab", role=role):
                rendered = jinja.get_template(
                    "templates/gitlab-ci-caller.yml.j2"
                ).render(**context, caller_role=role)
                yaml.safe_load(rendered)
                self.assertIn(f"CASC_CALLER_ROLE: '{role}'", rendered)

    def test_dispatch_pause_does_not_change_platform_only_onboarding(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("dispatch_enabled", content, pipeline)
            # Unused aggregate tenant_ids output/env must stay gone.
            self.assertNotRegex(content, r"(?m)^\s*tenant_ids:")
            self.assertNotIn('echo "tenant_ids=', content, pipeline)
            self.assertNotIn("BOOTSTRAP_TENANT_IDS", content, pipeline)
            self.assertNotIn("BOOTSTRAP_DISPATCH_TENANT_IDS", content)
            self.assertNotIn("dispatch_tenant_ids", content)
            self.assertIn(
                "BOOTSTRAP_FANOUT_TENANT_IDS"
                if "gitlab" in str(pipeline)
                else "fanout_tenant_ids",
                content,
            )

    def test_cross_namespace_clone_paths_are_collision_safe(self):
        role = (ROOT / "roles/git_clone_repos/tasks/main.yml").read_text()
        self.assertIn("item.clone_name | default(item.name)", role)
        for playbook in (ROOT / "site.yml", ROOT / "drift-detect.yml"):
            content = playbook.read_text()
            self.assertIn("item.tenant_id + '__' + item.repository", content)
            self.assertIn("casc_repo.clone_name | default(casc_repo.name)", content)

    def test_control_revision_is_an_immutable_commit_pin(self):
        with self.assertRaisesRegex(ValueError, "full hexadecimal commit SHA"):
            casc_runtime.ensure_control_files(
                provider="github",
                org="ww-platform",
                repo="casc-platform-control",
                branch="main",
                token="token",
                revision="main",
            )

        def control_file(**kwargs):
            if kwargs["path"] == "config.yml":
                return "env_branch_map:\n  dev: develop\n"
            if kwargs["path"] == "tenants.yml":
                return "tenants: []\n"
            raise urllib.error.HTTPError("url", 404, "missing", {}, None)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            casc_runtime, "fetch_control_text", side_effect=control_file
        ):
            revision = casc_runtime.ensure_control_files(
                provider="github",
                org="ww-platform",
                repo="casc-platform-control",
                branch="main",
                token="token",
                revision="A" * 40,
                dest_dir=tmp,
            )
            self.assertEqual(revision, "a" * 40)
            self.assertTrue((Path(tmp) / "config.yml").exists())
            self.assertTrue((Path(tmp) / "tenants.yml").exists())
            self.assertFalse((Path(tmp) / "naming-rules.yml").exists())

    def test_deleted_bootstrap_templates_have_no_consumers(self):
        deleted = (
            "user-template.yml.j2",
            "rbac-user-template.yml.j2",
            "rbac-team-template.yml.j2",
            "seed-combined-tenant.yml.j2",
        )
        all_text = "\n".join(
            path.read_text(errors="ignore")
            for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and "tests" not in path.parts
            and path.suffix in {".yml", ".j2", ".py", ".md"}
        )
        for name in deleted:
            self.assertFalse((ROOT / "templates" / name).exists())
            self.assertNotIn(name, all_text)


class DocumentationContractTests(unittest.TestCase):
    def test_required_docs_cover_lean_contract(self):
        docs = [
            ROOT / "README.md",
            ROOT / "docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md",
            ROOT / "docs/NONPRODUCTION_VALIDATION.md",
            ROOT / "docs/pipeline-trigger-logic.md",
            ROOT / "docs/resource-deletion-capabilities.md",
            ROOT / "docs/TENANT_RETIREMENT_RUNBOOK.md",
            ROOT / "templates/genesis-readme.md.j2",
        ]
        combined = "\n".join(path.read_text() for path in docs)
        for required in (
            "tenant_id",
            "aap_organization",
            "Brownfield",
            "naming-rules.yml",
            "Organization",
            "Team",
        ):
            self.assertIn(required, combined)
        guide = (ROOT / "docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md").read_text()
        self.assertIn("combined-only", guide)
        self.assertNotIn("Per-resource-type layouts remain available", guide)
        # ROADMAP-010 removed product advertising of these terms.
        for stale in (
            "bootstrap_dispatch_fanout",
            "onboarding_dispatch",
            "dispatcher_concurrency",
        ):
            self.assertNotIn(stale, combined, stale)
        self.assertIn("tenant retirement", combined.lower())
        self.assertIn("fanout", combined.lower())
        self.assertIn("AAP_ENV_TARGETS_JSON", guide)
        trigger = (ROOT / "docs/pipeline-trigger-logic.md").read_text()
        self.assertIn("`fanout`", trigger)
        nonprod = (ROOT / "docs/NONPRODUCTION_VALIDATION.md").read_text()
        self.assertIn("skips Bootstrap, fan-out, and trigger", nonprod)
        retirement = (ROOT / "docs/TENANT_RETIREMENT_RUNBOOK.md").read_text()
        self.assertIn("Remove markers (required before registry removal)", retirement)
        self.assertLess(
            retirement.index("Remove markers"),
            retirement.index("Remove registry entry"),
        )
        self.assertIn("[skip dispatch]", retirement)
        self.assertIn("_retirement_archive/", retirement)
        self.assertIn("outside `base/`", retirement)


if __name__ == "__main__":
    unittest.main()
