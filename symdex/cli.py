# Copyright (c) 2026 Muhammad Husnain
# This file is part of SymDex.
# License: See LICENSE file in the project root.

import json
import importlib.metadata
import os
import subprocess
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from symdex.core.context_pack import build_context_pack
from symdex.core.indexer import index_folder as _index_folder, invalidate as _invalidate
from symdex.core.quality import attach_quality_to_items
from symdex.core.watcher import WatcherAlreadyRunningError, watch as _watch_repo
from symdex.core.storage import (
    get_connection,
    get_db_path,
    get_index_status,
    get_registry_json_path,
    get_registry_path,  # noqa: F401 — imported for monkeypatching
    get_stale_repos,
    query_file_symbols,
    query_repos,
    query_repo_has_embeddings,
    query_routes,
    remove_repo,
    search_text_in_index,
    upsert_repo,
)
from symdex.core.token_metrics import (
    build_search_roi_summary_from_rows,
    format_search_roi_agent_hint,
    format_search_roi_summary,
)
from symdex.core.updates import get_update_notice
from symdex.search.symbol_search import search_symbols as _search_symbols
from symdex.search.semantic import search_semantic as _search_semantic

app = typer.Typer(name="symdex", help="SymDex - universal code indexer")
console = Console()
err_console = Console(stderr=True)
_UPDATE_NOTICE_EMITTED = False


def _apply_state_dir_override(state_dir: Optional[str]) -> None:
    if state_dir:
        os.environ["SYMDEX_STATE_DIR"] = state_dir


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(importlib.metadata.version("symdex"))
    raise typer.Exit()


@app.callback()
def main(
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show SymDex version and exit.",
    ),
) -> None:
    _apply_state_dir_override(state_dir)


def _format_language_breakdown(languages: dict[str, int]) -> str:
    if not languages:
        return "none"
    parts = [f"{name}: {count}" for name, count in sorted(languages.items())]
    return ", ".join(parts)


def _print_code_summary(summary: dict) -> None:
    table = Table(title="Code Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Files", str(summary.get("file_count", 0)))
    table.add_row("Lines of Code", str(summary.get("lines_of_code", 0)))
    table.add_row("Symbols", str(summary.get("symbol_count", 0)))
    table.add_row("Functions", str(summary.get("functions", 0)))
    table.add_row("Classes", str(summary.get("classes", 0)))
    table.add_row("Methods", str(summary.get("methods", 0)))
    table.add_row("Constants", str(summary.get("constants", 0)))
    table.add_row("Variables", str(summary.get("variables", 0)))
    table.add_row("Routes", str(summary.get("routes", 0)))
    table.add_row("Languages", _format_language_breakdown(summary.get("language_distribution", {})))
    table.add_row("Skipped", str(summary.get("skipped", 0)))
    table.add_row("Errors", str(summary.get("errored", 0)))
    console.print(table)


def _repo_root(repo: str) -> str | None:
    for entry in query_repos():
        if entry["name"] == repo:
            return entry["root_path"]
    return None


def _repo_entry(repo: str) -> dict | None:
    for entry in query_repos():
        if entry["name"] == repo:
            return entry
    return None


def _require_indexed_repo(repo: str) -> dict:
    entry = _repo_entry(repo)
    if entry is None:
        err_console.print(f"[red]Error:[/red] Repo not indexed: {repo}")
        raise typer.Exit(code=1)
    return entry


def _repo_has_semantic_embeddings(conn, repo: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM symbols WHERE repo = ? AND embedding IS NOT NULL LIMIT 1",
        (repo,),
    ).fetchone()
    return row is not None


def _print_search_roi(summary: dict) -> None:
    console.print(f"[bold green]{format_search_roi_summary(summary)}[/bold green]")


def _stdout_is_terminal() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(callable(isatty) and isatty())


