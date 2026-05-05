from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH
from .utils import detect_budget_mode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvp",
        description="MVP Agent orchestrator for routing tasks across Codex, OpenClaw, Hermes-style roles, and Ollama.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to mvp.config.json")
    parser.add_argument("--legacy", action="store_true", help="Use legacy MVPLoader instead of MvpAgent")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Plan and execute a delegated task")
    run_parser.add_argument("--task", help="Task text")
    run_parser.add_argument("--task-file", help="Read task text from a file")
    run_parser.add_argument("--mode", choices=["cheap", "balanced", "premium"], help="Budget/quality profile")
    run_parser.add_argument("--plan-only", action="store_true", help="Only plan and route; do not execute")

    plan_parser = subparsers.add_parser("plan", help="Alias of run --plan-only")
    plan_parser.add_argument("--task", help="Task text")
    plan_parser.add_argument("--task-file", help="Read task text from a file")
    plan_parser.add_argument("--mode", choices=["cheap", "balanced", "premium"], help="Budget/quality profile")

    subparsers.add_parser("workers", help="List configured workers")
    subparsers.add_parser("health", help="Check worker availability")
    ping_parser = subparsers.add_parser("ping", help="Run quick or deep roundtrip checks")
    ping_parser.add_argument("--mode", choices=["quick", "deep"], default="quick", help="Ping mode")
    subparsers.add_parser("shell", help="Open interactive agent shell")

    memory_parser = subparsers.add_parser("memory", help="View agent memory stats")
    memory_parser.add_argument("--recall", help="Recall similar past tasks for a query")
    memory_parser.add_argument("--recent", type=int, default=10, help="Show recent N conversations")
    memory_parser.add_argument("--workers", action="store_true", help="Show worker performance from memory")
    skills_parser = subparsers.add_parser("skills", help="Inspect MVP Agent skill mesh and recommendations")
    skills_parser.add_argument("--task", help="Task text to profile against the skill mesh")
    skills_parser.add_argument("--limit", type=int, default=20, help="Show at most N skills")
    subparsers.add_parser("evolve", help="Show learned capability gaps and evolution status")

    subparsers.add_parser("smoke", help="Run smoke tests on all workers")

    app_parser = subparsers.add_parser("app", help="Open the desktop mission control app")
    app_parser.add_argument("--self-test", action="store_true", help="Create the desktop app once, then exit")
    return parser


def normalize_global_args(argv: list[str] | None) -> list[str]:
    if argv is None:
        argv = sys.argv[1:]

    normalized: list[str] = []
    tail: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--config":
            normalized.append(token)
            if index + 1 < len(argv):
                normalized.append(argv[index + 1])
                index += 2
                continue
            index += 1
            continue
        if token.startswith("--config="):
            normalized.append(token)
            index += 1
            continue
        if token == "--legacy":
            normalized.append(token)
            index += 1
            continue
        tail.append(token)
        index += 1
    normalized.extend(tail)
    return normalized


def read_task(args: argparse.Namespace) -> str:
    if args.task:
        return args.task.strip()
    if getattr(args, "task_file", None):
        return Path(args.task_file).read_text(encoding="utf-8").strip()
    raise SystemExit("A task is required. Use --task or --task-file.")


def _get_agent(args: argparse.Namespace):
    """Create the appropriate agent/loader based on --legacy flag."""
    if getattr(args, "legacy", False):
        from .orchestrator import MVPLoader
        return MVPLoader(args.config)
    else:
        from .agent import MvpAgent
        return MvpAgent(args.config)


def print_workers(agent) -> None:
    for row in agent.list_workers():
        print(f"{row['worker_id']}: {row['display_name']}")
        print(f"  role: {row['role']}")
        print(f"  type: {row['type']}")
        print(f"  capabilities: {', '.join(row['capabilities'])}")
        print(
            "  routing: "
            f"cost={row['cost_tier']} quality={row['quality_tier']} speed={row['speed_tier']} "
            f"local={row['local_only']} write={row['supports_workspace_write']} api_cost={row['api_cost']}"
        )
        if row.get("memory_success_rate") is not None:
            print(f"  memory: success_rate={row['memory_success_rate']}, calls={row.get('memory_calls', 0)}")


def print_health(agent) -> None:
    for row in agent.health():
        state = "OK" if row["ok"] else "FAIL"
        extra = ""
        if row.get("memory_calls"):
            extra = f" | memory: {row['memory_calls']} calls"
        print(f"{state:<4} {row['worker_id']}: {row['summary']}{extra}")


def print_ping(agent, mode: str) -> None:
    for row in agent.ping(mode):
        state = "OK" if row["ok"] else "FAIL"
        print(f"{state:<4} {row['worker_id']}: {row['summary']}")
        print(f"  mode: {row.get('ping_mode', mode)} | latency_ms={row.get('latency_ms', '-')}")
        if row.get("deliverable"):
            print(f"  deliverable: {row['deliverable']}")


