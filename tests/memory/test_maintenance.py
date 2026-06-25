"""Tests for memory maintenance: consolidation plan parsing, backup/archive, apply, optimize config."""
import json
import time

import pytest

from nanocode.memory import maintenance


@pytest.fixture
def mem_env(tmp_path, monkeypatch):
    """Set up a temporary memory directory with sample files."""
    monkeypatch.chdir(tmp_path)
    # Override project_memory_dir to use tmp
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    monkeypatch.setattr(maintenance, "project_memory_dir", lambda: mem_dir)

    # Create sample memory files
    (mem_dir / "project_goals.md").write_text(
        "---\nname: goals\ndescription: project goals\ntype: project\n---\n"
        "We want to ship v2 by end of Q1."
    )
    (mem_dir / "user_prefs.md").write_text(
        "---\nname: preferences\ndescription: user preferences\ntype: user\n---\n"
        "Prefers concise answers. Uses vim."
    )
    (mem_dir / "feedback_testing.md").write_text(
        "---\nname: testing feedback\ndescription: testing notes\ntype: feedback\n---\n"
        "Always run pytest before committing."
    )
    return mem_dir


# ─── Plan Parsing ────────────────────────────────────────────


class TestParsePlan:
    def test_parse_valid_delete(self):
        raw = json.dumps({
            "summary": "Remove stale entry",
            "actions": [{"action": "delete", "targets": ["old.md"], "reason": "expired"}]
        })
        plan = maintenance.parse_consolidation_plan(raw)
        assert plan.summary == "Remove stale entry"
        assert len(plan.actions) == 1
        assert plan.actions[0].action == "delete"
        assert plan.actions[0].targets == ["old.md"]

    def test_parse_valid_merge(self):
        raw = json.dumps({
            "summary": "Merge duplicates",
            "actions": [{
                "action": "merge",
                "targets": ["a.md", "b.md"],
                "new_filename": "merged.md",
                "new_content": "---\nname: merged\n---\ncombined",
                "reason": "duplicates"
            }]
        })
        plan = maintenance.parse_consolidation_plan(raw)
        assert plan.actions[0].action == "merge"
        assert plan.actions[0].new_filename == "merged.md"

    def test_parse_valid_rewrite(self):
        raw = json.dumps({
            "summary": "Rewrite",
            "actions": [{
                "action": "rewrite",
                "targets": ["c.md"],
                "new_content": "updated content",
                "reason": "fix contradiction"
            }]
        })
        plan = maintenance.parse_consolidation_plan(raw)
        assert plan.actions[0].new_content == "updated content"

    def test_parse_empty_plan(self):
        raw = json.dumps({"summary": "Nothing to do", "actions": []})
        plan = maintenance.parse_consolidation_plan(raw)
        assert plan.actions == []

    def test_parse_invalid_action(self):
        raw = json.dumps({"actions": [{"action": "destroy", "targets": ["x.md"]}]})
        with pytest.raises(ValueError, match="Invalid action"):
            maintenance.parse_consolidation_plan(raw)

    def test_parse_missing_targets(self):
        raw = json.dumps({"actions": [{"action": "delete", "targets": []}]})
        with pytest.raises(ValueError, match="no targets"):
            maintenance.parse_consolidation_plan(raw)

    def test_parse_merge_missing_new_filename(self):
        raw = json.dumps({"actions": [{
            "action": "merge", "targets": ["a.md", "b.md"], "new_content": "x"
        }]})
        with pytest.raises(ValueError, match="new_filename"):
            maintenance.parse_consolidation_plan(raw)

    def test_parse_rewrite_missing_content(self):
        raw = json.dumps({"actions": [{"action": "rewrite", "targets": ["a.md"]}]})
        with pytest.raises(ValueError, match="new_content"):
            maintenance.parse_consolidation_plan(raw)

    def test_parse_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            maintenance.parse_consolidation_plan("not json")

    def test_parse_non_object(self):
        with pytest.raises(ValueError, match="JSON object"):
            maintenance.parse_consolidation_plan('"just a string"')


# ─── Backup & Rollback ───────────────────────────────────────


class TestBackup:
    def test_create_and_rollback(self, mem_env):
        # Create backup
        backup_id = maintenance.create_backup(["project_goals.md", "user_prefs.md"])
        assert backup_id  # non-empty string

        # Verify backup exists
        backup_dir = maintenance._backup_dir() / backup_id
        assert (backup_dir / "project_goals.md").exists()
        assert (backup_dir / "user_prefs.md").exists()
        assert (backup_dir / "_manifest.json").exists()

        # Modify original
        (mem_env / "project_goals.md").write_text("modified content")

        # Rollback
        restored = maintenance.rollback_backup(backup_id)
        assert "project_goals.md" in restored
        assert "We want to ship v2" in (mem_env / "project_goals.md").read_text()

    def test_rollback_nonexistent(self, mem_env):
        with pytest.raises(FileNotFoundError):
            maintenance.rollback_backup("nonexistent_id")

    def test_backup_missing_file_skipped(self, mem_env):
        # Backing up a non-existent file just doesn't copy it
        backup_id = maintenance.create_backup(["project_goals.md", "nonexistent.md"])
        backup_dir = maintenance._backup_dir() / backup_id
        assert (backup_dir / "project_goals.md").exists()
        assert not (backup_dir / "nonexistent.md").exists()


