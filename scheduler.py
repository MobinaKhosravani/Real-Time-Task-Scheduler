from __future__ import annotations

import argparse
import math
import sys
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk


TASK_COLORS = [
    "#4e79a7",
    "#f28e2b",
    "#59a14f",
    "#e15759",
    "#b07aa1",
    "#76b7b2",
    "#edc948",
    "#9c755f",
]

RESOURCE_COLORS = [
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#17becf",
    "#bcbd22",
    "#1f77b4",
]

IDLE_COLOR = "#dddddd"


@dataclass(frozen=True)
class CriticalSection:
    resource: str
    start_step: int
    end_step: int

    @property
    def length(self) -> int:
        return self.end_step - self.start_step + 1


@dataclass(frozen=True)
class Task:
    name: str
    wcet: int
    period: int
    deadline: int
    phase: int = 0
    critical_sections: tuple[CriticalSection, ...] = field(default_factory=tuple)
    order: int = 0


@dataclass
class Job:
    id: int
    task: Task
    release_time: int
    absolute_deadline: int
    remaining_time: int
    executed_steps: int = 0
    miss_recorded: bool = False

    @property
    def next_step(self) -> int:
        return self.executed_steps + 1


@dataclass(frozen=True)
class Slot:
    task_name: str = "IDLE"
    resource_name: str | None = None
    job_id: int | None = None


@dataclass
class Completion:
    task_name: str
    job_id: int
    release_time: int
    finish_time: int
    deadline: int

    @property
    def response_time(self) -> int:
        return self.finish_time - self.release_time

    @property
    def met_deadline(self) -> bool:
        return self.finish_time <= self.deadline


@dataclass
class ScheduleResult:
    timelines: list[list[Slot]]
    completions: list[Completion]
    deadline_misses: list[tuple[str, int, int]]
    busy_slots: int
    utilization: float
    resource_ceilings: dict[str, int]
    priority_rank: dict[str, int]
    blocking_terms: dict[str, int]


def parse_critical_sections(text: str, known_resources: set[str]) -> tuple[CriticalSection, ...]:
    """Accepts A:3-4 or A@3:2. Steps are one-based execution steps."""
    if not text.strip() or text.strip() == "-":
        return tuple()

    sections: list[CriticalSection] = []
    for raw_part in text.split(";"):
        part = raw_part.strip()
        if not part:
            continue

        if "@" in part:
            resource, rest = part.split("@", 1)
            start_text, length_text = rest.split(":", 1)
            start_step = int(start_text.strip())
            length = int(length_text.strip())
            end_step = start_step + length - 1
        else:
            resource, rest = part.split(":", 1)
            start_text, end_text = rest.split("-", 1)
            start_step = int(start_text.strip())
            end_step = int(end_text.strip())

        resource = resource.strip()
        if resource not in known_resources:
            raise ValueError(f"Resource '{resource}' is used but not defined.")
        if start_step < 1 or end_step < start_step:
            raise ValueError(f"Invalid critical-section range: {part}")
        sections.append(CriticalSection(resource, start_step, end_step))
    return tuple(sections)


def parse_resources(text: str) -> list[str]:
    resources: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if name in resources:
            raise ValueError(f"Duplicate resource name: {name}")
        resources.append(name)
    return resources


def parse_tasks(text: str, resources: list[str]) -> list[Task]:
    known_resources = set(resources)
    tasks: list[Task] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [part.strip() for part in stripped.split(",")]
        if len(parts) < 4:
            raise ValueError(f"Task line {line_no} must have at least name,C,T,D.")
        name = parts[0]
        wcet = int(parts[1])
        period = int(parts[2])
        deadline = int(parts[3])
        phase = int(parts[4]) if len(parts) >= 5 and parts[4] else 0
        cs_text = ",".join(parts[5:]).strip() if len(parts) >= 6 else ""
        if wcet <= 0 or period <= 0 or deadline <= 0 or phase < 0:
            raise ValueError(f"Invalid numeric values in task line {line_no}.")
        critical_sections = parse_critical_sections(cs_text, known_resources)
        for section in critical_sections:
            if section.end_step > wcet:
                raise ValueError(
                    f"{name} uses {section.resource} until step {section.end_step}, "
                    f"but C={wcet}."
                )
        tasks.append(
            Task(
                name=name,
                wcet=wcet,
                period=period,
                deadline=deadline,
                phase=phase,
                critical_sections=critical_sections,
                order=len(tasks),
            )
        )
    if not tasks:
        raise ValueError("At least one task is required.")
    if len({task.name for task in tasks}) != len(tasks):
        raise ValueError("Task names must be unique.")
    return tasks


