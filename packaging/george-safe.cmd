@echo off
REM Launches George with the cairo renderer instead of OpenGL.
REM Use this if the window comes up black or the HUD does not draw:
REM some integrated-graphics drivers cannot give GTK4 the GL context
REM its default renderer wants.
set GEORGE_SAFE_GRAPHICS=1
start "" "%~dp0George.exe" %*