def print_report(report) -> None:
    print(f"Status: {report.status}")
    print(f"Profile: {report.profile}")
    print(f"Agent Notes: {report.leader_notes}")
    print("")
    print("Plan Summary:")
    print(f"- {report.plan.summary}")
    print(f"- Strategy: {report.plan.execution_strategy}")
    if getattr(report.plan, "doctrine", None):
        print(f"- Doctrine: {' | '.join(report.plan.doctrine)}")
    if getattr(report.plan, "required_skills", None):
        print(f"- Skills: {', '.join(report.plan.required_skills)}")
    if getattr(report.plan, "capability_gaps", None):
        print(f"- Capability gaps: {'; '.join(report.plan.capability_gaps)}")
    print("")
    print("Assignments:")
    if not report.outcomes:
        for assignment in report.planned_assignments:
            subtask = assignment.subtask
            print(f"- {subtask.task_id} -> {assignment.worker_id} [{subtask.kind}] {subtask.title}")
            print(f"  goal: {subtask.goal}")
            print(f"  needs write: {subtask.needs_write_access}")
            print(f"  routing rationale: {', '.join(assignment.rationale)}")
    else:
        for outcome in report.outcomes:
            assignment = outcome.assignment
            result = outcome.result
            review = outcome.review
            print(f"- {assignment.subtask.task_id} -> {assignment.worker_id} [{assignment.subtask.kind}]")
            print(f"  title: {assignment.subtask.title}")
            print(f"  routing score: {assignment.score:.2f}")
            print(f"  routing rationale: {', '.join(assignment.rationale)}")
            print(f"  result: {result.status} | confidence={result.confidence:.2f}")
            print(f"  deliverable: {result.deliverable or result.summary}")
            if result.changes_made:
                print(f"  changes: {', '.join(result.changes_made)}")
            if review:
                print(f"  review: {review.decision} | confidence={review.confidence:.2f}")
                print(f"  review summary: {review.summary}")
                if review.findings:
                    print(f"  findings: {'; '.join(review.findings)}")
    print("")
    print(f"Saved report: {report.report_path}")