def static_priority_key(task: Task, algorithm: str) -> tuple[int, int, int]:
    # PCP needs static ceilings. For EDF we use relative deadline order for ceilings.
    if algorithm == "RM":
        return (task.period, task.deadline, task.order)
    return (task.deadline, task.period, task.order)


def compute_priority_rank(tasks: list[Task], algorithm: str) -> dict[str, int]:
    ordered = sorted(tasks, key=lambda task: static_priority_key(task, algorithm))
    return {task.name: index + 1 for index, task in enumerate(ordered)}


def compute_resource_ceilings(
    tasks: list[Task],
    resources: list[str],
    priority_rank: dict[str, int],
) -> dict[str, int]:
    ceilings = {resource: math.inf for resource in resources}
    for task in tasks:
        for section in task.critical_sections:
            ceilings[section.resource] = min(ceilings[section.resource], priority_rank[task.name])
    return ceilings


def active_section(job: Job) -> CriticalSection | None:
    for section in job.task.critical_sections:
        if section.start_step <= job.next_step <= section.end_step:
            return section
    return None


def held_resources(job: Job, locks: dict[str, int]) -> list[str]:
    return [resource for resource, holder_id in locks.items() if holder_id == job.id]


def release_inactive_locks(job: Job, locks: dict[str, int]) -> None:
    active = active_section(job)
    for resource in list(locks):
        if locks[resource] == job.id and (active is None or active.resource != resource):
            del locks[resource]


def system_ceiling_for_other_jobs(
    job: Job,
    locks: dict[str, int],
    resource_ceilings: dict[str, int],
) -> float:
    other_locked_ceilings = [
        resource_ceilings[resource]
        for resource, holder_id in locks.items()
        if holder_id != job.id
    ]
    return min(other_locked_ceilings) if other_locked_ceilings else math.inf


def can_execute_under_pcp(
    job: Job,
    locks: dict[str, int],
    priority_rank: dict[str, int],
    resource_ceilings: dict[str, int],
) -> bool:
    job_priority = priority_rank[job.task.name]
    system_ceiling = system_ceiling_for_other_jobs(job, locks, resource_ceilings)

    # PCP execution rule: a job may preempt/execute only when its priority is
    # higher than the ceiling of every resource locked by other jobs. Locks held
    # by the same job are excluded so the lock holder can finish its section.
    if job_priority >= system_ceiling:
        return False

    section = active_section(job)
    if section is None:
        return True

    holder = locks.get(section.resource)
    if holder == job.id:
        return True
    if holder is not None and holder != job.id:
        return False

    return True


def acquire_if_needed(job: Job, locks: dict[str, int]) -> str | None:
    section = active_section(job)
    if section is not None:
        locks[section.resource] = job.id
        return section.resource
    return None


def job_sort_key(
    job: Job,
    algorithm: str,
    priority_rank: dict[str, int],
    resource_ceilings: dict[str, int],
    locks: dict[str, int],
) -> tuple[float, int, int, int]:
    inherited_rank = priority_rank[job.task.name]
    for resource in held_resources(job, locks):
        inherited_rank = min(inherited_rank, resource_ceilings[resource])

    if algorithm == "EDF":
        return (job.absolute_deadline, inherited_rank, job.release_time, job.task.order)
    return (inherited_rank, job.release_time, job.absolute_deadline, job.task.order)


def compute_blocking_terms(
    tasks: list[Task],
    priority_rank: dict[str, int],
    resource_ceilings: dict[str, int],
) -> dict[str, int]:
    blocking_terms: dict[str, int] = {}
    for task in tasks:
        task_priority = priority_rank[task.name]
        blocking = 0
        for lower_task in tasks:
            if priority_rank[lower_task.name] <= task_priority:
                continue
            for section in lower_task.critical_sections:
                if resource_ceilings[section.resource] <= task_priority:
                    blocking = max(blocking, section.length)
        blocking_terms[task.name] = blocking
    return blocking_terms


