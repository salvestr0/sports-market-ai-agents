@echo off
:loop
echo [%date% %time%] Starting agent pipeline...
python -m agents.runner
echo [%date% %time%] Runner exited (code %errorlevel%), restarting in 15s...
timeout /t 15 /nobreak >nul
goto loop
