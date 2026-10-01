# The demos on Hetzner

A single Hetzner **CAX21** (4 Ampere vCPUs, 8 GB, arm64) runs the hosted
demos behind Caddy, replacing the Fly app described in [`../README.md`](../README.md).

**Status:** set up alongside Fly, not yet the public instance. The Fly app keeps
serving `white-cube-demo.fly.dev` until the cutover steps at the end are done.

## Why this box

Fly's `shared-cpu-2x` runs under a CPU quota: a small guaranteed share of each
core plus a burst allowance. A cold prewarm is a long run of NUTS fits, which
uses up the allowance and leaves the machine throttled. The expected cause of
every failed "Deploy demo" run since 2026-08-24 is story B passing
`prewarm.py`'s 45-minute limit. On a laptop the same story took 42s. The
throttling explanation fits that gap but was not measured on the Fly machine.

Why this particular box:

- **4 cores is the useful maximum for one demo.** A fit runs 4 NUTS chains,
  one per core, and fits within a tree are serialized behind its lock. More
  cores help only when several demos fit at once.
- **ARM works.** Every compiled dependency in `uv.lock` ships a
  `linux-aarch64` wheel for Python 3.14, and the base image is multi-arch.
  The image is built on the server, so moving to an x86 CPX later changes no
  file here.
- **Shared vCPU, not dedicated.** Hetzner's shared vCPUs contend with
  neighbours but, unlike Fly's, are not held to a quota. The first-deploy
  measurement below tests that assumption. If it fails, resize to a CCX.

## Files

| file | runs where | does |
|---|---|---|
| `bootstrap.sh` | server, as root, once | Docker, `deploy` user, keys-only SSH, unattended upgrades, swap, repo checkout |
| `compose.yaml` | server | one container per demo, plus Caddy (the only published ports) |
| `Caddyfile` | server | TLS and reverse proxy, one site block per demo |
| `env.example` | server, copied to `.env` | hostname and `BREAKDOWN_API_TOKEN` |
| `deploy.sh` | server, piped over SSH | check out a commit, build, restart, prewarm |
| `../../.github/workflows/deploy-demo-hetzner.yml` | GitHub Actions | runs `deploy.sh` (manual trigger until cutover) |

## Setup: the steps only a person can do

1. **Create the server.** Hetzner Cloud console → new server: location
   **Ashburn** (next to Fly's `iad`; if CAX is not offered there, pick
   Falkenstein/Nuremberg/Helsinki, where it is), image **Ubuntu 24.04**, type
   **CAX21**, your SSH key. Note the IPv4 address.

2. **Attach a Hetzner Cloud Firewall.** Allow inbound TCP 22, TCP 80, TCP 443,
   UDP 443; deny everything else. Use this rather than `ufw`, because
   Docker-published ports bypass `ufw`.

3. **Make the CI deploy key** on your laptop and bootstrap the server:

   ```bash
   ssh-keygen -t ed25519 -N '' -C breakdown-demo-ci -f ~/.ssh/breakdown-demo-ci
   ssh root@<ip> 'bash -s' -- "$(cat ~/.ssh/breakdown-demo-ci.pub)" < demo/hetzner/bootstrap.sh
   ```

4. **Point DNS.** An `A` record for `white-cube-demo.polycultureresearch.com`
   → the server's IPv4. This name is new (it was never wired to Fly), so
   pointing it here disturbs nothing. Caddy needs it resolving before first
   start, or certificate issuance fails.

5. **Write `.env` on the server:**

   ```bash
   ssh deploy@<ip>
   cd /srv/breakdown/demo/hetzner && cp env.example .env && chmod 600 .env && nano .env
   ```

   Reuse the Fly instance's `BREAKDOWN_API_TOKEN` if you still have it. Fly
   secrets cannot be read back, and a new token breaks any client's
   `claude mcp add` config.

6. **Add the GitHub settings**, all in the `demo` environment:

   ```bash
   gh secret set HETZNER_SSH_KEY --env demo < ~/.ssh/breakdown-demo-ci
   ssh-keyscan -t ed25519 <ip> | gh secret set HETZNER_KNOWN_HOSTS --env demo
   gh variable set HETZNER_HOST --env demo --body <ip>
   ```

   Compare the scanned key against the one the server prints
   (`ssh root@<ip> cat /etc/ssh/ssh_host_ed25519_key.pub`) before trusting it.

7. **First deploy:** Actions → *Deploy demo (Hetzner)* → Run workflow. Delete
   `~/.ssh/breakdown-demo-ci` from your laptop afterwards; GitHub has the
   copy it needs.

## Is the box good enough? Two checks

**Speed.** The workflow log prints each tour story's elapsed time:

```
  ok   B  professional churn: net_new_mrr -> top cause churned_mrr  (NNNs)
```

A few minutes per story is healthy. Tens of minutes means CPU contention.
Resize to a CCX in the Hetzner console with **"CPU and RAM only"** so the disk
does not grow, which keeps a later downgrade possible.

**Same numbers on ARM.** CI only runs x86, and a seeded sampler can land
differently on another architecture. The demo test suite pins every tour
number the pitch reads out, so run it on the box itself:

```bash
ssh deploy@<ip>
docker run --rm -v /srv/breakdown:/src:ro -w /src -e UV_PROJECT_ENVIRONMENT=/venv \
  ghcr.io/astral-sh/uv:python3.14-bookworm \
  uv run --frozen pytest -p no:cacheprovider -rs tests/test_white_cube_demo.py
```

Every test should **pass**, not skip; `-rs` prints the reason for any skip.
A failure here means the tour script no longer matches what this box computes.
Stop and investigate before cutover.

## Cutover

After both checks pass and `https://white-cube-demo.polycultureresearch.com/ui`
serves the tour:

1. `python demo/check_demos.py` against the new URL.
2. `deploy-demo-hetzner.yml`: add `deploy-demo.yml`'s `push` trigger and path
   filter. Delete `deploy-demo.yml`.
3. Replace the `fly.dev` URL with the custom domain in `demos.yaml` (`url`;
   drop `fly_app`), `pyproject.toml` (`[project.urls] Demo`, which becomes the
   PyPI sidebar link from the next release), and `docs/mcp.md` (two links).
4. `demo/README.md` "Deploying": replace the Fly instructions with a pointer
   here. Delete `fly.toml`.
5. **The `fly.dev` URL.** It appears in the v0.1.0 release notes and the PyPI
   sidebar of every release so far, so deleting the Fly app breaks those
   links. Decide whether to keep a minimal Fly app that redirects to the new
   domain, or accept the break. GitHub release notes can be edited, but PyPI
   metadata cannot change for releases already published.
6. Then `fly apps destroy white-cube-demo`.

## Operating it

- **A reboot or container restart empties the trace cache.** Unattended
  upgrades install security fixes but do not reboot. After a manual reboot,
  re-run the workflow, or run
  `docker compose exec -T white-cube /app/.venv/bin/python /demo/prewarm.py --rcas`
  in this directory.
- **Adding a demo:** a service in `compose.yaml` (copy `white-cube`, change
  the image name and env), a site block in `Caddyfile`, a hostname in `.env`,
  an `A` record, and a prewarm line in `deploy.sh`. Watch `docker stats` once
  there are two: each demo holds its fits in memory.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*
