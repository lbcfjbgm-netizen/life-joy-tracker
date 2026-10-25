' Silent launcher for Steam Tracker - no console window is shown.
' Double click, wait a few seconds, then open http://127.0.0.1:8001
Option Explicit

Dim fso, shell, root, pyw, script

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = root & "\.venv\Scripts\pythonw.exe"
script = root & "\start_silent.py"

If Not fso.FileExists(pyw) Then
    MsgBox "Python not found: " & pyw, 16, "Start failed"
    WScript.Quit 1
End If

If Not fso.FileExists(script) Then
    MsgBox "Launcher not found: " & script, 16, "Start failed"
    WScript.Quit 1
End If

' 0 = hidden window, False = do not wait for it to exit
shell.Run """" & pyw & """ """ & script & """", 0, False