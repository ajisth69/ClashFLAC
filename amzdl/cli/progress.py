import shutil
import sys
import threading
import time

from amzdl.cli import cli
from amzdl.cli.cli import FAINT as _FAINT
from amzdl.cli.cli import GREEN as _GREEN
from amzdl.cli.cli import GREY as _GREY
from amzdl.cli.cli import YELLOW as _YELLOW
from amzdl.cli.cli import paint as _paint
from amzdl.utils import (
    disp_width as _disp_width,
)
from amzdl.utils import (
    fixed as _fixed,
)
from amzdl.utils import (
    take_cols as _take_cols,
)
from amzdl.utils import (
    truncate as _truncate,
)

_FILL = "█"
_BRAND_TEXT = cli.BRAND_TEXT

_HEADER_MARK = "│"
_AGG_MARK = "├"
_SLOT_MARK = "├─"
_SLOT_MARK_LAST = "╰─"
_BAR_MARK = "╰"

_MARK_HEADER = cli.MARK_HEADER
_MARK_AGG = cli.MARK_TEE
_MARK_SLOT = _paint(_SLOT_MARK, _FAINT, _GREY)
_MARK_SLOT_LAST = _paint(_SLOT_MARK_LAST, _FAINT, _GREY)
_MARK_BAR = cli.MARK_CLOSE
_SEP = cli.SEP
_BRAND = cli.BRAND
_DONE = _paint("done", _GREEN)

_WIDTH_FRAC = .8

_SINGLE_STEPS = 5
_TRACK_STEPS = 5

_DESC_MAX = 20
_SLOT_NAME_W = 7
_SLOT_DESC_W = 18

_MARQUEE_CPS = 6
_TICK_INTERVAL = 0.12


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _bar(width: int, frac: float) -> str:
    filled = int(width * frac)
    return _paint(_FILL * filled + " " * (width - filled), _FAINT)


class _Slot:
    __slots__ = ("name", "total", "n", "desc", "done")

    def __init__(self, name: str, total: int):
        self.name = name
        self.total = max(1, total)
        self.n = 0
        self.desc = ""
        self.done = False


