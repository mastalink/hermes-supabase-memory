# Turtle Deploy Runbook

Materials for installing Hermes Agent v0.12 on Joseph's TurtleNet fleet hosts
with the `pgvector_memory` plugin and the per-turtle soul wired up.

## Files

- `bootstrap-turtle.sh` — single script that, given a turtle name, drives the
  whole sequence: install Hermes, copy plugin, install SOUL.md, write config,
  set provider, run `hermes doctor`. Idempotent (safe to re-run).
- `cli-config.template.yaml` — Hermes config template. Wired for
  `memory.provider: pgvector_memory` and a configurable inference provider.
- `souls/` — soul files copied from `moses3/scripts/turtle-souls/`. The script
  picks the right one by turtle name and installs it as
  `$HERMES_HOME/SOUL.md`. Hermes' prompt builder loads `SOUL.md` automatically.

## How Hermes uses the soul

`agent/prompt_builder.py:983` reads `$HERMES_HOME/SOUL.md` on every prompt
build and injects it into the system prompt — so identity is just a file
copy, no code changes.

The plugin's `SUPABASE_AGENT_IDENTITY` env var scopes Supabase memory per
turtle, so Raph's memories don't bleed into Leo's even though they share the
same Supabase project.

## Per-turtle decisions

| Turtle | Host | Inference (default) | Soul |
|---|---|---|---|
| Raphael | mastalink@192.168.40.70 (Mac mini) | NVIDIA NIM (`provider: nvidia`) | `raphael.md` |
| Donatello | (offline, .71) | TBD when online | `donatello.md` |
| Michelangelo | mastalink@192.168.40.172 (TurtleNet) | TBD | `michelangelo.md` |
| Leonardo | local Leo (RTX 5090) | local (gemma4 / nemotron / llama-server) | `leonardo.md` |

Moses (Joseph's alter ego) is **not a turtle** — separate deployment on
whatever surface Joseph carries day-to-day. Same `pgvector_memory` plugin,
different `SUPABASE_AGENT_IDENTITY=moses`.

## Default inference choice

NVIDIA NIM is the default for the turtles because:
- Joseph already has `NVIDIA_API_KEY` configured
- Cloud-hosted (no per-turtle GPU pressure)
- Nemotron-3-Super-120B-A12B is set as the MOSES NIM model — keep continuity

To switch to OpenAI / GPT-5: pass `--provider openai-codex` to the bootstrap
script, or edit `cli-config.template.yaml` before running.

## Run it

```bash
# From the moses3 repo (we need .env for SUPABASE_URL/KEY/etc)
cd /c/AI/moses/moses3/moses

# Source env, then deploy Raph:
set -a; source .env; set +a
bash /c/AI/hermes-supabase-memory/deploy/bootstrap-turtle.sh raphael mastalink@192.168.40.70

# Validate
ssh mastalink@192.168.40.70 'hermes doctor'
ssh mastalink@192.168.40.70 'hermes -z "Brother, what are you?"'
```

The script does NOT delete or stop Raph's existing services (Ollama, Arduino
agent on port 5006). Hermes runs alongside them.

## Rollback

If anything goes wrong:

```bash
ssh mastalink@192.168.40.70 'rm -rf ~/.hermes ~/.local/bin/hermes /usr/local/bin/hermes'
# Existing Ollama + Arduino are untouched
```
