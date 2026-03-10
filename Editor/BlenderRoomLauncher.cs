#if UNITY_EDITOR
using System.Diagnostics;
using System.IO;
using System.Reflection;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEngine;
using Debug = UnityEngine.Debug;

/// <summary>
/// Unity Editor から Blender をバックグラウンド実行してベイク&FBX 書き出しを行う。
/// Tools > Room Importer > Bake & Export from Blender
///
/// 設計方針: オリジナルの .blend を保護するため、
/// コピーを作成してコピー上で全処理を行い、完了後にコピーを削除する。
/// </summary>
public class BlenderRoomLauncher : EditorWindow
{
    private string _blenderPath    = "/Applications/Blender.app/Contents/MacOS/Blender";
    private string _blendFilePath  = "";
    private string _collectionName = "Root";
    private string _exportDir      = "";
    private bool   _autoRefresh    = true;

    private bool _autoLighting = false;
    private bool _isRunning    = false;

    [MenuItem("Tools/Room Importer/Bake & Export from Blender")]
    private static void Open()
    {
        var win = GetWindow<BlenderRoomLauncher>("Blender Room Launcher");
        win.minSize = new UnityEngine.Vector2(500, 260);

        string projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
        win._blendFilePath = Path.Combine(projectRoot, "SourceAssets", "Room.blend");
        win._exportDir     = Path.Combine(Application.dataPath, "FBX");
    }

    private void OnGUI()
    {
        EditorGUILayout.LabelField("Blender Room Launcher", EditorStyles.boldLabel);
        EditorGUILayout.Space();

        _blenderPath    = EditorGUILayout.TextField("Blender 実行ファイル", _blenderPath);
        _blendFilePath  = FileField("Blend ファイル (.blend)", _blendFilePath, "blend");
        _collectionName = EditorGUILayout.TextField("コレクション名", _collectionName);
        _exportDir      = FolderField("書き出し先フォルダ", _exportDir);
        _autoRefresh    = EditorGUILayout.Toggle("完了後に Unity をリフレッシュ", _autoRefresh);
        _autoLighting   = EditorGUILayout.Toggle("ライティングも自動ベイク（数分かかります）", _autoLighting);

        EditorGUILayout.Space();

        bool canRun = !_isRunning
                      && File.Exists(_blenderPath)
                      && File.Exists(_blendFilePath)
                      && !string.IsNullOrEmpty(_collectionName)
                      && !string.IsNullOrEmpty(_exportDir);

        GUI.enabled = canRun;
        if (GUILayout.Button(_isRunning ? "実行中..." : "Bake & Export", GUILayout.Height(36)))
            RunBlender();
        GUI.enabled = true;

        EditorGUILayout.Space();

        string scriptPath = GetPythonScriptPath();
        string copyPath   = _blendFilePath + ".__export_tmp.blend";
        EditorGUILayout.HelpBox(
            "実行内容:\n" +
            $"1. {Path.GetFileName(_blendFilePath)} → {Path.GetFileName(copyPath)} にコピー\n" +
            $"2. blender --background {Path.GetFileName(copyPath)} --python {Path.GetFileName(scriptPath)} -- \"{_collectionName}\" \"{_exportDir}\"\n" +
            "3. 完了後にコピーを削除",
            MessageType.Info);
    }

    private void RunBlender()
    {
        string scriptPath = GetPythonScriptPath();

        if (!File.Exists(scriptPath))
        {
            Debug.LogError($"[BlenderRoomLauncher] スクリプトが見つかりません: {scriptPath}");
            return;
        }

        // オリジナル .blend のコピーを作成して、コピー上で処理する
        string copyPath = _blendFilePath + ".__export_tmp.blend";
        try
        {
            File.Copy(_blendFilePath, copyPath, overwrite: true);
        }
        catch (System.Exception ex)
        {
            Debug.LogError($"[BlenderRoomLauncher] .blend コピー失敗: {ex.Message}");
            return;
        }

        string args = $"--background \"{copyPath}\" --python \"{scriptPath}\"" +
                      $" -- \"{_collectionName}\" \"{_exportDir}\"";

        var psi = new ProcessStartInfo
        {
            FileName               = _blenderPath,
            Arguments              = args,
            UseShellExecute        = false,
            RedirectStandardOutput = true,
            RedirectStandardError  = true,
            CreateNoWindow         = true,
        };

        _isRunning = true;
        Repaint();
        Debug.Log($"[BlenderRoomLauncher] 起動: {_blenderPath} {args}");

        var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        proc.OutputDataReceived += (_, e) => { if (e.Data != null) Debug.Log("[Blender] " + e.Data); };
        proc.ErrorDataReceived  += (_, e) => { if (e.Data != null) Debug.LogWarning("[Blender] " + e.Data); };
        proc.Exited += (_, __) =>
        {
            // コピー .blend を削除 (オリジナルは一切変更されていない)
            try { if (File.Exists(copyPath)) File.Delete(copyPath); }
            catch (System.Exception ex) { Debug.LogWarning($"[BlenderRoomLauncher] コピー削除失敗: {ex.Message}"); }

            Debug.Log($"[BlenderRoomLauncher] 完了 (exit={proc.ExitCode})");

            bool bakeLighting = _autoLighting;
            EditorApplication.delayCall += () =>
            {
                _isRunning = false;
                Repaint();
                if (_autoRefresh)
                    AssetDatabase.Refresh();
                if (bakeLighting)
                {
                    bool started = Lightmapping.BakeAsync();
                    if (!started)
                        Debug.LogWarning("[BlenderRoomLauncher] Lightmapping.BakeAsync() を開始できませんでした。Lighting Settings を確認してください。");
                    else
                        Debug.Log("[BlenderRoomLauncher] Generate Lighting を開始しました。");
                }
            };
        };

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
    }

    /// <summary>
    /// パッケージ内の SourceAssets~ フォルダにある Python スクリプトの絶対パスを返す。
    /// </summary>
    private static string GetPythonScriptPath()
    {
        var pkgInfo = PackageInfo.FindForAssembly(Assembly.GetExecutingAssembly());
        if (pkgInfo != null)
            return Path.Combine(pkgInfo.resolvedPath, "SourceAssets~",
                "blender_collection_hierarchy_fbx_export.py");

        // フォールバック: プロジェクトルートの SourceAssets/ (開発時など)
        string projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
        return Path.Combine(projectRoot, "SourceAssets",
            "blender_collection_hierarchy_fbx_export.py");
    }

    private static string FileField(string label, string path, string ext)
    {
        EditorGUILayout.BeginHorizontal();
        string result = EditorGUILayout.TextField(label, path);
        if (GUILayout.Button("...", GUILayout.Width(32)))
        {
            string dir = string.IsNullOrEmpty(path) ? "" : Path.GetDirectoryName(path) ?? "";
            string picked = EditorUtility.OpenFilePanel(label, dir, ext);
            if (!string.IsNullOrEmpty(picked)) result = picked;
        }
        EditorGUILayout.EndHorizontal();
        return result;
    }

    private static string FolderField(string label, string path)
    {
        EditorGUILayout.BeginHorizontal();
        string result = EditorGUILayout.TextField(label, path);
        if (GUILayout.Button("...", GUILayout.Width(32)))
        {
            string picked = EditorUtility.OpenFolderPanel(label, path, "");
            if (!string.IsNullOrEmpty(picked)) result = picked;
        }
        EditorGUILayout.EndHorizontal();
        return result;
    }
}
#endif
