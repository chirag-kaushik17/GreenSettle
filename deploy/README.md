# Running the server on a cloud VM (documentation only)

Nothing in this repository creates cloud resources. This page describes how
you *could* run the server on a VM while the worker stays on the laptop next
to LibreHardwareMonitor. Because the worker **pulls** work, the laptop never
needs an open port.

```
laptop (behind NAT)                         cloud VM
  worker ──HTTPS or SSH tunnel──────────►  reverse proxy :443 ──► server 127.0.0.1:8000
  LibreHardwareMonitor (local only)
```

## 1. Tokens

Generate two different tokens on a trusted machine:

```
python -c "import secrets; print(secrets.token_hex(32))"
```

- On the VM, set `GREENSETTLE_AGENT_TOKEN` and `GREENSETTLE_ADMIN_TOKEN`
  in the service's environment (not in the repo, not on the command line,
  which other users can see in the process list).
- On the laptop, set the same `GREENSETTLE_AGENT_TOKEN` for the worker.
- Users get their API keys from `POST /admin/users` (shown once).

## 2. Keep the server on loopback

Run it bound to 127.0.0.1 (the default):

```
python -m greensettle.server --db /var/lib/greensettle/greensettle.db --time-scale 600
```

The API speaks plain HTTP. Never pass `--host 0.0.0.0` without one of the
two options below in front of it.

## 3a. Option A: HTTPS reverse proxy

Put a reverse proxy (for example Caddy or nginx with a certificate from
Let's Encrypt) on port 443 and forward to `127.0.0.1:8000`. Open only port
443 in the VM's firewall/security group. The worker then uses
`--server https://your-domain`.

## 3b. Option B: SSH tunnel (no public HTTP at all)

Open only SSH (22) on the VM. On the laptop:

```
ssh -N -L 8000:127.0.0.1:8000 user@vm-address
```

Then run the worker and client against `http://127.0.0.1:8000`; the traffic
is encrypted by SSH.

## 4. Worker on the laptop

```
$env:GREENSETTLE_AGENT_TOKEN = "<agent token>"
python -m greensettle.cli calibrate-idle --seconds 30
python -m greensettle.agent.worker --server https://your-domain --agent-id laptop
```

## 5. Things to remember

- The server trusts the worker's measurements (see docs/phase3_explained.md).
- Back up the SQLite file; the ledger is the record of all credits.
- Rotating a token means restarting the server and the worker with the new value.
