# -*- coding: utf-8 -*-
"""Recursive mesh/texture exporter with a native Unreal settings dialog.

Enable Python Editor Script Plugin and Editor Scripting Utilities, restart UE,
then use File > Execute Python Script. No external Python modules needed.
Choose source/output folders in the dialog and press OK to export.
Subfolders are always included; other asset types are skipped.
Texture2D -> PNG/EXR/TGA/DDS via automatic Unreal exporter selection.
StaticMesh/SkeletalMesh -> FBX. Cube/volume textures are not included.
Texture export depends on available source data and Unreal exporter support.
export_journal.jsonl is flushed before each native load/export call.
No material baking or dependency collection outside the selected folder.
UE 5.7 APIs; syntax checked, not executed in an Unreal Editor here.
"""

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

import unreal


def checkpoint(output, entry):
    """Persist before calling native code: Python finally cannot catch an assert."""
    with (output / "export_journal.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def export_asset(asset, base_path, extensions, exporter_type, options, output, package):
    attempts = []
    base_path.parent.mkdir(parents=True, exist_ok=True)
    for extension in extensions:
        destination = Path(str(base_path) + "." + extension)
        if destination.exists():
            raise FileExistsError(str(destination))
        task = unreal.AssetExportTask()
        task.set_editor_property("object", asset)
        task.set_editor_property("filename", str(destination))
        # Critical: do NOT instantiate or assign a texture exporter.
        # Let Unreal select an exporter compatible with the object and extension.
        if exporter_type is not None:
            task.set_editor_property("exporter", exporter_type())
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", False)
        task.set_editor_property("selected", False)
        task.set_editor_property("write_empty_files", False)
        if extension == "fbx":
            task.set_editor_property("options", options)
        before = set(base_path.parent.iterdir())
        checkpoint(output, {"event": "exporting", "asset": package,
                            "file": str(destination), "format": extension})
        unreal.log("Exporting: " + package + " -> " + extension)
        unreal.log_flush()
        try:
            ok = unreal.Exporter.run_asset_export_task(task)
            messages = [str(e) for e in (task.get_editor_property("errors") or [])]
        except Exception as exc:
            ok = False
            messages = [str(exc)]
        # Unreal may emit multiple numbered files (e.g. UDIM tiles).
        created = sorted(p for p in set(base_path.parent.iterdir()) - before
                         if p.suffix.lower() == "." + extension
                         and p.is_file() and p.stat().st_size > 0)
        attempt = {"format": extension, "ok": bool(ok), "messages": messages,
                   "files": [str(p) for p in created]}
        attempts.append(attempt)
        checkpoint(output, {"event": "attempt_finished", "asset": package, **attempt})
        if ok and created:
            return {"format": extension, "files": attempt["files"], "attempts": attempts}
    raise RuntimeError("No successful export: " + json.dumps(attempts, ensure_ascii=False))


def export_folder(source_folder, output_folder, export_meshes=True,
                  export_textures=True, source_mesh=True):
    source = source_folder.strip().replace("\\", "/").rstrip("/")
    if source == "/All" or source.startswith("/All/"):
        source = source[4:]
    if not export_meshes and not export_textures:
        raise ValueError("Включи экспорт мешей или текстур.")
    if not source.startswith("/") or source == "" or ".." in source.split("/"):
        raise ValueError("Use an Unreal folder path such as /Game/MyAssets")
    base = Path(output_folder).expanduser()
    if not base.is_absolute():
        raise ValueError("OUTPUT_FOLDER must be an absolute filesystem path")

    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    registry.scan_paths_synchronous([source])
    assets = list(registry.get_assets_by_path(source, recursive=True) or [])
    if not assets:
        raise RuntimeError("No assets found in " + source)
    assets.sort(key=lambda item: str(item.package_name))

    output = base / (source.rsplit("/", 1)[-1] + "_" +
                     datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    output.mkdir(parents=True, exist_ok=False)
    specs = {
        "StaticMesh": ("fbx", unreal.StaticMeshExporterFBX),
        "SkeletalMesh": ("fbx", unreal.SkeletalMeshExporterFBX),
        "Texture2D": ("auto", None),
    }
    if not export_meshes:
        specs.pop("StaticMesh")
        specs.pop("SkeletalMesh")
    if not export_textures:
        specs.pop("Texture2D")
    options = unreal.FbxExportOption()
    options.set_editor_property("ascii", False)
    options.set_editor_property("export_source_mesh", source_mesh)
    options.set_editor_property("level_of_detail", False)
    options.set_editor_property("collision", False)
    options.set_editor_property("vertex_color", True)

    report = {"source": source, "output": str(output), "cancelled": False,
              "discovered": len(assets), "assets": []}
    try:
        with unreal.ScopedSlowTask(len(assets), "Export meshes and textures") as progress:
            progress.make_dialog(True)
            for data in assets:
                if progress.should_cancel():
                    report["cancelled"] = True
                    break
                package = str(data.package_name)
                kind = str(data.asset_class_path.asset_name)
                progress.enter_progress_frame(1, package)
                row = {"asset": package, "type": kind, "status": "skipped"}
                report["assets"].append(row)
                if kind not in specs:
                    row["reason"] = "Unsupported or disabled asset type"
                    continue
                try:
                    if not package.startswith(source + "/"):
                        raise ValueError("Asset is outside source folder")
                    relative = package[len(source) + 1:]
                    if ".." in relative.split("/"):
                        raise ValueError("Invalid relative path")
                    checkpoint(output, {"event": "loading", "asset": package, "type": kind})
                    asset = data.get_asset()
                    if asset is None:
                        raise RuntimeError("Could not load asset")
                    extension, exporter_type = specs[kind]
                    extensions = ("png", "exr", "tga", "dds") if kind == "Texture2D" else (extension,)
                    details = export_asset(asset, output / relative, extensions,
                                           exporter_type, options, output, package)
                    row.update(details)
                    row["status"] = "exported"
                    unreal.log("Exported: " + package)
                except Exception as exc:
                    row["status"] = "failed"
                    row["reason"] = str(exc)
                    unreal.log_warning("Export failed: " + package + " | " + str(exc))
                checkpoint(output, {"event": "finished", **row})
    finally:
        report["counts"] = dict(Counter(row["status"] for row in report["assets"]))
        report_path = output / "export_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        unreal.log("Export report: " + str(report_path))
        unreal.log("Export counts: " + str(report["counts"]))

    return report


@unreal.uclass()
class MeshTextureExportDialogSettings(unreal.Object):
    source_folder = unreal.uproperty(unreal.DirectoryPath, meta={
        "Category": "Export", "DisplayName": "Папка ассетов",
        "ContentDir": "", "LongPackageName": "",
        "ToolTip": "Папка Unreal, например /Game/Props. Все подпапки включены.",
        "DisplayPriority": "1"})
    output_folder = unreal.uproperty(unreal.DirectoryPath, meta={
        "Category": "Export", "DisplayName": "Куда экспортировать",
        "ToolTip": "Папка на диске. Для каждого запуска создаётся новый подкаталог.",
        "DisplayPriority": "2"})
    export_meshes = unreal.uproperty(bool, meta={
        "Category": "Export", "DisplayName": "Меши (FBX)",
        "DisplayPriority": "3"})
    export_textures = unreal.uproperty(bool, meta={
        "Category": "Export", "DisplayName": "Текстуры 2D (автоформат)",
        "DisplayPriority": "4"})
    source_mesh = unreal.uproperty(bool, meta={
        "Category": "Export", "DisplayName": "Исходная геометрия меша",
        "ToolTip": "Для Static Mesh использовать исходную геометрию вместо render mesh. "
                   "В этом режиме не экспортируются LOD и коллизия.",
        "DisplayPriority": "5"})


def show_export_dialog():
    settings = MeshTextureExportDialogSettings()
    selected = unreal.EditorUtilityLibrary.get_selected_folder_paths()
    initial_source = str(selected[0]) if len(selected) == 1 else "/Game"
    if initial_source.startswith("/All/"):
        initial_source = initial_source[4:]
    settings.set_editor_property("source_folder", unreal.DirectoryPath(path=initial_source))
    default_output = os.path.abspath(os.path.join(unreal.Paths.project_saved_dir(), "AssetExports"))
    settings.set_editor_property("output_folder", unreal.DirectoryPath(path=default_output))
    settings.set_editor_property("export_meshes", True)
    settings.set_editor_property("export_textures", True)
    settings.set_editor_property("source_mesh", True)

    while unreal.EditorDialog.show_object_details_view(
            "Экспорт мешей и текстур — OK для запуска", settings):
        try:
            result = export_folder(
                settings.get_editor_property("source_folder").path,
                settings.get_editor_property("output_folder").path,
                settings.get_editor_property("export_meshes"),
                settings.get_editor_property("export_textures"),
                settings.get_editor_property("source_mesh"))
        except Exception as exc:
            unreal.log_error(str(exc))
            unreal.EditorDialog.show_message(
                "Не удалось выполнить экспорт", str(exc), unreal.AppMsgType.OK)
            continue
        counts = result["counts"]
        message = (
            ("Экспорт прерван.\n" if result["cancelled"] else "Экспорт завершён.\n")
            + "Экспортировано: " + str(counts.get("exported", 0))
            + "\nОшибок: " + str(counts.get("failed", 0))
            + "\nПропущено: " + str(counts.get("skipped", 0))
            + "\n\nПапка: " + result["output"]
            + "\nПодробности: export_report.json")
        unreal.EditorDialog.show_message("Результат экспорта", message, unreal.AppMsgType.OK)
        break


if __name__ == "__main__":
    show_export_dialog()
