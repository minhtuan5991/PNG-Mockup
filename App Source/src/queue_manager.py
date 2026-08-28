from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

from atomic_json import atomic_write_json as _atomic_write_json
from config import AppConfig, PNG_IMAGE_EXTS, SUPPORTED_IMAGE_EXTS, load_config
from mockup_mode import PSD_MOCKUP_EXTS, explicit_sides_from_path


Job = dict[str, Any]
VARIANT_MARKERS = {"wh", "bl"}
PNG_SIDE_MARKERS = {"f", "b"}
PNG_NAME_MARKERS = VARIANT_MARKERS | PNG_SIDE_MARKERS
# Old PSD jobs never increment this counter, but preserve eligibility of saved jobs.
LEGACY_ATTEMPT_LIMIT = 3


def is_png_job(job: Any) -> bool:
    return (
        isinstance(job, dict)
        and job.get("runner") == "psd"
        and job.get("source_kind", "png") == "png"
    )


@dataclass(frozen=True)
class IncompleteVariantGroup:
    folder: Path
    base_name: str
    present: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class ParsedPngName:
    path: Path
    base_name: str
    color: str | None
    side: str | None


def image_files(folder: Path, extensions: tuple[str, ...] = SUPPORTED_IMAGE_EXTS) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(
        (
            path.resolve()
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in extensions
        ),
        key=lambda path: path.name.lower(),
    )


def psd_files(folder: Path) -> list[Path]:
    return image_files(folder, PSD_MOCKUP_EXTS)


def build_queue(
    config: AppConfig,
    preserve_existing: bool = True,
    save: bool = True,
) -> list[Job]:
    config.ensure_folders()
    existing = load_queue(config) if preserve_existing and config.queue_path.exists() else []
    existing_by_pair = {
        (_job_source_key(job), _norm_path(job["mockup_path"])): job
        for job in existing
        if job.get("mockup_path")
    }
    png_sources = _png_sources_from_folder(config.png_dir)
    local_psd_mockups = [
        path for path in psd_files(config.mockup_dir)
        if explicit_sides_from_path(path) is not None
    ]
    jobs: list[Job] = []
    for png_source, psd_path in product(png_sources, local_psd_mockups):
        design_sides = int(png_source["idea_sides"])
        mockup_sides = explicit_sides_from_path(psd_path)
        if mockup_sides is None or design_sides != mockup_sides:
            continue
        replacements = _psd_replacements_for_source(png_source, mockup_sides)
        if not replacements:
            continue
        artwork_paths = _psd_artwork_paths_from_replacements(replacements)
        output_prefix = f"{_safe_text(str(png_source['output_stem']))}__{_safe_stem(psd_path)}"
        key = (_norm_paths(artwork_paths), _norm_path(psd_path))
        previous = existing_by_pair.get(key, {})
        jobs.append({
            "id": previous.get("id", f"{len(jobs) + 1:06d}"),
            "runner": "psd",
            "idea_path": str(artwork_paths[0]),
            "artwork_paths": [str(path) for path in artwork_paths],
            "source_kind": "png",
            "idea_sides": design_sides,
            "idea_variant": png_source.get("selected_variant") or "psd",
            "idea_variant_pair": bool(png_source.get("variant_pair")),
            "png_front_back_pair": bool(png_source.get("png_front_back_pair")),
            "mockup_path": str(psd_path),
            "mockup_sides": mockup_sides,
            "mockup_tone": "psd",
            "psd_output_prefix": output_prefix,
            "psd_replacements": replacements,
            "output_name": f"{output_prefix}__PSD_JOB.png",
            "status": previous.get("status", "pending"),
            "attempts": int(previous.get("attempts", 0)),
            "last_error": previous.get("last_error"),
        })
    jobs.sort(key=lambda item: item["id"])
    if save:
        save_queue(config, jobs)
    return jobs


def find_incomplete_variant_groups(config: AppConfig) -> list[IncompleteVariantGroup]:
    config.ensure_folders()
    return _png_incomplete_groups(config.png_dir)


