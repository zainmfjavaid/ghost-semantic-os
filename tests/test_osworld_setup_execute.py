"""Fail-closed contracts for generic OSWorld execute setup steps."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "OSWorld" / "desktop_env" / "controllers" / "setup.py"


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_setup_module():
    class _Placeholder:
        pass

    stubs = {
        "playwright": _module("playwright"),
        "playwright.sync_api": _module(
            "playwright.sync_api", sync_playwright=lambda: None,
            TimeoutError=TimeoutError,
        ),
        "pydrive": _module("pydrive"),
        "pydrive.auth": _module("pydrive.auth", GoogleAuth=_Placeholder),
        "pydrive.drive": _module(
            "pydrive.drive", GoogleDrive=_Placeholder,
            GoogleDriveFile=_Placeholder, GoogleDriveFileList=_Placeholder,
        ),
        "requests_toolbelt": _module("requests_toolbelt"),
        "requests_toolbelt.multipart": _module("requests_toolbelt.multipart"),
        "requests_toolbelt.multipart.encoder": _module(
            "requests_toolbelt.multipart.encoder", MultipartEncoder=_Placeholder,
        ),
        "desktop_env": _module("desktop_env"),
        "desktop_env.controllers": _module("desktop_env.controllers"),
        "desktop_env.controllers.python": _module(
            "desktop_env.controllers.python", PythonController=_Placeholder,
        ),
        "desktop_env.evaluators": _module("desktop_env.evaluators"),
        "desktop_env.evaluators.metrics": _module("desktop_env.evaluators.metrics"),
        "desktop_env.evaluators.metrics.utils": _module(
            "desktop_env.evaluators.metrics.utils", compare_urls=lambda *_args: False,
        ),
        "desktop_env.providers": _module("desktop_env.providers"),
        "desktop_env.providers.aws": _module("desktop_env.providers.aws"),
        "desktop_env.providers.aws.proxy_pool": _module(
            "desktop_env.providers.aws.proxy_pool",
            get_global_proxy_pool=lambda: None,
            init_proxy_pool=lambda *_args, **_kwargs: None,
            ProxyInfo=_Placeholder,
        ),
        "dotenv": _module("dotenv", load_dotenv=lambda: None),
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(
            "osworld_setup_controller_under_test", MODULE_PATH,
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


SETUP = _load_setup_module()


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


def _result(returncode: int, *, output: str = "", error: str = ""):
    return _Response({
        "status": "success",
        "output": output,
        "error": error,
        "returncode": returncode,
    })


class ExecuteSetupFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = SETUP.SetupController("guest.invalid")

    def test_default_execute_rejects_nonzero_guest_returncode(self) -> None:
        with mock.patch.object(
            SETUP.requests, "post",
            return_value=_result(1, error="permission denied"),
        ):
            with self.assertRaisesRegex(RuntimeError, "return code 1"):
                self.controller._execute_setup(["mkdir", "/unwritable"])

    def test_default_execute_accepts_zero_guest_returncode(self) -> None:
        with mock.patch.object(
            SETUP.requests, "post", return_value=_result(0, output="ready"),
        ) as post:
            self.controller._execute_setup(["true"])

        self.assertEqual(post.call_count, 1)

    def test_until_retries_intermediate_nonzero_then_accepts_match(self) -> None:
        responses = [_result(1, error="not ready"), _result(0, output="ready")]
        with mock.patch.object(SETUP.requests, "post", side_effect=responses) as post:
            with mock.patch.object(SETUP.time, "sleep"):
                self.controller._execute_setup(
                    ["probe"], until={"returncode": 0},
                )

        self.assertEqual(post.call_count, 2)

    def test_until_can_explicitly_accept_nonzero_returncode(self) -> None:
        with mock.patch.object(
            SETUP.requests, "post", return_value=_result(3, error="expected"),
        ) as post:
            self.controller._execute_setup(
                ["probe"], until={"returncode": 3},
            )

        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
