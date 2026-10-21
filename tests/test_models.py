"""Model output validation protects workflow topology and tool authority."""

import unittest

from pydantic import ValidationError

from orchestrator.models import ChangeSet, Plan, Requirement, Task


def task(identifier, kind="implementation", dependencies=()):
    return {"id": identifier, "kind": kind, "description": "Implement the approved contract", "dependencies": list(dependencies)}


def plan(tasks, **extra):
    return Plan.model_validate({"rationale": "Small independent implementation and test branches", "impacted_paths": ["service/shortener/app.py"], "risks": [], "tasks": tasks, **extra})


class ModelTests(unittest.TestCase):
    def test_requirement_alias_bounds_are_optional_paired_ordered_and_bounded(self):
        fields = {"intent": "Add custom aliases", "acceptance": ["Respect the requested range"], "assumptions": [], "questions": []}
        self.assertIsNone(Requirement(**fields).alias_min)
        self.assertEqual(Requirement(**fields, alias_min=8, alias_max=8).alias_max, 8)
        for bounds in ({"alias_min": 8}, {"alias_max": 8}, {"alias_min": 3, "alias_max": 8},
                       {"alias_min": 4, "alias_max": 33}, {"alias_min": 9, "alias_max": 8}):
            with self.subTest(bounds=bounds), self.assertRaises(ValidationError):
                Requirement(**fields, **bounds)

    def test_independent_implementation_and_test_branches_are_valid(self):
        result = plan([task("task_build"), task("task_tests", "tests")])
        self.assertEqual([item.dependencies for item in result.tasks], [[], []])

    def test_sequential_writers_and_independent_tests_are_valid(self):
        result = plan([task("task_storage"), task("task_routes", dependencies=["task_storage"]), task("task_stats", dependencies=["task_routes"]), task("task_tests", "tests")])
        self.assertEqual(len(result.tasks), 4)

    def test_cycles_and_self_dependencies_are_rejected(self):
        invalid = [
            [task("task_build", dependencies=["task_build"]), task("task_tests", "tests")],
            [task("task_first", dependencies=["task_second"]), task("task_second", dependencies=["task_first"]), task("task_tests", "tests")],
        ]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaisesRegex(ValidationError, "cycle"):
                plan(tasks)

    def test_unknown_dependencies_and_duplicate_ids_are_rejected(self):
        invalid = [
            [task("task_build", dependencies=["task_missing"]), task("task_tests", "tests")],
            [task("task_build"), task("task_build", "tests")],
        ]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaises(ValidationError):
                plan(tasks)

    def test_parallel_implementation_writers_are_rejected(self):
        invalid = [
            [task("task_first"), task("task_second"), task("task_tests", "tests")],
            [task("task_shared"), task("task_first", dependencies=["task_shared"]), task("task_second", dependencies=["task_shared"]), task("task_tests", "tests")],
        ]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaisesRegex(ValidationError, "writers must be ordered"):
                plan(tasks)

    def test_test_branch_cannot_depend_on_implementation(self):
        with self.assertRaisesRegex(ValidationError, "independent"):
            plan([task("task_build"), task("task_tests", "tests", ["task_build"])])

    def test_implementation_cannot_depend_on_generated_tests(self):
        with self.assertRaisesRegex(ValidationError, "must not depend on generated tests"):
            plan([task("task_build", dependencies=["task_tests"]), task("task_tests", "tests")])

    def test_plan_requires_implementation_and_exactly_one_test_branch(self):
        invalid = [
            [task("task_first"), task("task_second", dependencies=["task_first"])],
            [task("task_build"), task("task_first", "tests"), task("task_second", "tests")],
            [task("task_first", "tests"), task("task_second", "tests")],
        ]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaises(ValidationError):
                plan(tasks)

    def test_model_cannot_add_privileged_actions_or_approval_fields(self):
        with self.assertRaises(ValidationError):
            Task.model_validate(task("task_execute", "shell"))
        with self.assertRaises(ValidationError):
            plan([task("task_build"), task("task_tests", "tests")], approved=True)
        with self.assertRaises(ValidationError):
            ChangeSet.model_validate({"rationale": "Proposed change", "changes": [{"path": "service/shortener/app.py", "previous_hash": None, "content": "value = 1\n", "execute": True}]})

    def test_changeset_cannot_express_deletion(self):
        with self.assertRaises(ValidationError):
            ChangeSet.model_validate({"rationale": "Delete a file", "changes": [{"path": "service/shortener/app.py", "previous_hash": "0" * 64, "content": None}]})


if __name__ == "__main__":
    unittest.main()
