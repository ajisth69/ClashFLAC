"""Raw single-line terminal input for the CLI. `read_long_line` reads Amazon's
very long post-login OAuth URLs raw (ICANON/ECHO off) via a platform-specific
backend, keeping the prompt a single non-scrolling line."""

import sys

try:
    import termios
except ImportError:
    termios = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


def read_long_line(prompt: str = "") -> str:
    if not sys.stdin.isatty():
        return input(prompt)
    if termios is not None:
        return _read_long_line_posix(prompt)
    if msvcrt is not None:
        return _read_long_line_windows(prompt)
    return input(prompt)


def _show_paste_count(prompt: str, n: int) -> None:
    sys.stdout.write(f"\r\033[K{prompt}[{n} chars]")
    sys.stdout.flush()


def _read_long_line_posix(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, new)
        chars = []
        while True:
            c = sys.stdin.read(1)
            if c in ("\n", "\r"):
                break
            chars.append(c)
            _show_paste_count(prompt, len(chars))
        return "".join(chars)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)
        sys.stdout.write("\n")
        sys.stdout.flush()


def _read_long_line_windows(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    chars = []
    while True:
        c = msvcrt.getwch()
        if c in ("\r", "\n"):
            break
        if c == "\x03":
            raise KeyboardInterrupt
        if c in ("\x00", "\xe0"):
            msvcrt.getwch()
            continue
        if c == "\b":
            if chars:
                chars.pop()
        else:
            chars.append(c)
        _show_paste_count(prompt, len(chars))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(chars)