def _maybe_print_update_notice(argv: list[str] | None = None, json_output: bool = False) -> None:
    global _UPDATE_NOTICE_EMITTED
    if _UPDATE_NOTICE_EMITTED or json_output or not _stdout_is_terminal():
        return

    notice = get_update_notice(argv)
    if notice is None:
        return

    _UPDATE_NOTICE_EMITTED = True
    console.print(
        f"[bold yellow]Update available:[/bold yellow] SymDex "
        f"{notice['latest_version']} (you have {notice['installed_version']})"
    )
    console.print(f"pip: [cyan]{notice['pip_command']}[/cyan]")
    console.print(f"uv tool: [cyan]{notice['uv_tool_command']}[/cyan]")
    console.print(f"uvx: [cyan]{notice['uvx_command']}[/cyan]")
    console.print("[dim]Fewer tokens. More signal. Stay current.[/dim]")
    console.print()


def _build_lazy_watch_command(
    path: str,
    repo: str,
    state_dir: Optional[str] = None,
    interval: float = 5.0,
    idle_timeout: float = 1800.0,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "symdex.cli",
        "watch",
        path,
        "--repo",
        repo,
        "--embed",
        "--interval",
        f"{interval:g}",
        "--idle-timeout",
        f"{idle_timeout:g}",
    ]
    if state_dir:
        command.extend(["--state-dir", state_dir])
    return command


def _start_lazy_embedding_watch(
    path: str,
    repo: str,
    state_dir: Optional[str] = None,
    interval: float = 5.0,
    idle_timeout: float = 1800.0,
) -> int:
    abs_path = os.path.abspath(path)
    effective_state_dir = os.path.abspath(state_dir) if state_dir else None
    command = _build_lazy_watch_command(
        abs_path,
        repo,
        state_dir=effective_state_dir,
        interval=interval,
        idle_timeout=idle_timeout,
    )
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    process = subprocess.Popen(
        command,
        cwd=abs_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=os.name != "nt",
        creationflags=creationflags,
    )
    return int(process.pid)


def _search_roi_summary(repo: str, rows: list[dict], result_kind: str) -> dict | None:
    root = _repo_root(repo)
    if not root or not rows:
        return None
    conn = get_connection(get_db_path(repo))
    try:
        return build_search_roi_summary_from_rows(
            conn,
            repo=repo,
            rows=rows,
            repo_root=root,
            result_kind=result_kind,
        )
    finally:
        conn.close()


def _attach_roi_payload(payload: dict, roi: dict | None) -> dict:
    if roi is not None:
        payload["roi"] = roi
        payload["roi_summary"] = format_search_roi_summary(roi)
        payload["roi_agent_hint"] = format_search_roi_agent_hint(roi)
    return payload


def _quality_context_for_cli(repo: str) -> tuple[bool, dict | None]:
    conn = get_connection(get_db_path(repo))
    try:
        has_embeddings = query_repo_has_embeddings(conn, repo)
    finally:
        conn.close()
    try:
        status = get_index_status(repo, get_db_path(repo))
    except Exception:  # noqa: BLE001
        status = None
    return has_embeddings, status


