import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_sim import make, run  # noqa: E402

from warmup.cli import main  # noqa: E402
from warmup.db import DB  # noqa: E402


def test_health_states():
    cfg, eng, tr, st = make()
    assert "no tick" in eng.health()
    run(eng, st, 1)
    assert eng.health() == ""
    st["t"] += 31 * 60                              # loop stopped
    assert "last tick was" in eng.health()
    eng.db.set("last_tick", str(int(st["t"])))      # ticking, but never succeeding for hours
    eng.db.set("last_ok", str(int(st["t"] - 4 * 3600)))
    assert "none has succeeded" in eng.health()


def test_backup_is_consistent_pruned_and_private():
    with tempfile.TemporaryDirectory() as d:
        db = DB(str(Path(d) / "warmup.db"))
        db.set("k", "v")
        db.x("insert into jobs(due_at,kind) values(1,'open')")
        outs = []
        for i in range(4):
            # distinct names even within one second
            Path(d, "b").mkdir(exist_ok=True)
            Path(d, "b", f"warmup-2020010{i}-000000.db").write_bytes(b"old")
        p = db.backup(str(Path(d) / "b"), keep=3)
        outs = sorted(x.name for x in Path(d, "b").glob("warmup-*.db"))
        assert len(outs) == 3 and p.name in outs, outs  # pruned to 3, newest kept
        assert oct(p.stat().st_mode & 0o777) == "0o600"
        c = sqlite3.connect(p)
        assert c.execute("select v from meta where k='k'").fetchone()[0] == "v"
        assert c.execute("select count(*) from jobs").fetchone()[0] == 1
        assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
        c.close()


def test_cli_health_and_backup_commands():
    import os
    import yaml
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as d:
        raw = yaml.safe_load((root / "config.example.yaml").read_text())
        raw["database"] = str(Path(d) / "w.db")
        cfg = Path(d) / "config.yaml"
        cfg.write_text(yaml.safe_dump(raw))
        cwd = os.getcwd()
        os.chdir(d)
        try:
            assert main(["health", "-c", str(cfg)]) == 1                      # never ticked
            assert main(["tick", "--dry-run", "-c", str(cfg)]) == 0
            assert main(["health", "-c", str(cfg)]) == 0                      # no secrets needed
            assert main(["backup", "-c", str(cfg), "--to", str(Path(d) / "bk")]) == 0
            assert len(list((Path(d) / "bk").glob("warmup-*.db"))) == 1
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
