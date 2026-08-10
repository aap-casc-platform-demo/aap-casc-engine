# aap-casc-engine

**AAP Multi-Tenant CasC Engine** provides a simple YAML interface for
multi-team, multi-environment AAP Configuration-as-Code. It keeps
`infra.aap_configuration` as the supported apply backend while centralizing the
repository scaffolding, validation, environment overlays, dispatch, onboarding,
and drift workflows that customers would otherwise build themselves.

## What the engine provides

- One central pipeline service for GitHub Actions and GitLab CI.
- Separate control, platform desired-state, and tenant desired-state repositories.
- Multiple YAML files per resource type, merged by the engine before dispatch.
- `base/` plus environment-overlay folders driven by `env_branch_map`.
- Repository creation or governed pre-created repository scaffolding.
- Combined control, platform, and tenant repositories with optional custom names.
- Greenfield and Brownfield tenant onboarding.
- Optional central tenant-bound Dispatcher JTs for independent tenant runs;
  omission keeps the shared serialized Dispatcher.
- Optional, customer-owned naming policy.
- Scoped platform or tenant dispatch through `infra.aap_configuration.dispatch`.
- Report-only Drift for missing declared Organizations, teams, credential types, projects, and inventories (apply via Dispatcher).

## Architecture

| Repository | Purpose |
|---|---|
| `aap-casc-engine` | Playbooks, schemas, reusable pipelines, and templates |
| `casc-platform-control` | Mandatory `config.yml` and `tenants.yml`; optional `naming-rules.yml` |
| Platform desired-state repo | Shared Organization, Team, RBAC, settings, and other platform YAML |
| Tenant desired-state repo | Tenant projects, inventories, credentials, templates, workflows, schedules, and notifications |

The control repository never contains AAP desired-state YAML. Normal platform
and tenant pushes dispatch only their own scope; a tenant dispatch does not
reapply platform desired state.

## Consumer interface

A tenant can keep each object in a separate file:

```text
casc-tenant-stores/
├── base/
│   ├── projects/project-deploy.yml
│   ├── inventories/inventory-dev.yml
│   └── templates/job-template-deploy.yml
├── dev/inventories/inventory-dev.yml
├── prd/inventories/inventory-dev.yml
└── .github/workflows/casc.yml
```

Each file uses an `infra.aap_configuration` variable key:

```yaml
---
controller_projects:
  - name: Stores Deployment
    organization: Example Stores Automation
    scm_type: git
    scm_url: https://github.example/example/stores-automation.git
    scm_branch: main
```

Filenames are organizational only. Optional naming policy validates resource
identities inside YAML, not filenames.

## Quick start

### Prerequisites

- Red Hat Ansible Automation Platform with an execution environment / project
  collection install that resolves to pinned
  `infra.aap_configuration==4.7.0` ([`collections/requirements.yml`](collections/requirements.yml);
  formal marketed support matrix remains ROADMAP-008).
- Declarative resource allowlist in [`schemas/resource-types.yml`](schemas/resource-types.yml):
  keyed scalar identities, raw settings, and atomic scoped/compound types are
  supported (plus engine-side `hub_roles` / `hub_group_roles`); launch/bulk/sync
  action keys remain unsupported. CI and Dispatcher share one merge contract.
- Human-facing [`Resource Catalog`](docs/RESOURCE_CATALOG.md) with one valid
  non-secret YAML example and the pinned field reference for every supported
  resource key.
- Inert comprehensive naming sample at
  [`examples/naming-rules.yml.sample`](examples/naming-rules.yml.sample)
  (catalog-generated; never auto-activates).
- GitHub or GitLab API access. The GitHub path is live-validated; GitLab remains
  static template parity only (live validation deferred).
- Multi-AAP topology as documented in the Setup and Operations Guide:
  Genesis/Bootstrap on a management AAP; Dispatcher on every host listed in
  `AAP_ENV_TARGETS_JSON`; Drift as an AAP JT (not CI-launched).
- Least-privilege launcher tokens (Execute only on the intended Job Template).
- SCM and AAP credentials described in the
  [Setup and Operations Guide](docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md).

### 1. Run Genesis

Genesis creates repositories when `repo_mode=create`, or scaffolds repositories
that already exist when `repo_mode=existing`. Pre-created repositories may be
empty when branch creation is enabled; the engine publishes the **full** managed
scaffold as the first ordinary-Git commit on each operation branch (never a
marker-only or README-only init), then creates the high-to-low environment branch
topology. Each `(repository, branch)` scaffold publish is zero or one coherent
commit.

```bash
export SCM_TOKEN='<scm-api-token>'
export SCM_BASE_URL='https://github.com'

ansible-playbook genesis.yml \
  -e platform_scm_org=example-platform \
  -e control_scm_org=example-platform \
  -e control_repo=casc-platform-control \
  -e platform_repo=casc-platform-global \
  -e repo_mode=create
```

Genesis creates the control and platform repositories (shortest path), then seeds
`config.yml` and `tenants.yml`. It does not activate a naming policy by default.
Use `repo_mode=existing` only when those repositories are pre-created under
customer governance.

### 2. Register a Greenfield tenant

```yaml
---
tenants:
  - tenant_id: stores
    aap_organization: Example Stores Automation  # optional; defaults to tenant_id
    team_name: Stores Automation
    tenant_scm_org: example-tenants
    repo_name: stores-aap-casc               # optional; default casc-tenant-stores
    repo_mode: create
    onboarding_mode: greenfield
    status: active
```

