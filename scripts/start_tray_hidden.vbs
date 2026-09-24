Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectRoot = fso.GetParentFolderName(scriptDir)
venvPythonw = fso.BuildPath(projectRoot, ".venv\Scripts\pythonw.exe")
venvPython = fso.BuildPath(projectRoot, ".venv\Scripts\python.exe")
localAppData = shell.ExpandEnvironmentStrings("%LocalAppData%")
python313w = fso.BuildPath(localAppData, "Programs\Python\Python313\pythonw.exe")
python313 = fso.BuildPath(localAppData, "Programs\Python\Python313\python.exe")
python312w = fso.BuildPath(localAppData, "Programs\Python\Python312\pythonw.exe")
python312 = fso.BuildPath(localAppData, "Programs\Python\Python312\python.exe")
trayApp = fso.BuildPath(scriptDir, "tray_app.py")

Function PythonWorks(path)
  If Not fso.FileExists(path) Then
    PythonWorks = False
    Exit Function
  End If
  command = """" & path & """ --version"
  PythonWorks = (shell.Run(command, 0, True) = 0)
End Function

If PythonWorks(venvPythonw) Then
  pythonExe = venvPythonw
ElseIf PythonWorks(venvPython) Then
  pythonExe = venvPython
ElseIf fso.FileExists(python313w) Then
  pythonExe = python313w
ElseIf fso.FileExists(python313) Then
  pythonExe = python313
ElseIf fso.FileExists(python312w) Then
  pythonExe = python312w
ElseIf fso.FileExists(python312) Then
  pythonExe = python312
Else
  pythonExe = "pythonw"
End If

shell.CurrentDirectory = projectRoot
shell.Run """" & pythonExe & """ """ & trayApp & """", 0, False
