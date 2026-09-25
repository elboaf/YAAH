# Remote devices, workspaces, and chats

## Status

Design accepted; implementation is phased. The first backend slice adds a registry of multiple remote sessions and routes namespaced workspace-tool calls to the owning host. Sidebar aggregation, remote-owned conversation caching/sync, and per-chat edit leases are subsequent phases.

## Product behavior

- Adding a remote device makes it a persistent device group in the local sidebar; it does not switch the whole application away from local workspaces.
- Local workspaces remain interactive while any number of remote devices are connected or saved.
- A device group contains that device's workspaces and its existing conversations. Multiple devices can appear together.
- A conversation has an explicit owner independent of its workspace:
  - Local-owned conversations remain in the local database, including existing conversations created while the old global remote mode was active.
  - Remote-owned conversations originate on their device. The local client caches their history and keeps it synchronized with that device.
- The local YAAH agent loop and configured local model/provider execute turns for both local- and remote-owned chats. Workspace-touching tools (shell, file, Git) route to the device owning the selected workspace.
- Remote chat history is readable from the local cache while its device is offline. Such chats are read-only until reconnection.
- An edit or turn on a remote-owned chat acquires a lock for that chat only. Reading does not lock. A turn/edit must release its lock when done; an expiring lease allows recovery when a client crashes or loses connectivity.
- If the device goes offline during a turn, stop remote-dependent work, retain unsynced partial output locally as pending sync, and keep the chat read-only until it reconnects and synchronization succeeds.
- Multiple chats can be active at once across local and remote devices. Switching the selected chat never redirects, pauses, or cancels another chat's stream. Locking, busy state, queued work, errors, and pending sync are per conversation; one device's chat must not block another's.

## Core model and invariants

1. **Workspace identity:** a local workspace is a local path; a remote workspace remains namespaced as `remote:<host-id>:<host-path>`. Host IDs are stable and path strings are never interpreted using the local OS.
2. **Conversation identity:** a conversation is identified by `(owner device ID, conversation ID)`, not its integer ID alone. Local and different remote databases can reuse numeric IDs.
3. **Workspace is not ownership:** a local-owned chat may use a remote workspace. Conversely, a remote-owned chat belongs to a device even if workspace metadata changes. Existing local chats are never silently reclassified or migrated.
4. **Credential boundary:** connection secrets stay in the local backend/secure local storage and are never sent to browser code. A saved/connected host is trusted for its authenticated remote operations; remote capabilities and lock requests must be explicit.
5. **No global remote mode:** all APIs that list, select, or operate on workspaces/conversations must take or derive an owner. Local operations remain local regardless of registered remote devices.
6. **Per-chat exclusivity:** an edit lease is keyed by owner and conversation ID, and must not serialize independent chats, even on the same device. Server-side turns retain their existing per-conversation run guard.
7. **Cache reconciliation:** remote history is cached locally with a remote revision/cursor and explicit pending-sync state. Sync is idempotent. Since concurrent writers are excluded by lease, the lease holder's committed messages/metadata flow back to the remote owner; stale clients refresh before editing.
8. **Offline safety:** cached history is readable but non-editable if the owner is unreachable or lease/sync state is uncertain. Pending local changes are preserved, never silently discarded.

## Target interaction

- Sidebar hierarchy: This device → local workspace groups → local-owned chats; each saved remote device → its remote workspace groups → remote-owned chats.
- Selecting a device/workspace is navigation, not a global execution-target toggle. Selecting a chat determines conversation ownership; its workspace determines where workspace tools execute.

