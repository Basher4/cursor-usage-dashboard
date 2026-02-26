#!/usr/bin/env python3
"""Pretty TUI dashboard for Cursor usage — powered by Textual."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Label, Static

from cursor_usage import (
    fetch_dashboard_data,
    format_money,
    format_tokens,
)


# ---------------------------------------------------------------------------
# Model colour mapping
# ---------------------------------------------------------------------------
# Each model family gets a distinct Rich colour.  Individual models within a
# family get a slight shade variation so they're visually distinguishable but
# clearly related.
#
# Colour names are Rich markup colour names / hex colours.

# Family -> list of colour hex values (base, shade1, shade2, ...)
# We pick shades that are close within a family but clearly different across
# families.

_FAMILY_PALETTES: Dict[str, List[str]] = {
    "claude":   ["#c4a0f5", "#d4b8fa", "#a87be0", "#e0ccff", "#b48ee8", "#c090f0", "#d8c0fc"],
    "gpt":      ["#74d4a0", "#5cc88c", "#8fe0b4", "#4dbc78", "#a2eac6", "#68d094", "#7cd8a8"],
    "o1":       ["#54c494", "#42b882", "#66d0a6", "#38ac76", "#78dcb8", "#4ec08e", "#5ccca0"],
    "o3":       ["#3eb488", "#2ea878", "#50c098", "#24a06c", "#62ccaa", "#38b080", "#4abc92"],
    "o4":       ["#2ca07c", "#1e946e", "#3eac8c", "#148862", "#50b89e", "#28a078", "#36a884"],
    "gemini":   ["#6eb8e6", "#5aace0", "#82c4ec", "#4aa0d8", "#96d0f2", "#62b4e2", "#76c0e8"],
    "deepseek": ["#e8a86c", "#e09858", "#f0b880", "#d88844", "#f4c894", "#e4a064", "#eca470"],
    "llama":    ["#e07878", "#d46464", "#ec8c8c", "#c85050", "#f4a0a0", "#dc7070", "#e88484"],
    "mistral":  ["#e0c060", "#d4b44c", "#eccc74", "#c8a838", "#f4d888", "#d8b854", "#e4c468"],
    "grok":     ["#e06080", "#d44c6c", "#ec7494", "#c83858", "#f488a8", "#dc5878", "#e86c8c"],
    "codestral": ["#c0a050", "#b49440", "#ccac60", "#a88830", "#d8b870", "#bc9c4c", "#c8a85c"],
    "cursor":   ["#80b0d0", "#70a4c8", "#90bcd8", "#6098c0", "#a0c8e0", "#78acc8", "#88b4d4"],
}

# Fallback palette for unknown families
_FALLBACK_PALETTE: List[str] = ["#b0b0b0", "#a0a0a0", "#c0c0c0", "#909090", "#d0d0d0", "#a8a8a8", "#b8b8b8"]


def _detect_family(model_name: str) -> str:
    """Map a model name to its family key."""
    lower = model_name.lower()

    # Order matters: check more specific prefixes before generic ones.
    # "o1" / "o3" / "o4" must be checked before "gpt" since they're OpenAI
    # but visually distinct.
    if "claude" in lower:
        return "claude"
    if "deepseek" in lower:
        return "deepseek"
    if "gemini" in lower:
        return "gemini"
    if "llama" in lower:
        return "llama"
    if "mistral" in lower:
        return "mistral"
    if "codestral" in lower:
        return "codestral"
    if "grok" in lower:
        return "grok"
    if "cursor" in lower:
        return "cursor"

    # OpenAI reasoning models
    if lower.startswith("o4") or "/o4" in lower:
        return "o4"
    if lower.startswith("o3") or "/o3" in lower:
        return "o3"
    if lower.startswith("o1") or "/o1" in lower:
        return "o1"

    # Generic GPT
    if "gpt" in lower:
        return "gpt"

    return "unknown"


def _model_shade_index(model_name: str, palette_size: int) -> int:
    """Deterministic index into a palette based on the full model name.

    Uses a simple FNV-1a-style hash for better distribution on short strings
    that differ only slightly (e.g. 'gpt-4o' vs 'gpt-4o-mini').
    """
    h = 0x811C9DC5
    for ch in model_name.encode():
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h % palette_size


def model_color(model_name: str) -> str:
    """Return a Rich-compatible hex colour for the given model name."""
    family = _detect_family(model_name)
    palette = _FAMILY_PALETTES.get(family, _FALLBACK_PALETTE)
    idx = _model_shade_index(model_name, len(palette))
    return palette[idx]


def colored_model(model_name: str) -> Text:
    """Return a Rich Text object with the model name coloured by family."""
    color = model_color(model_name)
    return Text(model_name, style=color)


def event_model_cell(model_name: str, is_token_based_call: bool) -> Text:
    """Return model text, optionally with an M badge for token-based calls."""
    cell = colored_model(model_name)
    if is_token_based_call:
        cell.append(" ")
        cell.append(" M ", style="bold black on #f2cd6f")
    return cell


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")


# Width threshold: if the events-box container is at least this wide, we
# show the extra token breakdown columns.
_TOKEN_COLS_MIN_WIDTH = 140


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class SummaryPanel(Static):
    """Top-level billing / cost summary."""

    def compose(self) -> ComposeResult:
        yield Label("Loading...", id="summary-content")

    def update_data(self, data: Dict[str, Any]) -> None:
        billing_requests = data.get("billing_requests")
        billing_total = data.get("billing_total")
        billing_start = data.get("billing_start")
        on_demand_usage = data.get("on_demand_usage")
        stats = data.get("stats", {})

        lines: list[str] = []

        if billing_requests is not None or billing_total is not None:
            period_label = "Billing period"
            if billing_start:
                try:
                    start_dt = datetime.fromisoformat(
                        billing_start.replace("Z", "+00:00")
                    )
                    period_label = f"Since {start_dt.date().isoformat()}"
                except ValueError:
                    period_label = f"Since {billing_start}"
            reqs = billing_requests if billing_requests is not None else "n/a"
            lines.append(
                f"[bold]{period_label}[/]  "
                f"[cyan]{reqs}[/] reqs  —  "
                f"[green]{format_money(billing_total)}[/]"
            )

        lines.append(
            f"[bold]On-demand[/]  [green]{format_money(on_demand_usage)}[/]"
        )
        lines.append(
            f"[bold]This month[/]  "
            f"[cyan]{stats.get('requests', 0)}[/] reqs  —  "
            f"[green]{format_money(stats.get('total_cost'))}[/] total  —  "
            f"[yellow]{format_money(stats.get('median_cost'))}[/] median"
        )

        self.query_one("#summary-content", Label).update("\n".join(lines))


class StatusBar(Static):
    """Bottom status showing last refresh time and interval."""

    refresh_interval: reactive[int] = reactive(60)
    last_refresh: reactive[str] = reactive("")
    is_loading: reactive[bool] = reactive(False)

    def render(self) -> str:
        status = "refreshing..." if self.is_loading else "idle"
        return (
            f" Last refresh: {self.last_refresh or '—'}  |  "
            f"Interval: {self.refresh_interval}s  |  "
            f"Status: {status}  |  "
            f"[+/-] change interval  [r] refresh  [q] quit"
        )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class CursorUsageTUI(App):
    """Cursor Usage TUI Dashboard."""

    CSS = """
    Screen {
        background: $surface;
    }

    #main-scroll {
        height: 1fr;
    }

    #summary-panel {
        border: round $accent;
        padding: 1 2;
        margin: 0 1 1 1;
        height: auto;
    }

    #summary-panel Label {
        width: 1fr;
    }

    #tables-row {
        height: auto;
        margin: 0 1 1 1;
    }

    .table-box {
        border: round $accent;
        padding: 0 1;
        height: auto;
    }

    #models-box {
        width: 1fr;
        margin-right: 1;
    }

    #expensive-box {
        width: 1fr;
    }

    #events-box {
        border: round $accent;
        padding: 0 1;
        margin: 0 1 1 1;
        height: auto;
        max-height: 80%;
    }

    .section-title {
        text-style: bold;
        color: $text;
        padding: 0 0 0 0;
        margin: 0 0 0 0;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
    }

    DataTable {
        height: auto;
        max-height: 30;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("plus,equal", "increase_interval", "+Interval", show=True),
        Binding("minus,underscore", "decrease_interval", "-Interval", show=True),
    ]

    TITLE = "Cursor Usage"

    def __init__(
        self,
        curl_command: str,
        limit: int = 50,
        keep_all_cookies: bool = False,
        cookie_allowlist: Optional[str] = None,
        minimal_headers: bool = False,
        usage_user: Optional[str] = None,
        interval: int = 60,
    ) -> None:
        super().__init__()
        self._curl_command = curl_command
        self._limit = limit
        self._keep_all_cookies = keep_all_cookies
        self._cookie_allowlist = cookie_allowlist
        self._minimal_headers = minimal_headers
        self._usage_user = usage_user
        self._interval = interval
        self._refresh_timer: Optional[Any] = None
        self._refresh_in_progress = False
        # Track whether we're currently showing the extended token columns
        self._events_has_token_cols = False

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="main-scroll"):
            with Vertical(id="summary-panel"):
                yield Label("[bold]Summary[/]", classes="section-title")
                yield SummaryPanel()
            with Horizontal(id="tables-row"):
                with Vertical(id="models-box", classes="table-box"):
                    yield Label("[bold]Top Models (this month)[/]", classes="section-title")
                    yield DataTable(id="models-table")
                with Vertical(id="expensive-box", classes="table-box"):
                    yield Label("[bold]Most Expensive Requests[/]", classes="section-title")
                    yield DataTable(id="expensive-table")
            with Vertical(id="events-box"):
                yield Label("[bold]Latest Events[/]", classes="section-title", id="events-title")
                yield DataTable(id="events-table")
        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        # Models table
        models_table = self.query_one("#models-table", DataTable)
        models_table.add_columns("Rank", "Model", "Requests", "Spend")
        models_table.cursor_type = "none"
        models_table.zebra_stripes = True

        # Expensive table
        expensive_table = self.query_one("#expensive-table", DataTable)
        expensive_table.add_columns("Rank", "Model", "Tokens", "Cost", "Time")
        expensive_table.cursor_type = "none"
        expensive_table.zebra_stripes = True

        # Events table — columns are set dynamically depending on width
        events_table = self.query_one("#events-table", DataTable)
        events_table.cursor_type = "none"
        events_table.zebra_stripes = True

        status = self.query_one("#status-bar", StatusBar)
        status.refresh_interval = self._interval

        self._schedule_timer()
        self.action_refresh()

    # -- timer -------------------------------------------------------------

    def _schedule_timer(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._refresh_timer = self.set_interval(self._interval, self._on_refresh_timer)

    def _on_refresh_timer(self) -> None:
        self.action_refresh()

    # -- actions -----------------------------------------------------------

    def action_refresh(self) -> None:
        if self._refresh_in_progress:
            return
        self._set_loading(True)
        self._do_refresh()

    def action_increase_interval(self) -> None:
        self._interval = min(self._interval + 10, 600)
        status = self.query_one("#status-bar", StatusBar)
        status.refresh_interval = self._interval
        self._schedule_timer()

    def action_decrease_interval(self) -> None:
        self._interval = max(self._interval - 10, 10)
        status = self.query_one("#status-bar", StatusBar)
        status.refresh_interval = self._interval
        self._schedule_timer()

    # -- helpers -----------------------------------------------------------

    def _should_show_token_cols(self) -> bool:
        """Decide whether there's enough horizontal space for token cols."""
        try:
            events_box = self.query_one("#events-box")
            return events_box.size.width >= _TOKEN_COLS_MIN_WIDTH
        except Exception:
            return self.size.width >= _TOKEN_COLS_MIN_WIDTH

    def _rebuild_events_columns(self, show_tokens: bool) -> None:
        """Rebuild the events DataTable columns if the mode changed."""
        events_table = self.query_one("#events-table", DataTable)
        if show_tokens == self._events_has_token_cols and events_table.columns:
            return  # no change needed
        events_table.clear(columns=True)
        if show_tokens:
            events_table.add_columns(
                "Time (UTC)", "Model", "Usage",
                "In", "Out", "Cache R", "Cache W",
            )
        else:
            events_table.add_columns("Time (UTC)", "Model", "Usage")
        self._events_has_token_cols = show_tokens

    # -- data fetch --------------------------------------------------------

    @work(thread=True, group="refresh")
    def _do_refresh(self) -> None:
        try:
            data = fetch_dashboard_data(
                self._curl_command,
                limit=self._limit,
                keep_all_cookies=self._keep_all_cookies,
                cookie_allowlist=self._cookie_allowlist,
                minimal_headers=self._minimal_headers,
                usage_user=self._usage_user,
            )
            self.call_from_thread(self._apply_data, data)
        except Exception as exc:
            self.call_from_thread(
                self.notify,
                f"Refresh failed: {exc}",
                severity="error",
                timeout=5,
            )
        finally:
            self.call_from_thread(self._set_loading, False)

    def _set_loading(self, is_loading: bool) -> None:
        """Set refresh status from the main thread."""
        self._refresh_in_progress = is_loading
        status = self.query_one("#status-bar", StatusBar)
        status.is_loading = is_loading
        if not is_loading:
            status.last_refresh = _now_str()

    def _apply_data(self, data: Dict[str, Any]) -> None:
        # Summary
        summary_panel = self.query_one(SummaryPanel)
        summary_panel.update_data(data)

        # Models table
        models_table = self.query_one("#models-table", DataTable)
        models_table.clear()
        for idx, row in enumerate(data.get("model_breakdown", []), start=1):
            model_name = str(row.get("model", "unknown"))
            models_table.add_row(
                str(idx),
                colored_model(model_name),
                str(row.get("requests", 0)),
                format_money(row.get("cost")),
            )

        # Expensive requests table
        expensive_table = self.query_one("#expensive-table", DataTable)
        expensive_table.clear()
        for idx, row in enumerate(data.get("top_expensive", []), start=1):
            model_name = str(row.get("model", "unknown"))
            expensive_table.add_row(
                str(idx),
                colored_model(model_name),
                format_tokens(row.get("tokens")),
                format_money(row.get("cost")),
                str(row.get("time", "n/a")),
            )

        # Events table — conditionally show token columns
        show_tokens = self._should_show_token_cols()
        self._rebuild_events_columns(show_tokens)

        events = data.get("event_summaries", [])
        events_table = self.query_one("#events-table", DataTable)
        events_table.clear()
        for row in events:
            model_name = str(row.get("model", "unknown"))
            base_cells: list[Any] = [
                row.get("time", ""),
                event_model_cell(
                    model_name,
                    bool(row.get("is_token_based_call")),
                ),
                row.get("usage", ""),
            ]
            if show_tokens:
                base_cells.extend([
                    format_tokens(row.get("input_tokens")),
                    format_tokens(row.get("output_tokens")),
                    format_tokens(row.get("cache_read")),
                    format_tokens(row.get("cache_write")),
                ])
            events_table.add_row(*base_cells)

        count = len(events)
        total = len(data.get("events", []))
        title_label = self.query_one("#events-title", Label)
        title_label.update(
            f"[bold]Latest Events[/] (showing {count} of {total})"
        )

        self.notify(f"Refreshed at {_now_str()}", timeout=2)

    def on_resize(self) -> None:
        """Re-evaluate token columns when terminal is resized."""
        show_tokens = self._should_show_token_cols()
        if show_tokens != self._events_has_token_cols:
            # Trigger a refresh to rebuild columns with current data
            self.action_refresh()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cursor usage TUI dashboard with auto-refresh."
    )
    parser.add_argument(
        "--curl",
        help="Full curl command wrapped in quotes.",
    )
    parser.add_argument(
        "--curl-file",
        help="Path to a text file containing the curl command.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of latest events to display (default: 50).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Auto-refresh interval in seconds (default: 60). Adjustable at runtime with +/-.",
    )
    parser.add_argument(
        "--keep-all-cookies",
        action="store_true",
        help="Do not filter cookies from the curl command.",
    )
    parser.add_argument(
        "--cookie-allowlist",
        default=None,
        help="Comma-separated cookie names to keep when filtering.",
    )
    parser.add_argument(
        "--minimal-headers",
        action="store_true",
        help="Drop browser-only headers.",
    )
    parser.add_argument(
        "--usage-user",
        help="Override user id for billing usage endpoint.",
    )

    args = parser.parse_args()

    curl_command = args.curl
    if args.curl_file:
        with open(args.curl_file, "r", encoding="utf-8") as handle:
            curl_command = handle.read().strip()

    if not curl_command:
        print("Error: Provide --curl or --curl-file with your curl command.", file=sys.stderr)
        sys.exit(1)

    app = CursorUsageTUI(
        curl_command=curl_command,
        limit=args.limit,
        keep_all_cookies=args.keep_all_cookies,
        cookie_allowlist=args.cookie_allowlist,
        minimal_headers=args.minimal_headers,
        usage_user=args.usage_user,
        interval=args.interval,
    )
    app.run()


if __name__ == "__main__":
    main()
