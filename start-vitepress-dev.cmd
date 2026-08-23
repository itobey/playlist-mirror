@echo off
setlocal

cd /d "%~dp0docs" || exit /b 1
npm run docs:dev -- --host

