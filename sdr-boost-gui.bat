@echo off
if exist "%~dp0sdr-boost-gui.exe" (
    start "" "%~dp0sdr-boost-gui.exe"
) else (
    start "" pythonw "%~dp0sdr_boost_gui.py"
)
