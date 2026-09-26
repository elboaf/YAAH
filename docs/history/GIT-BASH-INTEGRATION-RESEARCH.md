# Git Bash integration on Windows: research notes

**Status:** Research only; no behavior change approved or implemented.  
**Scope:** How YAAH could integrate Git Bash as the Windows implementation of its `bash` tool, including the failure surface in the reverted implementation.  
**Date:** 2026-09-26.

## Summary

Git Bash is a plausible Windows shell for YAAH’s `bash` tool, but treating it as “find `bash.exe` and execute `bash.exe -c <command>`” skips important Git-for-Windows launcher setup and Windows/MSYS process-boundary behavior. A robust implementation needs one explicit shell-runtime contract shared by command execution, tool help, local model context, and remote-host capability reporting.

The most concrete finding from the prior implementation (commit `082f97f`, reverted by `40e7af3`) is that it located and launched `<Git>\\usr\\bin\\bash.exe` directly. Git for Windows documents that its wrappers modify environment variables before spawning the actual binaries; its `bin\\bash.exe` wrapper is specifically documented as setting `MSYSTEM` and `PATH` before spawning `usr\\bin\\bash.exe`. Direct launch therefore bypasses documented setup. This is a credible integration defect to investigate, **not an established reason for the revert**: the revert commit says only “Revert” and records no technical rationale.

Recommended direction for a future implementation:

1. Define a Windows shell-runtime resolver that selects an explicit executable and invocation strategy; don’t let `create_subprocess_shell` and the model prompt independently infer the shell.
2. Discover a Git-for-Windows installation through stable signals, validate its layout, and invoke its documented wrapper (likely `<Git>\\bin\\bash.exe` for non-interactive `-c` use, or `git-bash.exe` if its startup contract is verified). Avoid globally adding Git’s `usr\\bin`/`mingw64\\bin` directories to PATH.
3. Launch an explicit executable with an argument vector; send the command as one Bash `-c` argument. Keep Windows process argument quoting distinct from Bash parsing. Avoid interpolating user-controlled values into shell snippets where positional args/stdin can be used.
4. Decide deliberately whether the invocation is interactive or login. A tool command with captured pipes is non-interactive; do not imply it behaves exactly like a user’s Git Bash terminal unless startup files and TTY behavior are deliberately provided.
5. Preserve the user’s Windows `PATH` for native tools and ensure the wrapper configures Git Bash internals. Test that native Windows executables, Git, and POSIX utilities all work. Test MSYS argument/path conversion with paths and arguments that look like POSIX switches.
6. Ensure timeout cancellation kills Bash and descendants using the Windows Job Object mechanism already present in YAAH, and verify the whole tree is gone. Preserve output and the actual exit status.
7. Share the resolved shell description/capability with prompt construction. For remote execution, the remote host must report its shell capability; never infer a remote shell from the client machine.
8. Decide post-`install_git` behavior explicitly: discovery should happen per invocation (or be invalidated after installation) so newly installed Git Bash is visible without restarting YAAH. Build a fresh child environment from the live backend environment and wrapper setup; PATH changes made by an installer do not retroactively mutate an already-running process environment.
9. Verify on clean Windows images with Git absent, standard system install, per-user install, Git Bash present but not on PATH, unrelated `bash.exe` earlier on PATH, spaces in install/workspace paths, and the actual bundled installer. Unit tests with fake filesystem layouts are useful but do not substitute for executing a real Git-for-Windows installation.

## Repository evidence

- Current `backend/agent/tools.py` invokes `asyncio.create_subprocess_shell(command, cwd=..., env=_tool_env(), ...)`; therefore Windows shell interpretation follows Python/Windows shell selection, normally `COMSPEC`/`cmd.exe`.
- `_tool_env()` uses `ghenv.command_env()`, which copies the backend process environment and prepends bundled `gh`; it does not currently locate Git or Git Bash.
- `backend/agent/loop.py::_shell_phrase()` describes `COMSPEC` on Windows. Existing tests verify this message, not a real Windows process selection.
- `backend/agent/gitenv.py::run_install_git()` installs the bundled Git for Windows silently. On success it returns a guessed default `Program Files\\Git\\cmd\\git.exe` fallback and says open shells need restart. It does not refresh the backend process PATH or expose Git Bash.
- Commit `082f97f` added `backend/agent/shell.py`, discovery by PATH/default install directories, direct invocation of `usr\\bin\\bash.exe -c`, fallback to the system shell only if process creation raised `OSError`, local and remote shell descriptions, and mocked tests. It did not record clean-Windows integration validation.
- Commit `40e7af3` reverts all those changes with no rationale beyond its subject/body. Do not infer an actual regression cause from this alone.
- `backend/agent/remote.py` forwards workspace shell tools to the host. Shell capability is a property of the executing host, so any future shell selection must be conveyed by host metadata and reflected in the remote prompt.

## Source findings

### Git for Windows wrappers and environment

Git for Windows explains that common entry points such as `<Git>\\git-bash.exe`, `<Git>\\cmd\\git.exe`, and `<Git>\\bin\\bash.exe` are wrappers, not the underlying binaries. The wrapper sets environment variables and spawns the actual implementation. In particular, `<Git>\\bin\\bash.exe` sets `MSYSTEM` and `PATH` and spawns `<Git>\\usr\\bin\\bash.exe`. The wrapper sets Git-specific PATH ordering, `HOME`, and `MSYSTEM`; it may set `PLINK_PROTOCOL` when needed. The project cautions against adding internal `usr\\bin` and `mingw64\\bin` directories globally to PATH because their DLLs can conflict with unrelated software.

