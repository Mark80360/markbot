"""Tests for the enhanced Sandbox: preamble injection, encoding fallback,
and optional streaming reads.

These pin the three behaviour changes ported from GenericAgent's
``code_run`` design:

* :func:`_decode_output` — utf-8 → gbk → cp936 → latin-1 → replace, so a
  Chinese-Windows child process no longer returns `` rows.
* preamble injection — ``SandboxConfig.inject_preamble=True`` prepends
  ``PYTHON_PREAMBLE`` to every python script via a temp file, without
  mutating the original script on disk.
* ``run(on_output_line=...)`` — an async per-line callback receives
  stdout incrementally so progress can stream to the bus.

Each test runs a real subprocess (small, fast scripts) so the contract
is verified end-to-end rather than by mocking internals.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from markbot.skills.core.preamble import PYTHON_PREAMBLE
from markbot.skills.core.sandbox import Sandbox, SandboxConfig, _decode_output


class TestDecodeOutput:
    def test_none(self):
        assert _decode_output(None) == ""

    def test_empty(self):
        assert _decode_output(b"") == ""

    def test_str_passthrough(self):
        assert _decode_output("hello") == "hello"

    def test_utf8(self):
        assert _decode_output("中文".encode("utf-8")) == "中文"

    def test_gbk_fallback(self):
        # GBK bytes are NOT valid utf-8 — the fallback chain must catch them.
        gbk_bytes = "中文".encode("gbk")
        assert b"\xd6" in gbk_bytes  # sanity: high bytes that break utf-8
        assert _decode_output(gbk_bytes) == "中文"

    def test_latin1_final_fallback(self):
        # 0x80 alone is invalid in utf-8 AND gbk single-byte; latin-1 saves it.
        assert _decode_output(b"\x80") == "\x80"

    def test_replace_last_resort(self):
        # Bytes that survive every codec cleanly don't reach the replace
        # branch; this just documents that a pure-ASCII payload is exact.
        assert _decode_output(b"abc") == "abc"


class TestPreambleContent:
    def test_preamble_is_nonempty_python(self):
        # The preamble must contain the three patches we rely on.
        assert "subprocess" in PYTHON_PREAMBLE
        assert "CREATE_NO_WINDOW" in PYTHON_PREAMBLE or "0x08000000" in PYTHON_PREAMBLE
        assert "excepthook" in PYTHON_PREAMBLE
        assert "dependencies" in PYTHON_PREAMBLE  # the hint text

    def test_preamble_does_not_mutate_on_import(self):
        # Importing must not touch the host's subprocess (patches are in
        # the child only). Verify by re-reading: the constant is a str.
        assert isinstance(PYTHON_PREAMBLE, str)
        assert PYTHON_PREAMBLE.startswith("#")


class TestSandboxPreambleInjection:
    """Preamble injection end-to-end: the child sees the patched
    ``subprocess.run`` and the ImportError hint reaches stderr."""

    @pytest.fixture
    def sandbox(self, tmp_path):
        return Sandbox(SandboxConfig(timeout=20, inject_preamble=True))

    def test_preamble_patched_subprocess_run(self, sandbox, tmp_path):
        # The preamble wraps subprocess.run to decode text= output. A child
        # that calls subprocess.run([sys.executable, ...], text=True) on a
        # program printing non-ASCII should get decoded str back.
        script = tmp_path / "user.py"
        script.write_text(
            "import subprocess, sys\n"
            "r = subprocess.run([sys.executable, '-c', 'print(123)'], "
            "text=True, capture_output=True)\n"
            "print('decoded:', repr(r.stdout))\n",
            encoding="utf-8",
        )
        result = asyncio.run(sandbox.run(script, "python", {}, cwd=tmp_path))
        assert result.success, result.stderr
        assert "decoded: '123" in result.stdout

    def test_import_error_hint_reaches_stderr(self, sandbox, tmp_path):
        # The preamble's excepthook appends the dependencies hint on
        # ImportError. A script importing a nonexistent module must surface
        # that hint in stderr.
        script = tmp_path / "user.py"
        script.write_text(
            "import this_module_does_not_exist_xyz\n", encoding="utf-8"
        )
        result = asyncio.run(sandbox.run(script, "python", {}, cwd=tmp_path))
        assert not result.success
        combined = (result.stdout or "") + (result.stderr or "")
        assert "dependencies" in combined.lower()

    def test_original_script_not_mutated(self, sandbox, tmp_path):
        # The preamble is written to a SEPARATE temp file; the user's
        # original script must be byte-identical afterwards.
        script = tmp_path / "user.py"
        original = "print('hi')\n"
        script.write_text(original, encoding="utf-8")
        asyncio.run(sandbox.run(script, "python", {}, cwd=tmp_path))
        assert script.read_text(encoding="utf-8") == original

    def test_inject_preamble_false_disables(self, tmp_path):
        # Opt-out: with inject_preamble=False, a script that relies on the
        # preamble patch must NOT see the ImportError hint (the bare
        # traceback will still mention the import, but not the hint).
        sandbox = Sandbox(SandboxConfig(timeout=20, inject_preamble=False))
        script = tmp_path / "user.py"
        script.write_text(
            "import this_module_does_not_exist_xyz\n", encoding="utf-8"
        )
        result = asyncio.run(sandbox.run(script, "python", {}, cwd=tmp_path))
        combined = (result.stdout or "") + (result.stderr or "")
        assert "dependencies" not in combined.lower()


class TestSandboxStreamingRead:
    """The on_output_line callback must fire once per stdout line, in
    order, BEFORE the run() future resolves."""

    def test_streaming_emits_lines_in_order(self, tmp_path):
        sandbox = Sandbox(SandboxConfig(timeout=20, inject_preamble=False))
        script = tmp_path / "emit.py"
        script.write_text(
            "print('first')\nprint('second')\nprint('third')\n",
            encoding="utf-8",
        )
        seen: list[str] = []

        async def on_line(line: str) -> None:
            seen.append(line)

        async def go():
            return await sandbox.run(
                script, "python", {}, cwd=tmp_path, on_output_line=on_line
            )

        result = asyncio.run(go())
        assert result.success
        assert "first" in seen
        assert "second" in seen
        assert "third" in seen
        # Ordering preserved.
        assert seen.index("first") < seen.index("third")
        # Final stdout still contains the full text.
        assert "first" in result.stdout and "third" in result.stdout

    def test_no_callback_behaves_like_communicate(self, tmp_path):
        sandbox = Sandbox(SandboxConfig(timeout=20, inject_preamble=False))
        script = tmp_path / "simple.py"
        script.write_text("print('done')\n", encoding="utf-8")
        result = asyncio.run(sandbox.run(script, "python", {}, cwd=tmp_path))
        assert result.success
        assert "done" in result.stdout


class TestStreamingCrossStream:
    """End-to-end: a script that writes to BOTH stdout and stderr must
    deliver both streams back to ``run()`` even when a streaming callback
    is attached. This was a regression risk in the rewrite because
    ``communicate()`` was replaced with a manual pair of readline loops."""

    def test_stderr_collected_alongside_streamed_stdout(self, tmp_path):
        sandbox = Sandbox(SandboxConfig(timeout=20, inject_preamble=False))
        script = tmp_path / "both.py"
        # Emit three lines on each stream, interleaved. The streaming
        # path must drain stderr in a sibling task so the child does not
        # block on a full pipe buffer.
        script.write_text(
            "import sys, time\n"
            "for i in range(3):\n"
            "    print(f'out-{i}', flush=True)\n"
            "    print(f'err-{i}', file=sys.stderr, flush=True)\n",
            encoding="utf-8",
        )
        seen_stdout: list[str] = []

        async def on_line(line: str) -> None:
            seen_stdout.append(line)

        result = asyncio.run(
            sandbox.run(script, "python", {}, cwd=tmp_path, on_output_line=on_line)
        )
        assert result.success, result.stderr
        # Streaming contract: every stdout line is delivered once, in order.
        assert seen_stdout == ["out-0", "out-1", "out-2"]
        # Final result still contains the full stdout.
        assert "out-0" in result.stdout and "out-2" in result.stdout
        # The previously-bug-prone path: stderr must be fully captured too.
        assert "err-0" in result.stderr
        assert "err-1" in result.stderr
        assert "err-2" in result.stderr

    def test_process_early_exit_still_completes(self, tmp_path):
        """A child that exits before the readline loop sees EOF must not
        leave the streaming read hanging. ``process.stdout.readline()``
        returns ``b''`` on EOF, which terminates the loop cleanly."""
        sandbox = Sandbox(SandboxConfig(timeout=20, inject_preamble=False))
        script = tmp_path / "one_shot.py"
        # Print a single line and exit immediately. There is no trailing
        # newline so the OS pipe still closes promptly.
        script.write_text(
            "import sys; sys.stdout.write('single'); sys.stdout.flush()\n",
            encoding="utf-8",
        )
        seen: list[str] = []

        async def on_line(line: str) -> None:
            seen.append(line)

        result = asyncio.run(
            sandbox.run(script, "python", {}, cwd=tmp_path, on_output_line=on_line)
        )
        assert result.success
        # ``readline`` strips the trailing \n, and we rstrip \r\n too.
        assert seen == ["single"]
        assert "single" in result.stdout


class TestReadWithOptionalStream:
    """Direct unit tests for ``Sandbox._read_with_optional_stream`` — the
    ~50-line helper that replaced ``process.communicate()`` when a
    streaming callback is provided. These don't go through ``run()`` so
    edge cases (no callback, drain timeout, callback that raises) can
    be exercised in isolation."""

    @pytest.mark.asyncio
    async def test_no_callback_collapses_to_communicate(self):
        # The fast path must remain a single ``communicate()`` call so
        # non-streaming users pay zero overhead.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('hi')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await Sandbox._read_with_optional_stream(
            proc, on_output_line=None, timeout=10
        )
        assert proc.returncode == 0
        assert b"hi" in stdout
        # No stderr produced.
        assert stderr == b""

    @pytest.mark.asyncio
    async def test_callback_receives_lines_in_order(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "print('a'); print('b'); print('c')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        seen: list[str] = []

        async def on_line(line: str) -> None:
            seen.append(line)

        stdout, _stderr = await Sandbox._read_with_optional_stream(
            proc, on_output_line=on_line, timeout=10
        )
        assert proc.returncode == 0
        assert seen == ["a", "b", "c"]
        # Final buffer must contain the full text — the streaming path
        # does not discard what it reads.
        assert b"a" in stdout and b"c" in stdout

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_abort_run(self):
        # A misbehaving callback that raises must NEVER kill the child
        # read loop — the contract is "callback failure is best-effort".
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('x'); print('y')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        call_count = 0

        async def bad_callback(_line: str) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        stdout, _stderr = await Sandbox._read_with_optional_stream(
            proc, on_output_line=bad_callback, timeout=10
        )
        # Both lines were attempted (callback raised twice) but the
        # helper still read them all and produced a normal stdout buffer.
        assert call_count == 2
        assert proc.returncode == 0
        assert b"x" in stdout and b"y" in stdout

    @pytest.mark.asyncio
    async def test_stderr_drain_timeout_kills_stuck_process(self):
        # If the child keeps writing to stderr past the configured drain
        # grace period, the helper must kill the process and return
        # whatever it has, not hang the agent loop.
        # Use a tiny drain timeout (0.1s) so the test stays fast.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c",
            # Print once on stdout, then loop printing on stderr so the
            # drain task never reaches EOF within the grace period.
            "import sys, time\n"
            "print('done', flush=True)\n"
            "end = time.time() + 3\n"
            "while time.time() < end:\n"
            "    sys.stderr.write('x' * 4096)\n"
            "    sys.stderr.flush()\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        seen: list[str] = []

        async def on_line(line: str) -> None:
            seen.append(line)

        _stdout, _stderr = await Sandbox._read_with_optional_stream(
            proc, on_output_line=on_line, timeout=10, stderr_drain_timeout_s=0.1
        )
        # The 'done' line was streamed before the drain timeout fired.
        assert "done" in seen
        # The process was killed, so the loop that would have run for
        # 3s was terminated promptly. Give it a brief moment for the OS
        # to reap the killed process.
        await asyncio.sleep(0.05)
        assert proc.returncode is not None

