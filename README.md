# BB Layer Cameras

A Blender extension that lets you assign a dedicated camera to each View Layer and automatically switches cameras at render time.

**Authors:** Blender Bob · Claude AI  
**Blender:** 4.2+ (Extension) · also works as a legacy addon in 4.0–4.1  
**Location:** Properties → View Layer → BB Layer Cameras

---

## The Problem

Blender's View Layers share a single scene camera. There is no built-in way to assign a different camera per layer — which is a common need in multi-angle or multi-pass production pipelines.

## What This Extension Does

- Displays all View Layers in a panel with a camera dropdown for each
- On a standard F12 render, automatically swaps to the active layer's assigned camera (and restores afterward)
- **Intercept F12** mode: remaps F12 to render every enabled layer in sequence, each with its correct camera, outputting to `<filepath>/<LayerName>/`
- **Multilayer EXR mode**: detects when your output is set to Multilayer EXR and provides a dedicated operator that renders each layer separately then merges them into a single multilayer EXR via the compositor

---

## Installation

### As a Blender Extension (4.2+)
1. Download `bb_layer_cameras.zip`
2. In Blender: **Edit → Preferences → Get Extensions → Install from Disk**
3. Select the zip — done

### As a Legacy Addon (4.0–4.1)
1. Download `bb_layer_cameras.zip`
2. In Blender: **Edit → Preferences → Add-ons → Install**
3. Select the zip and enable **BB Layer Cameras**

---

## Usage

### Setup
1. Open **Properties → View Layer** and expand the **BB Layer Cameras** panel
2. Click **↺** to sync the list with your current View Layers
3. For each layer, pick the camera you want from the dropdown

### Rendering

| Output Format | What to do |
|---|---|
| Any non-EXR / single EXR | Enable **Intercept F12** and press F12, or click **BB Render All Layers** |
| Multilayer EXR | Click **BB Render & Merge Multilayer EXR** |

### Outputs

- **BB Render All Layers** writes each layer to its own subfolder: `<your output path>/<LayerName>/0001.png`
- **BB Render & Merge Multilayer EXR** writes a single `bb_multilayer.exr` to your output folder, containing all layers

---

## Notes

- The panel is per-scene and camera assignments are saved with your `.blend` file
- If you add or rename View Layers, click **↺ Sync** to refresh the list
- The Multilayer EXR merge temporarily replaces your compositor tree during the render and fully restores it afterward — your compositor setup is not modified
- Blender 5.x users: the extension uses the new `media_type` API for EXR format detection and the redesigned File Output node API (`directory`, `file_name`, `file_output_items`)

---

## Compatibility

| Blender | Status |
|---|---|
| 5.x | ✅ Fully supported (new compositor + media_type API) |
| 4.2–4.5 | ✅ Fully supported |
| 4.0–4.1 | ✅ Works as legacy addon |
| 3.x | ⚠️ Not tested |

---

## License

GPL-2.0-or-later — see [SPDX](https://spdx.org/licenses/GPL-2.0-or-later.html)
