# Contributing to NemoClaw Escapades

This guide covers the development workflow, code standards, and merge request
requirements for NemoClaw Escapades.  It is written for both human developers
and AI coding assistants (so it can be referenced during automated code review).

## Development Setup

```bash
# Clone and install (editable mode + dev dependencies)
cd ~/workspace/nemoclaw_escapades
make install

# Or manually:
pip install -e ".[dev]"
```

The project uses:

- **Python 3.13+** (see `requires-python` in `pyproject.toml`)
- **Hatch** for builds, **pip** for editable installs
- **slack-bolt** for the Slack connector
- **httpx** / **aiohttp** for HTTP clients
- **pydantic** for data models and validation
- **tenacity** for retry logic
- **websockets** for the NMB broker
- **SQLAlchemy** + **aiosqlite** + **Alembic** for the audit database

## Project Structure

```
src/nemoclaw_escapades/
  main.py                  # Orchestrator entry point
  config.py                # Environment-based configuration
  orchestrator/            # Multi-turn agent loop, approval gates
  nmb/                     # NemoClaw Message Bus
    broker.py              #   Asyncio WebSocket message router
    client.py              #   Async MessageBus client
    models.py              #   Wire protocol types (Pydantic)
    sync.py                #   Synchronous wrapper
    audit/                 #   SQLite audit DB (Alembic-managed)
    testing/               #   Integration test infrastructure
  connectors/              # Channel connectors (Slack, etc.)
  backends/                # Inference backends (NVIDIA Inference Hub)
  tools/                   # Tool integrations (Jira, etc.)
  models/                  # Shared data types
tests/
  test_*.py                # Unit tests
  integration/             # Multi-sandbox NMB integration tests
```

## Running Checks

```bash
make lint         # ruff check + format check + mypy — must pass
make typecheck    # mypy src/ (strict mode) — must pass
make test         # pytest tests/ -v (unit, excludes integration)
make test-all     # unit + integration
make fmt          # auto-format code
```

### Test categories

The test suite contains two categories:

- **Unit tests** (always run): `tests/test_*.py`.
  These mock all external calls and should pass on any machine.

- **Integration tests** (require infrastructure): `tests/integration/`.
  These spin up real NMB broker instances and multiple sandboxes.
  They require a running OpenShell gateway.

### Quick check during development

```bash
make lint && make test
```

## Merge Request Requirements

Every MR must satisfy the following before merge:

1. **mypy passes with zero errors** (`make typecheck`).  The project uses
   `strict = true`; all code must be fully type-annotated.
2. **Linting passes** (`make lint`).  Ruff enforces style, import ordering,
   and common Python errors.
3. **All unit tests pass.**  These run without any external infrastructure.
4. **Integration tests pass for affected components.**  If your MR changes
   the NMB broker, the NMB integration tests must pass.
5. **New functionality has tests.**  Unit tests for pure logic, integration
   tests for multi-sandbox behaviour.
6. **Commit messages reference a ticket** where applicable
   (e.g. `AVPC-12345: Short description`).

## Code Guidelines

### 1. No Magic Numbers

Literal numeric values must not appear in function bodies.  Use named
constants at module scope:

```python
# Bad
@retry(max_retries=4)

# Good
_MAX_RETRIES: int = 4

@retry(max_retries=_MAX_RETRIES)
```

If the value should be user-configurable, add it to `config.py` as a
pydantic field with an environment variable override.

### 2. Test Coverage

All functionality should be covered by tests:

- **Unit tests** for pure logic, data models, and utilities.  These run
  offline and fast.  Place them in `tests/test_<module>.py`.
- **Integration tests** for multi-sandbox NMB scenarios.  Place them in
  `tests/integration/`.
- When fixing a bug, add a regression test that would have caught it.

### 3. Imports at the Top

All imports should be at the module level, not inside functions, unless
there is a specific reason (e.g. avoiding circular imports or deferring
heavy imports for startup speed):

```python
# Preferred
from nemoclaw_escapades.nmb.models import Envelope, MessageType

# Acceptable only when justified (e.g. deferring a heavy client import
# until the code path is actually invoked)
def handle_jira_action(...):
    from nemoclaw_escapades.tools.jira import JiraTool
```

### 4. Docstrings

All public functions, methods, and classes must have docstrings.  Use
the following conventions:

```python
def execute(
    self,
    sql: str,
    catalog: str | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Execute a SQL query against the configured Kratos backend.

    Tries DRS mode first; falls back to warehouse/cluster mode on
    4xx/5xx errors that indicate the table is not available in Trino.

    Args:
        sql: SQL query string.
        catalog: Override the default catalog.
        schema: Override the default schema.

    Returns:
        Uniform result dict with ``columns`` and ``data`` keys.

    Raises:
        RuntimeError: If Kratos is not configured or query fails.
        httpx.HTTPStatusError: On non-retryable HTTP errors.
    """
```

Key requirements:
- First line: concise summary of what the function does.
- **Args**: document every parameter with its type and purpose.
- **Returns**: describe the return value and its structure.
- **Raises**: list exceptions the caller should expect.
- Internal/private helpers (`_foo`) should still have docstrings, but
  they can be shorter.

### 5. Type Annotations

All function signatures must be fully type-annotated.  This is enforced
by mypy in strict mode.  Key rules:

```python
# All parameters and return types annotated
def search(query: str, limit: int = 20) -> dict[str, Any]: ...

# Use X | None instead of Optional[X]
def get_user(user_id: str | None = None) -> dict[str, Any]: ...

# Generic collections are parameterised
def list_items() -> list[dict[str, Any]]: ...

# Test methods include -> None and fixture params use Any
def test_search(self, invoke: Any, assert_envelope: Any) -> None: ...
```

For pytest fixture parameters in test files, use `Any` since the actual
types are injected by the framework.

### 6. mypy and Linting Must Pass

- **mypy**: `strict = true` in `pyproject.toml`.  Zero errors required.
  Do not add `type: ignore` comments unless absolutely necessary, and
  always include the specific error code (e.g. `# type: ignore[arg-type]`).
- **ruff**: Enforces PEP 8 style, import sorting (`I`), naming (`N`),
  and modern Python syntax (`UP`).  Run `make fmt` to auto-fix.
- **Line length**: 100 characters max.

## Credential Storage

NemoClaw Escapades loads secrets from environment variables and `.env` files.
Follow these guidelines to keep credentials safe:

1. **Set restrictive permissions** on any file containing tokens:
   ```bash
   chmod 600 .env
   ```
2. **Never commit `.env` files.**  The repo `.gitignore` excludes `.env`.
   If you create additional env files, add them to `.gitignore` before
   committing.
3. **Secrets flow through OpenShell providers**, not the filesystem.  In
   sandbox mode, credentials are injected via `openshell provider create`
   and are never written to disk inside the sandbox.

## Dependency Security

### Coding policy — untrusted data

- **No untrusted redirected streaming.**  If you add code that fetches
  from URLs not controlled by NVIDIA, disable redirects
  (`follow_redirects=False` for httpx) or validate the redirect target.
- **Cap response sizes.**  Set `timeout` and consider `max_content_length`
  guards on any HTTP response consumed from untrusted sources.
- **Sanitise user input.**  Any data originating from Slack messages or
  other external channels must be validated before being passed to tools,
  database queries, or shell commands.
