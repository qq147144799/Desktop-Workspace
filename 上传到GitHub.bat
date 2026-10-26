@echo off
cd /d "%~dp0"

REM 清掉代理设置：GitHub 走 SSH 直连，代理反而会把它带坏
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="

echo ============================================
echo    把改动上传到 GitHub
echo ============================================
echo.

REM 提交说明用纯英文：cmd 传给 git 的是 GBK 字节，中文会被当 UTF-8 读出乱码
set "STAMP=%date:~0,10% %time:~0,5%"
git add -A
git commit -m "update %STAMP%"
echo.
git push 2>&1

echo.
echo 上面最后一行如果是  main -^> main  就说明传上去了。
echo 仓库地址：https://github.com/qq147144799/Desktop-Workspace
echo.
pause
