# Shared workflows

Reusable GitHub Actions workflows for the repositories under this account.

## dependency-check

Produces a report of outdated dependencies (npm, pip, NuGet) with the release date of the latest version, and lists the open Dependabot security alerts. Updates are marked as ready only when the latest release is at least 30 days old, so freshly published versions have time to prove themselves. The workflow never opens pull requests.

Call it from a repository with:

```yaml
name: Dependency check
on:
  workflow_dispatch:
  schedule:
    - cron: "0 7 1 * *"
jobs:
  report:
    uses: ragePolpette/.github/.github/workflows/dependency-check.yml@main
    permissions:
      contents: read
      security-events: read
```

The report is attached to the run as the `dependency-report` artifact and shown in the job summary.
