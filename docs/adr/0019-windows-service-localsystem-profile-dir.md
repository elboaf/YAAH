# ADR 0019: Windows service runs as LocalSystem — profile recorded in the service's Parameters key

Date: 2026-10-20
Status: Accepted
Driven by: #459 (the wizard demanded the installing user's Windows account
password, granted the logon right programmatically, and broke whenever that
password changed).

## Context

The headless Windows server installed its service under the **installing
user's account**: the wizard prompted for that account's Windows password,
passed `--username/--password` to pywin32's `HandleCommandLine`
(`CreateService` stores the password), and granted `SeServiceLogonRight`
via `LsaAddAccountRights` because the programmatic path — unlike
Services.msc — does not (issue 65be612, event 7041). That bought one
thing: `Path.home()` inside the service resolved to the real profile, so
the passphrase config written at setup (`~/.yaah/config.json`) was found
and `workspace_root` was the user's actual home. It cost: a password
prompt in a wizard that otherwise needs no credentials, stored credentials
that rot on the next password change (1069 at next start), a whole
lsa-ctypes block to work around `ChangeServiceConfig` not granting the
logon right, and zero support for passwordless accounts (blank-password
security policy).

Running as **LocalSystem** — `CreateService`'s default when no username is
passed — eliminates all of that: no password, no stored credentials, no
logon right (it has it implicitly), immune to password changes. Its one
problem: LocalSystem's `Path.home()` is
`C:\Windows\System32\config\systemprofile`, so neither the passphrase
config nor the advertised `workspace_root` would resolve to the user's
home.

For the environment fix, the per-service registry values are a trap:
`AppEnvironment` is an srvany/NSSM convention the SCM ignores, and the
SCM's own undocumented `Environment` value is explicitly flagged as an
implementation detail (Microsoft Q&A, Sept 2026). Even a documented
mechanism would be a worse fit here: it would override `USERPROFILE` with
a REG_MULTI_SZ that does not expand `%`-references, while the passphrase
config is still read via `Path.home()`/`USERPROFILE` by our own code —
the indirection buys nothing over reading our own value.

## Decision

- The service is registered with **no** `--username/--password`:
  LocalSystem by default. The wizard's Windows-password prompt and the
  `_grant_logon_right` LSA block are deleted.
- The wizard records the installing user's **profile directory** in
  `HKLM\SYSTEM\CurrentControlSet\Services\YaahServer\Parameters\ProfileDir`
  (REG_SZ, literal path), where "installing user" is the account the
  elevated wizard process runs as.
- At service start, `_apply_service_profile_dir` reads that value **before
  any backend import** (backend.agent.config resolves its config path via
  `Path.home()` at module import) and sets `USERPROFILE`, `HOMEDRIVE`/
  `HOMEPATH` and `HOME` to that directory. It is best-effort: missing,
  unreadable, or no-longer-existing directories leave the SYSTEM defaults
  in place — the documented config-less state (server up, every remote
  request refused until setup is re-run), not a crash.

Consequently the passphrase gates **full command of the machine as
SYSTEM**, regardless of which account installed the service. This is
intended, and the trust-model line in SERVER-SETUP.md says so.

## Consequences

- Installing asks only passphrase + display name; UAC is the only
  credential (consent for admins, an over-the-shoulder admin credential
  for standard users — Windows reserves service installs for admins, so
  this one is not ours to remove).
- Changing a Windows password can no longer break an installed service,
  and passwordless accounts are supported.
- Setup must be run from the account whose workspace you want served; if
  the UAC popup is answered with a different admin's credentials, the
  recorded profile is that admin's (documented in SERVER-SETUP.md).
- Files created by remote exec under the workspace are owned by SYSTEM
  (administrable with standard tools; a known consequence of every
  SYSTEM-service design).
- There is no supported Windows primitive for injecting environment into
  a service process; if a future need exceeds this design (e.g. serving a
  *different* account's profile without re-running setup), the fallback
  is `YAAH_CONFIG_PATH` + a machine-global config — deliberately not
  built now.
