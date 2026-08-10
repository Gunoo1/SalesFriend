# SalesAgent — Windows container (Remerch/Demo conventions: servercore base
# matching the VM build; switch ltsc2019 -> ltsc2022 if the host is Server 2022).
FROM mcr.microsoft.com/windows/servercore:ltsc2019

SHELL ["powershell", "-Command", "$ErrorActionPreference = 'Stop';"]

# Python 3.12 (silent install, all users, on PATH)
RUN Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile 'C:\\py.exe'; \
    Start-Process C:\\py.exe -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait; \
    Remove-Item C:\\py.exe

WORKDIR C:/app
ENV PYTHONIOENCODING=utf-8

# requirements first for layer caching
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY salesagent/ salesagent/
COPY static/ static/
COPY run.py .

# Volumes (Windows containers can't bind-mount single files — mount DIRS):
#   -v C:\Apps\salesagent\data:C:/data      -e DATA_DIR=C:/data
#   -v C:\Apps\salesagent\prompts:C:/prompts -e PROMPTS_DIR=C:/prompts
# That's ALL the state. No external databases to copy in: the app builds its
# own reference estate under C:/data/estate on first use (ask the agent to
# run k12_build_reference — fresh public NCES/CRDC download, a few minutes).

EXPOSE 8504
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD powershell -Command "try { $r = Invoke-WebRequest -UseBasicParsing http://localhost:8504/api/health; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"

# Run:
#   docker run -d --name salesagent -p 8504:8504 --restart always ^
#     --env-file C:\Apps\salesagent\.env ^
#     -v C:\Apps\salesagent\data:C:/data -v C:\Apps\salesagent\prompts:C:/prompts ^
#     -e DATA_DIR=C:/data -e PROMPTS_DIR=C:/prompts salesagent
CMD ["python", "-m", "uvicorn", "salesagent.web.app:app", "--host", "0.0.0.0", "--port", "8504", "--workers", "1"]
