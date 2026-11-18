# Runner pool

Your own Linux servers as extra CI capacity, alongside the machines GitHub
provides for free. A Linux job runs on one of your servers when one has a
free slot, and on GitHub's machines when none does. Mac and Windows jobs
always run on GitHub.

## How it works

- **Slots.** Each server runs a few job slots at once (one per 2 CPU cores
  and 4 GB of memory, unless you set a number). A slot waits for one job,
  runs it in a brand-new container with its own Docker, then throws the
  container away and waits for the next. Nothing one job leaves behind
  reaches the next.
- **The switch.** GitHub cannot send a job to "whichever is free" by itself.
  A small service on each server checks every 15 seconds whether any of your
  slots is idle and sets the repository variable `OWN_RUNNERS_FREE` to
  `true` or `false`. Each Linux job in the workflows reads that variable as
  it is queued and goes to your servers on `true`, to GitHub otherwise.
- **What always stays on GitHub.** Pull requests from forks (anyone can
  open one, and their code would run on your server), workflows started by
  other people's issues and comments, and anything that publishes or signs
  a release. The list, with the reason for each, is in
  `codebase_rag/tests/test_workflow_runner_pool.py`.

A burst of jobs queued in the same second all see the same answer, so a
few of them can end up waiting for a slot on your servers while GitHub has
room. They start as soon as a slot frees up.

## Set up a server

You need an x86-64 (Intel or AMD) server running Ubuntu 22.04 or later (or
Debian 12), with root access. Arm servers are not supported yet. Docker is
installed for you if it is missing.

1. **Create a token** at
   [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):
   a fine-grained token, "Only select repositories" set to the repositories
   the pool serves, with two repository permissions:
   *Administration: Read and write* and *Variables: Read and write*.
   One token can serve every server.
2. **Copy this folder** to the server and run the installer once. It writes
   the settings file and stops:

   ```bash
   sudo ./install.sh
   ```

3. **Put the token** in `/etc/runner-pool/pool.env`, check the list of
   repositories, and run the installer again. The first run builds the job
   image, which takes 10 to 20 minutes:

   ```bash
   sudo nano /etc/runner-pool/pool.env
   sudo ./install.sh
   ```

The server's runners then appear under each repository's
*Settings → Actions → Runners*, and `OWN_RUNNERS_FREE` under
*Settings → Secrets and variables → Actions → Variables*.

## Day to day

| To | Do |
|---|---|
| Watch what a server is doing | `journalctl --follow --unit 'runner-pool-*'` |
| Change slots, repositories or the token | Edit `/etc/runner-pool/pool.env`, run `sudo ./install.sh` |
| Get the newest runner now (it also rebuilds weekly) | `sudo ./install.sh --rebuild` |
| Pause a server | `sudo systemctl stop runner-pool.target` (start it again the same way) |
| Remove a server | `sudo ./uninstall.sh` (add `--purge` to delete its settings too) |
| Send every job to GitHub, whatever the servers say | Stop the pool on every server; the last one to stop sets `OWN_RUNNERS_FREE` to `false` |

Re-running the installer restarts the slots, which cancels any job running
on that server at the time.

If every server goes down at once without being stopped (a power cut, say),
the variable can stay `true` and new Linux jobs then wait for your servers.
Set `OWN_RUNNERS_FREE` to `false` in the repository's settings to send them
to GitHub until the servers are back; the switch takes over again when they
return.

## Adding a repository

1. Add it to the token's repositories, and to `REPOS` in each server's
   `pool.env`, then re-run the installer.
2. In that repository's workflows, give each Linux job the same `runs-on`
   as the jobs here (see `.github/workflows/ci.yml`).

Your repositories belong to a personal account, so each runner serves one
repository and a server's slots are split between them. Moving the
repositories into a (free) GitHub organization would let every slot serve
every repository.

## Other providers

Nothing here is specific to Contabo: an Azure or other Ubuntu VM joins the
same pool by running the same installer.

## Files

| File | Purpose |
|---|---|
| `install.sh`, `uninstall.sh` | Add a server to the pool, or take it out |
| `pool.env.example` | The settings, with what each one means |
| `bin/runner-pool-slot` | Runs one slot: registers a single-use runner and starts its container |
| `bin/runner-pool-switch` | Keeps `OWN_RUNNERS_FREE` up to date |
| `bin/runner-pool-build` | Builds the job image around the newest runner release |
| `image/` | The job image: Ubuntu 24.04 with the tools GitHub's own machines have that the workflows use |
| `systemd/` | The services that keep all of the above running |
