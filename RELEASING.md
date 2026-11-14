# Releasing

Releases go to PyPI from GitHub Actions through
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/). There is no
PyPI API token to create, store or leak: PyPI accepts uploads only from the
`release.yml` workflow in this repository, running in the named GitHub
environment. Every upload also carries a signed
[attestation](https://docs.pypi.org/attestations/) linking the files to the
commit that built them.

## One-time setup

Do these once, in this order.

### 1. PyPI and TestPyPI accounts

You need an account on [pypi.org](https://pypi.org/account/register/) and a
separate one on [test.pypi.org](https://test.pypi.org/account/register/).
On both:

- Turn on two-factor authentication (**Account settings > Two factor
  authentication**) with an authenticator app or a security key, and save
  the recovery codes somewhere offline. PyPI requires 2FA to publish.
- Do **not** create an API token for this project. Trusted Publishing
  replaces it. Delete any old tokens you no longer use.

### 2. Register the pending publishers

The project does not exist on PyPI yet, so you add a *pending* publisher.
The first successful upload creates the project and turns the pending
publisher into a normal one.

Open [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)
and fill in the **GitHub** form:

| Field | Value |
| --- | --- |
| PyPI Project Name | `rlm-ig` |
| Owner | `ishaangupta-yb` |
| Repository name | `rlm-ig` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Do the same at
[test.pypi.org/manage/account/publishing](https://test.pypi.org/manage/account/publishing/)
with environment name `testpypi`.

Every value must match exactly. A typo means the upload is refused, not
that it goes somewhere else.

**A pending publisher does not reserve the name.** If someone else
registers `rlm-ig` before your first upload, the pending publisher stops
working. Do the first release soon after this step.

### 3. GitHub environments

In the repository: **Settings > Environments > New environment**

- `pypi`: add yourself under **Required reviewers**, so every real release
  waits for your click even if someone pushes a tag. Under **Deployment
  branches and tags**, choose *Selected branches and tags* and add the tag
  rule `v*`.
- `testpypi`: no reviewers needed.

### 4. Protect `main` and release tags

- **Settings > General > Default branch**: `main`.
- **Settings > Rules > Rulesets**: protect `main` (block force pushes and
  deletion, require the CI checks to pass), and add a tag ruleset for `v*`
  that restricts who can create, update or delete release tags.

## Cutting a release

1. Bump `__version__` in `src/rlm_ig/__init__.py` (this is the only place
   the version lives; `pyproject.toml` reads it).
2. Move the `Unreleased` entries in `CHANGELOG.md` under the new version.
3. Commit to `main` and wait for CI to pass.
4. Optional dry run: **Actions > Release > Run workflow** on `main`. This
   publishes to TestPyPI. Check it with
   ```bash
   pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ rlm-ig
   ```
   TestPyPI never accepts the same version twice, so a second dry run of
   one version fails at the upload step; that is expected.
5. Tag and push:
   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   ```
   ```bash
   git push origin v0.1.0
   ```
6. Approve the `pypi` deployment in the Actions run. The workflow runs the
   full test suite, refuses to continue if the tag does not equal
   `v` + `__version__`, builds, checks and uploads.
7. Optionally create a GitHub release from the tag with the changelog
   entry.

PyPI versions are permanent. A broken release cannot be overwritten, only
yanked and replaced by a new version number.