def _png_sources_from_folder(folder: Path) -> list[dict[str, Any]]:
    normal: list[dict[str, Any]] = []
    color_groups: dict[str, dict[str, Any]] = {}
    side_groups: dict[str, dict[str, dict[str, Path]]] = {}

    for path in image_files(folder, PNG_IMAGE_EXTS):
        parsed = _parse_png_name(path)
        if parsed.side is None and parsed.color is None:
            normal.append(
                {
                    "idea_path": path,
                    "artwork_paths": [path],
                    "idea_sides": 1,
                    "output_stem": parsed.base_name,
                    "source_kind": "png",
                    "variant_pair": False,
                }
            )
            continue

        if parsed.side is None and parsed.color in VARIANT_MARKERS:
            group = color_groups.setdefault(
                parsed.base_name.casefold(),
                {
                    "idea_sides": 1,
                    "output_stem": parsed.base_name,
                    "source_kind": "png",
                    "variants": {},
                },
            )
            group["variants"].setdefault(parsed.color, path)
            continue

        if parsed.side in PNG_SIDE_MARKERS:
            color_key = parsed.color or "plain"
            side_group = side_groups.setdefault(parsed.base_name.casefold(), {})
            color_group = side_group.setdefault(color_key, {})
            color_group.setdefault(parsed.side, path)
            continue

        normal.append(
            {
                "idea_path": path,
                "artwork_paths": [path],
                "idea_sides": 1,
                "output_stem": parsed.base_name,
                "source_kind": "png",
                "variant_pair": False,
            }
        )

    sources = normal
    for group in color_groups.values():
        variants = group["variants"]
        if set(variants) == VARIANT_MARKERS:
            sources.append(
                {
                    "idea_sides": 1,
                    "output_stem": group["output_stem"],
                    "source_kind": "png",
                    "variant_pair": True,
                    "variants": variants,
                }
            )
            continue

        for marker, path in sorted(variants.items()):
            sources.append(
                {
                    "idea_path": path,
                    "artwork_paths": [path],
                    "idea_sides": 1,
                    "output_stem": group["output_stem"],
                    "source_kind": "png",
                    "variant_pair": False,
                    "selected_variant": marker,
                }
            )

    for base_key, side_group in sorted(side_groups.items()):
        shared_side_variants = _png_shared_side_color_variants(side_group)
        if shared_side_variants is not None:
            first_path = shared_side_variants["bl"][0]
            sources.append(
                {
                    "idea_sides": 2,
                    "output_stem": _parse_png_name(first_path).base_name,
                    "source_kind": "png",
                    "variant_pair": True,
                    "png_front_back_pair": True,
                    "variants": shared_side_variants,
                }
            )
            continue

        plain = side_group.get("plain", {})
        if set(plain) == PNG_SIDE_MARKERS:
            sources.append(_png_two_side_source(base_key, plain))

        color_variants = {
            color: sides
            for color, sides in side_group.items()
            if color in VARIANT_MARKERS and set(sides) == PNG_SIDE_MARKERS
        }
        if set(color_variants) == VARIANT_MARKERS:
            front_path = color_variants["wh"]["f"]
            sources.append(
                {
                    "idea_sides": 2,
                    "output_stem": _parse_png_name(front_path).base_name,
                    "source_kind": "png",
                    "variant_pair": True,
                    "png_front_back_pair": True,
                    "variants": {
                        color: [sides["f"], sides["b"]]
                        for color, sides in color_variants.items()
                    },
                }
            )
            continue

        for color, sides in sorted(color_variants.items()):
            paths = [sides["f"], sides["b"]]
            sources.append(
                {
                    "idea_path": paths[0],
                    "artwork_paths": paths,
                    "idea_sides": 2,
                    "output_stem": _parse_png_name(paths[0]).base_name,
                    "source_kind": "png",
                    "variant_pair": False,
                    "png_front_back_pair": True,
                    "selected_variant": color,
                }
            )

        # A file explicitly marked .f or .b belongs to a two-sided design.
        # When its counterpart is missing, keep it out of the queue instead of
        # silently turning it into a one-sided PSD job. The validation dialog
        # already reports the missing side before the user starts a run.

    return sources


