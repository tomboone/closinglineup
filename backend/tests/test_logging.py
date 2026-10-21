import json
import logging
from collections.abc import Iterator

import pytest

from closinglineup.logging import configure_logging


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_emits_json_line_with_extras(
    root_logger: logging.Logger, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")

    logging.getLogger("closinglineup.test").info("loaded %s", "box", extra={"rows": 240})

    record = json.loads(capsys.readouterr().out)
    assert record["level"] == "INFO"
    assert record["logger"] == "closinglineup.test"
    assert record["message"] == "loaded box"
    assert record["rows"] == 240
    assert record["ts"].endswith("+00:00")


def test_one_object_per_line(
    root_logger: logging.Logger, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()

    log = logging.getLogger("closinglineup.test")
    log.info("first")
    log.warning("second\nspans lines")

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["first", "second\nspans lines"]


def test_honours_level(root_logger: logging.Logger, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")

    logging.getLogger("closinglineup.test").info("quiet")

    assert capsys.readouterr().out == ""


def test_includes_exception(
    root_logger: logging.Logger, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("closinglineup.test").exception("failed")

    record = json.loads(capsys.readouterr().out)
    assert "RuntimeError: boom" in record["exc"]
