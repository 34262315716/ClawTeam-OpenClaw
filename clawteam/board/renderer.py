"""Renders board data using Rich tables, panels, and columns."""

from __future__ import annotations

import time

from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from clawteam.platform_compat import install_signal_handlers, restore_signal_handlers


STATUS_EMOJI = {
    "healthy": "🟢",
    "degraded": "🟡",
    "open": "🔴",
    True: "🟢",
    False: "⚫",
}


class BoardRenderer:
    """Renders board data using Rich."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def render_team_board(self, data: dict, mode: str = "kanban") -> None:
        """Render a full team board to the console.

        Args:
            data: Board data from BoardCollector.
            mode: "kanban" (default, 4-column task layout) or
                  "agents" (per-agent slot grid showing current task).
        """
        if mode == "agents":
            self.console.print(self._build_agent_grid(data))
        else:
            self.console.print(self._build_team_board(data))

    def render_overview(self, teams: list[dict]) -> None:
        """Render a multi-team overview table."""
        if not teams:
            self.console.print("[dim]No teams found[/dim]")
            return

        table = Table(title="Team Overview")
        table.add_column("Team", style="cyan")
        table.add_column("Leader")
        table.add_column("Members", justify="right")
        table.add_column("Tasks", justify="right")
        table.add_column("Pending Msgs", justify="right")

        for t in teams:
            table.add_row(
                t["name"],
                t.get("leader", ""),
                str(t["members"]),
                str(t["tasks"]),
                str(t["pendingMessages"]),
            )
        self.console.print(table)

    def render_team_board_live(self, collector, team_name: str, interval: float = 2.0, mode: str = "kanban") -> None:
        """Render a live-refreshing team board. Ctrl+C to stop."""
        running = True

        def _handle_signal(signum, frame):
            nonlocal running
            running = False

        previous_handlers = install_signal_handlers(_handle_signal)

        try:
            with Live(console=self.console, refresh_per_second=1, screen=False) as live:
                while running:
                    try:
                        data = collector.collect_team(team_name)
                        renderable = self._build_team_board(data) if mode == "kanban" else self._build_agent_grid(data)
                    except ValueError as e:
                        renderable = Text(str(e), style="red")
                        live.update(renderable)
                        break
                    live.update(renderable)
                    time.sleep(interval)
        finally:
            restore_signal_handlers(previous_handlers)

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _build_team_board(self, data: dict) -> Group:
        """Build the full team board as a Rich Group of renderables."""
        team = data["team"]
        members = data["members"]
        tasks = data["tasks"]
        summary = data["taskSummary"]

        parts = []

        # 1. Team header panel
        header_text = (
            f"Leader: [cyan]{team.get('leaderName', team.get('leadAgentId', ''))}[/cyan]  |  "
            f"Members: [cyan]{len(members)}[/cyan]  |  "
            f"Created: [dim]{team['createdAt'][:19]}[/dim]"
        )
        cost = data.get("cost", {})
        total_cents = cost.get("totalCostCents", 0)
        if total_cents > 0:
            budget_cents = team.get("budgetCents", 0)
            if budget_cents > 0:
                header_text += f"  |  Cost: [yellow]${total_cents / 100:.2f} / ${budget_cents / 100:.2f}[/yellow]"
            else:
                header_text += f"  |  Cost: [yellow]${total_cents / 100:.2f}[/yellow]"
        desc = team.get("description", "")
        if desc:
            header_text = f"{desc}\n{header_text}"
        parts.append(Panel(header_text, title=f"Team: {team['name']}", border_style="bright_blue"))

        # 2. Members table
        has_user = any(m.get("user") for m in members)
        mem_table = Table(title="Members")
        mem_table.add_column("Name", style="cyan")
        if has_user:
            mem_table.add_column("User", style="magenta")
        mem_table.add_column("Type")
        mem_table.add_column("Joined", style="dim")
        mem_table.add_column("Inbox", justify="right")
        for m in members:
            inbox_style = "red" if m["inboxCount"] > 0 else "dim"
            row = [m["name"]]
            if has_user:
                row.append(m.get("user", ""))
            row.extend([
                m["agentType"],
                m["joinedAt"][:19],
                f"[{inbox_style}]{m['inboxCount']}[/{inbox_style}]",
            ])
            mem_table.add_row(*row)
        parts.append(mem_table)

        # 3. Task board (4-column kanban)
        parts.append(self._build_task_kanban(tasks, summary))

        return Group(*parts)

    def _build_task_kanban(self, tasks: dict, summary: dict) -> Panel:
        """Build the 4-column kanban task board."""
        columns_cfg = [
            ("PENDING", "pending", "yellow"),
            ("IN PROGRESS", "in_progress", "cyan"),
            ("COMPLETED", "completed", "green"),
            ("BLOCKED", "blocked", "red"),
        ]

        panels = []
        for label, key, color in columns_cfg:
            count = summary.get(key, 0)
            items = tasks.get(key, [])
            lines = []
            for t in items:
                task_id = t.get("id", "")[:8]
                subject = t.get("subject", "")
                owner = t.get("owner", "") or "-"
                lines.append(f"[bold]#{task_id}[/bold] {subject}")
                lines.append(f"  owner: {owner}")
                if key == "in_progress" and t.get("lockedBy"):
                    lines.append(f"  locked by: [yellow]{t['lockedBy']}[/yellow]")
                if key == "blocked" and t.get("blockedBy"):
                    lines.append(f"  blocked by: {', '.join(t['blockedBy'])}")
                lines.append("")

            body = "\n".join(lines).rstrip() if lines else "[dim]  (none)[/dim]"
            panels.append(
                Panel(
                    body,
                    title=f"{label} ({count})",
                    border_style=color,
                    expand=True,
                )
            )

        total = summary.get("total", 0)
        return Panel(
            Columns(panels, equal=True, expand=True),
            title=f"Task Board ({total} total)",
        )

    # ------------------------------------------------------------------
    # Agent-grid rendering
    # ------------------------------------------------------------------

    def _build_agent_grid(self, data: dict) -> Group:
        """Build an agent-centric view: each agent gets a slot showing
        their current task and completion status."""
        team = data["team"]
        members = data["members"]
        agents = data.get("agents", [])
        tasks = data["tasks"]
        summary = data["taskSummary"]

        parts = []

        # 1. Team header panel
        header_text = (
            f"Leader: [cyan]{team.get('leaderName', team.get('leadAgentId', ''))}[/cyan]  |  "
            f"Members: [cyan]{len(members)}[/cyan]  |  "
            f"Tasks: [cyan]{summary.get('total', 0)}[/cyan]  |  "
            f"Created: [dim]{team['createdAt'][:19]}[/dim]"
        )
        cost = data.get("cost", {})
        total_cents = cost.get("totalCostCents", 0)
        if total_cents > 0:
            budget_cents = team.get("budgetCents", 0)
            if budget_cents > 0:
                header_text += f"  |  Cost: [yellow]${total_cents / 100:.2f} / ${budget_cents / 100:.2f}[/yellow]"
            else:
                header_text += f"  |  Cost: [yellow]${total_cents / 100:.2f}[/yellow]"
        desc = team.get("description", "")
        if desc:
            header_text = f"{desc}\n{header_text}"
        parts.append(Panel(header_text, title=f"Team: {team['name']}", border_style="bright_blue"))

        # 2. Per-agent slots
        agent_panels = []
        for a in agents:
            panel = self._build_agent_slot(a)
            if panel:
                agent_panels.append(panel)

        if agent_panels:
            parts.append(
                Panel(
                    Columns(agent_panels, equal=True, expand=True),
                    title="Agent Grid",
                    border_style="cyan",
                )
            )

        # 3. Summary row: completed / in_progress / pending / blocked counts
        comp = summary.get("completed", 0)
        ip = summary.get("in_progress", 0)
        pend = summary.get("pending", 0)
        blk = summary.get("blocked", 0)
        summary_line = (
            f"  [green]Completed: {comp}[/green]  |  "
            f"[cyan]In Progress: {ip}[/cyan]  |  "
            f"[yellow]Pending: {pend}[/yellow]  |  "
            f"[red]Blocked: {blk}[/red]"
        )
        parts.append(Panel(summary_line, title="Task Summary"))

        return Group(*parts)

    def _build_agent_slot(self, agent: dict) -> Panel | None:
        """Build a single agent slot panel for the agent-grid view."""
        name = agent["name"]
        agent_type = agent.get("agentType", "")
        alive = agent.get("alive", False)
        inbox_count = agent.get("inboxCount", 0)
        current_task = agent.get("currentTask")
        completed = agent.get("completedCount", 0)
        task_count = agent.get("taskCount", 0)

        # Status icon
        alive_icon = "🟢" if alive else "⚫"
        inbox_hint = f" [red]📨{inbox_count}[/red]" if inbox_count > 0 else ""

        lines = [f"{alive_icon} [bold]{name}[/bold] [dim]({agent_type})[/dim]{inbox_hint}"]

        if current_task:
            tid = current_task.get("id", "")[:8]
            subject = current_task.get("subject", "")
            lines.append("")
            lines.append(f"  [cyan]▶ Current:[/cyan]")
            lines.append(f"    [bold]#{tid}[/bold] {subject}")
        else:
            lines.append("")
            if task_count > 0:
                lines.append("  [dim]⏸ Idle (no active task)[/dim]")
            else:
                lines.append("  [dim]📋 No tasks assigned[/dim]")

        lines.append("")
        lines.append(f"  ✅ Completed: [green]{completed}[/green]")
        lines.append(f"  📋 Total tasks: {task_count}")

        body = "\n".join(lines)
        return Panel(body, border_style="green" if alive else "dim")
