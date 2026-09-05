Write-Host "Starting Financial-Report Research Copilot..."

function Stop-Port($port) {
    $lines = netstat -ano | Select-String ":$port\s.*LISTENING"
    if ($lines) {
        $pids = $lines | ForEach-Object { ($_ -split "\s+")[-1] } | Sort-Object -Unique
        foreach ($p in $pids) {
            taskkill /PID $p /F 2>$null
        }
        Start-Sleep 1
    }
}

# Kill whatever is on the API and frontend ports — a leftover Streamlit process
# on 8501 survives a normal rerun and silently serves stale code (pages/ added
# after it started won't be registered), so it must be killed here too, not
# just 8000.
Stop-Port 8000
Stop-Port 8501

# API
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot'; uv run uvicorn copilot.api:app --port 8000"

Start-Sleep 2

# Frontend
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot'; uv run streamlit run frontend.py --server.port 8501 --server.headless true --browser.gatherUsageStats false"

Start-Sleep 2

Start-Process "http://localhost:8501"
Write-Host "Done. Frontend: http://localhost:8501"
