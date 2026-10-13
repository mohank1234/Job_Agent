' Runs a PowerShell script with no visible window (Task Scheduler would otherwise open a console every time) and appends its output to a log.
' Usage: wscript.exe run-hidden.vbs "<script.ps1>" "<log file>"
Set shell = CreateObject("WScript.Shell")
command = "cmd /c powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & WScript.Arguments(0) & """ >> """ & WScript.Arguments(1) & """ 2>&1"
' 0 = hidden window, True = wait for it to finish (so Task Scheduler's "do not start a new instance" still protects against overlap).
WScript.Quit shell.Run(command, 0, True)
