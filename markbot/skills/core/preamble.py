"""Python preamble injected before user-supplied scripts in the sandbox.

Inspired by GenericAgent's ``code_run_header.py``. The preamble is
prepended to every user script executed via :class:`Sandbox` (when
``SandboxConfig.inject_preamble`` is True) so that **inside the child
process** — and only there — three real-world pain points on
non-UTF-8 / Windows environments are patched without polluting the
host interpreter:

1. ``subprocess`` encoding — wraps ``subprocess.run`` and
   ``Popen.__init__`` so that code the agent writes (``subprocess.run(
   [...], text=True)``) decodes child output with a utf-8 → gbk →
   replace fallback, and so every grandchild process on Windows is
   created with ``CREATE_NO_WINDOW`` (no console popups).

2. ``ImportError`` guidance — installs an ``excepthook`` that, on
   ``ImportError`` / ``AttributeError`` (typically a missing third-party
   package), appends a behavioural hint to stderr telling the model to
   declare the package via the ``dependencies`` parameter on the next
   ``run_code`` call. This closes the loop at runtime instead of
   letting the model guess from a raw traceback.

The preamble runs in the child process and monkey-patches only that
process's globals, so the host (markbot itself) is unaffected. It is
trusted markbot code, so it is intentionally not run through the
:class:`SecurityScanner` — scanning inspects the *user* script, not
this boilerplate.

The text is kept as a plain ``str`` constant so it can be concatenated
with user code and written to the temp script file. It is designed to
be defensive: every patch is wrapped in ``try/except`` so a partial
failure (e.g. a frozen stdlib on some builds) never breaks the user
script itself.
"""

from __future__ import annotations

# Executed at the top of every sandboxed python script. Keep lines short
# and side-effect-tolerant. Uses a leading newline + comment header so
# tracebacks stay readable. The ``__name__ == "__main__"`` guard is NOT
# needed here because this file is concatenated *before* user code in a
# script run as ``__main__``; the patches just need to execute on import.
PYTHON_PREAMBLE = r'''# --- markbot sandbox preamble (auto-injected; do not edit) ---
import sys as _mb_sys
import subprocess as _mb_sp

# 1. Decoding helper: utf-8 first, then locale fallbacks, then replace.
def _mb_decode(b):
    if not b:
        return ""
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")

# 2. Wrap subprocess.run so text= output is decoded robustly.
_mb_orig_run = _mb_sp.run
def _mb_run(*a, **k):
    want_text = bool(k.pop("text", False) or k.pop("universal_newlines", False))
    enc = k.pop("encoding", None)
    k.pop("errors", None)
    if enc:
        want_text = True
    if want_text and isinstance(k.get("input"), str):
        k["input"] = k["input"].encode("utf-8", "utf-8", "ignore")
    r = _mb_orig_run(*a, **k)
    if want_text:
        if r.stdout is not None:
            r.stdout = _mb_decode(r.stdout)
        if r.stderr is not None:
            r.stderr = _mb_decode(r.stderr)
    return r
_mb_sp.run = _mb_run

# 3. Wrap Popen.__init__ so grandchildren on Windows get CREATE_NO_WINDOW.
_mb_orig_pinit = _mb_sp.Popen.__init__
def _mb_pinit(self, *a, **k):
    try:
        if _mb_sys.platform.startswith("win"):
            k["creationflags"] = (k.get("creationflags") or 0) | 0x08000000
    except Exception:
        pass
    return _mb_orig_pinit(self, *a, **k)
_mb_sp.Popen.__init__ = _mb_pinit

# 4. Excepthook: hint at missing packages instead of leaving a bare traceback.
_mb_orig_excepthook = _mb_sys.excepthook
def _mb_excepthook(etype, value, tb):
    try:
        _mb_orig_excepthook(etype, value, tb)
        if issubclass(etype, (ImportError, AttributeError, ModuleNotFoundError)):
            _mb_sys.stderr.write(
                "\n[markbot hint] This looks like a missing package. "
                "On your next run_code call, declare it via the "
                "`dependencies` parameter, e.g. dependencies=[\"requests\"].\n"
            )
    except Exception:
        pass
_mb_sys.excepthook = _mb_excepthook

# --- end preamble ---
'''


__all__ = ["PYTHON_PREAMBLE"]
