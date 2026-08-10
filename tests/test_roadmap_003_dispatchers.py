"""Contracts for optional tenant-bound Dispatcher Job Templates."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))

import casc_runtime  # noqa: E402
import dispatcher_launch  # noqa: E402


def config(**overrides):
    value = {
        "scm_provider": "github",
        "scm_base_url": "https://github.com",
        "control_scm_org": "example-platform",
        "control_repo": "casc-platform-control",
        "control_branch": "main",
        "platform_scm_org": "example-platform",
        "platform_repo": "casc-platform-global",
        "env_branch_map": {"poc": "dev", "prod": "main"},
        "job_templates": {"dispatcher": "jt-platform-casc_dispatcher"},
    }
    value.update(overrides)
    return value


def tenant(**overrides):
    value = {
        "tenant_id": "stores",
        "team_name": "Stores Automation",
        "tenant_scm_org": "example-tenants",
        "onboarding_mode": "greenfield",
    }
    value.update(overrides)
    return value


def dispatcher_defaults():
    return {
        "project": "prj-platform-casc_engine",
        "inventory": "inv-platform-localhost",
        "execution_environment": "ee-platform-casc",
        "credentials": ["crd-platform-scm", "crd-platform-aap"],
        "launcher_user": "svc_casc_launcher",
    }


class RegistryAndRoutingTests(unittest.TestCase):
    def test_shared_dispatcher_remains_default(self):
        route = casc_runtime.resolve_dispatch_route(
            {"tenants": [tenant()]},
            config(),
            caller_role="tenant",
            target_env="prod",
            triggered_repo="example-tenants/casc-tenant-stores",
        )
        self.assertFalse(route["dedicated"])
        self.assertEqual(route["job_template"], "jt-platform-casc_dispatcher")
        self.assertEqual(route["dispatch_scope"], "tenant")

    def test_dedicated_route_uses_fixed_binding(self):
        cfg = config(tenant_dispatcher_defaults=dispatcher_defaults())
        route = casc_runtime.resolve_dispatch_route(
            {
                "tenants": [
                    tenant(dispatcher_job_template="jt-platform-casc_dispatcher-stores")
                ]
            },
            cfg,
            caller_role="tenant",
            target_env="poc",
            triggered_repo="example-tenants/casc-tenant-stores",
        )
        self.assertTrue(route["dedicated"])
        self.assertEqual(route["job_template"], "jt-platform-casc_dispatcher-stores-poc")
        self.assertEqual(
            route["fixed_extra_vars"],
            {
                "scm_base_url": "https://github.com",
                "platform_scm_org": "example-platform",
                "target_env": "poc",
                "dispatch_scope": "tenant",
                "tenant_id": "stores",
                "triggered_repo": "example-tenants/casc-tenant-stores",
                "control_scm_org": "example-platform",
                "control_repo": "casc-platform-control",
                "control_branch": "main",
            },
        )

    def test_dedicated_registry_requires_one_shared_defaults_mapping(self):
        tenants = {
            "tenants": [tenant(dispatcher_job_template="jt-stores-dispatcher")]
        }
        with self.assertRaisesRegex(ValueError, "tenant_dispatcher_defaults"):
            casc_runtime.validate_tenant_registry(tenants, config())

        normalized = casc_runtime.validate_tenant_registry(
            tenants,
            config(tenant_dispatcher_defaults=dispatcher_defaults()),
        )
        self.assertEqual(normalized[0]["dispatcher_job_template"], "jt-stores-dispatcher")

    def test_dedicated_names_are_unique_and_not_shared(self):
        cfg = config(tenant_dispatcher_defaults=dispatcher_defaults())
        with self.assertRaisesRegex(ValueError, "central engine Job Templates"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        tenant(dispatcher_job_template="jt-platform-casc_dispatcher")
                    ]
                },
                cfg,
            )
        with self.assertRaisesRegex(ValueError, "assigned to both"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        tenant(dispatcher_job_template="jt-tenant-dispatcher"),
                        tenant(
                            tenant_id="network",
                            team_name="Network Automation",
                            dispatcher_job_template="jt-tenant-dispatcher",
                        ),
                    ]
                },
                cfg,
            )

    def test_env_bound_dispatcher_names_cannot_collide_with_engine_jts(self):
        cfg = config(
            tenant_dispatcher_defaults=dispatcher_defaults(),
            env_branch_map={"casc_dispatcher": "main"},
            job_templates={"dispatcher": "jt-platform-casc_dispatcher"},
        )
        with self.assertRaisesRegex(ValueError, "env-bound Dispatcher JT"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        tenant(dispatcher_job_template="jt-platform")
                    ]
                },
                cfg,
            )

    def test_dedicated_route_uses_persisted_custom_scm_base_url(self):
        cfg = config(
            scm_provider="gitlab",
            scm_base_url="https://gitlab.example.com/",
            tenant_dispatcher_defaults=dispatcher_defaults(),
        )
        route = casc_runtime.resolve_dispatch_route(
            {
                "tenants": [
                    tenant(dispatcher_job_template="jt-stores-dispatcher")
                ]
            },
            cfg,
            caller_role="tenant",
            target_env="prod",
            triggered_repo="example-tenants/casc-tenant-stores",
        )
        self.assertEqual(
            route["fixed_extra_vars"]["scm_base_url"],
            "https://gitlab.example.com/",
        )

        cfg.pop("scm_base_url")
        with self.assertRaisesRegex(ValueError, "scm_base_url"):
            casc_runtime.resolve_dispatch_route(
                {
                    "tenants": [
                        tenant(dispatcher_job_template="jt-stores-dispatcher")
                    ]
                },
                cfg,
                caller_role="tenant",
                target_env="prod",
                triggered_repo="example-tenants/casc-tenant-stores",
            )

    def test_brownfield_requires_existing_team_reference(self):
        with self.assertRaisesRegex(ValueError, "team_name"):
            casc_runtime.normalize_tenant_record(
                tenant(
                    onboarding_mode="brownfield",
                    aap_organization="Existing Stores",
                    team_name="",
                )
            )
        runtime = casc_runtime.public_tenant_runtime(
            tenant(
                onboarding_mode="brownfield",
                aap_organization="Existing Stores",
                team_name="Existing Operators",
            )
        )
        self.assertEqual(runtime["team_name"], "Existing Operators")

    def test_marker_v5_protects_team_and_dispatcher_binding(self):
        marker = casc_runtime.build_scaffold_marker(
            tenant(dispatcher_job_template="jt-stores-dispatcher"),
            repository="casc-tenant-stores",
        )
        self.assertEqual(marker["scaffold_version"], 5)
        self.assertEqual(marker["team_name"], "Stores Automation")
        self.assertEqual(marker["dispatcher_job_template"], "jt-stores-dispatcher")
        renamed = dict(marker, dispatcher_job_template="jt-renamed")
        with self.assertRaisesRegex(ValueError, "dispatcher_job_template"):
            casc_runtime.validate_scaffold_marker(marker, renamed)

class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        self.jinja.filters["to_json"] = json.dumps

    def test_bootstrap_renders_central_jt_and_execute_roles(self):
        context = {
            "_effective_dispatcher_job_template": "jt-platform-casc_dispatcher-stores",
            "_effective_tenant_id": "stores",
            "_effective_team_name": "Stores Automation",
            "_effective_aap_organization": "stores",
            "_tenant_dispatcher_defaults": dispatcher_defaults(),
            "_dispatcher_target_env": "prod",
            "_env_branch_map": {"poc": "dev", "prod": "main"},
            "tenant_scm_org": "example-tenants",
            "_tenant_repository": "casc-tenant-stores",
            "scm_base_url": "https://github.com",
            "control_scm_org": "example-platform",
            "control_repo": "casc-platform-control",
            "control_branch": "main",
            "platform_scm_org": "example-platform",
        }
        jt = yaml.safe_load(
            self.jinja.get_template(
                "templates/tenant-dispatcher-job-template.yml.j2"
            ).render(**context)
        )["controller_templates"][0]
        self.assertEqual(jt["name"], "jt-platform-casc_dispatcher-stores-prod")
        self.assertEqual(jt["extra_vars"]["target_env"], "prod")
        self.assertEqual(jt["extra_vars"]["scm_base_url"], "https://github.com")
        self.assertEqual(jt["extra_vars"]["platform_scm_org"], "example-platform")
        self.assertFalse(jt["ask_variables_on_launch"])
        self.assertFalse(jt["survey_enabled"])
        self.assertFalse(jt["allow_simultaneous"])
        self.assertEqual(jt["extra_vars"]["dispatch_scope"], "tenant")
        self.assertNotIn("organization", jt)
        self.assertEqual(jt["project"], dispatcher_defaults()["project"])


class LauncherTests(unittest.TestCase):
    def test_shared_launch_keeps_existing_runtime_variables(self):
        route = {
            "dedicated": False,
            "job_template": "jt-shared",
            "target_env": "prod",
            "dispatch_scope": "tenant",
            "tenant_id": "stores",
            "triggered_repo": "example-tenants/casc-tenant-stores",
        }
        responses = [
            {
                "count": 1,
                "results": [
                    {
                        "id": 41,
                        "name": "jt-shared",
                        "allow_simultaneous": False,
                    }
                ],
            },
            {"id": 98},
            {"status": "successful"},
        ]
        with mock.patch.dict(
            "os.environ",
            {
                "AAP_ENV_TARGETS_JSON": json.dumps(
                    {"prod": {"host": "aap", "token": "x"}}
                ),
                "CONTROL_REVISION": "abc123",
                "TRIGGER_COMMIT": "def456",
                "TRIGGER_SOURCE": "ci-cd-pipeline",
            },
            clear=False,
        ), mock.patch.object(
            dispatcher_launch, "_request", side_effect=responses
        ) as request, mock.patch.object(dispatcher_launch.time, "sleep"):
            dispatcher_launch.launch_and_wait(route, 1)

        payload = request.call_args_list[1].args[3]
        self.assertEqual(
            json.loads(payload["extra_vars"]),
            {
                "target_env": "prod",
                "dispatch_scope": "tenant",
                "tenant_id": "stores",
                "triggered_repo": "example-tenants/casc-tenant-stores",
                "trigger_commit": "def456",
                "trigger_source": "ci-cd-pipeline",
                "control_revision": "abc123",
            },
        )

    def test_dedicated_launch_sends_no_runtime_extra_vars(self):
        route = {
            "dedicated": True,
            "job_template": "jt-stores",
            "target_env": "prod",
            "dispatch_scope": "tenant",
            "fixed_extra_vars": {"target_env": "prod", "tenant_id": "stores"},
        }
        responses = [
            {
                "count": 1,
                "results": [
                    {
                        "id": 42,
                        "name": "jt-stores",
                        "allow_simultaneous": False,
                        "ask_variables_on_launch": False,
                        "survey_enabled": False,
                        "extra_vars": {"target_env": "prod", "tenant_id": "stores"},
                    }
                ],
            },
            {"id": 99},
            {"status": "successful"},
        ]
        with mock.patch.dict(
            "os.environ",
            {"AAP_ENV_TARGETS_JSON": json.dumps({"prod": {"host": "aap", "token": "x"}})},
            clear=False,
        ), mock.patch.object(
            dispatcher_launch, "_request", side_effect=responses
        ) as request, mock.patch.object(dispatcher_launch.time, "sleep"):
            dispatcher_launch.launch_and_wait(route, 1)
        self.assertEqual(request.call_args_list[1].args[3], {})

    def test_binding_mismatch_fails_before_launch(self):
        route = {
            "dedicated": True,
            "job_template": "jt-stores",
            "target_env": "prod",
            "dispatch_scope": "tenant",
            "fixed_extra_vars": {"tenant_id": "stores"},
        }
        lookup = {
            "count": 1,
            "results": [
                {
                    "id": 42,
                    "name": "jt-stores",
                    "allow_simultaneous": False,
                    "ask_variables_on_launch": False,
                    "survey_enabled": False,
                    "extra_vars": {"tenant_id": "other"},
                }
            ],
        }
        with mock.patch.dict(
            "os.environ",
            {"AAP_ENV_TARGETS_JSON": json.dumps({"prod": {"host": "aap", "token": "x"}})},
            clear=False,
        ), mock.patch.object(dispatcher_launch, "_request", return_value=lookup):
            with self.assertRaisesRegex(
                dispatcher_launch.DispatcherLaunchError, "binding mismatch"
            ):
                dispatcher_launch.launch_and_wait(route, 1)

    def test_missing_dedicated_jt_fails_without_shared_fallback(self):
        route = {
            "dedicated": True,
            "job_template": "jt-stores",
            "target_env": "prod",
            "dispatch_scope": "tenant",
            "fixed_extra_vars": {"tenant_id": "stores"},
        }
        with mock.patch.dict(
            "os.environ",
            {
                "AAP_ENV_TARGETS_JSON": json.dumps(
                    {"prod": {"host": "aap", "token": "x"}}
                )
            },
            clear=False,
        ), mock.patch.object(
            dispatcher_launch,
            "_request",
            return_value={"count": 0, "results": []},
        ) as request:
            with self.assertRaisesRegex(
                dispatcher_launch.DispatcherLaunchError, "exactly one"
            ):
                dispatcher_launch.launch_and_wait(route, 1)
        self.assertEqual(request.call_count, 1)


class PipelineTests(unittest.TestCase):
    def test_control_config_persists_scm_base_url(self):
        seed = (ROOT / "templates/seed-config.yml.j2").read_text(encoding="utf-8")
        bootstrap = (ROOT / "bootstrap.yml").read_text(encoding="utf-8")
        self.assertIn("scm_base_url: {{ scm_base_url | to_json }}", seed)
        self.assertIn("Verify authoritative SCM connection", bootstrap)
        self.assertIn("control_config.scm_base_url == scm_base_url", bootstrap)

    def test_all_pipelines_use_platform_only_onboarding_and_route_later_commits(self):
        pipelines = (
            ROOT / ".github/workflows/casc-validate-and-trigger.yml",
            ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml",
            ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml",
        )
        for path in pipelines:
            content = path.read_text(encoding="utf-8")
            self.assertIn("dispatcher_launch.py", content, path)
            self.assertIn("resolve-dispatch-route", content, path)
            self.assertNotIn("ONBOARDING_TENANTS", content, path)
            self.assertNotIn("BOOTSTRAP_DISPATCH_TENANT_IDS", content, path)
            self.assertNotIn("dispatch_tenant_ids", content, path)

    def test_shared_full_scope_is_refused_when_dedicated_jt_exists(self):
        site = (ROOT / "site.yml").read_text(encoding="utf-8")
        self.assertIn("Refuse shared full scope", site)
        self.assertIn("dispatcher_job_template", site)
        self.assertLess(
            site.index("Record normalized tenant runtime data"),
            site.index("Refuse shared full scope"),
        )

    def test_platform_dispatch_resolves_team_id_after_resource_apply(self):
        site = (ROOT / "site.yml").read_text(encoding="utf-8")
        dispatch = site.index("role: infra.aap_configuration.dispatch")
        resolve_org = site.index("Resolve tenant Dispatcher Organizations by exact name")
        resolve_team = site.index("Resolve tenant Dispatcher Teams by exact name and Organization")
        grant_team = site.index("Grant launcher and tenant Team Execute on tenant-bound Dispatchers")
        self.assertLess(dispatch, resolve_org)
        self.assertLess(resolve_org, resolve_team)
        self.assertLess(resolve_team, grant_team)
        self.assertIn("ansible.controller.controller_api", site)
        self.assertIn("'name': item.aap_organization", site)
        self.assertIn("'name': item.team_name", site)
        self.assertIn("'organization': _tenant_dispatcher_organization_ids[item.tenant_id]", site)
        self.assertIn("team: \"{{ _tenant_dispatcher_team_ids[item.tenant_id] }}\"", site)
        self.assertIn("user: \"{{ control_config.tenant_dispatcher_defaults.launcher_user }}\"", site)
        self.assertGreaterEqual(site.count("expect_one=true"), 2)
        self.assertGreaterEqual(site.count("return_ids=true"), 2)

    def test_bootstrap_does_not_generate_named_url_role_files(self):
        bootstrap = (ROOT / "bootstrap.yml").read_text(encoding="utf-8")
        self.assertNotIn("tenant-dispatcher-roles.yml.j2", bootstrap)
        self.assertNotIn("/roles/' + _effective_tenant_id + '-dispatcher.yml", bootstrap)
        self.assertFalse((ROOT / "templates/tenant-dispatcher-roles.yml.j2").exists())

if __name__ == "__main__":
    unittest.main()
