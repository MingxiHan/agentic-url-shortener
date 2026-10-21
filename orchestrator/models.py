"""Structured model outputs and deterministic graph validation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirement(StrictModel):
    intent: str = Field(min_length=1, max_length=2000)
    acceptance: list[str] = Field(min_length=1, max_length=15)
    assumptions: list[str] = Field(max_length=10)
    questions: list[str] = Field(max_length=3)
    alias_min: int | None = Field(default=None, ge=4, le=32)
    alias_max: int | None = Field(default=None, ge=4, le=32)

    @model_validator(mode="after")
    def valid_alias_bounds(self):
        if (self.alias_min is None) != (self.alias_max is None):
            raise ValueError("Alias bounds must be supplied together")
        if self.alias_min is not None and self.alias_min > self.alias_max:
            raise ValueError("Alias minimum must not exceed maximum")
        return self


class Task(StrictModel):
    id: str = Field(pattern=r"^task_[a-z0-9_]{1,40}$")
    kind: Literal["implementation", "tests"]
    description: str = Field(min_length=1, max_length=3000)
    dependencies: list[str] = Field(max_length=4)


class Plan(StrictModel):
    rationale: str = Field(min_length=1, max_length=4000)
    impacted_paths: list[str] = Field(max_length=30)
    risks: list[str] = Field(max_length=10)
    tasks: list[Task] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def valid_graph(self):
        tasks = {task.id: task for task in self.tasks}
        if len(tasks) != len(self.tasks):
            raise ValueError("Duplicate task IDs")
        implementations = [task.id for task in self.tasks if task.kind == "implementation"]
        tests = [task for task in self.tasks if task.kind == "tests"]
        if not implementations or len(tests) != 1:
            raise ValueError("Plan needs one to three implementation tasks and exactly one test task")
        if tests[0].dependencies:
            raise ValueError("Test authoring must be independent of implementation")
        visited, active = set(), set()

        def visit(task_id):
            if task_id in active:
                raise ValueError("Dependency cycle")
            if task_id in visited:
                return
            if task_id not in tasks:
                raise ValueError("Unknown dependency")
            active.add(task_id)
            for parent in tasks[task_id].dependencies:
                visit(parent)
            active.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)

        def ancestors(task_id):
            return set(tasks[task_id].dependencies).union(
                *(ancestors(parent) for parent in tasks[task_id].dependencies)
            )

        for a in implementations:
            if tests[0].id in ancestors(a):
                raise ValueError("Implementation must not depend on generated tests")
            for b in implementations:
                if a != b and a not in ancestors(b) and b not in ancestors(a):
                    raise ValueError("Implementation writers must be ordered")
        return self


class FileChange(StrictModel):
    path: str
    previous_hash: str | None
    content: str = Field(max_length=262144)


class ChangeSet(StrictModel):
    rationale: str = Field(min_length=1, max_length=4000)
    changes: list[FileChange] = Field(min_length=1, max_length=20)


class Finding(StrictModel):
    severity: Literal["blocking", "advisory"]
    path: str
    evidence: str
    recommendation: str


class Review(StrictModel):
    summary: str
    findings: list[Finding] = Field(max_length=10)


class Documentation(StrictModel):
    readme: str = Field(min_length=1, max_length=20000)
    rationale: str = Field(min_length=1, max_length=8000)
    limitations: list[str] = Field(max_length=15)
