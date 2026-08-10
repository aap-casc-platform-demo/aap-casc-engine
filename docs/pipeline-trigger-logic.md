# Pipeline Trigger Logic Reference

Authoritative behavior for the GitHub reusable workflow, GitHub standalone
workflow, and GitLab template.

## User action matrix

| User action | Pipeline path | AAP effect |
|---|---|---|
| Push to an environment-mapped platform branch | `validate -> trigger` | Applies platform scope to that environment |
| Push to an environment-mapped tenant branch | `validate -> trigger` | Resolves repository to `tenant_id` and applies only that tenant scope |
| Push to an unmapped feature branch | `validate` | None |
| Pull request / merge request to any target branch | `validate` | None; deploy credentials are not exposed |
| Control-branch push adding/correcting active Greenfield tenant | `validate -> bootstrap -> fanout` | SCM scaffold, Organization + Team foundation, optional tenant Dispatcher/RBAC, then platform-only apply across all environments |
| Control-branch push adding Brownfield tenant | `validate -> bootstrap` or `validate -> bootstrap -> fanout` | SCM scaffold; existing Org/Team unchanged. Platform-only fan-out runs only when an optional tenant Dispatcher/RBAC must be applied |
| Control change to mutable `status` / `dispatch_enabled` only | `validate` | No Bootstrap action |
| Push containing `[skip dispatch]` | `validate` | None |
| Manual platform/tenant run on a mapped branch | `validate -> trigger` | Reapplies caller scope to mapped environment |

## Jobs

| Job | Gate | Responsibility |
|---|---|---|
| `validate` | Every supported event | Structural YAML, control registry, optional naming policy, and OPA checks |
| `bootstrap` | Control caller, control branch, exact `tenants.yml` change, actionable lifecycle diff | Launches Bootstrap JT sequentially for actionable tenants |
| `fanout` | After Bootstrap writes platform desired state | Applies platform scope across all mapped environments; never tenant or `full` |
| `trigger` | Mapped platform/tenant push or manual run | Launches one scoped Dispatcher and polls to terminal |

### Fan-out failure recovery

If Bootstrap succeeds and `fanout` fails, **retry only the failed `fanout` job**.
A full pipeline rerun is not a reliable recovery path: after markers exist,
lifecycle diff yields no Bootstrap action for `corrected` / `activated`
tenants, so fan-out inputs are not regenerated. Do not add extra control-plane
state to paper over a full rerun.

## Tenant lifecycle diff

The three pipeline implementations use `scripts/pipeline/casc_runtime.py` for the
same behavior:

- Validate all tenant IDs, exact AAP Organization bindings, and repository ownership.
- Resolve the scalar combined tenant `repository` from the tenant record (`repo_name` override or default).
- Inspect markers across every mapped branch.
- Allow identity/topology corrections or removal before any marker exists.
- Reject identity/topology changes or removal after any marker exists.
- Do not rerun Bootstrap for `status` or `dispatch_enabled` changes alone.
- After Bootstrap writes platform desired state, automatically apply **platform
  scope only** across mapped environments. No tenant desired state is applied
  during onboarding; the first tenant apply requires a later tenant-repository
  commit.
- `team_name` is required for every tenant. Greenfield creates the Organization
  and Team; Brownfield requires exact existing references and does not modify them.
- An optional `dispatcher_job_template` selects a central tenant-bound JT.
  Omission keeps the shared Dispatcher.

## Optional naming policy

Control `config.yml` and `tenants.yml` are mandatory. Root
`naming-rules.yml` is optional:

- missing or empty: naming policy inactive;
- present: validate its schema, then enforce only listed resource types;
- no engine policy is copied as a fallback;
- `naming-rules.yml` itself is excluded from desired-state scanning.

Bootstrap validates the exact rendered Greenfield Organization and Team before
SCM mutation. Dispatcher and Drift have no naming-policy runtime dependency.

## Credentials

| Secret/variable | Use |
|---|---|
| `AAP_ENV_TARGETS_JSON` | Environment -> `{host, token}` execute-only Dispatcher access |
| `AAP_ENGINE_TOKEN` and engine host | Control-only Bootstrap JT launch |
| `ENGINE_REPO_TOKEN` | Private engine workflow/helper access |
| `CONTROL_REPO_TOKEN` | Pinned control metadata and marker reads |

Runtime deployment credentials are bearer-token only. GitHub checkout uses
`persist-credentials: false`, parsed tokens are masked, and pull/merge-request
validation does not receive AAP deployment credentials.

## Control revision

Validation resolves an explicit `control_revision` or control-branch HEAD. The
same revision is forwarded to Bootstrap, platform onboarding, the shared
Dispatcher, and Drift. Tenant-bound Dispatchers use fixed protected-branch
coordinates and have no revision launch input. Missing required control metadata
or a pin mismatch fails closed.

## Branch behavior

- `env_branch_map` values are unique and may use any valid branch names.
- Generated callers validate pull/merge requests to every target branch.
- Feature-branch pushes validate only because the branch does not map to an environment.
- Mapped-branch pushes dispatch only the caller's platform or tenant scope.
- Genesis and Bootstrap converge callers and required scaffold on every mapped branch.

## Dispatcher routing and concurrency

The shared Dispatcher remains the default. A tenant may instead name a central
tenant-bound Dispatcher in `tenants.yml`; Bootstrap scaffolds it from shared AAP
resource references in `config.yml`. Every JT requires
`allow_simultaneous=false`, every launch is polled to terminal, and timeout is a
failure. Different tenant-bound JTs may run independently. A configured
dedicated JT that is missing or mismatched fails closed without shared-JT
fallback. Shared `dispatch_scope=full` is refused while any dedicated JT exists.
These JTs separate execution queues, not the authorization scope of the shared
apply credential; tenant repositories therefore require trusted authors, branch
protection, and approvals.

## GitLab parity

GitLab uses `rules:changes` plus an internal `CI_COMMIT_BEFORE_SHA` exact diff
guard for `tenants.yml`. Dotenv artifacts carry actionable tenant IDs and
trigger suppression. Scope, lifecycle, optional naming, token, polling, and
protected onboarding behavior match GitHub.