# ─── Archive ─────────────────────────────────────────────────


class TestArchive:
    def test_archive_moves_file(self, mem_env):
        assert (mem_env / "user_prefs.md").exists()
        result = maintenance.archive_file("user_prefs.md", reason="test")
        assert result is True
        assert not (mem_env / "user_prefs.md").exists()

        # Check archive dir has the file
        archive = maintenance._archive_dir()
        archived_files = [f for f in archive.iterdir() if "user_prefs" in f.name and not f.name.endswith(".meta.json")]
        assert len(archived_files) == 1

        # Check metadata sidecar
        meta_files = [f for f in archive.iterdir() if f.name.endswith(".meta.json")]
        assert len(meta_files) >= 1
        meta = json.loads(meta_files[0].read_text())
        assert meta["original"] == "user_prefs.md"
        assert meta["reason"] == "test"

    def test_archive_nonexistent_returns_false(self, mem_env):
        assert maintenance.archive_file("nope.md") is False


# ─── Apply Plan ──────────────────────────────────────────────


class TestApplyPlan:
    def test_apply_delete(self, mem_env):
        plan = maintenance.ConsolidationPlan(
            actions=[maintenance.ConsolidationAction(
                action="delete", targets=["user_prefs.md"], reason="outdated"
            )],
            summary="Remove outdated prefs"
        )
        result = maintenance.apply_plan(plan)
        assert result.archived == 1
        assert result.errors == []
        assert not (mem_env / "user_prefs.md").exists()
        assert result.backup_id  # backup was created

    def test_apply_merge(self, mem_env):
        plan = maintenance.ConsolidationPlan(
            actions=[maintenance.ConsolidationAction(
                action="merge",
                targets=["project_goals.md", "feedback_testing.md"],
                new_filename="merged_project.md",
                new_content="---\nname: merged\ntype: project\n---\nGoals + testing combined",
                reason="related topics"
            )],
        )
        result = maintenance.apply_plan(plan)
        assert result.merged == 2
        assert (mem_env / "merged_project.md").exists()
        assert "Goals + testing combined" in (mem_env / "merged_project.md").read_text()
        assert not (mem_env / "project_goals.md").exists()
        assert not (mem_env / "feedback_testing.md").exists()

    def test_apply_rewrite(self, mem_env):
        new_content = "---\nname: goals\ntype: project\n---\nShip v3 by Q2."
        plan = maintenance.ConsolidationPlan(
            actions=[maintenance.ConsolidationAction(
                action="rewrite", targets=["project_goals.md"],
                new_content=new_content, reason="updated goal"
            )],
        )
        result = maintenance.apply_plan(plan)
        assert result.rewritten == 1
        assert "Ship v3 by Q2" in (mem_env / "project_goals.md").read_text()

    def test_apply_normalize_date(self, mem_env):
        new_content = "---\nname: goals\ntype: project\n---\nShip v2 by 2026-03-31."
        plan = maintenance.ConsolidationPlan(
            actions=[maintenance.ConsolidationAction(
                action="normalize_date", targets=["project_goals.md"],
                new_content=new_content, reason="relative→absolute"
            )],
        )
        result = maintenance.apply_plan(plan)
        assert result.normalized == 1
        assert "2026-03-31" in (mem_env / "project_goals.md").read_text()

    def test_apply_missing_target_aborts_early(self, mem_env):
        plan = maintenance.ConsolidationPlan(
            actions=[maintenance.ConsolidationAction(
                action="delete", targets=["nonexistent.md"], reason="n/a"
            )],
        )
        result = maintenance.apply_plan(plan)
        assert result.total_actions == 0
        assert any("Missing files" in e for e in result.errors)

    def test_apply_multiple_actions(self, mem_env):
        plan = maintenance.ConsolidationPlan(
            actions=[
                maintenance.ConsolidationAction(
                    action="delete", targets=["feedback_testing.md"], reason="stale"
                ),
                maintenance.ConsolidationAction(
                    action="rewrite", targets=["user_prefs.md"],
                    new_content="---\nname: preferences\ntype: user\n---\nUpdated prefs.",
                    reason="refresh"
                ),
            ],
            summary="Cleanup round"
        )
        result = maintenance.apply_plan(plan)
        assert result.archived == 1
        assert result.rewritten == 1
        assert not (mem_env / "feedback_testing.md").exists()
        assert "Updated prefs" in (mem_env / "user_prefs.md").read_text()

    def test_summary_line(self, mem_env):
        plan = maintenance.ConsolidationPlan(
            actions=[maintenance.ConsolidationAction(
                action="delete", targets=["user_prefs.md"], reason="x"
            )],
        )
        result = maintenance.apply_plan(plan)
        line = result.summary_line()
        assert "archived 1" in line
        assert "backup=" in line


