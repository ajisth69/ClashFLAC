"""Static CLI screens: account selection, login, region, and search pickers.
Composes the palette and one-screen-at-a-time bookkeeping from `cli` into the
interactive wizard screens."""

from amzdl.cli import cli
from amzdl.cli.cli import MARK_CLOSE, MARK_TEE, RED, YELLOW, faint, header, paint
from amzdl.cli.terminal import read_long_line


def _account_row(name: str, region: str, index: int | None = None,
                 marker: str | None = None) -> str:
    mark = marker or MARK_TEE
    label = (
        f"{faint('[')}{paint(str(index), YELLOW)}{faint(']')} "
        if index is not None
        else ""
    )
    return f"{mark} {label}{name}{faint(' — ')}{region}"


def prompt_region(title: str = "Add account") -> str:
    cli._erase_pending()
    cli._emit(header(title))
    return cli._read(f"{MARK_CLOSE} {faint('Region code (e.g. US): ')}").strip()


def prompt_account(options: list[tuple[str, str]]) -> int | None:
    cli._erase_pending()
    cli._emit(header("Select account"))
    for i, (name, region) in enumerate(options, 1):
        cli._emit(_account_row(name, region, index=i))
    n = len(options)
    prompt = f"{MARK_CLOSE} {faint(f'Select [1-{n}], or A to Add: ')}"
    while True:
        raw = cli._read(prompt).strip()
        if raw.lower() == "a":
            return None
        if raw.isdigit() and 1 <= int(raw) <= n:
            return int(raw) - 1


def prompt_manage_account(options: list[tuple[str, str]]) -> int | str:
    cli._erase_pending()
    cli._emit(header("Manage accounts"))
    for i, (name, region) in enumerate(options, 1):
        cli._emit(_account_row(name, region, index=i))
    n = len(options)
    prompt = (
        f"{MARK_CLOSE} "
        f"{faint(f'Select [1-{n}] to remove, A to add, or Q to quit: ')}"
    )
    while True:
        raw = cli._read(prompt).strip().lower()
        if raw in ("q", ""):
            return "quit"
        if raw == "a":
            return "add"
        if raw.isdigit() and 1 <= int(raw) <= n:
            return int(raw) - 1


def confirm_delete(name: str, region: str) -> bool:
    cli._erase_pending()
    cli._emit(header("Remove account"))
    label = f"{name}{' — '}{region}"
    question = paint(f"Permanently remove {label}? [y/N]: ", RED)
    return cli._read(f"{MARK_CLOSE} {question}").strip().lower() in ("y", "yes")


def prompt_oauth_url(app_title: str, url: str) -> str:
    step1 = faint("1. Open this URL: ")
    step2 = faint("2. After signing in you'll land on a blank / 'page not found' page.")
    step3 = faint(
        "3. Copy that page's FULL URL from the address bar and paste it below."
    )
    cli._erase_pending()
    cli._emit(header(f"Sign-in: {app_title}"))
    cli._emit(f"{MARK_TEE} {step1}{url}")
    cli._emit(f"{MARK_TEE} {step2}")
    cli._emit(f"{MARK_TEE} {step3}")
    prompt = f"{MARK_CLOSE} {faint('Paste the post-login URL and press Enter: ')}"
    return cli._read(prompt, read_long_line, echo=False).strip()


def print_account_summary(title: str, options: list[tuple[str, str]]) -> None:
    cli._erase_pending()
    cli._emit(header(title))
    last = len(options) - 1
    for i, (name, region) in enumerate(options):
        cli._emit(_account_row(name, region, index=i + 1,
                               marker=MARK_CLOSE if i == last else MARK_TEE))


def _search_title(type_label: str | None) -> str:
    return f"Search {type_label}s" if type_label else "Search"


def prompt_search_query(type_label: str | None = None) -> str:
    cli._erase_pending()
    cli._emit(header(_search_title(type_label)))
    return cli._read(f"{MARK_CLOSE} {faint('Enter query: ')}").strip()


def prompt_search_type(types: tuple[str, ...]) -> str:
    cli._erase_pending()
    cli._emit(header("Search"))
    prompt = f"{MARK_CLOSE} {faint('Search type (track, album, etc.): ')}"
    while True:
        raw = cli._read(prompt).strip().lower()
        if raw.endswith("s") and raw[:-1] in types:
            raw = raw[:-1]
        if raw in types:
            return raw


def _search_row(fields: tuple[str, ...], index: int) -> str:
    label = f"{faint('[')}{paint(str(index), YELLOW)}{faint(']')} "
    body = faint(" - ").join(f for f in fields if f)
    return f"{MARK_TEE} {label}{body}"


def prompt_search_results(
    type_label: str, rows: list[tuple[str, ...]]
) -> int | None:
    cli._erase_pending()
    cli._emit(header(_search_title(type_label)))
    for i, fields in enumerate(rows, 1):
        cli._emit(_search_row(fields, i))
    n = len(rows)
    prompt = f"{MARK_CLOSE} {faint(f'Select [1-{n}] to download, or Q to quit: ')}"
    while True:
        raw = cli._read(prompt).strip().lower()
        if raw in ("q", ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= n:
            return int(raw) - 1
