@echo off
if exist "%~dp0sdr-boost.exe" (
    "%~dp0sdr-boost.exe" %*
) else (
    python "%~dp0sdr_boost.py" %*
)