def simulate(
    tasks: list[Task],
    resources: list[str],
    cores: int,
    simulation_time: int,
    algorithm: str,
) -> ScheduleResult:
    priority_rank = compute_priority_rank(tasks, algorithm)
    resource_ceilings = compute_resource_ceilings(tasks, resources, priority_rank)
    blocking_terms = compute_blocking_terms(tasks, priority_rank, resource_ceilings)

    timelines = [[] for _ in range(cores)]
    ready: list[Job] = []
    locks: dict[str, int] = {}
    completions: list[Completion] = []
    deadline_misses: list[tuple[str, int, int]] = []
    busy_slots = 0
    next_job_id = 1

    for now in range(simulation_time):
        for task in tasks:
            if now >= task.phase and (now - task.phase) % task.period == 0:
                ready.append(
                    Job(
                        id=next_job_id,
                        task=task,
                        release_time=now,
                        absolute_deadline=now + task.deadline,
                        remaining_time=task.wcet,
                    )
                )
                next_job_id += 1

        for job in ready:
            if not job.miss_recorded and job.remaining_time > 0 and now >= job.absolute_deadline:
                job.miss_recorded = True
                deadline_misses.append((job.task.name, job.id, now))

        selected: list[Job] = []
        tentative_locks = locks.copy()
        for _core in range(cores):
            candidates = sorted(
                [job for job in ready if job.remaining_time > 0 and job not in selected],
                key=lambda job: job_sort_key(
                    job,
                    algorithm,
                    priority_rank,
                    resource_ceilings,
                    tentative_locks,
                ),
            )
            chosen = None
            for job in candidates:
                if can_execute_under_pcp(job, tentative_locks, priority_rank, resource_ceilings):
                    chosen = job
                    break
            if chosen is not None:
                selected.append(chosen)
                section = active_section(chosen)
                if section is not None:
                    tentative_locks[section.resource] = chosen.id

        for core_index in range(cores):
            if core_index >= len(selected):
                timelines[core_index].append(Slot())
                continue
            job = selected[core_index]
            resource = acquire_if_needed(job, locks)
            timelines[core_index].append(Slot(job.task.name, resource, job.id))
            job.executed_steps += 1
            job.remaining_time -= 1
            busy_slots += 1

        for job in selected:
            release_inactive_locks(job, locks)
            if job.remaining_time == 0:
                # A completed job releases all locks immediately.
                for resource in list(locks):
                    if locks[resource] == job.id:
                        del locks[resource]
                completions.append(
                    Completion(
                        task_name=job.task.name,
                        job_id=job.id,
                        release_time=job.release_time,
                        finish_time=now + 1,
                        deadline=job.absolute_deadline,
                    )
                )

        ready = [job for job in ready if job.remaining_time > 0]

    for job in ready:
        if not job.miss_recorded and simulation_time >= job.absolute_deadline:
            deadline_misses.append((job.task.name, job.id, simulation_time))

    total_slots = simulation_time * cores
    return ScheduleResult(
        timelines=timelines,
        completions=completions,
        deadline_misses=deadline_misses,
        busy_slots=busy_slots,
        utilization=busy_slots / total_slots if total_slots else 0.0,
        resource_ceilings=resource_ceilings,
        priority_rank=priority_rank,
        blocking_terms=blocking_terms,
    )


def utilization(tasks: list[Task]) -> float:
    return sum(task.wcet / task.period for task in tasks)


def lcm(values: list[int]) -> int:
    result = 1
    for value in values:
        result = result * value // math.gcd(result, value)
    return result


def liu_layland_bound(task_count: int) -> float:
    return task_count * (2 ** (1 / task_count) - 1)


def demand_bound_rows(tasks: list[Task], limit: int, processors: int = 1) -> list[tuple[int, int, int, bool]]:
    points = {0}
    for task in tasks:
        k = 0
        while k * task.period + task.deadline <= limit:
            points.add(k * task.period + task.deadline)
            k += 1
    rows = []
    for t in sorted(points):
        demand = 0
        for task in tasks:
            jobs_due = max(0, math.floor((t - task.deadline) / task.period) + 1)
            demand += jobs_due * task.wcet
        capacity = processors * t
        rows.append((t, demand, capacity, demand <= capacity))
    return rows


def response_time_analysis(
    tasks: list[Task],
    priority_rank: dict[str, int],
    blocking_terms: dict[str, int],
) -> list[dict[str, object]]:
    ordered = sorted(tasks, key=lambda task: priority_rank[task.name])
    rows: list[dict[str, object]] = []
    for index, task in enumerate(ordered):
        higher_priority = ordered[:index]
        blocking = blocking_terms[task.name]
        current = task.wcet + blocking
        iterations = [current]
        converged = True
        for _ in range(100):
            interference = sum(
                math.ceil(current / hp_task.period) * hp_task.wcet
                for hp_task in higher_priority
            )
            next_value = task.wcet + blocking + interference
            iterations.append(next_value)
            if next_value == current:
                break
            if next_value > 10_000:
                converged = False
                current = next_value
                break
            current = next_value
        rows.append(
            {
                "task": task.name,
                "hp": ", ".join(hp.name for hp in higher_priority) or "-",
                "blocking": blocking,
                "iterations": iterations,
                "response": current,
                "deadline": task.deadline,
                "ok": converged and current <= task.deadline,
            }
        )
    return rows


