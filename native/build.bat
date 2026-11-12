@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
cd /d "C:\Users\Administrator\.dsh\kxcam"
set INC=E:\tools\agora-cpp-sdk\Agora_Native_SDK_for_Windows_FULL\sdk\high_level_api\include
set INC2=E:\tools\agora-cpp-sdk\Agora_Native_SDK_for_Windows_FULL\sdk\low_level_api\include
set LIBP=E:\tools\agora-cpp-sdk\Agora_Native_SDK_for_Windows_FULL\sdk\x86_64
cl /nologo /EHsc /std:c++17 /W3 /MD /I%INC% /I%INC2% kx_cam.cpp /Fe:kx_cam.exe /link /LIBPATH:%LIBP% agora_rtc_sdk.dll.lib gdiplus.lib user32.lib
echo EXITCODE=%ERRORLEVEL%
