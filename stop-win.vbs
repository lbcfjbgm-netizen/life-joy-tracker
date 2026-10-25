' Silent stopper for Steam Tracker - no console window is shown.
Option Explicit

Dim fso, shell, root, pyw, script

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = root & "\.venv\Scripts\pythonw.exe"
script = root & "\stop_silent.py"

If Not fso.FileExists(pyw) Then
    MsgBox "Python not found: " & pyw, 16, "Stop failed"
    WScript.Quit 1
End If

shell.Run """" & pyw & """ """ & script & """", 0, False