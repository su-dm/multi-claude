"""Tests for session info read from Claude Code transcript files.

The JSONL fixture lines mirror the real format observed in Claude Code 2.1.x
(~/.claude/projects/<munged-cwd>/<session-id>.jsonl).
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from multi_claude.transcripts import (
    TokenReader,
    find_session_file,
    fmt_cost,
    fmt_model,
    fmt_tokens,
    project_dir,
    read_session_info,
    session_cost,
)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def assistant_line(
    ts: float,
    inp: int,
    cache_read: int,
    cache_create: int,
    out: int,
    model: str = "claude-sonnet-5",
    content: list | None = None,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": iso(ts),
            "message": {
                "role": "assistant",
                "model": model,
                "content": content or [],
                "usage": {
                    "input_tokens": inp,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_create,
                    "output_tokens": out,
                },
            },
        }
    )


def make_session(pdir: Path, session_id: str, start: float, usages: list[tuple]) -> Path:
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{session_id}.jsonl"
    lines = [
        json.dumps({"type": "mode", "mode": "normal", "sessionId": session_id}),
        json.dumps({"type": "user", "timestamp": iso(start), "message": {"role": "user"}}),
    ]
    for i, usage in enumerate(usages):
        lines.append(assistant_line(start + 10 * (i + 1), *usage))
    path.write_text("\n".join(lines) + "\n")
    return path


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cwd = "/home/user/code/my_proj.x"
        self.pdir = project_dir(self.home, self.cwd)

    def tearDown(self):
        self.tmp.cleanup()

    def test_project_dir_munging(self):
        self.assertEqual(self.pdir.name, "-home-user-code-my-proj-x")

    def test_info_uses_final_assistant_message(self):
        path = make_session(self.pdir, "s1", 1000.0, [(5, 100, 50, 10), (2, 18828, 11978, 12)])
        info = read_session_info(path)
        self.assertEqual(info.tokens, 2 + 18828 + 11978 + 12)
        self.assertEqual(info.model, "claude-sonnet-5")
        self.assertEqual(info.session_id, "s1")

    def test_no_assistant_message_yet(self):
        path = make_session(self.pdir, "s1", 1000.0, [])
        self.assertIsNone(read_session_info(path).tokens)

    def test_activity_prefers_latest_block(self):
        path = make_session(self.pdir, "s1", 1000.0, [])
        with open(path, "a") as fh:
            fh.write(
                assistant_line(
                    1100.0, 1, 1, 1, 1,
                    content=[
                        {"type": "text", "text": "I'll fix the parser now."},
                        {"type": "tool_use", "name": "Edit",
                         "input": {"file_path": "/repo/src/parser.py"}},
                    ],
                )
                + "\n"
            )
        info = read_session_info(path)
        self.assertEqual(info.activity, "Edit: parser.py")

    def test_activity_thinking_snippet(self):
        path = make_session(self.pdir, "s1", 1000.0, [])
        with open(path, "a") as fh:
            fh.write(
                assistant_line(
                    1100.0, 1, 1, 1, 1,
                    content=[{"type": "thinking", "thinking": "The bug must be in retry logic.\nBecause..."}],
                )
                + "\n"
            )
        self.assertEqual(read_session_info(path).activity, "∴ The bug must be in retry logic.")

    def test_ai_title(self):
        path = make_session(self.pdir, "s1", 1000.0, [(1, 1, 1, 1)])
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "Fix parser bug"}) + "\n")
        self.assertEqual(read_session_info(path).title, "Fix parser bug")

    def test_session_cost_sums_all_messages(self):
        # Two sonnet messages: (in=1000, cr=0, cc=0, out=1000) each
        # -> per msg: 1000*3/1M + 1000*15/1M = 0.018; x2 = 0.036
        path = make_session(self.pdir, "s1", 1000.0, [(1000, 0, 0, 1000), (1000, 0, 0, 1000)])
        self.assertAlmostEqual(session_cost(path), 0.036, places=6)

    def test_session_cost_dedupes_multi_block_responses(self):
        # Claude Code writes one JSONL line per content block; all lines of
        # one API response carry the SAME message id + usage and must be
        # counted once, not per line.
        path = make_session(self.pdir, "s1", 1000.0, [])
        line = json.loads(assistant_line(1100.0, 1000, 0, 0, 1000))
        line["message"]["id"] = "msg_abc"
        line["requestId"] = "req_1"
        with open(path, "a") as fh:
            fh.write(json.dumps(line) + "\n")
            fh.write(json.dumps(line) + "\n")
            fh.write(json.dumps(line) + "\n")
        # One sonnet response: 1000*3/1M + 1000*15/1M = 0.018
        self.assertAlmostEqual(session_cost(path), 0.018, places=6)

    def test_session_cost_distinct_responses_still_sum(self):
        path = make_session(self.pdir, "s1", 1000.0, [])
        with open(path, "a") as fh:
            for i in range(2):
                line = json.loads(assistant_line(1100.0 + i, 1000, 0, 0, 1000))
                line["message"]["id"] = f"msg_{i}"
                line["requestId"] = f"req_{i}"
                fh.write(json.dumps(line) + "\n")
        self.assertAlmostEqual(session_cost(path), 0.036, places=6)

    def test_session_cost_unknown_model_is_skipped(self):
        path = make_session(self.pdir, "s1", 1000.0, [])
        with open(path, "a") as fh:
            fh.write(assistant_line(1100.0, 1000, 0, 0, 1000, model="gpt-42") + "\n")
        self.assertIsNone(session_cost(path))

    def test_session_matching_picks_closest_start(self):
        make_session(self.pdir, "old", 1000.0, [(1, 1, 1, 1)])
        target = make_session(self.pdir, "mine", 5000.0, [(1, 1, 1, 1)])
        make_session(self.pdir, "newer", 9000.0, [(1, 1, 1, 1)])
        self.assertEqual(find_session_file(self.home, self.cwd, 5003.0), target)

    def test_session_matching_rejects_far_sessions(self):
        path = make_session(self.pdir, "old", 1000.0, [(1, 1, 1, 1)])
        os.utime(path, (1000.0, 1000.0))
        self.assertIsNone(find_session_file(self.home, self.cwd, 500000.0))

    def test_resumed_session_matches_by_recent_mtime(self):
        path = make_session(self.pdir, "old", 1000.0, [(2, 300, 100, 20)])
        os.utime(path, (500001.0, 500001.0))
        self.assertEqual(find_session_file(self.home, self.cwd, 500000.0), path)

    def test_missing_project_dir(self):
        self.assertIsNone(find_session_file(self.home, "/nope", 0.0))

    def test_token_reader_caches_and_tracks_updates(self):
        path = make_session(self.pdir, "s1", 5000.0, [(1, 100, 0, 10)])
        reader = TokenReader(self.home)
        self.assertEqual(reader.tokens_for("a", self.cwd, 5001.0), 111)
        with open(path, "a") as fh:
            fh.write(assistant_line(5100.0, 2, 300, 100, 20) + "\n")
        self.assertEqual(reader.tokens_for("a", self.cwd, 5001.0), 422)
        info = reader.info_for("a", self.cwd, 5001.0)
        self.assertEqual(info.session_id, "s1")
        self.assertIsNotNone(info.cost_usd)
        reader.forget("a")
        self.assertEqual(reader.tokens_for("a", self.cwd, 5001.0), 422)

    def test_formatting(self):
        self.assertEqual(fmt_tokens(None), "")
        self.assertEqual(fmt_tokens(30820), "31k")
        self.assertEqual(fmt_tokens(1_230_000), "1.2M")
        self.assertEqual(fmt_cost(None), "")
        self.assertEqual(fmt_cost(0.42), "42¢")
        self.assertEqual(fmt_cost(3.456), "$3.46")
        self.assertEqual(fmt_cost(123.4), "$123")
        self.assertEqual(fmt_model("claude-sonnet-5"), "sonnet-5")
        self.assertEqual(fmt_model("claude-fable-5"), "fable-5")


if __name__ == "__main__":
    unittest.main()