@app.command()
def index(
    path: str = typer.Argument(..., help="Directory to index"),
    repo: str = typer.Option(
        None,
        "--repo",
        "--name",
        "-r",
        "-n",
        help="Repo name (omit to auto-generate from git branch and path hash)",
    ),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
    embed: bool = typer.Option(
        True,
        "--embed/--no-embed",
        help="Build semantic embeddings during the foreground index.",
    ),
    lazy: bool = typer.Option(
        False,
        "--lazy",
        help="Index code now and build semantic embeddings in a background watcher.",
    ),
    lazy_interval: float = typer.Option(
        5.0,
        "--lazy-interval",
        help="Seconds between background lazy re-index cycles.",
    ),
    lazy_idle_timeout: float = typer.Option(
        1800.0,
        "--lazy-idle-timeout",
        help="Stop the lazy background watcher after this many idle seconds.",
    ),
) -> None:
    """Index a folder and register it."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:])
    if not os.path.isdir(path):
        err_console.print(f"[red]Error:[/red] Path does not exist: {path}")
        raise typer.Exit(code=1)
    foreground_embed = embed and not lazy
    result = _index_folder(
        path,
        repo=repo,
        progress_callback=lambda msg: console.print(f"[dim]{msg}[/dim]"),
        embed=foreground_embed,
    )
    upsert_repo(result.repo, root_path=os.path.abspath(path), db_path=result.db_path)
    table = Table(title="Index Result")
    table.add_column("Repo", style="cyan")
    table.add_column("Indexed", style="green")
    table.add_column("Skipped", style="yellow")
    table.add_column("DB Path")
    table.add_row(result.repo, str(result.indexed_count), str(result.skipped_count), result.db_path)
    console.print(table)
    console.print()
    _print_code_summary(result.summary)
    console.print(f"[dim]Registry DB:[/dim] {get_registry_path()}")
    console.print(f"[dim]Registry JSON:[/dim] {get_registry_json_path()}")
    if lazy:
        pid = _start_lazy_embedding_watch(
            path,
            result.repo,
            state_dir=state_dir,
            interval=lazy_interval,
            idle_timeout=lazy_idle_timeout,
        )
        console.print(
            "[green]Background semantic indexing started[/green] "
            f"for [cyan]{result.repo}[/cyan] (pid {pid})."
        )


@app.command()
def search(
    query: str = typer.Argument(..., help="Symbol name to search for"),
    repo: str = typer.Option(None, "--repo", "-r", help="Repo name (omit to search all repos)"),
    kind: str = typer.Option(None, "--kind", "-k", help="Symbol kind filter"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Find functions/classes by name (omit --repo to search all indexed repos)."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    if repo:
        _require_indexed_repo(repo)
        conn = get_connection(get_db_path(repo))
        try:
            symbols = _search_symbols(conn, repo=repo, query=query, kind=kind, limit=limit)
        finally:
            conn.close()
    else:
        from symdex.graph.registry import search_across_repos
        symbols = search_across_repos(query=query, kind=kind, limit=limit)
    if not symbols:
        err_console.print(f"[red]Error:[/red] No symbols found matching: {query}")
        raise typer.Exit(code=1)
    if json_output:
        if repo:
            has_embeddings, status = _quality_context_for_cli(repo)
            symbols = attach_quality_to_items(symbols, "symbol", has_embeddings, status)
        payload = {"symbols": symbols}
        if repo:
            roi = _search_roi_summary(repo, symbols, "symbol")
            _attach_roi_payload(payload, roi)
        typer.echo(json.dumps(payload))
        return
    table = Table(title=f"Symbols matching '{query}'")
    table.add_column("Repo", style="blue")
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("File")
    table.add_column("Start", style="dim")
    for s in symbols:
        table.add_row(s.get("repo", repo or ""), s["name"], s["kind"], s["file"], str(s["start_byte"]))
    console.print(table)
    if repo:
        roi = _search_roi_summary(repo, symbols, "symbol")
        if roi is not None:
            _print_search_roi(roi)


@app.command()
def find(
    name: str = typer.Argument(..., help="Exact symbol name"),
    repo: str = typer.Option(None, "--repo", "-r", help="Repo name"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Exact symbol name lookup by symbol name."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    if not repo:
        err_console.print("[red]Error:[/red] --repo is required")
        raise typer.Exit(code=1)
    _require_indexed_repo(repo)
    conn = get_connection(get_db_path(repo))
    try:
        symbols = _search_symbols(conn, repo=repo, query=name, limit=1)
    finally:
        conn.close()
    if not symbols:
        err_console.print(f"[red]Error:[/red] Symbol not found: {name}")
        raise typer.Exit(code=1)
    if json_output:
        has_embeddings, status = _quality_context_for_cli(repo)
        symbols = attach_quality_to_items(symbols, "symbol", has_embeddings, status)
        payload = {"symbols": symbols}
        roi = _search_roi_summary(repo, symbols, "symbol")
        _attach_roi_payload(payload, roi)
        typer.echo(json.dumps(payload))
        return
    s = symbols[0]
    table = Table(title=f"Symbol: {name}")
    table.add_column("Field")
    table.add_column("Value")
    for k, v in s.items():
        table.add_row(k, str(v) if v is not None else "")
    console.print(table)
    roi = _search_roi_summary(repo, symbols, "symbol")
    if roi is not None:
        _print_search_roi(roi)


@app.command()
def outline(
    file: str = typer.Argument(..., help="Relative file path within repo"),
    repo: str = typer.Option(..., "--repo", "-r", help="Repo name"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """List all symbols in a file."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    _require_indexed_repo(repo)
    conn = get_connection(get_db_path(repo))
    try:
        symbols = query_file_symbols(conn, repo=repo, file=file)
    finally:
        conn.close()
    if not symbols:
        err_console.print(f"[red]Error:[/red] No symbols found in: {file}")
        raise typer.Exit(code=1)
    if json_output:
        has_embeddings, status = _quality_context_for_cli(repo)
        symbols = attach_quality_to_items(symbols, "outline", has_embeddings, status)
        typer.echo(json.dumps({"symbols": symbols}))
        return
    table = Table(title=f"Outline: {file}")
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Start", style="dim")
    table.add_column("End", style="dim")
    for s in symbols:
        table.add_row(s["name"], s["kind"], str(s["start_byte"]), str(s["end_byte"]))
    console.print(table)