Implication: resolve the installation root and use a wrapper, rather than launching `usr\\bin\\bash.exe` with the backend’s ordinary Windows environment. Exact wrapper behavior should be checked against the Git-for-Windows version YAAH supports.

Sources:
- Git for Windows, “Git wrapper”: https://gitforwindows.org/git-wrapper.html
- Git for Windows source entry point referenced by that page (`setup_environment()`): https://github.com/git-for-windows/git (source moves; pin a release/commit when relying on exact implementation details).

### Windows process environment inheritance

Windows child processes inherit a copy of the parent’s environment block by default. A parent can supply a different environment block at process creation. An installer’s PATH update does not mutate the already-running backend’s environment snapshot; a child of that backend inherits that old environment unless YAAH constructs a new environment or uses an explicit executable path.

Sources:
- Microsoft Learn, “Changing Environment Variables”: https://learn.microsoft.com/en-us/windows/win32/procthread/changing-environment-variables
- Microsoft Learn, “Environment Variables”: https://learn.microsoft.com/en-us/windows/win32/procthread/environment-variables

### Process invocation and quoting

Python recommends an argument sequence where possible. On Windows, Python must serialize that sequence into the command-line string Windows process creation consumes; after launching Bash, Bash separately parses the string following `-c`. These are distinct quoting layers. Explicit executable selection avoids relying on PATH/command-line executable inference, particularly when paths contain spaces. A shell tool intentionally accepts shell code, so quote handling remains part of its interface; path/value parameters added by YAAH should not be concatenated into that code where safer argument or stdin channels are practical.

Sources:
- Python, `subprocess`: https://docs.python.org/3/library/subprocess.html
- Microsoft, `CreateProcessA`: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessa

### Login and interactive behavior

Bash startup-file behavior depends on whether it is interactive and/or a login shell and on invocation options. YAAH’s shell tool captures stdout/stderr over pipes, so it is not a normal interactive terminal. Select login/interactive semantics explicitly; do not assume launching a Git Bash terminal wrapper reproduces the startup behavior needed by a captured `-c` command.

Sources:
- GNU Bash manual, “Invoking Bash”: https://www.gnu.org/software/bash/manual/html_node/Invoking-Bash.html
- GNU Bash manual, “Bash Startup Files”: https://www.gnu.org/software/bash/manual/html_node/Bash-Startup-Files.html

### MSYS2 and path conversion

Git for Windows combines native Windows/MINGW programs and MSYS2 programs. Bash is an MSYS2 process; many Git executables are native Windows programs. MSYS runtime conversion can affect arguments and environment values when an MSYS process launches a native Windows executable. It is not safe to assume every Windows path and POSIX path is interchangeable or every slash-prefixed argument is converted the same way. Test actual command cases against the supported Git-for-Windows runtime, especially native programs receiving path-like arguments such as `/...`.

Sources:
- Git for Windows, “The difference between MINGW and MSYS2”: https://gitforwindows.org/the-difference-between-mingw-and-msys2.html
- Git for Windows MSYS2 runtime path conversion source: https://github.com/git-for-windows/msys2-runtime/blob/main/winsup/cygwin/msys2_path_conv.cc (moving branch; pin a revision for version-specific claims).

### Timeout, exit status, process trees

Python exposes a completed process’s status through `returncode`; negative signal statuses are a POSIX convention, not a portable Windows interpretation. A timeout or cancellation must account for descendant processes, not only the initial Bash wrapper. YAAH already has Windows Job Object process-tree handling in `tools.py`; a new explicit-executable path must preserve and validate that behavior. The exit status from Bash `-c` is meaningful for the tool, but commands that themselves spawn work in the background need explicit policy/tests.

Source:
- Python, `subprocess`: https://docs.python.org/3/library/subprocess.html

## Gaps and proposed verification

The reverted tests mocked `create_subprocess_exec`/`create_subprocess_shell`, checked basic discovery using placeholder files, and asserted prompt text. They did not execute the Git wrapper or a real Bash binary, verify effective `MSYSTEM`/PATH/HOME, validate clean install discovery, exercise MSYS path conversion, or prove Job Object cancellation of a Bash descendant on Windows.

Before reintroducing this behavior, add:

- Unit tests for shell-runtime selection and prompt/environment description using the same resolved runtime object.
- Windows integration tests running a real supported Git-for-Windows install: `printf`, `pwd`, POSIX utilities, `git --version`, and execution of a native Windows executable.
- Tests for installation in paths containing spaces, per-user installation, Git installed but absent from the backend PATH, unrelated Bash before Git Bash, and Git becoming available immediately after `install_git` without YAAH restart.
- Path-conversion tests that call a native executable with Windows paths, POSIX paths, and slash-prefixed option-like arguments.
- Timeout test that starts a descendant, cancels the tool, and confirms both Bash and descendants terminate.
- Remote test proving host-reported shell choice, not client-local detection, determines prompt guidance.
- Clean Windows CI/VM validation. Fake `bash.exe` marker files cannot test process creation or Git-for-Windows wrapper semantics.

## Sources consulted

All sources above are first-party project documentation/source, language documentation, or Microsoft documentation. The report makes no claim that the observed direct-launch gap caused the revert; the commit contains no recorded cause.
