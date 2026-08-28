"""Verify the private runtime without reading or modifying user project data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tk", action="store_true", help="Hide a Tk root and verify ImageTk.")
    parser.add_argument("--hold-ms", type=int, default=0, help="Hold this diagnostic child briefly to test the launcher session lock.")
    args = parser.parse_args()
    if not 0 <= args.hold_ms <= 15000:
        parser.error("--hold-ms must be between 0 and 15000")
    app_root = Path(__file__).resolve().parents[1]
    runtime = app_root / "runtime" / "python"
    if Path(sys.executable).resolve().parent != runtime.resolve():
        raise RuntimeError("The interpreter is not the private PNG Mockup runtime.")
    if not sys.flags.isolated or not sys.flags.ignore_environment or not sys.flags.no_user_site:
        raise RuntimeError("The private runtime is not isolated from machine/user Python settings.")
    allowed = (runtime.resolve(), (app_root / "src").resolve())
    for entry in sys.path:
        path = Path(entry).resolve()
        if not any(path == base or base in path.parents for base in allowed):
            raise RuntimeError(f"External interpreter search path: {path}")
    import tkinter
    import PIL
    from PIL import Image, ImageDraw, ImageTk
    origins = {name: str(Path(module.__file__).resolve()) for name, module in {
        "tkinter": tkinter, "PIL": PIL, "PIL.Image": Image, "PIL.ImageTk": ImageTk,
    }.items()}
    for name, origin in origins.items():
        if runtime.resolve() not in Path(origin).parents:
            raise RuntimeError(f"External dependency loaded: {name}: {origin}")

    with Image.new("RGBA", (12, 12), (0, 0, 0, 0)) as sample:
        ImageDraw.Draw(sample).rectangle((2, 2, 9, 9), fill=(24, 136, 128, 255))
        if args.tk:
            os.environ["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
            os.environ["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")
            root = tkinter.Tk()
            root.withdraw()
            try:
                photo = ImageTk.PhotoImage(sample, master=root)
                assert (photo.width(), photo.height()) == sample.size
                root.update_idletasks()
            finally:
                root.destroy()
    if args.hold_ms:
        import time
        time.sleep(args.hold_ms / 1000)
    print(json.dumps({
        "product_id": "png-mockup", "python": sys.version.split()[0],
        "pillow": PIL.__version__, "tk": tkinter.TkVersion,
        "isolated": bool(sys.flags.isolated), "private_paths_only": True,
        "tk_image_verified": bool(args.tk),
        "executable": str(Path(sys.executable).resolve()),
        "search_paths": list(sys.path), "module_origins": origins,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
