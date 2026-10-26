@echo off
cd /d "%~dp0"

REM 清掉代理设置：GitHub 走 SSH 直连，代理反而会把它带坏
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="

REM SSH 第一次连 github.com 时自动接受主机指纹，不然会卡在 yes/no 提示上
set "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"

REM 找 git：先看 PATH，再看几个常见安装位置（不依赖 PATH 是否已生效）
set "GIT=git"
where git >nul 2>nul
if not errorlevel 1 goto found
if exist "%ProgramFiles%\Git\cmd\git.exe" set "GIT=%ProgramFiles%\Git\cmd\git.exe"
if exist "%ProgramFiles%\Gitin\git.exe" set "GIT=%ProgramFiles%\Gitin\git.exe"
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GIT=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"

:found
"%GIT%" --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [失败] 这台机器上没找到 git。
    echo 装一个 Git for Windows 就好：https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)

echo ============================================
echo    把改动上传到 GitHub
echo ============================================
echo.

REM 提交说明用纯英文：cmd 传给 git 的是 GBK 字节，中文会被当 UTF-8 读出乱码
set "STAMP=%date:~0,10% %time:~0,5%"
"%GIT%" add -A
"%GIT%" commit -m "update %STAMP%"
echo.
"%GIT%" push 2>&1

echo.
echo 上面最后一行如果是  main -^> main  就说明传上去了。
echo 仓库地址：https://github.com/qq147144799/Desktop-Workspace
echo.
pause
