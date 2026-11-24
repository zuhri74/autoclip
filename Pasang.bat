@echo off
cd /d "%~dp0"
echo Memasang komponen AutoClip (sekali saja, butuh internet)...
python -m pip install -U -r requirements.txt
echo.
echo Selesai. Jalankan "AutoClip Studio.bat" untuk mulai.
pause