# ─── Retrieval Config Lifecycle (docs/22 Phase 7: moved to retrieval_config_store) ───


class TestRetrievalConfigStoreReplacesEvolveConfig:
    def test_evolve_config_functions_are_gone(self):
        # docs/22 Phase 7: the markdown-centric evolve_config lifecycle is deleted;
        # there is exactly one retrieval-config truth source.
        for name in ("load_evolve_config", "save_evolve_config",
                     "rollback_evolve_config", "evolve_config_path"):
            assert not hasattr(maintenance, name), f"{name} should be removed"

    def test_retrieval_config_store_roundtrip(self, tmp_path):
        from nanocode.memory import retrieval_config_store as RCS
        from nanocode.memory.engines.simplemem.retrieval_config import RetrievalConfig
        cfg = RetrievalConfig(semantic_top_k=12)
        RCS.save_retrieval_config(str(tmp_path), cfg, run_id="r1",
                                  report={"summary": {}, "cases": [], "history": {}})
        assert RCS.load_retrieval_config(str(tmp_path)) == cfg


# ─── Eval Provenance Pruning ─────────────────────────────────


class TestPruneOrphanedEvals:
    def test_prunes_orphaned(self, mem_env):
        eval_dir = maintenance._simplemem_dir() / "eval"
        eval_dir.mkdir(parents=True)

        # Eval referencing existing file
        (eval_dir / "eval1.json").write_text(json.dumps({"source_memory": "project_goals.md", "q": "what?"}))
        # Eval referencing deleted file
        (eval_dir / "eval2.json").write_text(json.dumps({"source_memory": "deleted_file.md", "q": "when?"}))
        # Eval with no source_memory (kept)
        (eval_dir / "eval3.json").write_text(json.dumps({"q": "how?"}))

        pruned = maintenance.prune_orphaned_evals(eval_dir)
        assert pruned == 1
        assert (eval_dir / "eval1.json").exists()
        assert not (eval_dir / "eval2.json").exists()
        assert (eval_dir / "eval3.json").exists()

    def test_no_eval_dir(self, mem_env):
        assert maintenance.prune_orphaned_evals() == 0


# ─── Curator Prompt Building ─────────────────────────────────


class TestCuratorPrompt:
    def test_build_user_message_includes_files(self, mem_env):
        msg = maintenance.build_curator_user_message()
        assert "project_goals.md" in msg
        assert "user_prefs.md" in msg
        assert "We want to ship v2" in msg

    def test_build_user_message_empty(self, mem_env):
        # Remove all files
        for f in mem_env.glob("*.md"):
            f.unlink()
        msg = maintenance.build_curator_user_message()
        assert "No memory files" in msg

    def test_curator_prompt_is_nonempty(self):
        assert len(maintenance.CURATOR_CONSOLIDATION_PROMPT) > 100
        assert "JSON" in maintenance.CURATOR_CONSOLIDATION_PROMPT


# ─── Integration: Full Round-trip ────────────────────────────


class TestIntegrationRoundtrip:
    def test_parse_apply_rollback(self, mem_env):
        """Simulate full curator → apply → rollback cycle."""
        # Curator produces a plan
        raw = json.dumps({
            "summary": "Clean up duplicates",
            "actions": [
                {"action": "delete", "targets": ["feedback_testing.md"], "reason": "redundant"},
                {"action": "rewrite", "targets": ["project_goals.md"],
                 "new_content": "---\nname: goals\ntype: project\n---\nNew goals content.",
                 "reason": "updated"}
            ]
        })

        # Parse
        plan = maintenance.parse_consolidation_plan(raw)
        assert len(plan.actions) == 2

        # Apply
        result = maintenance.apply_plan(plan)
        assert result.archived == 1
        assert result.rewritten == 1
        assert not (mem_env / "feedback_testing.md").exists()
        assert "New goals content" in (mem_env / "project_goals.md").read_text()

        # Rollback
        restored = maintenance.rollback_backup(result.backup_id)
        assert "project_goals.md" in restored
        assert "We want to ship v2" in (mem_env / "project_goals.md").read_text()
        # Note: feedback_testing.md was archived (moved), rollback only restores the backup copy
        # The file should be restored from backup
        assert "feedback_testing.md" in restored
