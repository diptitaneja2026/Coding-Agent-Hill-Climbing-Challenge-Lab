"""Robust coding agent with tools for inspecting, editing, and executing code in a local sandbox."""

import os
import subprocess
import sys
import warnings

from google.adk.agents import Agent
from google.genai import types

# The default model, shared with eval.py so there is a single source of truth.
DEFAULT_MODEL = "gemini-3.1-flash-lite"

# Silencing an ADK warning to declutter terminal output
warnings.filterwarnings("ignore", message=r".*JSON_SCHEMA_FOR_FUNC_DECL.*", category=UserWarning)


def _resolve(sandbox: str, path: str) -> str:
    """Turn a sandbox-relative path into an absolute one, strictly blocking escapes."""
    sandbox = os.path.realpath(sandbox)
    full = os.path.realpath(os.path.join(sandbox, path))
    if full != sandbox and not full.startswith(sandbox + os.sep):
        raise ValueError(f"Path escapes sandbox: {path!r}")
    return full


def _read(sandbox: str, path: str) -> str:
    try:
        with open(_resolve(sandbox, path), "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return f"Error: no such file: {path}"
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _write(sandbox: str, path: str, contents: str) -> str:
    try:
        full = _resolve(sandbox, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(contents)
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Wrote {len(contents)} chars to {path}"


def _list_dir(sandbox: str, path: str = ".") -> str:
    try:
        full = _resolve(sandbox, path)
        if not os.path.isdir(full):
            return f"Error: not a directory: {path}"
        entries = []
        for root, dirs, files in os.walk(full):
            rel_root = os.path.relpath(root, sandbox)
            prefix = "" if rel_root == "." else f"{rel_root}/"
            for f in files:
                entries.append(f"{prefix}{f}")
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as exc:
        return f"Error: {exc}"


def _run_shell(sandbox: str, command: str, timeout_seconds: int = 30) -> str:
    """Execute a shell command inside the sandbox root directory."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=sandbox,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_seconds,
        )
        output = result.stdout.strip()
        return f"Exit code: {result.returncode}\nOutput:\n{output if output else '(no output)'}"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout_seconds} seconds."
    except Exception as exc:
        return f"Error executing command: {exc}"


SYSTEM_INSTRUCTION = """You are an autonomous senior software engineer working in a project workspace.
Follow this systematic workflow for every request:

1. DISCOVER: Inspect existing files and structure using list_files and read_file to understand requirements, tests, and conventions.
2. IMPLEMENT: Write clean, modular, and idiomatic code using write_file. Handle all specified edge cases (e.g., input formats, rounding, error exit codes, malformed inputs).
3. VERIFY & TEST: Always run your code using run_command.
   - Test against the prompt's examples.
   - Test edge cases, empty inputs, invalid inputs, and boundary conditions.
   - Run any available test suites or write small verification commands.
4. DEBUG & REFINE: If test commands fail or exit with unexpected codes/outputs, inspect the error, adjust your implementation with write_file, and re-test.
5. CONCLUDE: Only provide your final explanation once you have verified through run_command that your implementation satisfies all requirements.
"""


def build_agent(sandbox: str, model: str) -> Agent:
    def read_file(path: str) -> str:
        """Read a text file (path is relative to the project sandbox root)."""
        return _read(sandbox, path)

    def write_file(path: str, contents: str) -> str:
        """Create or overwrite a text file (path is relative to the project sandbox root)."""
        return _write(sandbox, path, contents)

    def list_files(path: str = ".") -> str:
        """List all files recursively in the project sandbox starting from path."""
        return _list_dir(sandbox, path)

    def run_command(command: str) -> str:
        """Run a shell command (e.g., 'python3 src/foo.py' or pytest) in the sandbox root directory and return exit code and stdout/stderr."""
        return _run_shell(sandbox, command)

    return Agent(
        name="coding_agent",
        model=model,
        instruction=SYSTEM_INSTRUCTION,
        tools=[read_file, write_file, list_files, run_command],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,  # Lower temperature for deterministic, reliable code logic
            thinking_config=types.ThinkingConfig(
                thinking_budget=1024
            ),
        ),
    )


if __name__ == "__main__":
    import asyncio
    import glob
    import subprocess
    import sys
    from google.adk.runners import InMemoryRunner

    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "qwiklabs-gcp-01-08f6860366c1")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

    sandbox = os.path.join(os.path.dirname(os.path.realpath(__file__)), "out")
    os.makedirs(os.path.join(sandbox, "src"), exist_ok=True)
    prompt = " ".join(sys.argv[1:]) or "Write a hello world python script"

    async def _run_agent() -> None:
        runner = InMemoryRunner(agent=build_agent(sandbox, DEFAULT_MODEL))
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="me"
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        async for event in runner.run_async(
            user_id="me", session_id=session.id, new_message=message
        ):
            for part in (event.content and event.content.parts) or []:
                print(part.text or "", end="", flush=True)

    asyncio.run(_run_agent())

    for script in glob.glob(f"{sandbox}/**/*.py", recursive=True):
        print(
            f"\n=== output of {os.path.relpath(script, sandbox)} ===\n"
            + subprocess.run(
                [sys.executable, script],
                cwd=sandbox,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ).stdout,
            end="",
        )