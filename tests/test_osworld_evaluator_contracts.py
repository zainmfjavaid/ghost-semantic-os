#!/usr/bin/env python3
"""Regression checks for task contracts that previously produced invalid zeroes."""
from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

from docx import Document


REPO = Path(__file__).resolve().parents[1]
OSWORLD = REPO / "OSWorld"
sys.path.insert(0, str(OSWORLD))


def package(name: str, path: Path) -> None:
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Import only the evaluator modules under test. Importing OSWorld's aggregate
# metrics package eagerly loads every optional image/media evaluator and makes
# this focused contract depend on the entire benchmark environment.
metrics_root = OSWORLD / "desktop_env/evaluators/metrics"
package("desktop_env", OSWORLD / "desktop_env")
package("desktop_env.evaluators", OSWORLD / "desktop_env/evaluators")
package("desktop_env.evaluators.metrics", metrics_root)
utils = load("desktop_env.evaluators.metrics.utils", metrics_root / "utils.py")
general = load("desktop_env.evaluators.metrics.general", metrics_root / "general.py")

easyocr = types.ModuleType("easyocr")
easyocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", easyocr)
skimage = types.ModuleType("skimage")
skimage_color = types.ModuleType("skimage.color")
skimage_color.deltaE_ciede2000 = lambda *args, **kwargs: None
skimage_color.rgb2lab = lambda *args, **kwargs: None
skimage.color = skimage_color
sys.modules.setdefault("skimage", skimage)
sys.modules.setdefault("skimage.color", skimage_color)
docs = load("desktop_env.evaluators.metrics.docs", metrics_root / "docs.py")

fuzzy_place_math = general.fuzzy_place_math
compare_docx_files = docs.compare_docx_files

url_utils_spec = importlib.util.spec_from_file_location(
    "osworld_url_utils",
    OSWORLD / "desktop_env/evaluators/getters/url_utils.py",
)
assert url_utils_spec and url_utils_spec.loader
url_utils = importlib.util.module_from_spec(url_utils_spec)
url_utils_spec.loader.exec_module(url_utils)
normalize_access_tree_url_text = url_utils.normalize_access_tree_url_text


FUTIAN_TASK = (
    OSWORLD
    / "evaluation_examples/examples/multi_apps"
    / "7ff48d5b-2df2-49da-b500-a5150ffc7f18.json"
)


def write_docx(path: Path, values: list[str]) -> None:
    document = Document()
    for value in values:
        document.add_paragraph(value)
    document.save(path)


task = json.loads(FUTIAN_TASK.read_text())
rules = task["evaluator"]["expected"]["rules"]
assert rules["expected_count"] == 5
accepted = rules["expected"][:5]

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    five = root / "five.docx"
    three = root / "three.docx"
    write_docx(
        five,
        ["深圳市福田区24小时自助签注机地址"]
        + [f"{index}. 智能服务点：{address}" for index, address in enumerate(accepted, 1)],
    )
    write_docx(three, accepted[:3])
    assert fuzzy_place_math(str(five), rules) == 1.0
    assert fuzzy_place_math(str(three), rules) == 0.0

# Preserve the original generic metric contract for tasks that do not opt in.
with tempfile.TemporaryDirectory() as tmp:
    three = Path(tmp) / "three.docx"
    write_docx(three, accepted[:3])
    assert fuzzy_place_math(str(three), {"expected": accepted}) == 1.0

assert normalize_access_tree_url_text(
    "/home/user/Desktop/report with spaces.html", "https://"
) == "file:///home/user/Desktop/report%20with%20spaces.html"
assert normalize_access_tree_url_text(
    "file:///home/user/Desktop/report.html", "https://"
) == "file:///home/user/Desktop/report.html"
assert normalize_access_tree_url_text("example.com/path", "https://") == (
    "https://example.com/path"
)
assert normalize_access_tree_url_text("www.example.com", "https://www.") == (
    "https://www.example.com"
)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    trailing = root / "trailing.docx"
    clean = root / "clean.docx"
    write_docx(trailing, ["First sentence. ", "", "Second sentence. "])
    write_docx(clean, ["First sentence.", "", "Second sentence."])
    assert compare_docx_files(
        str(trailing), str(clean), ignore_blanks=False, strip_whitespace=True,
    ) == 1
    assert compare_docx_files(
        str(trailing), str(clean), ignore_blanks=False, strip_whitespace=False,
    ) == 0

print("OSWorld evaluator contract tests passed")
