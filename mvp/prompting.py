from __future__ import annotations

import json

from .models import Assignment, Subtask, WorkerResult


def worker_roster_prompt_lines(roster: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for worker_id, capabilities in roster.items():
        lines.append(f"- {worker_id}: {', '.join(capabilities)}")
    return "\n".join(lines)


def build_planner_prompt(
    task: str,
    mode: str,
    roster: dict[str, list[str]],
    max_subtasks_hint: int = 3,
    code_task: bool = False,
    repo_hints: list[str] | None = None,
) -> str:
    schema = {
        "summary": "One-sentence leader understanding of the task.",
        "execution_strategy": "How the leader should sequence work and why.",
        "subtasks": [
            {
                "task_id": "S1",
                "title": "Short title",
                "goal": "What this subtask must accomplish",
                "kind": "architecture|design|coding|testing|review|research|documentation|ops|coordination|security|performance",
                "difficulty": 1,
                "repeatability": 1,
                "risk": 1,
                "needs_write_access": False,
                "preferred_capabilities": ["planning"],
                "acceptance_criteria": ["Concrete pass/fail check"],
                "depends_on": [],
                "target_files": ["Optional path or module"],
                "validation_steps": ["Optional check or command"],
                "parallel_group": "Optional shared id for subtasks that can fan out in parallel after the same dependency gate",
                "required_skills": ["Optional internal or external skill id"],
                "coordination_notes": ["Optional execution boundary or handoff note"],
                "notes": "Anything useful for routing",
            }
        ],
    }

    code_rules = ""
    if code_task:
        code_rules = f"""
Code-task planning rules:
- Prefer a real engineering sequence when useful: architecture/impact scan -> implementation -> verification.
- Include at least one write-capable coding subtask when the user clearly wants code changes.
- Use `target_files` for likely files/modules/packages. If unknown, list the most probable touch points.
- Use `validation_steps` for checks the assignee should run or reason about.
- Use `depends_on` when a later subtask should wait for an earlier one.
- If implementation spans multiple disjoint files or modules, prefer multiple parallel-ready subtasks that share the same dependency gate.
- Do not merge architecture, coding, and verification into one subtask unless the task is trivial.
""".strip()

    repo_hint_block = ""
    if repo_hints:
        repo_lines = "\n".join(f"- {item}" for item in repo_hints)
        repo_hint_block = f"""
Likely repository touch points:
{repo_lines}

Use these as grounded hints when choosing `target_files`. Prefer real nearby files over invented paths.
""".strip()

    return f"""
You are MVP, the leader and decision-maker of a multi-agent AI team.
You do not do the actual work. You break the task into the smallest high-value delegatable subtasks.

Current operating mode: {mode}
Worker roster:
{worker_roster_prompt_lines(roster)}

Planning rules:
- Prefer 1 to {max_subtasks_hint} subtasks.
- Collapse low-value micro-steps. Only split when routing, safety, or quality materially improves.
- Make each subtask delegatable to one worker.
- Mark needs_write_access true only when the worker must actually change files or state in the current workspace.
- Difficulty, repeatability, and risk are integers from 1 to 5.
- If the user clearly wants low API spend, keep simple repetitive work local.
- Include acceptance criteria that a reviewer can check.
{code_rules}
{repo_hint_block}

Return JSON only — no markdown fences, no commentary, no trailing commas. Every key must be present. Every subtask must have a non-empty task_id, title, and goal.
Target JSON shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}

User task:
{task}
""".strip()


def build_execution_prompt(
    assignment: Assignment,
    mode: str,
    upstream_results: dict[str, dict[str, object]] | None = None,
    previous_attempt: dict[str, object] | None = None,
) -> str:
    subtask = assignment.subtask
    criteria_text = "\n".join(f"- {item}" for item in subtask.acceptance_criteria) or "- No explicit criteria provided."
    dependency_text = ", ".join(subtask.depends_on) if subtask.depends_on else "None"
    target_files_text = "\n".join(f"- {item}" for item in subtask.target_files) or "- Not specified"
    validation_text = "\n".join(f"- {item}" for item in subtask.validation_steps) or "- Not specified"
    required_skills_text = "\n".join(f"- {item}" for item in subtask.required_skills) or "- None"
    coordination_text = "\n".join(f"- {item}" for item in subtask.coordination_notes) or "- None"
    parallel_group = subtask.parallel_group or "None"
    schema = {
        "status": "completed|partial|blocked",
        "summary": "What was done",
        "deliverable": "Main output or conclusion",
        "artifacts": ["Relevant files, commands, or outputs"],
        "changes_made": ["Files changed or actions taken"],
        "risks": ["Remaining concern"],
        "recommended_next_steps": ["Optional follow-up"],
        "confidence": 0.0,
    }

    upstream_block = ""
    if upstream_results:
        lines = []
        for dep_id in subtask.depends_on:
            if dep_id in upstream_results:
                ur = upstream_results[dep_id]
                lines.append(
                    f"- {dep_id}: status={ur.get('status')}, summary={ur.get('summary', '')}, "
                    f"deliverable={ur.get('deliverable', '')}, changes_made={json.dumps(ur.get('changes_made', []))}"
                )
        if lines:
            upstream_block = (
                "Results from upstream subtasks that this subtask depends on:\n"
                + "\n".join(lines)
                + "\n\nUse these results to avoid duplicating work and to build on what was already done.\n"
            )

    previous_block = ""
    if previous_attempt:
        prev_worker = previous_attempt.get("worker_id", "unknown")
        prev_result = previous_attempt.get("result_summary", "")
        prev_findings = previous_attempt.get("review_findings", [])
        findings_text = "\n".join(f"- {f}" for f in prev_findings) if prev_findings else "- None"
        previous_block = (
            f"Previous attempt by {prev_worker}:\n"
            f"  Result: {prev_result}\n"
            f"  Review findings:\n{findings_text}\n\n"
            "Address the reviewer's findings. Do not repeat the same mistakes.\n"
        )

    return f"""
You are one specialist inside the MVP team.
The leader has already routed this subtask to you. Do not redesign the whole program. Execute only this subtask.

Operating mode: {mode}
Assigned worker id: {assignment.worker_id}
Subtask id: {subtask.task_id}
Title: {subtask.title}
Kind: {subtask.kind}
Goal: {subtask.goal}
Depends on: {dependency_text}
Parallel group: {parallel_group}
Needs current-workspace write access: {str(subtask.needs_write_access).lower()}
Likely target files or modules:
{target_files_text}
Acceptance criteria:
{criteria_text}
Validation steps:
{validation_text}
Required skills / playbooks:
{required_skills_text}
Coordination notes:
{coordination_text}
Notes: {subtask.notes or 'None'}
{upstream_block}{previous_block}
Hard rules:
- If current-workspace write access is false, do not claim to have created, edited, or deleted any files.
- If you only produced analysis or text, keep changes_made as an empty list.
- If this is a coding task, be explicit about the concrete files, modules, or surfaces you touched.
- If you could not perform a validation step, say so plainly in risks or recommended_next_steps.
- Be concrete and truthful about what you actually did.

Return JSON only.
Target JSON shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()


def build_review_prompt(subtask: Subtask, result: WorkerResult) -> str:
    criteria_text = "\n".join(f"  - {item}" for item in subtask.acceptance_criteria) or "  - No explicit criteria provided."
    target_files_text = "\n".join(f"  - {item}" for item in subtask.target_files) or "  - Not specified"
    validation_text = "\n".join(f"  - {item}" for item in subtask.validation_steps) or "  - Not specified"
    required_skills_text = "\n".join(f"  - {item}" for item in subtask.required_skills) or "  - None"
    coordination_text = "\n".join(f"  - {item}" for item in subtask.coordination_notes) or "  - None"
    parallel_group = subtask.parallel_group or "None"
    schema = {
        "decision": "pass|revise|fail",
        "summary": "Short acceptance verdict",
        "findings": ["Specific issue or reason for approval"],
        "confidence": 0.0,
    }
    return f"""
You are the acceptance reviewer for MVP, the leader layer.
Judge whether this delegated subtask satisfies the required outcome.

Subtask:
- id: {subtask.task_id}
- title: {subtask.title}
- goal: {subtask.goal}
- kind: {subtask.kind}
- depends_on: {json.dumps(subtask.depends_on, ensure_ascii=False)}
- parallel_group: {parallel_group}
- needs_write_access: {str(subtask.needs_write_access).lower()}
- target_files:
{target_files_text}
- acceptance criteria:
{criteria_text}
- validation_steps:
{validation_text}
- required_skills:
{required_skills_text}
- coordination_notes:
{coordination_text}

Worker result:
- status: {result.status}
- summary: {result.summary}
- deliverable: {result.deliverable}
- artifacts: {json.dumps(result.artifacts, ensure_ascii=False)}
- changes_made: {json.dumps(result.changes_made, ensure_ascii=False)}
- risks: {json.dumps(result.risks, ensure_ascii=False)}
- recommended_next_steps: {json.dumps(result.recommended_next_steps, ensure_ascii=False)}
- confidence: {result.confidence}

Acceptance guardrails:
- If needs_write_access is false, any claimed file creation or file modification should normally fail review unless the claim is clearly only hypothetical.
- Reward honest "analysis only" outputs over fictional implementation claims.
- For coding or testing tasks, missing target files, missing concrete verification, or vague change descriptions should normally trigger revise or fail.
- If a coding task claims success but changes_made is empty and no plausible reason is given, do not pass it.

Return JSON only.
Target JSON shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()


def build_synthesis_prompt(
    task: str,
    profile_name: str,
    outcomes: list[dict[str, object]],
) -> str:
    outcome_lines: list[str] = []
    for i, oc in enumerate(outcomes, start=1):
        title = oc.get("title", f"Subtask {i}")
        worker = oc.get("worker_id", "unknown")
        status = oc.get("status", "unknown")
        summary = oc.get("summary", "")
        deliverable = oc.get("deliverable", "")
        review_decision = oc.get("review_decision", "")
        outcome_lines.append(
            f"  {i}. [{title}] worker={worker} status={status} review={review_decision}\n"
            f"     Summary: {summary}\n"
            f"     Deliverable: {deliverable}"
        )
    outcomes_text = "\n".join(outcome_lines)
    schema = {
        "synthesis": "Consolidated final answer that weaves together all worker outputs into one coherent response for the user.",
        "key_findings": ["The most important conclusions across all subtasks"],
        "gaps": ["Anything still missing or uncertain"],
        "confidence": 0.0,
    }
    return f"""
You are MVP, the leader of a multi-agent AI team.
Your workers have completed their assigned subtasks. Now you must synthesize their outputs into a single coherent final answer for the user.

Original task: {task}
Operating mode: {profile_name}

Worker outcomes:
{outcomes_text}

Synthesis rules:
- Integrate findings across workers. Do not just list them.
- Highlight agreements and resolve any contradictions.
- If something is missing or uncertain, state it in gaps.
- Write in the user's language. Be direct and actionable.
- The synthesis should read as one unified response, not a collection of separate answers.

Return JSON only.
Target JSON shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()


def build_replan_prompt(
    failed_subtask: Subtask,
    failure_reason: str,
    review_findings: list[str],
    previous_results: list[dict[str, object]],
    roster: dict[str, list[str]],
) -> str:
    findings_text = "\n".join(f"- {f}" for f in review_findings) if review_findings else "- None"
    schema = {
        "summary": "Why the original subtask failed and how it will be split.",
        "subtasks": [
            {
                "task_id": "RX_1",
                "title": "Smaller subtask title",
                "goal": "Narrower goal",
                "kind": "coding|review|research|documentation|testing|coordination|security|performance",
                "difficulty": 1,
                "repeatability": 1,
                "risk": 1,
                "needs_write_access": False,
                "preferred_capabilities": ["planning"],
                "acceptance_criteria": ["Concrete pass/fail check"],
                "depends_on": [],
                "target_files": ["Optional path"],
                "validation_steps": ["Optional check"],
                "notes": "Replanned from failed subtask",
            }
        ],
    }
    return f"""
You are MVP, the leader of a multi-agent AI team.
A subtask has repeatedly failed. You must re-decompose it into smaller, more focused subtasks.

Original subtask:
- id: {failed_subtask.task_id}
- title: {failed_subtask.title}
- goal: {failed_subtask.goal}
- kind: {failed_subtask.kind}
- difficulty: {failed_subtask.difficulty}
- risk: {failed_subtask.risk}
- needs_write_access: {failed_subtask.needs_write_access}
- acceptance_criteria: {json.dumps(failed_subtask.acceptance_criteria, ensure_ascii=False)}
- target_files: {json.dumps(failed_subtask.target_files, ensure_ascii=False)}

Failure reason: {failure_reason}
Review findings:
{findings_text}

Previous attempts:
{json.dumps(previous_results, ensure_ascii=False, indent=2)}

Worker roster:
{worker_roster_prompt_lines(roster)}

Replanning rules:
- Split into 2-3 smaller subtasks that are each independently delegatable.
- Each new subtask should be narrower in scope than the original.
- Address the specific failure reason and review findings.
- Prefer read-only analysis before write attempts when the failure suggests wrong edits.
- Keep difficulty <= original difficulty, risk <= original risk.

Return JSON only.
Target JSON shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()
