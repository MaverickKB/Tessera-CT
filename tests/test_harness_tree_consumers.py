"""Synthetic local SummaryTree/View proof; no native AetherMind success is implied."""

import pytest

from tessera.cli import main
from tessera.log import ChatLog
from tessera.tree import SummaryTree
from tessera.view import PLACEHOLDER, View
from test_status_projection_consumers import disk_snapshot
from test_swarm_job_consumers import offline_capabilities


LOCAL_TREE = {
    "messages": ["Inspect alpha fixture", "Alpha finding: alpha-evidence-placeholder",
                 "Inspect beta fixture café\ncontinued", "Beta finding: beta-evidence-placeholder"],
    "summaries": [(1, 0, "alpha findings"), (1, 1, "beta findings"), (2, 0, "joined findings")],
    "view": "<chat>\n0+4|joined findings\n</chat>",
    "zoom": "0+2|alpha findings\n2+2|beta findings\n",
}


def local_surface(home):
    (home / "config.toml").write_text('[view]\nnode_bytes=64\nhigh_bytes=80\nlow_bytes=40\n')
    root = home / "chat"
    log = ChatLog(root / "log")
    tree = SummaryTree(root, log, node_bytes=64)
    view = View(root, tree, high_bytes=80, low_bytes=40)
    log.subscribe(view.on_message)
    return log, tree, view


def test_local_fixture_round_trips_compact_view_and_cli_zoom_without_provider(tmp_path, capsys):
    log, tree, view = local_surface(tmp_path)
    try:
        for index, text in enumerate(LOCAL_TREE["messages"]):
            log.append("user" if index % 2 == 0 else "agent", text)
        for level, index, text in LOCAL_TREE["summaries"]:
            tree.set_node(level, index, text)
        assert view.nodes == [(2, 0)] and view.render() == LOCAL_TREE["view"]
        assert view.zoom(0, 4) == LOCAL_TREE["zoom"]
    finally:
        log.close()
    before = disk_snapshot(tmp_path)
    assert main(["--home", str(tmp_path), "zoom", "0+4"]) == 0
    assert capsys.readouterr().out == LOCAL_TREE["zoom"] + "\n"
    assert main(["--home", str(tmp_path), "zoom", "2", "1"]) == 0
    assert capsys.readouterr().out == LOCAL_TREE["messages"][2] + "\n"
    with ChatLog(tmp_path / "chat" / "log") as reopened:
        restored_tree = SummaryTree(tmp_path / "chat", reopened, node_bytes=64)
        restored = View(tmp_path / "chat", restored_tree, high_bytes=80, low_bytes=40)
        assert restored.nodes == [(2, 0)] and restored.render() == LOCAL_TREE["view"]
        pages = []
        offset = 0
        raw = LOCAL_TREE["messages"][2]
        while offset < len(raw.encode("utf-8")):
            page = restored.zoom(2, 1, offset=offset, limit=5)
            pages.append(page)
            offset += len(page.encode("utf-8"))
        assert "".join(pages) == raw
        bad_offset = raw.encode("utf-8").index("é".encode("utf-8")) + 1
        with pytest.raises(ValueError, match="boundary"):
            restored.zoom(2, 1, offset=bad_offset, limit=5)
    assert disk_snapshot(tmp_path) == before


def test_unbuilt_local_summary_stays_honest_while_raw_cli_zoom_remains_available(tmp_path, capsys):
    log, tree, view = local_surface(tmp_path)
    raw = "café fixture " * 12
    try:
        log.append("tool", raw)
        log.append("user", "Inspect fixture")
        assert tree.node(0, 0) is None
        assert PLACEHOLDER in view.render()
        expected = "0+1|" + PLACEHOLDER + "\n1+1|Inspect fixture\n"
        assert view.zoom(0, 2) == expected
    finally:
        log.close()
    before = disk_snapshot(tmp_path)
    assert main(["--home", str(tmp_path), "zoom", "0+2"]) == 0
    assert capsys.readouterr().out == expected + "\n"
    assert main(["--home", str(tmp_path), "zoom", "0", "1"]) == 0
    assert capsys.readouterr().out == raw + "\n"
    assert disk_snapshot(tmp_path) == before
