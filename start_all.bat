@echo off
echo Starting Sports Betting System...
start "Sports Dashboard" cmd /k python sports_server.py
timeout /t 3 /nobreak >nul
start "Agent Pipeline" cmd /k start_agents.bat
timeout /t 2 /nobreak >nul
start chrome http://localhost:8050
echo Done. Dashboard: http://localhost:8050
