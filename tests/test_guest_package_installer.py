"""Security contracts for the root-owned semantic package helper."""
from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from guest_agent import package_installer, semantic_agent


class PackageInstallerTests(unittest.TestCase):
    def test_exactly_one_bounded_package_name_is_required(self) -> None:
        invalid = (
            [],
            ["helper"],
            ["helper", "-oDebug=true"],
            ["helper", "example;id"],
            ["helper", "example other"],
            ["helper", "Example"],
            ["helper", "example", "other"],
        )
        for argv in invalid:
            with (
                self.subTest(argv=argv),
                patch.object(package_installer.os, "execve") as execute,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(package_installer.main(argv), 64)
                execute.assert_not_called()

    def test_non_root_execution_is_rejected_before_apt(self) -> None:
        with (
            patch.object(package_installer.os, "geteuid", return_value=1000),
            patch.object(package_installer.os, "execve") as execute,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(package_installer.main(["helper", "example-utils"]), 77)
            execute.assert_not_called()

    def test_valid_name_executes_only_fixed_apt_argv_and_environment(self) -> None:
        with (
            patch.object(package_installer.os, "geteuid", return_value=0),
            patch.object(
                package_installer.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr="",
                ),
            ) as preflight,
            patch.object(package_installer.os, "execve") as execute,
        ):
            self.assertEqual(package_installer.main(["helper", "example-utils:amd64"]), 70)
        preflight.assert_called_once_with(
            [
                "/usr/bin/apt-get", "install", "--simulate",
                "--no-install-recommends", "--", "example-utils:amd64",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
            env=package_installer._FIXED_ENVIRONMENT,
        )
        execute.assert_called_once_with(
            "/usr/bin/apt-get",
            [
                "/usr/bin/apt-get", "install", "--yes",
                "--no-install-recommends", "--", "example-utils:amd64",
            ],
            {
                "DEBIAN_FRONTEND": "noninteractive",
                "HOME": "/root",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )

    def test_unprivileged_and_privileged_validators_do_not_drift(self) -> None:
        self.assertEqual(
            package_installer._PACKAGE_NAME.pattern,
            semantic_agent._PACKAGE_NAME.pattern,
        )


if __name__ == "__main__":
    unittest.main()