def exact_response_rows(tasks: list[Task], result: ScheduleResult) -> list[dict[str, object]]:
    rows = []
    for task in tasks:
        completions = [item for item in result.completions if item.task_name == task.name]
        responses = [item.response_time for item in completions]
        rows.append(
            {
                "task": task.name,
                "jobs": len(completions),
                "max_response": max(responses) if responses else None,
                "deadline": task.deadline,
                "responses": responses,
                "ok": all(item.met_deadline for item in completions),
            }
        )
    return rows


def build_analysis_text(
    tasks: list[Task],
    resources: list[str],
    cores: int,
    simulation_time: int,
    algorithm: str,
    result: ScheduleResult,
) -> str:
    u = utilization(tasks)
    ll_bound = liu_layland_bound(len(tasks))
    hyper = min(lcm([task.period for task in tasks]), simulation_time)
    dbf = demand_bound_rows(tasks, hyper, cores)
    rta = response_time_analysis(tasks, result.priority_rank, result.blocking_terms)
    exact = exact_response_rows(tasks, result)

    lines = [
        "===== Input Summary =====",
        f"Algorithm: {algorithm}",
        f"Cores: {cores}",
        f"Simulation time: {simulation_time}",
        f"Resources: {', '.join(resources) if resources else '-'}",
        "",
        "===== Necessary and Sufficient Bounds =====",
        f"U = sum(C_i/T_i) = {u:.4f} ({100 * u:.2f}%)",
        f"Necessary utilization bound for m cores: U <= m -> {u:.4f} <= {cores} : {'PASS' if u <= cores else 'FAIL'}",
        f"RMA Liu-Layland sufficient bound for n={len(tasks)}: n(2^(1/n)-1) = {ll_bound:.4f}",
        f"RMA sufficient test on one core: U <= bound -> {'PASS' if u <= ll_bound else 'NO GUARANTEE'}",
        f"EDF demand-bound check shown up to t={hyper}; condition dbf(t) <= m*t.",
        "",
        "===== Priority Ceiling Protocol =====",
        "Priority rank: " + ", ".join(f"{name}:{rank}" for name, rank in result.priority_rank.items()),
        "Resource ceilings: "
        + (
            ", ".join(
                f"{resource}:{'unused' if math.isinf(ceiling) else ceiling}"
                for resource, ceiling in result.resource_ceilings.items()
            )
            if result.resource_ceilings
            else "-"
        ),
        "Blocking terms B_i: " + ", ".join(f"{task}:{b}" for task, b in result.blocking_terms.items()),
        "",
        "===== Exact Response Times from Simulation =====",
        "Task | Completed Jobs | Max Exact R | D | All R values | Result",
    ]

    for row in exact:
        max_r = "-" if row["max_response"] is None else row["max_response"]
        result_label = "OK" if row["ok"] else "MISS"
        lines.append(
            f"{row['task']} | {row['jobs']} | {max_r} | {row['deadline']} | {row['responses']} | {result_label}"
        )

    lines.extend(
        [
            "",
            "===== Response-Time Analysis with Blocking =====",
            "Task | hp(i) | B_i | iterations | R_i | D_i | Result",
        ]
    )
    for row in rta:
        result_label = "Schedulable" if row["ok"] else "Not guaranteed"
        iteration_text = " -> ".join(str(item) for item in row["iterations"])
        lines.append(
            f"{row['task']} | {row['hp']} | {row['blocking']} | {iteration_text} | "
            f"{row['response']} | {row['deadline']} | {result_label}"
        )

    lines.extend(
        [
            "",
            "===== Demand Bound Function =====",
            "t | dbf(t) | m*t | Result",
        ]
    )
    for t, demand, capacity, ok in dbf:
        lines.append(f"{t} | {demand} | {capacity} | {'OK' if ok else 'FAIL'}")

    lines.extend(
        [
            "",
            "===== Simulation Summary =====",
            f"Completed jobs: {len(result.completions)}",
            f"Deadline misses: {len(result.deadline_misses)}",
            f"Busy slots: {result.busy_slots}/{simulation_time * cores}",
            f"Observed CPU utilization: {100 * result.utilization:.2f}%",
        ]
    )
    if result.deadline_misses:
        lines.append("Miss log: " + ", ".join(f"{name}#{job_id}@t={time}" for name, job_id, time in result.deadline_misses))
    return "\n".join(lines)