def interactive_shell(agent) -> int:
    current_mode = "balanced"
    print("MVP Agent shell")
    print("Commands: /mode cheap|balanced|premium, /workers, /health, /ping quick|deep,")
    print("          /memory, /recall <query>, /skills [query], /evolve, /quit")
    while True:
        try:
            raw = input(f"MVP[{current_mode}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not raw:
            continue
        if raw in {"/quit", "/exit"}:
            return 0
        if raw.startswith("/mode "):
            requested = raw.split(" ", 1)[1].strip().lower()
            if requested not in {"cheap", "balanced", "premium"}:
                print("Unknown mode.")
                continue
            current_mode = requested
            print(f"Mode set to {current_mode}.")
            continue
        if raw == "/workers":
            print_workers(agent)
            continue
        if raw == "/health":
            print_health(agent)
            continue
        if raw == "/memory":
            if hasattr(agent, "memory_stats"):
                stats = agent.memory_stats()
                print(f"Memory store:")
                print(f"  Tasks remembered: {stats['total_tasks_remembered']}")
                print(f"  Workers tracked:  {stats['total_workers_tracked']}")
                if stats.get("top_workers"):
                    print("  Top workers:")
                    for w in stats["top_workers"][:5]:
                        print(f"    {w['worker_id']}: {w['calls']} calls, success_rate={w['success_rate']}")
                if stats.get("recent_patterns"):
                    print("  Recent patterns:")
                    for p in stats["recent_patterns"][:5]:
                        print(f"    {p}")
            else:
                print("Memory not available (use --legacy?).")
            continue
        if raw.startswith("/recall "):
            query = raw.split(" ", 1)[1].strip()
            if hasattr(agent, "memory"):
                tasks = agent.memory.recall_similar_tasks(query, limit=5)
                if tasks:
                    print(f"Recalled {len(tasks)} similar tasks:")
                    for t in tasks:
                        status_icon = "+" if t["status"] == "completed" else "~"
                        print(f"  {status_icon} [{t['profile']}] {t['task'][:100]}")
                else:
                    print("No similar tasks found in memory.")
            else:
                print("Memory not available (use --legacy?).")
            continue
        if raw.startswith("/skills"):
            query = raw.split(" ", 1)[1].strip() if " " in raw else ""
            skills = agent.list_skills(limit=20) if hasattr(agent, "list_skills") else []
            print(f"Skill mesh ({len(skills)} shown):")
            for row in skills:
                print(f"  - {row['skill_id']}: {row['summary']}")
            if query and hasattr(agent, "planner") and getattr(agent, "skill_mesh", None) is not None:
                profile = agent.planner.skill_mesh.profile_task(query, [], agent.worker_specs)
                print("  Required:")
                for item in profile.required_skill_ids:
                    print(f"    {item}")
                if profile.capability_gaps:
                    print("  Gaps:")
                    for item in profile.capability_gaps:
                        print(f"    {item}")
            continue
        if raw == "/evolve":
            if hasattr(agent, "evolution_status"):
                status = agent.evolution_status()
                print(f"Skills available: {status['skills_available']}")
                print(f"Core skill pack enabled: {status['core_skill_pack_enabled']}")
                print(f"Workers tracked: {status['worker_count']}")
                for item in status.get("top_capability_gaps", []):
                    print(f"  gap: {item['gap']} ({item['count']})")
            continue
        if raw.startswith("/ping"):
            parts = raw.split()
            ping_mode = parts[1].strip().lower() if len(parts) > 1 else "quick"
            if ping_mode not in {"quick", "deep"}:
                print("Unknown ping mode.")
                continue
            print_ping(agent, ping_mode)
            continue

        task_mode = detect_budget_mode(raw, current_mode)
        report = agent.run(task=raw, profile_name=task_mode, plan_only=False)
        print_report(report)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_global_args(argv))
    agent = _get_agent(args)

    if args.command == "workers":
        print_workers(agent)
        return 0
    if args.command == "health":
        print_health(agent)
        return 0
    if args.command == "ping":
        print_ping(agent, args.mode)
        return 0
    if args.command == "shell":
        return interactive_shell(agent)
    if args.command == "memory":
        if hasattr(agent, "memory"):
            if args.recall:
                tasks = agent.memory.recall_similar_tasks(args.recall, limit=args.recent)
                print(f"Recall results for '{args.recall}':")
                for t in tasks:
                    icon = "+" if t["status"] == "completed" else "~"
                    print(f"  {icon} [{t['profile']}] {t['task'][:120]}")
                    if t.get("leader_notes"):
                        print(f"     {t['leader_notes'][:150]}")
            elif args.workers:
                stats = agent.memory.all_worker_stats()
                for wid, s in sorted(stats.items()):
                    print(f"{wid}: {s['total_calls']} calls, success_rate={s['success_rate']}, avg={s['avg_duration_ms']}ms")
            else:
                stats = agent.memory_stats()
                print(f"Memory Store Summary:")
                print(f"  Tasks remembered:  {stats['total_tasks_remembered']}")
                print(f"  Workers tracked:   {stats['total_workers_tracked']}")
                if stats["top_workers"]:
                    print(f"  Top workers by call volume:")
                    for w in stats["top_workers"][:8]:
                        print(f"    {w['worker_id']}: {w['calls']} calls, success={w['success_rate']}")
                if stats.get("top_capability_gaps"):
                    print("  Frequent capability gaps:")
                    for item in stats["top_capability_gaps"][:8]:
                        print(f"    {item['gap']} ({item['count']})")
        else:
            print("Memory not available with --legacy mode.")
        return 0
    if args.command == "skills":
        skills = agent.list_skills(limit=args.limit) if hasattr(agent, "list_skills") else []
        print(f"Skill mesh catalog ({len(skills)} shown):")
        for row in skills:
            print(f"- {row['skill_id']}")
            print(f"  title: {row['title']}")
            print(f"  source: {row['source']}")
            print(f"  capabilities: {', '.join(row['capabilities'])}")
            print(f"  summary: {row['summary']}")
        if args.task and getattr(agent, "skill_mesh", None) is not None:
            profile = agent.skill_mesh.profile_task(args.task, [], agent.worker_specs)
            print("")
            print("Task profile:")
            print(f"  required skills: {', '.join(profile.required_skill_ids) or '-'}")
            print(f"  recommended skills: {', '.join(profile.recommended_skill_ids) or '-'}")
            if profile.capability_gaps:
                print("  capability gaps:")
                for item in profile.capability_gaps:
                    print(f"    - {item}")
        return 0
    if args.command == "evolve":
        if hasattr(agent, "evolution_status"):
            status = agent.evolution_status()
            print(f"Skills available: {status['skills_available']}")
            print(f"Core skill pack enabled: {status['core_skill_pack_enabled']}")
            print(f"Workers tracked: {status['worker_count']}")
            if status.get("top_capability_gaps"):
                print("Top capability gaps:")
                for item in status["top_capability_gaps"]:
                    print(f"  - {item['gap']} ({item['count']})")
        return 0
    if args.command == "smoke":
        result = agent.smoke_test()
        for row in result:
            state = "OK" if row["ok"] else "FAIL"
            print(f"{state:<4} {row['worker_id']}: {row['summary']} ({row['latency_ms']}ms)")
        return 0
    if args.command == "app":
        from .app import main as app_main

        forwarded = ["--config", args.config]
        if getattr(args, "self_test", False):
            forwarded.append("--self-test")
        return app_main(forwarded)

    task = read_task(args)
    mode = detect_budget_mode(task, getattr(args, "mode", None))
    plan_only = getattr(args, "plan_only", False) or args.command == "plan"
    report = agent.run(task=task, profile_name=mode, plan_only=plan_only)
    print_report(report)
    return 0
