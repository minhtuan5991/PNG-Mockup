"""Opt-in real Photoshop integration test, using only newly created fixtures.

Creates two test PSDs, renders PNG one/two-side jobs, verifies resume and reruns
with a watermark. Existing Photoshop documents are not saved, modified or closed.
Run explicitly; this is intentionally excluded from automatic unit discovery.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "App Source" / "src"))

import app
import psd_mockup
from config import DEFAULT_CONFIG, load_config
from logger import setup_logger
from queue_manager import build_queue, pending_jobs
from state import apply_state_to_queue, load_state


FIXTURE_JSX = r'''
#target photoshop
(function () {
    var doc = null;
    var previousDoc = app.documents.length ? app.activeDocument : null;
    var previousUnits = app.preferences.rulerUnits;
    var previousDialogs = app.displayDialogs;
    var beforeCount = app.documents.length;
    function color(rgb) {
        var c = new SolidColor();
        c.rgb.red = rgb[0]; c.rgb.green = rgb[1]; c.rgb.blue = rgb[2];
        return c;
    }
    function fillLayer(group, name, rgb, box, smart) {
        var layer = group.artLayers.add();
        layer.name = name;
        doc.activeLayer = layer;
        doc.selection.select([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]);
        doc.selection.fill(color(rgb));
        doc.selection.deselect();
        if (smart) {
            executeAction(stringIDToTypeID("newPlacedLayer"), new ActionDescriptor(), DialogModes.NO);
            doc.activeLayer.name = name;
        }
    }
    function make(sides, filename) {
        doc = app.documents.add(400, 300, 72, "PNG Mockup integration fixture", NewDocumentMode.RGB, DocumentFill.TRANSPARENT);
        var variants = [["White", "Dark", [245, 245, 245]], ["Black", "Light", [25, 25, 25]]];
        for (var i = 0; i < variants.length; i++) {
            var group = doc.layerSets.add(); group.name = variants[i][0];
            fillLayer(group, "Background", variants[i][2], [0, 0, 400, 300], false);
            if (sides === 1) {
                fillLayer(group, "Design " + variants[i][1], [120, 120, 120], [136, 86, 264, 214], true);
            } else {
                fillLayer(group, "Design " + variants[i][1] + " Front", [120, 120, 120], [46, 86, 174, 214], true);
                fillLayer(group, "Design " + variants[i][1] + " Back", [120, 120, 120], [226, 86, 354, 214], true);
            }
        }
        var options = new PhotoshopSaveOptions(); options.layers = true;
        doc.saveAs(File(__PSD_DIR__ + "/" + filename), options, true, Extension.LOWERCASE);
        doc.close(SaveOptions.DONOTSAVECHANGES); doc = null;
    }
    var ok = false;
    try {
        app.preferences.rulerUnits = Units.PIXELS;
        app.displayDialogs = DialogModes.NO;
        make(1, "fixture.1S.psd");
        make(2, "fixture.2S.psd");
        ok = true;
    } finally {
        if (doc !== null) { try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (e) {} }
        app.preferences.rulerUnits = previousUnits;
        app.displayDialogs = previousDialogs;
        if (previousDoc !== null) { try { app.activeDocument = previousDoc; } catch (e) {} }
        var report = File(__RESULT__); report.encoding = "UTF8"; report.open("w");
        report.write('{"ok":' + (ok ? 'true' : 'false') + ',"documents_before":' + beforeCount + ',"documents_after":' + app.documents.length + '}');
        report.close();
    }
})();
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    verification = ROOT / "verification"
    verification.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="photoshop-", dir=verification)).resolve()
    assert verification.resolve() in work.parents
    print(f"Fixture directory: {work}", flush=True)
    settings = dict(DEFAULT_CONFIG)
    settings_path = work / "config.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    config = load_config(settings_path)
    config.ensure_folders()
    colors = {
        "single.bl.png": (26, 94, 181, 255),
        "single.wh.png": (243, 192, 40, 255),
        "double.f.png": (226, 75, 54, 255),
        "double.bl.b.png": (26, 94, 181, 255),
        "double.wh.b.png": (243, 192, 40, 255),
    }
    for name, fill in colors.items():
        image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        ImageDraw.Draw(image).rounded_rectangle((16, 16, 112, 112), radius=12, fill=fill)
        image.save(config.png_dir / name, dpi=(72, 72))
    jsx_path = work / "create_fixtures.jsx"
    jsx_path.write_text(
        FIXTURE_JSX.replace("__PSD_DIR__", json.dumps(str(config.mockup_dir))).replace("__RESULT__", json.dumps(str(work / "fixture-result.json"))),
        encoding="utf-8-sig",
    )
    vbs = work / "create_fixtures.vbs"
    vbs.write_text(psd_mockup._photoshop_vbs(jsx_path), encoding="utf-8")
    print("Creating test PSDs through Photoshop COM...", flush=True)
    result = subprocess.run(["cscript.exe", "//nologo", str(vbs)], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"Fixture creation failed ({result.returncode}): {result.stdout} {result.stderr}")
    fixture_report = json.loads((work / "fixture-result.json").read_text(encoding="utf-8-sig"))
    assert fixture_report["ok"] and fixture_report["documents_before"] == fixture_report["documents_after"], fixture_report
    input_hashes = {str(path): sha256(path) for folder in (config.png_dir, config.mockup_dir) for path in folder.iterdir() if path.is_file()}
    jobs = build_queue(config)
    assert len(jobs) == 2, jobs
    log = setup_logger(config.log_path)
    state = load_state(config)
    print("Rendering one-side + shared-front two-side PNG jobs...", flush=True)
    summary = asyncio.run(app.run_jobs(config, jobs, jobs, state, log))
    assert summary.completed == 2 and summary.failed == 0, summary
    outputs = sorted(config.output_dir.glob("*.png"))
    assert len(outputs) == 4, outputs
    before_watermark = {}
    for path in outputs:
        with Image.open(path) as image:
            image.load()
            assert image.size == (400, 300), (path, image.size)
            assert abs(image.info.get("dpi", (0,))[0] - 72) < 1, image.info
            assert not any(key in image.info for key in ("exif", "icc_profile", "XML:com.adobe.xmp")), image.info
            rgb = image.convert("RGB")
            expected = (243, 192, 40) if "Black" in path.stem else (26, 94, 181)
            if "double" in path.stem:
                assert rgb.getpixel((110, 150)) == (226, 75, 54), (path, rgb.getpixel((110, 150)))
                assert rgb.getpixel((290, 150)) == expected, (path, rgb.getpixel((290, 150)))
            else:
                assert rgb.getpixel((200, 150)) == expected, (path, rgb.getpixel((200, 150)))
            before_watermark[str(path)] = rgb.getpixel((390, 290))
    resumed = apply_state_to_queue(config, build_queue(config), load_state(config))
    assert pending_jobs(config, resumed) == [], resumed
    watermark_path = work / "Test Watermark.png"
    watermark = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    ImageDraw.Draw(watermark).rectangle((360, 270, 399, 299), fill=(112, 69, 213, 180))
    watermark.save(watermark_path, dpi=(72, 72))
    settings.update(output_width_px=400, output_height_px=300, output_watermark_enabled=True, output_watermark_path=str(watermark_path), rerun_done=True)
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    config = load_config(settings_path)
    jobs = apply_state_to_queue(config, build_queue(config), load_state(config))
    print("Rendering with real watermark and metadata cleanup...", flush=True)
    watermark_summary = asyncio.run(app.run_jobs(config, jobs, jobs, load_state(config), log))
    assert watermark_summary.completed == 2 and watermark_summary.failed == 0, watermark_summary
    for path in outputs:
        with Image.open(path) as image:
            assert image.convert("RGB").getpixel((390, 290)) != before_watermark[str(path)]
    assert all(sha256(Path(path)) == digest for path, digest in input_hashes.items()), "Source PNG/PSD changed"
    report = {"passed": True, "fixture": fixture_report, "first_run": asdict(summary), "watermark_run": asdict(watermark_summary), "output_count": len(outputs), "native_canvas": [400, 300], "resume_skipped_completed": True, "input_hashes_unchanged": True, "outputs": [str(path) for path in outputs]}
    (work / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
