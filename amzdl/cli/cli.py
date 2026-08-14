"""Console UI kit — the single source of truth for the CLI's look. Owns the
palette, tree markers, one-screen-at-a-time bookkeeping, and the bar-aware log
handler that every interactive surface shares."""

import logging
import re
import shutil
import sys

from amzdl._version import VERSION
from amzdl.utils import disp_width


def _enable_windows_ansi() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    enable_vt = 0x0004
    for handle_id in (-11, -12):
        handle = kernel32.GetStdHandle(handle_id)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | enable_vt)


_enable_windows_ansi()

RESET = "\033[0m"
BOLD = "\033[1m"
FAINT = "\033[2m"
GREY = "\033[37m"
CYAN = "\033[34m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"

_TTY = sys.stdout.isatty()


def paint(text: str, *codes: str) -> str:
    if not _TTY or not codes:
        return text
    return f"{''.join(codes)}{text}{RESET}"


def faint(text: str) -> str:
    return paint(text, FAINT, GREY)


BRAND_TEXT = f"amzdl v{VERSION}"

BRAND = paint(BRAND_TEXT, CYAN)
SEP = paint("|", FAINT, GREY, BOLD)

MARK_HEADER = faint("│")
MARK_TEE = paint("├", FAINT, GREY, BOLD)
MARK_CLOSE = paint("╰", FAINT, GREY, BOLD)


def header(*parts: str) -> str:
    segments = [BRAND, *(p for p in parts if p)]
    return f"{MARK_HEADER} " + f" {SEP} ".join(segments)


_pending_rows = 0

_footer_rows = 0
_alerts: list[tuple[int, str]] = []
_displayed_notes: list[str] = []
_pending_notes: list[str] = []

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _term_cols() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _vis_rows(text: str, term_w: int) -> int:
    width = disp_width(_ANSI_RE.sub("", text))
    if term_w <= 0:
        return 1
    return max(1, -(-width // term_w))


def _erase_pending() -> None:
    global _pending_rows, _footer_rows, _displayed_notes, _pending_notes
    total = _pending_rows + _footer_rows
    if _TTY and total:
        sys.stdout.write(f"\033[{total}F\033[J")
        sys.stdout.flush()
    _pending_rows = 0
    _footer_rows = 0
    _displayed_notes, _pending_notes = _pending_notes, []


def _emit(line: str) -> None:
    global _pending_rows
    print(line)
    _pending_rows += _vis_rows(line, _term_cols())


def _footer_lines() -> list[str]:
    lines = []
    for level, text in _alerts:
        if level >= logging.ERROR:
            lines.append(f"{paint('×', RED)} {paint(text, RED)}")
        else:
            lines.append(f"{paint('⚠', YELLOW)} {paint(text, YELLOW)}")
    for text in (*_displayed_notes, *_pending_notes):
        lines.append(f"  {faint(text)}")
    return lines


def _repaint_footer() -> None:
    global _footer_rows
    if not _TTY:
        return
    lines = _footer_lines()
    if _footer_rows:
        sys.stdout.write(f"\033[{_footer_rows}F\033[J")
    else:
        sys.stdout.write("\r\033[J")
    cols = _term_cols()
    rows = 0
    for line in lines:
        print(line)
        rows += _vis_rows(line, cols)
    sys.stdout.flush()
    _footer_rows = rows


def _read(prompt: str, reader=input, echo: bool = True) -> str:
    global _pending_rows, _footer_rows
    cols = _term_cols()
    lines = _footer_lines()
    if not (_TTY and lines):
        result = reader(prompt)
        _pending_rows += _vis_rows(prompt + result if echo else prompt, cols)
        return result

    prompt_rows = max(1, _vis_rows(_ANSI_RE.sub("", prompt), cols))
    n_footer = sum(_vis_rows(line, cols) for line in lines)
    sys.stdout.write("\n" * prompt_rows)
    for line in lines:
        sys.stdout.write(line + "\n")
    sys.stdout.write(f"\033[{prompt_rows + n_footer}A")
    sys.stdout.flush()
    result = reader(prompt)
    sys.stdout.write(f"\033[{n_footer}B\r")
    sys.stdout.flush()
    _pending_rows += _vis_rows(prompt + result if echo else prompt, cols)
    _footer_rows = n_footer
    return result


def consume_pending_screen() -> None:
    _erase_pending()


def adopt_pending_rows(rows: int) -> None:
    global _pending_rows
    _pending_rows = max(0, rows)


_log_handler = None


class _BarAwareHandler(logging.StreamHandler):

    def __init__(self):
        super().__init__()
        self._buffering = False
        self._buffer = []
        self._footer = False

    def emit(self, record):
        if self._buffering:
            self._buffer.append(record)
            return
        self._deliver(record)

    def _deliver(self, record):
        if self._footer:
            _record_alert(record.levelno, record.getMessage())
            _repaint_footer()
        else:
            super().emit(record)

    def begin_bar(self):
        with self.lock:
            self._buffering = True

    def end_bar(self):
        with self.lock:
            self._buffering = False
            buffered, self._buffer = self._buffer, []
            if self._footer:
                for record in buffered:
                    _record_alert(record.levelno, record.getMessage())
                if buffered:
                    _repaint_footer()
            else:
                for record in buffered:
                    super().emit(record)


def setup_logging(verbose: bool) -> None:
    global _log_handler
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)
    if _log_handler is None:
        _log_handler = _BarAwareHandler()
        _log_handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
        root.addHandler(_log_handler)
    _log_handler._footer = _TTY and not verbose


def begin_bar_logging() -> None:
    if _log_handler is not None:
        _log_handler.begin_bar()


def end_bar_logging() -> None:
    if _log_handler is not None:
        _log_handler.end_bar()


def note(text: str) -> None:
    if not _TTY:
        print(faint(text))
        return
    _pending_notes.append(text)
    _repaint_footer()


def _record_alert(level: int, text: str) -> None:
    _alerts.append((level, text))


def warn(text: str) -> None:
    if not _TTY:
        print(paint(text, YELLOW))
        return
    _record_alert(logging.WARNING, text)
    _repaint_footer()


def error(text: str) -> None:
    if not _TTY:
        print(paint(text, RED))
        return
    _record_alert(logging.ERROR, text)
    _repaint_footer()


def print_error(message: str) -> None:
    _erase_pending()
    _emit(header("Error"))
    _emit(f"{MARK_CLOSE} {paint('×', RED)} {message}")
    _repaint_footer()
