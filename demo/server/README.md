# The demos on a dedicated server

A single **OVH Eco SYS-1** (Intel Xeon-E 2136: 6 cores / 12 threads, 32 GB,
x86-64, a physical machine) runs the hosted demos behind Caddy, replacing the
Fly app described in [`../README.md`](../README.md).

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

- **Dedicated cores.** The failure is CPU starvation, so the fix is CPU that
  no neighbour or quota can take away. A physical server has none of either,
  and its CPU model is known before ordering, so the speed check below is a
  confirmation rather than a gamble. Shared-vCPU VPSes (OVH's own, Hetzner's
  CPX/CAX) are cheaper but leave exactly this question open, and OVH's VPS
  range has no dedicated-CPU tier to resize into if the answer is no.
- **Per-core speed is what counts.** A fit runs 4 NUTS chains, one per core,
  and fits within a tree are serialized behind its lock. The Xeon-E 2136
  turbos to 4.5 GHz; avoid the Eco models built on Xeon-D (KS-1, KS-2), whose
  ~2.6 GHz turbo would make every fit slower even with more cores. KS-5
  (Xeon-E3 1270v6, 4 cores) is the cheaper fallback if SYS-1 is out of stock.
- **Six cores and 32 GB** leave room for the three planned demos beside White
  Cube: two demos can fit at once without sharing four cores, and each holds
  its fits in memory.
- **Nothing here is OVH-specific.** The image is built on the server, any
  Ubuntu 24.04 host on amd64 or arm64 runs the same scripts, and the server
  holds no state worth backing up (a git checkout, `.env`, Caddy's
  certificates). Moving hosts is a re-run of the setup below.

## Files

| file | runs where | does |
|---|---|---|
| `bootstrap.sh` | server, as root (via sudo), once | Docker, `deploy` user, keys-only SSH, ufw, unattended upgrades, swap, repo checkout |
| `compose.yaml` | server | one container per demo, plus Caddy (the only published ports) |
| `Caddyfile` | server | TLS and reverse proxy, one site block per demo |
| `env.example` | server, copied to `.env` | hostname and `BREAKDOWN_API_TOKEN` |
| `deploy.sh` | server, piped over SSH | check out a commit, build, restart, prewarm |
| `../../.github/workflows/deploy-demo-server.yml` | GitHub Actions | runs `deploy.sh` (manual trigger until cutover) |

## Setup: the steps only a person can do

1. **Order the server.** [OVH Eco](https://eco.us.ovhcloud.com/) → **SYS-1**,
   US East (Vint Hill, next to Fly's `iad`; Beauharnois, near Montreal, if US
   East is out of stock). Monthly billing; the installation fee equals one
   month. Once delivered, install the **Ubuntu 24.04** template from the OVH
   control panel with your SSH key, keeping the default software RAID 1 across
   the two disks. Note the IPv4 address and the login user in the delivery
   email (`ubuntu` on OVH's template).

2. **Firewall.** Nothing to do here: `bootstrap.sh` enables `ufw` (inbound TCP
   22, TCP 80, TCP 443, UDP 443; deny everything else). Docker-published ports
   bypass `ufw`, which is harmless here only because `compose.yaml` publishes
   nothing but Caddy's 80/443; keep it that way. Leave OVH's Edge Network
   Firewall off: it is stateless, and a rule set that forgets return traffic
   drops the server's own DNS replies.

3. **Make the CI deploy key** on your laptop and bootstrap the server:

   ```bash
   ssh-keygen -t ed25519 -N '' -C breakdown-demo-ci -f ~/.ssh/breakdown-demo-ci
   ssh ubuntu@<ip> 'sudo bash -s' -- "$(cat ~/.ssh/breakdown-demo-ci.pub)" < demo/server/bootstrap.sh
   ```

   It refuses to finish if sshd does not end up keys-only, so a failure here
   names the config file to look at rather than leaving passwords on.

4. **Point DNS.** An `A` record for `white-cube-demo.polycultureresearch.com`
   → the server's IPv4. This name is new (it was never wired to Fly), so
   pointing it here disturbs nothing. Caddy needs it resolving before first
   start, or certificate issuance fails.

5. **Write `.env` on the server:**

   ```bash
   ssh deploy@<ip>
   cd /srv/breakdown/demo/server && cp env.example .env && chmod 600 .env && nano .env
   ```

   Reuse the Fly instance's `BREAKDOWN_API_TOKEN` if you still have it. Fly
   secrets cannot be read back, and a new token breaks any client's
   `claude mcp add` config.

6. **Add the GitHub settings**, all in the `demo` environment:

   ```bash
   gh secret set DEMO_SSH_KEY --env demo < ~/.ssh/breakdown-demo-ci
   ssh-keyscan -t ed25519 <ip> | gh secret set DEMO_KNOWN_HOSTS --env demo
   gh variable set DEMO_HOST --env demo --body <ip>
   ```

   Compare the scanned key against the one the server prints
   (`ssh ubuntu@<ip> cat /etc/ssh/ssh_host_ed25519_key.pub`) before trusting it.

7. **First deploy:** Actions → *Deploy demo (server)* → Run workflow. Delete
   `~/.ssh/breakdown-demo-ci` from your laptop afterwards; GitHub has the
   copy it needs.

## Is the box good enough? Two checks

**Speed.** The workflow log prints each tour story's elapsed time:

```
  ok   B  professional churn: net_new_mrr -> top cause churned_mrr  (NNNs)
```

A few minutes per story is healthy; on these cores expect one or two. Tens of
minutes on a dedicated machine is not contention, so look for something else
first: `docker stats` for memory pressure and swapping, `top` for a stray
process. A dedicated server cannot be resized, so if the CPU itself is the
problem the remedy is ordering a different model and re-running the setup.

**Same numbers here.** A seeded sampler can land differently on a different
numeric stack: the demo's collinear pair splits story D's gap differently on
macOS-arm64 than on Linux, from the same seed. This box is x86 Linux like CI,
but a different CPU can still take a different BLAS code path. The demo test
suite pins every tour number the pitch reads out, so run it on the box itself:

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
2. `deploy-demo-server.yml`: add `deploy-demo.yml`'s `push` trigger and path
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
