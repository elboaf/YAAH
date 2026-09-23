# Testing Enhancements Research Report

> Branch: `testing-enhancements` | Date: 2026-01-01

---

## Table of Contents

1. [YAAH Current Architecture](#1-yaah-current-architecture)
2. [Sandbox Testing Methods for AI-Generated Code](#2-sandbox-testing-methods)
3. [Sub-Agent Spawning: When to Spawn vs. Single Loop](#3-sub-agent-spawning)
4. [From Single-Loop to True Sub-Agent Architecture](#4-transition-plan)
5. [ZCode Feature Parity Analysis](#5-zcode-feature-parity)
6. [Frontier Agent Harness Comparison](#6-frontier-harness-comparison)
7. [Recommended V1 Plan](#7-recommended-v1-plan)

---

## 1. YAAH Current Architecture

### 1.1 The Agent Loop (`backend/agent/loop.py`)

YAAH runs a **single-threaded, synchronous agent loop** per conversation:

```
for each step:
    model.chat(messages, tools)  → streaming response
    if tool_calls:
        for each tool_call (sequential):
            execute_tool(name, args)  → result
            append tool result to messages
    else:
        yield done
```

Key characteristics:
- **One agent, one context, one conversation** — all tool calls happen in a single loop iteration
- **Sequential tool execution** — even when the model emits multiple tool calls, they execute one after another
- **Skills as prompt injection** — `load_skill` appends skill bodies to the system prompt mid-turn; no isolation
- **`ask_user` blocks the loop** — the loop waits on an `asyncio.Future` until the user answers
- **Step budget** — configurable max steps (default 200), with cancellation support
- **No sub-agent primitive** — the model has no way to delegate work to an isolated agent instance

### 1.2 Tool Registry (`backend/agent/tools.py`)

Current tools: `bash`, `powershell`, `web_search`, `web_fetch`, `view_image`, `ask_user`, `read_file`, `write_file`, `edit_file`, `create_file`, `delete_file`, `move_file`, `search_files`, `git_status`, `git_diff`, `git_add`, `git_commit`, `git_push`, `git_pull`, `load_skill`, `ask_user`.

Windows-specific safety: Job Objects for process group termination, path escape validation.

### 1.3 Skills System (`backend/agent/skills.py`)

Skills are markdown folders under `~/.yaah/skills/` with YAML frontmatter. Two invocation paths:
- User invokes with `/s <name>` — body injected into system prompt for that turn
- Model calls `load_skill` — same injection, mid-turn

Skills are **not isolated contexts** — they're just prompt text appended to the system message.

### 1.4 Model Client (`backend/agent/model_client.py`)

OpenAI-compatible chat completions. Single provider per session. Streaming SSE with content deltas, tool call deltas, and finish reason.

---

## 2. Sandbox Testing Methods for AI-Generated Code

### 2.1 The Threat Model

AI agents executing on a user's machine face four categories of risk:

| Risk | Description | Mitigation |
|------|-------------|------------|
| **Filesystem damage** | Agent deletes/overwrites files outside project | Mount project read-only, restrict to workspace root |
| **Network exfiltration** | Agent sends secrets to external servers | Disable outbound networking, allowlist egress |
| **Privilege escalation** | Container escape via kernel exploit | MicroVMs or gVisor |
| **Resource exhaustion** | Infinite loops, disk fill, memory leak | cgroup limits, timeouts, watchdog |

### 2.2 Sandbox Technology Spectrum

| Technology | Isolation | Startup | Overhead | Best For |
|------------|-----------|---------|----------|----------|
| **Docker** | Namespace + cgroup | <1s | <2% CPU | Default choice, 90% of needs |
| **Podman** | Rootless namespace | <1s | <2% CPU | Security-conscious, no daemon |
| **E2B** | Firecracker microVM | <100ms | <5MB RAM | AI-native, cloud-managed |
| **Firecracker** | Hardware VM (KVM) | <150ms | <5MB RAM | Self-hosted, strongest isolation |
| **gVisor** | Userspace kernel | <1s | 5-15% syscalls | Docker + stronger isolation |
| **Modal** | gVisor + containers | <1s | Managed | Cloud, GPU support |

### 2.3 Recommended Sandbox Strategy for YAAH

For YAAH's testing enhancements, a **tiered approach** makes the most sense:

**Tier 1 — Immediate (Docker containers):**
- Spin up ephemeral Docker containers for test execution
- Mount workspace read-only, disable networking (`--network=none`)
- Run tests, linting, builds inside the container
- Compare results against host execution
- Cost: Docker is already widely available

**Tier 2 — Medium (Firecracker/E2B for untrusted code):**
- For code that the agent generates and the user hasn't reviewed
- Sub-100ms boot time means the overhead is negligible
- Hardware isolation prevents kernel-level escapes

**Tier 3 — Long-term (Integrated sandbox API):**
- A `run_in_sandbox` tool that abstracts the isolation layer
- Configurable isolation depth per task (read-only, container, microVM)
- Result streaming back to the agent loop

### 2.4 Code Verification Methods

1. **Automated test execution in sandbox** — run the project's test suite after agent changes
2. **Static analysis** — lint/type-check inside the sandbox before accepting changes
3. **Diff review** — show the agent's changes to the user for approval
4. **Git worktree isolation** — run agent work in a temporary worktree, only merge if tests pass
5. **Property-based verification** — check invariants (e.g., "all tests pass", "no new lint errors")
6. **Rollback capability** — if verification fails, revert changes and let the agent self-correct

---

## 3. Sub-Agent Spawning: When to Spawn vs. Single Loop

### 3.1 What Claude Code and ZCode Teach Us

**Claude Code's sub-agent model** provides the clearest decision framework:

| Spawn a Sub-Agent | Stay in Single Loop |
|-------------------|---------------------|
| **Context isolation needed** — task would bloat the main context window | Task is small enough that context stays manageable |
| **Parallel execution** — independent subtasks that can run concurrently | Task has sequential dependencies |
| **Specialized expertise** — needs a different system prompt, model, or tool set | Same model and tools suffice |
| **Tool restrictions** — should only have read access, or only bash access | Needs full tool access |
| **Long-running investigation** — shouldn't block the main conversation | Should complete before main agent continues |
| **Risk containment** — untrusted code execution | Safe, reviewed operations |

### 3.2 Decision Matrix for YAAH

```
Is the task independent of the main conversation's context?
  ├── Yes → Can it run in parallel with other tasks?
  │         ├── Yes → SPAWN (parallel sub-agents)
  │         └── No  → SPAWN (isolated context, no blocking)
  └── No  → Is the task small (< 5 tool calls)?
            ├── Yes → SINGLE LOOP (keep it simple)
            └── No  → Is it a different expertise area?
                      ├── Yes → SPAWN (specialized agent)
                      └── No  → SINGLE LOOP (sequential steps)
```

### 3.3 Specific Scenarios for YAAH

**Spawn a sub-agent when:**
- Running the full test suite (isolated, long-running, read-only from main's perspective)
- Code review of a large diff (specialized prompt, read-only tools)
- Architecture research / call-chain mapping (Explore-style, read-only)
- Parallel test execution across multiple modules
- Security scanning of generated code (isolated, restricted tools)
- Documentation generation (different expertise, doesn't need code tools)

**Stay in single loop when:**
- Fixing a single bug (sequential, context-dependent)
- Small refactoring (few tool calls, stays in context)
- File edits that depend on previous tool results
- Interactive debugging (needs back-and-forth with main context)
- User is actively steering the work

---

## 4. Transition Plan: Single-Loop to True Sub-Agent Architecture

### 4.1 What Needs to Change

#### 4.1.1 Core Architecture Changes

**Current:**
```
run_agent() → for each step: model.chat() → execute tools sequentially → repeat
```

**Target:**
```
run_agent() → for each step:
    model.chat() → returns tool_calls + optional sub_agent_delegations
    for each sub_agent_delegation:
        spawn_sub_agent(agent_type, prompt, tools, isolation)
    wait_for_sub_agents() → collect results
    for each tool_call:
        execute_tool()
    append all results to messages
```

#### 4.1.2 New Components Required

1. **Sub-Agent Registry** (`backend/agent/subagents.py`)
   - Define sub-agent types with: name, description, system prompt, allowed tools, model override, max turns
   - Support filesystem-based definitions (`.yaah/agents/*.md`) matching ZCode's `~/.zcode/agents/` pattern
   - Support programmatic definitions (config-driven)

2. **Sub-Agent Spawner** (`backend/agent/spawner.py`)
   - Create isolated agent instances with their own context window
   - Manage lifecycle: spawn, monitor, collect results, terminate
   - Support foreground (blocking) and background (non-blocking) execution
   - Handle result serialization: sub-agent's final message becomes the tool result

3. **Agent Tool** (`backend/agent/tools.py` — new tool)
   - `spawn_agent(agent_type, prompt, run_in_background)` — the model's interface to sub-agents
   - `list_agents()` — show running sub-agents
   - `get_agent_result(agent_id)` — fetch a background agent's output

4. **Concurrency Layer** (`backend/agent/concurrency.py`)
   - `asyncio.TaskGroup`-based parallel execution
   - Result collection with timeout
   - Cancellation propagation from parent to children

5. **Isolation Layer** (`backend/agent/isolation.py`)
   - Git worktree support (Claude Code's `isolation: worktree`)
   - Docker container support (OpenHands's approach)
   - Configurable isolation depth per sub-agent type

#### 4.1.3 Loop Modifications

```python
# In loop.py, the tool execution section becomes:

# Execute sub-agent delegations (parallel)
sub_agent_tasks = {}
for tc in tool_calls:
    if tc["function"]["name"] == "spawn_agent":
        args = json.loads(tc["function"]["arguments"])
        task = asyncio.create_task(_spawn_sub_agent(args))
        sub_agent_tasks[tc["id"]] = task
    else:
        # Regular tool execution (sequential, as before)
        result = await execute_tool(name, args, workspace)
        messages.append(tool_result)

# Collect sub-agent results
for tc_id, task in sub_agent_tasks.items():
    result = await task
    messages.append({
        "role": "tool",
        "tool_call_id": tc_id,
        "content": result["output"],  # sub-agent's final message
    })
```

#### 4.1.4 Event Stream Changes

The NDJSON event stream needs new event types:

```json
{"type": "sub_agent_spawned", "agent_id": 1, "agent_type": "code-reviewer", "status": "running"}
{"type": "sub_agent_progress", "agent_id": 1, "text": "..."}
{"type": "sub_agent_result", "agent_id": 1, "output": "..."}
{"type": "sub_agent_done", "agent_id": 1, "status": "completed|failed|timeout"}
```

#### 4.1.5 Database Changes

- New table: `sub_agents` — id, parent_conversation_id, agent_type, status, created_at, completed_at
- New table: `sub_agent_messages` — id, sub_agent_id, role, content, timestamp
- Existing conversation messages table unchanged (sub-agent messages are separate)

#### 4.1.6 Frontend Changes

- Sub-agent list panel showing running/completed agents
- Per-agent progress indicators
- Ability to view sub-agent output without breaking main conversation flow
- Background agent notifications when they complete

### 4.2 Implementation Phases

**Phase 1 — Foundation (2-3 weeks):**
- Sub-agent registry with filesystem-based definitions
- Basic `spawn_agent` tool (foreground, same model, same tools)
- Result collection and serialization
- NDJSON event stream extensions

**Phase 2 — Concurrency (2-3 weeks):**
- Background execution support
- Parallel sub-agent spawning
- Cancellation propagation
- Sub-agent list UI

**Phase 3 — Isolation (3-4 weeks):**
- Git worktree isolation
- Docker container isolation
- Tool restriction enforcement
- Per-agent model selection

**Phase 4 — Advanced (ongoing):**
- Goal mode (ZCode's auto-verification loop)
- Custom sub-agent definitions in Settings UI
- Trajectory inspection and step replay
- Agent-to-agent messaging

---

## 5. ZCode Feature Parity Analysis

### 5.1 ZCode Feature Map vs. YAAH

| Feature | ZCode | YAAH | Gap |
|---------|-------|------|-----|
| **Goal Mode** | Auto-iteration with verification | ❌ | Major — core differentiator |
| **Sub-agents** | Built-in (general-purpose, Explore) + custom | ❌ | Major — architectural change |
| **Execution modes** | 5 modes (balanced, cautious, semi-auto, plan, full) | ❌ | Medium — permission model |
| **Safety confirmations** | Destructive command warnings | Partial — tool descriptions | Medium |
| **Edit history** | File rewind with safety summary | ❌ | Medium |
| **Browser preview** | Live browser rendering | ❌ | Medium |
| **Remote control** | Phone pairing via QR | ❌ | Low |
| **Bot channel** | Feishu/WeChat integration | ❌ | Low |
| **MCP support** | Yes | ❌ | Medium |
| **Hooks** | Pre/post tool hooks | ❌ | Medium |
| **Memory** | Persistent memory across sessions | ❌ | Low |
| **Wiki** | Knowledge base generation | ❌ | Low |
| **Plugin system** | Skills, subagents, MCP, hooks | Skills only | Medium |
| **BYOK** | Full BYOK private deployment | Partial — provider config | Low |
| **Trajectory replay** | Internal activity log | Partial — conversation history | Medium |

### 5.2 ZCode's Goal Mode — Deep Dive

Goal Mode is ZCode's killer feature for long-running tasks:

1. User sets objective: `/goal Refactor the auth module and keep tests passing`
2. Agent works in rounds
3. At end of each round, **automatic verification** checks if goal is met
4. If not met → next step generated, next round starts automatically
5. If met → task wraps up with summary
6. Verification requires **real evidence** (changed files, test results), not just claims
7. User can pause/resume/replace/clear at any time
8. Goal state persists across session close/reopen

**What this means for YAAH:**
- Requires a **verification loop** separate from the main agent loop
- Needs a **goal state machine** (pending → running → verifying → complete/paused)
- Requires **evidence collection** (file diffs, command output, test results)
- Needs **persistent goal storage** in the database

### 5.3 ZCode's Sub-Agent System

- **General-purpose**: full tool access, default worker
- **Explore**: read-only, code search specialist
- **Custom**: user-defined via `~/.zcode/agents/*.md` with frontmatter
  - `name`, `description`, `model`, `thoughtLevel`, `color`, `tools`, `maxTurns`, `injectAgentsMd`, `mcpServers`
- **Execution**: foreground (parallel, blocking) or background (non-blocking)
- **Inheritance**: sub-agents inject workspace AGENTS.md by default
- **Limitation**: sub-agents cannot spawn their own sub-agents (no nesting)

---

## 6. Frontier Agent Harness Comparison

### 6.1 Feature Matrix

| Capability | YAAH | Claude Code | ZCode | Codex CLI | DeepSeek Harness | OpenHands | Aider |
|------------|------|-------------|-------|-----------|------------------|-----------|-------|
| **Agent loop** | Single, sequential | Hierarchical, parallel | Single + goal mode | Single | Plugin-configurable | Docker-isolated | Dual-model |
| **Sub-agents** | ❌ | ✅ (tens/hundreds) | ✅ (built-in + custom) | ❌ | ✅ (plugin-based) | ❌ | ❌ |
| **Parallel execution** | ❌ | ✅ | ✅ (foreground) | ❌ | ✅ | ❌ | ❌ |
| **Background agents** | ❌ | ✅ | ❌ (foreground only) | ❌ | ✅ | ❌ | ❌ |
| **Sandbox** | None | Permission-based | Safety confirmations | OS-level container | Plugin-based | Docker | Git-based |
| **Goal mode** | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **MCP support** | ❌ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ |
| **Hooks** | ❌ | ✅ | ❌ | ❌ | ✅ (event seams) | ❌ | ❌ |
| **Context compaction** | ❌ (image pruning only) | ✅ (auto at 98%) | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Trajectory replay** | Partial | ✅ (JSONL step replay) | Activity log | ❌ | ✅ (forking) | ❌ | ❌ |
| **Worktree isolation** | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ (auto-commit) |
| **Model flexibility** | BYO provider | Claude family | GLM-5.3 | OpenAI | Multi-model | Multi-model | Multi-model |
| **Open source** | ✅ | ❌ | ❌ (client) | ❌ (CLI) | ✅ (MIT) | ✅ | ✅ |
| **Desktop app** | ✅ (Tauri) | Terminal/IDE | ✅ (Electron) | CLI | Web UI/CLI | Web | CLI |
| **Plugin system** | Skills only | Plugins | Skills + subagents + MCP | ❌ | Everything is plugin | ❌ | ❌ |

### 6.2 Key Differentiators by Harness

**Claude Code:**
- **Parallel sub-agents**: Can spawn tens to hundreds of sub-agents in a single session
- **Hooks system**: Pre/post tool hooks for validation, logging, safety
- **Trajectory forking**: Pause at step N, modify prompt, resume from fork
- **Context compaction**: Automatic at 98% of context window
- **Worktree isolation**: Sub-agents can run in temporary git worktrees

**ZCode:**
- **Goal mode**: Auto-iteration with verification — the closest thing to "set and forget"
- **Custom sub-agents**: Per-agent model selection (cheap model for research, expensive for coding)
- **Execution modes**: 5 levels of confirmation friction
- **Browser preview**: Live rendering of web changes
- **Remote control**: Phone-based steering

**DeepSeek Harness:**
- **Plugin everything**: Models, tools, sandboxes, loops, sessions are all plugins
- **Cordis microkernel**: Service/event/reversible-effect extension model
- **Trajectory forking**: Append-only JSONL with step-level fork and resume
- **Profile system**: Named compositions of bundles and patches

**OpenHands:**
- **Docker sandbox**: Full filesystem + network isolation
- **Headless API**: CI/CD integration for autonomous issue resolution
- **Self-hosted**: No cloud dependency

**Aider:**
- **Dual-model mode**: One model designs, another implements
- **Git hygiene**: Every change auto-committed
- **Architect/editor split**: Scoping phase before implementation

### 6.3 What YAAH Has That Others Don't

- **Tauri desktop app** with embedded FastAPI backend — lighter than Electron
- **Skills system** — markdown-based, user-extensible, model-invocable mid-turn
- **Computer use tools** — screenshot, UI automation, mouse/keyboard (unique among coding agents)
- **Voice dictation** — local whisper.cpp + Kokoro TTS read-aloud
- **Local-first privacy** — zero telemetry, localhost-only, no accounts
- **GIMP integration** — image editing via MCP tools (unique)

---

## 7. Recommended V1 Plan

### 7.1 Priority 1: Sub-Agent Foundation (Weeks 1-4)

**Goal**: Enable the model to delegate isolated tasks to sub-agents.

```
backend/agent/subagents.py      # Sub-agent registry + definitions
backend/agent/spawner.py        # Spawn, monitor, collect results
backend/agent/tools.py          # New: spawn_agent, list_agents tools
backend/agent/loop.py           # Modified: handle sub-agent delegations
backend/db/database.py          # New: sub_agents, sub_agent_messages tables
```

**Sub-agent definitions** (filesystem-based, ZCode-style):
```yaml
# ~/.yaah/agents/code-reviewer.md
---
name: code-reviewer
description: Expert code reviewer. Use for quality, security, and maintainability reviews.
tools: [read_file, search_files, bash]
maxTurns: 20
---
You are a code review specialist...
```

**New tool**: `spawn_agent(agent_type, prompt, run_in_background=false)`
- Model calls this to delegate work
- Returns immediately with agent_id
- Result collected when agent completes (foreground) or on demand (background)

### 7.2 Priority 2: Goal Mode (Weeks 5-8)

**Goal**: Set an objective, agent iterates until verified.

```
backend/agent/goal.py           # Goal state machine + verification
backend/agent/loop.py           # Modified: goal-aware loop
backend/db/database.py          # New: goals table
```

**Goal state machine**:
```
PENDING → RUNNING → VERIFYING → RUNNING → ... → COMPLETE
                    ↓
                  PAUSED (user intervention)
```

**Verification**: After each round, run a verification step that checks:
- Changed files (git diff)
- Command output (test results, lint output)
- Goal-specific criteria (user-defined)

### 7.3 Priority 3: Concurrency & Background (Weeks 9-12)

**Goal**: Parallel sub-agents, background execution.

```
backend/agent/concurrency.py  # TaskGroup-based parallel execution
backend/agent/spawner.py      # Background execution support
src/components/               # Sub-agent list UI, progress indicators
```

### 7.4 Priority 4: Isolation (Weeks 13-16)

**Goal**: Sandboxed sub-agent execution.

```
backend/agent/isolation.py    # Worktree + Docker isolation
backend/agent/spawner.py      # Isolation-aware spawning
```

### 7.5 Feature Parity Roadmap to ZCode V1

| ZCode Feature | YAAH Equivalent | Priority |
|---------------|-----------------|----------|
| Goal Mode | Goal mode with auto-verification | P1 |
| Sub-agents (built-in) | Sub-agent registry with Explore + general-purpose | P1 |
| Sub-agents (custom) | Filesystem-based agent definitions | P1 |
| Execution modes | Permission/confirmation levels | P2 |
| Safety confirmations | Destructive command warnings | P2 |
| Edit history | File diff viewer + git integration | P2 |
| Hooks | Pre/post tool hooks | P3 |
| MCP support | MCP client integration | P3 |
| Browser preview | Web server + iframe preview | P3 |
| Trajectory replay | Enhanced conversation history | P3 |

### 7.6 Architecture Decision: Keep It Simple First

**Recommendation**: Start with a **single-loop enhancement** before full sub-agent architecture.

The single-loop can be enhanced with:
1. **`run_in_sandbox` tool** — execute commands in Docker, return results
2. **`verify_goal` tool** — check goal progress, return next step
3. **Parallel tool execution** — when the model emits independent tool calls, run them concurrently

This gives 80% of the value with 20% of the architectural change. Sub-agents come next.

---

## Appendix A: Code Review — Current Loop vs. Proposed Loop

### Current Loop (Simplified)

```python
async def run_agent(conversation_id, user_text, workspace):
    messages = [system_prompt] + history
    tools = get_schemas()
    
    for step in range(max_steps):
        response = await model.chat(messages, tools)
        
        if not response.tool_calls:
            yield done
            return
        
        for tc in response.tool_calls:
            result = await execute_tool(tc.name, tc.args)
            messages.append(tool_result(tc.id, result))
```

### Proposed Loop (With Sub-Agents)

```python
async def run_agent(conversation_id, user_text, workspace):
    messages = [system_prompt] + history
    tools = get_schemas()
    
    for step in range(max_steps):
        response = await model.chat(messages, tools)
        
        if not response.tool_calls:
            yield done
            return
        
        # Separate sub-agent delegations from regular tools
        agent_tasks = {}
        regular_tools = []
        
        for tc in response.tool_calls:
            if tc.name == "spawn_agent":
                agent_tasks[tc.id] = asyncio.create_task(
                    _spawn_sub_agent(tc.args)
                )
            else:
                regular_tools.append(tc)
        
        # Execute regular tools (sequential, as before)
        for tc in regular_tools:
            result = await execute_tool(tc.name, tc.args)
            messages.append(tool_result(tc.id, result))
        
        # Collect sub-agent results
        for tc_id, task in agent_tasks.items():
            result = await task
            messages.append(tool_result(tc_id, result))
```

---

## Appendix B: Sub-Agent Definition File Format

Based on ZCode's Markdown frontmatter approach:

```markdown
---
name: code-reviewer
description: Expert code review specialist for security, performance, and maintainability.
model: sonnet          # Optional: override model for this agent
tools: [read_file, search_files, bash, git_status]
disallowedTools: [write_file, delete_file]
maxTurns: 20
background: true        # Run as non-blocking background task
isolation: worktree     # Run in temporary git worktree
effort: medium          # Reasoning effort level
---

You are a code review specialist. When reviewing code:
- Identify security vulnerabilities
- Check for performance issues
- Verify adherence to coding standards
- Suggest specific improvements

Be thorough but concise in your feedback.
```

---

## Appendix C: Threat Model for Testing Enhancements

When adding sandbox testing capabilities, the threat model expands:

| New Risk | Mitigation |
|----------|------------|
| Sandbox escape via Docker/KVM exploit | Use read-only mounts, drop capabilities |
| Agent tricks sandbox into exfiltrating data | Disable network in sandbox, audit tool outputs |
| Sandbox resource abuse (crypto mining, etc.) | CPU/memory limits, timeouts |
| Malicious test suite in workspace | Run tests in isolated container, not host |
| Agent modifies sandbox config to weaken isolation | Lock sandbox configuration, validate before spawn |

---

*End of report.*