**Verification notes:** Device groups and management flows exist and selection does not switch global execution scope. Follow-up gaps found in audit: remote workspace groups show only the three newest chats with no expansion path; chats associated with a removed device profile lose their sidebar group; no focused frontend tests cover device hierarchy/status/management/local-chat visibility. Clarify whether a distinct “connecting” status is required and whether refresh should be per-device.
- Add-device flow: discover or enter URL, verify protocol and passphrase, save device profile, fetch workspace list and remote conversation metadata/history, then expose the device group. Add/remove/refresh actions are on the device group.
- A device status indicator reports online, offline/cached, connecting, or authentication/error state. Cached remote chat rows remain available offline and communicate read-only/pending-sync status.
- A selected remote chat uses the same composer and local provider controls. Starting a turn first refreshes its revision and acquires the per-chat edit lease. Losing the lease or host aborts remote-dependent execution and marks any local-only turn output pending sync.
- Workspace tool dispatch uses the selected workspace's owner, not whichever device was most recently connected. Local workspace calls always run locally.

## Backend/API architecture

### Multi-device registry and workspace routing

- Replace the singleton-only remote connection assumption with a backend registry keyed by stable host ID. Preserve a compatibility active-session accessor only while old UI paths are being migrated.
- Connect/register, remove/disconnect, status, and list operations address one host ID. Keep discovery separate from saved devices.
- Aggregate local workspace rows and each reachable host's rows, applying the existing namespace. An unreachable host contributes its cached frontend rows rather than making local listing fail.
- Route remote workspace endpoints, file operations, attachments, and `REMOTE_TOOLS` by the host ID in the workspace namespace. Unknown host IDs fail closed; they must never fall through to local execution. Local paths always use local executors.
- Workspace schemas/environment hints must be derived from the turn's target workspace, not a process-wide active host. Preserve host OS-specific tool schemas for the owning device.

### Remote conversation cache, sync, and leases

- Add owner-aware conversation transport without confusing client-owned chats stored under remote paths with host-owned chats.
- Define a stable remote conversation identity and local cache tables/metadata: owner ID, host conversation ID, remote revision/cursor, sync status, and lease state.
- Extend the authenticated host protocol for paginated conversation metadata/history fetch, revision-aware sync/commit, and per-chat edit lease acquire/renew/release. Locks expire after a bounded lease and are enforced by the host on writes/turn-related commits.
- Keep sync idempotent; preserve message ordering and IDs where possible. Metadata conflict policy is latest committed edit under the exclusive lease. Do not overwrite unseen remote revisions: refresh before acquiring/editing and reject stale commit tokens.
- Route images and attachments by conversation owner, with cache-aware retrieval. Remote image URLs must be host-scoped in the local UI without exposing credentials.
- Treat scheduled-agent records and their tapes as a separate follow-up unless included in the explicit scope of remote chats; ordinary conversations are the first sync target.

### Frontend state and concurrency

- Use composite conversation keys (`local:<id>` or `remote:<host-id>:<id>`) consistently for selection, message buffers, streams, status, abort controllers, pending questions, locks, and cache records.
- Maintain independent streams for different conversation keys. Existing backend per-conversation run guards remain; remove scalar UI send state that can incorrectly mark all chats idle when one stream finishes.
- Attribute global activity/log entries to their conversation or device so concurrent streams do not mix their traces.
- A workspace path selects the tool execution owner per request/turn. A local chat pointed at a remote workspace remains locally owned while workspace tools route remotely; a remote-owned chat still runs the model locally.

## Delivery phases

### Phase 1 — Multi-host session/routing foundation (complete)

- Add a registry of multiple backend remote sessions while retaining the old singleton accessor for compatibility.
- Route namespaced workspace-tool calls by host ID and fail closed for a missing owner.
- Add tests proving two registered hosts route independently and local workspace tool execution is unaffected by merely registering hosts.

### Phase 2 — Per-device connection API and workspace aggregation

- [x] Add APIs to save/connect/remove/query multiple devices without replacing a global active connection.
- [x] Aggregate local and reachable remote workspace rows; route remote file/attachment/workspace CRUD operations by explicit owner.
- [x] Cover online/offline/error responses and prevent one host's failure from hiding other workspaces.

