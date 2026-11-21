import json
import os

_out = os.environ.get("BR_LABEL_OUT")
_fail = set()
_errors = []


def pytest_runtest_logreport(report):
    if report.failed:
        _fail.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _errors.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _out:
        with open(_out, "w") as f:
            json.dump({"failing": sorted(_fail), "collect_errors": _errors, "exit": int(exitstatus)}, f)