def _png_shared_side_color_variants(
    side_group: dict[str, dict[str, Path]],
) -> dict[str, list[Path]] | None:
    """
    Build a two-colour 2S design where one side is shared by both colours.

    Valid examples contain exactly three files:
      design.f + design.b.bl + design.b.wh
      design.b + design.f.bl + design.f.wh
    Marker order in the file name is irrelevant because _parse_png_name()
    already accepts both .f.bl and .bl.f forms.
    """

    if set(side_group) != ({"plain"} | VARIANT_MARKERS):
        return None

    plain_sides = side_group["plain"]
    if len(plain_sides) != 1:
        return None

    shared_side = next(iter(plain_sides))
    colour_specific_side = "b" if shared_side == "f" else "f"
    if any(
        set(side_group[color]) != {colour_specific_side}
        for color in VARIANT_MARKERS
    ):
        return None

    shared_path = plain_sides[shared_side]
    variants: dict[str, list[Path]] = {}
    for color in sorted(VARIANT_MARKERS):
        specific_path = side_group[color][colour_specific_side]
        if shared_side == "f":
            variants[color] = [shared_path, specific_path]
        else:
            variants[color] = [specific_path, shared_path]
    return variants


def _png_two_side_source(base_key: str, sides: dict[str, Path]) -> dict[str, Any]:
    del base_key
    paths = [sides["f"], sides["b"]]
    return {
        "idea_path": paths[0],
        "artwork_paths": paths,
        "idea_sides": 2,
        "output_stem": _parse_png_name(paths[0]).base_name,
        "source_kind": "png",
        "variant_pair": False,
        "png_front_back_pair": True,
    }


def _psd_replacements_for_source(
    source: dict[str, Any], mockup_sides: int
) -> list[dict[str, Any]]:
    if mockup_sides == 2:
        dark_paths = _source_paths_for_variant(source, "bl")
        light_paths = _source_paths_for_variant(source, "wh")
        if len(dark_paths) < 2 or len(light_paths) < 2:
            return []

        return [
            {
                "slot": "dark_front",
                "layer_names": [
                    "Design Dark Front",
                    "Design Front Dark",
                    "Front Design Dark",
                    "Dark Front",
                ],
                "path": str(dark_paths[0]),
            },
            {
                "slot": "dark_back",
                "layer_names": [
                    "Design Dark Back",
                    "Design Back Dark",
                    "Back Design Dark",
                    "Dark Back",
                ],
                "path": str(dark_paths[1]),
            },
            {
                "slot": "light_front",
                "layer_names": [
                    "Design Light Front",
                    "Design Front Light",
                    "Front Design Light",
                    "Light Front",
                ],
                "path": str(light_paths[0]),
            },
            {
                "slot": "light_back",
                "layer_names": [
                    "Design Light Back",
                    "Design Back Light",
                    "Back Design Light",
                    "Light Back",
                ],
                "path": str(light_paths[1]),
            },
        ]

    dark_paths = _source_paths_for_variant(source, "bl")
    light_paths = _source_paths_for_variant(source, "wh")
    if not dark_paths or not light_paths:
        return []

    return [
        {
            "slot": "dark",
            "layer_names": ["Design Dark"],
            "path": str(dark_paths[0]),
        },
        {
            "slot": "light",
            "layer_names": ["Design Light"],
            "path": str(light_paths[0]),
        },
    ]


def _source_paths_for_variant(source: dict[str, Any], marker: str) -> list[Path]:
    if source.get("variant_pair"):
        selected = source.get("variants", {}).get(marker)
        if isinstance(selected, list):
            return [Path(path) for path in selected]
        if selected:
            return [Path(selected)]

    paths = source.get("artwork_paths")
    if paths:
        return [Path(path) for path in paths]
    return [Path(source["idea_path"])]