class Progress:
    def __init__(self, asin: str, plain: bool = False, steps: int = _SINGLE_STEPS):
        self.asin = asin
        self.name = None
        self.desc = ""
        self.done = False
        self.start = time.time()

        self.steps_total = max(1, steps)
        self.n = 0

        self._album = False
        self._slots: list = []
        self.completed = 0
        self.track_total = 0
        self.album_start = None
        self._agg_reserve = 0
        self._rate_label = "tracks/s"

        self._tty = sys.stdout.isatty() and not plain
        self._rendered = False
        self._rendered_lines = 0
        self._lock = threading.Lock()
        self._stop = False
        self._ticker = None
        if self._tty:
            cli.begin_bar_logging()
            self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
            self._ticker.start()

    def set_name(self, name: str):
        self.name = name
        self.render()

    def set_desc(self, desc: str):
        self.desc = desc
        self.render()

    def update(self, desc: str = None, advance: int = 1):
        self.n += advance
        if desc is not None:
            self.desc = desc
        self.render()

    def begin_custom(self, total: int, rate_label: str = "tracks/s"):
        with self._lock:
            self._album = True
            self._rate_label = rate_label
            self.track_total = max(1, total)
            self.completed = 0
            self.album_start = time.time()
            self._slots = []
            cw = len(str(self.track_total))
            sample = (
                f"{self.track_total:>{cw}}/{self.track_total}"
                f"  [00:00<00:00,  0.0 {rate_label}]"
            )
            self._agg_reserve = _disp_width(sample)
        self.render()

    def advance_aggregate(self, n: int = 1):
        with self._lock:
            self.completed += n
        self.render()

    def track_start(self, name: str, steps: int = _TRACK_STEPS) -> _Slot:
        slot = _Slot(name, steps)
        with self._lock:
            self.desc = name
            for i, s in enumerate(self._slots):
                if s is None or s.done:
                    self._slots[i] = slot
                    break
            else:
                self._slots.append(slot)
        self.render()
        return slot

    def track_step(self, slot: _Slot, desc: str):
        with self._lock:
            slot.n += 1
            slot.desc = desc
            self.desc = desc
        self.render()

    def track_done(self, slot: _Slot):
        with self._lock:
            self.completed += 1
            slot.done = True
            slot.n = slot.total
        self.render()

    def finish(self, desc: str = None):
        self.done = True
        self.n = self.steps_total
        if self._album:
            self.completed = self.track_total
        if desc is not None:
            self.desc = desc
        self._shutdown_ticker()
        self.render()
        cli.end_bar_logging()

    def abort(self):
        self._shutdown_ticker()
        if self._tty and self._rendered:
            cli.adopt_pending_rows(self._rendered_lines)
            cli.consume_pending_screen()
            self._rendered = False
        cli.end_bar_logging()

    def _tick_loop(self):
        while not self._stop:
            time.sleep(_TICK_INTERVAL)
            if not self._stop:
                self.render()

    def _shutdown_ticker(self):
        self._stop = True
        if self._ticker is not None and self._ticker is not threading.current_thread():
            self._ticker.join(timeout=_TICK_INTERVAL * 2)

    @staticmethod
    def _term_width() -> int:
        cols = shutil.get_terminal_size((100, 24)).columns
        return max(40, int(cols * _WIDTH_FRAC))

    def _marquee(self, text: str, width: int) -> str:
        if width <= 0 or not text:
            return ""
        if _disp_width(text) <= width:
            return text
        cycle = text + "   "
        pos = int((time.time() - self.start) * _MARQUEE_CPS) % len(cycle)
        rolled = cycle[pos:] + cycle[:pos]
        while _disp_width(rolled) < width:
            rolled += cycle
        return _take_cols(rolled, width)

    def _title(self, term_w: int) -> str:
        if not self.name:
            return ""
        prefix_w = 2 + _disp_width(_BRAND_TEXT) + 3 + _disp_width(self.asin) + 3
        avail = term_w - prefix_w
        if self.done:
            return _truncate(self.name, avail)
        return self._marquee(self.name, avail)

    def _header(self, term_w: int) -> str:
        parts = [_BRAND, _paint(self.asin, _YELLOW)]
        title = self._title(term_w)
        if title:
            parts.append(title)
        return f"{_MARK_HEADER} " + f" {_SEP} ".join(parts)

    def _single_line(self, term_w: int) -> str:
        frac = 1.0 if self.done else min(1.0, self.n / self.steps_total)
        pct = f"{int(frac * 100):>3}%"
        overhead = _disp_width(_BAR_MARK) + 1 + len(pct) + 1 + 1 + _DESC_MAX
        bar_w = max(10, term_w - overhead)
        suffix = _DONE if self.done else _paint(_truncate(self.desc, _DESC_MAX), _FAINT)
        return f"{_MARK_BAR} {pct} {_bar(bar_w, frac)} {suffix}"

    def _agg_frac(self) -> float:
        if self.track_total == 0:
            return 0.0
        active = sum(s.n / s.total for s in self._slots if s)
        return min(1.0, (self.completed + active) / self.track_total)

    def _agg_line(self, term_w: int, last: bool = False) -> str:
        mark = _MARK_BAR if last else _MARK_AGG
        frac = 1.0 if self.done else self._agg_frac()
        pct = f"{int(frac * 100):>3}%"
        if self.done:
            suffix = _DONE
        else:
            cw = len(str(self.track_total))
            count = f"{self.completed:>{cw}}/{self.track_total}"
            elapsed = time.time() - (self.album_start or self.start)
            rate = self.completed / elapsed if elapsed > 0 else 0.0
            eta = (self.track_total - self.completed) / rate if rate > 0 else 0.0
            suffix = _paint(
                f"{count}  [{_fmt_time(elapsed)}<{_fmt_time(eta)}, "
                f"{rate:4.1f} {self._rate_label}]",
                _FAINT,
            )
        overhead = _disp_width(_AGG_MARK) + 1 + len(pct) + 1 + 1 + self._agg_reserve
        bar_w = max(10, term_w - overhead)
        return f"{mark} {pct} {_bar(bar_w, frac)} {suffix}"

    def _slot_line(self, term_w: int, slot: _Slot, last: bool) -> str:
        mark = _MARK_SLOT_LAST if last else _MARK_SLOT
        name = _paint(_fixed(slot.name, _SLOT_NAME_W), _FAINT)
        if slot.done:
            frac = 1.0
            suffix = _DONE
        else:
            frac = min(1.0, slot.n / slot.total)
            suffix = _truncate(slot.desc, _SLOT_DESC_W)
        pct = f"{int(frac * 100):>3}%"
        overhead = 2 + 1 + _SLOT_NAME_W + 1 + len(pct) + 1 + 1 + _SLOT_DESC_W
        bar_w = max(8, term_w - overhead)
        return f"{mark} {name} {pct} {_bar(bar_w, frac)} {suffix}"

    def _lines(self, term_w: int) -> list:
        lines = [self._header(term_w)]
        if self._album:
            active = [s for s in self._slots if s and not s.done]
            lines.append(self._agg_line(term_w, last=not active))
            for i, slot in enumerate(active):
                lines.append(self._slot_line(term_w, slot, last=(i == len(active) - 1)))
        else:
            lines.append(self._single_line(term_w))
        return lines

    def _render_plain(self):
        try:
            if self.done:
                print(f"{_HEADER_MARK} done | {self.name or self.asin}")
            elif self._album:
                pct = int(self._agg_frac() * 100)
                print(
                    f"{_HEADER_MARK} {pct:>3}% | "
                    f"{self.completed}/{self.track_total} | {self.desc}"
                )
            else:
                pct = int(min(1.0, self.n / self.steps_total) * 100)
                print(f"{_HEADER_MARK} {pct:>3}% | {_truncate(self.desc, _DESC_MAX)}")
        except Exception:
            pass

    def render(self):
        if not self._tty:
            self._render_plain()
            return
        term_w = self._term_width()
        with self._lock:
            if self._stop and not self.done:
                return
            lines = self._lines(term_w)
            if self._rendered:
                sys.stdout.write(f"\033[{self._rendered_lines}F\033[J")
            else:
                cli.consume_pending_screen()
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            self._rendered_lines = len(lines)
            self._rendered = True