@app.command()
def text(
    query: str = typer.Argument(..., help="Text to search for"),
    repo: str = typer.Option(None, "--repo", "-r", help="Repo name"),
    pattern: str = typer.Option(None, "--pattern", "-p", help="File glob pattern"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Text search across indexed files."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    if not repo:
        err_console.print("[red]Error:[/red] --repo is required")
        raise typer.Exit(code=1)
    repo_info = _require_indexed_repo(repo)
    conn = get_connection(get_db_path(repo))
    try:
        matches = search_text_in_index(conn, repo=repo, query=query, repo_root=repo_info["root_path"], file_pattern=pattern)
    finally:
        conn.close()
    if not matches:
        err_console.print(f"[red]Error:[/red] No matches found for: {query}")
        raise typer.Exit(code=1)
    if json_output:
        has_embeddings, status = _quality_context_for_cli(repo)
        matches = attach_quality_to_items(matches, "text", has_embeddings, status)
        payload = {"matches": matches}
        roi = _search_roi_summary(repo, matches, "text")
        _attach_roi_payload(payload, roi)
        typer.echo(json.dumps(payload))
        return
    table = Table(title=f"Text matches for '{query}'")
    table.add_column("File")
    table.add_column("Line", style="dim")
    table.add_column("Text")
    for m in matches:
        table.add_row(m["file"], str(m["line"]), m["text"])
    console.print(table)
    roi = _search_roi_summary(repo, matches, "text")
    if roi is not None:
        _print_search_roi(roi)


@app.command()
def semantic(
    query: str = typer.Argument(..., help="Natural language query"),
    repo: str = typer.Option(None, "--repo", "-r", help="Repo name"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Semantic similarity search by meaning."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    from symdex.search.semantic import search_semantic
    if not repo:
        err_console.print("[red]Error:[/red] --repo is required")
        raise typer.Exit(code=1)
    _require_indexed_repo(repo)
    backend = os.environ.get("SYMDEX_EMBED_BACKEND", "local").strip().lower()
    if (
        backend == "local"
        and "sentence_transformers" in sys.modules
        and sys.modules["sentence_transformers"] is None
    ):
        local_extra = escape("symdex[local]")
        err_console.print(
            f'[red]Error:[/red] The local semantic backend requires `{local_extra}`. '
            f'Install it with `pip install "{local_extra}"`.'
        )
        raise typer.Exit(code=1)
    conn = get_connection(get_db_path(repo))
    try:
        if not _repo_has_semantic_embeddings(conn, repo):
            local_extra = escape("symdex[local]")
            err_console.print(
                "[red]Error:[/red] "
                f"Repo has no semantic embeddings: {repo}. "
                f"Install `{local_extra}` or enable another embedding backend, then run `symdex index`, "
                "`symdex index --lazy`, or `symdex watch --embed`."
            )
            raise typer.Exit(code=1)
        try:
            results = _search_semantic(
                conn,
                query=query,
                repo=repo,
                limit=limit,
                progress_callback=lambda msg: console.print(f"[dim]{msg}[/dim]"),
            )
        except Exception as exc:
            err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1)
    finally:
        conn.close()
    if not results:
        err_console.print(f"[red]Error:[/red] No semantic matches found for: {query}")
        raise typer.Exit(code=1)
    if json_output:
        has_embeddings, status = _quality_context_for_cli(repo)
        results = attach_quality_to_items(results, "semantic", has_embeddings, status)
        payload = {"symbols": results}
        roi = _search_roi_summary(repo, results, "symbol")
        _attach_roi_payload(payload, roi)
        typer.echo(json.dumps(payload))
        return
    table = Table(title=f"Semantic matches for '{query}'")
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Score", style="green")
    table.add_column("File")
    for s in results:
        table.add_row(s["name"], s["kind"], str(s["score"]), s["file"])
    console.print(table)
    roi = _search_roi_summary(repo, results, "symbol")
    if roi is not None:
        _print_search_roi(roi)


@app.command()
def pack(
    query: str = typer.Argument(..., help="Question or topic to build context for"),
    repo: str = typer.Option(..., "--repo", "-r", help="Repo name"),
    budget: int = typer.Option(6000, "--budget", "-b", help="Approximate token budget"),
    include: Optional[str] = typer.Option(None, "--include", help="Comma-separated pack sections to include"),
    exclude: Optional[str] = typer.Option(None, "--exclude", help="Comma-separated pack sections to exclude"),
    output_format: str = typer.Option("text", "--format", help="Output format: text or json"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Build a token-budgeted context pack for an agent query."""
    _apply_state_dir_override(state_dir)
    json_output = output_format.lower() == "json"
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    if output_format.lower() not in {"text", "json"}:
        err_console.print("[red]Error:[/red] --format must be 'text' or 'json'")
        raise typer.Exit(code=1)
    _require_indexed_repo(repo)
    try:
        payload = build_context_pack(
            repo=repo,
            query=query,
            token_budget=budget,
            include=include,
            exclude=exclude,
        )
    except ValueError as exc:
        err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(payload))
        return

    budget_info = payload["budget"]
    table = Table(title="Context Pack")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Repo", payload["repo"])
    table.add_row("Query", payload["query"])
    table.add_row(
        "Tokens",
        f"{budget_info['estimated_tokens']} used / {budget_info['available_tokens']} available",
    )
    table.add_row("Selected", str(len(payload["selected_evidence"])))
    table.add_row("Omitted", str(len(payload["omitted_candidates"])))
    console.print(table)

    evidence = Table(title="Selected Evidence")
    evidence.add_column("Type", style="magenta")
    evidence.add_column("File")
    evidence.add_column("Title", style="cyan")
    evidence.add_column("Tokens", style="green")
    for item in payload["selected_evidence"]:
        evidence.add_row(
            item.get("type", ""),
            item.get("file", ""),
            str(item.get("title", "")),
            str(item.get("estimated_tokens", 0)),
        )
    console.print(evidence)

    if payload["warnings"]:
        for warning in payload["warnings"]:
            console.print(f"[yellow]Warning:[/yellow] {escape(str(warning))}")


@app.command()
def callers(
    name: str = typer.Argument(..., help="Function name to find callers of"),
    repo: str = typer.Option(..., "--repo", "-r", help="Repo name"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Show all functions that call the named function."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    from symdex.graph.call_graph import get_callers as _get_callers
    _require_indexed_repo(repo)
    conn = get_connection(get_db_path(repo))
    try:
        results = _get_callers(conn, name=name, repo=repo)
    finally:
        conn.close()
    if not results:
        err_console.print(f"[red]Error:[/red] No callers found for: {name}")
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({"callers": results}))
        return
    table = Table(title=f"Callers of '{name}'")
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("File")
    for s in results:
        table.add_row(s["name"], s["kind"], s["file"])
    console.print(table)


@app.command()
def callees(
    name: str = typer.Argument(..., help="Function name to find callees of"),
    repo: str = typer.Option(..., "--repo", "-r", help="Repo name"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Show all functions called by the named function."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    from symdex.graph.call_graph import get_callees as _get_callees
    _require_indexed_repo(repo)
    conn = get_connection(get_db_path(repo))
    try:
        results = _get_callees(conn, name=name, repo=repo)
    finally:
        conn.close()
    if not results:
        err_console.print(f"[red]Error:[/red] No callees found for: {name}")
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({"callees": results}))
        return
    table = Table(title=f"Callees of '{name}'")
    table.add_column("Name", style="cyan")
    table.add_column("File")
    for s in results:
        table.add_row(s["name"], s.get("file") or "")
    console.print(table)


@app.command()
def repos(
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """List all indexed repositories."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    all_repos = query_repos()
    if not all_repos:
        err_console.print("[red]Error:[/red] No repos indexed yet.")
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({
            "repos": all_repos,
            "registry_db": get_registry_path(),
            "registry_json": get_registry_json_path(),
        }))
        return
    table = Table(title="Indexed Repositories")
    table.add_column("Name", style="cyan")
    table.add_column("Root Path")
    table.add_column("Last Indexed", style="dim")
    for r in all_repos:
        table.add_row(r["name"], r["root_path"], str(r.get("last_indexed", "")))
    console.print(table)
    console.print(f"[dim]Registry DB:[/dim] {get_registry_path()}")
    console.print(f"[dim]Registry JSON:[/dim] {get_registry_json_path()}")


@app.command()
def invalidate(
    repo: str = typer.Option(..., "--repo", "-r", help="Repo name"),
    file: str = typer.Option(None, "--file", "-f", help="Specific file to invalidate"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Force re-index of a repo or specific file."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    _require_indexed_repo(repo)
    count = _invalidate(repo, file=file)
    if json_output:
        typer.echo(json.dumps({"invalidated": count}))
        return
    console.print(f"Invalidated [green]{count}[/green] record(s) for repo '[cyan]{repo}[/cyan]'")


@app.command()
def routes(
    repo: str = typer.Argument(..., help="Repo name to query routes for."),
    method: Optional[str] = typer.Option(None, "--method", "-m", help="Filter by HTTP method (GET, POST, ...)."),
    path_contains: Optional[str] = typer.Option(None, "--path", "-p", help="Filter routes whose path contains this string."),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """List HTTP routes indexed for a repo."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:])
    _require_indexed_repo(repo)
    db_path = get_db_path(repo)
    conn = get_connection(db_path)
    try:
        rows = query_routes(conn, repo=repo, method=method, path_contains=path_contains)
    finally:
        conn.close()

    if not rows:
        console.print(f"[yellow]No routes indexed for repo '{repo}'.[/yellow]")
        return

    table = Table(title=f"Routes - {repo}", show_header=True, header_style="bold")
    table.add_column("Method", style="cyan", width=8)
    table.add_column("Path")
    table.add_column("Handler")
    table.add_column("File")
    for r in rows:
        table.add_row(r["method"], r["path"], r.get("handler") or "", r["file"])
    console.print(table)


@app.command()
def serve(
    port: int = typer.Option(None, "--port", "-p", help="HTTP port (omit for stdio mode)"),
    host: str = typer.Option(None, "--host", "-h", help="HTTP host (IPv4) default is 127.0.0.1/localhost (omit for stdio mode)"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Start the MCP server.
    
    :param host: The host to bind the server to. If omitted, will use mcp defaults.
    :type host: str, optional
    :param port: The port to bind the server to. If omitted, will use mcp defaults.
    :type port: int, optional
    """ 
    
    input_args = locals().keys() - ["state_dir"]
    # drop state_dir from kwargs since it is handled separately
    kwargs ={
        i: locals().get(i) for i in input_args if not isinstance(
            locals().get(i),type(None)
        )
    }
    # bundle everything into kwargs for future expansion of serve command
    
    if not isinstance(port, type(None)):
        assert isinstance(port, int) and 0 < port < 65536, f"Invalid port: {port}. Must be an integer between 1 and 65535"
    if host and host != "localhost":
        assert host.count(".") == 3, f"Invalid host: {host}"
        assert all([0 <= int(x if x.isdigit() else -1) <256 for x in host.split(".")]), f"Invalid host: {host}. Must be a valid IPv4 address"
    #quick check to ensure host is a valid IPv4 address IPv6 not added 

    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:])
    from symdex.mcp.server import mcp
    if len(kwargs) != 0:
        mcp.run(transport="streamable-http", **kwargs)
    else:
        mcp.run()


@app.command()
def watch(
    path: str = typer.Argument(..., help="Path to the directory to watch."),
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        "--name",
        "-r",
        "-n",
        help="Repo name (omit to auto-generate from git branch and path hash)",
    ),
    interval: float = typer.Option(5.0, "--interval", "-i", help="Seconds between re-index cycles."),
    embed: bool = typer.Option(
        False,
        "--embed",
        help="Refresh semantic embeddings while watching. Off by default to keep watch low-memory.",
    ),
    idle_timeout: float = typer.Option(
        1800.0,
        "--idle-timeout",
        help="Exit after this many seconds with no file activity. Use 0 to disable.",
    ),
    forever: bool = typer.Option(
        False,
        "--forever",
        help="Disable idle shutdown and keep the watcher running until interrupted.",
    ),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Watch a directory and keep its index up to date automatically."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:])
    effective_idle_timeout = None if forever or idle_timeout <= 0 else idle_timeout
    mode = "with semantic embeddings" if embed else "low-memory, no embedding refresh"
    idle_label = "disabled" if effective_idle_timeout is None else f"{effective_idle_timeout:g}s"
    console.print(
        f"[bold]Watching[/bold] {path} "
        f"(interval={interval:g}s, {mode}, idle-timeout={idle_label}) - Ctrl+C to stop"
    )
    try:
        _watch_repo(
            path,
            repo=repo,
            interval=interval,
            embed=embed,
            idle_timeout=effective_idle_timeout,
        )
    except WatcherAlreadyRunningError as exc:
        console.print(f"[yellow]{escape(str(exc))}[/yellow]")
        raise typer.Exit(0) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]Watch stopped.[/yellow]")


@app.command()
def gc(
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Remove stale index databases for repos whose directories no longer exist."""
    _apply_state_dir_override(state_dir)
    _maybe_print_update_notice(sys.argv[1:], json_output=json_output)
    stale = get_stale_repos()
    removed = []
    for entry in stale:
        remove_repo(entry["name"])
        removed.append(entry["name"])
    if json_output:
        typer.echo(json.dumps({"removed": removed, "count": len(removed)}))
        return
    if not removed:
        console.print("Registry is clean - nothing to remove.")
        return
    for name in removed:
        console.print(f"Removed stale index: [cyan]{name}[/cyan]")
    console.print(f"[green]{len(removed)}[/green] stale index(es) removed.")


@app.command("index-folder", hidden=True)
def index_folder_alias(
    path: str = typer.Argument(..., help="Directory to index"),
    repo: str = typer.Option(
        None,
        "--repo",
        "--name",
        "-r",
        "-n",
        help="Repo name (omit to auto-generate from git branch and path hash)",
    ),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Compatibility alias for MCP-style shell usage."""
    index(
        path=path,
        repo=repo,
        state_dir=state_dir,
        embed=True,
        lazy=False,
        lazy_interval=5.0,
        lazy_idle_timeout=1800.0,
    )


@app.command("index-repo", hidden=True)
def index_repo_alias(
    path: str = typer.Argument(..., help="Directory to index"),
    repo: str = typer.Option(
        None,
        "--repo",
        "--name",
        "-r",
        "-n",
        help="Repo name (omit to auto-generate from git branch and path hash)",
    ),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Compatibility alias for MCP-style shell usage."""
    index(
        path=path,
        repo=repo,
        state_dir=state_dir,
        embed=True,
        lazy=False,
        lazy_interval=5.0,
        lazy_idle_timeout=1800.0,
    )


@app.command("list-repos", hidden=True)
def list_repos_alias(
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    state_dir: Optional[str] = typer.Option(
        None,
        "--state-dir",
        help="State directory for SymDex indexes and registry (for example .symdex)",
    ),
) -> None:
    """Compatibility alias for MCP-style shell usage."""
    repos(json_output=json_output, state_dir=state_dir)


if __name__ == "__main__":
    app()
