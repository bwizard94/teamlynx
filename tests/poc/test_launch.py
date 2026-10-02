"""lynx-poc command planning. Does not start windows or cameras."""

from pathlib import Path

import pytest

from lynx.poc.launch import build_parser, build_plan, format_plan

ROOT = Path(__file__).resolve().parents[2]


def plan(argv):
    return build_plan(build_parser().parse_args(argv))


def test_tier_a_dry_run_matches_headset_docs():
    p = plan(["--tier", "laptop", "--dry-run"])
    assert p.start_relay is True
    assert p.url == "ws://127.0.0.1:8765"
    cmds = p.commands(py="python")
    assert cmds[0] == ["python", "-m", "lynx.net.server", "--host", "127.0.0.1", "--port", "8765"]
    alpha, bravo = cmds[1], cmds[2]
    assert alpha[alpha.index("--callsign") + 1] == "ALPHA"
    assert bravo[bravo.index("--callsign") + 1] == "BRAVO"
    assert bravo[bravo.index("--y") + 1] == "20"
    assert bravo[bravo.index("--heading") + 1] == "180"
    assert bravo[bravo.index("--pitch") + 1] == "-10"
    assert alpha[alpha.index("--source") + 1] == "synthetic"
    assert "--edge" not in alpha


def test_bench_binds_all_interfaces_but_clients_use_localhost():
    p = plan(["--tier", "bench"])
    assert p.host == "0.0.0.0"
    assert p.url == "ws://127.0.0.1:8765"
    assert p.operators[1].source == "1"


def test_tier_b_two_laptops_roles():
    alpha = plan(["--tier", "bench", "--role", "alpha", "--host", "0.0.0.0"])
    assert alpha.start_relay is True
    assert len(alpha.operators) == 1
    assert alpha.operators[0].callsign == "ALPHA"
    assert alpha.operators[0].source == "0"

    bravo = plan([
        "--tier", "bench", "--role", "bravo",
        "--url", "ws://192.168.8.1:8765", "--bravo-source", "0",
    ])
    assert bravo.start_relay is False
    assert bravo.url == "ws://192.168.8.1:8765"
    assert len(bravo.operators) == 1
    assert bravo.operators[0].callsign == "BRAVO"
    assert bravo.operators[0].source == "0"


def test_serial_pose_override_and_edge():
    p = plan(["--tier", "bench", "--bravo-pose", "serial:/dev/ttyUSB0", "--edge"])
    assert p.operators[1].pose == "serial:/dev/ttyUSB0"
    assert "--edge" in p.headset_common
    assert "--allow-no-detector" in p.headset_common


def test_example_configs_exist_and_load():
    for name in ("laptop.json", "bench.json", "bravo-serial.example.json"):
        path = ROOT / "deploy" / "poc" / name
        assert path.is_file(), path
        p = plan(["--config", str(path), "--dry-run"])
        assert p.operators
        text = format_plan(p, py="python")
        assert "lynx.headset.app" in text


def test_unknown_operator_field_rejected():
    from lynx.poc.launch import OperatorSpec
    with pytest.raises(ValueError, match="jetson"):
        OperatorSpec.from_dict({"node": 1, "callsign": "A", "jetson": True})
