"""Lyrics parsing for the music-xray-service response. Parses
`get_track_lyrics()` into synced lines, falling back to any plain-text field for
regions/tracks that return an unsynced-only payload."""

from dataclasses import dataclass


@dataclass
class LyricsLine:
    timestamp_ms: int | None
    text: str


@dataclass
class Lyrics:
    lines: list[LyricsLine]

    @staticmethod
    def from_xray(resp: dict) -> "Lyrics":
        payload = resp.get("lyrics", {}) if isinstance(resp, dict) else {}
        raw_lines = payload.get("lines", [])
        if isinstance(raw_lines, dict):
            raw_lines = list(raw_lines.values())

        parsed: list[LyricsLine] = []
        for line in raw_lines if isinstance(raw_lines, list) else []:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            start = line.get("startTime")
            try:
                ts = int(start) if start is not None else None
            except (TypeError, ValueError):
                ts = None
            parsed.append(LyricsLine(timestamp_ms=ts, text=text))

        if not parsed:
            plain = (
                payload.get("text")
                or payload.get("plainText")
                or payload.get("displayText")
                or (resp.get("lyricsText") if isinstance(resp, dict) else None)
            )
            if plain:
                for line in str(plain).strip().splitlines():
                    if line.strip():
                        parsed.append(LyricsLine(timestamp_ms=None, text=line.strip()))

        return Lyrics(lines=parsed)

    @staticmethod
    def _ms_to_lrc_timestamp(ms: int) -> str:
        total_seconds = ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        hundredths = (ms % 1000) // 10
        return f"{minutes:02}:{seconds:02}.{hundredths:02}"

    def _has_synced(self) -> bool:
        return any(line.timestamp_ms is not None for line in self.lines)

    def to_lrc(self) -> str:
        if not self._has_synced():
            return ""
        out = []
        for line in self.lines:
            if line.timestamp_ms is None:
                continue
            out.append(f"[{self._ms_to_lrc_timestamp(line.timestamp_ms)}]{line.text}")
        return "\n".join(out)

    def save_lrc(self, path) -> str:
        content = self.to_lrc()
        if not content:
            return str(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return str(path)

    def to_mp4_lyrics(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def has_content(self) -> bool:
        return len(self.lines) > 0
