@echo off
REM Pull training_events.log from connected Android device to the host path

setlocal enabledelayedexpansion

set ADB=%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe
set PKG=com.example.hfl_experiment
set DEVICE_PATH=/sdcard/Android/data/%PKG%/files/training_events.log
set HOST_PATH=%~dp0\training_log.txt

if not exist "%ADB%" (
  echo adb not found at %ADB%
  echo Please install Android SDK Platform Tools and ensure adb is in PATH.
  exit /b 1
)

"%ADB%" shell ls %DEVICE_PATH% >nul 2>&1
if errorlevel 1 (
  echo Failed to locate training log on device. Ensure a real device is connected (adb devices) and the app has written the log.
  exit /b 1
)

"%ADB%" pull %DEVICE_PATH% "%HOST_PATH%"
if errorlevel 1 (
  echo Failed to pull training log. Ensure a real device is connected and the app is debuggable or has file permissions to write logs.
  exit /b 1
)

echo Training log pulled to %HOST_PATH%
exit /b 0
