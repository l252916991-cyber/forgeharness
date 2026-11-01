# ADR 044: A file backend protocol, and a container for code that runs

Date: 2026-10-30. Status: implemented.

## Context

The runtime had protocols for every stateful boundary except the filesystem:
`CheckpointStore`, `AsyncExecutionBackend`, `VectorStore`, `SessionStore`. Tools called
`Path` directly through `tools/paths.py`, and every environment they ran in was the host.

`tools/policy.py` says so itself: refusing a path prefix is "defence in depth, not a
sandbox". A policy can refuse a path and refuse a command shape. It cannot stop a
permitted command from deleting files it is allowed to reach, and it cannot stop that
command from running on the operator's machine. That is not a bug in the policy; it is
the boundary of what a policy is.

The cost of both facts is portability and blast radius. Adding a new execution
environment meant editing the tools; and the first time a permitted command is destructive,
the workspace it destroys is the real one.

## Decision

`backends/protocol.py` defines `FileBackend`: list, read, write, digest, search, run. Errors
are typed (`FileConflict`, `FileTooLarge`, `NotRegularFile`, `BackendError`) so a tool can
react to *why* something failed instead of parsing a message.

Three implementations, each with a reason to exist:

- `LocalBackend` -- file operations hold no-follow POSIX directory descriptors from
  the workspace root to each parent. Reads and hashes open only regular files; atomic
  writes create an exclusive temporary file and rename relative to the held parent.
  Symlinks, including aliases within the workspace, and multiply linked files are
  refused by content reads/hashes and existing-file updates. A concurrent parent
  name replaced with a symlink cannot redirect an already opened operation to its
  target. Directory locks serialize cooperating backend writers. These are file API
  protections: configured commands still run on the host and require a container when
  repository code is untrusted.
- `ContainerBackend` -- file operations still go over the bind mount through
  `LocalBackend`, because the harness's own file tools never execute repository code: the
  threat they face is a wrong path, already refused. The threat that needs a container is
  *executing repository code*, so that is what is containerised.
- `CompositeBackend` -- routes by path prefix so one workspace can mix a host directory
  and a sandboxed one. Commands always use the default backend: a command is not
  path-scoped, and `pytest tests/` touches the whole workspace.

The container flags are the decision, and each one is load-bearing: `--network none` (a
test command cannot fetch a payload), `--read-only` plus a single read-write bind mount
(the container's own filesystem is immutable; only the workspace is writable), `--user
1000:1000` with `--cap-drop ALL` and `no-new-privileges` (no capability and no setuid path),
`--memory` with `--memory-swap` equal to it (an unset swap turns the memory cap into a
slowdown instead of a limit), `--pids-limit`, and `--init` so a forking command does not
leak processes past the container's life. `ContainerLimits` refuses `user="0"` at
validation time: a sandbox that can run as root is not a sandbox.

Commands are always argv vectors. Nothing builds a shell line, inside or outside the
container, so an argument stays a word even when it looks like `; rm -rf /`.

`WarmContainerPool` starts containers ahead of demand, because a container start costs a
second and a test-heavy run starts hundreds. A container that produced a timeout (exit
124) is discarded rather than reused: a command that ignored a kill signal has an unknown
process tree, and handing that to the next caller would leak state between tasks.

## Consequences

`coding/tools.py` now constructs a backend per tool call (`BackendFactory = Callable[[Path],
FileBackend]`) and records the backend name in every tool's metadata, so a trace can answer
"did this run on the host or in the sandbox" without guessing from the command.

`ReadObservationTool` deliberately bypasses the backend: it reads harness state under
`.forgeharness/`, not workspace content, and routing it through a backend would let a
routing rule hide the record of what happened.

A backend that owns containers must be closable, so `ContainerBackend` never constructs a
pool it has no way to close; the pool is passed in by whoever owns it.

`LocalBackend.iter_files` is the public streaming listing surface for SDK
consumers that filter a file type before applying their own count limit. It
holds the same no-follow directory descriptors as bounded listings; consumers
must close it when stopping early. Repository mapping imports it through the SDK
and never reaches into backend traversal internals. This preserves its Python-file
limit even when hundreds of other files sort before the Python sources.

Both local and container file search use the no-follow file boundary over the workspace
bind mount, rather than letting a new `rg` process reopen checked path names. The literal
scanner emits `path:line:column` matches and bounds returned bytes. It skips hidden entries,
binary files, and files over 1,000,000 bytes; skipped oversized files set `truncated=True`.
Unlike ripgrep, this scanner does not read repository ignore files. This deliberate
compatibility boundary prevents search from reading another image directory or following
a concurrently substituted parent. The coding repository map also uses the safe walker
and bounded reads; changed files produce a parse-error record instead of outside symbols.

The host controls workspace placement and the Docker daemon/image. These checks do not
turn `LocalBackend.run` into an OS sandbox, prevent another privileged process from moving
an already opened directory, or qualify Docker isolation on a real deployment. Recursive
directory enumeration can still be expensive on very large operator-selected workspaces;
runtime call deadlines and output bounds do not establish a per-directory memory quota.