def _psd_artwork_paths_from_replacements(replacements: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for replacement in replacements:
        path = Path(str(replacement["path"]))
        key = _norm_path(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _png_incomplete_groups(folder: Path) -> list[IncompleteVariantGroup]:
    if not folder.exists():
        return []

    parsed_files = [_parse_png_name(path) for path in image_files(folder, PNG_IMAGE_EXTS)]
    groups: list[IncompleteVariantGroup] = []

    color_grouped: dict[str, set[str]] = {}
    side_grouped: dict[str, set[str]] = {}
    structured_side_groups: dict[str, dict[str, dict[str, Path]]] = {}
    display_name: dict[str, str] = {}

    for parsed in parsed_files:
        display_name.setdefault(parsed.base_name.casefold(), parsed.base_name)
        if parsed.color in VARIANT_MARKERS:
            color_key = "|".join(
                [
                    parsed.base_name.casefold(),
                    "two-side" if parsed.side in PNG_SIDE_MARKERS else "one-side",
                ]
            )
            color_grouped.setdefault(color_key, set()).add(parsed.color)
        if parsed.side in PNG_SIDE_MARKERS:
            color_key = parsed.color or "plain"
            structured_side_groups.setdefault(
                parsed.base_name.casefold(),
                {},
            ).setdefault(color_key, {}).setdefault(parsed.side, parsed.path)
            side_key = "|".join(
                [
                    parsed.base_name.casefold(),
                    color_key,
                ]
            )
            side_grouped.setdefault(side_key, set()).add(parsed.side)

    valid_shared_side_groups = {
        base_key
        for base_key, side_group in structured_side_groups.items()
        if _png_shared_side_color_variants(side_group) is not None
    }

    for key, present_markers in color_grouped.items():
        if present_markers == VARIANT_MARKERS:
            continue
        base_key = key.split("|", 1)[0]
        if base_key in valid_shared_side_groups:
            continue
        groups.append(
            IncompleteVariantGroup(
                folder=folder,
                base_name=display_name.get(base_key, base_key),
                present=tuple(sorted(present_markers)),
                missing=tuple(sorted(VARIANT_MARKERS - present_markers)),
            )
        )

    for key, present_markers in side_grouped.items():
        if present_markers == PNG_SIDE_MARKERS:
            continue
        base_key = key.split("|", 1)[0]
        if base_key in valid_shared_side_groups:
            continue
        groups.append(
            IncompleteVariantGroup(
                folder=folder,
                base_name=display_name.get(base_key, base_key),
                present=tuple(sorted(present_markers)),
                missing=tuple(sorted(PNG_SIDE_MARKERS - present_markers)),
            )
        )

    return groups


def _parse_png_name(path: Path) -> ParsedPngName:
    parts = path.stem.split(".")
    markers: list[str] = []
    while parts and parts[-1].casefold() in PNG_NAME_MARKERS:
        markers.append(parts.pop().casefold())

    base_name = ".".join(parts).strip() or path.stem
    color = next((marker for marker in markers if marker in VARIANT_MARKERS), None)
    side = next((marker for marker in markers if marker in PNG_SIDE_MARKERS), None)
    return ParsedPngName(path=path, base_name=base_name, color=color, side=side)


def load_queue(config: AppConfig) -> list[Job]:
    if not config.queue_path.exists():
        return []
    data = json.loads(config.queue_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Queue must be a JSON list: {config.queue_path}")
    # Ignore unrelated jobs if an old application's queue is copied here manually.
    return [job for job in data if is_png_job(job)]


def save_queue(config: AppConfig, jobs: list[Job]) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(config.queue_path, [job for job in jobs if is_png_job(job)])


def pending_jobs(config: AppConfig, jobs: list[Job]) -> list[Job]:
    selected: list[Job] = []
    for job in jobs:
        if not is_png_job(job):
            continue
        if config.rerun_done and job["status"] == "done":
            job["status"] = "pending"
        if job["status"] in {"pending", "running"}:
            selected.append(job)
        elif job["status"] == "failed" and int(job.get("attempts", 0)) < LEGACY_ATTEMPT_LIMIT:
            selected.append(job)
    return selected


def _safe_stem(path: Path) -> str:
    return _safe_text(path.stem)


def _safe_text(value: str) -> str:
    stem = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return stem or "image"


def _norm_path(path: str | Path) -> str:
    return str(Path(path).resolve()).casefold()


def _norm_paths(paths: list[Path] | tuple[Path, ...]) -> str:
    return "||".join(_norm_path(path) for path in paths)


def _job_source_key(job: dict[str, Any]) -> str:
    paths = job.get("artwork_paths")
    if isinstance(paths, list) and paths:
        return "||".join(_norm_path(path) for path in paths)
    return _norm_path(job["idea_path"])


def main() -> int:
    config = load_config()
    jobs = build_queue(config)
    print(f"Built queue with {len(jobs)} PNG job(s): {config.queue_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