**Verification notes (2025-06-16):** Core device APIs, aggregation, and owner-based routing are present. Follow-up gaps found in audit: add a test proving one connected host's failure does not hide another host's workspaces; directly test remote workspace deletion and file preview/delete routing; confirm whether workspace update semantics are required for CRUD. Per-host malformed-response isolation also needs consideration.

### Phase 3 — Sidebar device hierarchy and persistent profiles

- [x] Add device-parent groups with connection status and device-specific workspace/local-chat grouping.
- [x] Keep local workspace rows and chats fully usable at all times; preserve existing local-owned chats.
- [x] Add device management (add/discover, reconnect, refresh, remove) outside the composer; selecting a device must not toggle global execution scope.

### Phase 4 — Remote-owned conversation cache and read path (in progress)

- [ ] Add composite owner identity throughout frontend state and read APIs.
- [x] Cache remote metadata/messages locally; load from cache offline and display read-only status.
- [ ] Scope image/attachment retrieval and conversation actions by owner. Test ID collisions between local and multiple remote databases.

**Backend read/cache slice:** Added authenticated host metadata/history endpoints, host-keyed cache tables, and per-device read routes that refresh while connected and serve cached transcripts on loss of connectivity. Backend DB/cache and remote API tests pass. Frontend composite identity/read-only rendering and owner-scoped media remain outstanding.

**Implementation notes:** Phase 4 is deliberately read-only. Host-owned chats must remain distinct from local-owned chats that happen to use remote workspaces. Use composite identity `(owner device ID, conversation ID)`; do not reuse local integer-keyed conversation tables. Defer leases and bidirectional edits/sync to Phase 5, and remote-workspace turn execution to Phase 6.

### Phase 5 — Per-chat leases and bidirectional sync

- Implement host-enforced, expiring edit leases keyed by conversation; only edits/turns acquire them.
- Sync remote history and metadata idempotently; refresh before edit; surface lock held elsewhere and stale revision errors.
- Preserve partial turn output as pending sync after network loss. Retry sync on reconnection; do not silently discard or run an unsynchronized second editor.

### Phase 6 — Local agent turns with per-workspace remote tool dispatch

- Keep the model/provider and agent loop local for all conversations.
- Bind each turn to explicit conversation owner and workspace owner. Route workspace tools to that workspace's host; leave local tools and local workspaces local.
- Preserve streaming and allow simultaneous independent turns in local and remote chats across devices. Ensure switching chats affects only selection.

### Phase 7 — Migration, hardening, and end-to-end verification

- Keep old connected-mode chats local-owned and ensure their existing workspace namespace continues to route correctly.
- Add migration compatibility, retry/backoff, auth expiry, partial failures, host removal with cached data, and offline/lease-loss coverage.
- Verify multi-device UI plus simultaneous local/remote turns, cancellation per chat, same-chat exclusion, pending sync recovery, and no local workspace regression.

## First-slice acceptance criteria

- Two remote sessions with distinct stable host IDs coexist in the backend registry.
- A workspace tool call for `remote:<host-a>:...` is sent only to host A; the equivalent host B call is sent only to host B.
- A remote namespace with no registered host returns an explicit error and never invokes a local executor.
- Merely registering remote sessions does not change execution for an ordinary local workspace.
- The current connect/status and tests remain usable during this phase; there is no UI global-mode removal until the explicit per-device UI/API phases land.

## Risks and open implementation notes

- Host authentication currently grants broad API access, including agent/tool capabilities; the new endpoints must not widen access accidentally and must remain behind the passphrase guard.
- The current proxy buffers ordinary HTTP responses. Streaming remote tool progress and future synchronization must be designed explicitly; remote agent turns are local streams in the target model.
- A host lock is only safe if every client that edits/syncs remote history honors server-side lease tokens. Expiry, renewals, lost acknowledgements, and restart recovery need tests.
- Cached offline data needs an explicit deletion/removal policy so removing a device does not unexpectedly erase recoverable pending changes.
- Global agent defaults/access settings are local configuration; per-chat model selection and host-owned files should not leak provider secrets to remote devices.