def merge_slots(slots: list[Slot]) -> list[tuple[int, int, Slot]]:
    if not slots:
        return []
    segments: list[tuple[int, int, Slot]] = []
    start = 0
    current = slots[0]
    for index, slot in enumerate(slots[1:], start=1):
        if slot.task_name != current.task_name or slot.resource_name != current.resource_name:
            segments.append((start, index - start, current))
            start = index
            current = slot
    segments.append((start, len(slots) - start, current))
    return segments


def plot_gantt(
    tasks: list[Task],
    resources: list[str],
    result: ScheduleResult,
    algorithm: str,
    simulation_time: int,
    save_path: str | Path | None = None,
    show: bool = True,
) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    task_colors = {task.name: TASK_COLORS[index % len(TASK_COLORS)] for index, task in enumerate(tasks)}
    resource_colors = {
        resource: RESOURCE_COLORS[index % len(RESOURCE_COLORS)]
        for index, resource in enumerate(resources)
    }

    cores = len(result.timelines)
    fig_height = max(3.2, 1.2 + cores * 0.85)
    fig, ax = plt.subplots(figsize=(14, fig_height))

    for core_index, timeline in enumerate(result.timelines):
        y = cores - core_index
        for start, duration, slot in merge_slots(timeline):
            if slot.task_name == "IDLE":
                color = IDLE_COLOR
                label = "IDLE"
                edge = "white"
            elif slot.resource_name is not None:
                color = resource_colors[slot.resource_name]
                label = f"{slot.task_name}\nCS:{slot.resource_name}"
                edge = "black"
            else:
                color = task_colors[slot.task_name]
                label = slot.task_name
                edge = "black"

            ax.broken_barh(
                [(start, duration)],
                (y - 0.36, 0.72),
                facecolors=color,
                edgecolors=edge,
                linewidth=0.8,
            )
            if slot.task_name != "IDLE" and duration >= 1:
                ax.text(
                    start + duration / 2,
                    y,
                    label,
                    ha="center",
                    va="center",
                    fontsize=8,
                    color="white",
                )

    ax.set_xlim(0, simulation_time)
    ax.set_ylim(0.4, cores + 0.7)
    ax.set_xlabel("Time")
    ax.set_ylabel("Cores")
    ax.set_yticks(range(1, cores + 1))
    ax.set_yticklabels([f"Core {index}" for index in range(cores, 0, -1)])
    ax.set_title(f"{algorithm} Scheduling with Priority Ceiling Protocol")
    ax.grid(axis="x", linestyle="--", alpha=0.35)

    legend_items = [Patch(facecolor=color, edgecolor="black", label=f"Task {name}") for name, color in task_colors.items()]
    legend_items += [
        Patch(facecolor=color, edgecolor="black", label=f"Critical Section: {name}")
        for name, color in resource_colors.items()
    ]
    legend_items.append(Patch(facecolor=IDLE_COLOR, edgecolor="white", label="IDLE"))
    ax.legend(handles=legend_items, loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=3)
    fig.tight_layout()

    if save_path is not None:
        fig.savefig(save_path, dpi=160, bbox_inches="tight")
    if show:
        plt.show()
    else:
        plt.close(fig)


EXAMPLE_RESOURCES = """A
B"""

EXAMPLE_TASKS = """# name,C,T,D,phase,critical sections
T1,4,10,8,0,A:3-4
T2,2,7,7,0,-
T3,3,12,10,0,B:2-3"""


class SchedulerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Real-Time Scheduler with PCP")
        self.root.geometry("980x760")

        self.simulation_time = tk.StringVar(value="30")
        self.cores = tk.StringVar(value="2")
        self.algorithm = tk.StringVar(value="RM")

        self._build_ui()

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        top = ttk.LabelFrame(main, text="Processing Environment", padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Simulation time").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.simulation_time, width=10).grid(row=0, column=1, padx=8)

        ttk.Label(top, text="Cores").grid(row=0, column=2, sticky="w")
        ttk.Entry(top, textvariable=self.cores, width=10).grid(row=0, column=3, padx=8)

        ttk.Label(top, text="Algorithm").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            top,
            textvariable=self.algorithm,
            values=["RM", "EDF"],
            state="readonly",
            width=10,
        ).grid(row=0, column=5, padx=8)

        input_area = ttk.Frame(main)
        input_area.pack(fill=tk.BOTH, expand=False, pady=10)

        resource_box = ttk.LabelFrame(input_area, text="Resources: one name per line", padding=8)
        resource_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        self.resource_text = tk.Text(resource_box, height=8, width=28)
        self.resource_text.pack(fill=tk.BOTH, expand=True)
        self.resource_text.insert("1.0", EXAMPLE_RESOURCES)

        task_box = ttk.LabelFrame(
            input_area,
            text="Task Profile: name,C,T,D,phase,CS   |   CS examples: A:3-4 or A@3:2",
            padding=8,
        )
        task_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))
        self.task_text = tk.Text(task_box, height=8, width=70)
        self.task_text.pack(fill=tk.BOTH, expand=True)
        self.task_text.insert("1.0", EXAMPLE_TASKS)

        buttons = ttk.Frame(main)
        buttons.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(buttons, text="Load Example", command=self.load_example).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Schedule", command=self.schedule).pack(side=tk.LEFT, padx=8)

        output_box = ttk.LabelFrame(main, text="Analysis Output", padding=8)
        output_box.pack(fill=tk.BOTH, expand=True)
        self.output_text = tk.Text(output_box, height=24, wrap=tk.NONE)
        y_scroll = ttk.Scrollbar(output_box, orient=tk.VERTICAL, command=self.output_text.yview)
        x_scroll = ttk.Scrollbar(output_box, orient=tk.HORIZONTAL, command=self.output_text.xview)
        self.output_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        output_box.rowconfigure(0, weight=1)
        output_box.columnconfigure(0, weight=1)

    def load_example(self) -> None:
        self.simulation_time.set("30")
        self.cores.set("2")
        self.algorithm.set("RM")
        self.resource_text.delete("1.0", tk.END)
        self.resource_text.insert("1.0", EXAMPLE_RESOURCES)
        self.task_text.delete("1.0", tk.END)
        self.task_text.insert("1.0", EXAMPLE_TASKS)

    def schedule(self) -> None:
        try:
            simulation_time = int(self.simulation_time.get())
            cores = int(self.cores.get())
            if simulation_time <= 0 or cores <= 0:
                raise ValueError("Simulation time and cores must be positive.")
            algorithm = self.algorithm.get()
            resources = parse_resources(self.resource_text.get("1.0", tk.END))
            tasks = parse_tasks(self.task_text.get("1.0", tk.END), resources)
            result = simulate(tasks, resources, cores, simulation_time, algorithm)
            analysis = build_analysis_text(tasks, resources, cores, simulation_time, algorithm, result)

            self.output_text.delete("1.0", tk.END)
            self.output_text.insert("1.0", analysis)
            plot_gantt(tasks, resources, result, algorithm, simulation_time, show=True)
        except Exception as exc:
            messagebox.showerror("Input error", str(exc))

    def run(self) -> None:
        self.root.mainloop()


def run_demo(no_gui: bool) -> None:
    if no_gui:
        import matplotlib

        matplotlib.use("Agg")

    resources = parse_resources(EXAMPLE_RESOURCES)
    tasks = parse_tasks(EXAMPLE_TASKS, resources)
    result = simulate(tasks, resources, cores=2, simulation_time=30, algorithm="RM")
    analysis = build_analysis_text(tasks, resources, 2, 30, "RM", result)
    output_path = Path(__file__).with_name("demo_output.txt")
    plot_path = Path(__file__).with_name("demo_gantt.png")
    output_path.write_text(analysis, encoding="utf-8")
    plot_gantt(tasks, resources, result, "RM", 30, save_path=plot_path, show=not no_gui)
    print(analysis)
    print(f"\nWrote {output_path}")
    print(f"Wrote {plot_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-time scheduling diagram app with PCP.")
    parser.add_argument("--demo", action="store_true", help="Run the built-in example.")
    parser.add_argument("--no-gui", action="store_true", help="Do not open windows; useful for testing.")
    args = parser.parse_args(argv)

    if args.demo:
        run_demo(args.no_gui)
        return 0
    if args.no_gui:
        print("--no-gui is only useful with --demo.", file=sys.stderr)
        return 2
    SchedulerApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
