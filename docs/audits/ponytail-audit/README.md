
## Post-audit banner: isolation machinery removed

The per-chat worktree, landing, residue, sweeper, and remote-twin
machinery referenced throughout this audit was removed for good by
ADR-0017 (#362). Audit items naming that machinery (dedupe/shrink/YAGNI
entries against worktrees.py, wt_sweep.py, wt_remote.py, runwatch.py,
landing.py, and the status-strip worktree hash) no longer apply — the
modules and their vocabulary are gone.
