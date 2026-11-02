# Security

agent-recall copies your conversations with AI agents, your notes and your GitHub activity into one
file. A bug that leaks that file, a secret from it, or writes outside its own folders is a security
problem, not just a bug.

## Report

Please report privately via **Security → Report a vulnerability** on GitHub, not in a public issue:

- secrets (tokens, keys, passwords) that the masking misses and that end up in the database, an
  export or a handoff;
- anything that sends data off your computer (agent-recall only reads local files and, if you enable
  it, the GitHub API with your token);
- the MCP server exposing more than read-only access to the database;
- the installer or `brain integrate` damaging another tool's config (`.codex/config.toml`, Cline
  settings, shell profiles, crontab) beyond the agent-recall entries.

Include your OS, the agent-recall version (`brainctl status`) and steps to reproduce. Do not attach
your real `brain.db` — reproduce with a small test file instead.

## What to expect

A reply within a few days and a fix in a patch release. Users get it with `brainctl update`.

## What agent-recall does and does not protect

- Secret masking is pattern matching before anything is written. It reduces risk; it is not a
  guarantee. Keep the database on an encrypted disk and do not publish it.
- Data stays local unless you configure `after_sync` / `daily_snapshots_dir` to point at a synced or
  shared folder — then it is as safe as that folder.
- The MCP server is read-only and runs as your user over stdio; any agent you connect can read the
  whole archive. Connect only agents you trust with it.
- Handoffs go to your clipboard; you decide where to paste them.
