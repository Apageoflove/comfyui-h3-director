# 重启 H3 制片流水线：按命令行精确匹配旧实例，避免误杀其他 python 进程
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*video_pipeline.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
# H3 官方 h3-prompt-writing skill：后续重启也自动注入；设为 0 可临时关闭
$env:H3_PROMPT_SKILL = '1'
Start-Process -FilePath 'D:\ComfyUI\venv\Scripts\pythonw.exe' `
  -ArgumentList 'D:\H3DirectorDesk\video_pipeline.py' -WindowStyle Hidden
Write-Output 'pipeline restarted: http://127.0.0.1:8898'