Greenfield Bootstrap scaffolds the tenant repository and writes two platform
foundation declarations on every mapped branch:

- `base/organizations/stores.yml`
- `base/teams/stores.yml`

Users, IdP mappings, RBAC assignments, credentials, Galaxy associations, and
execution-environment associations remain normal customer desired state.

### 3. Register a Brownfield tenant

```yaml
---
tenants:
  - tenant_id: legacy_app
    aap_organization: Existing LDAP/SAML Organization
    team_name: Existing Automation Team
    tenant_scm_org: example-tenants
    repo_name: legacy-app-aap-casc
    repo_mode: create
    onboarding_mode: brownfield
    status: active
```

Brownfield Bootstrap requires exact existing AAP Organization and Team names
and never creates or modifies them. With the shared Dispatcher it is SCM-only.
When `dispatcher_job_template` is configured, Bootstrap additionally scaffolds
the central tenant-bound Dispatcher and Execute roles as platform desired state;
the normal platform apply resolves those references before onboarding completes.
Onboarding never applies tenant desired state. Use `repo_mode=existing` when the
tenant repository is pre-created.

### 4. Apply one scope

```bash
ansible-playbook site.yml \
  -e target_env=dev \
  -e dispatch_scope=tenant \
  -e tenant_id=stores
```

## Tenant identity

| Field | Meaning |
|---|---|
| `tenant_id` | Required stable engine key. Must match `^[a-z][a-z0-9_]*$`, maximum 64 characters. |
| `aap_organization` | Exact AAP Organization name. Optional for Greenfield; required for Brownfield. |
| `team_name` | Required exact Team name. Greenfield creates it; Brownfield references the existing Team without modifying it. |
| `dispatcher_job_template` | Optional customer-owned central Dispatcher JT name. Omit it to use the shared Dispatcher. |

Repository routing is `repository -> tenants.yml -> tenant_id`. It never infers
the AAP Organization from a repository name.

## Repository names

The engine is combined-only:

| Scope | Config field | Default |
|---|---|---|
| Platform desired state | `platform_repo` in `config.yml` | `casc-platform-global` |
| Tenant desired state | optional `repo_name` in `tenants.yml` | `casc-tenant-<tenant_id>` |

Runtime and scaffold markers expose the resolved scalar `repository`. Legacy
per-resource fields (`repo_pattern`, `repo_names`, `platform_repo_pattern`,
`platform_repo_names`, `platform_repos`) are rejected. Blank names or collisions
with control/platform ownership fail before SCM mutation.

## Environment branches

`env_branch_map` is ordered from lowest to highest environment:

```yaml
env_branch_map:
  dev: develop
  tst: release/tst
  prd: main
```

Missing branches are created from high to low so lower environments start from
the approved higher-environment baseline. Changes are promoted low to high.
Feature-branch pushes and pull/merge requests validate only; mapped-branch
pushes dispatch to the corresponding environment.

## Optional naming policy

Naming validation is inactive when control-root `naming-rules.yml` is missing or
empty. Genesis seeds inert `naming-rules.yml.sample` from
`examples/naming-rules.yml.sample` onto the control repository `control_branch`
only (environment branches belong to desired-state repos). Rename, adapt, and
uncomment rules as `naming-rules.yml` to activate. The exact rendered
Greenfield Organization and Team are validated before any SCM mutation. A rule
applies only to resource types explicitly present in the policy.

## Safety boundaries

- Scaffold markers make tenant identity and repository topology immutable after
  scaffolding starts; pre-scaffold corrections remain allowed.
- `status` and `dispatch_enabled` are mutable operational controls.
- A paused new Greenfield tenant is still scaffolded and receives its
  platform-owned Organization/Team foundation; tenant desired state waits until
  `dispatch_enabled` is re-enabled.
- Absence from SCM is never interpreted as deletion.
- Generated user examples contain no password, and apply paths disable the
  collection's `change_me` fallback.
- The production baseline is serialized and requires Dispatcher
  `allow_simultaneous=false`.
- Optional tenant-bound Dispatcher JTs reuse the shared Project, Inventory, EE,
  credentials, and CI launcher. Each JT remains serialized while different
  tenant JTs may run independently.
- Tenant-bound JTs separate execution queues; because they reuse one apply
  credential, trusted authors, branch protection, and approvals remain the
  tenant desired-state security boundary.

## Current limitations

- Launch, bulk-host create, and repository-sync action keys remain unsupported.
- Drift is report-only (`identity_presence`) for Organizations, teams,
  credential types, projects, and inventories. Undeclared live objects are
  ignored. Apply declared state by launching the Dispatcher Job Template.

## Documentation

- [Resource Catalog](docs/RESOURCE_CATALOG.md) — supported resource keys,
  complete examples, parameters, merge behavior, ownership, and current safety
  capabilities
- [Setup and Operations Guide](docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md) —
  canonical install, multi-AAP topology, secrets lifecycle, and day-2 recovery
- [Tenant Retirement Runbook](docs/TENANT_RETIREMENT_RUNBOOK.md) — manual
  fail-safe procedure for removing scaffolded tenants
- [Pipeline Trigger Logic](docs/pipeline-trigger-logic.md)
- [Nonproduction Validation](docs/NONPRODUCTION_VALIDATION.md)
- [Resource Deletion Capabilities](docs/resource-deletion-capabilities.md)

## License

[GPL-3.0](LICENSE)
