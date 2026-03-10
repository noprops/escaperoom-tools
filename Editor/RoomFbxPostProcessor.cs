#if UNITY_EDITOR
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.Universal;
using UnityEngine.SceneManagement;

/// <summary>
/// Assets/FBX/ 以下の FBX をインポートしたとき自動処理を行う。
///
/// [FBX アセット側]
/// - root.localScale を Vector3.one にリセット (Blender UnitScaleFactor=100 補正)
/// - _DYN サフィックスなし → StaticEditorFlags.ContributeGI + ReceiveGI.Lightmaps
/// - _DYN サフィックスあり → ReceiveGI.LightProbes (動的オブジェクト扱い)
/// - URP Lit マテリアルにベイク済みテクスチャを割り当て
///
/// [シーン側 / idempotent]
/// - LightingSettings アセット: 未割り当てなら作成して割り当て
/// - Lighting Settings: Baked GI 有効 + Mixed = Baked Indirect (未設定なら)
/// - Environment Lighting: Source = Color, 黒, Intensity = 0 (Skybox モードなら変更)
/// - シーン内ライト: Realtime → Mixed (Realtime のものがあれば)
/// - Global Volume: 未存在なら isGlobal = true + VolumeProfile (Tonemapping ACES) で生成
/// - Adaptive Probe Volume: 未存在なら Global モードで生成
/// - Reflection Probe: 未存在なら生成
/// </summary>
public class RoomFbxPostProcessor : AssetPostprocessor
{
    private const string WatchFolder = "Assets/FBX";
    private const string DynSuffix   = "_DYN";

    // -------------------------------------------------------
    // インポート前処理
    // -------------------------------------------------------

    void OnPreprocessModel()
    {
        if (!assetPath.Replace('\\', '/').StartsWith(WatchFolder)) return;
        var importer = assetImporter as ModelImporter;
        if (importer == null) return;

        importer.materialImportMode = ModelImporterMaterialImportMode.ImportViaMaterialDescription;
        importer.materialLocation   = ModelImporterMaterialLocation.External;
        importer.materialName       = ModelImporterMaterialName.BasedOnMaterialName;
        importer.materialSearch     = ModelImporterMaterialSearch.Local;
    }

    // -------------------------------------------------------
    // インポート後処理
    // -------------------------------------------------------

    void OnPostprocessModel(GameObject root)
    {
        if (!assetPath.Replace('\\', '/').StartsWith(WatchFolder)) return;

        // Scale リセット (Blender FBX の UnitScaleFactor=100 を Unity が自動適用するのを上書き)
        root.transform.localScale = Vector3.one;

        // テクスチャ割り当て
        string fbxDir     = Path.GetDirectoryName(assetPath).Replace('\\', '/');
        string textureDir = fbxDir + "/Textures";
        foreach (var renderer in root.GetComponentsInChildren<MeshRenderer>(true))
        {
            foreach (var mat in renderer.sharedMaterials)
            {
                if (mat == null) continue;
                if (AssignTextures(mat, textureDir))
                    EditorUtility.SetDirty(mat);
            }
        }

        // GI フラグ: _DYN サフィックスで Static / 動的を判定
        foreach (var renderer in root.GetComponentsInChildren<MeshRenderer>(true))
        {
            if (renderer.gameObject.name.EndsWith(DynSuffix))
            {
                // 動的オブジェクト: APV からサンプリング
                renderer.receiveGI = ReceiveGI.LightProbes;
            }
            else
            {
                // 静的オブジェクト: GI ベイク対象
                var flags = GameObjectUtility.GetStaticEditorFlags(renderer.gameObject);
                GameObjectUtility.SetStaticEditorFlags(renderer.gameObject,
                    flags | StaticEditorFlags.ContributeGI);
                renderer.receiveGI = ReceiveGI.Lightmaps;
            }
        }

        // シーンレベルの設定はインポート完了後に実行 (delayCall でメインスレッドに戻す)
        EditorApplication.delayCall += SetupSceneLighting;
    }

