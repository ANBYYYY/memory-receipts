# Best-effort Chinese explanation for the "no interpreter" screen.
#
# Started by START-HERE.cmd only AFTER the ASCII block has already been printed,
# so if this file cannot run at all, the reader still saw a self-sufficient
# message. It must never be the only thing that speaks.
#
# Stored as UTF-8 WITH BOM: that is this house's standard format for .ps1 under
# PowerShell 5.1 (without the BOM, 5.1 reads the file as ANSI and mangles it).
# It is not a new encoding surface -- it is the one we already use everywhere.
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

Write-Host ''
Write-Host '----------------------------------------------------------------------'
Write-Host '中文说明'
Write-Host ''
Write-Host '  这台机器上没找到能用的 Python。'
Write-Host '  这不是你的环境坏了，也不是你做错了什么 —— 演示只是需要一个 Python 来跑。'
Write-Host ''
Write-Host '  装一个（复制粘贴一行）：'
Write-Host '      winget install -e --id Python.Python.3.12'
Write-Host ''
Write-Host '  或者你已经装了、只是不在 PATH 上：把完整路径告诉这个文件'
Write-Host '      set MEMORY_PACK_PYTHON=C:\你的路径\python.exe'
Write-Host '      START-HERE.cmd'
Write-Host ''
Write-Host '  装完之后：关掉这个窗口，重新开一个新窗口，再跑一次 START-HERE.cmd。'
Write-Host '  一定要新窗口 —— 这个窗口的 PATH 不会变。'
Write-Host ''
Write-Host '  一个已知的坑：本机可能装了「微软商店的 python 占位程序」，它叫 python，'
Write-Host '  但已经不是解释器了（跑 python -V 什么都不打印）。本脚本会自动跳过它。'
Write-Host '----------------------------------------------------------------------'
