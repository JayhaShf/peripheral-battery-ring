Option Explicit
' Silent launcher for PeripheralBatteryRing (no console window).
Dim fso, sh, dir, args, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " " & Chr(34) & WScript.Arguments(i) & Chr(34)
Next
sh.Run "pythonw.exe main.py" & args, 0, False
