#target photoshop

(function () {
    var openedDoc = null;
    var previousUnits = app.preferences.rulerUnits;
    var previousDisplayDialogs = app.displayDialogs;

    function parseJson(text) {
        if (typeof JSON !== "undefined" && JSON.parse) {
            return JSON.parse(text);
        }
        return eval("(" + text + ")");
    }

    function jsonEscape(text) {
        return String(text)
            .replace(/\\/g, "\\\\")
            .replace(/"/g, "\\\"")
            .replace(/\r/g, "\\r")
            .replace(/\n/g, "\\n")
            .replace(/\t/g, "\\t");
    }

    function stringifyJson(value) {
        var type = typeof value;
        var i;
        var parts;
        if (value === null) {
            return "null";
        }
        if (type === "string") {
            return "\"" + jsonEscape(value) + "\"";
        }
        if (type === "number" || type === "boolean") {
            return String(value);
        }
        if (value instanceof Array) {
            parts = [];
            for (i = 0; i < value.length; i += 1) {
                parts.push(stringifyJson(value[i]));
            }
            return "[" + parts.join(",") + "]";
        }
        if (type === "object") {
            parts = [];
            for (i in value) {
                if (value.hasOwnProperty(i)) {
                    parts.push(stringifyJson(i) + ":" + stringifyJson(value[i]));
                }
            }
            return "{" + parts.join(",") + "}";
        }
        return "null";
    }

    function readJson(path) {
        var file = File(path);
        if (!file.exists) {
            throw new Error("Job JSON not found: " + path);
        }
        file.encoding = "UTF8";
        if (!file.open("r")) {
            throw new Error("Cannot open job JSON: " + path);
        }
        var text = file.read();
        file.close();
        return parseJson(text);
    }

    function writeResult(path, payload) {
        var file = File(path);
        var folder = file.parent;
        if (!folder.exists) {
            folder.create();
        }
        file.encoding = "UTF8";
        if (!file.open("w")) {
            throw new Error("Cannot write result JSON: " + path);
        }
        file.write(stringifyJson(payload));
        file.close();
    }

    function normalizeName(value) {
        return String(value).replace(/^\s+|\s+$/g, "").toLowerCase();
    }

    function layerNameMatches(layer, names) {
        var layerName = normalizeName(layer.name);
        for (var i = 0; i < names.length; i += 1) {
            if (layerName === normalizeName(names[i])) {
                return true;
            }
        }
        return false;
    }

    function collectMatchingLayers(container, names, matches) {
        for (var i = 0; i < container.layers.length; i += 1) {
            var layer = container.layers[i];
            if (layer.typename === "ArtLayer") {
                if (layerNameMatches(layer, names)) {
                    matches.push(layer);
                }
            } else if (layer.typename === "LayerSet") {
                collectMatchingLayers(layer, names, matches);
            }
        }
    }

    function replaceSmartObject(layer, designPath) {
        if (layer.typename !== "ArtLayer" || layer.kind !== LayerKind.SMARTOBJECT) {
            throw new Error("Layer is not a smart object: " + layer.name);
        }
        app.activeDocument.activeLayer = layer;

        var descriptor = new ActionDescriptor();
        descriptor.putPath(charIDToTypeID("null"), File(designPath));
        executeAction(stringIDToTypeID("placedLayerReplaceContents"), descriptor, DialogModes.NO);
    }

    function applyReplacement(doc, replacement) {
        var names = replacement.layer_names || [];
        if (!names.length) {
            throw new Error("PSD replacement has no layer names.");
        }

        var matches = [];
        collectMatchingLayers(doc, names, matches);
        if (!matches.length) {
            throw new Error("Smart layer not found: " + names.join(" / "));
        }

        for (var i = 0; i < matches.length; i += 1) {
            replaceSmartObject(matches[i], replacement.path);
        }
    }

    function topLayerSets(doc) {
        var groups = [];
        for (var i = 0; i < doc.layers.length; i += 1) {
            if (doc.layers[i].typename === "LayerSet") {
                groups.push(doc.layers[i]);
            }
        }
        return groups;
    }

    function collectVisibility(container, records) {
        for (var i = 0; i < container.layers.length; i += 1) {
            var layer = container.layers[i];
            records.push({ layer: layer, visible: layer.visible });
            if (layer.typename === "LayerSet") {
                collectVisibility(layer, records);
            }
        }
    }

    function restoreVisibility(records) {
        for (var i = records.length - 1; i >= 0; i -= 1) {
            try {
                records[i].layer.visible = records[i].visible;
            } catch (ignored) {
            }
        }
    }

    function safeName(value) {
        var text = String(value).replace(/^\s+|\s+$/g, "");
        text = text.replace(/\.(png|jpg|jpeg|webp|psd|psb)$/i, "");
        text = text.replace(/[\\\/:\*\?"<>\|]+/g, "_");
        text = text.replace(/\s+/g, "_");
        text = text.replace(/^[\._]+|[\._]+$/g, "");
        return text || "group";
    }

    function exportPng(doc, filePath) {
        var options = new PNGSaveOptions();
        options.compression = 6;
        options.interlaced = false;
        doc.saveAs(File(filePath), options, true, Extension.LOWERCASE);
    }

    function exportGroups(doc, exportDirPath, outputPrefix) {
        var exportDir = Folder(exportDirPath);
        if (!exportDir.exists) {
            exportDir.create();
        }

        var outputs = [];
        var groups = topLayerSets(doc);
        if (!groups.length) {
            var mergedPath = exportDir.fsName + "/" + safeName(outputPrefix) + ".png";
            exportPng(doc, mergedPath);
            outputs.push(mergedPath);
            return outputs;
        }

        var visibilityRecords = [];
        collectVisibility(doc, visibilityRecords);

        try {
            for (var i = 0; i < groups.length; i += 1) {
                for (var j = 0; j < groups.length; j += 1) {
                    groups[j].visible = (i === j);
                }
                var fileName = safeName(outputPrefix) + "__" + ("00" + (i + 1)).slice(-2) + "_" + safeName(groups[i].name) + ".png";
                var filePath = exportDir.fsName + "/" + fileName;
                exportPng(doc, filePath);
                outputs.push(filePath);
            }
        } finally {
            restoreVisibility(visibilityRecords);
        }

        return outputs;
    }

    try {
        if (typeof PSD_MOCKUP_JOB_PATH === "undefined") {
            throw new Error("PSD_MOCKUP_JOB_PATH is not defined.");
        }

        var spec = (typeof PSD_MOCKUP_SPEC !== "undefined")
            ? PSD_MOCKUP_SPEC
            : readJson(PSD_MOCKUP_JOB_PATH);
        app.displayDialogs = DialogModes.NO;
        app.preferences.rulerUnits = Units.PIXELS;

        openedDoc = app.open(File(spec.psd_path));

        for (var r = 0; r < spec.replacements.length; r += 1) {
            applyReplacement(openedDoc, spec.replacements[r]);
        }

        var exported = exportGroups(openedDoc, spec.export_dir, spec.output_prefix);
        openedDoc.close(SaveOptions.DONOTSAVECHANGES);
        openedDoc = null;

        writeResult(spec.result_path, { ok: true, outputs: exported });
    } catch (error) {
        try {
            if (openedDoc !== null) {
                openedDoc.close(SaveOptions.DONOTSAVECHANGES);
            }
        } catch (ignoredCloseError) {
        }

        try {
            var fallbackSpec = (typeof PSD_MOCKUP_SPEC !== "undefined")
                ? PSD_MOCKUP_SPEC
                : readJson(PSD_MOCKUP_JOB_PATH);
            writeResult(fallbackSpec.result_path, {
                ok: false,
                error: String(error && error.message ? error.message : error)
            });
        } catch (ignoredResultError) {
            throw error;
        }
    } finally {
        app.preferences.rulerUnits = previousUnits;
        app.displayDialogs = previousDisplayDialogs;
    }
})();
