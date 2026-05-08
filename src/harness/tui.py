from __future__ import annotations

from pathlib import Path


def run_read_only_tui(project_root: Path) -> None:
    from textual.app import App, ComposeResult
    from textual.widgets import Footer, Header, Static

    class HarnessReadOnlyTui(App):
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Static(
                "\n".join(
                    [
                        "Agent Harness",
                        "",
                        f"Project: {project_root}",
                        "",
                        "Read-only TUI launch is available.",
                        "Dashboard panels are planned for the next v1.4 slice.",
                        "",
                        "Press q to exit.",
                    ]
                )
            )
            yield Footer()

    HarnessReadOnlyTui().run()
