@echo off
title duanyan2api
cd /d "%~dp0"
echo ================================
echo  duanyan2api - 书生端砚 OpenAI 反代
echo  http://127.0.0.1:9095/v1
echo ================================
python server.py
pause