    // -------------------------------------------------------
    // シーンライティング設定 (idempotent)
    // -------------------------------------------------------

    private static void SetupSceneLighting()
    {
        // APV 有効化チェック
        var urpAsset = GraphicsSettings.currentRenderPipeline as UniversalRenderPipelineAsset;
        if (urpAsset == null || urpAsset.lightProbeSystem != LightProbeSystem.ProbeVolumes)
        {
            Debug.LogWarning(
                "[RoomFbxPostProcessor] APV が無効です。" +
                "URP Asset (PC_RPAsset / Mobile_RPAsset) の Light Probe System を " +
                "Adaptive Probe Volumes に変更してからもう一度 FBX をインポートしてください。" +
                "シーン設定をスキップします。");
            return;
        }

        bool sceneChanged = false;

        // 1. LightingSettings アセット: 未割り当てなら作成して割り当て
        if (Lightmapping.lightingSettings == null)
        {
            const string settingsPath = "Assets/Settings/LightingSettings.lighting";
            var ls = AssetDatabase.LoadAssetAtPath<LightingSettings>(settingsPath);
            if (ls == null)
            {
                ls = new LightingSettings();
                if (!AssetDatabase.IsValidFolder("Assets/Settings"))
                    AssetDatabase.CreateFolder("Assets", "Settings");
                AssetDatabase.CreateAsset(ls, settingsPath);
                AssetDatabase.SaveAssets();
            }
            Lightmapping.lightingSettings = ls;
            Debug.Log("[RoomFbxPostProcessor] LightingSettings アセットを割り当てました。");
        }

        // 2. Lighting Settings: Baked GI 有効 + Mixed Lighting = Baked Indirect
        var lightingSettings = Lightmapping.lightingSettings;
        if (lightingSettings != null &&
            (!lightingSettings.bakedGI ||
             lightingSettings.mixedBakeMode != MixedLightingMode.IndirectOnly))
        {
            lightingSettings.bakedGI       = true;
            lightingSettings.mixedBakeMode = MixedLightingMode.IndirectOnly;
            Debug.Log("[RoomFbxPostProcessor] Lighting を Baked Indirect (Mixed) に設定しました。");
        }

        // 3. Environment Lighting: Skybox モードなら Color (黒) に変更
        if (RenderSettings.ambientMode != AmbientMode.Flat)
        {
            RenderSettings.ambientMode      = AmbientMode.Flat;
            RenderSettings.ambientLight     = Color.black;
            RenderSettings.ambientIntensity = 0f;
            sceneChanged = true;
            Debug.Log("[RoomFbxPostProcessor] Environment Lighting を Color (黒) に設定しました。");
        }

        // 4. シーン内ライト: Realtime のものを Mixed に変更
        foreach (var light in Object.FindObjectsByType<Light>(
                     FindObjectsInactive.Include, FindObjectsSortMode.None))
        {
            if (light.lightmapBakeType == LightmapBakeType.Realtime)
            {
                light.lightmapBakeType = LightmapBakeType.Mixed;
                EditorUtility.SetDirty(light.gameObject);
                sceneChanged = true;
                Debug.Log($"[RoomFbxPostProcessor] Light '{light.name}' を Mixed に変更しました。");
            }
        }

        // 5. Global Volume: 未存在なら生成
        if (Object.FindFirstObjectByType<Volume>() == null)
        {
            const string profilePath = "Assets/Settings/EscapeRoomVolumeProfile.asset";
            var profile = AssetDatabase.LoadAssetAtPath<VolumeProfile>(profilePath);
            if (profile == null)
            {
                profile = ScriptableObject.CreateInstance<VolumeProfile>();
                if (!AssetDatabase.IsValidFolder("Assets/Settings"))
                    AssetDatabase.CreateFolder("Assets", "Settings");
                AssetDatabase.CreateAsset(profile, profilePath);

                var tonemap = profile.Add<Tonemapping>(true);
                tonemap.mode.overrideState = true;
                tonemap.mode.value         = TonemappingMode.ACES;

                EditorUtility.SetDirty(profile);
                AssetDatabase.SaveAssets();
            }

            var go  = new GameObject("Global Volume");
            var vol = go.AddComponent<Volume>();
            vol.isGlobal      = true;
            vol.sharedProfile = profile;
            Undo.RegisterCreatedObjectUndo(go, "Create Global Volume");
            sceneChanged = true;
            Debug.Log("[RoomFbxPostProcessor] Global Volume を生成しました。");
        }

        // 6. Adaptive Probe Volume: 未存在なら Global モードで生成
        if (Object.FindFirstObjectByType<ProbeVolume>() == null)
        {
            var go = new GameObject("Adaptive Probe Volume");
            var pv = go.AddComponent<ProbeVolume>();
            pv.mode = ProbeVolumeMode.Global;
            Undo.RegisterCreatedObjectUndo(go, "Create Adaptive Probe Volume");
            sceneChanged = true;
            Debug.Log("[RoomFbxPostProcessor] Adaptive Probe Volume を生成しました。");
        }

        // 7. Reflection Probe: 未存在なら生成
        if (Object.FindFirstObjectByType<ReflectionProbe>() == null)
        {
            var go = new GameObject("Reflection Probe (Auto)");
            var rp = go.AddComponent<ReflectionProbe>();
            rp.mode = ReflectionProbeMode.Baked;
            Undo.RegisterCreatedObjectUndo(go, "Create Reflection Probe");
            sceneChanged = true;
            Debug.Log("[RoomFbxPostProcessor] Reflection Probe を生成しました。");
        }

        if (sceneChanged)
            EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene());
    }

    // -------------------------------------------------------
    // ヘルパー
    // -------------------------------------------------------

    private static bool AssignTextures(Material mat, string textureDir)
    {
        string matKey = SanitizeName(mat.name);
        bool changed = false;

        changed |= AssignIfExists(mat, textureDir, matKey, "_BaseMap",     isNormalMap: false);
        changed |= AssignIfExists(mat, textureDir, matKey, "_BumpMap",     isNormalMap: true);
        changed |= AssignIfExists(mat, textureDir, matKey, "_MaskMap",     isNormalMap: false);
        changed |= AssignIfExists(mat, textureDir, matKey, "_EmissionMap", isNormalMap: false);

        if (FindTexturePath(textureDir, matKey, "MaskMap") != null)
        {
            mat.SetFloat("_Metallic",   1f);
            mat.SetFloat("_Smoothness", 1f);
            changed = true;
        }

        if (FindTexturePath(textureDir, matKey, "EmissionMap") != null)
        {
            mat.EnableKeyword("_EMISSION");
            mat.SetColor("_EmissionColor", Color.white);
            changed = true;
        }

        return changed;
    }

    private static bool AssignIfExists(Material mat, string textureDir, string matKey,
                                       string shaderProp, bool isNormalMap)
    {
        string suffix = shaderProp.TrimStart('_');
        string path   = FindTexturePath(textureDir, matKey, suffix);
        if (path == null) return false;

        if (isNormalMap)
        {
            var ti = AssetImporter.GetAtPath(path) as TextureImporter;
            if (ti != null && ti.textureType != TextureImporterType.NormalMap)
            {
                ti.textureType = TextureImporterType.NormalMap;
                ti.sRGBTexture = false;
                ti.SaveAndReimport();
            }
            mat.EnableKeyword("_NORMALMAP");
        }

        var tex = AssetDatabase.LoadAssetAtPath<Texture2D>(path);
        if (tex == null) return false;

        mat.SetTexture(shaderProp, tex);
        return true;
    }

    private static string FindTexturePath(string textureDir, string matKey, string suffix)
    {
        string candidate = $"{textureDir}/{matKey}_{suffix}.png";
        string abs = Path.Combine(
            Directory.GetCurrentDirectory(),
            candidate.Replace('/', Path.DirectorySeparatorChar));
        return File.Exists(abs) ? candidate : null;
    }

    private static string SanitizeName(string name)
        => name.Replace(".", "_").Replace(" ", "_");
}
#endif
