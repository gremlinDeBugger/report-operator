# Deploy: laptop → Mac mini → VPS (same code, just a different host)

The only host-specific value is OPERATOR_MASTER_KEY (in the environment) and the
location of the data/ + clients/ folders. Nothing is hardcoded to a machine.

## Move to a new host
1. Copy the repo (scp -r report-operator user@host:~/).
2. Copy data/clients.json and clients/ (your encrypted store + workspaces).
3. Set OPERATOR_MASTER_KEY in the new host's environment (the SAME key, or the
   store can't be decrypted). Never put it in the repo.
4. pip install -r requirements.txt
5. Run the scheduler unattended:

### Mac mini (launchd)
Create ~/Library/LaunchAgents/com.gremlinhunter.operator.plist pointing at:
   python /path/to/report-operator/run_scheduler.py
Set the mac mini to never sleep (System Settings → Energy).

### VPS (systemd)
Create /etc/systemd/system/report-operator.service running the same command.
Harden first: SSH keys only, firewall on, no root login, auto security updates.

## Why a VPS / Mac mini and not a laptop
The scheduler is catch-up aware, so a sleepy laptop won't silently drop runs —
but an always-on host means runs fire on time without anyone present. That is the
whole point of moving off the laptop once you hold real client keys.
