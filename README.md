# Escape Room Tools

Unity UPM package for escape room game development.
Automates Blender → Unity FBX import, lighting setup, and scene initialization.

## Requirements

- Unity 6000.0 or later
- Universal Render Pipeline (URP)

## Installation

In Unity, open **Window > Package Manager**, click **+**, and choose **Add package from git URL**:

```
https://github.com/noprops/escaperoom-tools.git
```

To pin a specific version:

```
https://github.com/noprops/escaperoom-tools.git#v1.0.0
```

## Features

### Blender Room Launcher (`Tools > Room Importer > Bake & Export from Blender`)

Launches Blender in background to export a collection hierarchy as FBX.

- Protects the original `.blend` by working on a temporary copy
- Triggers `AssetDatabase.Refresh()` on completion
- Optional: runs `Lightmapping.BakeAsync()` after import (opt-in checkbox)

**Setup:**
1. Set **Blender executable path** (default: `/Applications/Blender.app/Contents/MacOS/Blender`)
2. Set **.blend file** (default: `<ProjectRoot>/SourceAssets/Room.blend`)
3. Set **Collection name** to export (default: `Root`)
4. Set **Export folder** (default: `Assets/0/FBX/`)

### Room FBX Post Processor (automatic on FBX import under `Assets/0/FBX/`)

Runs automatically whenever an FBX is imported into `Assets/0/FBX/`.

**Per-FBX:**
- Resets `root.localScale` to `Vector3.one` (compensates Blender UnitScaleFactor=100)
- Assigns textures from `FBX/Textures/` folder (`_BaseMap`, `_BumpMap`, `_MaskMap`, `_EmissionMap`)
- Sets `ContributeGI` + `ReceiveGI.Lightmaps` on static meshes
- Sets `ReceiveGI.LightProbes` on dynamic meshes (objects whose name ends with `_DYN`)

**Per-scene (idempotent — runs once, skips if already configured):**
- Creates and assigns a `LightingSettings` asset (`Assets/Settings/LightingSettings.lighting`)
- Sets Lighting Mode to **Mixed / Baked Indirect**
- Sets Environment Lighting to **Color (black)**, Intensity = 0
- Converts Realtime lights to **Mixed**
- Creates **Global Volume** with Tonemapping (ACES) profile
- Creates **Adaptive Probe Volume** (Global mode)
- Creates **Reflection Probe** (Baked)

> **Prerequisite:** Enable APV in your URP Asset:
> `Light Probe System` → `Adaptive Probe Volumes`
> If disabled, scene setup is skipped and a warning is logged.

## Naming Convention

Objects in Blender that should be **dynamic** (doors, drawers, pickup items, NPCs) must have `_DYN` at the end of their name:

```
Door_01_DYN        ← dynamic (no ContributeGI)
Key_RoomA_DYN      ← dynamic
Wall_North         ← static (ContributeGI applied)
Floor_01           ← static
```

## Project Folder Convention

The post processor watches `Assets/0/FBX/`. Create this folder in new projects and place exported FBX files there.

```
Assets/
  0/
    FBX/
      Room.fbx
      Textures/
        Mat_Wall_BaseMap.png
        Mat_Wall_BumpMap.png
```

## License

MIT
